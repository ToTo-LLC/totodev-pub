# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseManager — manage a pool of FolderBackedCase-derived workflows.

A single FolderBackedCase already knows how to store itself and step through
its own lifecycle. Coordinating many of them is a different job: shared
storage and archiving, fair progress under limited resources, restart after
interruption, isolation of cases that keep failing, and a place for hosts to
observe and intervene. CaseManager exists for that aggregate problem.

Typical use
    Users of CaseManager provision cases externally, then adds them to a
    CaseManager. From there, most or all of each case's remaining life is
    spent in the manager's pool, being advanced toward a terminal state.
    The manager owns storage layout, archiving, recovery, and related
    housekeeping so callers do not have to invent that fleet machinery.

Scale and tuning
    Performs best with hundreds to low thousands of live cases. Past that,
    this may not be the right manager. Behavior is highly adjustable via
    persisted policy (Layout and Tunables) and via Bindings such as the pool
    driver (how work is paced and choke resources are shared), the case-type
    registry, and the case classes themselves. Choke resources (CPU, DB
    connections, external API capacity, and similar) are tracked so the
    pool can respect configured budgets.

Running and advancing
    In its run mode the manager repeatedly advances pooled cases through
    automatic stages, using asyncio for concurrent progress. Authors of
    FolderBackedCase subclasses should avoid blocking the event loop
    (push long work onto awaited threads or external processes). Manual
    actions described by FolderBackedCaseInterface can be queued for
    execution alongside automatic advances.

Failure isolation and recovery
    When fleet machinery cannot handle a case safely, the manager stops
    driving it and parks it in quarantine. Resolving a quarantined case
    is an owner responsibility — typically fix the root cause and
    ``reopen_case()``. The manager is bound to a working directory that
    holds the cases and protocol state as a persistent asset; on restart
    that directory is inspected and recovery is attempted so interrupted
    fleets can resume rather than start from scratch.

Hosts and integration
    Companion helpers make it straightforward to build case-processing
    servers of various shapes, including file-format protocols for
    cross-process communication. The usual whole-process host is::

        from totodev_pub.case_manager_support.case_manager_host import serve

    See that module for process ownership, signals, exit codes, and
    watchdog behavior. The manager also exposes an aggregated view of
    notable pool activity for observers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, NamedTuple, TYPE_CHECKING

from totodev_pub.case_manager_support.adopt import AdoptResult, adopt_case_folder
from totodev_pub.case_manager_support.case_manager_config import CaseManagerConfig
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest, ManifestPaths
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.case_store import (
    LIVE,
    QUARANTINED,
    TERMINATED,
    CaseEntry,
    LocalCaseStore,
)
from totodev_pub.case_manager_support.constants import (
    EJECT_SUBDIR,
    FLEET_STATUS_FILENAME,
    MANIFEST_FILENAME,
    POLICY_FILENAME,
    PULSE_INTERVAL_SECS,
    QUARANTINE_SUBDIR,
    RESULTS_SUBDIR,
    TERMINATION_SUBDIR,
)
from totodev_pub.case_manager_support.eject import (
    EjectResult,
    EjectTicket,
    begin_eject,
    eject_dir,
    eject_ticket_path,
    process_eject_ticket,
)
from totodev_pub.case_manager_support.notice import CaseNotice, NoticeRegistry
from totodev_pub.case_manager_support.fleet_status import (
    FleetStatusBoardWriter,
    ensure_board_file,
)
from totodev_pub.case_manager_support.exceptions import (
    AmbiguousExternalKeyError,
    CacheRootStateError,
    EjectAbandonedError,
    EjectTimeoutError,
    InvalidAddressingError,
    LiveCaseNotFoundError,
    ManagerNotRunningError,
    PolicyFileMissingError,
    PolicyMismatchError,
    RecoverRequiredError,
    StuckTrigger,
)
from totodev_pub.case_manager_support.layout import (
    CaseLocation,
    assert_case_folder_movable,
    policy_manager_dir,
)
from totodev_pub.case_manager_support.manager_lease import (
    acquire_manager_lease,
    build_manager_lease,
)
from totodev_pub.case_manager_support.purge import run_redundant_purge
from totodev_pub.case_manager_support.quarantine import (
    QuarantineTicket,
    pending_quarantine_tickets,
    process_quarantine_ticket,
    quarantine_case,
    quarantine_ticket_exists,
)
from totodev_pub.case_manager_support.readmit import OrphanReadmitReport, readmit_orphans
from totodev_pub.case_manager_support.recover import RecoverReport, recover_manager
from totodev_pub.case_manager_support.staging import allocate_staging_folder
from totodev_pub.case_manager_support.ticket_attempts import TicketAttemptLedger
from totodev_pub.case_manager_support.termination import (
    TerminationTicket,
    begin_termination,
    process_pending_ticket,
    replay_pending,
    termination_dir,
    ticket_exists,
)
from totodev_pub.folder_backed_case import (
    FolderBackedCase,
    IncompatibleReclassError,
    ReclassifyAssertionError,
)
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.exceptions import UnregisteredCaseTypeError
from totodev_pub.folder_backed_case_support.case_pool_driver import (
    CasePoolDriver,
    CasePoolEvent,
    CasePoolEventNames,
)
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry,
    case_type_registry,
)
from totodev_pub.folder_backed_case_support.heartbeat_lease import LeaseOwnershipLostError
from totodev_pub.folder_backed_case_support.seniority_case_pool_driver import SeniorityCasePoolDriver

logger = logging.getLogger(__name__)

_LAYOUT_KWARGS = frozenset(CaseManagerPolicy.layout_field_names())
_TUNABLES_KWARGS = frozenset(CaseManagerPolicy.tunables_field_names())

# Consecutive failed loop iterations before the manager gives up retrying and
# hands the failure to the host (or re-raises). A loop that fails the same way
# every tick is wedged, not unlucky.
_LOOP_FAILURE_LIMIT = 3


def _first_supplied(*candidates: Any, default: Callable[[], Any]) -> Any:
    """First non-None candidate, else ``default()``.

    Collaborators may be falsy when empty (e.g. an empty pool driver), so
    injection is decided by identity with ``None``, not truthiness.
    """
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return default()


class _ResolvedPolicy(NamedTuple):
    """A working directory's policy reconciled with what the caller supplied.

    ``is_new`` means the policy record does not exist yet and may be written.
    Derived paths are valid either way.
    """

    policy: CaseManagerPolicy
    policy_path: Path
    manager_dir: Path
    tunables_overrides: dict[str, Any]
    is_new: bool


