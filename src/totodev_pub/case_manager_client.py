# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Read-only introspection and request submission for out-of-process callers.

Stateless by design: every call reads current state rather than caching it, so a
long-lived worker never serves a stale answer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import FLEET_STATUS_FILENAME, MANIFEST_FILENAME
from totodev_pub.case_manager_support.exceptions import LiveCaseNotFoundError, ManagerNotFreshError
from totodev_pub.case_manager_support.fleet_status import FleetStatusRow, read_board
from totodev_pub.case_manager_support.fleet_status_watcher import FleetStatusBoardWatcher
from totodev_pub.case_manager_support.case_store import LIVE
from totodev_pub.case_manager_support.layout import CaseLocation, policy_manager_dir
from totodev_pub.case_manager_support.mailbox import MailboxTransport, RequestHandle
from totodev_pub.case_manager import CaseManager
from totodev_pub.folder_backed_case import IncompatibleReclassError
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.exceptions import UnregisteredCaseTypeError


class CaseManagerClient:
    """Read-only + mailbox submit surface for out-of-process tiers."""

    def __init__(self, cache_root: str | Path) -> None:
        self._cache_root = Path(cache_root).resolve()
        self._manager = CaseManager(self._cache_root)
        self._transport = MailboxTransport(self._manager._manager_dir, self._manager._policy)

    @classmethod
    def from_manifest(cls, manifest_path: str | Path) -> "CaseManagerClient":
        path = Path(manifest_path).resolve()
        return cls(path.parent.parent if path.parent.name.startswith(".case") else path.parent)

    def _manifest(self) -> CaseManagerManifest:
        mgr_dir = policy_manager_dir(self._cache_root, self._manager._policy)
        return CaseManagerManifest.load(
            str(mgr_dir / MANIFEST_FILENAME), acquire_lock=False
        )

    def _check_fresh(self, only_if_fresh: bool) -> None:
        if not only_if_fresh:
            return
        manifest = self._manifest()
        if manifest.stopped_at:
            raise ManagerNotFreshError(stopped_at=manifest.stopped_at)
        if manifest.heartbeat_at:
            hb = datetime.fromisoformat(manifest.heartbeat_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            if age > manifest.manifest_stale_secs:
                raise ManagerNotFreshError(heartbeat_at=manifest.heartbeat_at)

    def locate(self, case_id: str) -> CaseLocation | None:
        return self._manager.locate(case_id)

    def locate_all(self, *, external_key: str) -> list[CaseLocation]:
        return self._manager.locate_all(external_key=external_key)

    def reader(
        self,
        *,
        case_id: str | None = None,
        external_key: str | None = None,
        case_folder: Path | None = None,
    ) -> FolderBackedCaseReader:
        return self._manager.reader(
            case_id=case_id, external_key=external_key, case_folder=case_folder
        )

    def readers_by_external_key(self, external_key: str) -> list[FolderBackedCaseReader]:
        return self._manager.readers_by_external_key(external_key)

    def list_live_pool(self) -> list[CaseLocation]:
        return [
            self._manager.locate(r.case_id)
            for r in self._manager.iter_live_pool()
            if self._manager.locate(r.case_id) is not None
        ]

    def allocate_staging_folder(self, *, only_if_fresh: bool = True) -> Path:
        self._check_fresh(only_if_fresh)
        return self._manager.allocate_staging_folder()

    def submit_fire(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        trigger: str | None = None,
        trigger_kwargs: dict | None = None,
        only_if_fresh: bool = True,
    ) -> RequestHandle:
        self._check_fresh(only_if_fresh)
        return self._transport.submit_fire(
            case_id=case_id,
            case_folder=case_folder,
            trigger=trigger,
            trigger_kwargs=trigger_kwargs,
        )

    def submit_reclassify(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        target_type: str | type,
        only_if_fresh: bool = True,
        preflight: bool | Literal["strict"] = True,
    ) -> RequestHandle:
        """Queue a reclassification: switch a live pooled case to a different registered
        case type (see ``CaseManager.reclassify_case``). ``target_type`` may be a class
        or its bare registered name; only the name travels through the mailbox. The
        result (via ``wait_result``/``poll_result``) is a ``ReclassifyResult``.

        ``preflight`` runs the same-process, class-agnostic sanity checks below,
        synchronously, before the request ever touches the mailbox — so an obviously
        doomed request fails fast with the exact exception ``CaseManager.reclassify_case``
        would eventually raise, instead of round-tripping through a maintenance tick just
        to come back as an error result:

        - ``True`` (default): the case must be live (``LiveCaseNotFoundError`` otherwise).
          If — and only if — *this* process's case-type registry also knows
          ``target_type`` (i.e. the case classes were imported/registered here too, not
          just in the manager process), additionally check the shared-state contract
          (``IncompatibleReclassError`` otherwise). When the type is unknown to this
          process's registry that second check is silently skipped: a client tier that
          never imports case classes (the common two-process split) still gets the free
          existence check and nothing that requires class knowledge it doesn't have.
        - ``"strict"``: same, but a ``target_type`` this process's registry can't resolve
          raises ``UnregisteredCaseTypeError`` here instead of deferring to the manager.
          Use this only when the client process is known to import the full case-type
          catalog, so an unresolvable name is almost certainly a typo worth catching
          immediately rather than a class the client simply hasn't loaded.
        - ``False``: skip all of the above; submit unconditionally.

        A clean preflight is advisory, not a guarantee — the case can change state or
        leave the pool in the gap between this check and the manager's next maintenance
        tick. The manager-side check inside ``reclassify_case`` is authoritative and
        always runs regardless of this setting."""
        self._check_fresh(only_if_fresh)
        name = target_type if isinstance(target_type, str) else target_type.__name__
        if preflight:
            self._preflight_reclassify(
                case_id=case_id,
                case_folder=case_folder,
                target_type_name=name,
                strict=(preflight == "strict"),
            )
        return self._transport.submit_reclassify(
            case_id=case_id,
            case_folder=case_folder,
            target_type=name,
        )

    def _preflight_reclassify(
        self,
        *,
        case_id: str | None,
        case_folder: Path | None,
        target_type_name: str,
        strict: bool,
    ) -> None:
        # NOTE: check the stored status, not loc.in_pool — this client attaches its
        # OWN CaseManager/driver (a separate, unsynced in-memory pool from whatever
        # process is actually running the fleet), so in_pool would read as False for
        # every case, always. Status is a disk fact and needs no driver.
        loc = self._manager.locate(case_id) if case_id else (
            self._manager._resolve_single(case_folder=case_folder)
            if case_folder is not None else None
        )
        if loc is None or loc.status != LIVE:
            raise LiveCaseNotFoundError(case_id or (loc.case_id if loc else str(case_folder)))
        target_cls = self._manager._registry.resolve_case_type(target_type_name)
        if target_cls is None:
            if strict:
                raise UnregisteredCaseTypeError(target_type_name)
            return  # unresolvable here; defer to the manager's authoritative check
        reader = self._manager.reader(case_id=loc.case_id)
        if reader.case_state not in target_cls.case_type_spec().fsm.states:
            raise IncompatibleReclassError(reader.case_state, target_cls.__name__)

    def poll_result(self, handle: RequestHandle):
        return self._transport.poll_result(handle)

    async def wait_result(self, handle: RequestHandle, *, timeout: float = 30.0):
        return await self._transport.wait_result(handle, timeout=timeout)

    def fleet_status_board_path(self) -> Path:
        """The board's known location (manifest-advertised when available)."""
        try:
            manifest = self._manifest()
            if manifest.paths.fleet_status_board:
                return self._cache_root / manifest.paths.fleet_status_board
        except Exception:
            pass  # no/old manifest — the board location is fixed by policy anyway
        return (
            policy_manager_dir(self._cache_root, self._manager._policy)
            / FLEET_STATUS_FILENAME
        )

    def read_fleet_status(self, *, only_if_fresh: bool = True) -> dict[str, FleetStatusRow]:
        """One-file bulk fleet snapshot, last-wins merged by case_id.

        Raises FleetStatusBoardDisabledError when the deployment has the board
        disabled, and ManagerNotFreshError when only_if_fresh=True and the
        manager heartbeat is stale/stopped."""
        self._check_fresh(only_if_fresh)
        return read_board(self.fleet_status_board_path())

    def fleet_status_watcher(self, *, emit_initial: bool = False) -> FleetStatusBoardWatcher:
        """Snapshot-diff change watcher for LONG-LIVED observer processes (the
        diff baseline lives in watcher memory). Call poll() on your cadence."""
        return FleetStatusBoardWatcher(
            self.fleet_status_board_path(), emit_initial=emit_initial
        )

    def submit_adopt(
        self,
        source_folder: Path,
        *,
        correlation_id: str | None = None,
        only_if_fresh: bool = True,
    ) -> RequestHandle:
        self._check_fresh(only_if_fresh)
        return self._transport.submit_adopt(
            source_folder=source_folder,
            correlation_id=correlation_id,
        )

    def submit_shutdown(
        self,
        *,
        graceful: bool = False,
        reason: str | None = None,
        only_if_fresh: bool = True,
    ) -> RequestHandle:
        """Ask the manager process to shut down via the shutdown mailbox.

        ``graceful=True`` requests a drain (in-flight case steps settle first);
        the default is an immediate exit (leases lapse, recover() reconciles —
        the same hard path the system already tolerates, requested on purpose).

        This always exits nonzero — the case-manager process cannot be told
        through this API to stay down. Under a supervisor configured to restart
        on nonzero exit (e.g. Docker with ``restart: on-failure``, this process
        as PID 1 via an exec-form entrypoint), it will come back. To
        decommission permanently, stop it by other means (an orchestrator
        scale-down, an OS signal from something with process reach, or — if the
        manager itself should decide when it's done —
        ``serve(..., stop_when_empty=True)``). Kubernetes caveat: Deployments
        default to ``restartPolicy: Always``, which restarts exit 0 too — on
        k8s, decommission means scaling the workload down; the exit code alone
        cannot express "stay down" there.

        Result semantics: on the graceful path an "acknowledged, shutting down"
        result is written before the drain, so poll_result/wait_result resolve.
        On the immediate path the handle may never resolve — the manifest's
        ``stopped_at`` is the real confirmation either way."""
        self._check_fresh(only_if_fresh)
        return self._transport.submit_shutdown(graceful=graceful, reason=reason)

    async def wait_adopt(self, handle: RequestHandle, *, timeout: float = 60.0):
        return await self._transport.wait_result(handle, timeout=timeout)
