# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TerminalCase, attach_adapter, transport_for, TicketCase, adopt_into_live, provision_manager, seed_detached_case


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

    queued = adapter.transport.queued()
    queued.mkdir(parents=True, exist_ok=True)
    (queued / "req.yaml").write_text("op: fire\n", encoding="utf-8")

    assert adapter.is_idle is False, "the adapter sees the backlog"
    assert manager.is_idle is True, "the fleet has nothing pooled, and says only that"


@pytest.mark.asyncio
async def test_a_departure_in_flight_is_not_idle(tmp_path):
    """A case leaves the pool when termination is *enqueued*, not when it lands.

    Exiting the moment the pool empties would leave a batch job's own output
    sitting in `live` with pending tickets — recoverable only by a restart that,
    for a finished job, never comes.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager._driver.fire(case.case_folder, "finish")

    manager._reconcile_terminal_in_pool()
    assert len(manager._driver) == 0, "the case has left the pool"
    assert manager.is_idle is False, "but its archive move has not happened yet"

    await manager._maintenance_tick()

    assert manager.is_idle is True, "and now it has"
