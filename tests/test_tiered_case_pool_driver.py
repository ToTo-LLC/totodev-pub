"""Tests for TieredCasePoolDriver — the concrete MLFQ scheduling driver.

Driven deterministically: tier-reclassification tests use ``fire(folder, None)`` (which
routes one step through the same completion path the beat uses, bypassing the countdown),
and beat-level tests pair ``advance(suggested_interval_secs=0.0)`` with ``settle()`` so the
background case-step tasks run to a quiescent point before assertions.
"""

import asyncio
import shutil

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseInFlightError,
    DetachedCaseError,
)
from totodev_pub.folder_backed_case_support.tiered_case_pool_driver import (
    TieredCasePoolDriver,
    Tier,
    _TierPolicy,
)
from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolEventNames


# ---------------------------------------------------------------------------
# Registry isolation (the singleton is process-wide mutable state)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


# ---------------------------------------------------------------------------
# Case fixtures (small FSMs covering each behaviour the driver keys off)
# ---------------------------------------------------------------------------

class AutoCase(FolderBackedCase):
    """Two auto edges to a terminal: progresses on every step, then closes."""
    fsm_state_chains = ["^s0--step-->s1--step2-->s2^"]

    async def perform_step(self, tctx):
        pass

    async def perform_step2(self, tctx):
        pass


class ManualCase(FolderBackedCase):
    """Manual-only (no auto exit): not advanceable -> accelerated demotion."""
    fsm_state_chains = ["^waiting==push-->done^"]


class GuardedCase(FolderBackedCase):
    """Has an auto exit (advanceable) whose guard always declines: blocked, normal ladder."""
    fsm_state_chains = ["^hold--blockit#go-->done^"]

    async def guard_blockit(self, tctx):
        return False

    async def perform_go(self, tctx):
        pass


class FailCase(FolderBackedCase):
    """Auto edge whose work raises, with retry room (@FAIL<5): repeated failures."""
    fsm_state_chains = ["^start--@FAIL<5#tryit-->done^"]

    async def perform_tryit(self, tctx):
        raise RuntimeError("boom")


class AlertProgressCase(FolderBackedCase):
    """One step that logs an alert AND progresses to a terminal (exercises event order)."""
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        self.case_log_alert("heads up")


class BlockingCase(FolderBackedCase):
    """Auto step that blocks on an injected gate, to hold a case in-flight."""
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        await self._gate.wait()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make(case_cls, tmp_path, name, **kw):
    return case_cls.create_case_in_folder(tmp_path / name, **kw)


# ---------------------------------------------------------------------------
# Container + membership
# ---------------------------------------------------------------------------

def test_add_contains_len_get_find(tmp_path):
    driver = TieredCasePoolDriver()
    case = _make(AutoCase, tmp_path, "c1", case_id="c1")
    try:
        driver.add(case)
        assert len(driver) == 1
        assert case.case_folder in driver
        assert driver[case.case_folder] is case
        assert driver.find(case.case_folder) is case
        assert list(iter(driver)) == [case]
    finally:
        case.case_detach()


def test_add_rejects_duplicate(tmp_path):
    driver = TieredCasePoolDriver()
    case = _make(AutoCase, tmp_path, "dup")
    try:
        driver.add(case)
        with pytest.raises(ValueError):
            driver.add(case)
    finally:
        case.case_detach()


def test_add_rejects_detached(tmp_path):
    driver = TieredCasePoolDriver()
    case = _make(AutoCase, tmp_path, "det")
    case.case_detach()
    with pytest.raises(DetachedCaseError):
        driver.add(case)


def test_admission_tiers(tmp_path):
    driver = TieredCasePoolDriver()
    auto = _make(AutoCase, tmp_path, "auto")
    manual = _make(ManualCase, tmp_path, "manual")
    try:
        driver.add(auto)
        driver.add(manual)
        assert driver.peek(auto.case_folder).tier is Tier.HOT
        assert driver.peek(manual.case_folder).tier is Tier.WARM
    finally:
        auto.case_detach()
        manual.case_detach()


