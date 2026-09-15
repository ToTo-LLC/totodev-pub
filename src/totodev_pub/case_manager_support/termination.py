# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Termination: a synchronous hand-off, then an archive move that can be replayed.

Split because the two halves have different failure modes. Removing a terminal
case from the pool must not fail, and must happen while the case object is still
in hand; relocating its folder is slow, can fail, and has to survive a crash."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.case_store import TERMINATED
from totodev_pub.case_manager_support.constants import TERMINATION_SUBDIR
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.constants import DEFAULT_LEASE_TTL_SECS
from totodev_pub.folder_backed_case_support.folder_backed_case_reader import FolderBackedCaseReader

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
    from totodev_pub.case_manager_support.case_store import LocalCaseStore

logger = logging.getLogger(__name__)

_LEASE_BLOCKED_REASON = "active lease present"


class TerminationState(str, Enum):
    """States a ticket can be *found* in. There is no terminal success state —
    a completed termination has no ticket, only a case at ``terminated``."""

    PENDING = "pending"
    MOVING = "moving"
    FAILED = "failed"


class TerminationPeekOutcome(str, Enum):
    """Result of verifying a case is ready to archive, without rehydrating it."""

    ARCHIVE = "archive"
    WAIT = "wait"
    ANOMALY = "anomaly"


class TerminationTicket(BaseModel, FileMappedPydanticMixin):
    case_id: str
    case_folder: str
    enqueued_at: str
    state: TerminationState = TerminationState.PENDING
    retry_count: int = 0
    last_error: str | None = None
    #: Wall-clock stamp of the first time peek saw a held lease. Caps the wait
    #: at one lease TTL; not derived from ``enqueued_at`` (a ticket can sit for
    #: hours before a leftover lease appears).
    lease_blocked_at: str | None = None


def termination_dir(manager_dir: Path) -> Path:
    return manager_dir / TERMINATION_SUBDIR


def ticket_path(manager_dir: Path, case_id: str, *, subdir: str = "pending") -> Path:
    return termination_dir(manager_dir) / subdir / f"{case_id}.yaml"


def ticket_exists(manager_dir: Path, case_id: str) -> bool:
    """True while a termination is in flight or has been given up on.

    There is no ``done/`` to consult: a completed termination is proved by the
    case's stored status, not by a receipt file. Keeping receipts as the
    idempotency guard meant one file per terminated case forever, with nothing
    to sweep them."""
    return any(
        ticket_path(manager_dir, case_id, subdir=sub).exists()
        for sub in ("pending", "failed")
    )


