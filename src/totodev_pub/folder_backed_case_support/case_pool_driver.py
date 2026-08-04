"""CasePoolDriver — abstract base for driving a pool of FolderBackedCase objects.

A ``FolderBackedCase`` takes one forward step (``case_advance()``) but has no
opinion about fleet scheduling. ``CasePoolDriver`` owns that: which cases advance,
when, lease keepalive for cases due this beat, and aggregate pool events.

Cases arrive already bound (live, lease-held) and leave the same way. Disk layout,
archival, and discovery belong to ``CaseManager`` (``totodev_pub.case_manager``),
never to a driver.

Typical lifecycle: ``start()`` → ``add(case)`` → autonomous ``advance()`` beats →
``TERMINATED`` event → ``remove()`` → ``stop()``.

Concrete implementations
------------------------
``BalancedCasePoolDriver`` — default MLFQ load-balancer. Use when many cases should
each make steady, incremental progress; contested capacity (in-flight ceiling,
choke permits) is throttled but not seniority-ranked.

``SeniorityCasePoolDriver`` — same tier cadence with a queue contract. Use when
cases should burst through automatic work one-at-a-time (or front-of-line first)
and the head of the queue should win scarce capacity over cases behind it.

Contract for implementers
--------------------------
Everything below is a MUST unless noted otherwise. ``BalancedCasePoolDriver`` and its
subclass ``SeniorityCasePoolDriver`` are the reference for what honoring it looks like.

1. Identity. A case's folder path is the sole key across container access
   (``__contains__``, ``__getitem__``, ``find``), membership (``add``, ``remove``,
   ``request_halt``), and manual driving (``fire``, ``boost``). It must not change while
   the case is in the pool. ``get_by_case_id`` (and other indexes such as external_key)
   are conveniences layered over the folder-keyed store, never a replacement for it.

2. Admission is not rehydration. ``add()`` accepts only an already-live, lease-held
   object. A detached case handed to ``add()`` is a caller error — reject it (e.g.
   ``DetachedCaseError``) rather than rehydrating on the caller's behalf. This is
   deliberately asymmetric with #3: rehydration tolerance covers cases that go stale
   *after* admission, not admission itself.

3. Rehydration tolerance. ``advance()`` and ``fire()`` — and any other path that touches
   a case's liveness, including heartbeating — MUST auto-rehydrate a detached case
   before driving it, keyed by folder path, and MUST fire ``EVICTED`` and drop the case
   if rehydration is impossible (folder gone, owned elsewhere). Route these checks
   through one internal chokepoint so a case is never evicted twice. After a successful
   rehydrate, container access must return the fresh live object.

4. Lease liveness is a guarantee, not a nicety. Whatever cadence a driver uses, every
   case that remains in the pool (and isn't already settled for removal) must have its
   lease refreshed before the TTL lapses. "Heartbeat cases due this beat" describes a
   scheduling *policy* for picking who gets heartbeated on a given beat; that no case's
   lease is ever allowed to expire is the underlying, non-negotiable guarantee.

5. No overlapping steps on one case. ``FolderBackedCase`` forbids more than one FSM
   trigger in flight on a single live object (``CaseTransitionInFlightError``). A
   driver's own concurrency must not violate this: ``fire()`` must never launch a
   second concurrent ``case_advance()``. When a step is already in flight, a
   trigger-less ``fire(folder, None)`` coalesces (awaits the existing task and returns
   its result); a ``fire()`` with a pinned trigger queues behind it (awaits the
   existing task, then launches the requested trigger as its own step) so an explicit
   trigger is never silently dropped.

   Tick-paced fires use ``attach_fire()`` instead: records append to a per-slot deque
   and execute one per sweep turn (preempting auto-advance), under the same concurrency
   ceiling and sweep choke budget as ordinary steps. Concurrent ``attach_fire`` calls
   never overlap; they simply queue.

6. Halt is terminal for scheduling. Once ``HALTED`` has fired for a case, the driver
   must not schedule further advances for it. ``request_halt()`` returns immediately;
   ``HALTED`` fires exactly once, when the case is no longer in-flight or scheduled.
   There is no in-place undo: to drive the case again, ``remove()`` it and ``add()`` it
   back (re-admission builds a fresh scheduling slot; tier/streaks/queue position reset).
   Pending ``attach_fire`` records on a halted (or removed/evicted) slot must be failed
   via their ``on_complete`` callbacks.

7. Event discipline.
   - Membership events fire exactly once per transition: ``ADMITTED`` on a successful
     ``add()``, ``REMOVED`` on a successful ``remove()``, ``HALTED`` once a halt request
     settles, ``EVICTED`` on rehydration failure / lost ownership.
   - Per beat, a case's advance-derived events follow
     ``ALERTED -> ADVANCED -> FAILED -> TERMINATED``; ``ADVANCED``/``FAILED`` are
     mutually exclusive for one ``AdvanceResult``, and ``TERMINATED`` never fires
     without an ``ADVANCED`` in the same beat.
   - ``advance_result`` is populated for the four advance-derived events, ``None`` for
     membership events.

8. Query defaults are a floor. ``stalled_cases()``, ``terminal_cases()``,
   ``cases_in_state()``, ``failed_cases()`` have default O(N) implementations. A
   subclass may override any of them with a cached/indexed version (as
   ``BalancedCasePoolDriver.terminal_cases()`` does), but an override must return the
   same set the default would — faster, never different.

9. The ABC is the portable surface. Diagnostics/extension APIs (``peek``, ``snapshot``,
   ``by_tier``, ``settle``, ``find_by_external_key``, ...) live on concrete subclasses.
   Code written against ``CasePoolDriver`` alone must not assume any of that extra
   surface exists.

Each beat (``advance()``) has three scheduling obligations with **no promised ordering**:

1. Heartbeat cases due this beat (keep leases live — see #4 above).
2. Fire ``case_advance()`` on cases due to advance.
3. Pace work with an async-friendly smoothing strategy (``asyncio.sleep`` is one
   valid choice, not guaranteed).

``suggested_interval_secs`` is advisory. When honored it is a *target period*
between beats (time already spent working counts against it), not a flat sleep
appended to each beat. Pass ``0.0`` in tests for no delay.

Event extensibility: ``CasePoolEventNames`` covers standard events. Derived drivers
may define a separate ``str, enum.Enum`` for additional event names; ``CasePoolEvent.event``
accepts any string.
"""

