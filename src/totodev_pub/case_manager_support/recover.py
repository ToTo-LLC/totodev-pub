# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Startup recovery sequence (§5.6)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from totodev_pub.case_manager_support.case_store import LIVE
from totodev_pub.case_manager_support.shutdown import discard_stale_requests, shutdown_intake_dir
from totodev_pub.case_manager_support.watchdog import log_recent_death_records
from totodev_pub.folder_backed_case_support.pool_membership_journal import (
    PoolMembershipJournal,
    restore_pool_from_journal,
)

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)


@dataclass
class RecoverReport:
    pool_restored: int = 0
    termination_pending: int = 0
    eject_pending: int = 0
    adopt_drop_seen: int = 0
    adopt_drop_admitted: int = 0
    adopt_drop_rejected: int = 0
    adopt_drop_skipped: int = 0
    mailbox_fire_replayed: int = 0
    mailbox_adopt_replayed: int = 0
    mailbox_reclassify_replayed: int = 0
    dropped_paths: list[Path] = field(default_factory=list)
    death_records_recent: int = 0
    shutdown_requests_discarded: int = 0
    orphans_readmitted: int = 0
    orphan_anomalies: list[str] = field(default_factory=list)
    stale_pool_entries: list[str] = field(default_factory=list)


async def recover_manager(manager: "CaseManager") -> RecoverReport:
    report = RecoverReport()
    manager._ensure_namespace()
    manager._write_manifest()

    report.death_records_recent = log_recent_death_records(manager._manager_dir)
    report.shutdown_requests_discarded = discard_stale_requests(
        shutdown_intake_dir(manager._manager_dir, manager._policy)
    )

    # One scan, not two: the store's own status index is the working set, so the
    # manager does not run a second pass of its own over the same folders.
    live_paths = [entry.case_folder for entry in manager._store.iter_by_status(LIVE)]

    journal_path = manager._policy.journal_path
    if journal_path:
        journal = PoolMembershipJournal(journal_path)
    else:
        import tempfile
        journal = PoolMembershipJournal(
            manager._manager_dir / ".ephemeral_journal.jsonl"
        )
    journal.compact_from_live(live_paths)

    rebuild = await restore_pool_from_journal(
        manager._driver,
        journal,
        registry=manager._registry,
        attach=manager._policy.journal_attach_steady_state,
    )
    report.pool_restored = len(rebuild.readded)
    report.dropped_paths = list(rebuild.dropped)

    if manager._policy.enable_mailbox:
        report.mailbox_fire_replayed = manager._mailbox.replay_fire_on_recover()
        report.mailbox_adopt_replayed = manager._mailbox.replay_adopt_on_recover()
        report.mailbox_reclassify_replayed = manager._mailbox.replay_reclassify_on_recover()

    if manager._policy.startup_adopt_scan:
        drop_report = await manager._scan_adopt_drop()
        report.adopt_drop_seen = drop_report["seen"]
        report.adopt_drop_admitted = drop_report["admitted"]
        report.adopt_drop_rejected = drop_report["rejected"]
        report.adopt_drop_skipped = drop_report["skipped"]

    report.termination_pending = manager._count_termination_pending()
    report.eject_pending = manager._count_eject_pending()

    readmit_report = await manager.readmit_orphans()
    report.orphans_readmitted = len(readmit_report.readmitted)
    report.orphan_anomalies = list(readmit_report.anomalies)
    report.stale_pool_entries = list(readmit_report.stale_pool_entries)
    return report
