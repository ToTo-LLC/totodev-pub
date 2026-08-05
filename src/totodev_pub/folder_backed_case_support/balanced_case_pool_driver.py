# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""BalancedCasePoolDriver — MLFQ load-balancing driver for a case fleet.

**When to use:** Default choice when many cases should each make steady,
incremental progress. Suited to large pools where no single case should monopolize
in-flight slots or scarce choke permits — the driver spreads attention by cadence
(HOT/WARM/COLD) rather than finishing cases strictly one-by-one. For
queue-ordered, seniority-first bursting, see ``SeniorityCasePoolDriver``.

**Strategy:** Multi-level feedback queue (MLFQ) tiers driven by ``AdvanceResult``
observations. Every live case is polled on its tier schedule; promotion/demotion
adjusts cadence. Contested capacity is a launch gate only, but the two gates
resolve contention differently: the concurrency ceiling is decided by incidental
sweep order (whoever is due and visited first wins any headroom), while choke
permits are decided by ``choke_wait_streak`` — among due cases contending for
the same beat's frozen choke budget, whoever has been declined longest goes
first, so no case is starved indefinitely by dict order. If a gate declines,
``case_advance()`` is not called; the slot retries next beat (``skip_countdown
= 1``, not a full tier reload). See ``ChokePermitGovernor`` for beat-quantized
permit budgeting, and ``_order_chokeables`` for the ordering hook
(``SeniorityCasePoolDriver`` overrides it to preserve queue order instead).

Implements ``CasePoolDriver`` with extensions: ``peek``, ``by_tier``, ``snapshot``,
``find_by_external_key``, ``settle``. Each case has a ``Slot`` (tier +
``skip_countdown``); ``_DORMANT`` (-1) marks terminal or in-flight slots. Beat loop:
sweep → heartbeat slice → fixed-rate sleep (target period ``I0``, minus time already
spent since the last beat, floored at ``BEAT_YIELD_FLOOR``; a sweep that launched
work targets the shorter ``I0 * EAGER_BEAT_FRACTION`` while the system keeps up —
see ``TierPolicy``). No disk management —
``PoolMembershipJournal`` handles crash recovery separately.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import time
import uuid
from collections import deque
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
from totodev_pub.folder_backed_case_support.choke_permit_governor import (
    ChokeGrant, ChokePermitGovernor,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseInFlightError,
    DetachedCaseError, FireRejectedError, OwnershipLostError, UnconfiguredChokeError,
)

logger = logging.getLogger(__name__)

# _DORMANT (-1): terminal or in-flight — never decremented or fired. Live slots fire at 0
# and reload countdown; they never rest at 0 between beats.
_DORMANT = -1


class Tier(enum.Enum):
    """Scheduling tier of a slot — a label that drives its cadence (HOT fastest, COLD
    slowest). Promotion/demotion mutates this in place; slots never move."""
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


# Tier policy (promotion/demotion rules and tunables)

