# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Aberrant holding for mechanical failures (§5.8)."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from totodev_pub.case_manager_support.constants import PLACEHOLDER_HEADER
from totodev_pub.case_manager_support.layout import (
    aberrant_grouping_key,
    live_grouping_key,
    ref_path_for_case,
)

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)


async def move_case_to_aberrant(
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    case_id: str,
    case_folder: Path,
    reason: str,
    *,
    from_grouping: tuple[str, ...] | None = None,
) -> Path:
    """Move a partial or failed case into the aberrant bucket. Returns its folder.

    Two paths. When the case still has a cache entry, the cache relocates it.
    When it does not — a case orphaned by a termination that already moved it out
    of the live bucket — the folder is copied in and *registered*, so the rescued
    case stays visible to ``iter_aberrant()`` and every other cache-driven query.
    Registration is what makes this coroutine rather than a plain function:
    ``upsert_file`` is the only entry-creating call the cache offers.
    """
    logger.warning("Moving case %s to aberrant: %s", case_id, reason)
    ref_path = ref_path_for_case(policy, case_id)
    src_grouping = from_grouping or live_grouping_key(policy)
    dst_grouping = aberrant_grouping_key(policy)

    if cache.find_file(ref_path, src_grouping) is not None:
        cache.move_file(
            ref_path,
            ref_path,
            grouping_key=src_grouping,
            new_grouping_key=dst_grouping,
            overwrite=True,
        )
        return cache.find_file(ref_path, dst_grouping).slave_dir_path

    dest_slave = await _register_aberrant_entry(cache, ref_path, dst_grouping)
    if case_folder.exists() and not case_folder.samefile(dest_slave):
        shutil.copytree(case_folder, dest_slave, dirs_exist_ok=True)
    return dest_slave


async def _register_aberrant_entry(
    cache: "CachedFileFolders",
    ref_path: str,
    dst_grouping: tuple[str, ...],
) -> Path:
    """Create the aberrant cache entry for ``ref_path`` and return its slave dir."""
    existing = cache.find_file(ref_path, dst_grouping)
    if existing is not None:
        return existing.slave_dir_path

    from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(PLACEHOLDER_HEADER)
        tmp_path = tmp.name
    try:
        await cache.upsert_file(
            LocalFileProxy(tmp_path, ref_path=ref_path, delete_after_deploy=True),
            dst_grouping,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return cache.get_slave_dir(dst_grouping, ref_path)
