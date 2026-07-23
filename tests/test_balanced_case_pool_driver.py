"""Tests for BalancedCasePoolDriver — the concrete MLFQ scheduling driver.

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
    UnconfiguredChokeError,
)
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import (
    BalancedCasePoolDriver,
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

    asset_aliases = []
    fsm_trigger_chokes = {}
    """Two auto edges to a terminal: progresses on every step, then closes."""
    fsm_state_chains = ["[*] --> s0 -- step --> s1 -- step2 --> s2 --> [*]"]

    async def perform_step(self, tctx):
        pass

    async def perform_step2(self, tctx):
        pass


class LongAutoCase(FolderBackedCase):

    asset_aliases = []
    fsm_trigger_chokes = {}
    """Four auto edges to a terminal: progresses on every step, then closes.
    Long enough to observe several consecutive eager-paced beats before the
    tempo relaxes back to the full period."""
    fsm_state_chains = ["[*] --> s0 -- step1 --> s1 -- step2 --> s2 -- step3 --> s3 -- step4 --> s4 --> [*]"]

    async def perform_step1(self, tctx):
        pass

    async def perform_step2(self, tctx):
        pass

    async def perform_step3(self, tctx):
        pass

    async def perform_step4(self, tctx):
        pass


class ManualCase(FolderBackedCase):


    asset_aliases = []
    fsm_trigger_chokes = {}
    """Manual-only (no auto exit): not advanceable -> accelerated demotion."""
    fsm_state_chains = ["[*] --> waiting == push ==> done --> [*]"]


class GuardedCase(FolderBackedCase):


    asset_aliases = []
    fsm_trigger_chokes = {}
    """Has an auto exit (advanceable) whose guard always declines: blocked, normal ladder."""
    fsm_state_chains = ["[*] --> hold -- go [blockit] --> done --> [*]"]

    async def guard_blockit(self, tctx):
        return False

    async def perform_go(self, tctx):
        pass


class FailCase(FolderBackedCase):


    asset_aliases = []
    fsm_trigger_chokes = {}
    """Auto edge whose work raises, with retry room (@FAIL<5): repeated failures."""
    fsm_state_chains = ["[*] --> start -- tryit [@FAIL<5] --> done --> [*]"]

    async def perform_tryit(self, tctx):
        raise RuntimeError("boom")


class AlertProgressCase(FolderBackedCase):


    asset_aliases = []
    fsm_trigger_chokes = {}
    """One step that logs an alert AND progresses to a terminal (exercises event order)."""
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        self.case_emit_alert_event("heads up")


class BlockingCase(FolderBackedCase):


    asset_aliases = []
    fsm_trigger_chokes = {}
    """Auto step that blocks on an injected gate, to hold a case in-flight."""
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        await self._gate.wait()


class BlockingThenManualCase(FolderBackedCase):

    asset_aliases = []
    fsm_trigger_chokes = {}
    """Blocking auto step into a state with a manual exit: lets a test queue a pinned
    trigger behind an in-flight step."""
    fsm_state_chains = ["[*] --> s0 -- step --> s1 == push ==> done --> [*]"]

    async def perform_step(self, tctx):
        await self._gate.wait()


class ChokedBlockingCase(FolderBackedCase):

    asset_aliases = []
    fsm_trigger_chokes = {"step": {"cpu"}}
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        await self._gate.wait()


class CpuChokedCase(FolderBackedCase):

    asset_aliases = []
    fsm_trigger_chokes = {"step": {"cpu"}}
    fsm_state_chains = ["[*] --> s0 -- step --> s1 --> [*]"]

    async def perform_step(self, tctx):
        pass


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
    driver = BalancedCasePoolDriver()
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
    driver = BalancedCasePoolDriver()
    case = _make(AutoCase, tmp_path, "dup")
    try:
        driver.add(case)
        with pytest.raises(ValueError):
            driver.add(case)
    finally:
        case.case_detach()


def test_add_rejects_detached(tmp_path):
    driver = BalancedCasePoolDriver()
    case = _make(AutoCase, tmp_path, "det")
    case.case_detach()
    with pytest.raises(DetachedCaseError):
        driver.add(case)


def test_admission_tiers(tmp_path):
    driver = BalancedCasePoolDriver()
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


def test_admission_terminal_case_is_dormant(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(AutoCase, tmp_path, "terminal")
        # Drive it to its terminal state first (still bound), then admit it.
        await case.case_advance()
        await case.case_advance()
        assert case.case_is_terminal
        driver.add(case)
        peek = driver.peek(case.case_folder)
        assert peek.terminal is True
        assert peek.skip_countdown <= 0          # dormant
        assert case in driver.terminal_cases()
        case.case_detach()

    _run(body())


# ---------------------------------------------------------------------------
# Tier reclassification (driven via fire(None), deterministic)
# ---------------------------------------------------------------------------

def test_progress_sets_hot(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
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

def test_advance_event_order_alerted_advanced_terminated(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(AlertProgressCase, tmp_path, "order")
        driver.add(case)
        seen = []
        driver.case_event_subscribe(
            {CasePoolEventNames.ALERTED, CasePoolEventNames.ADVANCED,
             CasePoolEventNames.TERMINATED, CasePoolEventNames.FAILED},
            lambda ev: seen.append(ev.event),
        )
        await driver.fire(case.case_folder, None)
        assert seen == ["alerted", "advanced", "terminated"]
        case.case_detach()

    _run(body())


def test_failed_event_fires(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
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
    driver = BalancedCasePoolDriver()
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
    driver = BalancedCasePoolDriver()
    driver.case_event_subscribe(CasePoolEventNames.ADMITTED, lambda ev: None, handle="h")
    with pytest.raises(ValueError):
        driver.case_event_subscribe(CasePoolEventNames.REMOVED, lambda ev: None, handle="h")


# ---------------------------------------------------------------------------
# fire() / boost()
# ---------------------------------------------------------------------------

def test_fire_manual_trigger(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(ManualCase, tmp_path, "firem")
        driver.add(case)
        result = await driver.fire(case.case_folder, "push")
        assert result.progressed
        assert case.case_state == "done"
        case.case_detach()

    _run(body())


def test_fire_inflight_returns_in_progress_result(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(BlockingCase, tmp_path, "inflight")
        case._gate = asyncio.Event()
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance(suggested_interval_secs=0.0)   # launches the step; blocks on gate
        slot = driver._by_folder[case.case_folder]
        assert slot.in_flight is True
        # A trigger-less fire while in-flight must hand back the in-progress result,
        # not launch anew.
        case._gate.set()
        result = await driver.fire(case.case_folder, None)
        assert result.progressed
        assert case.case_state == "s1"
        await driver.settle()

    _run(body())


def test_fire_pinned_trigger_queues_behind_inflight(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(BlockingThenManualCase, tmp_path, "queued")
        case._gate = asyncio.Event()
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance(suggested_interval_secs=0.0)   # launches step; blocks on gate
        assert driver._by_folder[case.case_folder].in_flight is True
        # A pinned trigger must NOT coalesce (which would drop it): it waits for the
        # in-flight step to finish, then fires as its own step.
        fire_task = asyncio.create_task(driver.fire(case.case_folder, "push"))
        await asyncio.sleep(0.01)
        assert not fire_task.done()
        assert case.case_state == "s0"                      # push not applied yet
        case._gate.set()
        result = await fire_task
        assert result.progressed
        assert result.initial_state == "s1"                 # ran AFTER the blocked step
        assert case.case_state == "done"
        await driver.settle()

    _run(body())


# ---------------------------------------------------------------------------
# attach_fire (tick-paced queue)
# ---------------------------------------------------------------------------

def test_attach_fire_runs_pinned_trigger_on_sweep(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(ManualCase, tmp_path, "attach1")
        driver.add(case)
        seen: list[str] = []
        driver.attach_fire(
            case.case_folder, "push", {},
            on_launch=lambda: seen.append("launch"),
            on_complete=lambda r, e: seen.append("complete" if e is None and r and r.progressed else f"err:{e}"),
        )
        assert driver.peek(case.case_folder).pending_fire_count == 1
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert case.case_state == "done"
        assert seen == ["launch", "complete"]
        assert driver.peek(case.case_folder).pending_fire_count == 0

    _run(body())


def test_attach_fire_multiple_apply_one_per_beat(tmp_path):
    async def body():
        class ChainManual(FolderBackedCase):
            asset_aliases = []
            fsm_trigger_chokes = {}
            fsm_state_chains = ["[*] --> s0 == a ==> s1 == b ==> s2 --> [*]"]

        driver = BalancedCasePoolDriver()
        case = _make(ChainManual, tmp_path, "multi")
        driver.add(case)
        completed: list[str] = []
        driver.attach_fire(
            case.case_folder, "a", {},
            on_complete=lambda r, e: completed.append(r.final_state if r else "err"),
        )
        driver.attach_fire(
            case.case_folder, "b", {},
            on_complete=lambda r, e: completed.append(r.final_state if r else "err"),
        )
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert case.case_state == "s1"
        assert completed == ["s1"]
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert case.case_state == "s2"
        assert completed == ["s1", "s2"]

    _run(body())


def test_attach_fire_choke_denied_stays_queued(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver(choke_limits={"cpu": 1})
        holder = _make(ChokedBlockingCase, tmp_path, "ch_hold")
        waiter = _make(CpuChokedCase, tmp_path, "ch_wait")
        holder._gate = asyncio.Event()
        driver.add(holder)
        driver.add(waiter)
        driver.boost(holder.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert driver._by_folder[holder.case_folder].in_flight

        done: list[bool] = []
        driver.attach_fire(
            waiter.case_folder, None, None,
            on_complete=lambda r, e: done.append(bool(r and r.progressed)),
        )
        await driver.advance(suggested_interval_secs=0.0)
        assert not done
        assert driver.peek(waiter.case_folder).pending_fire_count == 1
        assert driver.peek(waiter.case_folder).choked == frozenset({"cpu"})

        holder._gate.set()
        await driver.settle()
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert done == [True]
        assert waiter.case_state == "s1"

    _run(body())


def test_attach_fire_callback_exception_does_not_wedge(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(ManualCase, tmp_path, "cb_boom")
        driver.add(case)

        def bad_launch():
            raise RuntimeError("launch boom")

        driver.attach_fire(case.case_folder, "push", {}, on_launch=bad_launch)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert case.case_state == "done"   # step still ran

    _run(body())


def test_attach_fire_rejects_halted_and_terminal(tmp_path):
    from totodev_pub.folder_backed_case_support.exceptions import FireRejectedError

    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(ManualCase, tmp_path, "rej")
        driver.add(case)
        driver.request_halt(case.case_folder)
        with pytest.raises(FireRejectedError):
            driver.attach_fire(case.case_folder, "push", {})

        term = _make(AutoCase, tmp_path, "term")
        await term.case_advance()
        await term.case_advance()
        assert term.case_is_terminal
        driver.add(term)
        with pytest.raises(FireRejectedError):
            driver.attach_fire(term.case_folder, None, None)

    _run(body())


def test_attach_fire_remove_fails_pending(tmp_path):
    from totodev_pub.folder_backed_case_support.exceptions import FireRejectedError

    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(ManualCase, tmp_path, "rmq")
        driver.add(case)
        errs: list[BaseException] = []
        driver.attach_fire(
            case.case_folder, "push", {},
            on_complete=lambda r, e: errs.append(e) if e else None,
        )
        driver.remove(case.case_folder)
        assert len(errs) == 1
        assert isinstance(errs[0], FireRejectedError)

    _run(body())


def test_attach_fire_while_inflight_queues(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
        case = _make(BlockingThenManualCase, tmp_path, "infl_q")
        case._gate = asyncio.Event()
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert driver._by_folder[case.case_folder].in_flight

        done: list[str] = []
        driver.attach_fire(
            case.case_folder, "push", {},
            on_complete=lambda r, e: done.append(r.final_state if r else "err"),
        )
        assert driver.peek(case.case_folder).pending_fire_count == 1
        case._gate.set()
        await driver.settle()
        # After the blocking step finishes, pending fire should be due next beat.
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert done == ["done"]
        assert case.case_state == "done"

    _run(body())


def test_boost_schedules_next_beat(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver()
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
    driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
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
        driver = BalancedCasePoolDriver()
        case = _make(GuardedCase, tmp_path, "hb")
        driver.add(case)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert driver._by_folder[case.case_folder].last_heartbeat_at is not None
        case.case_detach()

    _run(body())


def test_concurrency_ceiling_defers_launches(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver(concurrency_ceiling=1)
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


# ---------------------------------------------------------------------------
# Choke resources (§9.2)
# ---------------------------------------------------------------------------

def test_add_rejects_unconfigured_choke_resource(tmp_path):
    driver = BalancedCasePoolDriver(choke_limits={})
    case = _make(CpuChokedCase, tmp_path, "choked")
    try:
        with pytest.raises(UnconfiguredChokeError) as excinfo:
            driver.add(case)
        assert "CpuChokedCase" in str(excinfo.value)
        assert "cpu" in str(excinfo.value)
        assert "choke_limits" in str(excinfo.value)
    finally:
        case.case_detach()


def test_choke_throttle_limits_concurrent_cpu_steps(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver(choke_limits={"cpu": 1})
        cases = []
        for i in range(3):
            c = _make(ChokedBlockingCase, tmp_path, f"thr_{i}")
            c._gate = asyncio.Event()
            cases.append(c)
            driver.add(c)
            driver.boost(c.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert driver._in_flight_count == 1
        assert driver.snapshot()["chokes"]["cpu"]["in_use"] == 1
        for c in cases:
            c._gate.set()
        await driver.settle()
        assert driver.snapshot()["chokes"]["cpu"]["in_use"] == 0

    _run(body())


def test_choke_beat_quantization_retries_next_beat(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver(choke_limits={"cpu": 1})
        holder = _make(ChokedBlockingCase, tmp_path, "holder")
        waiter = _make(CpuChokedCase, tmp_path, "waiter")
        holder._gate = asyncio.Event()
        driver.add(holder)
        driver.add(waiter)
        driver.boost(holder.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        assert holder.case_state == "s0"
        assert driver._by_folder[holder.case_folder].in_flight

        driver.boost(waiter.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        waiter_slot = driver._by_folder[waiter.case_folder]
        assert not waiter_slot.in_flight
        assert waiter_slot.skip_countdown == 1
        assert waiter_slot.choked == frozenset({"cpu"})

        holder._gate.set()
        await driver.settle()
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert waiter.case_state == "s1"

    _run(body())


def test_fire_awaits_choke_permit(tmp_path):
    async def body():
        driver = BalancedCasePoolDriver(choke_limits={"cpu": 1})
        holder = _make(ChokedBlockingCase, tmp_path, "hold")
        waiter = _make(CpuChokedCase, tmp_path, "wait")
        holder._gate = asyncio.Event()
        driver.add(holder)
        driver.add(waiter)
        driver.boost(holder.case_folder)
        await driver.advance(suggested_interval_secs=0.0)

        fire_task = asyncio.create_task(driver.fire(waiter.case_folder, None))
        await asyncio.sleep(0.01)
        assert not fire_task.done()
        assert driver.snapshot()["fire_waiters"] == 1

        holder._gate.set()
        await driver.settle()
        result = await fire_task
        assert result.progressed
        assert driver.snapshot()["fire_waiters"] == 0

    _run(body())


def test_choke_contention_favors_longest_waiting_streak_over_dict_order(tmp_path):
    """Fairness is decided by ``choke_wait_streak``, not incidental sweep (dict) order:
    a slot admitted earlier but new to choke contention loses to one admitted later
    that's already been declined repeatedly."""
    async def body():
        driver = BalancedCasePoolDriver(choke_limits={"cpu": 1})
        holder = _make(ChokedBlockingCase, tmp_path, "streak_holder")
        early = _make(CpuChokedCase, tmp_path, "streak_early")   # dict-order first
        late = _make(CpuChokedCase, tmp_path, "streak_late")     # dict-order second
        holder._gate = asyncio.Event()
        driver.add(holder)
        driver.add(early)
        driver.add(late)
        # Park "early" and "late" out of the sweep's way until explicitly boosted,
        # so neither races into contention before this test intends it to.
        driver._by_folder[early.case_folder].skip_countdown = 1000
        driver._by_folder[late.case_folder].skip_countdown = 1000

        driver.boost(holder.case_folder)
        await driver.advance(suggested_interval_secs=0.0)   # holder in-flight, holds cpu
        assert driver._by_folder[holder.case_folder].in_flight

        # "late" gets declined twice while cpu is held, building up its streak.
        driver.boost(late.case_folder)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.advance(suggested_interval_secs=0.0)
        late_slot = driver._by_folder[late.case_folder]
        assert late_slot.choke_wait_streak == 2
        assert late.case_state == "s0"       # never got the permit yet
        assert driver._by_folder[early.case_folder].choke_wait_streak == 0

        # Now both are due in the same beat, cpu is freshly released, but only 1
        # permit exists — "early" holds dict-order precedence, "late" does not.
        driver.boost(early.case_folder)
        holder._gate.set()
        await driver.settle()
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()

        # "late" (higher streak) wins despite "early" holding dict-order precedence.
        assert late.case_state == "s1"
        assert early.case_state == "s0"
        assert driver._by_folder[late.case_folder].choke_wait_streak == 0
        assert driver._by_folder[early.case_folder].choke_wait_streak == 1

    _run(body())


