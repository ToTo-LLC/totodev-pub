# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Aberrant holding for mechanical failures (§5.8)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.layout import (
    aberrant_grouping_key,
    live_grouping_key,
    ref_path_for_case,
    write_placeholder_file,
)

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy


class AberrantSidecar(BaseModel, FileMappedPydanticMixin):
    case_id: str
    reason: str
    observed_at: str
    source_path: str | None = None
    last_error: str | None = None


def aberrant_meta_dir(manager_dir: Path) -> Path:
    return manager_dir / "aberrant"


def move_case_to_aberrant(
    cache: "CachedFileFolders",
    policy: "CaseManagerPolicy",
    manager_dir: Path,
    case_id: str,
    case_folder: Path,
    reason: str,
    *,
    from_grouping: tuple[str, ...] | None = None,
) -> Path:
    """Move a partial or failed case entry to aberrant/ with sidecar metadata."""
    ref_path = ref_path_for_case(policy, case_id)
    src_grouping = from_grouping or live_grouping_key(policy)
    dst_grouping = aberrant_grouping_key(policy)

    ref = cache.find_file(ref_path, src_grouping)
    if ref is not None:
        cache.move_file(
            ref_path,
            ref_path,
            grouping_key=src_grouping,
            new_grouping_key=dst_grouping,
            overwrite=True,
        )
        dest_folder = cache.find_file(ref_path, dst_grouping).slave_dir_path
    else:
        # Ensure aberrant entry exists from folder on disk
        placeholder = cache.root_dir / dst_grouping[0] / ref_path
        write_placeholder_file(placeholder)
        dest_slave = cache.get_slave_dir(dst_grouping, ref_path)
        if case_folder.exists() and not dest_slave.exists():
            import shutil
            shutil.copytree(case_folder, dest_slave)
        dest_folder = dest_slave

    sidecar = AberrantSidecar(
        case_id=case_id,
        reason=reason,
        observed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_path=str(case_folder),
        last_error=reason,
    )
    meta_path = aberrant_meta_dir(manager_dir) / f"{case_id}.yaml"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar.save(str(meta_path), retain_lock=False)
    return dest_folder
