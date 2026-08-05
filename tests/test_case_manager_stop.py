# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import adopt_into_live, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_stop_drains_in_flight(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    from case_manager_test_utils import TicketCase
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    await adopt_into_live(manager, staging / "c1")
    await manager.start()
    await manager.stop()
    assert not manager._running
    assert manager._stop_completed


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    await manager.stop()
    await manager.stop()  # must not raise or redo teardown
    assert manager._stop_completed
    assert not manager._filespace_lease.is_active()
