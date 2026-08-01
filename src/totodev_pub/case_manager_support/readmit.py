# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Startup orphan recovery: put abandoned live cases back to work.

The question this answers is *"my live storage holds a case nobody is driving —
what happened?"*. Nothing is harvested or destroyed; orphans are **revived**.

It runs once, at recovery. Mid-run divergence — a relocation that died halfway —
is not its job: that is what ticket replay does, every tick. The two mechanisms
divide cleanly:

=====================================  ==================  ==========
what                                   mechanism           cadence
=====================================  ==================  ==========
a half-completed relocation            ticket replay       every tick
a crash left a case undriven           this module         at recover
=====================================  ==================  ==========

**Terminal-in-pool is deliberately not handled here.** A terminal case sitting in
the active pool is harmless — it no-ops on its next visit, and the manager's
per-tick reconciler archives it within one tick. So every orphan is simply
re-admitted, terminal or not, and the reconciler sorts out the terminal ones on
the next tick. That collapse is what leaves exactly one archive-label
computation in the codebase instead of two that could disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from totodev_pub.case_manager_support.case_store import LIVE
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import CaseTypeRegistry

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_store import LocalCaseStore
    from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolDriver

logger = logging.getLogger(__name__)


@dataclass
class OrphanReadmitReport:
    readmitted: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    #: Cases the pool holds that the store no longer calls live. Under an
    #: exclusively-owned filespace this should always be empty; it is a
    #: consistency assertion, not a routine repair.
    stale_pool_entries: list[str] = field(default_factory=list)


def readmit_orphans(
    *,
    store: "LocalCaseStore",
    driver: "CasePoolDriver",
    registry: CaseTypeRegistry,
    has_departure_ticket: Callable[[str], bool],
    emit_anomaly: Callable[[str, Path | None, str], None] | None = None,
) -> OrphanReadmitReport:
    """Re-admit every undriven live case, and report the reverse disagreement.

    One rule: *a live-status case that is not in the pool and whose lease has
    expired gets re-admitted; if it cannot be rehydrated, escalate.* The
    expired-lease gate is what keeps this from stealing a case another process is
    actively working, and the departure-ticket check keeps it from re-admitting a
    case already on its way out.

    Keying is by ``case_id`` throughout, never by path: a status change moves the
    folder, so a path read at the top of the loop can be stale by the time it is
    used.
    """
    report = OrphanReadmitReport()
    live = {entry.case_id: entry for entry in store.iter_by_status(LIVE)}
    pooled = {case.case_id for case in driver}

    for case_id in live:
        if case_id in pooled:
            continue
        # Re-verify rather than trusting the snapshot: the store may have moved
        # this case since the iterator produced its entry.
        current = store.find(case_id)
        if current is None or current.status != LIVE:
            continue
        # The same tri-state rule every relocation uses: only a *held* lease
        # (False) means hands off. True is expired and None is no lease at all,
        # which is exactly the state a cleanly released case is left in — and
        # treating that as "owned" would strand precisely the orphans this exists
        # to rescue.
        if FolderBackedCase.is_heartbeat_expired(current.case_folder) is False:
            continue        # another owner is actively working it
        if has_departure_ticket(case_id):
            continue        # already on its way out; re-admitting would undo that
        try:
            driver.add(registry.rehydrate(current.case_folder))
            report.readmitted.append(case_id)
        except Exception as exc:
            report.anomalies.append(case_id)
            logger.warning("Orphan re-admit failed for %s: %s", case_id, exc)
            if emit_anomaly:
                emit_anomaly(case_id, current.case_folder, str(exc))

    # The reverse direction: the pool holds a case the store no longer calls live,
    # so the pooled object is addressing a folder that has already moved.
    for case_id in sorted(pooled - live.keys()):
        report.stale_pool_entries.append(case_id)
        logger.warning(
            "Pool holds case %s which the store no longer reports as live", case_id
        )
        if emit_anomaly:
            emit_anomaly(case_id, None, "pooled case is not live in the store")

    return report