@dataclass
class TierPolicy:
    """The promotion/demotion rules and timing tunables as data, so thresholds and the
    kind->effect mapping can be swapped or subclassed without touching the sweep.

    All intervals are integer multiples ``M`` of base beat ``I0``. Tunables below are
    starting defaults; adjust for workload.
    """
    # Cadence
    I0: float = 0.5                       # base beat / hot period (seconds)
    BEAT_YIELD_FLOOR: float = 0.05        # min sleep per beat: an overrunning beat still yields
    # Eager tempo: after a sweep that launched at least one step, target
    # period*EAGER_BEAT_FRACTION instead of the full period — but only while the
    # beat is still inside that eager window (a slow/congested beat falls back to
    # full-period pacing). A sweep that launches nothing always paces at the full
    # period, so an idle pool never speeds up. 1.0 disables.
    EAGER_BEAT_FRACTION: float = 0.25
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
        """Initial tier for a freshly added (live) case: default HOT, but a structural
        dead-end (no auto exits) starts WARM. (Terminal cases never enter rotation.)"""
        return Tier.HOT if case.case_is_advanceable else Tier.WARM

    def reclassify(self, slot: "Slot", result: AdvanceResult) -> None:
        """Mutate ``slot`` (tier / reset_multiple / streaks) from the latest result.

        Only ever called for a NON-terminal case (terminal cases are made dormant by the
        driver and never reclassified). Maps result.progressed / failed / no-op to tier changes.
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
        accelerated = not slot.case.case_is_advanceable
        self._apply_noop_ladder(slot, accelerated=accelerated)

    def _apply_noop_ladder(self, slot: "Slot", *, accelerated: bool) -> None:
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


# Slot + read-only peek view

@dataclass
class _PendingFire:
    """One tick-paced fire attached to a slot via ``attach_fire``."""
    trigger: str | None
    trigger_kwargs: dict[str, Any]
    on_launch: Callable[[], None] | None = None
    on_complete: Callable[[AdvanceResult | None, BaseException | None], None] | None = None


@dataclass
class Slot:
    """Per-case scheduling state. Slots never move: inserted once, removed once;
    promote/demote is in-place mutation of ``tier`` / ``reset_multiple``."""
    case: FolderBackedCase
    tier: Tier
    reset_multiple: int                  # countdown reload (tier base, or warm backoff)
    skip_countdown: int                  # beats to next step; _DORMANT when dormant
    noop_streak: int = 0
    fail_streak: int = 0
    choke_wait_streak: int = 0           # consecutive beats declined a choke grant
    in_flight: bool = False
    terminal: bool = False
    halt_requested: bool = False
    halt_settled: bool = False           # HALTED event has fired
    last_result: Optional[AdvanceResult] = None
    last_advanced_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None
    task: Optional[asyncio.Task] = None  # the in-flight case-step task, if any
    choked: Optional[frozenset[str]] = None
    pending_grant: Optional[ChokeGrant] = None
    pending_fires: deque[_PendingFire] = field(default_factory=deque)
    active_fire: Optional[_PendingFire] = None


@dataclass(frozen=True)
class CasePeek:
    """A cheap point-in-time view of one case's scheduling state (extension surface)."""
    case_folder: Path
    case_id: str
    state: str
    tier: Tier
    in_flight: bool
    terminal: bool
    halt_requested: bool
    noop_streak: int
    fail_streak: int
    choke_wait_streak: int
    skip_countdown: int
    last_result: Optional[AdvanceResult]
    choked: Optional[frozenset[str]] = None
    pending_fire_count: int = 0


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class BalancedCasePoolDriver(CasePoolDriver):
    """Concrete MLFQ scheduling driver over a pool of FolderBackedCase objects.

    Identity is the case folder path. ``_by_folder`` is the authoritative slot store;
    ``_by_case_id`` / ``_by_external_key`` are optional indexes. See module docstring.
    """

    def __init__(
        self,
        *,
        policy: Optional[TierPolicy] = None,
        concurrency_ceiling: int = 50,
        choke_limits: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self._policy = policy or TierPolicy()
        self._ceiling = concurrency_ceiling
        self._choke_limits = dict(choke_limits or {})
        self._governor = ChokePermitGovernor(self._choke_limits)
        self._choke_validated_types: set[type] = set()

        self._by_folder: dict[Path, Slot] = {}
        self._by_case_id: dict[str, Slot] = {}
        self._by_external_key: dict[str, list[Slot]] = {}

        self._in_flight_count = 0
        self._stagger_counter = 0          # scatters same-tier items across phases
        self._hb_cursor = 0                # heartbeat walk cursor
        self._beat_anchor: Optional[float] = None  # monotonic time the last advance() returned
        self._last_sweep_launches = 0      # steps launched by the most recent sweep

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

        self._validate_case_chokes(type(case))
        slot = self._make_slot(case)
        self._by_folder[folder] = slot
        self._index_add(slot)
        self._emit(CasePoolEventNames.ADMITTED, case)

    def _make_slot(self, case: FolderBackedCase) -> Slot:
        if case.case_is_terminal:
            return Slot(
                case=case, tier=Tier.COLD, reset_multiple=self._policy.M_COLD,
                skip_countdown=_DORMANT, terminal=True,
            )
        tier = self._policy.admission_tier(case)
        reset_multiple = self._policy.base_multiple(tier)
        return Slot(
            case=case, tier=tier, reset_multiple=reset_multiple,
            skip_countdown=self._staggered_countdown(reset_multiple),
        )

    def _validate_case_chokes(self, case_type: type[FolderBackedCase]) -> None:
        if case_type in self._choke_validated_types:
            return
        declared = case_type.case_type_spec().declared_choke_resources()
        configured = frozenset(self._choke_limits)
        missing = declared - configured
        if missing:
            trigger_chokes = case_type.case_type_spec().fsm.trigger_chokes
            example_trigger = next(
                (t for t, names in trigger_chokes.items() if names & missing),
                None,
            )
            raise UnconfiguredChokeError(
                case_type.__name__, frozenset(missing), example_trigger=example_trigger,
            )
        self._choke_validated_types.add(case_type)

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
        self._fail_slot_fires(
            slot, FireRejectedError(case_folder, reason="case removed from pool"),
        )
        self._index_remove(slot)
        del self._by_folder[case_folder]
        self._emit(CasePoolEventNames.REMOVED, slot.case)
        return slot.case

    # -- Driving (the beat) ------------------------------------------------

    async def advance(self, suggested_interval_secs: float | None = None) -> None:
        """One beat, paced at a fixed rate: the period (``I0``, or the advisory
        ``suggested_interval_secs``) is the *target* time between beats, not a dead
        sleep appended to each one. Work done since the previous beat — this sweep,
        the heartbeat slice, and any caller work between ``advance()`` calls — is
        subtracted from the sleep. An overrunning beat still sleeps at least
        ``BEAT_YIELD_FLOOR`` (clamped to the period) so in-flight case-step tasks
        always get event-loop time; back-to-back sweeps never spin the loop.

        Eager tempo: when the sweep just launched work AND the beat is still inside
        the eager window (elapsed < period * ``EAGER_BEAT_FRACTION``), the target
        period shrinks to that fraction — active work is picked up sooner while a
        quiet or congested system stays at the relaxed tempo.

        A zero/negative advisory interval disables pacing entirely (deterministic
        tests)."""
        self._sweep_once()
        self._heartbeat_slice()
        period = self._policy.I0 if suggested_interval_secs is None else suggested_interval_secs
        if not period or period <= 0:
            self._beat_anchor = None      # unpaced beat: don't let a stale anchor
            return                        # swallow a later paced beat's sleep
        elapsed = 0.0
        if self._beat_anchor is not None:
            elapsed = time.monotonic() - self._beat_anchor
        fraction = self._policy.EAGER_BEAT_FRACTION
        if 0.0 < fraction < 1.0 and self._last_sweep_launches > 0:
            eager_period = period * fraction
            if elapsed < eager_period:
                period = eager_period
        floor = min(self._policy.BEAT_YIELD_FLOOR, period)
        await asyncio.sleep(max(floor, period - elapsed))
        self._beat_anchor = time.monotonic()

    def _sweep_once(self) -> None:
        self._sweep_preamble()
        launched = 0
        try:
            chokeables: list[tuple[Slot, frozenset[str]]] = []
            for slot in list(self._by_folder.values()):
                if slot.skip_countdown <= 0:          # dormant (terminal / in-flight)
                    continue
                slot.skip_countdown -= 1
                if slot.skip_countdown != 0:
                    continue
                if slot.halt_requested:               # defensive: halt should already be dormant
                    slot.skip_countdown = _DORMANT
                    continue
                self._slot_prelaunch(slot)
                needed = self._slot_choke_need(slot)
                if needed:
                    # Defer: this beat's choke budget is assigned by priority below,
                    # not incidental sweep order.
                    chokeables.append((slot, needed))
                    continue
                if self._attempt_launch(slot, needed):
                    launched += 1
            for slot, needed in self._order_chokeables(chokeables):
                if self._attempt_launch(slot, needed):
                    launched += 1
        finally:
            self._last_sweep_launches = launched
            self._sweep_end()

    def _slot_choke_need(self, slot: Slot) -> frozenset[str]:
        """The choke resources ``slot``'s next launch will need: its lead pending
        fire's trigger-specific need if one is queued, else the generic need for
        its current state."""
        if slot.pending_fires:
            return self._needed_chokes_for_fire(slot, slot.pending_fires[0].trigger)
        return slot.case.case_type_spec().fsm.pending_chokes_for(slot.case.case_state)

    def _order_chokeables(
        self, chokeables: list[tuple[Slot, frozenset[str]]],
    ) -> list[tuple[Slot, frozenset[str]]]:
        """Order in which this beat's choke-needing due slots compete for the
        frozen budget. Default: longest-waiting-first, so no slot is starved by
        incidental sweep order. Subclasses may override for a different contract
        (e.g. queue/seniority order — see ``SeniorityCasePoolDriver``)."""
        return sorted(chokeables, key=lambda item: item[0].choke_wait_streak, reverse=True)

    def _attempt_launch(self, slot: Slot, needed: frozenset[str]) -> bool:
        """Gate a due slot through the concurrency ceiling, then the choke budget,
        then launch it (via its pending fire if one is queued, else the generic
        advance). Tracks ``choke_wait_streak`` on decline/success so a later beat's
        ``_order_chokeables`` can prioritize the longest-waiting slot."""
        if self._in_flight_count >= self._ceiling:
            slot.skip_countdown = 1               # backpressure: retry next beat
            return False
        grant = self._governor.try_acquire(needed)
        if grant is None:
            slot.choked = needed
            slot.choke_wait_streak += 1
            slot.skip_countdown = 1
            return False
        slot.choked = None
        launched = (
            self._launch_pending_fire(slot, grant) if slot.pending_fires
            else self._launch_generic(slot, grant)
        )
        if launched:
            slot.choke_wait_streak = 0
        return launched

    def _launch_generic(self, slot: Slot, grant: ChokeGrant) -> bool:
        if not self._live_or_evict(slot):
            self._choke_release(grant)
            return False
        self._launch_with_grant(slot, grant)
        return True

    def _launch_pending_fire(self, slot: Slot, grant: ChokeGrant) -> bool:
        """Launch the head pending fire using an already-acquired ``grant``. Returns
        False (releasing the grant) if the case couldn't be kept live."""
        if not self._live_or_evict(slot):
            self._choke_release(grant)
            return False
        record = slot.pending_fires.popleft()
        slot.active_fire = record
        self._invoke_fire_callback(record.on_launch)
        pass_kwargs = record.trigger_kwargs if record.trigger is not None else None
        try:
            self._launch_case_step(slot, record.trigger, pass_kwargs, grant=grant)
        except BaseException as err:
            self._choke_release(grant)
            slot.active_fire = None
            self._invoke_fire_callback(record.on_complete, None, err)
            if not slot.halt_requested and not slot.terminal:
                slot.skip_countdown = 1 if slot.pending_fires else max(1, slot.reset_multiple)
            raise
        return True

    def _sweep_preamble(self) -> None:
        self._governor.begin_sweep()

    def _sweep_end(self) -> None:
        self._governor.end_sweep()

    def _slot_prelaunch(self, slot: Slot) -> None:
        """Hook for subclasses (e.g. queue wake / requeue). No-op on tiered."""

    def _launch_with_grant(self, slot: Slot, grant: ChokeGrant) -> asyncio.Task:
        return self._launch_case_step(slot, grant=grant)

    def _choke_release(self, grant: ChokeGrant | None) -> None:
        if grant is not None:
            self._governor.release(grant)

    def _release_slot_grant(self, slot: Slot) -> None:
        grant = slot.pending_grant
        if grant is not None:
            self._choke_release(grant)
            slot.pending_grant = None
            slot.choked = None

    def _needed_chokes_for_fire(
        self, slot: Slot, trigger: str | None,
    ) -> frozenset[str]:
        fsm = slot.case.case_type_spec().fsm
        if trigger is not None:
            return fsm.trigger_chokes.get(trigger, frozenset())
        return fsm.pending_chokes_for(slot.case.case_state)

    # -- Manual driving ----------------------------------------------------

    async def fire(
        self, case_folder: Path, trigger: str | None, **trigger_kwargs: Any
    ) -> AdvanceResult:
        """See ``CasePoolDriver.fire`` for the portable contract. In-flight behavior:

        - ``trigger=None`` while a step is in flight COALESCES — "advance it" is
          already being satisfied, so the in-progress step's result is returned.
        - A pinned ``trigger`` while a step is in flight QUEUES — the in-flight step
          is awaited (its outcome belongs to its own awaiter and is discarded here),
          then the requested trigger launches as its own step. Concurrent pinned
          fires thus apply sequentially, never dropped, never overlapping.

        Choke permits come from ``ChokePermitGovernor.acquire_priority`` — this call
        may wait for live capacity, and waiters are served FIFO ahead of the next
        sweep's beat budget."""
        slot = self._by_folder[case_folder]
        while slot.in_flight and slot.task is not None:
            if trigger is None:
                # Already mid-transition: hand back the in-progress result, don't launch anew.
                return await slot.task
            # Pinned trigger: wait out the current step, then fire ours below. The
            # awaited step's exception (if any) is its own awaiter's business.
            try:
                await slot.task
            except Exception:
                pass
            if self._by_folder.get(case_folder) is not slot:
                raise KeyError(case_folder)       # evicted/removed while waiting
        if not self._live_or_evict(slot):
            raise KeyError(case_folder)           # evicted during rehydrate
        needed = self._needed_chokes_for_fire(slot, trigger)
        grant = await self._governor.acquire_priority(needed)
        # With a trigger, pass the kwargs dict as-is (a pinned MANUAL edge requires an
        # explicit bag, even {}); with no trigger, pass None so case_advance() does the
        # auto sweep (a kwargs bag without a trigger is misuse there).
        pass_kwargs = trigger_kwargs if trigger is not None else None
        try:
            task = self._launch_case_step(slot, trigger, pass_kwargs, grant=grant)
        except BaseException:
            self._choke_release(grant)
            raise
        return await task

    def attach_fire(
        self,
        case_folder: Path,
        trigger: str | None,
        trigger_kwargs: dict[str, Any] | None = None,
        *,
        on_launch: Callable[[], None] | None = None,
        on_complete: Callable[
            [AdvanceResult | None, BaseException | None], None
        ] | None = None,
    ) -> None:
        """Queue a tick-paced fire — see ``CasePoolDriver.attach_fire``."""
        slot = self._by_folder[case_folder]
        if slot.halt_requested or slot.halt_settled:
            raise FireRejectedError(case_folder, reason="case is halted")
        if slot.terminal or slot.case.case_is_terminal:
            raise FireRejectedError(case_folder, reason="case is terminal")
        slot.pending_fires.append(
            _PendingFire(
                trigger=trigger,
                trigger_kwargs=dict(trigger_kwargs or {}),
                on_launch=on_launch,
                on_complete=on_complete,
            )
        )
        if not slot.in_flight and not slot.halt_requested and not slot.terminal:
            slot.skip_countdown = 1

    def boost(self, case_folder: Path) -> None:
        slot = self._by_folder[case_folder]
        if slot.terminal or slot.halt_requested:
            return                                # nothing to nudge
        if not slot.in_flight:
            slot.skip_countdown = 1               # fire next beat; tier/cadence unchanged

    # -- Case-step launch + completion ------------------------------------

    def _launch_case_step(
        self,
        slot: Slot,
        trigger: str | None = None,
        trigger_kwargs: dict | None = None,
        *,
        grant: ChokeGrant | None = None,
    ) -> asyncio.Task:
        slot.pending_grant = grant
        slot.in_flight = True
        slot.skip_countdown = _DORMANT
        self._in_flight_count += 1
        task = asyncio.ensure_future(self._run_case_step(slot, trigger, trigger_kwargs))
        slot.task = task
        task.add_done_callback(self._consume_task)
        return task

    async def _run_case_step(
        self, slot: Slot, trigger: str | None, trigger_kwargs: dict | None,
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
            self._release_slot_grant(slot)
            self._finish_in_flight(slot)
            self._evict(slot, reason=err)
            return result
        except BaseException as err:
            # Misuse (e.g. ValueError for a bad trigger via fire()) or an unexpected error:
            # never wedge the slot. Clear in-flight, give it a normal reload, and re-raise to
            # any awaiter (fire()); beat-launched tasks have their exception consumed.
            self._release_slot_grant(slot)
            self._finish_in_flight(slot)
            self._complete_active_fire(slot, None, err)
            if slot.halt_requested:
                slot.skip_countdown = _DORMANT
                self._settle_halt(slot)
            elif not slot.terminal:
                slot.skip_countdown = (
                    1 if slot.pending_fires else max(1, slot.reset_multiple)
                )
            raise
        self._complete_step(slot, result)
        return result

    def _complete_step(self, slot: Slot, result: AdvanceResult) -> None:
        self._release_slot_grant(slot)
        self._finish_in_flight(slot)
        slot.last_result = result
        if result.progressed:
            slot.last_advanced_at = time.monotonic()

        terminal_now = slot.case.case_is_terminal
        if terminal_now:
            slot.terminal = True
        else:
            old_reset = slot.reset_multiple
            self._policy.reclassify(slot, result)
            # Phase-stagger when the cadence changed (add/demote); otherwise reload the
            # true interval so scattered items keep their phase.
            if slot.reset_multiple != old_reset:
                slot.skip_countdown = self._staggered_countdown(slot.reset_multiple)
            else:
                slot.skip_countdown = max(1, slot.reset_multiple)

        self._slot_post_step(slot, result)
        # Resolve the active attach_fire BEFORE emitting events: TERMINATED handlers
        # (e.g. CaseManager.begin_termination → remove) run synchronously and would
        # otherwise cancel the active fire with "removed from pool" before its
        # awaiter sees the successful AdvanceResult.
        self._complete_active_fire(slot, result, None)
        self._emit_advance_events(slot, result)

        if slot.halt_requested:
            slot.skip_countdown = _DORMANT
            self._settle_halt(slot)
        elif terminal_now:
            slot.skip_countdown = _DORMANT
            # Terminal after a fire: fail any leftover queued fires.
            if slot.pending_fires:
                self._fail_slot_fires(
                    slot,
                    FireRejectedError(
                        slot.case.case_folder, reason="case became terminal",
                    ),
                )
        elif slot.pending_fires:
            # More attached fires wait: don't wait a full tier interval.
            slot.skip_countdown = 1

    def _slot_post_step(self, slot: Slot, result: AdvanceResult) -> None:
        """Hook for subclasses (e.g. requeue-on-wake). No-op on tiered."""

    def _finish_in_flight(self, slot: Slot) -> None:
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

    def _settle_halt(self, slot: Slot) -> None:
        self._fail_slot_fires(
            slot,
            FireRejectedError(slot.case.case_folder, reason="case is halted"),
        )
        if not slot.halt_settled:
            slot.halt_settled = True
            self._emit(CasePoolEventNames.HALTED, slot.case)

    @staticmethod
    def _invoke_fire_callback(cb: Callable[..., Any] | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            logger.exception("attach_fire callback raised")

    def _complete_active_fire(
        self,
        slot: Slot,
        result: AdvanceResult | None,
        error: BaseException | None,
    ) -> None:
        rec = slot.active_fire
        if rec is None:
            return
        slot.active_fire = None
        self._invoke_fire_callback(rec.on_complete, result, error)

    def _fail_slot_fires(self, slot: Slot, error: BaseException) -> None:
        """Fail the in-flight attached fire (if any) and every pending record."""
        if slot.active_fire is not None:
            rec = slot.active_fire
            slot.active_fire = None
            self._invoke_fire_callback(rec.on_complete, None, error)
        while slot.pending_fires:
            rec = slot.pending_fires.popleft()
            self._invoke_fire_callback(rec.on_complete, None, error)

    def _fail_all_pending_fires(self, error: BaseException) -> None:
        for slot in list(self._by_folder.values()):
            if slot.active_fire is not None or slot.pending_fires:
                self._fail_slot_fires(slot, error)

    def _emit_advance_events(self, slot: Slot, result: AdvanceResult) -> None:
        # Order: ALERTED → ADVANCED → FAILED → TERMINATED
        case = slot.case
        if result.alerted:
            self._emit(CasePoolEventNames.ALERTED, case, result)
        if result.progressed:
            self._emit(CasePoolEventNames.ADVANCED, case, result)
        if result.failed:
            self._emit(CasePoolEventNames.FAILED, case, result)
        if slot.terminal and result.progressed:
            # TERMINATED is always preceded by ADVANCED in the same beat.
            self._emit(CasePoolEventNames.TERMINATED, case, result)

    # -- Detach recovery + eviction ---------------------------------------

    def _live_or_evict(self, slot: Slot) -> bool:
        """Ensure ``slot.case`` is live; return False (and evict) if it can't be made live.

        Cheap non-raising precheck first (the common path); on detachment, rehydrate a fresh
        object and adopt it into the slot, keeping the slot — and the case's identity —
        stable. Eviction only when rehydration is impossible.

        **Any** rehydration failure evicts. This runs mid-sweep, so an escaping
        exception leaves ``advance()`` and lands in the caller's loop with no
        per-case containment; because it would repeat deterministically every
        beat, a single unreadable ``case_record.yaml`` would take the whole fleet
        down. Eviction is the fail-safe: the slot is released, pending fires are
        rejected with the reason, and an EVICTED event names the case.
        """
        if not slot.case.case_is_detached:
            return True
        folder = slot.case.case_folder
        try:
            fresh = case_type_registry.rehydrate(folder)
        except Exception as err:
            self._evict(slot, reason=err)
            return False
        # Adopt the fresh object; re-point any derived indexes at the same slot.
        self._index_remove(slot)
        slot.case = fresh
        self._index_add(slot)
        return True

    def _evict(self, slot: Slot, *, reason: BaseException) -> None:
        self._release_slot_grant(slot)
        folder = slot.case.case_folder
        self._fail_slot_fires(
            slot,
            FireRejectedError(folder, reason=f"case evicted ({reason!r})"),
        )
        logger.warning("BalancedCasePoolDriver evicting case %s: %r", folder, reason)
        self._index_remove(slot)
        self._by_folder.pop(folder, None)
        self._emit(CasePoolEventNames.EVICTED, slot.case)

    # -- Heartbeat walk ---------------------------------------------------

    def _heartbeat_slice(self) -> None:
        """Walk a slice of the store, beating each retained case's lease. A full lap
        completes in ~HEARTBEAT_WALK_PERIOD (well inside the TTL). Every retained case is
        walked — including terminal-awaiting-removal — through the same _live_or_evict
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

    def get_by_case_id(self, case_id: str) -> FolderBackedCase | None:
        slot = self._by_case_id.get(case_id)
        return None if slot is None else slot.case

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

    def active_cases(self) -> list[FolderBackedCase]:
        return [
            slot.case for slot in self._by_folder.values()
            if not slot.halt_requested and not slot.terminal
        ]

    def in_flight_cases(self) -> list[FolderBackedCase]:
        return [slot.case for slot in self._by_folder.values() if slot.in_flight]

    def blocked_cases(self) -> list[FolderBackedCase]:
        out: list[FolderBackedCase] = []
        for slot in self._by_folder.values():
            if slot.case.case_was_blocked or not slot.case.case_is_advanceable:
                out.append(slot.case)
        return out

    def terminal_cases(self) -> list[FolderBackedCase]:
        # Override the O(N) ABC default with the cached slot flag.
        return [slot.case for slot in self._by_folder.values() if slot.terminal]

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
            terminal=slot.terminal,
            halt_requested=slot.halt_requested,
            noop_streak=slot.noop_streak,
            fail_streak=slot.fail_streak,
            choke_wait_streak=slot.choke_wait_streak,
            skip_countdown=slot.skip_countdown,
            last_result=slot.last_result,
            choked=slot.choked,
            pending_fire_count=len(slot.pending_fires) + (1 if slot.active_fire else 0),
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
        terminal = sum(1 for s in self._by_folder.values() if s.terminal)
        blocked = len(self.blocked_cases())
        pending_fires = sum(
            len(s.pending_fires) + (1 if s.active_fire else 0)
            for s in self._by_folder.values()
        )
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
            "terminal": terminal,
            "blocked": blocked,
            "pending_fires": pending_fires,
            "lease_ttl_secs": DEFAULT_LEASE_TTL_SECS,
            "oldest_dwell_secs": oldest_dwell_secs,
            "oldest_dwell_case": oldest_case,
            "chokes": self._governor.resource_usage(),
            "fire_waiters": self._governor.priority_waiter_count(),
        }

    # -- Lifecycle ---------------------------------------------------------

    async def stop(self) -> None:
        """Stop the beat loop, then drain any in-flight steps so completions reclassify
        out cleanly before returning. Fail any still-queued attach_fire records."""
        await super().stop()
        await self.settle()
        self._fail_all_pending_fires(
            RuntimeError("driver stopped with pending fire(s)"),
        )

    async def settle(self) -> None:
        """Await all currently in-flight case steps to a quiescent point. Useful for a
        clean shutdown and for deterministic tests after ``advance(suggested_interval_secs=0.0)``."""
        while True:
            tasks = [s.task for s in self._by_folder.values() if s.task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- Internal index maintenance ---------------------------------------

    def _index_add(self, slot: Slot) -> None:
        self._by_case_id[slot.case.case_id] = slot
        key = slot.case.case_external_key
        if key is not None:
            self._by_external_key.setdefault(key, []).append(slot)

    def _index_remove(self, slot: Slot) -> None:
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
        For M == 1 (hot) this is always 1."""
        self._stagger_counter += 1
        return 1 + (self._stagger_counter % max(1, multiple))
