# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""TieredCasePoolDriver — the concrete MLFQ-style scheduling driver atop CasePoolDriver.

A ``FolderBackedCase`` knows how to take ONE forward step (``case_advance()``); it does
not drive a fleet. This driver owns the scheduling policy and the aggregate view:

1. Run cases — call ``case_advance()`` on the right cases at a controlled cadence.
2. Aggregate view — be the one place that sees the whole fleet's flow (events + queries).

It implements every abstract method of the approved ``CasePoolDriver`` ABC and adds a few
derived-class extensions (``peek``, ``by_tier``, ``snapshot``, ``find_by_external_key``,
``settle``). The ABC is left untouched.

SCHEDULING MODEL (Multi-Level Feedback Queue)
---------------------------------------------
Each case occupies a ``_Slot`` carrying a tier label (HOT / WARM / COLD) and a per-slot
``skip_countdown`` of beats until its next step. A single per-beat sweep decrements every
live slot's countdown and launches the step when it reaches zero, reloading the countdown
from the tier's ``reset_multiple``. Tiers are chosen purely from observed behaviour
(``AdvanceResult``) by a replaceable ``_TierPolicy`` — the driver never introspects domain
logic:

- progress (auto step or ``fire()``)        -> HOT, streaks reset
- failure (a transition raised)             -> held WARM with a lengthening backoff
- no-op (guard declined / blocked)          -> normal no-op-streak demotion ladder
- structural dead-end (not ``advanceable``) -> accelerated demotion to COLD

COLD is slow-but-still-polled (a watch loop for out-of-band change), never frozen.
Dormancy (``skip_countdown <= 0``) is reserved for closed and in-flight cases only.

PACING
------
The beat is the ABC's ``advance()``. It sweeps once, runs a heartbeat slice (lease
keepalive, §6), then applies an async-friendly smoothing delay (``asyncio.sleep(I0)``).
Tests pass ``advance(suggested_interval_secs=0.0)`` to drop the delay; because the actual
case steps run as background tasks, ``await settle()`` awaits them to a quiescent point.

