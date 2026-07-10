# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case
from totodev_pub.pytest_tools import very_lazy_test


@pytest.mark.asyncio
@pytest.mark.slow
@very_lazy_test(
    [
        "case_manager_test_utils.py",
        "totodev_pub.case_manager",
        "totodev_pub.case_manager_support.recover",
        "totodev_pub.folder_backed_case_support.pool_membership_journal",
    ],
    reverify_days=21,
)
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
