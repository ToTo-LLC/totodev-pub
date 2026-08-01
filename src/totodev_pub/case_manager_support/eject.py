# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Eject from pool: export a case out of managed filespace entirely.

The one departure whose destination is a path rather than a status. An ejected
case has left the store, and afterwards the manager reports it as absent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.constants import EJECT_SUBDIR
from totodev_pub.case_manager_support.exceptions import EjectAbandonedError

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
    from totodev_pub.case_manager_support.case_store import LocalCaseStore
    from totodev_pub.folder_backed_case import FolderBackedCase

logger = logging.getLogger(__name__)


class EjectState(str, Enum):
    """States a ticket can be *found* in. A completed eject has no ticket — the
    case is simply gone from the store and its folder sits at the export path."""

    PENDING = "pending"
    EXPORTING = "exporting"
    FAILED = "failed"


class EjectTicket(BaseModel, FileMappedPydanticMixin):
    case_id: str
    export_to_folder: str
    case_folder: str
    enqueued_at: str
    state: EjectState = EjectState.PENDING
    retry_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class EjectResult:
    case_id: str
    export_folder: Path
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
    driver_remove: Callable[[Path], "FolderBackedCase"],
) -> EjectTicket:
    """Sync slice: remove, detach, enqueue.

    **The case must already be halted.** The driver refuses to remove one whose
    advance is in flight, so awaiting HALTED is the caller's job — it is the only
    part of this that can await, and doing it here would make the whole slice
    async for one wait.
    """
    folder = case.case_folder
    driver_remove(folder)
    case.case_detach()
    ticket = EjectTicket(
        case_id=case.case_id,
        export_to_folder=str(export_to_folder.resolve()),
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
    store: "LocalCaseStore",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
) -> EjectResult | None:
    case_id = ticket.case_id
    export_path = Path(ticket.export_to_folder)

    if not store.contains(case_id):
        # A previous attempt already exported it; the export is what matters, not
        # the ticket, so this is a completion rather than a failure.
        ticket_file.unlink(missing_ok=True)
        return EjectResult(
            case_id=case_id,
            export_folder=export_path,
            completed_at=datetime.now(timezone.utc),
        )

    ticket.state = EjectState.EXPORTING
    ticket.save(str(ticket_file), retain_lock=False)

    try:
        await store.export(case_id, export_path)
        ticket_file.unlink(missing_ok=True)
        return EjectResult(
            case_id=case_id,
            export_folder=export_path,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        ticket.retry_count += 1
        ticket.last_error = str(exc)
        logger.exception("Eject export failed for %s", case_id)
        if ticket.retry_count >= policy.eject_max_retries:
            failed = eject_dir(manager_dir) / "failed" / f"{case_id}.yaml"
            ticket.state = EjectState.FAILED
            ticket.save(str(failed), retain_lock=False)
            ticket_file.unlink(missing_ok=True)
            raise EjectAbandonedError(case_id=case_id, reason=str(exc)) from exc
        ticket.save(str(ticket_file), retain_lock=False)
        return None