from __future__ import annotations

import asyncio
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Hashable, Iterator

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult


# ---------------------------------------------------------------------------
# Public event types
# ---------------------------------------------------------------------------

class CasePoolEventNames(str, enum.Enum):
    """Standard event names fired by a CasePoolDriver.

    Values are plain strings. Derived drivers may define their own enum for
    extra events; ``CasePoolEvent.event`` is typed as ``str``.

    Pool membership: ADMITTED, HALTED, REMOVED, EVICTED.

    Advance-derived (typical beat order: ALERTED → ADVANCED → FAILED? → TERMINATED).
    FAILED and ADVANCED are mutually exclusive. The same ``AdvanceResult`` may
    appear in multiple events within one beat.
    """
    ADMITTED = "admitted"
    HALTED   = "halted"
    REMOVED  = "removed"
    EVICTED  = "evicted"    # rehydration failed or OwnershipLostError

    ALERTED    = "alerted"
    ADVANCED   = "advanced"
    TERMINATED = "terminated"
    FAILED     = "failed"


@dataclass(frozen=True)
class CasePoolEvent:
    """Payload delivered to event subscribers."""
    event: str
    case: FolderBackedCase
    handle: Hashable
    advance_result: AdvanceResult | None = None


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class CasePoolDriver(ABC):
    """Scheduling seam over a pool of FolderBackedCase objects.

    Container-like access by folder path; concrete subclasses are named for their
    policy (e.g. ``BalancedCasePoolDriver``). The full derived-class contract lives in
    the module docstring above — the load-bearing invariants are:

    - Folder path is the sole identity key and must not change while a case is in the
      pool (extra indexes are layered conveniences, never authoritative).
    - ``add()`` requires an already-live object; it is not a rehydration point.
    - ``advance()``/``fire()`` MUST auto-rehydrate a detached case (keyed by folder
      path) before driving it, and fire ``EVICTED`` if that's impossible.
    - Once ``HALTED`` fires for a case, it must never be scheduled again.
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None

    # -- Container read interface ------------------------------------------

    @abstractmethod
    def __len__(self) -> int:
        """Number of cases currently in the pool."""

    @abstractmethod
    def __contains__(self, case_folder: object) -> bool:
        """True if a case with the given folder path is in the pool."""

    @abstractmethod
    def __iter__(self) -> Iterator[FolderBackedCase]:
        """Iterate over all live case objects in the pool."""

    @abstractmethod
    def __getitem__(self, case_folder: Path) -> FolderBackedCase:
        """Return the live case object for the given folder path.

        Raises KeyError if not present.
        """

    # -- Membership --------------------------------------------------------

    @abstractmethod
    def add(self, case: FolderBackedCase) -> None:
        """Add a live, lease-held case to the pool.

        On success, fire ``ADMITTED`` exactly once (see module docstring #7).
        Raises ValueError if the folder path is already present.
        """

    @abstractmethod
    def request_halt(self, case_folder: Path) -> None:
        """Request that a case stop being driven. Returns immediately.

        HALTED fires once settled (no longer in-flight or scheduled). Wait for
        HALTED before ``remove()`` if an advance may be in progress. Halt is not
        reversible in place; to drive again, ``remove()`` then ``add()`` the case.
        """

    @abstractmethod
    def remove(self, case_folder: Path) -> FolderBackedCase:
        """Remove a case and return the still-bound object.

        On success, fire ``REMOVED`` exactly once (see module docstring #7).
        Raises CaseInFlightError if an advance is in progress.
        Does not detach or release the lease.
        """

    # -- Driving -----------------------------------------------------------

    @abstractmethod
    async def advance(self, suggested_interval_secs: float | None = None) -> None:
        """Perform one beat. See module docstring for scheduling obligations and contract.

        Subclasses MUST rehydrate detached cases before driving (see class docstring).
        ``suggested_interval_secs`` is advisory.
        """

    async def start(self) -> None:
        """Begin driving: spawn a task that loops ``advance()`` until ``stop()``."""
        if self._run_task is not None:
            return
        self._stop_event.clear()
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the beat loop after the current ``advance()`` returns.

        Subclasses may override to drain in-flight case steps (e.g. ``settle()``).
        """
        self._stop_event.set()
        if self._run_task is not None:
            await self._run_task
            self._run_task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.advance()

    # -- Manual driving ----------------------------------------------------

    @abstractmethod
    async def fire(self, case_folder: Path, trigger: str | None, **trigger_kwargs: Any) -> AdvanceResult:
        """Immediate one-step primitive: route ``case_advance()`` through the driver now.

        With no ``trigger``, sweeps auto edges in declared order (first guard that
        permits). With ``trigger``, pins that edge. Rehydrates detached cases like
        ``advance()``.

        If a step is already in flight for the case: trigger-less fire coalesces
        with it (returns the in-progress result); a pinned trigger waits for the
        in-flight step to finish and then fires — sequential, never dropped, never
        overlapping (contract #5).

        Choke permits are acquired on the priority path (may await live capacity;
        served FIFO, ahead of the next sweep's beat budget) rather than the sweep's
        non-blocking beat-quantized budget. This path deliberately bypasses the
        concurrency ceiling — use ``attach_fire()`` for tick-paced, uniform-capacity
        fires (the CaseManager / mailbox path).

        Prefer ``fire()`` / ``attach_fire()`` over direct ``await case.<trigger>()``
        when pool lifecycle events should fire; direct calls skip driver processing
        but remain safe.
        """

    @abstractmethod
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
        """Queue a tick-paced fire on the case's scheduling slot (contract #5).

        Appends a record to the slot's pending-fire deque and nudges the slot due
        next beat (when not already in flight). The next sweep that reaches this
        slot launches the head fire *instead of* auto ``case_advance()``, under the
        same concurrency ceiling and sweep choke budget as ordinary steps. One fire
        per turn; remaining records stay queued and reload ``skip_countdown = 1``.

        Opaque callbacks (invoked sandboxed — exceptions are logged, never wedge
        the slot):

        - ``on_launch``: just before the step launches (e.g. mailbox moves
          ``pending/ → firing/`` for crash-recovery attribution).
        - ``on_complete(result, error)``: exactly once per record. On success
          ``error`` is None and ``result`` is the ``AdvanceResult``; on failure /
          cancellation ``result`` is None and ``error`` is set.

        Raises:
            KeyError: folder not in the pool.
            FireRejectedError: slot is halted or terminal.
        """

    # -- Scheduling hints --------------------------------------------------

    @abstractmethod
    def boost(self, case_folder: Path) -> None:
        """Schedule the case for the next sweep without forcing a specific trigger."""

    # -- Lookup ------------------------------------------------------------

    @abstractmethod
    def find(self, case_folder: Path) -> FolderBackedCase:
        """Look up a case by folder path. Raises KeyError if not present."""

    @abstractmethod
    def get_by_case_id(self, case_id: str) -> FolderBackedCase | None:
        """Look up a pooled case by ``case_id``, or ``None`` if not in the pool.

        Folder path remains the driver's identity key for membership and driving;
        this is an indexed convenience over that store.
        """

    # -- Queries -----------------------------------------------------------

    @abstractmethod
    def halted_cases(self) -> list[FolderBackedCase]:
        """Cases awaiting HALTED after request_halt()."""

    @abstractmethod
    def in_flight_cases(self) -> list[FolderBackedCase]:
        """Cases with an advance currently in progress."""

    @abstractmethod
    def blocked_cases(self) -> list[FolderBackedCase]:
        """Cases that cannot currently auto-advance."""

    def stalled_cases(self, threshold_secs: float) -> list[FolderBackedCase]:
        """Cases with ``case_dwell_secs`` >= threshold. Default O(N)."""
        return [case for case in self if case.case_dwell_secs >= threshold_secs]

    def terminal_cases(self) -> list[FolderBackedCase]:
        """Terminal cases awaiting removal. Default O(N)."""
        return [case for case in self if case.case_is_terminal]

    def cases_in_state(self, state_name: str) -> list[FolderBackedCase]:
        """Cases in the given FSM state. Default O(N)."""
        return [case for case in self if case.case_state == state_name]

    def failed_cases(self) -> list[FolderBackedCase]:
        """Cases with ``case_transition_fail_count`` > 0. Default O(N)."""
        return [case for case in self if case.case_transition_fail_count > 0]

    # -- Events ------------------------------------------------------------

    @abstractmethod
    def case_event_subscribe(
        self,
        events: str | set[str],
        callback: Callable[[CasePoolEvent], None],
        handle: Hashable | None = None,
    ) -> Hashable:
        """Subscribe to pool events. Returns the subscription handle.

        ``advance_result`` is set for ALERTED, ADVANCED, TERMINATED, FAILED; None for
        membership events. The same instance may appear in multiple events per beat.
        """

    @abstractmethod
    def case_event_unsubscribe(self, handle: Hashable) -> None:
        """Remove a subscription by handle. Raises KeyError if not found."""
