# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Quarantine: stop driving a case now, relocate it when its lease lapses.

Quarantine is an exception-handling strategy, and it decomposes into three
things that happen at different times:

===========================  ==========================================
Stop driving this case       immediately
Record why                   immediately, as an event on the case itself
Relocate it                  only once the heartbeat lease has expired
===========================  ==========================================

The last one is the whole point. Consider a case that keeps its lease alive yet
raises on every attempted interaction: every option is ugly, and the least-bad is
to stop touching it and let the lease clock run out, after which the folder can
be moved while still honoring the lease protocol. Relocating immediately would
move a folder out from under a process that believes it owns the case — the
owner's open handles follow the inode while every new path-based open fails.

So the move is attempted immediately and, when the lease refuses it, deferred
behind a durable ticket that the manager re-drives every tick until it takes.
The ticket exists because the manager's *intent* has to outlive a crash: without
it, a restart re-admits a live-status case that quarantine had already given up
on, which for the pathological case puts the thing that raises on every
interaction straight back into the pool.

``quarantined`` is the frozen status; the reason string explains why.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.case_store import QUARANTINED
from totodev_pub.case_manager_support.constants import QUARANTINE_SUBDIR
from totodev_pub.case_manager_support.exceptions import (
    CaseLeaseHeldError,
    CaseNotInStoreError,
)
from totodev_pub.folder_backed_case_support.case_journal import CaseEventJournal

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_store import LocalCaseStore

logger = logging.getLogger(__name__)

# A manager-authored event on the case's own journal. Deliberately outside the
# CASE_ prefix, which is reserved to the case family's own lifecycle writes.
EV_QUARANTINED = "MANAGER_QUARANTINED"


class QuarantineTicket(BaseModel, FileMappedPydanticMixin):
    case_id: str
    case_folder: str
    reason: str
    enqueued_at: str
    retry_count: int = 0
    last_error: str | None = None


def quarantine_dir(manager_dir: Path) -> Path:
    return manager_dir / QUARANTINE_SUBDIR


def quarantine_ticket_path(manager_dir: Path, case_id: str, *, subdir: str = "pending") -> Path:
    return quarantine_dir(manager_dir) / subdir / f"{case_id}.yaml"


def quarantine_ticket_exists(manager_dir: Path, case_id: str) -> bool:
    return any(
        quarantine_ticket_path(manager_dir, case_id, subdir=sub).exists()
        for sub in ("pending", "failed")
    )


def pending_quarantine_tickets(manager_dir: Path) -> list[Path]:
    pending = quarantine_dir(manager_dir) / "pending"
    return sorted(pending.glob("*.yaml")) if pending.exists() else []


def record_quarantine_reason(case_folder: Path, case_id: str, reason: str) -> None:
    """Write the reason onto the case's own event journal.

    The journal travels with the folder and survives every relocation, which a
    manager-side sidecar does not — and it keeps the store ignorant of case
    internals, since nothing about *why* ever reaches the storage boundary.
    Best-effort: a case whose folder is already unreadable is exactly the kind
    being quarantined, and failing to annotate it must not block the quarantine.
    """
    try:
        CaseEventJournal.for_folder(case_folder).primitive.create_event(
            EV_QUARANTINED, reason, {"case_id": case_id}
        )
    except Exception:
        logger.warning(
            "Could not record the quarantine reason on case %s at %s",
            case_id,
            case_folder,
            exc_info=True,
        )


async def quarantine_case(
    store: "LocalCaseStore",
    manager_dir: Path,
    case_id: str,
    case_folder: Path,
    reason: str,
) -> Path | None:
    """Quarantine a case. Returns its new folder, or None if the move was deferred.

    Deferral is the normal outcome when the case's owner has not yet released the
    lease; the ticket left behind is re-driven every maintenance tick.
    """
    logger.warning("Quarantining case %s: %s", case_id, reason)
    record_quarantine_reason(case_folder, case_id, reason)
    try:
        return await _relocate(store, case_id, case_folder)
    except CaseLeaseHeldError:
        _write_ticket(
            QuarantineTicket(
                case_id=case_id,
                case_folder=str(case_folder),
                reason=reason,
                enqueued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
            quarantine_ticket_path(manager_dir, case_id),
        )
        logger.info("Quarantine of %s deferred until its heartbeat lease lapses", case_id)
        return None


async def process_quarantine_ticket(
    ticket: QuarantineTicket,
    ticket_file: Path,
    *,
    store: "LocalCaseStore",
    manager_dir: Path,
    max_retries: int,
) -> Path | None:
    """Re-drive one deferred quarantine. Returns the new folder once it lands.

    A still-held lease is not a failure and does not spend a retry — waiting is
    the designed behavior, and a lease can legitimately outlive many ticks. Only
    a genuine error counts, and enough of those retire the ticket to ``failed/``
    so a case that can never be relocated stops consuming a tick forever.
    """
    try:
        folder = await _relocate(store, ticket.case_id, Path(ticket.case_folder))
    except CaseLeaseHeldError:
        return None
    except Exception as exc:
        ticket.retry_count += 1
        ticket.last_error = str(exc)
        logger.exception("Quarantine relocation failed for %s", ticket.case_id)
        if ticket.retry_count >= max_retries:
            _write_ticket(
                ticket, quarantine_ticket_path(manager_dir, ticket.case_id, subdir="failed")
            )
            ticket_file.unlink(missing_ok=True)
        else:
            _write_ticket(ticket, ticket_file)
        return None
    ticket_file.unlink(missing_ok=True)
    return folder


async def _relocate(store: "LocalCaseStore", case_id: str, case_folder: Path) -> Path:
    """Put the case at ``quarantined`` status, wherever it is coming from.

    A case the store has never heard of still gets rescued: that is the residue
    of a relocation that died between moving the folder and recording the move,
    and leaving it unindexed would make it invisible to every query the manager
    has.
    """
    try:
        return await store.set_status(case_id, QUARANTINED)
    except CaseNotInStoreError:
        return await store.absorb_orphan(case_id, case_folder, status=QUARANTINED)


def _write_ticket(ticket: QuarantineTicket, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ticket.save(str(path), retain_lock=False)
