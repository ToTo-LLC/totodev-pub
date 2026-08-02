# Part of the totodev_pub library.

import pytest

from totodev_pub.case_manager import CaseManager
from case_manager_test_utils import TicketCase, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_adopt_drop_scan(tmp_path):
    store = CaseManager.open_local_store(
        tmp_path / "cache",
        # Stage cases into the drop folder before recovery scans it, and keep the
        # redundant purge from reaping this test's fixtures.
        startup_adopt_scan=True,
        redundant_purge_terminal_after_secs=None,
        redundant_purge_aberrant_after_secs=None,
    )
    manager = CaseManager(store, register_types=[TicketCase])
    drop = manager._manager_dir / manager._policy.adopt_drop_subdir
    case_dir = drop / "fixture1"
    case_dir.mkdir(parents=True)
    seed_detached_case(TicketCase, case_dir)
    report = await manager.recover()
    assert report.adopt_drop_seen >= 1
