# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseManager — fleet coordinator for folder-backed cases.

Host entry point (process ownership, signals, exit codes, watchdog arm/park):
    from totodev_pub.case_manager_support.case_manager_host import serve

``serve(manager)`` is the only way to run a CaseManager as a whole process; see
that module for the full host contract (exit codes, signal wiring, watchdog
orchestration).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, NamedTuple, Sequence, TYPE_CHECKING

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
    CaseManagerStopTimeoutError,
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
from totodev_pub.folder_backed_case import FolderBackedCase, IncompatibleReclassError
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
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import BalancedCasePoolDriver

logger = logging.getLogger(__name__)

_LAYOUT_KWARGS = frozenset(CaseManagerPolicy.layout_field_names())
_TUNABLES_KWARGS = frozenset(CaseManagerPolicy.tunables_field_names())

# Consecutive failed loop iterations before the manager gives up retrying and
# hands the failure to the host (or re-raises). A loop that fails the same way
# every tick is wedged, not unlucky.
_LOOP_FAILURE_LIMIT = 3


def _first_supplied(*candidates: Any, default: Callable[[], Any]) -> Any:
    """First non-None candidate, else ``default()``.

    Truthiness is wrong for these collaborators — an empty pool driver is falsy
    — so injection is decided on identity with None alone.
    """
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return default()


class _ResolvedPolicy(NamedTuple):
    """A filespace's policy reconciled with what the caller supplied.

    ``is_new`` says the record does not exist yet and the caller is cleared to
    write it; every path derived here is valid either way.
    """

    policy: CaseManagerPolicy
    policy_path: Path
    manager_dir: Path
    tunables_overrides: dict[str, Any]
    is_new: bool


