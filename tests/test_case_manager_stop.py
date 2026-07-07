# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import ManualCase, adopt_into_live, provision_manager, seed_detached_case


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
