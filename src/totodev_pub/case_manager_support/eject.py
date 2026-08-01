# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Eject from pool: export detached folder out of managed filespace (§5.11)."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.constants import EJECT_SUBDIR
from totodev_pub.case_manager_support.layout import live_grouping_key, ref_path_for_case

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
    from totodev_pub.folder_backed_case import FolderBackedCase

logger = logging.getLogger(__name__)


class EjectState(str, Enum):
    PENDING = "pending"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"


class EjectTicket(BaseModel, FileMappedPydanticMixin):
    case_id: str
    export_to_folder: str
    source_ref_path: str
    case_folder: str
    enqueued_at: str
    state: EjectState = EjectState.PENDING
    retry_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class EjectResult:
    case_id: str
    export_folder: Path
    source_ref_path: str
    completed_at: datetime


def eject_dir(manager_dir: Path) -> Path:
    return manager_dir / EJECT_SUBDIR


def eject_ticket_path(manager_dir: Path, case_id: str, *, subdir: str = "pending") -> Path:
    return eject_dir(manager_dir) / subdir / f"{case_id}.yaml"


def begin_eject(
    case: "FolderBackedCase",
    *,
    export_to_folder: Path,
    manager_dir: Path,
    policy: "CaseManagerPolicy",
    request_halt: Callable[[Path], None],
    wait_halted: Callable[[Path], asyncio.Future | None],
    driver_remove: Callable[[Path], "FolderBackedCase"],
) -> EjectTicket:
    folder = case.case_folder
    request_halt(folder)
    if wait_halted(folder) is not None:
        pass  # caller awaits halt separately
    driver_remove(folder)
    case.case_detach()
    ticket = EjectTicket(
        case_id=case.case_id,
        export_to_folder=str(export_to_folder.resolve()),
        source_ref_path=ref_path_for_case(policy, case.case_id),
        case_folder=str(folder),
        enqueued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    path = eject_ticket_path(manager_dir, case.case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ticket.save(str(path), retain_lock=False)
    return ticket


async def process_eject_ticket(
    ticket: EjectTicket,
    ticket_file: Path,
    *,
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
) -> EjectResult | None:
    case_id = ticket.case_id
    ref_path = ticket.source_ref_path
    src_grouping = live_grouping_key(policy)
    ref = cache.find_file(ref_path, src_grouping)
    if ref is None:
        ticket_file.unlink(missing_ok=True)
        export = Path(ticket.export_to_folder)
        return EjectResult(
            case_id=case_id,
            export_folder=export,
            source_ref_path=ref_path,
            completed_at=datetime.now(timezone.utc),
        )

    export_path = Path(ticket.export_to_folder)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    ticket.state = EjectState.EXPORTING
    ticket.save(str(ticket_file), retain_lock=False)

    try:
        slave = ref.slave_dir_path
        if export_path.exists():
            shutil.rmtree(export_path)
        try:
            shutil.move(str(slave), str(export_path))
        except OSError:
            shutil.copytree(slave, export_path)
        # Remove cache entry
        await cache.delete_file(ref_path, src_grouping)
        done = eject_dir(manager_dir) / "done" / f"{case_id}.yaml"
        ticket.state = EjectState.COMPLETED
        ticket.save(str(done), retain_lock=False)
        ticket_file.unlink(missing_ok=True)
        return EjectResult(
            case_id=case_id,
            export_folder=export_path,
            source_ref_path=ref_path,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        ticket.retry_count += 1
        ticket.last_error = str(exc)
        if ticket.retry_count >= policy.eject_max_retries:
            failed = eject_dir(manager_dir) / "failed" / f"{case_id}.yaml"
            ticket.state = EjectState.FAILED
            ticket.save(str(failed), retain_lock=False)
            ticket_file.unlink(missing_ok=True)
        else:
            ticket.save(str(ticket_file), retain_lock=False)
        logger.exception("Eject export failed for %s", case_id)
        return None
