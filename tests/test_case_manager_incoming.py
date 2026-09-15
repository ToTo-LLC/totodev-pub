# Part of the totodev_pub library.

"""The one loading dock: allocation, the ``.ready`` gate, and the cleaner rule.

Replaces ``test_case_manager_staging.py`` and ``test_case_manager_adopt_drop.py``.
``staging/`` and ``adopt_drop/`` were merged into ``incoming/`` because two docks
meant two cleaner rules with no stated rule for choosing between them, and because
``adopt_drop`` was named after one of the actions that read it rather than after
what it holds.

The cleaner rule is the substance here. A folder is reclaimed only when all three
of "old", "unleased" and "no ``.ready`` marker" hold, and each case below pins one
row of that truth table — including the two rows the old arrangement got wrong,
where a slow or finished upload could be deleted out from under its writer.
"""

from __future__ import annotations

import pytest

from case_manager_test_utils import TicketCase, provision_manager, seed_detached_case
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.incoming import (
    READY_MARKER,
    incoming_root,
    is_ready,
    mark_ready,
    sweep_incoming,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


def test_allocate_incoming_folder(tmp_path):
    manager = provision_manager(tmp_path)
    path = manager.allocate_incoming_folder()
    assert path.is_dir()
    assert path.parent.name == "incoming"


def test_allocated_folders_are_distinct(tmp_path):
    """Two builders must never be handed the same folder."""
    manager = provision_manager(tmp_path)
    assert manager.allocate_incoming_folder() != manager.allocate_incoming_folder()


def test_mark_ready_writes_the_marker(tmp_path):
    manager = provision_manager(tmp_path)
    folder = manager.allocate_incoming_folder()

    assert not is_ready(folder)
    mark_ready(folder)
    assert is_ready(folder)
    assert (folder / READY_MARKER).exists()


# ---------------------------------------------------------------- cleaner rule


def _aged_folder(manager: CaseManager, name: str):
    folder = incoming_root(manager._manager_dir, manager._policy) / name
    folder.mkdir(parents=True)
    return folder


def test_cleaner_removes_an_abandoned_unmarked_folder(tmp_path):
    """Old, unleased, no marker — an upload that was abandoned half-way."""
    manager = provision_manager(tmp_path)
    folder = _aged_folder(manager, "abandoned")

    removed = sweep_incoming(
        incoming_root(manager._manager_dir, manager._policy),
        policy=manager._policy,
        clock=_far_future(manager),
    )

    assert removed == 1
    assert not folder.exists()


def test_cleaner_spares_a_ready_folder_however_old(tmp_path):
    """The row the old two-dock arrangement got wrong.

    A finished upload waiting to be admitted must never be reclaimed — the
    manager is about to consume it, and deleting it would lose a complete case
    that its submitter believes was accepted.
    """
    manager = provision_manager(tmp_path)
    folder = _aged_folder(manager, "finished")
    mark_ready(folder)

    removed = sweep_incoming(
        incoming_root(manager._manager_dir, manager._policy),
        policy=manager._policy,
        clock=_far_future(manager),
    )

    assert removed == 0
    assert folder.is_dir()


def test_cleaner_spares_a_fresh_folder(tmp_path):
    """An upload in progress has neither a lease nor a marker; age protects it."""
    manager = provision_manager(tmp_path)
    folder = _aged_folder(manager, "uploading")

    removed = sweep_incoming(
        incoming_root(manager._manager_dir, manager._policy), policy=manager._policy
    )

    assert removed == 0
    assert folder.is_dir()


def test_cleaner_spares_the_excluded_folder(tmp_path):
    """The folder being allocated right now is never swept by its own allocate."""
    manager = provision_manager(tmp_path)
    folder = _aged_folder(manager, "mine")

    removed = sweep_incoming(
        incoming_root(manager._manager_dir, manager._policy),
        policy=manager._policy,
        exclude=folder,
        clock=_far_future(manager),
    )

    assert removed == 0
    assert folder.is_dir()


def _far_future(manager: CaseManager) -> float:
    """A clock well past every reclaim threshold, so only the rules decide."""
    import time

    return time.time() + manager._policy.incoming_stale_lease_secs * 10


# ---------------------------------------------------------------- admission


@pytest.mark.asyncio
async def test_startup_scan_admits_a_ready_folder(tmp_path):
    """Replaces the adopt-drop scan. The marker is what makes it eligible."""
    store = CaseManager.open_local_store(
        tmp_path / "cache",
        startup_incoming_scan=True,
        redundant_purge_terminal_after_secs=None,
        redundant_purge_quarantined_after_secs=None,
    )
    case_type_registry.register_case_types(TicketCase)
    manager = CaseManager(store)
    case_dir = incoming_root(manager._manager_dir, manager._policy) / "fixture1"
    case_dir.mkdir(parents=True)
    seed_detached_case(TicketCase, case_dir)
    mark_ready(case_dir)

    report = await manager.recover()

    assert report.incoming_seen >= 1
    assert report.incoming_admitted >= 1


@pytest.mark.asyncio
async def test_a_folder_without_the_marker_is_not_admitted(tmp_path):
    """The gate that lets an out-of-process writer take no lease.

    Without it, a manager tick could adopt an upload that is still being written,
    which is exactly the race the marker exists to close.
    """
    store = CaseManager.open_local_store(
        tmp_path / "cache",
        startup_incoming_scan=True,
        redundant_purge_terminal_after_secs=None,
        redundant_purge_quarantined_after_secs=None,
    )
    case_type_registry.register_case_types(TicketCase)
    manager = CaseManager(store)
    case_dir = incoming_root(manager._manager_dir, manager._policy) / "still-uploading"
    case_dir.mkdir(parents=True)
    seed_detached_case(TicketCase, case_dir)  # complete on disk, but unmarked

    report = await manager.recover()

    assert report.incoming_seen == 0
    assert report.incoming_admitted == 0
    assert case_dir.is_dir(), "an unmarked folder is passed over, not consumed"
