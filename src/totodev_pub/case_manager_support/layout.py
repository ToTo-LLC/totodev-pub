# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Addressing helpers shared across the CaseManager package.

Bucket names, ref paths, and slave directories are **not** here — they are
storage mechanics and live behind ``case_store``. What remains is the small set
of facts about a case folder that several modules need and none of them owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.exceptions import CaseLeaseHeldError
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


@dataclass(frozen=True)
class CaseLocation:
    """Where a case was, and what the pool thought of it, **when this was read**.

    Both mutable fields are point-in-time. ``case_folder`` is invalidated by any
    status change, because status changes move the folder; ``in_pool`` is a
    snapshot of pool membership, which the very next tick may alter. Re-resolve
    before acting rather than holding either across an await.
    """

    case_id: str
    external_key: str | None
    case_folder: Path
    status: str
    in_pool: bool
    terminal: bool


def policy_manager_dir(cache_root: Path, policy: CaseManagerPolicy) -> Path:
    return cache_root / policy.manager_namespace


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
