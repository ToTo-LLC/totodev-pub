"""CasePoolDriver — abstract base for driving a pool of FolderBackedCase objects.

PURPOSE
-------
A ``FolderBackedCase`` knows how to take a single forward step
(``case_advance()``) but has no opinion about when that step should be called
or how many cases should be driven concurrently. ``CasePoolDriver`` fills that
gap: it accepts a collection of cases and takes responsibility for advancing
them automatically, spreading the resulting system load over time rather than
triggering work in surges.

Beyond scheduling, the driver handles two concerns that callers should not
have to manage themselves:

- **Lease keepalive.** Each ``FolderBackedCase`` holds an on-disk lease that
  expires if not renewed. The driver heartbeats every case regularly so leases
  stay live without caller involvement.
- **Aggregate monitoring.** Rather than subscribing to events on individual
  cases, callers subscribe once to the pool and receive a unified event stream
  covering all cases.

The driver is primarily an in-memory scheduling layer. It stays agnostic about
disk layout and does not manage case folders, archival, or discovery — those
belong to a higher-level manager. Cases arrive already bound (live, lease-held)
and leave the same way.

TYPICAL LIFECYCLE
-----------------
1. Construct and ``start()`` the driver.
2. ``add()`` a ``FolderBackedCase`` instance whenever a case becomes active.
3. The driver advances it automatically, firing events as it progresses.
4. When a case reaches a terminal state the driver fires a ``CLOSED`` event.
   Remove it with ``remove()`` — good hygiene, though the performance penalty
   for leaving closed cases in the pool is low.
5. ``stop()`` the driver on shutdown; it drains in-flight advances cleanly.

The driver exposes container-like access (``__len__``, ``__contains__``,
``__iter__``, ``__getitem__``) so callers can inspect the pool naturally.
Derived classes implement the abstract methods to define their scheduling
policy; the class is named for that policy (e.g. ``TieredCasePoolDriver``).

INVOCATION MODEL
----------------
Subclasses implement ``advance()``, which performs one full beat of the
driver: sweep eligible cases, fire any due advances, run the heartbeat walk,
then sleep internally (via ``asyncio.sleep``) for the remainder of the beat
period. Sleeping inside ``advance()`` is intentional — the driver's job is to
pace load smoothly, and the beat period is an implementation-internal concern.

The base class provides a concrete ``start()`` / ``stop()`` that loop over
``advance()``. Derived classes may override these if they need additional
concurrent tasks, but the default is sufficient for most implementations and
serves as a readable reference for how the loop works.

``advance()`` accepts an optional ``suggested_interval_secs`` parameter that
is advisory — pass ``0.0`` in tests to suppress sleeping and force immediate
processing.

EVENT EXTENSIBILITY
-------------------
``CasePoolEventNames`` is a ``StrEnum`` covering the standard events. Derived
drivers that need additional events define their own separate ``StrEnum``
alongside it — no subclassing required since ``CasePoolEvent.event`` is typed
as ``str`` and accepts any string value.
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

    A StrEnum so values are plain strings, support iteration, and guarantee
    uniqueness. Derived drivers that need additional events define their own
    separate StrEnum alongside this one; no subclassing needed since
    ``CasePoolEvent.event`` is typed as ``str``.

    Pool membership events (entry / exit):
    """
    ADMITTED = "admitted"   # case added to the pool via add()
    HALTED   = "halted"     # request_halt() fully drained; case no longer driven
    REMOVED  = "removed"    # case removed by caller via remove()
    EVICTED  = "evicted"    # involuntary removal — DetachedCaseError / OwnershipLostError

    # Advance-derived events (fired as a result of case_advance() completing).
    # Firing order within a beat: ALERTED → ADVANCED → CLOSED.
    # The same AdvanceResult instance may be delivered in multiple events
    # within one beat (e.g. ALERTED and ADVANCED carry the identical record).
    ALERTED  = "alerted"    # CASE_ALERT logged during an advance
    ADVANCED = "advanced"   # case made progress (state changed)
    CLOSED   = "closed"     # case reached a terminal state; always preceded by ADVANCED in the same beat
    FAILED   = "failed"     # exception prevented transition (consistent with DSL @FAIL semantics)