def verify_termination_peek(folder: Path) -> tuple[TerminationPeekOutcome, str | None]:
    """Confirm a case really is finished, without rehydrating it.

    Reading the record is cheap and takes no lease; rehydrating to ask the same
    question would acquire one, which is the opposite of what a case on its way
    out needs.

    Three outcomes:
    - ``ARCHIVE`` — terminal, stamped, no held lease; safe to move.
    - ``WAIT`` — a lease is still held; do not burn the retry budget.
    - ``ANOMALY`` — the record will never become archivale by waiting
      (not terminal, or ``terminated_at`` missing).
    """
    reader = FolderBackedCaseReader(folder)
    if not reader.case_is_terminal:
        return TerminationPeekOutcome.ANOMALY, "record is not terminal"
    if reader.case_terminal_at is None:
        return (
            TerminationPeekOutcome.ANOMALY,
            "terminated_at (terminal) missing from record",
        )
    if reader.case_lease_secs_left is not None and reader.case_lease_secs_left > 0:
        return TerminationPeekOutcome.WAIT, _LEASE_BLOCKED_REASON
    return TerminationPeekOutcome.ARCHIVE, None


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
    """Sync slice: remove → detach → enqueue. Idempotent by case_id.

    Always ensures the case is out of the pool and detached. Writes a ticket
    only when one is absent. Returns whether this call wrote the ticket.
    """
    case_id = case.case_id
    folder = case.case_folder
    try:
        driver_remove(folder)
    except KeyError:
        # Already gone from the pool — idempotent remove.
        pass
    case.case_detach()
    if ticket_exists(manager_dir, case_id):
        return False
    ticket = TerminationTicket(
        case_id=case_id,
        case_folder=str(folder),
        enqueued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    write_ticket(ticket, ticket_path(manager_dir, case_id))
    return True


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


async def _fail_and_quarantine(
    ticket: TerminationTicket,
    ticket_file: Path,
    *,
    case_id: str,
    folder: Path,
    reason: str,
    manager_dir: Path,
    quarantine: Callable[..., Awaitable[Path | None]],
    emit_notice: Callable[..., None] | None,
) -> None:
    ticket.state = TerminationState.FAILED
    ticket.last_error = reason
    write_ticket(ticket, termination_dir(manager_dir) / "failed" / f"{case_id}.yaml")
    ticket_file.unlink(missing_ok=True)
    await quarantine(case_id, folder, reason)
    if emit_notice:
        emit_notice("TERMINATION_VERIFICATION_FAILED", case_id, folder, reason)


async def process_pending_ticket(
    ticket: TerminationTicket,
    ticket_file: Path,
    *,
    store: "LocalCaseStore",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    quarantine: Callable[..., Awaitable[Path | None]],
    emit_notice: Callable[..., None] | None = None,
) -> None:
    """Advance one termination ticket: verify, then archive the case.

    Re-drivable by construction: a ticket that dies mid-move is replayed next
    tick, and the store's status change is idempotent, so the second attempt
    either finishes the move or finds it already done.

    A held lease is a wait, not a failure — it does not spend
    ``termination_max_retries``. After one lease TTL from first sight the wait
    is treated as a live-owner conflict and the case is quarantined. Anomalous
    records (not terminal / missing ``terminated_at``) quarantine on first
    observation. Past ``termination_max_retries``, a case whose folder move
    keeps failing is quarantined rather than retried forever.
    """
    case_id = ticket.case_id

    folder = Path(ticket.case_folder)
    if not folder.exists():
        # Addressing rule: a path recorded before a move is stale afterwards.
        resolved = await store.resolve_path(case_id)
        if resolved is not None:
            folder = resolved

    outcome, reason = verify_termination_peek(folder)

    if outcome is TerminationPeekOutcome.ANOMALY:
        await _fail_and_quarantine(
            ticket,
            ticket_file,
            case_id=case_id,
            folder=folder,
            reason=reason or "verification failed",
            manager_dir=manager_dir,
            quarantine=quarantine,
            emit_notice=emit_notice,
        )
        return

    if outcome is TerminationPeekOutcome.WAIT:
        ticket.last_error = reason or _LEASE_BLOCKED_REASON
        if ticket.lease_blocked_at is None:
            ticket.lease_blocked_at = _utc_now_stamp()
        blocked_for = (
            datetime.now(timezone.utc) - _parse_utc_stamp(ticket.lease_blocked_at)
        ).total_seconds()
        if blocked_for >= DEFAULT_LEASE_TTL_SECS:
            await _fail_and_quarantine(
                ticket,
                ticket_file,
                case_id=case_id,
                folder=folder,
                reason=reason or _LEASE_BLOCKED_REASON,
                manager_dir=manager_dir,
                quarantine=quarantine,
                emit_notice=emit_notice,
            )
        else:
            write_ticket(ticket, ticket_file)
        return

    ticket.state = TerminationState.MOVING
    write_ticket(ticket, ticket_file)
    try:
        archived = await store.set_status(case_id, TERMINATED)
        ticket_file.unlink(missing_ok=True)
        if emit_notice:
            emit_notice("CASE_TERMINATED", case_id, archived)
    except Exception as exc:
        ticket.retry_count += 1
        ticket.last_error = str(exc)
        logger.exception("Termination move failed for %s", case_id)
        if ticket.retry_count >= policy.termination_max_retries:
            await _fail_and_quarantine(
                ticket,
                ticket_file,
                case_id=case_id,
                folder=folder,
                reason=str(exc),
                manager_dir=manager_dir,
                quarantine=quarantine,
                emit_notice=emit_notice,
            )
        else:
            ticket.state = TerminationState.PENDING
            write_ticket(ticket, ticket_file)


def replay_pending(manager_dir: Path) -> list[Path]:
    pending = termination_dir(manager_dir) / "pending"
    if not pending.exists():
        return []
    return sorted(pending.glob("*.yaml"))
