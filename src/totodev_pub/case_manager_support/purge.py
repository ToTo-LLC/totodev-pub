# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Redundant ephemeral purge on terminal_* and aberrant folders (§5.9)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from totodev_pub.case_manager_support.case_store import QUARANTINED, TERMINATED
from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
    from totodev_pub.case_manager_support.case_store import LocalCaseStore

logger = logging.getLogger(__name__)


@dataclass
class PurgeReport:
    folders_purged: list[Path] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)


def run_redundant_purge(
    store: "LocalCaseStore",
    policy: "CaseManagerPolicy",
    *,
    clock: float | None = None,
) -> PurgeReport:
    """Redundant ephemeral purge over cases the pool has finished with.

    A case's own keep manifest is authoritative about what survives; this is the
    manager's backstop for cases whose owning process never got to run it.
    """
    now = clock if clock is not None else time.time()
    report = PurgeReport()

    windows = (
        (TERMINATED, policy.redundant_purge_terminal_after_secs),
        (QUARANTINED, policy.redundant_purge_aberrant_after_secs),
    )
    for status, min_age_secs in windows:
        if min_age_secs is None:
            continue
        for entry in store.iter_by_status(status):
            if not _folder_old_enough(entry.case_folder, now, min_age_secs):
                continue
            removed = CaseKeepManifest(entry.case_folder).purge()
            if removed:
                report.folders_purged.append(entry.case_folder)
                report.files_removed.extend(removed)

    return report


def _folder_old_enough(folder: Path, now: float, min_age_secs: int) -> bool:
    try:
        return (now - folder.stat().st_ctime) >= min_age_secs
    except OSError:
        return False
