# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import attach_adapter, transport_for, TicketCase, adopt_into_live, provision_manager, seed_detached_case


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
    assert manager.is_recovered is True


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
async def test_pending_intake_is_the_adapters_half_of_idle(tmp_path):
    """The manager can no longer see unserved requests, and should not pretend to.

    "Is the whole system idle" is now a composite: an empty pool *and* an empty
    intake. Each half is answered by whoever owns it.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    adapter = attach_adapter(manager)
    assert manager.is_idle is True
    assert adapter.is_idle is True

    intake = adapter.transport.fire_intake()
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "req.yaml").write_text("pending: true\n", encoding="utf-8")

    assert adapter.is_idle is False, "the adapter sees the backlog"
    assert manager.is_idle is True, "the fleet has nothing pooled, and says only that"