def test_admission_closed_case_is_dormant(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(AutoCase, tmp_path, "closed")
        # Drive it to its terminal state first (still bound), then admit it.
        await case.case_advance()
        await case.case_advance()
        assert case.case_is_closed
        driver.add(case)
        peek = driver.peek(case.case_folder)
        assert peek.closed is True
        assert peek.skip_countdown <= 0          # dormant
        assert case in driver.closed_cases()
        case.case_detach()

    _run(body())


# ---------------------------------------------------------------------------
# Tier reclassification (driven via fire(None), deterministic)
# ---------------------------------------------------------------------------

def test_progress_sets_hot(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(AutoCase, tmp_path, "prog")
        driver.add(case)
        result = await driver.fire(case.case_folder, None)
        assert result.progressed
        peek = driver.peek(case.case_folder)
        assert peek.tier is Tier.HOT
        assert peek.noop_streak == 0
        assert case.case_state == "s1"
        case.case_detach()

    _run(body())


def test_noop_streak_demotes_hot_to_warm(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(GuardedCase, tmp_path, "guarded")
        driver.add(case)
        assert driver.peek(case.case_folder).tier is Tier.HOT
        # K_HOT_TO_WARM == 3 blocked no-ops.
        for _ in range(3):
            await driver.fire(case.case_folder, None)
        assert driver.peek(case.case_folder).tier is Tier.WARM
        case.case_detach()

    _run(body())


def test_structural_deadend_accelerated_demotion(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(ManualCase, tmp_path, "accel")
        driver.add(case)
        assert driver.peek(case.case_folder).tier is Tier.WARM   # admitted warm (not advanceable)
        # Accelerated K_WARM_TO_COLD == 2.
        for _ in range(2):
            await driver.fire(case.case_folder, None)
        assert driver.peek(case.case_folder).tier is Tier.COLD
        case.case_detach()

    _run(body())


def test_failure_holds_warm_with_backoff(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(FailCase, tmp_path, "fail")
        driver.add(case)
        for n in range(1, 4):
            result = await driver.fire(case.case_folder, None)
            assert result.failed
            slot = driver._by_folder[case.case_folder]
            assert slot.tier is Tier.WARM
            assert slot.fail_streak == n
            assert slot.reset_multiple == driver._policy.M_WARM * n
        case.case_detach()

    _run(body())


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def test_advance_event_order_alerted_advanced_closed(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(AlertProgressCase, tmp_path, "order")
        driver.add(case)
        seen = []
        driver.case_event_subscribe(
            {CasePoolEventNames.ALERTED, CasePoolEventNames.ADVANCED,
             CasePoolEventNames.CLOSED, CasePoolEventNames.FAILED},
            lambda ev: seen.append(ev.event),
        )
        await driver.fire(case.case_folder, None)
        assert seen == ["alerted", "advanced", "closed"]
        case.case_detach()

    _run(body())


def test_failed_event_fires(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(FailCase, tmp_path, "failev")
        driver.add(case)
        seen = []
        driver.case_event_subscribe(
            CasePoolEventNames.FAILED, lambda ev: seen.append(ev),
        )
        await driver.fire(case.case_folder, None)
        assert len(seen) == 1
        assert seen[0].event == "failed"
        assert seen[0].advance_result is not None and seen[0].advance_result.failed
        case.case_detach()

    _run(body())


def test_admitted_and_removed_events_and_unsubscribe(tmp_path):
    driver = TieredCasePoolDriver()
    case = _make(AutoCase, tmp_path, "evmember")
    try:
        seen = []
        handle = driver.case_event_subscribe(
            {CasePoolEventNames.ADMITTED, CasePoolEventNames.REMOVED},
            lambda ev: seen.append(ev.event),
        )
        driver.add(case)
        returned = driver.remove(case.case_folder)
        assert returned is case
        assert seen == ["admitted", "removed"]
        driver.case_event_unsubscribe(handle)
        with pytest.raises(KeyError):
            driver.case_event_unsubscribe(handle)
    finally:
        case.case_detach()


def test_duplicate_subscription_handle_rejected(tmp_path):
    driver = TieredCasePoolDriver()
    driver.case_event_subscribe(CasePoolEventNames.ADMITTED, lambda ev: None, handle="h")
    with pytest.raises(ValueError):
        driver.case_event_subscribe(CasePoolEventNames.REMOVED, lambda ev: None, handle="h")


# ---------------------------------------------------------------------------
# fire() / boost()
# ---------------------------------------------------------------------------

def test_fire_manual_trigger(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(ManualCase, tmp_path, "firem")
        driver.add(case)
        result = await driver.fire(case.case_folder, "push")
        assert result.progressed
        assert case.case_state == "done"
        case.case_detach()

    _run(body())


def test_fire_inflight_returns_in_progress_result(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(BlockingCase, tmp_path, "inflight")
        case._gate = asyncio.Event()
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance(suggested_interval_secs=0.0)   # launches the step; blocks on gate
        slot = driver._by_folder[case.case_folder]
        assert slot.in_flight is True
        # A fire while in-flight must hand back the in-progress result, not launch anew.
        case._gate.set()
        result = await driver.fire(case.case_folder, None)
        assert result.progressed
        assert case.case_state == "s1"
        await driver.settle()

    _run(body())


def test_boost_schedules_next_beat(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(GuardedCase, tmp_path, "boost")
        driver.add(case)
        driver.boost(case.case_folder)
        assert driver._by_folder[case.case_folder].skip_countdown == 1
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert driver.peek(case.case_folder).last_result is not None
        case.case_detach()

    _run(body())


# ---------------------------------------------------------------------------
# request_halt / remove
# ---------------------------------------------------------------------------

def test_request_halt_fires_halted_then_remove(tmp_path):
    driver = TieredCasePoolDriver()
    case = _make(GuardedCase, tmp_path, "halt")
    try:
        seen = []
        driver.case_event_subscribe(
            CasePoolEventNames.HALTED, lambda ev: seen.append(ev.event),
        )
        driver.add(case)
        driver.request_halt(case.case_folder)
        assert seen == ["halted"]
        assert case not in driver.halted_cases()        # settled (HALTED fired)
        returned = driver.remove(case.case_folder)
        assert returned is case
        assert case.case_folder not in driver
    finally:
        case.case_detach()


def test_remove_inflight_raises(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(BlockingCase, tmp_path, "rmflight")
        case._gate = asyncio.Event()
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert driver._by_folder[case.case_folder].in_flight is True
        with pytest.raises(CaseInFlightError):
            driver.remove(case.case_folder)
        case._gate.set()
        await driver.settle()

    _run(body())


# ---------------------------------------------------------------------------
# Detach recovery + eviction
# ---------------------------------------------------------------------------

def test_rehydrate_on_detach(tmp_path):
    async def body():
        case_type_registry.register_case_types(AutoCase)
        driver = TieredCasePoolDriver()
        case = _make(AutoCase, tmp_path, "rehy")
        folder = case.case_folder
        driver.add(case)
        case.case_detach()
        assert case.case_is_detached
        # A beat hits the slot (boosted to fire), notices the detach, and rehydrates fresh.
        driver.boost(folder)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert folder in driver
        fresh = driver[folder]
        assert fresh is not case
        assert not fresh.case_is_detached
        fresh.case_detach()

    _run(body())


def test_evict_on_missing_folder(tmp_path):
    async def body():
        case_type_registry.register_case_types(AutoCase)
        driver = TieredCasePoolDriver()
        case = _make(AutoCase, tmp_path, "evict")
        folder = case.case_folder
        seen = []
        driver.case_event_subscribe(
            CasePoolEventNames.EVICTED, lambda ev: seen.append(ev.event),
        )
        driver.add(case)
        case.case_detach()
        shutil.rmtree(folder)                # folder gone -> rehydrate impossible
        driver.boost(folder)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert folder not in driver
        assert seen == ["evicted"]

    _run(body())


# ---------------------------------------------------------------------------
# Heartbeat walk + concurrency ceiling
# ---------------------------------------------------------------------------

def test_heartbeat_walk_touches_cases(tmp_path):
    async def body():
        driver = TieredCasePoolDriver()
        case = _make(GuardedCase, tmp_path, "hb")
        driver.add(case)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert driver._by_folder[case.case_folder].last_heartbeat_at is not None
        case.case_detach()

    _run(body())


def test_concurrency_ceiling_defers_launches(tmp_path):
    async def body():
        driver = TieredCasePoolDriver(concurrency_ceiling=1)
        a = _make(BlockingCase, tmp_path, "cap_a")
        b = _make(BlockingCase, tmp_path, "cap_b")
        a._gate = asyncio.Event()
        b._gate = asyncio.Event()
        driver.add(a)
        driver.add(b)
        driver.boost(a.case_folder)
        driver.boost(b.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        # Only one may be in-flight; the other is deferred to retry next beat.
        assert driver._in_flight_count == 1
        deferred = [s for s in driver._by_folder.values() if not s.in_flight]
        assert len(deferred) == 1
        assert deferred[0].skip_countdown == 1
        a._gate.set()
        b._gate.set()
        await driver.settle()

    _run(body())