# ---------------------------------------------------------------------------
# Beat pacing (fixed-rate with a yield floor)
# ---------------------------------------------------------------------------

class _FakeTime:
    """Stand-in for the driver module's ``time`` with a controllable clock."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _SleepRecorder:
    """Stand-in for the driver module's ``asyncio``: records ``sleep`` delays
    (returning immediately) and delegates everything else to the real module."""

    def __init__(self):
        self.calls: list[float] = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        self.calls.append(delay)


def _patch_pacing(monkeypatch):
    from totodev_pub.folder_backed_case_support import balanced_case_pool_driver as mod
    fake_time = _FakeTime()
    recorder = _SleepRecorder()
    monkeypatch.setattr(mod, "time", fake_time)
    monkeypatch.setattr(mod, "asyncio", recorder)
    return fake_time, recorder


def test_paced_beat_targets_period_and_subtracts_elapsed(monkeypatch):
    """The period is a target between beats: work time since the previous beat
    (including caller work between ``advance()`` calls) shrinks the sleep."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()             # I0 = 0.5
        await driver.advance()                        # first paced beat: full period
        assert recorder.calls == [pytest.approx(0.5)]
        fake_time.now += 0.3                          # 0.3s consumed since last beat
        await driver.advance()
        assert recorder.calls[-1] == pytest.approx(0.2)

    _run(body())


