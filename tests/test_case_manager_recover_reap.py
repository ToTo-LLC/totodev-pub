# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_recover_restores_pool(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    case = await adopt_into_live(manager, staging / "c1")
    case_id = case.case_id
    await manager.stop() if manager._running else None
    manager2 = provision_manager(tmp_path)
    report = await manager2.recover()
    assert report.pool_restored >= 0
    assert manager2.locate(case_id=case_id) is not None
