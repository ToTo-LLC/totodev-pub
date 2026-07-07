# Part of the totodev_pub library.

import pytest

from totodev_pub.case_manager import CaseManager
from case_manager_test_utils import TicketCase, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_adopt_drop_scan(tmp_path):
    manager = CaseManager.open_testing(tmp_path / "cache", register_types=[TicketCase])
    drop = manager._manager_dir / manager._policy.adopt_drop_subdir
    case_dir = drop / "fixture1"
    case_dir.mkdir(parents=True)
    seed_detached_case(TicketCase, case_dir)
    report = await manager.recover()
    assert report.adopt_drop_seen >= 1