def test_paced_beat_overrun_sleeps_yield_floor(monkeypatch):
    """A beat that overruns its period still sleeps BEAT_YIELD_FLOOR — the loop
    always yields to in-flight tasks, never spins back-to-back sweeps."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()
        await driver.advance()
        fake_time.now += 2.0                          # way past the 0.5s period
        await driver.advance()
        assert recorder.calls[-1] == pytest.approx(driver._policy.BEAT_YIELD_FLOOR)

    _run(body())


def test_yield_floor_clamped_to_small_periods(monkeypatch):
    """A deliberately fast tempo (period below the floor) is not inflated: the
    effective floor is min(BEAT_YIELD_FLOOR, period)."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver(policy=_TierPolicy(I0=0.01))
        await driver.advance()
        fake_time.now += 5.0
        await driver.advance()
        assert recorder.calls[-1] == pytest.approx(0.01)

    _run(body())


def test_suggested_interval_is_the_target_period(monkeypatch):
    """An advisory interval replaces I0 as the fixed-rate target, with the same
    elapsed-time subtraction."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()
        await driver.advance(suggested_interval_secs=2.0)
        assert recorder.calls == [pytest.approx(2.0)]
        fake_time.now += 0.5
        await driver.advance(suggested_interval_secs=2.0)
        assert recorder.calls[-1] == pytest.approx(1.5)

    _run(body())


def test_eager_beat_after_a_sweep_that_launched(tmp_path, monkeypatch):
    """A sweep that launched at least one step paces at period * EAGER_BEAT_FRACTION;
    once nothing launches (pool gone quiet), pacing returns to the full period."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()             # I0=0.5, fraction=0.25
        case = _make(AutoCase, tmp_path, "eager")
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance()                        # launches step 1 → eager
        assert recorder.calls[-1] == pytest.approx(0.125)
        await driver.settle()                         # s0 → s1, reloads HOT countdown
        await driver.advance()                        # launches step 2 → still eager
        assert recorder.calls[-1] == pytest.approx(0.125)
        await driver.settle()                         # s1 → s2 (terminal)
        await driver.advance()                        # quiet sweep → full period
        assert recorder.calls[-1] == pytest.approx(0.5)
        case.case_detach()

    _run(body())