class CaseManager:
    """Fleet coordinator composing cache, driver, registry, and protocol dirs."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        filespace: LocalCaseStore | str | Path,
        *,
        driver: CasePoolDriver | None = None,
        driver_class: type | None = None,
        driver_kwargs: dict[str, Any] | None = None,
        registry: CaseTypeRegistry | None = None,
        register_types: Sequence[type[FolderBackedCase]] = (),
        **tunables_overrides: Any,
    ) -> None:
        """Build a manager over an existing filespace.

        ``filespace`` is either a ``LocalCaseStore`` or the path to a filespace
        that has already been opened once. Construction never creates one: a path
        with no policy record raises ``PolicyFileMissingError``, so bringing a
        filespace into existence always means naming ``open_local_store()``.

        Keyword arguments are the manager's Bindings — driver, registry, case types
        — plus any Tunables field, which is applied in memory and leaves the
        policy record untouched. A Layout field is fixed for the filespace and
        cannot be supplied here; one that disagrees with the record raises.
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
        self._registry = registry or case_type_registry
        if register_types:
            self._registry.register_case_types(*register_types)
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
            driver_class=driver_class,
            driver_kwargs=driver_kwargs or {},
            registry=registry,
            register_types=register_types,
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
        self._run_task: asyncio.Task[None] | None = None
        self._loop_failure_cb: Callable[[BaseException], None] | None = None
        self._terminated_handle: Any = None
        self._eject_waiters: dict[str, asyncio.Future[EjectResult]] = {}
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
        """Open the managed filespace at ``cache_root``, creating it when absent.

        The filespace is the policy record, the manager's protocol namespace, and
        the case storage taken together; this is the only thing that brings one
        into existence. Pass the returned store to ``CaseManager`` to get a
        manager over it. With ``init_if_new=False`` an absent policy record
        raises ``PolicyFileMissingError`` rather than being created.

        Idempotent: opening the same filespace again is a no-op, and opening it
        with a conflicting Layout field is loud. Tunables are applied to the
        returned store's policy in memory; on the call that creates the
        filespace they are also written into the record as its durable defaults.
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
    # Read-only lifecycle introspection (host/observability surface)
    # ------------------------------------------------------------------

    @property
    def is_recovered(self) -> bool:
        """True once recover() has completed (start() precondition)."""
        return self._recovered

    @property
    def is_running(self) -> bool:
        """True between start() and stop()."""
        return self._running

    @property
    def is_idle(self) -> bool:
        """No pooled cases, and no departure still in flight.

        The second half is not pedantry. A case leaves the pool when termination
        is *enqueued*, but its folder is archived a tick or more later, by the
        ticket. A job that exited the moment the pool emptied would leave its
        own output sitting in `live` with pending tickets — recoverable only by
        a restart that may never come, since the job is done.

        This is still only *half* of "is the whole system idle": a signaling
        adapter may hold requests that have not reached the pool yet, and the
        manager can no longer see them. The host composes the two."""
        return len(self._driver) == 0 and not self._departures_in_flight()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def recover(self) -> RecoverReport:
        """Take the filespace, reconcile storage, and rebuild the pool.

        Required before ``start()``, and where a crash is repaired: the filespace
        lease is claimed, case leases are waited out, orphans re-admitted, tickets
        counted. It can take tens of seconds after a hard kill, by design — a held
        lease is indistinguishable from a live owner's until it has been watched
        for longer than one beat. Raises ``CompetingManagerError`` if another
        manager owns this cache root.
        """
        report = await recover_manager(self)
        self._recovered = True
        self.last_recover_report = report
        return report

    async def start(self) -> None:
        """Begin the maintenance/sweep loop and the liveness pulse.

        Idempotent, and refuses a manager that has not recovered — starting one
        that has not reconciled with disk would schedule an empty pool over a
        filespace full of work.
        """
        if self._running:
            return
        if not self._recovered:
            raise RecoverRequiredError()
        self._running = True
        self._stopping = False
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

    async def stop(self, *, timeout: float | None = None) -> None:
        """Stop the loop and let in-flight steps settle.

        With ``timeout`` set, a settle that overruns raises
        ``CaseManagerStopTimeoutError`` carrying the triggers still running —
        which is the diagnosis, not just the failure.
        """
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
        if timeout is not None:
            try:
                await asyncio.wait_for(self._settle_with_diagnostics(), timeout=timeout)
            except asyncio.TimeoutError:
                stuck = self._collect_stuck_triggers()
                raise CaseManagerStopTimeoutError(
                    timeout_secs=timeout, stuck=stuck, detail={"pool_size": len(self._driver)}
                )
        else:
            await self._settle_with_diagnostics()
        self._publish_fleet_status_board(force=True)
        self._write_manifest(running=False, stopped=True)
        # Last, and only after the fleet has settled: releasing earlier would invite a
        # successor in while this one is still writing. A clean release is also what
        # spares that successor the lease-expiry wait a crash would have cost it.
        self._release_filespace()

    # ------------------------------------------------------------------
    # Case fleet API — intake & departure
    # ------------------------------------------------------------------

    async def adopt_case(
        self,
        source_folder: Path,
        *,
        expected_case_id: str | None = None,
    ) -> AdoptResult:
        """Take a detached case folder into managed storage and the live pool.

        Carries no correlation id: de-duplicating a re-delivered request is
        transport business, and the fleet has no opinion about whether two
        requests to adopt the same folder came from one client retrying.
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
            expected_case_id=expected_case_id,
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
        """An empty scratch folder inside managed space, for building a case to adopt.

        The folder comes back already created, because that is what both ways of
        filling it want: ``create_case_in_folder()`` accepts an existing empty
        directory, and ``copytree(..., dirs_exist_ok=True)`` copies into one. So
        building a case for adoption is three lines with no ceremony::

            staged = manager.allocate_staging_folder()
            MyCase.create_case_in_folder(staged, external_key="K-1")
            await manager.adopt_case(staged)

        Staging sits on the same filesystem as managed storage, so the transfer
        adopt performs is a rename rather than a copy. Adopt *consumes* what it is
        given, which is the other reason to stage: a case assembled here is
        expendable, where the caller's own folder is not.

        Sweeps abandoned staging folders as a side effect, so it is also the
        thing that keeps that space from growing.
        """
        return allocate_staging_folder(self._manager_dir, self._policy)

    async def eject_from_pool(
        self,
        case_id: str,
        *,
        export_to_folder: Path,
        timeout: float | None = None,
    ) -> EjectResult:
        """Export a case out of managed storage entirely, and wait for it.

        Halts the case first and waits for it to settle, so ejecting one that is
        mid-step works rather than raising. ``timeout`` bounds each phase; on
        expiry ``EjectTimeoutError`` names the triggers still running.
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
                stuck = self._collect_stuck_triggers()
                raise EjectTimeoutError(case_id=case_id, stuck=stuck)
        return await fut

    async def reopen_case(self, case_id: str) -> None:
        """Return a departed case to the live pool.

        The one status change that moves in the *gaining* direction, which is why
        it re-resolves the folder after the move rather than reusing the one it
        looked up beforehand.
        """
        loc = self.locate(case_id=case_id)
        if loc is None or loc.in_pool:
            raise LiveCaseNotFoundError(case_id)
        folder = await self._store.set_status(case_id, LIVE)
        self._driver.add(self._registry.rehydrate(folder))

    # ------------------------------------------------------------------
    # Case fleet API — live operations
    # ------------------------------------------------------------------

    def get_live(self, case_id: str) -> FolderBackedCase:
        """The live case object itself, for out-of-band work.

        Returns the *managed* instance, so anything done with it happens outside
        the pool's scheduling and bookkeeping. Prefer ``fire()`` unless that is
        specifically what you want.
        """
        for case in self._driver:
            if case.case_id == case_id:
                return case
        raise LiveCaseNotFoundError(case_id)

    async def fire(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        trigger: str | None = None,
        **trigger_kwargs: Any,
    ) -> AdvanceResult:
        """Queue one case step on the slot and await its sweep-time result.

        Addressing: exactly one of ``case_id`` / ``case_folder``. With
        ``trigger=None`` the case's auto edges are swept; with a pinned
        ``trigger`` that edge fires.

        Timing and resource promises:

        - **Requires a running manager.** Raises ``ManagerNotRunningError`` if
          the loop is not running. Mailbox files and this API share one queue:
          attach to the case's scheduling slot, execute at the slot's turn in
          the normal sweep (maintenance drains intake at the head of each tick,
          then the sweep launches due slots).
        - **Uniform capacity.** Queued fires use the same concurrency ceiling and
          beat-quantized choke budget as ordinary advances — they do **not** take
          the priority ``acquire_priority`` path used by the driver's immediate
          ``fire()`` primitive.
        - **One fire per turn, sequential per case.** Multiple fires on the same
          case queue on the slot and apply one sweep at a time; they preempt
          auto-advance while pending.
        - **Do not await this from inside a case step's own hook** for the same
          case — that deadlocks (the fire cannot run until the step finishes).
        - **Escape hatch:** for an immediate out-of-band step, ``get_live()`` and
          call a trigger directly on the case object. That skips pool events and
          scheduling bookkeeping (and is guarded by
          ``CaseTransitionInFlightError`` if a step is already running).
        """
        if not self._running:
            raise ManagerNotRunningError()
        loc = self._resolve_single(case_id=case_id, case_folder=case_folder)
        if loc is None:
            raise LiveCaseNotFoundError(case_id or str(case_folder))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[AdvanceResult] = loop.create_future()

        def on_complete(
            result: AdvanceResult | None, error: BaseException | None,
        ) -> None:
            if fut.done():
                return
            if error is not None:
                fut.set_exception(error)
            elif result is not None:
                fut.set_result(result)
            else:
                fut.set_exception(RuntimeError("fire completed with no result or error"))

        self._driver.attach_fire(
            loc.case_folder,
            trigger,
            trigger_kwargs,
            on_complete=on_complete,
        )
        return await fut

    def attach_fire(
        self,
        case_folder: Path,
        trigger: str | None,
        trigger_kwargs: dict[str, Any],
        *,
        on_launch: Callable[[], None] | None = None,
        on_complete: Callable[[AdvanceResult | None, BaseException | None], None] | None = None,
    ) -> None:
        """Queue a fire on a case's slot and return immediately.

        The fire-and-report half of ``fire()``, for a caller that reports the
        outcome somewhere other than an awaited return value — a transport
        publishing a result file, above all. Both callbacks run on the event-loop
        thread: ``on_launch`` when the sweep actually starts the step,
        ``on_complete`` when it settles either way.

        Public because the signaling adapter needs it. Reaching into
        ``manager._driver`` instead is how a transport ends up depending on the
        scheduling layer's internals.
        """
        self._driver.attach_fire(
            case_folder,
            trigger,
            trigger_kwargs,
            **({"on_launch": on_launch} if on_launch is not None else {}),
            **({"on_complete": on_complete} if on_complete is not None else {}),
        )

    async def reclassify_case(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        target_type: str | type[FolderBackedCase],
    ) -> FolderBackedCase:
        """Switch a live pooled case to a different registered case type, in place.

        Wraps ``FolderBackedCase.case_reclassify_to()`` with the pool choreography a
        managed fleet needs: the case's slot is removed for the identity switch and the
        fresh object re-admitted, which per the driver contract builds a NEW scheduling
        slot — a freshly admitted advanceable case starts HOT, and the follow-up
        ``boost()`` schedules it for the very next beat, so any automated paths the new
        type opened up are taken immediately.

        Addressing mirrors ``fire()``: exactly one of ``case_id`` / ``case_folder``.
        ``target_type`` may be the registered class or its bare name (the mailbox path
        always sends the name).

        Raises:
            LiveCaseNotFoundError: the case is not in the live pool.
            UnregisteredCaseTypeError: ``target_type`` is not a registered case type.
            IncompatibleReclassError: the case's current state is not a state of the
                target class (checked BEFORE the slot is touched).
            CaseInFlightError: a step is mid-flight for this case (from
                ``driver.remove()``); retry after the step settles.
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
        self._driver.remove(folder)      # raises CaseInFlightError if a step is running
        try:
            fresh = case.case_reclassify_to(target_cls)
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
    # Case fleet API — lookup & iteration
    # ------------------------------------------------------------------

    def locate(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
    ) -> CaseLocation | None:
        """Find a case at any status. Exactly one of ``case_id`` / ``case_folder``.

        Both directions are index lookups, not fleet scans — which matters
        because every addressed ``fire()`` resolves through here, so a scan would
        put an O(fleet) cost on the mailbox path.
        """
        if (case_id is None) == (case_folder is None):
            raise InvalidAddressingError()
        if case_folder is not None:
            case_id = self._store.case_id_at(case_folder)
            if case_id is None:
                return None
        entry = self._store.find(case_id)
        return None if entry is None else self._to_location(entry)

    def locate_all(self, *, external_key: str) -> list[CaseLocation]:
        """Every case carrying ``external_key``.

        Unlike ``locate()`` this is a full scan that reads each case's record —
        external keys are not indexed. Use ``locate(case_id=…)`` on any hot path.
        """
        hits: list[CaseLocation] = []
        for entry in self._store.iter_all():
            if FolderBackedCaseReader(entry.case_folder).case_external_key == external_key:
                hits.append(self._to_location(entry))
        return hits

    def reader(
        self,
        *,
        case_id: str | None = None,
        external_key: str | None = None,
        case_folder: Path | None = None,
    ) -> FolderBackedCaseReader:
        """A read-only view of one case, addressed any of three ways.

        Takes no lease and never rehydrates, so it is safe against a case
        another process is driving. ``external_key`` raises if it is ambiguous;
        the other two address exactly one case by construction.
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
        """Every case carrying ``external_key`` — the ambiguity-tolerant ``reader()``."""
        return [FolderBackedCaseReader(loc.case_folder) for loc in self.locate_all(external_key=external_key)]

    def iter_live_pool(self) -> Iterator[FolderBackedCaseReader]:
        """Readers over the cases the pool is actively driving right now.

        Narrower than ``iter_live_bucket()``, which is every case at live
        *status* whether or not this process holds it."""
        for case in self._driver:
            yield FolderBackedCaseReader(case.case_folder)

    def iter_live_bucket(self) -> Iterator[FolderBackedCaseReader]:
        """Every case at live status, whether or not the pool currently holds it."""
        yield from self._readers_at(LIVE)

    def iter_terminal(self, *, partition: str | None = None) -> Iterator[FolderBackedCaseReader]:
        """Archived cases, optionally narrowed to one archive partition."""
        for entry in self._store.iter_by_status(TERMINATED):
            if partition is None or entry.partition == partition:
                yield FolderBackedCaseReader(entry.case_folder)

    def iter_quarantine(self) -> Iterator[FolderBackedCaseReader]:
        """Cases the manager has stopped driving. See ``support.quarantine``."""
        yield from self._readers_at(QUARANTINED)

    # ------------------------------------------------------------------
    # Observability hooks
    # ------------------------------------------------------------------

    def on_notice(self, callback: Callable[[CaseNotice], None]) -> int:
        """Subscribe to manager notices. Returns a handle for ``off_notice()``.

        One channel carries problems and case-departure lifecycle facts alike;
        filter on ``notice.kind.is_lifecycle``. Handlers must not block — they
        run on the manager's own loop — and an exception in one is swallowed so
        it cannot take the manager down.
        """
        return self._notices.register(callback)

    def off_notice(self, handle: int) -> None:
        """Unsubscribe. Unknown handles are ignored."""
        self._notices.unregister(handle)

    def on_loop_failure(self, callback: Callable[[BaseException], None]) -> None:
        """Register the single host callback invoked when the manager loop gives
        up after repeated consecutive failures. A host (``serve()``) wires this
        to the watchdog's kill ladder; with no callback registered the loop
        re-raises instead (embedded usage — logged loudly, task dies)."""
        self._loop_failure_cb = callback

    def on_maintenance(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run ``callback`` at the head of every maintenance tick.

        The seam a signaling adapter attaches to. At the *head* deliberately:
        requests drained here reach the pool before the same tick's sweep, so a
        fire submitted between ticks is stepped by the very next sweep rather
        than the one after it.

        Each callback is isolated like any other tick item — one that raises is
        logged and noticed, and the rest of the tick still runs.
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

        Layout fields are fixed facts the storage was built on: a supplied value
        that disagrees with the record raises, naming the field. Tunables are
        applied in memory and never compared — the record holds their
        defaults, not their only permitted values.
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
        """Resolve against a store's own policy rather than re-reading the record.

        A store already is the filespace resolved, and its policy may carry Tunables
        tuning the record deliberately does not — re-reading here would discard
        exactly what the caller opened the store to set. Layout needs no check
        against the record: the store's Layout fields are what its storage was
        built on, which is the stronger of the two claims.
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
        """Sort override names into (Layout, Tunables), rejecting anything else.

        A name that is neither is a typo or a stale field, and silently dropping
        it would leave the caller believing a setting took effect. Bindings keys
        are accepted as named parameters on ``CaseManager``, not via this bag.
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
        """Construct the driver from ``self._config`` (driver_class/driver_kwargs).

        Beat-tempo tunables (``I0``, ``EAGER_BEAT_FRACTION``, ``BEAT_YIELD_FLOOR``,
        tier multiples, ...) live on the driver's ``TierPolicy`` and are NOT part of
        ``CaseManagerPolicy``. To override them, pass ``driver_kwargs={"policy":
        TierPolicy(...)}`` to ``CaseManager`` — see ``CaseManagerConfig``'s
        ``driver_kwargs`` field for details. Left unset, both concrete drivers run
        with ``TierPolicy()`` defaults, including the eager beat tempo enabled
        (``EAGER_BEAT_FRACTION = 0.25``)."""
        cls = self._config.driver_class or BalancedCasePoolDriver
        kwargs = dict(self._config.driver_kwargs)
        if cls in (BalancedCasePoolDriver, SeniorityCasePoolDriver):
            kwargs.setdefault("concurrency_ceiling", self._policy.concurrency_ceiling)
            kwargs.setdefault("choke_limits", self._policy.choke_limits)
        return cls(**kwargs)

    def _ensure_namespace(self) -> None:
        self._ensure_namespace_dirs(self._manager_dir, self._policy)

    @staticmethod
    def _ensure_namespace_dirs(mgr_dir: Path, policy: CaseManagerPolicy) -> None:
        """Create the manager's own protocol namespace — and nothing outside it.

        Storage buckets are the case store's to create; this only owns what lives
        under the manager namespace.
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
        """Guard the only path that can create a filespace.

        Reached solely when no policy record was found, so a non-empty root here
        is somebody else's directory: initialising over it is not a mistake worth
        making convenient.
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
    # Filespace lease
    # ------------------------------------------------------------------

    async def _acquire_filespace(self) -> None:
        """Claim the one-manager-per-cache-root lease. Idempotent within a session.

        Re-recovering must not trip over the lease this manager already holds, so
        an active lease is left alone rather than re-acquired.
        """
        if self._filespace_lease.is_active():
            return
        await acquire_manager_lease(self._filespace_lease)

    def _beat_filespace(self) -> None:
        """Refresh the filespace lease, surfacing a stolen one as loop failure.

        ``heartbeat`` self-throttles, so calling it every pulse costs a comparison
        on all but one tick in twenty.
        """
        if not self._filespace_lease.is_active():
            return
        self._filespace_lease.heartbeat()

    def _release_filespace(self) -> None:
        """Drop the filespace lease so the next owner need not wait it out."""
        if not self._filespace_lease.is_active():
            return
        try:
            self._filespace_lease.release()
        except Exception:
            logger.exception("Could not release the filespace lease; it will lapse instead")

    # ------------------------------------------------------------------
    # Manager loops
    # ------------------------------------------------------------------

    async def _manager_loop(self) -> None:
        """One tick = maintenance (mailbox intake first), then the pool sweep.

        Maintenance runs at the HEAD of the tick deliberately: externally submitted
        fire requests are executed before the sweep spends its beat-quantized choke
        budget, and the post-fire ``boost()`` lands before the sweep so the boosted
        case is stepped in this same tick rather than the next one.

        Pacing lives inside ``driver.advance()``: the advisory interval is its
        fixed-rate target period, and maintenance time between beats counts against
        that period. The loop itself only sleeps on the failure path, where
        ``advance()`` may have raised before pacing."""
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
        """Liveness pulse — measures the event loop, not the tick.

        Stamps ``_last_pulse`` every ``PULSE_INTERVAL_SECS`` (the watchdog's
        kill-authorized signal) and writes the manifest heartbeat on its own
        cadence, so a healthy manager doing one slow mailbox fire never looks
        stale to clients."""
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
        """One maintenance pass: termination tickets, eject tickets, mailbox, purge.

        Every item is isolated. A single malformed ticket must degrade to "that
        ticket is quarantined and escalated" rather than aborting the tick — an
        aborted tick silently skips the mailbox drain, the purge, and the board
        publish, and a ticket that fails the same way every tick would otherwise
        exhaust the loop-failure budget and take the whole manager down.
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
            self._policy.redundant_purge_aberrant_after_secs is not None
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
    # Ticket processing
    # ------------------------------------------------------------------

    async def _drive_ticket(
        self, what: str, ticket_file: Path, advance: Callable[[Path], Awaitable[None]]
    ) -> None:
        """Run one ticket, counting attempts somewhere the ticket cannot corrupt.

        A ticket's own ``retry_count`` is the right place to count — right up
        until the ticket is the thing that is broken. One that will not parse can
        never record that it was tried, so without an outside count it is retried
        every tick forever while the case it describes sits removed from the
        pool, detached, and un-enqueueable.

        Below the threshold this re-raises, so the enclosing isolation logs and
        notices exactly as before and the next tick tries again — a transient
        failure must not burn through the budget on its first occurrence. At the
        threshold the ticket is retired.
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
        """Give up on a ticket, and on driving the case it describes.

        The case is quarantined rather than repaired: the manager could not read
        what it was supposed to do, so guessing would mean inventing a departure
        the operator never asked for. Quarantine is the established posture for
        exactly this — stop interacting, record why, leave it recoverable via
        ``reopen_case()``.

        The ``case_id`` comes from the *filename*, which is the one field a
        corrupt ticket cannot take with it.
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

    async def _advance_eject_ticket(self, ticket_file: Path) -> None:
        """Drive one eject ticket and settle its waiter either way.

        A give-up has to reach the caller. ``eject_from_pool()`` resolves on the
        export completing, and with the ticket retired to ``failed/`` there is
        nothing left that could ever resolve it — a caller who passed no timeout
        would wait forever.
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
        """True when the case is already on its way out of the pool."""
        return (
            eject_ticket_path(self._manager_dir, case_id).exists()
            or quarantine_ticket_exists(self._manager_dir, case_id)
        )

    def _departures_in_flight(self) -> bool:
        """True while any case is on its way out of managed storage."""
        return bool(
            replay_pending(self._manager_dir)
            or pending_quarantine_tickets(self._manager_dir)
            or self._count_eject_pending()
        )

    # ------------------------------------------------------------------
    # Fleet status board & pool events
    # ------------------------------------------------------------------

    def _fleet_locate(self) -> Callable[[str], Any]:
        return lambda cid: self.locate(case_id=cid)

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
        """Interesting pool edges → notify the board; it decides append vs full flush.

        REMOVED / EVICTED need a full publish so departed non-terminal cases drop
        off the board (append alone cannot remove a case_id under last-wins).
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
        """Enqueue termination for terminal cases still sitting in the pool.

        Isolated per case. ``begin_termination`` is remove → detach → write ticket;
        a ticket write that raises leaves that one case out of the pool, detached,
        and ticketless, which is bad enough on its own — it must not also abort the
        pass for every other terminal case, nor spend the loop-failure budget.
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
        """A phase of ``recover()``. See ``case_manager_support.readmit``.

        The join across pool membership, the store's status, and the type
        registry stays here rather than moving into the store: answering it needs
        the driver and the registry, and injecting those into a storage object
        would rebuild the very dependency the store exists to remove.

        What it found reaches callers on the ``RecoverReport``; there is no reason
        to run it on its own, and running it mid-flight would fight ticket replay.
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
        """Stop scheduling a case and wait until it is genuinely idle.

        ``request_halt()`` returns immediately; ``HALTED`` fires once the case is
        neither in flight nor scheduled. A case that has already halted will not
        fire it again, so that is checked first rather than waited for.
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
                        case_id=case_id, stuck=self._collect_stuck_triggers()
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
        return landed

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
            in_pool=any(c.case_id == entry.case_id for c in self._driver),
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
        return self.locate(case_id=case_id, case_folder=case_folder)

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

    # ------------------------------------------------------------------
    # Diagnostics & escalations
    # ------------------------------------------------------------------

    @contextmanager
    def _isolated_tick_item(self, what: str, source: Path) -> Iterator[None]:
        """Contain one maintenance-tick item's failure. See ``_maintenance_tick``."""
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

    def _collect_stuck_triggers(self) -> list[StuckTrigger]:
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
