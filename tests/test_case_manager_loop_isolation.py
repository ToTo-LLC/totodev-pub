# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""One bad case must not take down the fleet.

The manager loop gives up after ``_LOOP_FAILURE_LIMIT`` consecutive failures, so
any per-case failure that repeats deterministically every tick is a fleet-wide
outage unless it is contained. These tests pin the containment at the three
places a per-case failure can reach the loop: the pool sweep's rehydration
recovery, the terminal reconciler, and escalation detection.
"""

from pathlib import Path

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import (
    BalancedCasePoolDriver,
)
from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolEventNames
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@pytest.mark.asyncio
async def test_unrehydratable_case_is_evicted_not_raised(tmp_path, monkeypatch):
    """A corrupt case_record.yaml evicts one slot instead of killing the sweep.

    _live_or_evict runs mid-sweep, outside any per-item isolation, and repeats
    every beat — so an escaping exception exhausts the loop-failure budget in
    three ticks.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case.case_detach()   # force _live_or_evict down the rehydrate path

    def corrupt_rehydrate(folder):
        raise ValueError("case_record.yaml is not parseable")

    monkeypatch.setattr(case_type_registry, "rehydrate", corrupt_rehydrate)

    evicted: list[str] = []
    manager._driver.case_event_subscribe(
        CasePoolEventNames.EVICTED, lambda ev: evicted.append(ev.case.case_id)
    )

    driver = manager._driver
    assert isinstance(driver, BalancedCasePoolDriver)
    slot = next(iter(driver._by_folder.values()))
    assert driver._live_or_evict(slot) is False, "the slot must be evicted, not raised through"
    assert evicted == [case.case_id]
    assert len(driver) == 0


@pytest.mark.asyncio
async def test_terminal_reconcile_isolates_one_failing_case(tmp_path, monkeypatch):
    """A failing begin_termination costs one case, not the whole reconcile pass."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    cases = []
    for name in ("a", "b"):
        staging = tmp_path / f"inbound_{name}"
        seed_detached_case(TicketCase, staging)
        cases.append(await adopt_into_live(manager, staging))

    monkeypatch.setattr(
        manager._driver, "terminal_cases", lambda: list(cases)
    )

    import totodev_pub.case_manager as case_manager_module

    calls: list[str] = []

    def flaky_begin_termination(case, **kwargs):
        calls.append(case.case_id)
        if case.case_id == cases[0].case_id:
            raise OSError("stale .lock sidecar")
        return True

    monkeypatch.setattr(case_manager_module, "begin_termination", flaky_begin_termination)

    notices = []
    manager.subscribe_notices(notices.append)

    count = manager._reconcile_terminal_in_pool()

    assert calls == [c.case_id for c in cases], "the second case is still visited"
    assert count == 1, "only the healthy case was enqueued"
    assert [n.kind.value for n in notices] == ["MAINTENANCE_ITEM_FAILED"]

    for case in cases:
        if not case.case_is_detached:
            case.case_detach()


@pytest.mark.asyncio
async def test_escalation_detection_failure_does_not_abort_the_tick(tmp_path, monkeypatch):
    """_detect_escalations is diagnostic; its failure must not end the tick."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    def boom():
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(manager, "_detect_escalations", boom)

    notices = []
    manager.subscribe_notices(notices.append)

    await manager._maintenance_tick()

    assert manager._last_tick_completed is not None, "the tick still completed"
    assert [n.kind.value for n in notices] == ["MAINTENANCE_ITEM_FAILED"]
    assert "condition detection" in notices[0].detail["message"]


@pytest.mark.asyncio
async def test_on_tick_completed_runs_after_maintenance(tmp_path):
    """Tail seam fires after maintenance on each successful loop iteration."""
    import asyncio

    manager = provision_manager(tmp_path, enable_fleet_status_board=False)
    await manager.recover()

    order: list[str] = []

    async def maintenance_cb():
        order.append("maintenance")

    async def tick_done_cb():
        order.append("tick_completed")

    manager.on_maintenance(maintenance_cb)
    manager.on_tick_completed(tick_done_cb)
    await manager.start()
    try:
        for _ in range(50):
            if order.count("tick_completed") >= 1 and order.count("maintenance") >= 1:
                break
            await asyncio.sleep(0.05)
        assert "maintenance" in order and "tick_completed" in order
        first_maint = order.index("maintenance")
        first_done = order.index("tick_completed")
        assert first_maint < first_done
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_on_tick_completed_raise_is_isolated(tmp_path):
    """A raising tick-completed callback does not kill the manager loop."""
    import asyncio

    manager = provision_manager(tmp_path, enable_fleet_status_board=False)
    await manager.recover()

    calls = {"boom": 0, "ok": 0}
    notices = []
    manager.subscribe_notices(notices.append)

    async def boom():
        calls["boom"] += 1
        raise RuntimeError("tick observer exploded")

    async def ok():
        calls["ok"] += 1

    manager.on_tick_completed(boom)
    manager.on_tick_completed(ok)
    await manager.start()
    try:
        for _ in range(50):
            if calls["ok"] >= 1 and calls["boom"] >= 1:
                break
            await asyncio.sleep(0.05)
        assert calls["boom"] >= 1 and calls["ok"] >= 1
        assert any(n.kind.value == "MAINTENANCE_ITEM_FAILED" for n in notices)
        assert manager.is_running
    finally:
        await manager.stop()
