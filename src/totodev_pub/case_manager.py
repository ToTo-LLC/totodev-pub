# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseManager — fleet coordinator for folder-backed cases (§1)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TYPE_CHECKING

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.case_manager_support.aberrant import move_case_to_aberrant
from totodev_pub.case_manager_support.adopt import AdoptResult, adopt_case_folder
from totodev_pub.case_manager_support.case_manager_config import CaseManagerConfig
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest, ManifestPaths
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.constants import (
    FLEET_STATUS_FILENAME,
    MANIFEST_FILENAME,
    POLICY_FILENAME,
    PULSE_INTERVAL_SECS,
)
from totodev_pub.case_manager_support.eject import (
    EjectResult,
    EjectTicket,
    begin_eject,
    eject_ticket_path,
    process_eject_ticket,
)
from totodev_pub.case_manager_support.escalation import CaseEscalation, EscalationRegistry
from totodev_pub.case_manager_support.fleet_status import (
    FleetStatusBoardWriter,
    ensure_board_file,
)
from totodev_pub.case_manager_support.exceptions import (
    AmbiguousExternalKeyError,
    CacheRootStateError,
    CaseManagerStopTimeoutError,
    EjectTimeoutError,
    InvalidAddressingError,
    LiveCaseNotFoundError,
    PolicyFileMissingError,
    PolicyMismatchError,
    RecoverRequiredError,
    StuckTrigger,
)
from totodev_pub.case_manager_support.layout import (
    CaseLocation,
    aberrant_grouping_key,
    folder_matches_case_tree,
    iter_all_managed_folders,
    iter_case_folders_in_grouping,
    live_grouping_key,
    normalize_case_folder_path,
    policy_manager_dir,
    ref_path_for_case,
    write_placeholder_file,
)
from totodev_pub.case_manager_support.mailbox.processor import MailboxProcessor
from totodev_pub.case_manager_support.purge import PurgeReport, run_redundant_purge
from totodev_pub.case_manager_support.reap import ReapReport, reap as run_reap
from totodev_pub.case_manager_support.recover import RecoverReport, recover_manager
from totodev_pub.case_manager_support.staging import allocate_staging_folder
from totodev_pub.case_manager_support.termination import (
    TerminationTicket,
    begin_termination,
    enqueue_termination_from_disk,
    process_pending_ticket,
    replay_pending,
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
from totodev_pub.folder_backed_case_support.queued_case_pool_driver import QueuedCasePoolDriver
from totodev_pub.folder_backed_case_support.tiered_case_pool_driver import TieredCasePoolDriver

logger = logging.getLogger(__name__)

_TIER1_KWARGS = frozenset(CaseManagerPolicy.tier1_field_names())
_TIER2_KWARGS = frozenset(CaseManagerPolicy.tier2_field_names())

# §1 loop hardening: consecutive failed loop iterations before the manager
# gives up retrying and hands the failure to the host (or re-raises).
_LOOP_FAILURE_LIMIT = 3


class CaseManager:
    """Fleet coordinator composing cache, driver, registry, and protocol dirs."""

    def __init__(
        self,
        config: CaseManagerConfig,
        *,
        driver: CasePoolDriver | None = None,
        cache: CachedFileFolders | None = None,
    ) -> None:
        self._config = config
        self._policy = config.policy
        self._cache_root = Path(config.cache_root)
        self._manager_dir = config.manager_dir
        self._registry = config.registry or case_type_registry
        if config.register_types:
            self._registry.register_case_types(*config.register_types)
        self._cache = cache or config.cache_override or CachedFileFolders(
            self._policy.grouping_pattern,
            str(self._cache_root),
        )
        self._driver = driver or config.driver or self._build_default_driver()
        self._escalations = EscalationRegistry()
        self._mailbox = MailboxProcessor(self)
        self._running = False
        self._stopping = False
        self._run_task: asyncio.Task[None] | None = None
        self._loop_failure_cb: Callable[[BaseException], None] | None = None
        self._terminated_handle: Any = None
        self._eject_waiters: dict[str, asyncio.Future[EjectResult]] = {}
        # §2 liveness stamps (read by the watchdog thread; write-only here).
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

    # ------------------------------------------------------------------
    # Construction: provision / attach / open
    # ------------------------------------------------------------------

    @classmethod
    def provision(
        cls,
        cache_root: str | Path,
        policy: CaseManagerPolicy | None = None,
        **overrides: Any,
    ) -> Path:
        root = Path(cache_root).resolve()
        cls._validate_fresh_root_for_provision(root)

        pol = policy or CaseManagerPolicy()
        tier1 = {k: v for k, v in overrides.items() if k in _TIER1_KWARGS}
        tier2 = {k: v for k, v in overrides.items() if k in _TIER2_KWARGS}
        for k, v in tier1.items():
            setattr(pol, k, v)
        pol = pol.apply_tier2_overrides(**tier2)

        policy_path = policy_manager_dir(root, pol) / POLICY_FILENAME
        if policy_path.exists():
            existing = CaseManagerPolicy.load(str(policy_path), acquire_lock=False)
            if existing.model_dump() != pol.model_dump():
                raise PolicyMismatchError("policy", file_value="on disk", override_value="supplied")
            return root

        mgr_dir = policy_manager_dir(root, pol)
        mgr_dir.mkdir(parents=True, exist_ok=True)
        pol.save(str(policy_path), retain_lock=False)
        cls._ensure_namespace_dirs(mgr_dir, pol)
        CachedFileFolders(pol.grouping_pattern, str(root))
        return root

    @classmethod
    def attach(cls, cache_root: str | Path, **wiring: Any) -> "CaseManager":
        root = Path(cache_root).resolve()
        policy_path = cls._find_policy_path(root)
        if policy_path is None:
            raise PolicyFileMissingError(root / ".case_manager" / POLICY_FILENAME)

        policy = CaseManagerPolicy.load(str(policy_path), acquire_lock=False)
        tier1_overrides = {k: v for k, v in wiring.items() if k in _TIER1_KWARGS}
        tier2_overrides = {k: v for k, v in wiring.items() if k in _TIER2_KWARGS}
        for field, override in tier1_overrides.items():
            file_val = getattr(policy, field)
            if file_val != override:
                raise PolicyMismatchError(field, file_value=file_val, override_value=override)

        effective = policy.apply_tier2_overrides(**tier2_overrides)
        mgr_dir = policy_manager_dir(root, policy)

        config = CaseManagerConfig(
            cache_root=root,
            policy=effective,
            policy_path=policy_path,
            manager_dir=mgr_dir,
            tier2_overrides=tier2_overrides,
            driver=wiring.get("driver"),
            driver_class=wiring.get("driver_class"),
            driver_kwargs=wiring.get("driver_kwargs") or {},
            registry=wiring.get("registry"),
            register_types=wiring.get("register_types") or (),
            cache_override=wiring.get("cache"),
        )
        manager = cls(config, driver=config.driver, cache=config.cache_override)
        manager._log_startup_summary()
        return manager

    @classmethod
    def open(cls, cache_root: str | Path, **overrides: Any) -> "CaseManager":
        root = Path(cache_root).resolve()
        policy_path = cls._find_policy_path(root)
        if policy_path is not None:
            return cls.attach(root, **overrides)
        if not root.exists() or cls._is_empty_dir(root):
            cls.provision(root, **overrides)
            return cls.attach(root, **overrides)
        raise CacheRootStateError(root)

    @classmethod
    def open_balanced(cls, cache_root: str | Path, **overrides: Any) -> "CaseManager":
        return cls.open(cache_root, **overrides)

    @classmethod
    def open_queued(cls, cache_root: str | Path, **overrides: Any) -> "CaseManager":
        return cls.open(cache_root, driver_class=QueuedCasePoolDriver, **overrides)

    @classmethod
    def open_inprocess(cls, cache_root: str | Path, **overrides: Any) -> "CaseManager":
        return cls.open(cache_root, enable_mailbox=False, **overrides)

    @classmethod
    def open_testing(cls, cache_root: str | Path, **overrides: Any) -> "CaseManager":
        return cls.open(
            cache_root,
            startup_adopt_scan=True,
            redundant_purge_terminal_after_secs=None,
            redundant_purge_aberrant_after_secs=None,
            **overrides,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def recover(self) -> RecoverReport:
        report = await recover_manager(self)
        self._recovered = True
        self.last_recover_report = report
        return report

    async def start(self) -> None:
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

    async def _manager_loop(self) -> None:
        interval = self._policy.maintenance_interval_secs
        consecutive_failures = 0
        while self._running and not self._stopping:
            try:
                await self._driver.advance(suggested_interval_secs=interval)
                self._reconcile_terminal_in_pool()
                await self._maintenance_tick()
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
            if now - self._last_heartbeat_write >= self._policy.maintenance_interval_secs:
                try:
                    self._write_manifest(running=True)
                except Exception:
                    logger.exception("Pulse loop manifest heartbeat write failed")
                else:
                    self._last_heartbeat_write = now
            await asyncio.sleep(PULSE_INTERVAL_SECS)

    async def _maintenance_tick(self) -> None:
        self._last_tick_started = time.monotonic()
        loop = asyncio.get_running_loop()
        for ticket_file in replay_pending(self._manager_dir):
            ticket = TerminationTicket.load(str(ticket_file), acquire_lock=False)
            await loop.run_in_executor(
                None,
                lambda t=ticket, p=ticket_file: process_pending_ticket(
                    t,
                    p,
                    cache=self._cache,
                    policy=self._policy,
                    manager_dir=self._manager_dir,
                    move_to_aberrant=self._move_to_aberrant_sync,
                    emit_escalation=self._emit_termination_failed,
                ),
            )
        eject_pending = self._manager_dir / "eject" / "pending"
        if eject_pending.exists():
            for ticket_file in sorted(eject_pending.glob("*.yaml")):
                ticket = EjectTicket.load(str(ticket_file), acquire_lock=False)
                result = await process_eject_ticket(
                    ticket,
                    ticket_file,
                    cache=self._cache,
                    policy=self._policy,
                    manager_dir=self._manager_dir,
                )
                if result and ticket.case_id in self._eject_waiters:
                    fut = self._eject_waiters.pop(ticket.case_id)
                    if not fut.done():
                        fut.set_result(result)
        await self._mailbox.maintenance_tick()
        if self._policy.redundant_purge_terminal_after_secs is not None or (
            self._policy.redundant_purge_aberrant_after_secs is not None
        ):
            await loop.run_in_executor(None, lambda: run_redundant_purge(self._cache, self._policy))
        self._publish_fleet_status_board()
        self._detect_escalations()
        self._last_tick_completed = time.monotonic()

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
        count = 0
        for case in self._driver.terminal_cases():
            if not ticket_exists(self._manager_dir, case.case_id):
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

    async def reap(self) -> ReapReport:
        return run_reap(
            cache=self._cache,
            policy=self._policy,
            driver=self._driver,
            registry=self._registry,
            enqueue_from_disk=lambda f: enqueue_termination_from_disk(
                f, manager_dir=self._manager_dir, policy=self._policy
            ),
            has_eject_ticket=lambda cid: eject_ticket_path(
                self._manager_dir, cid
            ).exists(),
            emit_anomaly=lambda cid, folder, msg: self._escalations.emit_simple(
                "REAP_ANOMALY", cid, folder, msg
            ),
        )

    async def run_redundant_purge(self) -> PurgeReport:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: run_redundant_purge(self._cache, self._policy)
        )

    # ------------------------------------------------------------------
    # Case fleet API
    # ------------------------------------------------------------------

    async def adopt_case(
        self,
        source_folder: Path,
        *,
        correlation_id: str | None = None,
        expected_case_id: str | None = None,
    ) -> AdoptResult:
        existing = self._load_adopt_result(correlation_id)
        if existing is not None:
            return existing
        result = await adopt_case_folder(
            Path(source_folder),
            cache=self._cache,
            policy=self._policy,
            manager_dir=self._manager_dir,
            registry=self._registry,
            driver_add=self._driver.add,
            case_id_exists=self._case_id_exists,
            move_to_aberrant=self._move_to_aberrant_sync,
            correlation_id=correlation_id,
            expected_case_id=expected_case_id,
        )
        if result.status == "rejected":
            self._escalations.emit_simple(
                "ADOPT_REJECTED", result.case_id or None, Path(source_folder), result.rejection_reason
            )
        elif result.status == "error":
            self._escalations.emit_simple(
                "ADOPT_FAILED", result.case_id, Path(source_folder), result.rejection_reason
            )
        self._publish_adopt_result(result)
        return result

    def allocate_staging_folder(self) -> Path:
        return allocate_staging_folder(self._manager_dir, self._policy)

    async def eject_from_pool(
        self,
        case_id: str,
        *,
        export_to_folder: Path,
        timeout: float | None = None,
    ) -> EjectResult:
        case = self.get_live(case_id)
        fut: asyncio.Future[EjectResult] = asyncio.get_running_loop().create_future()
        self._eject_waiters[case_id] = fut
        begin_eject(
            case,
            export_to_folder=export_to_folder,
            manager_dir=self._manager_dir,
            policy=self._policy,
            request_halt=self._driver.request_halt,
            wait_halted=lambda f: None,
            driver_remove=self._driver.remove,
        )
        if timeout is not None:
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                stuck = self._collect_stuck_triggers()
                raise EjectTimeoutError(case_id=case_id, stuck=stuck)
        return await fut

    async def reopen_case(self, case_id: str) -> None:
        loc = self.locate(case_id=case_id)
        if loc is None or loc.in_pool:
            raise LiveCaseNotFoundError(case_id)
        ref_path = ref_path_for_case(self._policy, case_id)
        self._cache.move_file(
            ref_path,
            ref_path,
            grouping_key=loc.grouping_key,
            new_grouping_key=live_grouping_key(self._policy),
        )
        new_loc = self.locate(case_id=case_id)
        assert new_loc is not None
        case = self._registry.rehydrate(new_loc.case_folder)
        self._driver.add(case)

    def get_live(self, case_id: str) -> FolderBackedCase:
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
        loc = self._resolve_single(case_id=case_id, case_folder=case_folder)
        if loc is None:
            raise LiveCaseNotFoundError(case_id or str(case_folder))
        return await self._driver.fire(loc.case_folder, trigger, **trigger_kwargs)

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
            # Best-effort re-admission so the case never falls out of management.
            try:
                readd = case
                if readd.case_is_detached:
                    readd = self._registry.rehydrate(folder)
                self._driver.add(readd)
            except Exception:
                logger.exception(
                    "reclassify_case: failed to re-admit %s after reclassify error", folder
                )
            raise
        self._driver.add(fresh)          # fresh slot: advanceable cases are admitted HOT
        self._driver.boost(folder)       # and fire on the next beat
        self._notify_fleet_board(fresh)
        return fresh

    def locate(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
    ) -> CaseLocation | None:
        if (case_id is None) == (case_folder is None):
            raise InvalidAddressingError()
        if case_id is not None:
            for cid, folder, gk in iter_all_managed_folders(self._cache, self._policy):
                if cid == case_id:
                    return self._to_location(cid, folder, gk)
            return None
        folder = normalize_case_folder_path(case_folder)
        for cid, f, gk in iter_all_managed_folders(self._cache, self._policy):
            if folder_matches_case_tree(folder, f):
                return self._to_location(cid, f, gk)
        return None

    def locate_all(self, *, external_key: str) -> list[CaseLocation]:
        hits: list[CaseLocation] = []
        for cid, folder, gk in iter_all_managed_folders(self._cache, self._policy):
            reader = FolderBackedCaseReader(folder)
            if reader.case_external_key == external_key:
                hits.append(self._to_location(cid, folder, gk))
        return hits

    def reader(
        self,
        *,
        case_id: str | None = None,
        external_key: str | None = None,
        case_folder: Path | None = None,
    ) -> FolderBackedCaseReader:
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
        return [FolderBackedCaseReader(loc.case_folder) for loc in self.locate_all(external_key=external_key)]

    def iter_live_pool(self) -> Iterator[FolderBackedCaseReader]:
        for case in self._driver:
            yield FolderBackedCaseReader(case.case_folder)

    def iter_live_bucket(self) -> Iterator[FolderBackedCaseReader]:
        for _cid, folder, _gk in iter_case_folders_in_grouping(
            self._cache, live_grouping_key(self._policy)
        ):
            yield FolderBackedCaseReader(folder)

    def iter_terminal(self, *, grouping_glob: str | None = None) -> Iterator[FolderBackedCaseReader]:
        glob = grouping_glob or f"{self._policy.terminal_prefix}_*"
        for grouping in self._cache.groupings(filters=[glob]):
            gk = grouping.grouping_key
            if gk is None:
                continue
            for _cid, folder, _gk in iter_case_folders_in_grouping(self._cache, gk):
                yield FolderBackedCaseReader(folder)

    def iter_aberrant(self) -> Iterator[FolderBackedCaseReader]:
        for _cid, folder, _gk in iter_case_folders_in_grouping(
            self._cache, aberrant_grouping_key(self._policy)
        ):
            yield FolderBackedCaseReader(folder)

    def iter_quarantine(self, *, grouping_glob: str = "quarantine_*") -> Iterator[FolderBackedCaseReader]:
        for grouping in self._cache.groupings(filters=[grouping_glob]):
            gk = grouping.grouping_key
            if gk is None:
                continue
            for _cid, folder, _gk in iter_case_folders_in_grouping(self._cache, gk):
                yield FolderBackedCaseReader(folder)

    def on_escalation(self, callback: Callable[[CaseEscalation], None]) -> int:
        return self._escalations.register(callback)

    def off_escalation(self, handle: int) -> None:
        self._escalations.unregister(handle)

    def on_loop_failure(self, callback: Callable[[BaseException], None]) -> None:
        """Register the single host callback invoked when the manager loop gives
        up after repeated consecutive failures. A host (``serve()``) wires this
        to the watchdog's kill ladder; with no callback registered the loop
        re-raises instead (embedded usage — logged loudly, task dies)."""
        self._loop_failure_cb = callback

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_default_driver(self) -> CasePoolDriver:
        cls = self._config.driver_class or TieredCasePoolDriver
        kwargs = dict(self._config.driver_kwargs)
        if cls in (TieredCasePoolDriver, QueuedCasePoolDriver):
            kwargs.setdefault("concurrency_ceiling", self._policy.concurrency_ceiling)
            kwargs.setdefault("choke_limits", self._policy.choke_limits)
        return cls(**kwargs)

    def _to_location(self, case_id: str, folder: Path, grouping_key: tuple[str, ...]) -> CaseLocation:
        reader = FolderBackedCaseReader(folder)
        in_pool = any(c.case_id == case_id for c in self._driver)
        return CaseLocation(
            case_id=case_id,
            external_key=reader.case_external_key,
            case_folder=folder,
            grouping_key=grouping_key,
            in_pool=in_pool,
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

    def _case_id_exists(self, case_id: str) -> bool:
        return self.locate(case_id=case_id) is not None

    def _move_to_aberrant_sync(
        self, case_id: str, folder: Path, reason: str, *, from_grouping: tuple[str, ...] | None = None
    ) -> Path:
        return move_case_to_aberrant(
            self._cache,
            self._policy,
            self._manager_dir,
            case_id,
            folder,
            reason,
            from_grouping=from_grouping,
        )

    def _emit_termination_failed(
        self, kind: str, case_id: str, folder: Path, detail: str | None
    ) -> None:
        self._escalations.emit_simple(kind, case_id, folder, detail)

    def _replay_termination_pending(self) -> int:
        return len(replay_pending(self._manager_dir))

    def _replay_eject_pending(self) -> int:
        pending = self._manager_dir / "eject" / "pending"
        return len(list(pending.glob("*.yaml"))) if pending.exists() else 0

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

    def _ensure_namespace(self) -> None:
        self._ensure_namespace_dirs(self._manager_dir, self._policy)

    @staticmethod
    def _ensure_namespace_dirs(mgr_dir: Path, policy: CaseManagerPolicy) -> None:
        mgr_dir.mkdir(parents=True, exist_ok=True)
        for sub in (
            policy.staging_subdir,
            policy.adopt_drop_subdir,
            policy.fire_mailbox_subdir,
            policy.adopt_mailbox_subdir,
            "results",
            "termination/pending",
            "termination/failed",
            "termination/done",
            "eject/pending",
            "eject/failed",
            "eject/done",
            "aberrant",
        ):
            (mgr_dir / sub).mkdir(parents=True, exist_ok=True)
        for sub in ("intake", "malformed"):
            (mgr_dir / policy.fire_mailbox_subdir / sub).mkdir(parents=True, exist_ok=True)
        for sub in ("intake", "malformed", "executing"):
            (mgr_dir / policy.reclassify_mailbox_subdir / sub).mkdir(parents=True, exist_ok=True)
        (mgr_dir / policy.adopt_mailbox_subdir / "intake").mkdir(parents=True, exist_ok=True)
        (mgr_dir / policy.adopt_mailbox_subdir / "pending").mkdir(parents=True, exist_ok=True)
        ensure_board_file(mgr_dir, enabled=policy.enable_fleet_status_board)
        live = mgr_dir.parent / policy.live_bucket
        live.mkdir(parents=True, exist_ok=True)

    def _write_manifest(self, *, running: bool = False, stopped: bool = False) -> None:
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
            results=rel(self._manager_dir / "results"),
            adopt_drop=rel(self._manager_dir / self._policy.adopt_drop_subdir),
            termination_pending=rel(self._manager_dir / "termination" / "pending"),
            eject_pending=rel(self._manager_dir / "eject" / "pending"),
            staging=rel(self._manager_dir / self._policy.staging_subdir),
            fleet_status_board=rel(self._manager_dir / FLEET_STATUS_FILENAME),
        )
        manifest = CaseManagerManifest(
            cache_root=str(self._cache_root),
            manager_namespace=self._policy.manager_namespace,
            live_bucket=self._policy.live_bucket,
            terminal_prefix=self._policy.terminal_prefix,
            manifest_stale_secs=self._policy.manifest_stale_secs,
            paths=paths,
            heartbeat_at=CaseManagerManifest.utc_now_iso() if running else None,
            stopped_at=CaseManagerManifest.utc_now_iso() if stopped else None,
            pool_index=self._policy.journal_path if self._policy.journal_attach_steady_state else None,
        )
        manifest.save(str(self._manager_dir / MANIFEST_FILENAME), retain_lock=False)

    def _log_startup_summary(self) -> None:
        driver_name = type(self._driver).__name__
        logger.info(
            "CaseManager attach: cache_root=%s policy=%s driver=%s types=%d handlers=%d",
            self._cache_root,
            self._config.policy_path,
            driver_name,
            len(self._registry._registry),
            len(self._escalations._handlers),
        )

    def _load_adopt_result(self, correlation_id: str | None) -> AdoptResult | None:
        if not correlation_id:
            return None
        path = self._manager_dir / "results" / f"{correlation_id}.yaml"
        if path.exists():
            try:
                return AdoptResult.load(str(path), acquire_lock=False)
            except Exception:
                return None
        return None

    def _publish_adopt_result(self, result: AdoptResult) -> None:
        path = self._manager_dir / "results" / f"{result.correlation_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        result.save(str(path), retain_lock=False)

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
                    self._escalations.emit_simple(
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
                    self._escalations.emit_simple(
                        "STALLED", case.case_id, case.case_folder, case_state=case.case_state
                    )
        if self._policy.escalation_blocked:
            for case in self._driver.blocked_cases():
                self._escalations.emit_simple(
                    "AUTO_BLOCKED", case.case_id, case.case_folder, case_state=case.case_state
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
    def _validate_fresh_root_for_provision(root: Path) -> None:
        if root.exists() and not CaseManager._is_empty_dir(root):
            if CaseManager._find_policy_path(root) is None:
                raise CacheRootStateError(root)
        elif not root.exists():
            parent = root.parent
            if not parent.exists():
                raise CacheRootStateError(root, detail="parent directory missing")

    @staticmethod
    def _is_empty_dir(path: Path) -> bool:
        if not path.is_dir():
            return False
        return not any(path.iterdir())