@dataclass(frozen=True)
class CasePoolEvent:
    """Payload delivered to event subscribers."""
    event: str                                    # a CasePoolEventNames constant or subclass extension
    case: FolderBackedCase
    handle: Hashable                              # the subscription handle that fired
    advance_result: AdvanceResult | None = None


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class CasePoolDriver(ABC):
    """Scheduling seam over a pool of FolderBackedCase objects.

    Behaves like a container: cases are added, managed, and eventually
    removed. The driver owns scheduling policy — which cases advance, when,
    and at what cadence — but not folder, lease, or disk management.

    Concrete subclasses are named for their scheduling policy
    (e.g. TieredCasePoolDriver).

    A case's folder path is its identity within the pool. Methods that operate
    on a single case (``request_halt``, ``remove``, ``fire``, ``boost``,
    ``find``, and the container accessors) identify it by that folder path. The
    folder path of a case MUST NOT be changed while the case is in the pool —
    doing so invalidates the driver's ability to locate it and produces
    undefined behavior. Remove the case first if its folder must move.
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

        Raises if a case with the same folder path is already present.
        """

    @abstractmethod
    def request_halt(self, case_folder: Path) -> None:
        """Request that a case stop being driven. Always returns immediately.

        The case is marked so that no further advances will be scheduled. Any
        in-flight transition runs to completion entirely independently of this
        call. A HALTED event fires once the case is fully settled (i.e. no
        longer in-flight and no longer scheduled). Subscribe to HALTED to know
        when remove() is safe.
        """

    @abstractmethod
    def remove(self, case_folder: Path) -> FolderBackedCase:
        """Remove a case and return the still-bound object.

        Raises CaseInFlightError if an advance is currently in progress;
        call request_halt() and wait for HALTED before removing if needed.
        Does not detach or release the lease — caller becomes steward.
        """

    # -- Driving -----------------------------------------------------------

    @abstractmethod
    async def advance(self, suggested_interval_secs: float | None = None) -> None:
        """Perform one full beat of the driver.

        Sweeps eligible cases, fires due advances, runs the heartbeat walk,
        then sleeps internally for the remainder of the beat period.
        ``suggested_interval_secs`` is advisory — pass ``0.0`` in tests to
        suppress sleeping and force immediate processing.

        If ``advance()`` discovers that one of its cases has been detached
        (i.e. deliberately disconnected from disk), it may rehydrate a fresh
        case object from disk — keyed by the case's folder path — in order to
        satisfy the request and continue driving it.

        This is the autonomous beat. To prompt a specific transition out of band
        (e.g. a user-interface action), see fire(), which routes a single advance
        through the same standard processing this beat performs.
        """

    async def start(self) -> None:
        """Begin driving the pool by repeatedly calling advance(). Idempotent.

        The base implementation is intentionally minimal: it spawns a single
        task that loops over advance() until stop() is called. This is
        sufficient for most drivers. Derived classes are invited to override
        if they need additional concurrent tasks (e.g. a separate heartbeat
        loop) or more complex startup sequencing.
        """
        if self._run_task is not None:
            return
        self._stop_event.clear()
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Gracefully stop driving: signal halt, drain in-flight, release resources."""
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
        """Manually fire a trigger on a case, routed through the driver.

        If no trigger is provided, the driver will fire the first auto edge
        that is eligible.
        If a trigger is provided, it will be fired with the given keyword arguments.
        Routing through the driver ensures the result informs scheduling state.
        If the case is already mid-transition (in-flight), the caller receives
        the result of that in-progress advance rather than a new invocation.

        Under the hood, fire() eventually results in a call to
        FolderBackedCase.case_advance() for the given trigger name — or, if you
        pass no trigger name, to case_advance() with no trigger, which produces
        the same auto-advance style behavior the driver uses on every beat.

        fire() is the graceful, preferred mechanism for prompting a transition:
        because the call is routed through the driver, standard processing occurs
        around the advance — most importantly, the pool's lifecycle events are
        fired. It is therefore the best way to deliver USER triggers, e.g. a
        manual edge fired in response to a user-interface action.

        A note on safety vs. gracefulness: it is not harmful for other code to
        invoke triggers directly on the Case object even while that case lives in
        the pool. The Case object's own protections still apply — in particular,
        it will not let trigger A start while trigger B is still processing — so a
        direct invocation cannot corrupt in-flight state. What a direct invocation
        does NOT do is route through the driver, so it skips the standard
        processing above (notably, the pool will not emit its lifecycle events for
        that transition). Prefer fire() whenever you want that standard processing;
        reach past it to the Case object only when you deliberately do not.
        """

    # -- Scheduling hints --------------------------------------------------

    @abstractmethod
    def boost(self, case_folder: Path) -> None:
        """Schedule the case for the next sweep, regardless of current countdown.

        Does not change tier or reset cadence; the case resumes normal scheduling
        after it fires. Intended for UI or external signals that something changed.

        Prefer boost() over fire() when the caller wants to prompt the driver to
        check on a case soon without forcing a specific trigger — it respects the
        driver's scheduling model and avoids contention with in-flight advances.
        """

    # -- Lookup ------------------------------------------------------------

    @abstractmethod
    def find(self, case_folder: Path) -> FolderBackedCase:
        """Look up a case by folder path. Raises KeyError if not present."""

    # -- Queries -----------------------------------------------------------
    #
    # Abstract queries require driver-internal state that cannot be deduced
    # by inspecting case objects alone. Concrete defaults are O(N) iterations
    # over the pool; derived classes may override with faster implementations.

    @abstractmethod
    def halted_cases(self) -> list[FolderBackedCase]:
        """Cases for which request_halt() has been called but HALTED not yet fired."""

    @abstractmethod
    def in_flight_cases(self) -> list[FolderBackedCase]:
        """Cases with an advance currently in progress."""

    @abstractmethod
    def blocked_cases(self) -> list[FolderBackedCase]:
        """Cases that cannot currently auto-advance (blocked or non-advanceable)."""

    def stalled_cases(self, threshold_secs: float) -> list[FolderBackedCase]:
        """Cases that have not changed state in at least ``threshold_secs`` seconds.

        Uses ``case_dwell_secs``, which measures time since the case entered
        its current state. Cases that are repeatedly failing (and therefore
        never leaving their current state) are correctly included.
        Default: O(N) iteration. Override for better performance.
        """
        return [case for case in self if case.case_dwell_secs >= threshold_secs]

    def closed_cases(self) -> list[FolderBackedCase]:
        """Cases that have reached a terminal state and await removal.

        Default: O(N) iteration. Override for better performance.
        """
        return [case for case in self if case.case_is_closed]

    def cases_in_state(self, state_name: str) -> list[FolderBackedCase]:
        """Cases whose current FSM state matches ``state_name``.

        Default: O(N) iteration. Override for better performance.
        """
        return [case for case in self if case.case_state == state_name]

    def failed_cases(self) -> list[FolderBackedCase]:
        """Cases with at least one transition failure since entering their current state.

        Uses ``case_transition_fail_count``, which counts failures since the
        last state entry (the same value the DSL's ``@FAIL`` guard reads).
        Default: O(N) iteration. Override for better performance.
        """
        return [case for case in self if case.case_transition_fail_count > 0]

    # -- Events ------------------------------------------------------------

    @abstractmethod
    def case_event_subscribe(
        self,
        events: str | set[str],
        callback: Callable[[CasePoolEvent], None],
        handle: Hashable | None = None,
    ) -> Hashable:
        """Subscribe to one or more named pool events.

        ``events`` is a single event name string (e.g. ``CasePoolEventNames.CLOSED``)
        or a set of them; the callback fires for any matching event.
        ``handle`` may be any hashable supplied by the caller for their own
        bookkeeping; if omitted a UUID is generated. Raises if the handle is
        already in use. Returns the handle.

        The callback receives a ``CasePoolEvent`` with: the event name,
        the case object, the subscription handle, and an ``advance_result``
        (populated for advance-derived events — ALERTED, ADVANCED, CLOSED,
        FAILED; None for membership events — ADMITTED, REMOVED, HALTED,
        EVICTED). The same ``AdvanceResult`` instance may appear in multiple
        events within a single beat (e.g. ALERTED and ADVANCED share the
        identical record).
        """

    @abstractmethod
    def case_event_unsubscribe(self, handle: Hashable) -> None:
        """Remove a subscription by handle. Raises KeyError if not found."""

