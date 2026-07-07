# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Mechanical fixups: terminal-in-live, orphan re-admit (§5.7)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from totodev_pub.case_manager_support.layout import (
    iter_case_folders_in_grouping,
    live_grouping_key,
    read_case_id_from_folder,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.case_type_registry import CaseTypeRegistry

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
    from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolDriver

logger = logging.getLogger(__name__)


@dataclass
class ReapReport:
    termination_enqueued: list[str] = field(default_factory=list)
    readmitted: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def reap(
    *,
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    driver: "CasePoolDriver",
    registry: CaseTypeRegistry,
    enqueue_from_disk: Callable[[Path], bool],
    has_eject_ticket: Callable[[str], bool],
    emit_anomaly: Callable[[str, Path, str], None] | None = None,
) -> ReapReport:
    report = ReapReport()
    live_gk = live_grouping_key(policy)
    pool_folders = {c.case_folder.resolve() for c in driver}

    for case_id, folder, _gk in iter_case_folders_in_grouping(cache, live_gk):
        folder = folder.resolve()
        in_pool = folder in pool_folders

        reader = FolderBackedCaseReader(folder)
        if reader.case_is_terminal and not in_pool:
            if enqueue_from_disk(folder):
                report.termination_enqueued.append(case_id)
            continue

        if not in_pool and FolderBackedCase.is_heartbeat_expired(folder):
            if has_eject_ticket(case_id):
                continue
            try:
                case = registry.rehydrate(folder)
                driver.add(case)
                report.readmitted.append(case_id)
            except Exception as exc:
                report.anomalies.append(case_id)
                logger.warning("Reap re-admit failed for %s: %s", case_id, exc)
                if emit_anomaly:
                    emit_anomaly(case_id, folder, str(exc))

    return report