def test_eager_beat_fits_three_or_more_beats_in_one_nominal_interval(tmp_path, monkeypatch):
    """The concrete point of the mechanic: on a lightly loaded pool with a case
    that keeps progressing, 3+ beats land within the wall-clock span that a
    single non-eager I0 beat would have consumed, instead of just 1."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()             # I0=0.5, fraction=0.25
        case = _make(LongAutoCase, tmp_path, "eager_burst")
        driver.add(case)
        driver.boost(case.case_folder)

        launching_sleeps: list[float] = []
        while case.case_state != "s4":                # 4 auto edges to terminal
            await driver.advance()
            await driver.settle()
            launching_sleeps.append(recorder.calls[-1])

        assert len(launching_sleeps) >= 3              # 3+ beats actually launched
        assert all(s == pytest.approx(0.125) for s in launching_sleeps)
        # The first 3 of them alone already fit inside one nominal (non-eager) I0
        # beat — confirming the mechanic delivers 3+ beats per interval, not just 1.
        assert sum(launching_sleeps[:3]) < driver._policy.I0

        case.case_detach()

    _run(body())


def test_eager_beat_skipped_when_outside_eager_window(tmp_path, monkeypatch):
    """A launching sweep whose beat already consumed more than the eager window
    falls back to full-period pacing (congestion means no speed-up)."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()
        case = _make(AutoCase, tmp_path, "eager_window")
        driver.add(case)
        driver._by_folder[case.case_folder].skip_countdown = 1000  # park until boosted
        await driver.advance()                        # quiet paced beat: sets anchor
        fake_time.now += 0.3                          # past the 0.125s eager window
        driver.boost(case.case_folder)
        await driver.advance()                        # launches, but window exceeded
        assert recorder.calls[-1] == pytest.approx(0.2)   # full-period pacing: 0.5 - 0.3
        await driver.settle()
        case.case_detach()

    _run(body())


