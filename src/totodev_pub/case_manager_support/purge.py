# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Redundant ephemeral purge on terminal_* and aberrant folders (§5.9)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from totodev_pub.case_manager_support.layout import iter_case_folders_in_grouping
from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)


@dataclass
class PurgeReport:
    folders_purged: list[Path] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)


def run_redundant_purge(
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    *,
    clock: float | None = None,
) -> PurgeReport:
    now = clock if clock is not None else time.time()
    report = PurgeReport()

    terminal_after = policy.redundant_purge_terminal_after_secs
    aberrant_after = policy.redundant_purge_aberrant_after_secs

    # Terminal groupings
    if terminal_after is not None:
        prefix = f"{policy.terminal_prefix}_"
        for grouping in cache.groupings(filters=[f"{prefix}*"]):
            gk = grouping.grouping_key
            if gk is None:
                continue
            for _cid, folder, _gk in iter_case_folders_in_grouping(cache, gk):
                if _folder_old_enough(folder, now, terminal_after):
                    removed = CaseKeepManifest(folder).purge()
                    if removed:
                        report.folders_purged.append(folder)
                        report.files_removed.extend(removed)

    # Aberrant
    if aberrant_after is not None:
        gk = (policy.aberrant_bucket,)
        for _cid, folder, _gk in iter_case_folders_in_grouping(cache, gk):
            if _folder_old_enough(folder, now, aberrant_after):
                removed = CaseKeepManifest(folder).purge()
                if removed:
                    report.folders_purged.append(folder)
                    report.files_removed.extend(removed)

    return report


def _folder_old_enough(folder: Path, now: float, min_age_secs: int) -> bool:
    try:
        return (now - folder.stat().st_ctime) >= min_age_secs
    except OSError:
        return False
