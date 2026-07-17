# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_lifecycle_properties(tmp_path):
    manager = provision_manager(tmp_path)
    assert manager.is_recovered is False
    assert manager.is_running is False
    await manager.recover()
    assert manager.is_recovered is True
    await manager.start()
    assert manager.is_running is True
    await manager.stop()
    assert manager.is_running is False


@pytest.mark.asyncio
async def test_is_idle(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    assert manager.is_idle is True
    # A pooled case means not idle.
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await adopt_into_live(manager, staging / "c1")
    assert manager.is_idle is False


@pytest.mark.asyncio
async def test_is_idle_false_with_pending_intake(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    intake = manager._mailbox.fire_intake()
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "req.yaml").write_text("pending: true\n", encoding="utf-8")
    assert manager.is_idle is False