def test_eager_beat_disabled_by_policy(tmp_path, monkeypatch):
    """EAGER_BEAT_FRACTION=1.0 disables the eager tempo even for launching sweeps."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver(policy=_TierPolicy(EAGER_BEAT_FRACTION=1.0))
        case = _make(AutoCase, tmp_path, "eager_off")
        driver.add(case)
        driver.boost(case.case_folder)
        await driver.advance()                        # launches, but eager disabled
        assert recorder.calls[-1] == pytest.approx(0.5)
        await driver.settle()
        case.case_detach()

    _run(body())


def test_unpaced_beat_skips_sleep_and_resets_anchor(monkeypatch):
    """``suggested_interval_secs=0.0`` never sleeps, and clears the pacing anchor
    so a later paced beat sleeps its full period instead of seeing a huge stale
    elapsed window (which would wrongly collapse it to the floor)."""
    async def body():
        fake_time, recorder = _patch_pacing(monkeypatch)
        driver = BalancedCasePoolDriver()
        await driver.advance()                        # paced: sets the anchor
        fake_time.now += 60.0
        await driver.advance(suggested_interval_secs=0.0)
        assert len(recorder.calls) == 1               # no sleep for the unpaced beat
        await driver.advance()                        # paced again: fresh anchor
        assert recorder.calls[-1] == pytest.approx(0.5)

    _run(body())
