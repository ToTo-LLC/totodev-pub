# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Adopt workflow: validate, transfer, verify, admit.

The order matters. Validation happens against the *source* so a bad folder is
rejected before anything is moved, and verification happens after the transfer
because a move that half-succeeded must not be admitted as a live case."""

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
from totodev_pub.case_manager_support.layout import read_case_id_from_folder
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import CaseTypeRegistry
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME
from totodev_pub.folder_backed_case_support.exceptions import UnregisteredCaseTypeError

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
    from totodev_pub.case_manager_support.case_store import LocalCaseStore

logger = logging.getLogger(__name__)


class AdoptRejectReason(str, Enum):
    ACTIVE_LEASE = "active_lease"
    DUPLICATE_CASE_ID = "duplicate_case_id"
    UNREGISTERED_TYPE = "unregistered_type"
    MALFORMED_RECORD = "malformed_record"
    INVALID_SOURCE = "invalid_source"


class AdoptResult(BaseModel, FileMappedPydanticMixin):
    kind: Literal["adopt"] = "adopt"
    status: Literal["completed", "rejected", "error"]
    case_id: str
    case_folder: str | None = None
    source_folder: str
    # Set when a failed adopt's residue was quarantined outright. None also covers
    # the case where quarantine was deferred behind a still-held lease.
    quarantine_folder: str | None = None
    correlation_id: str
    rejection_reason: str | None = None
    completed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _is_valid_adopt_source(
    source: Path, manager_dir: Path, policy: "CaseManagerPolicy", store: "LocalCaseStore"
) -> bool:
    """A source must be somewhere the store does not already manage.

    Adopting a folder out of managed storage would mean taking a case the store
    already owns and admitting it a second time.

    Staging and adopt-drop are scratch, and stay valid sources wherever the
    manager namespace is parked. With the default layout the namespace is a
    sibling of the storage buckets, so they would pass the ``owns_path`` test
    below anyway; name it after a bucket and they would not, which is what the
    check ahead of that test is for.
    """
    source = source.resolve()
    for scratch in (policy.staging_subdir, policy.adopt_drop_subdir):
        try:
            source.relative_to((manager_dir / scratch).resolve())
            return True
        except ValueError:
            continue
    return not store.owns_path(source)


def validate_adopt_source(
    source_folder: Path,
    *,
    store: "LocalCaseStore",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    registry: CaseTypeRegistry,
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
    if not _is_valid_adopt_source(source, manager_dir, policy, store):
        return None, AdoptRejectReason.INVALID_SOURCE, "source inside managed grouping bucket"
    case_id = read_case_id_from_folder(source)
    if not case_id:
        return None, AdoptRejectReason.MALFORMED_RECORD, "case_id unreadable"
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
    store: "LocalCaseStore",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    registry: CaseTypeRegistry,
    driver_add: Callable[[FolderBackedCase], None],
    case_id_exists: Callable[[str], bool],
    quarantine: Callable[..., Awaitable[Path | None]],
    correlation_id: str | None = None,
) -> AdoptResult:
    corr = correlation_id or str(uuid.uuid4())
    source = Path(source_folder).resolve()

    case_id, reject_reason, detail = validate_adopt_source(
        source,
        store=store,
        policy=policy,
        manager_dir=manager_dir,
        registry=registry,
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

    admitted: FolderBackedCase | None = None
    try:
        dest = await store.create_location(case_id)
        if (dest / RECORD_NAME).exists():
            raise DuplicateCaseIdError(case_id)
        _transfer_into_slave(source, dest)
        if read_case_id_from_folder(dest) != case_id:
            raise ValueError("case_id mismatch after transfer")
        admitted = registry.rehydrate(dest)   # acquires the heartbeat lease
        driver_add(admitted)
        return AdoptResult(
            status="completed",
            case_id=case_id,
            case_folder=str(dest),
            source_folder=str(source),
            correlation_id=corr,
        )
    except Exception as exc:
        logger.exception("Adopt failed for %s from %s", case_id, source)
        # rehydrate() took the lease. If the failure came after that -- a rejected
        # driver_add, say -- the case still holds it, and quarantine legitimately
        # refuses to relocate a leased folder. Release it first so the failure
        # path can finish instead of deferring behind a lease nobody will drop.
        if admitted is not None and not admitted.case_is_detached:
            try:
                admitted.case_detach()
            except Exception:
                logger.exception("Adopt failure path: could not detach %s", case_id)
        quarantine_path = None
        residue = await store.resolve_path(case_id)
        if residue is not None and residue.exists():
            try:
                landed = await quarantine(case_id, residue, str(exc))
                quarantine_path = None if landed is None else str(landed)
            except Exception:
                # Quarantine is best-effort; the caller still gets an "error"
                # result naming the original failure rather than this one.
                logger.exception("Adopt failure path: quarantine failed for %s", case_id)
        return AdoptResult(
            status="error",
            case_id=case_id,
            source_folder=str(source),
            quarantine_folder=quarantine_path,
            correlation_id=corr,
            rejection_reason=str(exc),
        )
