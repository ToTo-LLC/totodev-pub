# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Runtime manifest written by CaseManager (.case_manager/manifest.yaml)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.constants import MANIFEST_PROTOCOL_VERSION


class ManifestPaths(BaseModel):
    fire_mailbox_intake: str
    adopt_mailbox_intake: str
    results: str
    adopt_drop: str
    termination_pending: str
    eject_pending: str
    staging: str
    fleet_status_board: Optional[str] = None


class CaseManagerManifest(BaseModel, FileMappedPydanticMixin):
    """Machine-written runtime projection for clients (§12)."""

    protocol_version: int = MANIFEST_PROTOCOL_VERSION
    cache_root: str
    manager_namespace: str
    live_bucket: str
    terminal_prefix: str
    manifest_stale_secs: int = 30
    client_read_only_ok: bool = True
    paths: ManifestPaths
    heartbeat_at: Optional[str] = None
    stopped_at: Optional[str] = None
    pool_index: Optional[str] = None

    @staticmethod
    def utc_now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
