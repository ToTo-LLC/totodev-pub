# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Stateless companion for web workers (§11)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME
from totodev_pub.case_manager_support.exceptions import ManagerNotFreshError
from totodev_pub.case_manager_support.layout import CaseLocation, policy_manager_dir
from totodev_pub.case_manager_support.mailbox.processor import MailboxProcessor, RequestHandle
from totodev_pub.case_manager_support.staging import allocate_staging_folder
from totodev_pub.case_manager import CaseManager
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader


class CaseManagerClient:
    """Read-only + mailbox submit surface for out-of-process tiers."""

    def __init__(self, cache_root: str | Path) -> None:
        self._cache_root = Path(cache_root).resolve()
        self._manager = CaseManager.attach(self._cache_root)
        self._mailbox = MailboxProcessor(self._manager)

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

    def locate(self, *, case_id: str | None = None, case_folder: Path | None = None) -> CaseLocation | None:
        return self._manager.locate(case_id=case_id, case_folder=case_folder)

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
            self._manager.locate(case_id=r.case_id)
            for r in self._manager.iter_live_pool()
            if self._manager.locate(case_id=r.case_id) is not None
        ]

    def allocate_staging_folder(self, *, only_if_fresh: bool = True) -> Path:
        self._check_fresh(only_if_fresh)
        return allocate_staging_folder(
            self._manager._manager_dir, self._manager._policy
        )

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
        return self._mailbox.submit_fire(
            case_id=case_id,
            case_folder=case_folder,
            trigger=trigger,
            trigger_kwargs=trigger_kwargs,
        )

    def poll_result(self, handle: RequestHandle):
        return self._mailbox.poll_result(handle)

    async def wait_result(self, handle: RequestHandle, *, timeout: float = 30.0):
        return await self._mailbox.wait_result(handle, timeout=timeout)

    def submit_adopt(
        self,
        source_folder: Path,
        *,
        expected_case_id: str | None = None,
        correlation_id: str | None = None,
        only_if_fresh: bool = True,
    ) -> RequestHandle:
        self._check_fresh(only_if_fresh)
        return self._mailbox.submit_adopt(
            source_folder=source_folder,
            expected_case_id=expected_case_id,
            correlation_id=correlation_id,
        )

    async def wait_adopt(self, handle: RequestHandle, *, timeout: float = 60.0):
        return await self._mailbox.wait_result(handle, timeout=timeout)
