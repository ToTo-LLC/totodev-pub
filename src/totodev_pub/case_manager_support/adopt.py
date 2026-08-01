# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Adopt workflow: validate, transfer, verify, admit (§5.2)."""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.exceptions import DuplicateCaseIdError
from totodev_pub.case_manager_support.constants import PLACEHOLDER_HEADER
from totodev_pub.case_manager_support.layout import (
    live_grouping_key,
    read_case_id_from_folder,
    ref_path_for_case,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import CaseTypeRegistry
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME
from totodev_pub.folder_backed_case_support.exceptions import UnregisteredCaseTypeError

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)


class AdoptRejectReason(str, Enum):
    ACTIVE_LEASE = "active_lease"
    DUPLICATE_CASE_ID = "duplicate_case_id"
    UNREGISTERED_TYPE = "unregistered_type"
    MALFORMED_RECORD = "malformed_record"
    INVALID_SOURCE = "invalid_source"
    EXPECTED_CASE_ID_MISMATCH = "expected_case_id_mismatch"


class AdoptResult(BaseModel, FileMappedPydanticMixin):
    status: Literal["completed", "rejected", "error"]
    case_id: str
    case_folder: str | None = None
    source_folder: str
    aberrant_folder: str | None = None
    correlation_id: str
    rejection_reason: str | None = None
    completed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _is_valid_adopt_source(source: Path, manager_dir: Path, policy: "CaseManagerPolicy") -> bool:
    source = source.resolve()
    staging_root = (manager_dir / policy.staging_subdir).resolve()
    adopt_drop = (manager_dir / policy.adopt_drop_subdir).resolve()
    try:
        source.relative_to(staging_root)
        return True
    except ValueError:
        pass
    try:
        source.relative_to(adopt_drop)
        return True
    except ValueError:
        pass
    cache_root = manager_dir.parent.resolve()
    try:
        rel = source.relative_to(cache_root)
    except ValueError:
        # External staging outside the cache root is always valid.
        return True
    # Inside cache: reject paths under managed life-stage buckets.
    if rel.parts:
        head = rel.parts[0]
        if head in (policy.live_bucket, policy.aberrant_bucket) or head.startswith(
            f"{policy.terminal_prefix}_"
        ):
            return False
    return True


def validate_adopt_source(
    source_folder: Path,
    *,
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    registry: CaseTypeRegistry,
    expected_case_id: str | None,
    case_id_exists: Callable[[str], bool],
) -> tuple[str | None, AdoptRejectReason | None, str | None]:
    source = Path(source_folder).resolve()
    if not source.is_dir():
        return None, AdoptRejectReason.MALFORMED_RECORD, "source is not a directory"
    if not (source / RECORD_NAME).exists():
        return None, AdoptRejectReason.MALFORMED_RECORD, "case_record.yaml missing"
    lease_state = FolderBackedCase.is_heartbeat_expired(source)
    if lease_state is False:
        return None, AdoptRejectReason.ACTIVE_LEASE, "active lease on source"
    if not _is_valid_adopt_source(source, manager_dir, policy):
        return None, AdoptRejectReason.INVALID_SOURCE, "source inside managed grouping bucket"
    case_id = read_case_id_from_folder(source)
    if not case_id:
        return None, AdoptRejectReason.MALFORMED_RECORD, "case_id unreadable"
    if expected_case_id is not None and expected_case_id != case_id:
        return None, AdoptRejectReason.EXPECTED_CASE_ID_MISMATCH, "expected_case_id mismatch"
    if case_id_exists(case_id):
        return None, AdoptRejectReason.DUPLICATE_CASE_ID, "duplicate case_id"
    try:
        registry.peek_class(source, return_class_object=True)
    except UnregisteredCaseTypeError:
        return None, AdoptRejectReason.UNREGISTERED_TYPE, "unregistered case type"
    except (FileNotFoundError, ValueError) as exc:
        return None, AdoptRejectReason.MALFORMED_RECORD, str(exc)
    return case_id, None, None


def _transfer_into_slave(source: Path, dest_slave: Path) -> None:
    """Move detached case folder contents into an existing (possibly empty) slave dir."""
    dest_slave.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        target = dest_slave / child.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(child), str(target))
    source.rmdir()


async def adopt_case_folder(
    source_folder: Path,
    *,
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    registry: CaseTypeRegistry,
    driver_add: Callable[[FolderBackedCase], None],
    case_id_exists: Callable[[str], bool],
    move_to_aberrant: Callable[..., Awaitable[Path]],
    correlation_id: str | None = None,
    expected_case_id: str | None = None,
) -> AdoptResult:
    corr = correlation_id or str(uuid.uuid4())
    source = Path(source_folder).resolve()

    case_id, reject_reason, detail = validate_adopt_source(
        source,
        cache=cache,
        policy=policy,
        manager_dir=manager_dir,
        registry=registry,
        expected_case_id=expected_case_id,
        case_id_exists=case_id_exists,
    )
    if reject_reason is not None:
        return AdoptResult(
            status="rejected",
            case_id=case_id or "",
            source_folder=str(source),
            correlation_id=corr,
            rejection_reason=f"{reject_reason.value}: {detail}",
        )

    ref_path = ref_path_for_case(policy, case_id)
    grouping = live_grouping_key(policy)

    try:
        import tempfile
        from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(PLACEHOLDER_HEADER)
            tmp_path = tmp.name
        try:
            proxy = LocalFileProxy(tmp_path, ref_path=ref_path, delete_after_deploy=True)
            await cache.upsert_file(proxy, grouping)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        dest_slave = cache.get_slave_dir(grouping, ref_path)
        if (dest_slave / RECORD_NAME).exists():
            raise DuplicateCaseIdError(case_id)
        _transfer_into_slave(source, dest_slave)
        if read_case_id_from_folder(dest_slave) != case_id:
            raise ValueError("case_id mismatch after transfer")
        case = registry.rehydrate(dest_slave)
        driver_add(case)
        return AdoptResult(
            status="completed",
            case_id=case_id,
            case_folder=str(dest_slave),
            source_folder=str(source),
            correlation_id=corr,
        )
    except Exception as exc:
        logger.exception("Adopt failed for %s from %s", case_id, source)
        aberrant_path = None
        dest_slave = cache.get_slave_dir(grouping, ref_path) if cache.find_file(ref_path, grouping) else None
        if dest_slave is not None and dest_slave.exists():
            aberrant_path = str(
                await move_to_aberrant(case_id, dest_slave, str(exc), from_grouping=grouping)
            )
        return AdoptResult(
            status="error",
            case_id=case_id,
            source_folder=str(source),
            aberrant_folder=aberrant_path,
            correlation_id=corr,
            rejection_reason=str(exc),
        )
