# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Startup recovery: what the fleet finds on disk before it starts driving."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from totodev_pub.case_manager_support.case_store import LIVE
from totodev_pub.case_manager_support.exceptions import RecoveryIntegrityError
from totodev_pub.case_manager_support.shutdown import discard_stale_requests, shutdown_intake_dir
from totodev_pub.folder_backed_case_support.pool_membership_journal import (
    PoolMembershipJournal,
    restore_pool_from_journal,
)

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)


@dataclass
class RecoverReport:
    """What the *fleet* found at startup.

    Requests in flight when the process died are not here: settling those is the
    signaling adapter's recovery, which the host sequences alongside this one.
    Watchdog death records are not here either, for the same reason — only a host
    writes them, so reporting them is the host's recovery. Neither half knows how
    to do the other's."""

    pool_restored: int = 0
    termination_pending: int = 0
    eject_pending: int = 0
    adopt_drop_seen: int = 0
    adopt_drop_admitted: int = 0
    adopt_drop_rejected: int = 0
    adopt_drop_skipped: int = 0
    dropped_paths: list[Path] = field(default_factory=list)
    shutdown_requests_discarded: int = 0
    orphans_readmitted: int = 0
    orphan_anomalies: list[str] = field(default_factory=list)
    stale_pool_entries: list[str] = field(default_factory=list)


async def recover_manager(manager: "CaseManager") -> RecoverReport:
    report = RecoverReport()
    manager._ensure_namespace()
    # Announce the state before the slow part: reclaiming leases from a crashed
    # owner takes tens of seconds and emits no heartbeat.
    manager._write_manifest(recovering=True)

    # Ownership before anything else. Recovery rebuilds a pool and re-admits
    # orphans; doing that beside another live manager is the split-brain the whole
    # lease system exists to prevent, and it is far cheaper to refuse here than to
    # discover it later through contended cases.
    await manager._acquire_filespace()

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

    if manager._policy.startup_adopt_scan:
        drop_report = await manager._scan_adopt_drop()
        report.adopt_drop_seen = drop_report["seen"]
        report.adopt_drop_admitted = drop_report["admitted"]
        report.adopt_drop_rejected = drop_report["rejected"]
        report.adopt_drop_skipped = drop_report["skipped"]

    report.termination_pending = manager._count_termination_pending()
    report.eject_pending = manager._count_eject_pending()

    readmit_report = await manager._readmit_orphans()
    report.orphans_readmitted = len(readmit_report.readmitted)
    report.orphan_anomalies = list(readmit_report.anomalies)
    report.stale_pool_entries = list(readmit_report.stale_pool_entries)

    _log_report(report)
    if manager._policy.strict_recovery and (
        report.orphan_anomalies or report.stale_pool_entries
    ):
        raise RecoveryIntegrityError(
            anomalies=report.orphan_anomalies,
            stale_pool_entries=report.stale_pool_entries,
        )
    return report


def _log_report(report: RecoverReport) -> None:
    """Say what recovery did, every time, whether or not anything went wrong.

    A crashed-and-revived fleet that says nothing is indistinguishable from one
    that had nothing to do, so the summary is unconditional. The two problem
    lists are separate and louder because each is a standing defect: an anomaly
    is work nobody will ever pick up, and a stale entry means a second writer.
    """
    logger.info(
        "Recovery complete: pool_restored=%d orphans_readmitted=%d "
        "termination_pending=%d eject_pending=%d adopt_drop=%d/%d/%d/%d "
        "shutdown_requests_discarded=%d",
        report.pool_restored,
        report.orphans_readmitted,
        report.termination_pending,
        report.eject_pending,
        report.adopt_drop_seen,
        report.adopt_drop_admitted,
        report.adopt_drop_rejected,
        report.adopt_drop_skipped,
        report.shutdown_requests_discarded,
    )
    if report.dropped_paths:
        logger.warning(
            "Recovery could not restore %d case(s) the journal listed: %s",
            len(report.dropped_paths),
            ", ".join(str(p) for p in report.dropped_paths),
        )
    if report.orphan_anomalies:
        logger.error(
            "Recovery left %d live case(s) undriven — they could not be rehydrated and "
            "will fail the same way on every restart until resolved (a build whose case "
            "classes no longer match what is on disk is the usual cause): %s",
            len(report.orphan_anomalies),
            ", ".join(report.orphan_anomalies),
        )
    if report.stale_pool_entries:
        logger.error(
            "Recovery evicted %d pooled case(s) the store no longer calls live: %s",
            len(report.stale_pool_entries),
            ", ".join(report.stale_pool_entries),
        )
