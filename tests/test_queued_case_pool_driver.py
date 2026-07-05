"""Tests for QueuedCasePoolDriver — queue-ordered seniority and requeue-on-wake."""

import asyncio

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.queued_case_pool_driver import (
    QueuedCasePoolDriver,
)


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


class BurstCase(FolderBackedCase):
    """Auto chain ending at a manual gate — for burst-FIFO tests."""

    asset_aliases = {}
    fsm_trigger_chokes = {"a": {"cpu"}, "b": {"cpu"}}
    fsm_state_chains = ["^s0--a-->s1--b-->waiting==done-->done^"]

    async def perform_a(self, tctx):
        pass

    async def perform_b(self, tctx):
        pass

    async def perform_done(self, tctx):
        pass


class WakeCase(FolderBackedCase):
    """Manual stall then auto resume — for requeue-on-wake."""

    asset_aliases = {}
    fsm_trigger_chokes = {"resume": {"cpu"}, "work": {"cpu"}}
    fsm_state_chains = ["^idle==resume-->active--work-->done^"]

    async def perform_resume(self, tctx):
        pass

    async def perform_work(self, tctx):
        pass


class ChokedStepCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {"step": {"cpu"}}
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        await self._gate.wait()


class FastChokedCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {"step": {"cpu"}}
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        pass


class PlainAutoCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        pass


def _run(coro):
    return asyncio.run(coro)


def _make(case_cls, tmp_path, name, **kw):
    return case_cls.create_case_in_folder(tmp_path / name, **kw)


def _folders(driver):
    return list(driver._by_folder.keys())


def test_seniority_front_case_gets_choke_permit(tmp_path):
    async def body():
        driver = QueuedCasePoolDriver(choke_limits={"cpu": 1})
        front = _make(ChokedStepCase, tmp_path, "front")
        back = _make(FastChokedCase, tmp_path, "back")
        front._gate = asyncio.Event()
        driver.add(front)
        driver.add(back)
        driver.boost(front.case_folder)
        driver.boost(back.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert front.case_folder == _folders(driver)[0]
        assert driver._in_flight_count == 1
        assert driver._by_folder[back.case_folder].skip_countdown == 1
        front._gate.set()
        await driver.settle()

    _run(body())


def test_requeue_on_wake_moves_to_tail(tmp_path):
    async def body():
        driver = QueuedCasePoolDriver(choke_limits={"cpu": 1})
        stall = _make(WakeCase, tmp_path, "stall")
        peer = _make(FastChokedCase, tmp_path, "peer")
        driver.add(stall)
        driver.add(peer)
        assert driver.peek(stall.case_folder).queue_position == 0

        await driver.fire(stall.case_folder, "resume")
        assert stall.case_state == "active"
        assert driver.peek(stall.case_folder).queue_position == 1
        assert driver.peek(peer.case_folder).queue_position == 0

        await driver.fire(stall.case_folder, None)
        await driver.settle()
        assert stall.case_state == "done"

    _run(body())


def test_ceiling_backpressure_favors_senior(tmp_path):
    async def body():
        driver = QueuedCasePoolDriver(concurrency_ceiling=1, choke_limits={})
        cases = [_make(PlainAutoCase, tmp_path, f"c{i}") for i in range(3)]
        for c in cases:
            driver.add(c)
            driver.boost(c.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert driver._in_flight_count == 1
        senior = _folders(driver)[0]
        assert driver._by_folder[senior].in_flight
        deferred = [
            s for s in driver._by_folder.values()
            if not s.in_flight and s.skip_countdown == 1
        ]
        assert len(deferred) >= 1
        await driver.settle()

    _run(body())


def test_burst_fifo_case_one_reaches_manual_before_case_two(tmp_path):
    async def body():
        driver = QueuedCasePoolDriver(choke_limits={"cpu": 1})
        one = _make(BurstCase, tmp_path, "one")
        two = _make(BurstCase, tmp_path, "two")
        driver.add(one)
        driver.add(two)
        driver.boost(one.case_folder)
        driver.boost(two.case_folder)

        for _ in range(4):
            await driver.advance(suggested_interval_secs=0.0)
            await driver.settle()
            if one.case_state == "waiting":
                break

        assert one.case_state == "waiting"
        assert two.case_state == "s0"

    _run(body())


def test_fire_priority_wakes_on_release(tmp_path):
    async def body():
        driver = QueuedCasePoolDriver(choke_limits={"cpu": 1})
        holder = _make(ChokedStepCase, tmp_path, "holder")
        waiter_case = _make(WakeCase, tmp_path, "waiter")
        holder._gate = asyncio.Event()
        driver.add(holder)
        driver.add(waiter_case)
        driver.boost(holder.case_folder)
        await driver.advance(suggested_interval_secs=0.0)

        fire_task = asyncio.create_task(
            driver.fire(waiter_case.case_folder, "resume"),
        )
        await asyncio.sleep(0.01)
        assert not fire_task.done()

        holder._gate.set()
        await driver.settle()
        result = await fire_task
        assert result.progressed
        assert waiter_case.case_state == "active"

    _run(body())
