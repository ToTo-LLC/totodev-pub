# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Termination queue: sync slice + async archive worker (§5.4)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.layout import (
    live_grouping_key,
    read_case_id_from_folder,
    ref_path_for_case,
    terminal_grouping_key,
    write_placeholder_file,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)


class TerminationState(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    MOVING = "moving"
    COMPLETED = "completed"
    FAILED = "failed"


class TerminationTicket(BaseModel, FileMappedPydanticMixin):
    case_id: str
    case_folder: str
    destination_grouping_key: str
    enqueued_at: str
    state: TerminationState = TerminationState.PENDING
    retry_count: int = 0
    last_error: str | None = None


def termination_dir(manager_dir: Path) -> Path:
    return manager_dir / "termination"


def ticket_path(manager_dir: Path, case_id: str, *, subdir: str = "pending") -> Path:
    return termination_dir(manager_dir) / subdir / f"{case_id}.yaml"


def ticket_exists(manager_dir: Path, case_id: str) -> bool:
    for sub in ("pending", "failed", "done"):
        if ticket_path(manager_dir, case_id, subdir=sub).exists():
            return True
    return False


def destination_key_for_case(case: FolderBackedCase, policy: "CaseManagerPolicy") -> str:
    label = case.archive_grouping_label()
    return f"{policy.terminal_prefix}_{label}"


def verify_termination_peek(folder: Path) -> tuple[bool, str | None]:
    """Manager-owned peek checks (§5.12) without rehydrate."""
    reader = FolderBackedCaseReader(folder)
    if not reader.case_is_terminal:
        return False, "record is not terminal"
    if reader.case_terminal_at is None:
        return False, "terminated_at (terminal) missing from record"
    if reader.case_lease_secs_left is not None and reader.case_lease_secs_left > 0:
        return False, "active lease present"
    return True, None


def destination_key_from_record(folder: Path, policy: "CaseManagerPolicy") -> str | None:
    reader = FolderBackedCaseReader(folder)
    if not reader.case_is_terminal:
        return None
    terminal = reader.case_terminal_at
    if terminal is None:
        return None
    label = terminal.strftime("%Y-%m")
    return f"{policy.terminal_prefix}_{label}"


def write_ticket(ticket: TerminationTicket, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ticket.save(str(path), retain_lock=False)


def begin_termination(
    case: FolderBackedCase,
    *,
    manager_dir: Path,
    policy: "CaseManagerPolicy",
    driver_remove: Callable[[Path], FolderBackedCase],
) -> bool:
    """Sync slice: remove → detach → enqueue. Idempotent by case_id."""
    case_id = case.case_id
    if ticket_exists(manager_dir, case_id):
        return False
    dest_key = destination_key_for_case(case, policy)
    folder = case.case_folder
    driver_remove(folder)
    case.case_detach()
    ticket = TerminationTicket(
        case_id=case_id,
        case_folder=str(folder),
        destination_grouping_key=dest_key,
        enqueued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    write_ticket(ticket, ticket_path(manager_dir, case_id))
    return True


def enqueue_termination_from_disk(
    case_folder: Path,
    *,
    manager_dir: Path,
    policy: "CaseManagerPolicy",
) -> bool:
    case_id = read_case_id_from_folder(case_folder)
    if case_id is None or ticket_exists(manager_dir, case_id):
        return False
    dest_key = destination_key_from_record(case_folder, policy)
    if dest_key is None:
        return False
    ticket = TerminationTicket(
        case_id=case_id,
        case_folder=str(case_folder),
        destination_grouping_key=dest_key,
        enqueued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    write_ticket(ticket, ticket_path(manager_dir, case_id))
    return True


def process_pending_ticket(
    ticket: TerminationTicket,
    ticket_file: Path,
    *,
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    move_to_aberrant: Callable[[str, Path, str], None],
    emit_escalation: Callable[..., None] | None = None,
) -> None:
    case_id = ticket.case_id
    ref_path = ref_path_for_case(policy, case_id)
    src_grouping = live_grouping_key(policy)
    dst_grouping = (ticket.destination_grouping_key,)

    folder = Path(ticket.case_folder)
    if not folder.exists():
        ref = cache.find_file(ref_path, src_grouping)
        if ref is not None:
            folder = ref.slave_dir_path

    ok, reason = verify_termination_peek(folder)
    if not ok:
        ticket.retry_count += 1
        ticket.last_error = reason
        if ticket.retry_count >= policy.termination_max_retries:
            ticket.state = TerminationState.FAILED
            write_ticket(ticket, termination_dir(manager_dir) / "failed" / f"{case_id}.yaml")
            ticket_file.unlink(missing_ok=True)
            move_to_aberrant(case_id, folder, reason or "verification failed")
            if emit_escalation:
                emit_escalation("TERMINATION_VERIFICATION_FAILED", case_id, folder, reason)
        else:
            write_ticket(ticket, ticket_file)
        return

    ticket.state = TerminationState.MOVING
    write_ticket(ticket, ticket_file)
    try:
        cache.move_file(
            ref_path,
            ref_path,
            grouping_key=src_grouping,
            new_grouping_key=dst_grouping,
        )
        ticket.state = TerminationState.COMPLETED
        done_path = termination_dir(manager_dir) / "done" / f"{case_id}.yaml"
        write_ticket(ticket, done_path)
        ticket_file.unlink(missing_ok=True)
    except Exception as exc:
        ticket.retry_count += 1
        ticket.last_error = str(exc)
        logger.exception("Termination move failed for %s", case_id)
        if ticket.retry_count >= policy.termination_max_retries:
            ticket.state = TerminationState.FAILED
            write_ticket(ticket, termination_dir(manager_dir) / "failed" / f"{case_id}.yaml")
            ticket_file.unlink(missing_ok=True)
            move_to_aberrant(case_id, folder, str(exc))
            if emit_escalation:
                emit_escalation("TERMINATION_VERIFICATION_FAILED", case_id, folder, str(exc))
        else:
            ticket.state = TerminationState.PENDING
            write_ticket(ticket, ticket_file)


def replay_pending(manager_dir: Path) -> list[Path]:
    pending = termination_dir(manager_dir) / "pending"
    if not pending.exists():
        return []
    return sorted(pending.glob("*.yaml"))
