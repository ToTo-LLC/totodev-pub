# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Coverage for case_manager_support/purge.py.

run_redundant_purge deletes files inside closed and quarantined case folders,
so the two things worth pinning are that it respects the retention window and
that a failure mid-sweep cannot go unnoticed.
"""

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.quarantine import quarantine_case
from totodev_pub.case_manager_support.purge import run_redundant_purge
from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME

_FAR_FUTURE = 4_000_000_000.0  # well past any folder ctime


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


async def _quarantined_case(manager, tmp_path, name):
    staging = tmp_path / name
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    folder = case.case_folder
    case.case_detach()
    return await quarantine_case(
        manager._store, manager._manager_dir, case.case_id, folder, "for purge"
    )


@pytest.mark.asyncio
async def test_purge_removes_scratch_but_keeps_the_record(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    folder = await _quarantined_case(manager, tmp_path, "a")
    scratch = folder / "scratch.bin"
    scratch.write_text("ephemeral", encoding="utf-8")

    report = run_redundant_purge(manager._store, manager._policy, clock=_FAR_FUTURE)

    assert folder in report.folders_purged
    assert not scratch.exists()
    assert (folder / RECORD_NAME).exists(), "the case record is retained"


@pytest.mark.asyncio
async def test_purge_respects_the_retention_window(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    folder = await _quarantined_case(manager, tmp_path, "b")
    scratch = folder / "scratch.bin"
    scratch.write_text("ephemeral", encoding="utf-8")

    # Real clock: the folder was created moments ago, far inside the window.
    report = run_redundant_purge(manager._store, manager._policy)

    assert report.folders_purged == []
    assert scratch.exists()


@pytest.mark.asyncio
async def test_purge_disabled_by_policy_does_nothing(tmp_path):
    manager = provision_manager(
        tmp_path,
        redundant_purge_terminal_after_secs=None,
        redundant_purge_aberrant_after_secs=None,
    )
    await manager.recover()
    folder = await _quarantined_case(manager, tmp_path, "c")
    scratch = folder / "scratch.bin"
    scratch.write_text("ephemeral", encoding="utf-8")

    report = run_redundant_purge(manager._store, manager._policy, clock=_FAR_FUTURE)

    assert report.folders_purged == []
    assert scratch.exists()


@pytest.mark.asyncio
async def test_a_failure_mid_purge_is_not_swallowed(tmp_path, monkeypatch):
    """run_redundant_purge has no internal error handling by design.

    It propagates, and _maintenance_tick's per-item isolation is what keeps one
    bad folder from taking down the tick. Pinning the propagation here means
    that contract cannot be quietly inverted.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    await _quarantined_case(manager, tmp_path, "d")

    def boom(self):
        raise OSError("disk went away")

    monkeypatch.setattr(CaseKeepManifest, "purge", boom)

    with pytest.raises(OSError, match="disk went away"):
        run_redundant_purge(manager._store, manager._policy, clock=_FAR_FUTURE)


@pytest.mark.asyncio
async def test_maintenance_tick_survives_a_purge_failure(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await _quarantined_case(manager, tmp_path, "e")

    def boom(self):
        raise OSError("disk went away")

    monkeypatch.setattr(CaseKeepManifest, "purge", boom)
    # The tick uses the real clock, so force the age gate open; otherwise purge
    # legitimately skips the folder and there is no failure to isolate.
    monkeypatch.setattr(
        "totodev_pub.case_manager_support.purge._folder_old_enough",
        lambda folder, now, min_age_secs: True,
    )
    escalations = []
    manager.on_escalation(escalations.append)

    await manager._maintenance_tick()  # must not raise

    assert manager._last_tick_completed is not None, "the tick ran to completion"
    kinds = [e.kind.value for e in escalations]
    assert "MAINTENANCE_ITEM_FAILED" in kinds
    assert any("redundant purge" in str(e.detail) for e in escalations)
