# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The one loading dock for cases on their way in, and the GC that reclaims it.

This replaced two docks doing nearly the same job. ``staging/`` was for code
assembling a case inside the manager's own process and held a lease while
building; ``adopt_drop/`` was for a folder handed over by another process and held
none. Both were accepted as adopt sources, and nothing in the code or the docs
said which to use when — so the choice was a coin flip with two different cleaner
rules behind it.

``adopt_drop`` was also named after one of the *actions that read it* rather than
after what it holds, which misleads the moment anything else reads it too.

One dock needs one rule, and it has to be correct for both kinds of writer. A
folder is reclaimed only when all three hold:

1. it is old, **and**
2. no process holds a live lease on it, **and**
3. it carries no ``.ready`` marker

The marker is what makes this safe for an out-of-process writer that takes no
lease: an upload in progress has neither a lease nor a marker, so rule 1 protects
it while it is fresh, and a *finished* upload is protected by rule 3 indefinitely
because the manager is about to consume it. Under the old two-dock arrangement a
slow upload — or a case waiting on a human — could have its folder deleted out
from under it.
"""

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

#: Written last by whoever assembles a folder, and the only thing that marks it
#: complete. The manager ignores any folder without one, so a half-written upload
#: is never admitted, and the cleaner spares any folder with one, so a complete
#: upload is never reclaimed out from under a manager about to take it.
READY_MARKER = ".ready"


def incoming_root(manager_dir: Path, policy: "CaseManagerPolicy") -> Path:
    return manager_dir / policy.incoming_subdir


def is_ready(folder: Path) -> bool:
    """Whether a folder is complete and waiting to be admitted."""
    return (folder / READY_MARKER).exists()


def mark_ready(folder: Path) -> Path:
    """Write the completion marker. Call last, after every asset is in place."""
    marker = folder / READY_MARKER
    marker.touch()
    return marker


def sweep_incoming(
    incoming: Path,
    *,
    policy: "CaseManagerPolicy",
    exclude: Path | None = None,
    clock: float | None = None,
) -> int:
    """Reclaim abandoned folders under ``incoming/``. Returns the count removed.

    Age is taken from directory ``ctime``. The three-condition rule in the module
    docstring, in order of what it protects:

    - a ``.ready`` folder is **never** reclaimed — the manager owns it now
    - a live lease means an in-process builder is working; reclaimed only after
      ``incoming_stale_lease_secs``, and logged, because that is a wedged builder
    - otherwise, reclaimed once older than ``incoming_min_age_secs``

    Lazy rather than scheduled: runs from ``allocate_incoming_folder``, so an idle
    manager never sweeps on its own.
    """
    now = clock if clock is not None else time.time()
    removed = 0
    if not incoming.exists():
        return 0
    for child in list(incoming.iterdir()):
        if not child.is_dir():
            continue
        if exclude is not None and child.resolve() == exclude.resolve():
            continue
        if is_ready(child):
            continue  # complete and awaiting admission; not ours to delete
        try:
            age = now - child.stat().st_ctime
        except FileNotFoundError:
            continue
        if _reclaim(child, age=age, policy=policy):
            removed += 1
    return removed


def _reclaim(child: Path, *, age: float, policy: "CaseManagerPolicy") -> bool:
    expired_lease = FolderBackedCase.is_heartbeat_expired(child)
    if not expired_lease:
        if age < policy.incoming_stale_lease_secs:
            return False
        logger.warning("Removing incoming folder with stale active lease: %s", child)
        shutil.rmtree(child, ignore_errors=True)
        return True
    if age < policy.incoming_min_age_secs:
        return False
    try:
        shutil.rmtree(child)
        return True
    except FileNotFoundError:
        return False


def allocate_incoming_folder(manager_dir: Path, policy: "CaseManagerPolicy") -> Path:
    """Make an empty folder under ``incoming/`` for a case being assembled."""
    root = incoming_root(manager_dir, policy)
    root.mkdir(parents=True, exist_ok=True)
    sweep_incoming(root, policy=policy)
    folder = root / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    return folder
