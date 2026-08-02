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
    #: Cases the pool held that the store no longer calls live, now evicted. Under
    #: an exclusively-owned filespace this should always be empty: a non-empty list
    #: means something wrote to the filespace besides this manager.
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
    pooled = {case.case_id: case for case in driver}

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
        case = None
        try:
            case = registry.rehydrate(current.case_folder)   # takes the lease
            driver.add(case)
            report.readmitted.append(case_id)
        except Exception as exc:
            # Release the lease rehydrate took. Leaving it held would make the
            # orphan look *owned* to every later pass — including the next
            # restart's — so a case that failed to re-admit once would never be
            # looked at again.
            if case is not None and not case.case_is_detached:
                try:
                    case.case_detach()
                except Exception:
                    logger.exception("Could not release the lease on orphan %s", case_id)
            report.anomalies.append(case_id)
            logger.warning("Orphan re-admit failed for %s: %s", case_id, exc)
            if emit_anomaly:
                emit_anomaly(case_id, current.case_folder, str(exc))

    # The reverse direction: the pool holds a case the store no longer calls live,
    # so the pooled object is addressing a folder that has already moved. Driving it
    # can only fail, and failing later at tick time would report the symptom far from
    # the cause — so it is evicted here, loudly, with its real status named.
    for case_id in sorted(set(pooled) - live.keys()):
        case = pooled[case_id]
        current = store.find(case_id)
        whereabouts = (
            f"store reports {current.status}"
            if current is not None
            else "no longer present in storage at all"
        )
        evicted = _evict(driver, case, case_id)
        report.stale_pool_entries.append(case_id)
        logger.error(
            "Evicting pooled case %s: the store no longer calls it live (%s). Under a "
            "singly-owned filespace this cannot happen — something else wrote here.%s",
            case_id,
            whereabouts,
            "" if evicted else " Eviction itself failed; the pool entry remains.",
        )
        if emit_anomaly:
            emit_anomaly(case_id, case.case_folder, f"pooled case is not live: {whereabouts}")

    return report


def _evict(driver: "CasePoolDriver", case: FolderBackedCase, case_id: str) -> bool:
    """Drop a case from the pool and release the lease the pool was holding.

    ``driver.remove`` deliberately leaves the lease alone, so detaching is not
    optional here: a lease left held on a folder that has moved (or gone) makes the
    case look owned to every later pass, including the next restart's.
    """
    try:
        driver.remove(case.case_folder)
    except Exception:
        logger.exception("Could not evict stale pool entry %s", case_id)
        return False
    try:
        if not case.case_is_detached:
            case.case_detach()
    except Exception:
        logger.exception("Evicted %s but could not release its lease", case_id)
    return True
