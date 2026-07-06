"""CasePoolDriver — abstract base for driving a pool of FolderBackedCase objects.

A ``FolderBackedCase`` takes one forward step (``case_advance()``) but has no
opinion about fleet scheduling. ``CasePoolDriver`` owns that: which cases advance,
when, lease keepalive for cases due this beat, and aggregate pool events.

Cases arrive already bound (live, lease-held) and leave the same way. Disk layout,
archival, and discovery belong to a planned ``CaseManager`` (draft:
notebooks/DEVDAVE/case_manager_classes/CaseManager Model.md).

Typical lifecycle: ``start()`` → ``add(case)`` → autonomous ``advance()`` beats →
``TERMINATED`` event → ``remove()`` → ``stop()``.

Concrete implementations
------------------------
``TieredCasePoolDriver`` — default MLFQ load-balancer. Use when many cases should
each make steady, incremental progress; contested capacity (in-flight ceiling,
choke permits) is throttled but not seniority-ranked.

``QueuedCasePoolDriver`` — same tier cadence with a queue contract. Use when
cases should burst through automatic work one-at-a-time (or front-of-line first)
and the head of the queue should win scarce capacity over cases behind it.

Invocation model
----------------
Each beat (``advance()``) has three obligations with **no promised ordering**:

1. Heartbeat cases due this beat (keep leases live).
2. Fire ``case_advance()`` on cases due to advance.
3. Pace work with an async-friendly smoothing strategy (``asyncio.sleep`` is one
   valid choice, not guaranteed).

``suggested_interval_secs`` is advisory (pass ``0.0`` in tests for no delay).

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

    Container-like access by folder path. Concrete subclasses are named for their
    policy (e.g. ``TieredCasePoolDriver``). Do not change a case's folder path
    while it is in the pool.

    Rehydration tolerance: ``advance()`` and ``fire()`` MUST auto-rehydrate detached
    cases before driving (keyed by folder path). On failure (folder gone, owned
    elsewhere), evict and fire ``EVICTED``. Other methods may use cached state or
    return objects as-is — not every method rehydrates.
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

        Raises ValueError if the folder path is already present.
        """

    @abstractmethod
    def request_halt(self, case_folder: Path) -> None:
        """Request that a case stop being driven. Returns immediately.

        HALTED fires once settled (no longer in-flight or scheduled). Wait for
        HALTED before ``remove()`` if an advance may be in progress.
        """

    @abstractmethod
    def remove(self, case_folder: Path) -> FolderBackedCase:
        """Remove a case and return the still-bound object.

        Raises CaseInFlightError if an advance is in progress.
        Does not detach or release the lease.
        """

    # -- Driving -----------------------------------------------------------

    @abstractmethod
    async def advance(self, suggested_interval_secs: float | None = None) -> None:
        """Perform one beat. See module docstring for the three obligations.

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
        """Route one ``case_advance()`` through the driver for standard pool events.

        With no ``trigger``, sweeps auto edges in declared order (first guard that
        permits). With ``trigger``, pins that edge. Rehydrates detached cases like
        ``advance()``.

        Prefer ``fire()`` over direct ``await case.<trigger>()`` when pool lifecycle
        events should fire; direct calls skip driver processing but remain safe.
        """

    # -- Scheduling hints --------------------------------------------------

    @abstractmethod
    def boost(self, case_folder: Path) -> None:
        """Schedule the case for the next sweep without forcing a specific trigger."""

    # -- Lookup ------------------------------------------------------------

    @abstractmethod
    def find(self, case_folder: Path) -> FolderBackedCase:
        """Look up a case by folder path. Raises KeyError if not present."""

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
