# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Case addressing and cache layout helpers for CaseManager."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.constants import PLACEHOLDER_HEADER
from totodev_pub.case_manager_support.exceptions import CaseLeaseHeldError
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


@dataclass(frozen=True)
class CaseLocation:
    case_id: str
    external_key: str | None
    case_folder: Path
    grouping_key: tuple[str, ...]
    in_pool: bool
    terminal: bool


def policy_manager_dir(cache_root: Path, policy: CaseManagerPolicy) -> Path:
    return cache_root / policy.manager_namespace


def ref_path_for_case(policy: CaseManagerPolicy, case_id: str) -> str:
    return policy.case_ref_path_template.format(case_id=case_id)


def live_grouping_key(policy: CaseManagerPolicy) -> tuple[str, ...]:
    return (policy.live_bucket,)


def aberrant_grouping_key(policy: CaseManagerPolicy) -> tuple[str, ...]:
    return (policy.aberrant_bucket,)


def terminal_grouping_key(policy: CaseManagerPolicy, label: str) -> tuple[str, ...]:
    return (f"{policy.terminal_prefix}_{label}",)


def managed_grouping_globs(policy: CaseManagerPolicy) -> list[str]:
    return [
        policy.live_bucket,
        policy.aberrant_bucket,
        f"{policy.terminal_prefix}_*",
    ]


def assert_case_folder_movable(case_folder: Path, case_id: str, operation: str) -> None:
    """Refuse to relocate a case folder while its heartbeat lease is held.

    Moving a folder out from under a process that believes it owns the case is a
    split-brain: the owner's open handles follow the inode while every new
    path-based open fails. The lease is the only thing that says "someone is
    working this", so every relocation checks it.

    ``is_heartbeat_expired`` is tri-state (``folder_backed_case.py``): ``True``
    expired, ``False`` held, ``None`` no lease file at all. Only ``False`` blocks
    a move — an absent lease means released or never claimed, which is exactly
    the state a detached case is left in.
    """
    if FolderBackedCase.is_heartbeat_expired(case_folder) is False:
        raise CaseLeaseHeldError(case_id=case_id, case_folder=case_folder, operation=operation)


def read_case_id_from_folder(folder: Path) -> str | None:
    record_path = folder / RECORD_NAME
    if not record_path.exists():
        return None
    try:
        text = record_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("case_id:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            return value or None
    return None


def slave_dir_for_ref(
    cache: CachedFileFolders,
    grouping_key: tuple[str, ...],
    ref_path: str,
) -> Path | None:
    ref = cache.find_file(ref_path, grouping_key)
    if ref is None:
        return None
    return ref.slave_dir_path


def iter_case_folders_in_grouping(
    cache: CachedFileFolders,
    grouping_key: tuple[str, ...],
) -> Iterator[tuple[str, Path, tuple[str, ...]]]:
    """Yield (case_id, slave_dir, grouping_key) for each cache entry in a grouping."""
    for ref in cache.files(grouping_key):
        slave = ref.slave_dir_path
        if not slave.exists():
            continue
        case_id = read_case_id_from_folder(slave)
        if case_id is None:
            case_id = Path(ref.ref_path).stem
        yield case_id, slave, grouping_key


def iter_all_managed_folders(
    cache: CachedFileFolders,
    policy: CaseManagerPolicy,
) -> Iterator[tuple[str, Path, tuple[str, ...]]]:
    for glob in managed_grouping_globs(policy):
        if glob.endswith("*"):
            prefix = glob[:-1]
            for grouping in cache.groupings(filters=[glob]):
                gk = grouping.grouping_key
                if gk and gk[0].startswith(prefix):
                    yield from iter_case_folders_in_grouping(cache, gk)
        else:
            yield from iter_case_folders_in_grouping(cache, (glob,))


def write_placeholder_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLACEHOLDER_HEADER, encoding="utf-8")


def normalize_case_folder_path(path: Path) -> Path:
    p = path.resolve()
    if p.name.endswith("._slave"):
        return p
    # If path points inside a case tree, walk up to find slave root or record
    for candidate in [p, *p.parents]:
        if (candidate / RECORD_NAME).exists():
            return candidate
    return p


def folder_matches_case_tree(path: Path, case_folder: Path) -> bool:
    try:
        path.resolve().relative_to(case_folder.resolve())
        return True
    except ValueError:
        return path.resolve() == case_folder.resolve()