This driver deliberately does NO disk/folder management: cases arrive already bound (live,
lease-held) and leave the same way. The membership journal / crash recovery (design §8b) is
a separate observer that rides the event stream and is out of scope here.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Hashable, Iterator, Optional

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.case_pool_driver import (
    CasePoolDriver, CasePoolEvent, CasePoolEventNames,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import (
    DEFAULT_LEASE_TTL_SECS,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError, CaseInFlightError, CaseTypeMismatchError,
    DetachedCaseError, OwnershipLostError,
)

logger = logging.getLogger(__name__)

# Sentinel: a slot with skip_countdown <= 0 is DORMANT (closed or in-flight) — never
# decremented or fired. A live item never rests at 0 (it fires and reloads), so the
# sentinel is unambiguous.
_DORMANT = -1


class Tier(enum.Enum):
    """Scheduling tier of a slot — a label that drives its cadence (HOT fastest, COLD
    slowest). Promotion/demotion mutates this in place; slots never move."""
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


# ---------------------------------------------------------------------------
# Tier policy (the replaceable promotion/demotion rules, design §4 / §4b)
# ---------------------------------------------------------------------------

@dataclass
class _TierPolicy:
    """The promotion/demotion rules and timing tunables as data, so thresholds and the
    kind->effect mapping can be swapped or subclassed without touching the sweep.

    All intervals are integer multiples ``M`` of the base beat ``I0``; a slot fires every
    ``reset_multiple`` beats. The PLACEHOLDER values come straight from design §4b and are
    expected to be tuned against real workloads.
    """
    # Cadence
    I0: float = 0.5                       # base beat / hot period (seconds)
    M_HOT: int = 1
    M_WARM: int = 10                      # 5 s at I0=0.5
    M_COLD: int = 120                     # 60 s at I0=0.5
    HEARTBEAT_WALK_PERIOD: float = 10.0   # a full heartbeat lap completes this often (< TTL)

    # No-op streak demotion ladder
    K_HOT_TO_WARM: int = 3
    K_WARM_TO_COLD: int = 5
    # Accelerated ladder for a structural dead-end (not advanceable)
    K_HOT_TO_WARM_ACCEL: int = 1
    K_WARM_TO_COLD_ACCEL: int = 2

    # Failure backoff: keep WARM, lengthen the reload as fails accrue, capped below M_COLD
    # so a backing-off warm case never out-waits a genuine cold case.
    FAIL_BACKOFF_CAP: int = 100

    def base_multiple(self, tier: Tier) -> int:
        return {Tier.HOT: self.M_HOT, Tier.WARM: self.M_WARM, Tier.COLD: self.M_COLD}[tier]

    def admission_tier(self, case: FolderBackedCase) -> Tier:
        """Initial tier for a freshly added (open) case: default HOT, but a structural
        dead-end (no auto exits) starts WARM. (Closed cases never enter rotation.)"""
        return Tier.HOT if case.case_advanceable else Tier.WARM

    def reclassify(self, slot: "_Slot", result: AdvanceResult) -> None:
        """Mutate ``slot`` (tier / reset_multiple / streaks) from the latest result.

        Only ever called for a NON-closed case (closed cases are made dormant by the driver
        and never reclassified). Implements the kind-aware effect table of design §4.
        """
        if result.progressed:
            slot.tier = Tier.HOT
            slot.reset_multiple = self.M_HOT
            slot.noop_streak = 0
            slot.fail_streak = 0
            return
        if result.failed:
            # A transition raised: hold WARM and back off. @FAIL>=n divert edges only ripen
            # as fails accrue, so we keep retrying rather than demoting to cold.
            slot.tier = Tier.WARM
            slot.fail_streak += 1
            slot.noop_streak = 0
            slot.reset_multiple = min(self.M_WARM * slot.fail_streak, self.FAIL_BACKOFF_CAP)
            return
        # A no-op: guard declined, blocked, or a structural dead-end. Walk the streak ladder;
        # a structural dead-end (no auto exits at all) demotes on the accelerated thresholds.
        slot.fail_streak = 0
        slot.noop_streak += 1
        accelerated = not slot.case.case_advanceable
        self._apply_noop_ladder(slot, accelerated=accelerated)

    def _apply_noop_ladder(self, slot: "_Slot", *, accelerated: bool) -> None:
        k_hot_to_warm = self.K_HOT_TO_WARM_ACCEL if accelerated else self.K_HOT_TO_WARM
        k_warm_to_cold = self.K_WARM_TO_COLD_ACCEL if accelerated else self.K_WARM_TO_COLD
        if slot.tier is Tier.HOT and slot.noop_streak >= k_hot_to_warm:
            slot.tier = Tier.WARM
            slot.reset_multiple = self.M_WARM
            slot.noop_streak = 0
        elif slot.tier is Tier.WARM and slot.noop_streak >= k_warm_to_cold:
            slot.tier = Tier.COLD
            slot.reset_multiple = self.M_COLD
            slot.noop_streak = 0
        else:
            # No demotion this pass: keep the tier, ensure the reload is the tier base
            # (clears any stale warm-backoff multiple once failures stop).
            slot.reset_multiple = self.base_multiple(slot.tier)


# ---------------------------------------------------------------------------
# Slot (design §8a) + read-only peek view
# ---------------------------------------------------------------------------

@dataclass
class _Slot:
    """Per-case scheduling state. Slots never move: inserted once, removed once;
    promote/demote is in-place mutation of ``tier`` / ``reset_multiple``."""
    case: FolderBackedCase
    tier: Tier
    reset_multiple: int                  # countdown reload (tier base, or warm backoff)
    skip_countdown: int                  # beats to next step; <= 0 == dormant
    noop_streak: int = 0
    fail_streak: int = 0
    in_flight: bool = False
    closed: bool = False
    halt_requested: bool = False
    halt_settled: bool = False           # HALTED event has fired
    last_result: Optional[AdvanceResult] = None
    last_advanced_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None
    task: Optional[asyncio.Task] = None  # the in-flight case-step task, if any


@dataclass(frozen=True)
class CasePeek:
    """A cheap point-in-time view of one case's scheduling state (extension surface)."""
    case_folder: Path
    case_id: str
    state: str
    tier: Tier
    in_flight: bool
    closed: bool
    halt_requested: bool
    noop_streak: int
    fail_streak: int
    skip_countdown: int
    last_result: Optional[AdvanceResult]


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class TieredCasePoolDriver(CasePoolDriver):
    """Concrete MLFQ scheduling driver over a pool of FolderBackedCase objects.

    Identity is the case folder path. ``_by_folder`` is THE STORE (the sweep iterates its
    values); ``_by_case_id`` / ``_by_external_key`` are optional derived indexes pointing at
    the same slots. See the module docstring for the scheduling model.
    """

    def __init__(
        self,
        *,
        policy: Optional[_TierPolicy] = None,
        concurrency_ceiling: int = 50,
    ) -> None:
        super().__init__()
        self._policy = policy or _TierPolicy()
        self._ceiling = concurrency_ceiling

        self._by_folder: dict[Path, _Slot] = {}
        self._by_case_id: dict[str, _Slot] = {}
        self._by_external_key: dict[str, list[_Slot]] = {}

        self._in_flight_count = 0
        self._stagger_counter = 0          # scatters same-tier items across phases
        self._hb_cursor = 0                # heartbeat walk cursor

        # handle -> (frozenset of event-name strings, callback)
        self._subs: dict[Hashable, tuple[frozenset[str], Callable[[CasePoolEvent], None]]] = {}

    # -- Container read interface ------------------------------------------

    def __len__(self) -> int:
        return len(self._by_folder)

    def __contains__(self, case_folder: object) -> bool:
        return case_folder in self._by_folder

    def __iter__(self) -> Iterator[FolderBackedCase]:
        return iter([slot.case for slot in self._by_folder.values()])

    def __getitem__(self, case_folder: Path) -> FolderBackedCase:
        return self._by_folder[case_folder].case

    # -- Membership --------------------------------------------------------

    def add(self, case: FolderBackedCase) -> None:
        folder = case.case_folder
        if folder in self._by_folder:
            raise ValueError(f"A case for folder {folder} is already in the pool.")
        if case.case_is_detached:
            # Admission must not silently swap the caller's object — reject, don't rehydrate.
            raise DetachedCaseError(folder)

        if case.case_is_closed:
            # Closed on arrival: dormant immediately, never enters rotation; heartbeat walk
            # keeps the lease warm until removed.
            slot = _Slot(
                case=case, tier=Tier.COLD, reset_multiple=self._policy.M_COLD,
                skip_countdown=_DORMANT, closed=True,
            )
        else:
            tier = self._policy.admission_tier(case)
            reset_multiple = self._policy.base_multiple(tier)
            slot = _Slot(
                case=case, tier=tier, reset_multiple=reset_multiple,
                skip_countdown=self._staggered_countdown(reset_multiple),
            )

        self._by_folder[folder] = slot
        self._index_add(slot)
        self._emit(CasePoolEventNames.ADMITTED, case)

    def request_halt(self, case_folder: Path) -> None:
        slot = self._by_folder[case_folder]
        slot.halt_requested = True
        if not slot.in_flight:
            # Already settled: stop scheduling and announce HALTED now.
            slot.skip_countdown = _DORMANT
            self._settle_halt(slot)
        # If in-flight, the step's completion path stops scheduling and fires HALTED.

    def remove(self, case_folder: Path) -> FolderBackedCase:
        slot = self._by_folder[case_folder]
        if slot.in_flight:
            raise CaseInFlightError(case_folder)
        self._index_remove(slot)
        del self._by_folder[case_folder]
        self._emit(CasePoolEventNames.REMOVED, slot.case)
        return slot.case

    # -- Driving (the beat) ------------------------------------------------

    async def advance(self, suggested_interval_secs: float | None = None) -> None:
        self._sweep_once()
        self._heartbeat_slice()
        delay = self._policy.I0 if suggested_interval_secs is None else suggested_interval_secs
        if delay and delay > 0:
            await asyncio.sleep(delay)

    def _sweep_once(self) -> None:
        # Snapshot values(): a launch or eviction may mutate the dict mid-sweep.
        for slot in list(self._by_folder.values()):
            if slot.skip_countdown <= 0:          # dormant (closed / in-flight)
                continue
            slot.skip_countdown -= 1
            if slot.skip_countdown != 0:
                continue
            if slot.halt_requested:               # defensive: halt should already be dormant
                slot.skip_countdown = _DORMANT
                continue
            if self._in_flight_count >= self._ceiling:
                slot.skip_countdown = 1           # backpressure: retry next beat
                continue
            if self._live_or_evict(slot):         # rehydrate-or-evict BEFORE touching the case
                self._launch_case_step(slot)

    # -- Manual driving ----------------------------------------------------

    async def fire(
        self, case_folder: Path, trigger: str | None, **trigger_kwargs: Any
    ) -> AdvanceResult:
        slot = self._by_folder[case_folder]
        if slot.in_flight and slot.task is not None:
            # Already mid-transition: hand back the in-progress result, don't launch anew.
            return await slot.task
        if not self._live_or_evict(slot):
            raise KeyError(case_folder)           # evicted during rehydrate
        # With a trigger, pass the kwargs dict as-is (a pinned MANUAL edge requires an
        # explicit bag, even {}); with no trigger, pass None so case_advance() does the
        # auto sweep (a kwargs bag without a trigger is misuse there).
        pass_kwargs = trigger_kwargs if trigger is not None else None
        task = self._launch_case_step(slot, trigger, pass_kwargs)
        return await task

    def boost(self, case_folder: Path) -> None:
        slot = self._by_folder[case_folder]
        if slot.closed or slot.halt_requested:
            return                                # nothing to nudge
        if not slot.in_flight:
            slot.skip_countdown = 1               # fire next beat; tier/cadence unchanged

    # -- Case-step launch + completion ------------------------------------

    def _launch_case_step(
        self, slot: _Slot, trigger: str | None = None, trigger_kwargs: dict | None = None,
    ) -> asyncio.Task:
        slot.in_flight = True
        slot.skip_countdown = _DORMANT
        self._in_flight_count += 1
        task = asyncio.ensure_future(self._run_case_step(slot, trigger, trigger_kwargs))
        slot.task = task
        task.add_done_callback(self._consume_task)
        return task

    async def _run_case_step(
        self, slot: _Slot, trigger: str | None, trigger_kwargs: dict | None,
    ) -> AdvanceResult:
        """Run one case step and perform ALL post-processing within the task, so that when
        the task completes the slot is fully reclassified and events have fired (keeps tests
        deterministic via ``settle()``)."""
        case = slot.case
        try:
            result = await case.case_advance(trigger, trigger_kwargs)
        except OwnershipLostError as err:
            # Fatal ownership breach mid-step: give up on this case.
            result = AdvanceResult(case.case_state, case.case_state, exceptions=(err,))
            self._finish_in_flight(slot)
            self._evict(slot, reason=err)
            return result
        except BaseException:
            # Misuse (e.g. ValueError for a bad trigger via fire()) or an unexpected error:
            # never wedge the slot. Clear in-flight, give it a normal reload, and re-raise to
            # any awaiter (fire()); beat-launched tasks have their exception consumed.
            self._finish_in_flight(slot)
            if not slot.halt_requested and not slot.closed:
                slot.skip_countdown = max(1, slot.reset_multiple)
            raise
        self._complete_step(slot, result)
        return result

    def _complete_step(self, slot: _Slot, result: AdvanceResult) -> None:
        self._finish_in_flight(slot)
        slot.last_result = result
        if result.progressed:
            slot.last_advanced_at = time.monotonic()

        closed_now = slot.case.case_is_closed
        if closed_now:
            slot.closed = True
        else:
            old_reset = slot.reset_multiple
            self._policy.reclassify(slot, result)
            # Phase-stagger when the cadence changed (add/demote); otherwise reload the
            # true interval so scattered items keep their phase.
            if slot.reset_multiple != old_reset:
                slot.skip_countdown = self._staggered_countdown(slot.reset_multiple)
            else:
                slot.skip_countdown = max(1, slot.reset_multiple)

        self._emit_advance_events(slot, result)

        if slot.halt_requested:
            slot.skip_countdown = _DORMANT
            self._settle_halt(slot)
        elif closed_now:
            slot.skip_countdown = _DORMANT

    def _finish_in_flight(self, slot: _Slot) -> None:
        if slot.in_flight:
            slot.in_flight = False
            self._in_flight_count -= 1
        slot.task = None

    def _consume_task(self, task: asyncio.Task) -> None:
        """Done-callback so a beat-launched task's exception is always retrieved (no
        'Task exception was never retrieved' warning). Awaiters of ``fire()`` still see it."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("case-step task ended with exception: %r", exc)

    def _settle_halt(self, slot: _Slot) -> None:
        if not slot.halt_settled:
            slot.halt_settled = True
            self._emit(CasePoolEventNames.HALTED, slot.case)

    def _emit_advance_events(self, slot: _Slot, result: AdvanceResult) -> None:
        # Order within a beat: ALERTED -> ADVANCED -> CLOSED. FAILED (mutually exclusive with
        # ADVANCED for a single result) rides between them.
        case = slot.case
        if result.alerted:
            self._emit(CasePoolEventNames.ALERTED, case, result)
        if result.progressed:
            self._emit(CasePoolEventNames.ADVANCED, case, result)
        if result.failed:
            self._emit(CasePoolEventNames.FAILED, case, result)
        if slot.closed and result.progressed:
            # CLOSED is always preceded by ADVANCED in the same beat.
            self._emit(CasePoolEventNames.CLOSED, case, result)

    # -- Detach recovery + eviction (design §8 / §8a) ---------------------

    def _live_or_evict(self, slot: _Slot) -> bool:
        """Ensure ``slot.case`` is live; return False (and evict) if it can't be made live.

        Cheap non-raising precheck first (the common path); on detachment, rehydrate a fresh
        object and adopt it into the slot, keeping the slot — and the case's identity —
        stable. Eviction only when rehydration is impossible.
        """
        if not slot.case.case_is_detached:
            return True
        folder = slot.case.case_folder
        try:
            fresh = case_type_registry.rehydrate(folder)
        except (FileNotFoundError, CaseAlreadyOpenError, CaseTypeMismatchError) as err:
            self._evict(slot, reason=err)
            return False
        # Adopt the fresh object; re-point any derived indexes at the same slot.
        self._index_remove(slot)
        slot.case = fresh
        self._index_add(slot)
        return True

    def _evict(self, slot: _Slot, *, reason: BaseException) -> None:
        folder = slot.case.case_folder
        logger.warning("TieredCasePoolDriver evicting case %s: %r", folder, reason)
        self._index_remove(slot)
        self._by_folder.pop(folder, None)
        self._emit(CasePoolEventNames.EVICTED, slot.case)

    # -- Heartbeat walk (design §6) ---------------------------------------

    def _heartbeat_slice(self) -> None:
        """Walk a slice of the store, beating each retained case's lease. A full lap
        completes in ~HEARTBEAT_WALK_PERIOD (well inside the TTL). Every retained case is
        walked — including closed-awaiting-removal — through the same _live_or_evict
        chokepoint so a quietly-detached idle case is rehydrated or evicted here."""
        n = len(self._by_folder)
        if n == 0:
            return
        beats_per_lap = max(1, round(self._policy.HEARTBEAT_WALK_PERIOD / self._policy.I0))
        slice_size = max(1, math.ceil(n / beats_per_lap))
        folders = list(self._by_folder.keys())
        for _ in range(slice_size):
            if not folders:
                break
            folder = folders[self._hb_cursor % len(folders)]
            self._hb_cursor += 1
            slot = self._by_folder.get(folder)
            if slot is None:
                continue
            if not self._live_or_evict(slot):
                continue
            try:
                slot.case.case_heartbeat()
                slot.last_heartbeat_at = time.monotonic()
            except OwnershipLostError as err:
                self._evict(slot, reason=err)

    # -- Lookup ------------------------------------------------------------

    def find(self, case_folder: Path) -> FolderBackedCase:
        return self._by_folder[case_folder].case

    def find_by_external_key(self, key: str) -> list[FolderBackedCase]:
        """Cases whose record carries ``external_key == key`` (not unique -> list).
        Extension: lives on the derived class, not the ABC."""
        return [slot.case for slot in self._by_external_key.get(key, [])]

    # -- Queries -----------------------------------------------------------

    def halted_cases(self) -> list[FolderBackedCase]:
        return [
            slot.case for slot in self._by_folder.values()
            if slot.halt_requested and not slot.halt_settled
        ]

    def in_flight_cases(self) -> list[FolderBackedCase]:
        return [slot.case for slot in self._by_folder.values() if slot.in_flight]

    def blocked_cases(self) -> list[FolderBackedCase]:
        out: list[FolderBackedCase] = []
        for slot in self._by_folder.values():
            last_blocked = slot.last_result is not None and slot.last_result.blocked
            if last_blocked or not slot.case.case_advanceable:
                out.append(slot.case)
        return out

    def closed_cases(self) -> list[FolderBackedCase]:
        # Override the O(N) ABC default with the cached slot flag.
        return [slot.case for slot in self._by_folder.values() if slot.closed]

    # -- Events ------------------------------------------------------------

    def case_event_subscribe(
        self,
        events: str | set[str],
        callback: Callable[[CasePoolEvent], None],
        handle: Hashable | None = None,
    ) -> Hashable:
        names = {events} if isinstance(events, str) else set(events)
        event_set = frozenset(self._evname(e) for e in names)
        if handle is None:
            handle = uuid.uuid4()
        if handle in self._subs:
            raise ValueError(f"Subscription handle {handle!r} is already in use.")
        self._subs[handle] = (event_set, callback)
        return handle

    def case_event_unsubscribe(self, handle: Hashable) -> None:
        del self._subs[handle]

    def _emit(
        self,
        event: str,
        case: FolderBackedCase,
        advance_result: AdvanceResult | None = None,
    ) -> None:
        name = self._evname(event)
        for handle, (event_set, callback) in list(self._subs.items()):
            if name in event_set:
                callback(CasePoolEvent(
                    event=name, case=case, handle=handle, advance_result=advance_result,
                ))

    @staticmethod
    def _evname(event: object) -> str:
        return event.value if isinstance(event, enum.Enum) else str(event)

    # -- Extensions (diagnostics) -----------------------------------------

    def peek(self, case_folder: Path) -> CasePeek:
        """A cheap point-in-time view of one case's scheduling state (extension)."""
        slot = self._by_folder[case_folder]
        return CasePeek(
            case_folder=case_folder,
            case_id=slot.case.case_id,
            state=slot.case.case_state,
            tier=slot.tier,
            in_flight=slot.in_flight,
            closed=slot.closed,
            halt_requested=slot.halt_requested,
            noop_streak=slot.noop_streak,
            fail_streak=slot.fail_streak,
            skip_countdown=slot.skip_countdown,
            last_result=slot.last_result,
        )

    def by_tier(self) -> dict[str, int]:
        """Count of cases per tier (extension)."""
        counts = {tier.value: 0 for tier in Tier}
        for slot in self._by_folder.values():
            counts[slot.tier.value] += 1
        return counts

    def snapshot(self) -> dict[str, Any]:
        """Pull-based aggregate diagnostics (extension)."""
        in_flight = sum(1 for s in self._by_folder.values() if s.in_flight)
        closed = sum(1 for s in self._by_folder.values() if s.closed)
        blocked = len(self.blocked_cases())
        oldest_dwell_secs = 0.0
        oldest_case: Optional[Path] = None
        for slot in self._by_folder.values():
            dwell = slot.case.case_dwell_secs
            if dwell > oldest_dwell_secs:
                oldest_dwell_secs = dwell
                oldest_case = slot.case.case_folder
        return {
            "total": len(self._by_folder),
            "by_tier": self.by_tier(),
            "in_flight": in_flight,
            "closed": closed,
            "blocked": blocked,
            "lease_ttl_secs": DEFAULT_LEASE_TTL_SECS,
            "oldest_dwell_secs": oldest_dwell_secs,
            "oldest_dwell_case": oldest_case,
        }

    # -- Lifecycle ---------------------------------------------------------

    async def stop(self) -> None:
        """Stop the beat loop, then drain any in-flight steps so completions reclassify
        out cleanly before returning (design §7)."""
        await super().stop()
        await self.settle()

    async def settle(self) -> None:
        """Await all currently in-flight case steps to a quiescent point. Useful for a
        clean shutdown and for deterministic tests after ``advance(suggested_interval_secs=0.0)``."""
        while True:
            tasks = [s.task for s in self._by_folder.values() if s.task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- Internal index maintenance ---------------------------------------

    def _index_add(self, slot: _Slot) -> None:
        self._by_case_id[slot.case.case_id] = slot
        key = slot.case.case_external_key
        if key is not None:
            self._by_external_key.setdefault(key, []).append(slot)

    def _index_remove(self, slot: _Slot) -> None:
        self._by_case_id.pop(slot.case.case_id, None)
        key = slot.case.case_external_key
        if key is not None:
            bucket = self._by_external_key.get(key)
            if bucket is not None:
                self._by_external_key[key] = [s for s in bucket if s is not slot]
                if not self._by_external_key[key]:
                    del self._by_external_key[key]

    def _staggered_countdown(self, multiple: int) -> int:
        """First countdown when (re)scheduling into a tier of interval ``multiple``: scatter
        same-tier items across the M phases so they don't all fire on the same beat
        (design §5 phase-staggering). For M == 1 (hot) this is always 1."""
        self._stagger_counter += 1
        return 1 + (self._stagger_counter % max(1, multiple))
