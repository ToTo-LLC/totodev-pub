# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Scratch space for cases on their way in, and the lazy GC that reclaims it."""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from totodev_pub.folder_backed_case import FolderBackedCase

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)


def staging_root(manager_dir: Path, policy: "CaseManagerPolicy") -> Path:
    return manager_dir / policy.staging_subdir


def sweep_staging(
    staging: Path,
    *,
    policy: "CaseManagerPolicy",
    exclude: Path | None = None,
    clock: float | None = None,
) -> int:
    """Reclaim abandoned staging folders. Returns the count removed.

    Rules (age from directory ``ctime``):

    - heartbeat absent/expired and age >= ``staging_min_age_secs`` → delete
    - heartbeat still active and age >= ``staging_stale_lease_secs`` → delete
      (builder presumed wedged; logged)
    - otherwise leave alone (in-flight assemble)

    Lazy rather than scheduled: only runs from ``allocate_staging_folder``, so
    an idle manager never sweeps on its own.
    """
    now = clock if clock is not None else time.time()
    removed = 0
    if not staging.exists():
        return 0
    for child in list(staging.iterdir()):
        if not child.is_dir():
            continue
        if exclude is not None and child.resolve() == exclude.resolve():
            continue
        try:
            age = now - child.stat().st_ctime
        except FileNotFoundError:
            continue
        expired_lease = FolderBackedCase.is_heartbeat_expired(child)
        if not expired_lease:
            if age < policy.staging_stale_lease_secs:
                continue
            logger.warning("Removing staging folder with stale active lease: %s", child)
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            continue
        if age < policy.staging_min_age_secs:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def allocate_staging_folder(
    manager_dir: Path,
    policy: "CaseManagerPolicy",
) -> Path:
    root = staging_root(manager_dir, policy)
    root.mkdir(parents=True, exist_ok=True)
    sweep_staging(root, policy=policy)
    folder = root / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    return folder
