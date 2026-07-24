"""Tests for SeniorityCasePoolDriver — queue-ordered seniority and requeue-on-wake."""

import asyncio

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import (
    Tier,
    _TierPolicy,
)
from totodev_pub.folder_backed_case_support.seniority_case_pool_driver import (
    SeniorityCasePoolDriver,
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
    fsm_state_chains = ["[*] --> s0 -- a --> s1 -- b --> waiting == done ==> done --> [*]"]

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
    fsm_state_chains = ["[*] --> idle == resume ==> active -- work --> done --> [*]"]

    async def perform_resume(self, tctx):
        pass

    async def perform_work(self, tctx):
        pass


class ChokedStepCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {"step": {"cpu"}}
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        await self._gate.wait()


class FastChokedCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {"step": {"cpu"}}
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        pass


class PlainAutoCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        pass


class GuardedNoopCase(FolderBackedCase):
    """Advanceable auto exit whose guard always declines — normal HOT demotion ladder."""

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> hold -- go [blockit] --> done --> [*]"]

    async def guard_blockit(self, tctx):
        return False

    async def perform_go(self, tctx):
        pass


class LongAutoCase(FolderBackedCase):
    """Several auto steps so post-step cadence can be inspected while still HOT."""

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> s0 -- a --> s1 -- b --> s2 -- c --> s3 --> [*]"]

    async def perform_a(self, tctx):
        pass

    async def perform_b(self, tctx):
        pass

    async def perform_c(self, tctx):
        pass


def _run(coro):
    return asyncio.run(coro)


def _make(case_cls, tmp_path, name, **kw):
    return case_cls.create_case_in_folder(tmp_path / name, **kw)


def _folders(driver):
    return list(driver._by_folder.keys())


def test_seniority_front_case_gets_choke_permit(tmp_path):
    async def body():
        driver = SeniorityCasePoolDriver(choke_limits={"cpu": 1})
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
        driver = SeniorityCasePoolDriver(choke_limits={"cpu": 1})
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
        driver = SeniorityCasePoolDriver(concurrency_ceiling=1, choke_limits={})
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
        driver = SeniorityCasePoolDriver(choke_limits={"cpu": 1})
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


def test_seniority_order_overrides_choke_wait_streak(tmp_path):
    """SeniorityCasePoolDriver overrides ``_order_chokeables`` to keep queue order
    even when the base class's wait-streak fairness would disagree: the junior case
    accrues a higher ``choke_wait_streak`` than the senior one, yet the senior case
    still wins the contested permit."""
    async def body():
        driver = SeniorityCasePoolDriver(choke_limits={"cpu": 1})
        holder = _make(ChokedStepCase, tmp_path, "streak_holder")
        senior = _make(FastChokedCase, tmp_path, "streak_senior")   # front of queue
        junior = _make(FastChokedCase, tmp_path, "streak_junior")   # back of queue
        holder._gate = asyncio.Event()
        driver.add(holder)
        driver.add(senior)
        driver.add(junior)
        driver._by_folder[senior.case_folder].skip_countdown = 1000
        driver._by_folder[junior.case_folder].skip_countdown = 1000

        driver.boost(holder.case_folder)
        await driver.advance(suggested_interval_secs=0.0)   # holder in-flight, holds cpu
        assert driver._by_folder[holder.case_folder].in_flight

        driver.boost(junior.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.advance(suggested_interval_secs=0.0)
        assert driver._by_folder[junior.case_folder].choke_wait_streak == 2
        assert driver._by_folder[senior.case_folder].choke_wait_streak == 0

        driver.boost(senior.case_folder)
        holder._gate.set()
        await driver.settle()
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()

        assert senior.case_state == "s1"
        assert junior.case_state == "s0"

    _run(body())


def test_fire_priority_wakes_on_release(tmp_path):
    async def body():
        driver = SeniorityCasePoolDriver(choke_limits={"cpu": 1})
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


# ---------------------------------------------------------------------------
# Senior-hot acceleration
# ---------------------------------------------------------------------------

def test_senior_hot_faster_than_junior_hot(tmp_path):
    """Front-N HOT slots get senior_hot_multiple; the rest keep policy.M_HOT."""
    async def body():
        policy = _TierPolicy(M_HOT=5)
        driver = SeniorityCasePoolDriver(
            policy=policy,
            choke_limits={},
            senior_count=1,
            senior_hot_multiple=1,
        )
        front = _make(LongAutoCase, tmp_path, "front")
        back = _make(LongAutoCase, tmp_path, "back")
        driver.add(front)
        driver.add(back)

        await driver.fire(front.case_folder, None)
        await driver.fire(back.case_folder, None)

        front_slot = driver._by_folder[front.case_folder]
        back_slot = driver._by_folder[back.case_folder]
        assert front_slot.tier is Tier.HOT
        assert back_slot.tier is Tier.HOT
        assert front_slot.reset_multiple == 1
        assert front_slot.skip_countdown == 1
        assert back_slot.reset_multiple == 5
        assert back_slot.skip_countdown == 5
        assert driver.peek(front.case_folder).is_senior is True
        assert driver.peek(back.case_folder).is_senior is False

    _run(body())


def test_warm_ignores_seniority(tmp_path):
    """A front-of-queue WARM case keeps M_WARM — seniority never overrides WARM/COLD."""
    async def body():
        policy = _TierPolicy(M_HOT=5, M_WARM=10)
        driver = SeniorityCasePoolDriver(
            policy=policy,
            choke_limits={},
            senior_count=3,
            senior_hot_multiple=1,
        )
        case = _make(GuardedNoopCase, tmp_path, "guarded")
        driver.add(case)
        assert driver.peek(case.case_folder).is_senior is True
        assert driver.peek(case.case_folder).tier is Tier.HOT

        for _ in range(policy.K_HOT_TO_WARM):
            await driver.fire(case.case_folder, None)

        peek = driver.peek(case.case_folder)
        slot = driver._by_folder[case.case_folder]
        assert peek.tier is Tier.WARM
        assert peek.is_senior is True
        assert slot.reset_multiple == policy.M_WARM

    _run(body())


def test_demotion_drops_senior_hot_acceleration(tmp_path):
    """Demoting past K_HOT_TO_WARM automatically drops senior-hot override."""
    async def body():
        policy = _TierPolicy(M_HOT=5, M_WARM=10)
        driver = SeniorityCasePoolDriver(
            policy=policy,
            choke_limits={},
            senior_count=3,
            senior_hot_multiple=1,
        )
        case = _make(GuardedNoopCase, tmp_path, "demote")
        driver.add(case)
        assert driver._by_folder[case.case_folder].reset_multiple == 1

        for _ in range(policy.K_HOT_TO_WARM):
            await driver.fire(case.case_folder, None)

        slot = driver._by_folder[case.case_folder]
        assert slot.tier is Tier.WARM
        assert slot.reset_multiple == policy.M_WARM

    _run(body())


def test_admission_time_senior_hotty(tmp_path):
    """Empty/small pools admit HOT cases as senior-hotty when len(pool) < N."""
    async def body():
        policy = _TierPolicy(M_HOT=5)
        driver = SeniorityCasePoolDriver(
            policy=policy,
            choke_limits={},
            senior_count=3,
            senior_hot_multiple=1,
        )
        case = _make(PlainAutoCase, tmp_path, "solo")
        driver.add(case)
        slot = driver._by_folder[case.case_folder]
        assert slot.tier is Tier.HOT
        assert slot.reset_multiple == 1
        assert slot.skip_countdown == 1
        assert driver.peek(case.case_folder).is_senior is True

    _run(body())


def test_default_construction_is_noop_for_hot_cadence(tmp_path):
    """Stock defaults: senior_hot_multiple matches M_HOT, so seniority does not change cadence."""
    async def body():
        driver = SeniorityCasePoolDriver(choke_limits={})
        cases = [_make(LongAutoCase, tmp_path, f"c{i}") for i in range(4)]
        for c in cases:
            driver.add(c)

        for c in cases:
            await driver.fire(c.case_folder, None)

        for c in cases:
            slot = driver._by_folder[c.case_folder]
            assert slot.tier is Tier.HOT
            assert slot.reset_multiple == driver._policy.M_HOT
            assert slot.skip_countdown == driver._policy.M_HOT

    _run(body())
