"""CasePoolDriver — abstract base for driving a pool of FolderBackedCase objects.

PURPOSE
-------
A ``FolderBackedCase`` knows how to take a single forward step
(``case_advance()``) but has no opinion about when that step should be called
or how many cases should be driven concurrently. That is fine for one case, but
anyone working with *many* cases quickly runs into the same set of needs:
something has to keep every case moving forward, watch their progress so a human
or system can intervene when a case stalls or alerts, and do all of this in a
way that is sensitive to server performance — pacing the work rather than
hammering the machine in surges. ``CasePoolDriver`` is the home for that
responsibility.

It also makes a fleet of cases pleasant to work with in aggregate: callers deal
with one pool object instead of bookkeeping a loose bag of individual cases.
The intended mental model is that most programs that work with cases will keep a
case pool driver at their core — one that effectively *owns* all the open cases.
Cases generally live inside a driver from the moment they open until they close;
holding a live case outside of a driver is the exception, not the norm.

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

(Side note, not central to this class: a complete real-world deployment will
usually need additional classes and structures layered around the driver to
handle resource management — e.g. where case folders are stored, archiving old
cases, and detaching case objects once they are closed or have gone inactive.
The driver deliberately leaves that out of scope so it can stay a focused
scheduling layer.)

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
Subclasses implement ``advance()``, which performs one beat of the driver. A
beat carries exactly three obligations, with NO promised ordering among them
and no requirement that any particular mechanism be used:

1. Heartbeat the cases that are due for a heartbeat, keeping their leases live.
2. Fire ``case_advance()`` on the cases that are due to advance.
3. Use an async-friendly strategy to smooth trigger execution across the pool
   so load is paced rather than surged.

The pacing in (3) is a contract about *behavior*, not *implementation*: a
beat must not hammer the whole fleet at once, but it is free to achieve that
however it likes. An ``asyncio.sleep`` is one perfectly good choice, but it is
not guaranteed — and the relative order of heartbeating, advancing, and any
smoothing delay is deliberately left to the subclass. Callers must not depend
on a beat sleeping, nor on any fixed sequence of these steps.

The base class provides a concrete ``start()`` / ``stop()`` that loop over
``advance()``. Derived classes may override these if they need additional
concurrent tasks, but the default is sufficient for most implementations and
serves as a readable reference for how the loop works.

``advance()`` accepts an optional ``suggested_interval_secs`` parameter that
is advisory — pass ``0.0`` in tests to request immediate processing with no
smoothing delay.

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

    Rehydration tolerance (NOT a blanket guarantee). A case may be detached
    (its live object disconnected from disk) at any time, by this driver or by
    other code. MANY of the driver's methods tolerate this by auto-rehydrating
    a fresh case object from disk (keyed by the folder path) before using it,
    so a detached case does not interrupt them. This is deliberately NOT a
    promise that EVERY method on the surface rehydrates: some methods only read
    cached state, and others (e.g. ``remove``) hand the object back as-is. In
    particular, subclasses MUST implement auto-rehydration for ``advance()`` and
    ``fire()`` — the two methods that drive a case forward — so that driving
    never fails merely because a case was detached out from under it. Where
    rehydration is impossible (folder gone, or the case is owned elsewhere), the
    affected case is evicted and an ``EVICTED`` event fires.
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
        """Perform one beat of the driver.

        A beat has three obligations and makes no promise about the order in
        which they happen or the mechanism used (see INVOCATION MODEL):

        1. Heartbeat the cases that are due, keeping their leases live.
        2. Fire ``case_advance()`` on the cases that are due to advance.
        3. Smooth trigger execution across the pool with an async-friendly
           strategy so load is paced rather than surged.

        ``suggested_interval_secs`` is advisory — pass ``0.0`` in tests to
        request immediate processing with no smoothing delay. Note that an
        ``asyncio.sleep`` is only one valid way to satisfy (3); callers must
        not rely on a beat sleeping, nor on any fixed sequence of these steps.

        Graceful recovery from detachment (REQUIRED of this method): a case may
        be detached (its live object disconnected from disk) at any time, by this
        driver or by other code. Before ``advance()`` exercises a case, the
        subclass MUST auto-rehydrate any that appear detached — transparently
        opening a fresh case object from disk, keyed by the folder path — so a
        detached case does not interrupt the beat. This is the caller's
        expectation of ``advance()`` specifically (see the class docstring: such
        tolerance is required of ``advance()`` and ``fire()``, but is not a
        blanket guarantee across every method). Where rehydration is impossible
        (the folder is gone, or the case is now owned elsewhere), the driver
        gives up on that case and fires ``EVICTED``.

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

        Like ``advance()``, ``fire()`` drives a case forward, so the subclass
        MUST auto-rehydrate a detached case before firing (see the class
        docstring) — a case detached out from under the driver must not cause
        ``fire()`` to fail; it rehydrates and proceeds, or evicts and fires
        ``EVICTED`` when rehydration is impossible.

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