class CaseManager:
    """Pool coordinator for FolderBackedCase-derived workflows.

    A case is considered "managed" when it has been copied into the disk
    space maintained by the manager using the ``adopt_case`` method.
    From a caller's point of view, managed cases sit in one of three
    execution statuses: Live, Terminated, and Quarantined.

    **Live pool.** Cases the manager is actively advancing. You adopt a
    detached case folder into managed storage; it joins the pool and is
    driven toward a terminal FSM state under shared concurrency and choke
    limits. Most of a case's productive life is spent here.

    **Terminated.** Cases that finished their lifecycle. When a pooled case
    reaches a terminal FSM state, the manager archives it out of the live
    pool into terminal storage (datetime-encoded under the ``terminated``
    status). These are kept for retention and inspection; they are not driven
    again.

    **Quarantine.** A holding area for cases the manager has *stopped*
    driving but kept in managed storage. This is not a normal lifecycle
    outcome. A case lands here when fleet machinery cannot finish admit /
    archive / control-ticket work safely, or when an operator parks it via
    ``quarantine_case()`` for investigation. Quarantined cases remain on
    disk; a ``MANAGER_QUARANTINED`` reason is recorded on the case journal
    when possible. Inspect with ``iter_quarantine()``, fix the underlying
    problem, then ``reopen_case()`` to return the case to the live pool.
    Repeated transition failures and stalls are surfaced as operator
    notices; they do not by themselves move a case into quarantine.

    Composes case storage, pool driver, type registry, and the manager's
    control directory under a working directory. See the module docstring
    for construction, scale, and hosting.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        filespace: LocalCaseStore | str | Path,
        *,
        driver: CasePoolDriver | None = None,
        registry: CaseTypeRegistry | None = None,
        **tunables_overrides: Any,
    ) -> None:
        """Build a manager over an existing working directory.

        ``filespace`` is a ``LocalCaseStore`` or a path to a working directory that
        already has a policy record. Construction never creates one — use
        ``open_local_store()`` for that. A path with no policy record raises
        ``PolicyFileMissingError``.

        ``driver`` is an already-built pool driver, or ``None`` to use a
        ``SeniorityCasePoolDriver`` wired from this manager's policy.
        ``registry`` is an already-populated type catalog, or ``None`` for the
        process-global ``case_type_registry`` (register types on it before
        construction). Other keyword arguments are Tunables applied in memory
        without changing the policy record. Layout fields cannot be overridden
        here; a disagreement with the record raises.
        """
        if isinstance(filespace, LocalCaseStore):
            supplied_store: LocalCaseStore | None = filespace
            root = filespace.root_dir
            resolved = self._resolve_from_store(filespace, tunables_overrides)
        else:
            supplied_store = None
            root = Path(filespace).resolve()
            resolved = self._resolve_policy(root, None, tunables_overrides, init_if_new=False)

        self._policy = resolved.policy
        self._cache_root = root
        self._manager_dir = resolved.manager_dir
        # Identity, not truthiness: CaseTypeRegistry defines __len__, so an empty
        # injected registry is falsy and `or` would discard it for the global.
        self._registry = case_type_registry if registry is None else registry
        # The record exists — _resolve_policy raised otherwise — so this attaches
        # to storage rather than creating any.
        self._store = supplied_store if supplied_store is not None else LocalCaseStore.provision(
            root, resolved.policy
        )
        self._config = CaseManagerConfig(
            cache_root=root,
            policy=resolved.policy,
            policy_path=resolved.policy_path,
            manager_dir=resolved.manager_dir,
            tunables_overrides=resolved.tunables_overrides,
            driver=driver,
            registry=registry,
        )
        # Explicit `is not None`, never truthiness: a CasePoolDriver defines
        # __len__, so a freshly constructed (empty) driver is falsy and `or`
        # would silently discard the one the caller injected.
        self._driver = _first_supplied(driver, default=self._build_default_driver)
        self._notices = NoticeRegistry()
        # One manager per cache root. Built here, claimed in recover() — construction
        # stays cheap and side-effect-free, and claiming needs to be able to wait.
        self._filespace_lease = build_manager_lease(self._manager_dir)
        # Attempts at tickets that cannot record their own. See ticket_attempts.
        self._ticket_attempts = TicketAttemptLedger()
        # Transport is not the fleet's business. Anything that wants to be driven
        # by the tick registers here; the manager knows only that it is a
        # coroutine. See case_manager_support/signaling_adapter.py.
        self._maintenance_cbs: list[Callable[[], Awaitable[None]]] = []
        self._running = False
        self._stopping = False
        self._stop_completed = False
        self._run_task: asyncio.Task[None] | None = None
        self._loop_failure_cb: Callable[[BaseException], None] | None = None
        self._terminated_handle: Any = None
        self._eject_waiters: dict[str, asyncio.Future[EjectResult]] = {}
        self._quarantine_waiters: dict[str, asyncio.Future[Path]] = {}
        # Liveness stamps. Written only here, read from the watchdog thread —
        # which is why they are plain floats and never a compound object.
        self._last_pulse: float | None = None
        self._last_tick_started: float | None = None
        self._last_tick_completed: float | None = None
        self._last_heartbeat_write: float = 0.0
        self._pulse_task: asyncio.Task[None] | None = None
        self._recovered = False
        self.last_recover_report: RecoverReport | None = None
        self._fleet_board: FleetStatusBoardWriter | None = None
        self._fleet_event_handle: Any = None
        if self._policy.enable_fleet_status_board:
            self._fleet_board = FleetStatusBoardWriter(
                self._manager_dir,
                full_flush_interval_secs=self._policy.fleet_status_full_flush_interval_secs,
                terminal_retention_secs=self._policy.fleet_status_terminal_retention_secs,
            )
        self._log_startup_summary()

    @classmethod
    def open_local_store(
        cls,
        cache_root: str | Path,
        policy: CaseManagerPolicy | None = None,
        *,
        init_if_new: bool = True,
        **overrides: Any,
    ) -> LocalCaseStore:
        """Open the managed working directory at ``cache_root``, creating it when absent.

        This creates the policy record, the manager's control directory, and case
        storage together — the only creator. Pass the returned store to
        ``CaseManager``. With ``init_if_new=False``, an absent policy record raises
        ``PolicyFileMissingError``.

        Idempotent when the working directory already exists. A conflicting Layout
        field raises. Tunables apply in memory to the returned store's policy; on
        first create they are also written as the record's durable defaults.
        """
        root = Path(cache_root).resolve()
        resolved = cls._resolve_policy(root, policy, overrides, init_if_new=init_if_new)

        # Storage first. The manager namespace lives inside the cache root, so
        # laying it down ahead of the cache would leave the cache adopting a
        # non-empty root instead of initialising a clean one.
        store = LocalCaseStore.provision(root, resolved.policy)
        if resolved.is_new:
            resolved.manager_dir.mkdir(parents=True, exist_ok=True)
            resolved.policy.save(str(resolved.policy_path), retain_lock=False)
            cls._ensure_namespace_dirs(resolved.manager_dir, resolved.policy)
        return store

    # ------------------------------------------------------------------
    # Status (host / observability)
    # ------------------------------------------------------------------

    @property
    def is_recovered(self) -> bool:
        """True once ``recover()`` has completed (``start()`` precondition)."""
        return self._recovered

    @property
    def is_running(self) -> bool:
        """True between ``start()`` and ``stop()``."""
        return self._running

    @property
    def is_idle(self) -> bool:
        """True when the pool is empty and no termination, eject, or quarantine is
        still pending.

        A case can leave the pool as soon as termination is enqueued, while its
        folder is archived on a later tick — so an empty pool alone is not enough.
        This does not cover signaling-adapter backlog the manager cannot see; the
        host composes the two.
        """
        return len(self._driver) == 0 and not self._departures_in_flight()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def recover(self) -> RecoverReport:
        """Claim exclusive ownership of the working directory, reconcile storage, and
        rebuild the pool.

        Required before ``start()``. Waits out competing case leases, restores live
        cases that belong in the pool, and counts pending terminate/eject/quarantine
        work. After a hard kill this can take tens of seconds while a held lease is
        watched past one heartbeat. Raises ``CompetingManagerError`` if another
        manager owns this working directory.
        """
        report = await recover_manager(self)
        self._recovered = True
        self.last_recover_report = report
        return report

    async def start(self) -> None:
        """Enter run mode: start advancing pooled cases and the liveness heartbeat.

        Idempotent. Raises if ``recover()`` has not completed — starting without
        reconciling disk would drive an empty pool over a working directory that
        may still hold cases.
        """
        if self._running:
            return
        if not self._recovered:
            raise RecoverRequiredError()
        self._running = True
        self._stopping = False
        self._stop_completed = False
        self._terminated_handle = self._driver.case_event_subscribe(
            CasePoolEventNames.TERMINATED,
            self._on_terminated_event,
        )
        if self._fleet_board is not None:
            self._fleet_event_handle = self._driver.case_event_subscribe(
                {
                    CasePoolEventNames.ADMITTED,
                    CasePoolEventNames.ALERTED,
                    CasePoolEventNames.ADVANCED,
                    CasePoolEventNames.FAILED,
                    CasePoolEventNames.REMOVED,
                    CasePoolEventNames.EVICTED,
                },
                self._on_fleet_board_event,
            )
        self._write_manifest(running=True)
        self._last_heartbeat_write = time.monotonic()
        self._last_pulse = time.monotonic()
        self._run_task = asyncio.create_task(self._manager_loop())
        self._pulse_task = asyncio.create_task(self._pulse_loop())

    async def stop(self) -> None:
        """Leave run mode, wait until active advances finish, and release the filespace.

        Idempotent: a second call after a completed ``stop()`` is a no-op.
        There is no timeout here — aborting mid-teardown leaves the manager
        half-stopped with no safe resume short of process restart. Hosts that
        need a grace bound (``serve``, the watchdog) await this call with their
        own deadline and hard-exit if it has not finished; see
        ``case_manager_host.await_manager_stop``.
        """
        if self._stop_completed:
            return
        self._stopping = True
        self._running = False
        if self._pulse_task is not None:
            self._pulse_task.cancel()
            try:
                await self._pulse_task
            except asyncio.CancelledError:
                pass
            self._pulse_task = None
        if self._terminated_handle is not None:
            try:
                self._driver.case_event_unsubscribe(self._terminated_handle)
            except KeyError:
                pass
            self._terminated_handle = None
        if self._fleet_event_handle is not None:
            try:
                self._driver.case_event_unsubscribe(self._fleet_event_handle)
            except KeyError:
                pass
            self._fleet_event_handle = None
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # The loop already logged its own failure before dying; a dead
                # loop task must not abort a deliberate stop().
                logger.exception("Manager loop task had already died; continuing stop()")
            self._run_task = None
        await self._driver.stop()
        await self._settle_with_diagnostics()
        self._publish_fleet_status_board(force=True)
        self._write_manifest(running=False, stopped=True)
        # Last, and only after the fleet has settled: releasing earlier would invite a
        # successor in while this one is still writing. A clean release is also what
        # spares that successor the lease-expiry wait a crash would have cost it.
        self._release_filespace()
        self._stop_completed = True

    # ------------------------------------------------------------------
    # Case pool API — add & remove
    # ------------------------------------------------------------------

    async def adopt_case(
        self,
        source_folder: Path,
    ) -> AdoptResult:
        """Take a detached case folder into managed storage and the live pool.

        ``source_folder`` need not be under staging — any path outside managed
        storage is accepted (staging / adopt-drop are just convenient scratch).
        On success the source is consumed: its contents are moved into the new
        managed location and the emptied source directory is removed. Prefer
        ``allocate_staging_folder()`` when the tree is expendable and you want
        a same-filesystem rename rather than a cross-device copy.

        If adopt fails after the case has already been partially taken into
        managed storage, the residue is quarantined rather than left half-live.

        No correlation id — de-duplicating re-delivered requests is transport's job.
        """
        result = await adopt_case_folder(
            Path(source_folder),
            store=self._store,
            policy=self._policy,
            manager_dir=self._manager_dir,
            registry=self._registry,
            driver_add=self._driver.add,
            case_id_exists=self._store.contains,
            quarantine=self._quarantine,
        )
        if result.status == "rejected":
            self._notices.emit_simple(
                "ADOPT_REJECTED", result.case_id or None, Path(source_folder), result.rejection_reason
            )
        elif result.status == "error":
            self._notices.emit_simple(
                "ADOPT_FAILED", result.case_id, Path(source_folder), result.rejection_reason
            )
        return result

    def allocate_staging_folder(self) -> Path:
        """Allocate one folder into which a case may be placed, typically prior
        to calling adopt_case().

        Purpose: cases built here adopt cheaply — contents can be renamed into
        managed storage instead of copied across devices. The folder is
        expendable; ``adopt_case`` consumes it. Returned already created so
        both ``create_case_in_folder()`` and ``copytree(..., dirs_exist_ok=True)``
        can fill it::

            staged = manager.allocate_staging_folder()
            MyCase.create_case_in_folder(staged, external_key="K-1")
            await manager.adopt_case(staged)

        Abandoned staging is reclaimed on each allocate (lazy GC):

        - no / expired lease and older than ``staging_min_age_secs`` (default 5 min)
          → removed
        - still-leased but older than ``staging_stale_lease_secs`` (default 24 h)
          → removed (stale builder presumed dead)

        In-flight builds younger than those thresholds are left alone. Sweep does
        not run on a timer — only when something allocates again.
        """
        return allocate_staging_folder(self._manager_dir, self._policy)

    async def eject_from_pool(
        self,
        case_id: str,
        *,
        export_to_folder: Path,
        timeout: float | None = None,
    ) -> EjectResult:
        """Export a case out of managed storage and wait until export completes,
        including all file move/copy.

        Halts the case and waits until its active advance finishes first, so a
        mid-advance eject works. ``timeout`` bounds each phase; on expiry
        ``EjectTimeoutError`` names any triggers still running.
        """
        case = self.get_live(case_id)
        # The driver refuses to remove a case mid-step, and says so: wait for
        # HALTED before remove() if an advance may be in progress. Skipping the
        # wait made eject fail outright on a *busy* case — which is exactly the
        # case an operator most often wants out.
        await self._halt_and_settle(case.case_folder, case_id=case_id, timeout=timeout)

        fut: asyncio.Future[EjectResult] = asyncio.get_running_loop().create_future()
        self._eject_waiters[case_id] = fut
        try:
            begin_eject(
                case,
                export_to_folder=export_to_folder,
                manager_dir=self._manager_dir,
                driver_remove=self._driver.remove,
            )
        except KeyError:
            # The case left the pool while we waited for it to halt — it reached
            # a terminal state, or something else removed it. Either way there is
            # nothing left to eject, and that is not the same as a failure.
            self._eject_waiters.pop(case_id, None)
            raise LiveCaseNotFoundError(case_id) from None
        except BaseException:
            # Never leave a waiter nothing can resolve.
            self._eject_waiters.pop(case_id, None)
            raise
        if timeout is not None:
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                stuck = self.collect_stuck_triggers()
                raise EjectTimeoutError(case_id=case_id, stuck=stuck)
        return await fut

    async def quarantine_case(
        self,
        case_id: str,
        *,
        reason: str,
        timeout: float | None = None,
    ) -> Path | None:
        """Force a live case into quarantine, stopping it but keeping
        it in the CaseManager's storage (restorable via ``reopen_case()``).

        Requires a non-empty ``reason``; that text is recorded on the case
        journal. Stops further scheduling and waits until the case is visible
        via ``iter_quarantine()`` (any in-flight step is allowed to finish; it
        is not cancelled).

        Returns the quarantined folder path when the case can be found among
        quarantined cases. Returns ``None`` if ``timeout`` elapses first —
        not a malfunction; the case may still appear in quarantine later
        (for example after a deferred lease-aware move). Misuse and unexpected
        failures raise.

        When the case actually lands in quarantine, a ``CASE_QUARANTINED``
        notice is emitted; subscribe with ``on_notice()`` to observe completion
        without awaiting this call (useful if it returned ``None`` on timeout).
        """
        if not reason or not reason.strip():
            raise ValueError("quarantine_case requires a non-empty reason")
        case = self.get_live(case_id)
        reason_text = reason.strip()
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        def remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - loop.time())

        try:
            await self._halt_and_settle(
                case.case_folder, case_id=case_id, timeout=remaining()
            )
        except EjectTimeoutError:
            return None

        try:
            removed = self._driver.remove(case.case_folder)
        except KeyError:
            raise LiveCaseNotFoundError(case_id) from None
        removed.case_detach()

        fut: asyncio.Future[Path] = loop.create_future()
        self._quarantine_waiters[case_id] = fut
        try:
            landed = await self._quarantine(
                case_id, removed.case_folder, reason_text
            )
            if landed is not None:
                return landed
            if fut.done():
                return fut.result()
            entry = self._store.find(case_id)
            if entry is not None and entry.status == QUARANTINED:
                return entry.case_folder
            rem = remaining()
            if rem is not None and rem <= 0:
                return None
            try:
                if rem is None:
                    return await fut
                return await asyncio.wait_for(fut, timeout=rem)
            except asyncio.TimeoutError:
                return None
        finally:
            self._quarantine_waiters.pop(case_id, None)

    async def reopen_case(self, case_id: str) -> None:
        """Return a quarantined case to the live pool after the owner has fixed it
        or it is believed to be able to resume.  Note that an aberrant case may
        be pushed back into quarantine if it misbehaves.

        Quarantine is a holding pattern: the manager stopped driving the case
        but kept it. Primary use is to resume lifecycle once the fault is
        remedied (bad asset repaired, poison ticket cleared, etc.).

        Do not use this to "undo" termination — a case already in a terminal
        FSM state will just be reconciled out of the pool again. Re-resolves
        the folder after the status move (the path changes).
        """
        loc = self.locate(case_id=case_id)
        if loc is None or loc.in_pool:
            raise LiveCaseNotFoundError(case_id)
        folder = await self._store.set_status(case_id, LIVE)
        self._driver.add(self._registry.rehydrate(folder))

    # ------------------------------------------------------------------
    # Case pool API — live operations
    # ------------------------------------------------------------------

    def get_live(self, case_id: str) -> FolderBackedCase:
        """Return the pooled case object — use cautiously due to risk of race
        conditions.

        The returned instance is the same object the pool still owns. While the
        manager is running, the driver may call ``advance()`` on it at any await
        boundary. The process is single-threaded, but interleaved pool advances
        can still race your own calls on that object and produce odd errors or
        half-applied side effects.

        Prefer safer surfaces:

        - ``fire()`` to invoke triggers under pool scheduling
        - ``reader()`` for read-only inspection (no live case object)

        Reach for this only when you truly need the live ``FolderBackedCase``
        (e.g. manager-internal halt/eject/quarantine paths, or a deliberate
        out-of-band step you accept is outside pool bookkeeping).
        """
        case = self._driver.get_by_case_id(case_id)
        if case is None:
            raise LiveCaseNotFoundError(case_id)
        return case

    def reader(
        self,
        *,
        case_id: str | None = None,
        external_key: str | None = None,
        case_folder: Path | None = None,
    ) -> FolderBackedCaseReader:
        """A read-only view of one case, addressed any of three ways.

        Takes no lease and never rehydrates, so it is safe while another process
        drives the case. ``external_key`` raises if ambiguous; the other forms
        address exactly one case.
        """
        if external_key is not None:
            hits = self.locate_all(external_key=external_key)
            if not hits:
                raise FileNotFoundError(f"No case with external_key {external_key!r}")
            if len(hits) > 1:
                raise AmbiguousExternalKeyError(
                    external_key, case_ids=[h.case_id for h in hits]
                )
            return FolderBackedCaseReader(hits[0].case_folder)
        loc = self._resolve_single(case_id=case_id, case_folder=case_folder)
        if loc is None:
            raise FileNotFoundError("case not found")
        return FolderBackedCaseReader(loc.case_folder)

    def readers_by_external_key(self, external_key: str) -> list[FolderBackedCaseReader]:
        """Every case carrying ``external_key`` — ambiguity-tolerant ``reader()``.
        """
        return [FolderBackedCaseReader(loc.case_folder) for loc in self.locate_all(external_key=external_key)]

    async def fire(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        trigger: str | None = None,
        wait: bool = True,
        on_launch: Callable[[], None] | None = None,
        on_complete: Callable[[AdvanceResult | None, BaseException | None], None] | None = None,
        **trigger_kwargs: Any,
    ) -> AdvanceResult | None:
        """Queue an advance (or manual action) on a pooled case.

        Addressing: exactly one of ``case_id`` / ``case_folder``. With
        ``trigger=None``, available automatic transitions are attempted; with a
        pinned ``trigger``, that manual action runs.

        ``wait=True`` (default) awaits the run loop and returns the
        ``AdvanceResult``. ``wait=False`` returns ``None`` immediately after
        enqueue — use when an intake loop must keep draining without waiting
        for each step (completion via ``on_complete`` or other observation).

        Optional ``on_launch`` / ``on_complete`` run on the event-loop thread
        when the step starts and finishes (success or error). They are additive
        with either ``wait`` value.

        - Requires run mode (``ManagerNotRunningError`` otherwise). Mailbox files
          and this API share one queue with the pool's normal advances.
        - Uses the same concurrency ceiling and choke-resource budgets as ordinary
          advances — not the driver's priority path.
        - Multiple queued advances on one case run one at a time and preempt
          automatic advancing while pending.
        - Do not await this from inside a step hook for the same case (deadlock).
        - For an immediate step outside pool scheduling, use ``get_live()`` and call
          a trigger on the case (skips pool events/bookkeeping; guarded by
          ``CaseTransitionInFlightError`` if a step is already running).
        """
        if not self._running:
            raise ManagerNotRunningError()
        loc = self._resolve_single(case_id=case_id, case_folder=case_folder)
        if loc is None:
            raise LiveCaseNotFoundError(case_id or str(case_folder))

        fut: asyncio.Future[AdvanceResult] | None = None
        if wait:
            fut = asyncio.get_running_loop().create_future()

        def _complete(
            result: AdvanceResult | None, error: BaseException | None,
        ) -> None:
            # Settle the waiter first so a raising user callback cannot strand awaiters.
            if fut is not None and not fut.done():
                if error is not None:
                    fut.set_exception(error)
                elif result is not None:
                    fut.set_result(result)
                else:
                    fut.set_exception(
                        RuntimeError("fire completed with no result or error")
                    )
            if on_complete is not None:
                on_complete(result, error)

        attach_kwargs: dict[str, Any] = {}
        if on_launch is not None:
            attach_kwargs["on_launch"] = on_launch
        if wait or on_complete is not None:
            attach_kwargs["on_complete"] = _complete

        self._driver.attach_fire(
            loc.case_folder,
            trigger,
            trigger_kwargs,
            **attach_kwargs,
        )
        if wait:
            assert fut is not None
            return await fut
        return None

    async def reclassify_case(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        target_type: str | type[FolderBackedCase],
    ) -> FolderBackedCase:
        """Switch a live pooled case to a different registered case type, in place.

        Wraps ``FolderBackedCase.case_reclassify_to()`` with pool choreography: the
        case is removed for the identity switch, re-added under the new type, then
        scheduled promptly so any new automatic paths can run.

        Addressing mirrors ``fire()``: exactly one of ``case_id`` / ``case_folder``.
        ``target_type`` may be the registered class or its bare name.

        Raises:
            LiveCaseNotFoundError: the case is not in the live pool.
            UnregisteredCaseTypeError: ``target_type`` is not registered.
            IncompatibleReclassError: current state is not a state of the target
                class (checked before the pool membership is changed).
            ReclassifyAssertionError: type switch committed but the new class's
                assertions for the preserved state failed; the case is quarantined
                under the new type, then this exception is re-raised.
            CaseInFlightError: a step is mid-advance (from ``driver.remove()``);
                retry after it finishes.
        """
        if isinstance(target_type, str):
            target_cls = self._registry.resolve_case_type(target_type)
            if target_cls is None:
                raise UnregisteredCaseTypeError(target_type)
        else:
            target_cls = target_type
            if self._registry.resolve_case_type(target_cls.__name__) is not target_cls:
                raise UnregisteredCaseTypeError(target_cls.__name__)
        loc = self._resolve_single(case_id=case_id, case_folder=case_folder)
        if loc is None:
            raise LiveCaseNotFoundError(case_id or str(case_folder))
        case = self.get_live(loc.case_id)
        # Pre-validate the shared-state contract before touching the slot, so an
        # incompatible request leaves the pool untouched.
        if case.case_state not in target_cls.case_type_spec().fsm.states:
            raise IncompatibleReclassError(case.case_state, target_cls.__name__)
        folder = case.case_folder
        case_id_resolved = case.case_id
        self._driver.remove(folder)      # raises CaseInFlightError if a step is running
        try:
            fresh = case.case_reclassify_to(target_cls)
        except ReclassifyAssertionError as exc:
            # Type switch already committed — do NOT re-admit the old object.
            # Park under the new class with a durable reason, then re-raise.
            await self._quarantine_after_reclassify_assertion_failure(
                case_id_resolved, folder, exc,
            )
            raise
        except BaseException:
            # The slot is already gone, so the case would fall out of management
            # entirely if we just propagated. Re-admit, then raise the ORIGINAL
            # failure — a recovery that also fails is logged, never substituted,
            # because the caller needs to know why the reclassify failed.
            self._readmit_after_failed_reclassify(folder, case)
            raise
        self._driver.add(fresh)          # fresh slot: advanceable cases are admitted HOT
        self._driver.boost(folder)       # and fire on the next beat
        self._notify_fleet_board(fresh)
        return fresh

    # ------------------------------------------------------------------
    # Case pool API — lookup & iteration
    # ------------------------------------------------------------------

    def locate(self, case_id: str) -> CaseLocation | None:
        """Find a case at any status by ``case_id``.

        Pooled (live) cases resolve via the driver's case-id index. Anything
        not in this process's pool falls through to the store — live-but-not-
        pooled, terminated, quarantined, or missing.
        """
        case = self._driver.get_by_case_id(case_id)
        if case is not None:
            return CaseLocation(
                case_id=case.case_id,
                external_key=case.case_external_key,
                case_folder=case.case_folder,
                status=self._store.live_status,
                in_pool=True,
                terminal=case.case_is_terminal,
            )
        entry = self._store.find(case_id)
        return None if entry is None else self._to_location(entry)

    def locate_all(self, *, external_key: str) -> list[CaseLocation]:
        """Every case carrying ``external_key``.

        Full scan (external keys are not indexed). Prefer ``locate(case_id)``
        on hot paths.
        """
        hits: list[CaseLocation] = []
        for entry in self._store.iter_all():
            if FolderBackedCaseReader(entry.case_folder).case_external_key == external_key:
                hits.append(self._to_location(entry))
        return hits

    def iter_live_pool(self) -> Iterator[FolderBackedCaseReader]:
        """Readers over cases the pool is actively driving.

        Narrower than ``iter_live_bucket()``, which includes every live-status
        case whether or not this process holds it.
        """
        for case in self._driver:
            yield FolderBackedCaseReader(case.case_folder)

    def iter_live_bucket(self) -> Iterator[FolderBackedCaseReader]:
        """Every case at live status, whether or not the pool currently holds it.
        """
        yield from self._readers_at(LIVE)

    def iter_terminal(
        self,
        *,
        reverse: bool = False,
        after: datetime | date | None = None,
        before: datetime | date | None = None,
    ) -> Iterator[FolderBackedCaseReader]:
        """Archived cases, optionally filtered by activity-time bounds."""
        for entry in self._store.iter_by_status(
            TERMINATED, reverse=reverse, after=after, before=before
        ):
            yield FolderBackedCaseReader(entry.case_folder)

    def iter_quarantine(self) -> Iterator[FolderBackedCaseReader]:
        """Cases the manager has stopped driving. See ``support.quarantine``."""
        yield from self._readers_at(QUARANTINED)

    # ------------------------------------------------------------------
    # Observability hooks
    # ------------------------------------------------------------------

    def on_notice(self, callback: Callable[[CaseNotice], None]) -> int:
        """Subscribe to manager notices. Returns a handle for ``off_notice()``.

        One channel carries problems and lifecycle notices for cases leaving the
        pool (terminate / eject / quarantine); filter on ``notice.kind.is_lifecycle``.
        Handlers must not block (they run on the manager's loop). Exceptions in a
        handler are swallowed.
        """
        return self._notices.register(callback)

    def off_notice(self, handle: int) -> None:
        """Unsubscribe. Unknown handles are ignored."""
        self._notices.unregister(handle)

    def on_loop_failure(self, callback: Callable[[BaseException], None]) -> None:
        """Register the host callback invoked when the manager loop gives up after
        repeated consecutive failures.

        ``serve()`` connects this to the watchdog. With no callback the loop
        re-raises (embedded usage).
        """
        self._loop_failure_cb = callback

    def on_maintenance(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run ``callback`` at the head of every maintenance tick.

        Callbacks run before the same tick's advances, so work added here can be
        advanced soon. Each callback is isolated: a raise is logged and noticed,
        and the rest of the tick continues.
        """
        self._maintenance_cbs.append(callback)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_policy(
        cls,
        root: Path,
        policy: CaseManagerPolicy | None,
        overrides: dict[str, Any],
        *,
        init_if_new: bool,
    ) -> _ResolvedPolicy:
        """Reconcile a supplied policy and overrides against ``root``'s record.

        Layout: a supplied value that disagrees with the record raises. Tunables:
        applied in memory and not compared — the record holds defaults, not the
        only permitted values.
        """
        layout, tunables = cls._split_overrides(overrides)
        persisted_path = cls._find_policy_path(root)

        if persisted_path is None:
            if not init_if_new:
                raise PolicyFileMissingError(
                    root / CaseManagerPolicy().manager_namespace / POLICY_FILENAME
                )
            cls._validate_fresh_root(root)
            fresh = policy.model_copy(deep=True) if policy is not None else CaseManagerPolicy()
            for field, value in layout.items():
                setattr(fresh, field, value)
            mgr_dir = policy_manager_dir(root, fresh)
            return _ResolvedPolicy(
                policy=fresh.apply_tunables_overrides(**tunables),
                policy_path=mgr_dir / POLICY_FILENAME,
                manager_dir=mgr_dir,
                tunables_overrides=tunables,
                is_new=True,
            )

        persisted = CaseManagerPolicy.load(str(persisted_path), acquire_lock=False)
        claimed = dict(layout)
        if policy is not None:
            # A whole policy object claims every Layout field it carries, not just
            # the ones that happen to differ from the defaults.
            for field in _LAYOUT_KWARGS:
                claimed.setdefault(field, getattr(policy, field))
        for field, value in claimed.items():
            file_value = getattr(persisted, field)
            if file_value != value:
                raise PolicyMismatchError(field, file_value=file_value, override_value=value)

        return _ResolvedPolicy(
            policy=persisted.apply_tunables_overrides(**tunables),
            policy_path=persisted_path,
            manager_dir=policy_manager_dir(root, persisted),
            tunables_overrides=tunables,
            is_new=False,
        )

    @classmethod
    def _resolve_from_store(
        cls, store: LocalCaseStore, overrides: dict[str, Any]
    ) -> _ResolvedPolicy:
        """Resolve against a store's policy rather than re-reading the record.

        The store may already carry Tunables overrides applied at open; re-reading
        the record would discard them. Layout is checked against the store's
        policy (the layout its storage was built with).
        """
        root = store.root_dir
        policy_path = cls._find_policy_path(root)
        if policy_path is None:
            raise PolicyFileMissingError(root / store.policy.manager_namespace / POLICY_FILENAME)

        layout, tunables = cls._split_overrides(overrides)
        for field, value in layout.items():
            store_value = getattr(store.policy, field)
            if store_value != value:
                raise PolicyMismatchError(field, file_value=store_value, override_value=value)

        return _ResolvedPolicy(
            policy=store.policy.apply_tunables_overrides(**tunables),
            policy_path=policy_path,
            manager_dir=policy_manager_dir(root, store.policy),
            tunables_overrides=tunables,
            is_new=False,
        )

    @staticmethod
    def _split_overrides(overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Partition override names into (Layout, Tunables); reject anything else.

        Unknown names are typos or stale fields — dropping them silently would
        hide misconfiguration. Bindings are named parameters on ``CaseManager``,
        not entries in this bag.
        """
        layout = {k: v for k, v in overrides.items() if k in _LAYOUT_KWARGS}
        tunables = {k: v for k, v in overrides.items() if k in _TUNABLES_KWARGS}
        unknown = sorted(set(overrides) - set(layout) - set(tunables))
        if unknown:
            raise TypeError(
                f"Unknown CaseManager policy field(s): {', '.join(unknown)}. "
                "Keyword arguments must name a Layout field or a Tunables field."
            )
        return layout, tunables

    def _build_default_driver(self) -> CasePoolDriver:
        """Build the default ``SeniorityCasePoolDriver`` from this manager's policy.

        Callers that need a different driver class, or a custom ``TierPolicy``
        for beat tempo, construct the instance themselves and pass ``driver=``.
        """
        return SeniorityCasePoolDriver(
            concurrency_ceiling=self._policy.concurrency_ceiling,
            choke_limits=self._policy.choke_limits,
        )

    def _ensure_namespace(self) -> None:
        self._ensure_namespace_dirs(self._manager_dir, self._policy)

    @staticmethod
    def _ensure_namespace_dirs(mgr_dir: Path, policy: CaseManagerPolicy) -> None:
        """Create the manager's control directory under the manager dir.

        Storage buckets are created by the case store; this owns only the manager
        control directory (mailboxes, tickets, leases, and related state).
        """
        mgr_dir.mkdir(parents=True, exist_ok=True)
        for sub in (
            policy.staging_subdir,
            policy.adopt_drop_subdir,
            policy.fire_mailbox_subdir,
            policy.adopt_mailbox_subdir,
            RESULTS_SUBDIR,
            *(f"{TERMINATION_SUBDIR}/{leaf}" for leaf in ("pending", "failed")),
            *(f"{EJECT_SUBDIR}/{leaf}" for leaf in ("pending", "failed")),
            *(f"{QUARANTINE_SUBDIR}/{leaf}" for leaf in ("pending", "failed")),
        ):
            (mgr_dir / sub).mkdir(parents=True, exist_ok=True)
        for sub in ("intake", "malformed"):
            (mgr_dir / policy.fire_mailbox_subdir / sub).mkdir(parents=True, exist_ok=True)
        for sub in ("intake", "malformed", "executing"):
            (mgr_dir / policy.reclassify_mailbox_subdir / sub).mkdir(parents=True, exist_ok=True)
        (mgr_dir / policy.adopt_mailbox_subdir / "intake").mkdir(parents=True, exist_ok=True)
        (mgr_dir / policy.adopt_mailbox_subdir / "pending").mkdir(parents=True, exist_ok=True)
        (mgr_dir / policy.shutdown_mailbox_subdir / "intake").mkdir(parents=True, exist_ok=True)
        ensure_board_file(mgr_dir, enabled=policy.enable_fleet_status_board)

    def _write_manifest(
        self, *, running: bool = False, stopped: bool = False, recovering: bool = False
    ) -> None:
        rel = lambda p: str(Path(p).relative_to(self._cache_root))
        paths = ManifestPaths(
            fire_mailbox_intake=rel(
                self._manager_dir / self._policy.fire_mailbox_subdir / "intake"
            ),
            adopt_mailbox_intake=rel(
                self._manager_dir / self._policy.adopt_mailbox_subdir / "intake"
            ),
            reclassify_mailbox_intake=rel(
                self._manager_dir / self._policy.reclassify_mailbox_subdir / "intake"
            ),
            shutdown_mailbox_intake=rel(
                self._manager_dir / self._policy.shutdown_mailbox_subdir / "intake"
            ),
            results=rel(self._manager_dir / RESULTS_SUBDIR),
            adopt_drop=rel(self._manager_dir / self._policy.adopt_drop_subdir),
            termination_pending=rel(termination_dir(self._manager_dir) / "pending"),
            eject_pending=rel(eject_dir(self._manager_dir) / "pending"),
            staging=rel(self._manager_dir / self._policy.staging_subdir),
            fleet_status_board=rel(self._manager_dir / FLEET_STATUS_FILENAME),
        )
        manifest = CaseManagerManifest(
            cache_root=str(self._cache_root),
            manager_namespace=self._policy.manager_namespace,
            manifest_stale_secs=self._policy.manifest_stale_secs,
            paths=paths,
            heartbeat_at=CaseManagerManifest.utc_now_iso() if running else None,
            stopped_at=CaseManagerManifest.utc_now_iso() if stopped else None,
            recovering_at=CaseManagerManifest.utc_now_iso() if recovering else None,
            pool_index=self._policy.journal_path if self._policy.journal_attach_steady_state else None,
        )
        manifest.save(str(self._manager_dir / MANIFEST_FILENAME), retain_lock=False)

    def _log_startup_summary(self) -> None:
        driver_name = type(self._driver).__name__
        logger.info(
            "CaseManager ready: cache_root=%s policy=%s driver=%s types=%d handlers=%d",
            self._cache_root,
            self._config.policy_path,
            driver_name,
            len(self._registry),
            len(self._notices),
        )

    @staticmethod
    def _find_policy_path(root: Path) -> Path | None:
        if not root.exists():
            return None
        for candidate in (
            root / ".case_manager" / POLICY_FILENAME,
            root / POLICY_FILENAME,
        ):
            if candidate.exists():
                return candidate
        ns_dirs = [
            p for p in root.iterdir()
            if p.is_dir() and p.name.startswith(".case_manager")
        ]
        for ns in ns_dirs:
            p = ns / POLICY_FILENAME
            if p.exists():
                return p
        return None

    @staticmethod
    def _validate_fresh_root(root: Path) -> None:
        """Refuse to create a working directory in a non-empty root with no policy record.

        Called only when no policy file was found.
        """
        if root.exists() and not CaseManager._is_empty_dir(root):
            raise CacheRootStateError(root)
        if not root.exists() and not root.parent.exists():
            raise CacheRootStateError(root, detail="parent directory missing")

    @staticmethod
    def _is_empty_dir(path: Path) -> bool:
        if not path.is_dir():
            return False
        return not any(path.iterdir())

    # ------------------------------------------------------------------
    # Working-directory lease
    # ------------------------------------------------------------------

    async def _acquire_filespace(self) -> None:
        """Claim exclusive ownership of the working directory. Idempotent within a session.

        Leaves an already-held lease alone so re-recover does not fail on itself.
        """
        if self._filespace_lease.is_active():
            return
        await acquire_manager_lease(self._filespace_lease)

    def _beat_filespace(self) -> None:
        """Refresh the working-directory lease; loss of ownership surfaces as loop failure.

        ``heartbeat`` self-throttles, so calling it every pulse is cheap.
        """
        if not self._filespace_lease.is_active():
            return
        self._filespace_lease.heartbeat()

    def _release_filespace(self) -> None:
        """Release the working-directory lease so the next owner need not wait it out."""
        if not self._filespace_lease.is_active():
            return
        try:
            self._filespace_lease.release()
        except Exception:
            logger.exception("Could not release the filespace lease; it will lapse instead")

    # ------------------------------------------------------------------
    # Run-mode loops
    # ------------------------------------------------------------------

    async def _manager_loop(self) -> None:
        """One tick: maintenance (including mailbox intake), then advance the pool.

        Maintenance runs first so externally queued advances are eligible in the
        same tick before choke-resource budget is spent. Pacing is inside
        ``driver.advance()``. This loop sleeps only on the failure path.
        """
        interval = self._policy.maintenance_interval_secs
        consecutive_failures = 0
        while self._running and not self._stopping:
            try:
                await self._maintenance_tick()
                await self._driver.advance(suggested_interval_secs=interval)
                self._reconcile_terminal_in_pool()
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                logger.exception(
                    "Manager loop iteration failed (%d consecutive of %d allowed)",
                    consecutive_failures,
                    _LOOP_FAILURE_LIMIT,
                )
                if consecutive_failures >= _LOOP_FAILURE_LIMIT:
                    if self._loop_failure_cb is not None:
                        logger.error(
                            "Manager loop giving up after %d consecutive failures; "
                            "invoking on_loop_failure",
                            consecutive_failures,
                        )
                        self._loop_failure_cb(exc)
                        return
                    raise
                await asyncio.sleep(interval)

    async def _pulse_loop(self) -> None:
        """Liveness heartbeat for the event loop (separate from the advance tick).

        Stamps ``_last_pulse`` every ``PULSE_INTERVAL_SECS`` (watchdog signal) and
        writes the manifest heartbeat on its own cadence so a slow queued advance
        does not make a healthy manager look stale.
        """
        while self._running:
            now = time.monotonic()
            self._last_pulse = now
            # Losing the filespace lease is not a pulse hiccup to log and carry on
            # from: it means another manager is on this cache root and this one must
            # stop touching it. Hand it to the loop-failure path, which is the seam
            # that already knows how to take a manager down.
            try:
                self._beat_filespace()
            except LeaseOwnershipLostError as exc:
                logger.critical(
                    "Filespace ownership lost — another manager has taken this cache root. "
                    "Standing down rather than competing with it: %r", exc,
                )
                # Clearing _running also winds the manager loop down, so even a bare
                # embedded manager with no failure callback stops driving.
                self._running = False
                if self._loop_failure_cb is not None:
                    self._loop_failure_cb(exc)
                return
            if now - self._last_heartbeat_write >= self._policy.maintenance_interval_secs:
                try:
                    self._write_manifest(running=True)
                except Exception:
                    logger.exception("Pulse loop manifest heartbeat write failed")
                else:
                    self._last_heartbeat_write = now
            await asyncio.sleep(PULSE_INTERVAL_SECS)

    async def _maintenance_tick(self) -> None:
        """One maintenance pass: terminate/eject/quarantine tickets, mailbox, purge.

        Each item is isolated so one bad ticket is quarantined/escalated without
        aborting the rest of the tick or exhausting the loop-failure budget.
        """
        self._last_tick_started = time.monotonic()
        loop = asyncio.get_running_loop()
        for callback in self._maintenance_cbs:
            with self._isolated_tick_item("maintenance callback", self._manager_dir):
                await callback()
        for ticket_file in replay_pending(self._manager_dir):
            with self._isolated_tick_item("termination ticket", ticket_file):
                await self._drive_ticket(
                    "termination", ticket_file, self._advance_termination_ticket
                )
        for ticket_file in pending_quarantine_tickets(self._manager_dir):
            with self._isolated_tick_item("quarantine ticket", ticket_file):
                await self._drive_ticket(
                    "quarantine", ticket_file, self._advance_quarantine_ticket
                )
        eject_pending = eject_dir(self._manager_dir) / "pending"
        if eject_pending.exists():
            for ticket_file in sorted(eject_pending.glob("*.yaml")):
                with self._isolated_tick_item("eject ticket", ticket_file):
                    await self._drive_ticket(
                        "eject", ticket_file, self._advance_eject_ticket
                    )
        if self._policy.redundant_purge_terminal_after_secs is not None or (
            self._policy.redundant_purge_quarantined_after_secs is not None
        ):
            with self._isolated_tick_item("redundant purge", self._manager_dir):
                await loop.run_in_executor(
                    None, lambda: run_redundant_purge(self._store, self._policy)
                )
        self._publish_fleet_status_board()
        with self._isolated_tick_item("condition detection", self._manager_dir):
            self._detect_escalations()
        self._last_tick_completed = time.monotonic()

    # ------------------------------------------------------------------
    # Terminate / eject / quarantine tickets
    # ------------------------------------------------------------------

    async def _drive_ticket(
        self, what: str, ticket_file: Path, advance: Callable[[Path], Awaitable[None]]
    ) -> None:
        """Process one terminate/eject/quarantine ticket, counting attempts outside it.

        Unparseable tickets cannot update their own ``retry_count``; without an
        external ledger they would retry every tick forever. Below the threshold
        this re-raises so isolation can log/notice and the next tick can retry.
        Past the retry budget, a corrupt or otherwise unprocessable ticket is
        retired and the named case is quarantined (see
        ``_retire_unprocessable_ticket``).
        """
        try:
            await advance(ticket_file)
        except Exception:
            if self._ticket_attempts.record_failure(ticket_file) < (
                self._policy.termination_max_retries
            ):
                raise
            await self._retire_unprocessable_ticket(what, ticket_file)
        else:
            self._ticket_attempts.forget(ticket_file)

    async def _retire_unprocessable_ticket(self, what: str, ticket_file: Path) -> None:
        """Retire a ticket and stop driving the case it names.

        Quarantines the case rather than inventing a status change the caller did
        not ask for. ``case_id`` is taken from the ticket filename — the one field
        a corrupt ticket cannot remove. Recoverable via ``reopen_case()``.
        """
        case_id = ticket_file.stem
        self._ticket_attempts.forget(ticket_file)
        logger.error(
            "Abandoning unprocessable %s ticket for case %s after %d attempts",
            what,
            case_id,
            self._policy.termination_max_retries,
        )
        failed_dir = ticket_file.parent.parent / "failed"
        try:
            failed_dir.mkdir(parents=True, exist_ok=True)
            ticket_file.replace(failed_dir / ticket_file.name)
        except OSError:
            logger.exception("Could not retire %s to %s; unlinking", ticket_file, failed_dir)
            ticket_file.unlink(missing_ok=True)

        self._notices.emit_simple(
            "TICKET_ABANDONED", case_id, ticket_file, f"{what} ticket is unprocessable"
        )
        # The waiter map is populated only by eject, so this settles an eject
        # caller who would otherwise wait on a ticket that no longer exists.
        waiter = self._eject_waiters.pop(case_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(
                EjectAbandonedError(case_id=case_id, reason="eject ticket is unprocessable")
            )

        folder = await self._store.resolve_path(case_id)
        if folder is not None:
            await self._quarantine(
                case_id, folder, f"{what} ticket could not be processed"
            )

    async def _advance_termination_ticket(self, ticket_file: Path) -> None:
        ticket = TerminationTicket.load(str(ticket_file), acquire_lock=False)
        await process_pending_ticket(
            ticket,
            ticket_file,
            store=self._store,
            policy=self._policy,
            manager_dir=self._manager_dir,
            quarantine=self._quarantine,
            emit_notice=self._emit_notice,
        )

    async def _advance_quarantine_ticket(self, ticket_file: Path) -> None:
        ticket = QuarantineTicket.load(str(ticket_file), acquire_lock=False)
        landed = await process_quarantine_ticket(
            ticket,
            ticket_file,
            store=self._store,
            manager_dir=self._manager_dir,
            max_retries=self._policy.termination_max_retries,
        )
        if landed is not None:
            self._emit_notice("CASE_QUARANTINED", ticket.case_id, landed, ticket.reason)
            self._resolve_quarantine_waiter(ticket.case_id, landed)

    async def _advance_eject_ticket(self, ticket_file: Path) -> None:
        """Process one eject ticket and complete its waiter either way.

        If the ticket is retired to ``failed/`` without resolving the waiter,
        ``eject_from_pool()`` callers without a timeout would hang.
        """
        ticket = EjectTicket.load(str(ticket_file), acquire_lock=False)
        try:
            result = await process_eject_ticket(
                ticket,
                ticket_file,
                store=self._store,
                policy=self._policy,
                manager_dir=self._manager_dir,
            )
        except EjectAbandonedError as exc:
            self._notices.emit_simple(
                "EJECT_FAILED", ticket.case_id, Path(ticket.case_folder), exc.reason
            )
            fut = self._eject_waiters.pop(ticket.case_id, None)
            if fut is not None and not fut.done():
                fut.set_exception(exc)
            return
        if result is None:
            return
        self._emit_notice("CASE_EJECTED", ticket.case_id, result.export_folder)
        fut = self._eject_waiters.pop(ticket.case_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _count_termination_pending(self) -> int:
        return len(replay_pending(self._manager_dir))

    def _count_eject_pending(self) -> int:
        pending = eject_dir(self._manager_dir) / "pending"
        return len(list(pending.glob("*.yaml"))) if pending.exists() else 0

    def _has_departure_ticket(self, case_id: str) -> bool:
        """True when eject or quarantine work for this case is already pending."""
        return (
            eject_ticket_path(self._manager_dir, case_id).exists()
            or quarantine_ticket_exists(self._manager_dir, case_id)
        )

    def _departures_in_flight(self) -> bool:
        """True while any terminate, eject, or quarantine work is still pending."""
        return bool(
            replay_pending(self._manager_dir)
            or pending_quarantine_tickets(self._manager_dir)
            or self._count_eject_pending()
        )

    # ------------------------------------------------------------------
    # Pool status board & events
    # ------------------------------------------------------------------

    def _fleet_locate(self) -> Callable[[str], Any]:
        return lambda cid: self.locate(cid)

    def _notify_fleet_board(self, case: FolderBackedCase, *, force: bool = False) -> None:
        if self._fleet_board is None:
            return
        try:
            self._fleet_board.notify(
                case,
                force=force,
                live_cases=list(self._driver),
                locate=self._fleet_locate(),
            )
        except Exception:
            logger.warning("fleet status board notify failed", exc_info=True)

    def _publish_fleet_status_board(self, *, force: bool = False) -> None:
        if self._fleet_board is None:
            return
        try:
            self._fleet_board.publish_full_if_due(
                list(self._driver),
                locate=self._fleet_locate(),
                force=force,
            )
        except Exception:
            logger.warning("fleet status board full flush failed", exc_info=True)

    def _on_fleet_board_event(self, event: CasePoolEvent) -> None:
        """Forward interesting pool events to the status board.

        REMOVED / EVICTED force a full publish so cases that left the pool drop
        off (append alone cannot remove a ``case_id`` under last-wins).
        """
        if self._fleet_board is None:
            return
        if event.event in (CasePoolEventNames.REMOVED, CasePoolEventNames.EVICTED):
            self._publish_fleet_status_board(force=True)
            return
        if event.event == CasePoolEventNames.ADVANCED:
            ar = event.advance_result
            if ar is None or not ar.progressed:
                return
        self._notify_fleet_board(event.case)

    def _reconcile_terminal_in_pool(self) -> int:
        """Enqueue termination for terminal cases still in the pool.

        Isolated per case so a failed ticket write cannot abort the rest of the
        pass or spend the loop-failure budget.
        """
        count = 0
        for case in self._driver.terminal_cases():
            if ticket_exists(self._manager_dir, case.case_id):
                continue
            if self._store.status_of(case.case_id) not in (LIVE, None):
                continue    # already departed; its stored status is the receipt
            with self._isolated_tick_item("terminal reconcile", case.case_folder):
                if self._fleet_board is not None:
                    self._fleet_board.note_terminal(case)
                    self._notify_fleet_board(case, force=True)
                if begin_termination(
                    case,
                    manager_dir=self._manager_dir,
                    policy=self._policy,
                    driver_remove=self._driver.remove,
                ):
                    count += 1
        return count

    def _on_terminated_event(self, event: CasePoolEvent) -> None:
        if self._fleet_board is not None:
            self._fleet_board.note_terminal(event.case)
            self._notify_fleet_board(event.case, force=True)
        begin_termination(
            event.case,
            manager_dir=self._manager_dir,
            policy=self._policy,
            driver_remove=self._driver.remove,
        )

    async def _readmit_orphans(self) -> OrphanReadmitReport:
        """Recover phase: restore live cases that belong in the pool but are missing.

        See ``case_manager_support.readmit``. Lives here because it needs the driver
        and type registry, not only the store. Results appear on ``RecoverReport``.
        Not safe to run while the manager is already advancing cases (fights ticket
        replay).
        """
        return readmit_orphans(
            store=self._store,
            driver=self._driver,
            registry=self._registry,
            has_departure_ticket=self._has_departure_ticket,
            emit_anomaly=lambda cid, folder, msg: self._notices.emit_simple(
                "READMIT_ANOMALY", cid, folder, msg
            ),
        )

    # ------------------------------------------------------------------
    # Case operation helpers
    # ------------------------------------------------------------------

    async def _halt_and_settle(
        self, case_folder: Path, *, case_id: str, timeout: float | None
    ) -> None:
        """Stop scheduling a case and wait until it is idle.

        ``request_halt()`` returns immediately; ``HALTED`` is emitted when the case
        is neither mid-advance nor scheduled. If already halted, returns without
        waiting for another event.
        """
        folder = case_folder.resolve()
        if any(c.case_folder.resolve() == folder for c in self._driver.halted_cases()):
            return

        settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def on_halted(event: CasePoolEvent) -> None:
            if not settled.done() and event.case.case_folder.resolve() == folder:
                settled.set_result(None)

        # Subscribe before requesting: an already-idle case fires HALTED
        # synchronously inside request_halt(), and we must not miss it.
        handle = self._driver.case_event_subscribe(CasePoolEventNames.HALTED, on_halted)
        try:
            self._driver.request_halt(case_folder)
            if timeout is None:
                await settled
            else:
                try:
                    await asyncio.wait_for(settled, timeout=timeout)
                except asyncio.TimeoutError:
                    raise EjectTimeoutError(
                        case_id=case_id, stuck=self.collect_stuck_triggers()
                    ) from None
        finally:
            try:
                self._driver.case_event_unsubscribe(handle)
            except KeyError:
                pass

    async def _quarantine(self, case_id: str, folder: Path, reason: str) -> Path | None:
        landed = await quarantine_case(
            self._store, self._manager_dir, case_id, folder, reason
        )
        if landed is not None:
            self._emit_notice("CASE_QUARANTINED", case_id, landed, reason)
            self._resolve_quarantine_waiter(case_id, landed)
        return landed

    def _resolve_quarantine_waiter(self, case_id: str, landed: Path) -> None:
        fut = self._quarantine_waiters.get(case_id)
        if fut is not None and not fut.done():
            fut.set_result(landed)

    async def _scan_adopt_drop(self) -> dict[str, int]:
        drop = self._manager_dir / self._policy.adopt_drop_subdir
        seen = admitted = rejected = skipped = 0
        if not drop.exists():
            return {"seen": 0, "admitted": 0, "rejected": 0, "skipped": 0}
        for child in sorted(drop.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("ADOPT_REJECTED_"):
                skipped += 1
                continue
            seen += 1
            result = await self.adopt_case(child)
            if result.status == "completed":
                admitted += 1
            else:
                rejected += 1
                new_name = drop / f"ADOPT_REJECTED_{child.name}"
                child.rename(new_name)
        return {"seen": seen, "admitted": admitted, "rejected": rejected, "skipped": skipped}

    def _readers_at(self, status: str) -> Iterator[FolderBackedCaseReader]:
        for entry in self._store.iter_by_status(status):
            yield FolderBackedCaseReader(entry.case_folder)

    def _to_location(self, entry: CaseEntry) -> CaseLocation:
        reader = FolderBackedCaseReader(entry.case_folder)
        return CaseLocation(
            case_id=entry.case_id,
            external_key=reader.case_external_key,
            case_folder=entry.case_folder,
            status=entry.status,
            in_pool=self._driver.get_by_case_id(entry.case_id) is not None,
            terminal=reader.case_is_terminal,
        )

    def _resolve_single(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
    ) -> CaseLocation | None:
        if (case_id is None) == (case_folder is None):
            raise InvalidAddressingError()
        if case_folder is not None:
            case_id = self._store.case_id_at(case_folder)
            if case_id is None:
                return None
        return self.locate(case_id)

    def _emit_notice(
        self, kind: str, case_id: str, folder: Path | None, detail: str | None = None
    ) -> None:
        self._notices.emit_simple(kind, case_id, folder, detail)

    def _readmit_after_failed_reclassify(
        self, folder: Path, case: FolderBackedCase
    ) -> None:
        """Best-effort return of a case to the pool after a failed reclassify."""
        try:
            readd = self._registry.rehydrate(folder) if case.case_is_detached else case
            self._driver.add(readd)
        except Exception:
            logger.exception(
                "reclassify_case: failed to re-admit %s after reclassify error", folder
            )

    async def _quarantine_after_reclassify_assertion_failure(
        self,
        case_id: str,
        folder: Path,
        exc: ReclassifyAssertionError,
    ) -> None:
        """Park a post-commit reclassify whose new-class assertions failed.

        The type switch already stuck; detach the rebound instance (so the lease
        does not block relocation) and quarantine with a reason covering every
        failure. Best-effort: quarantine problems are logged, never substituted
        for the original ``ReclassifyAssertionError``.
        """
        parts = []
        for f in exc.failures:
            label = f"{exc.state}.{f.name}" if f.name else f.source
            parts.append(f"{label}: {f.msg}")
        reason = (
            f"reclassify to {exc.target_type} at {exc.state}: assertion failures: "
            + "; ".join(parts)
        )
        try:
            if not exc.case.case_is_detached:
                exc.case.case_detach()
        except Exception:
            logger.exception(
                "reclassify_case: failed to detach %s before quarantine", case_id,
            )
        try:
            await self._quarantine(case_id, folder, reason)
        except Exception:
            logger.exception(
                "reclassify_case: failed to quarantine %s after assertion failure",
                case_id,
            )

    # ------------------------------------------------------------------
    # Diagnostics & escalations
    # ------------------------------------------------------------------

    @contextmanager
    def _isolated_tick_item(self, what: str, source: Path) -> Iterator[None]:
        """Contain one maintenance-tick item's failure. See ``_maintenance_tick``.
        """
        try:
            yield
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Maintenance tick: %s failed (%s); skipping", what, source)
            self._notices.emit_simple(
                "MAINTENANCE_ITEM_FAILED", None, source, f"{what}: {exc!r}"
            )

    async def _settle_with_diagnostics(self) -> None:
        if hasattr(self._driver, "settle"):
            await self._driver.settle()

    def collect_stuck_triggers(self) -> list[StuckTrigger]:
        """In-flight triggers still running — for host shutdown diagnostics."""
        stuck: list[StuckTrigger] = []
        for case in self._driver.in_flight_cases():
            active = FolderBackedCaseReader(case.case_folder).case_active_trigger
            stuck.append(
                StuckTrigger(
                    case_id=case.case_id,
                    trigger=active.trigger if active else "unknown",
                    elapsed_secs=active.elapsed_secs if active else 0.0,
                    case_folder=case.case_folder,
                )
            )
        return stuck

    def _detect_escalations(self) -> None:
        if self._policy.escalation_fail_threshold is not None:
            for case in self._driver:
                if case.case_transition_fail_count >= self._policy.escalation_fail_threshold:
                    self._notices.emit_simple(
                        "REPEATED_FAILURE",
                        case.case_id,
                        case.case_folder,
                        case_state=case.case_state,
                    )
        if self._policy.escalation_stall_secs is not None:
            for case in self._driver:
                if (
                    not case.case_is_terminal
                    and case.case_dwell_secs >= self._policy.escalation_stall_secs
                ):
                    self._notices.emit_simple(
                        "STALLED", case.case_id, case.case_folder, case_state=case.case_state
                    )
        if self._policy.escalation_blocked:
            for case in self._driver.blocked_cases():
                self._notices.emit_simple(
                    "AUTO_BLOCKED", case.case_id, case.case_folder, case_state=case.case_state
                )
