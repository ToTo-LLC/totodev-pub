# Part of the totodev_pub library.

import asyncio
import os
import time

import pytest

from case_manager_test_utils import transport_for,  provision_manager
from totodev_pub.case_manager_support.shutdown import shutdown_intake_dir, write_shutdown_request
from totodev_pub.case_manager_support.watchdog import (
    ManagerWatchdog,
    log_recent_death_records,
    write_death_record,
)


class ExitRecorder:
    def __init__(self):
        self.codes = []

    def __call__(self, code):
        self.codes.append(code)


def _make_watchdog(manager, loop, exit_fn, **kwargs):
    kwargs.setdefault("check_interval_secs", 0.02)
    kwargs.setdefault("stop_grace_secs", 1.0)
    return ManagerWatchdog(manager, loop=loop, exit_code=70, exit_fn=exit_fn, **kwargs)


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_pulse_stuck_triggers_kill_ladder(tmp_path):
    manager = provision_manager(tmp_path, watchdog_pulse_stuck_secs=0.1)
    await manager.recover()
    escalations = []
    manager.subscribe_notices(escalations.append)
    # Simulate a running manager whose pulse went silent long ago.
    manager._running = True
    manager._last_pulse = time.monotonic() - 99.0
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog._armed_at = time.monotonic() - 99.0  # skip the arm-time grace for the test
    assert await _wait_for(lambda: exit_fn.codes)
    dog.stop()
    manager._running = False
    assert exit_fn.codes == [70]
    death_records = list(manager._manager_dir.glob("manager_death_*.yaml"))
    assert len(death_records) == 1
    body = death_records[0].read_text(encoding="utf-8")
    assert "check: pulse_stuck" in body
    assert escalations and escalations[0].kind.value == "MANAGER_UNRESPONSIVE"


@pytest.mark.asyncio
async def test_loop_task_death_detected(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def doomed():
        raise RuntimeError("dead loop")

    manager._running = True
    manager._last_pulse = time.monotonic()  # pulse looks healthy

    task = asyncio.get_running_loop().create_task(doomed())
    await asyncio.sleep(0)  # let it die
    await asyncio.sleep(0.05)
    manager._run_task = task
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    # Keep the fake pulse fresh so only the task-death check can fire.
    ok = False
    for _ in range(200):
        manager._last_pulse = time.monotonic()
        if exit_fn.codes:
            ok = True
            break
        await asyncio.sleep(0.02)
    dog.stop()
    manager._running = False
    manager._run_task = None
    assert ok and exit_fn.codes == [70]


@pytest.mark.asyncio
async def test_shutdown_request_pickup_wins_and_parks(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    manager._running = True
    picked = []
    exit_fn = ExitRecorder()
    dog = _make_watchdog(
        manager,
        asyncio.get_running_loop(),
        exit_fn,
        on_shutdown_request=picked.append,
    )
    intake = shutdown_intake_dir(manager._manager_dir, manager._policy)
    write_shutdown_request(intake, graceful=True, reason="test")
    dog.arm()
    ok = False
    for _ in range(200):
        manager._last_pulse = time.monotonic()
        if picked:
            ok = True
            break
        await asyncio.sleep(0.02)
    dog.stop()
    manager._running = False
    assert ok
    assert picked[0].graceful is True
    assert exit_fn.codes == []          # a shutdown request is not a failure
    assert dog._parked.is_set()         # choreography owns the process now
    assert not any(p for p in intake.iterdir() if not p.name.startswith("."))


@pytest.mark.asyncio
async def test_parked_watchdog_never_fires(tmp_path):
    manager = provision_manager(tmp_path, watchdog_pulse_stuck_secs=0.05)
    await manager.recover()
    manager._running = True
    manager._last_pulse = time.monotonic() - 99.0
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog.park()
    await asyncio.sleep(0.3)
    dog.stop()
    manager._running = False
    assert exit_fn.codes == []


@pytest.mark.asyncio
async def test_alarm_only_diagnoses_without_dying(tmp_path):
    manager = provision_manager(
        tmp_path, watchdog_action="alarm_only", watchdog_pulse_stuck_secs=0.05
    )
    await manager.recover()
    escalations = []
    manager.subscribe_notices(escalations.append)
    manager._running = True
    manager._last_pulse = time.monotonic() - 99.0
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog._armed_at = time.monotonic() - 99.0
    assert await _wait_for(lambda: dog._parked.is_set())
    dog.stop()
    manager._running = False
    assert exit_fn.codes == []
    assert not list(manager._manager_dir.glob("manager_death_*.yaml"))
    assert escalations  # diagnosed loudly, did not die


def test_death_record_roundtrip(tmp_path, caplog):
    path = write_death_record(tmp_path, check="pulse_stuck", reason="test wedge")
    assert path.name.startswith("manager_death_")
    assert path.name.endswith(".yaml")
    assert "check: pulse_stuck" in path.read_text(encoding="utf-8")
    import logging

    with caplog.at_level(logging.WARNING):
        count = log_recent_death_records(tmp_path)
    assert count == 1
    assert any("pulse_stuck" in r.getMessage() for r in caplog.records)
    # Old records fall outside the 24h window.
    old = time.time() - 90000
    os.utime(path, (old, old))
    assert log_recent_death_records(tmp_path) == 0


# ---------------------------------------------------------------------------
# §1 coverage gaps: tick_slow (alarm only, always) and mailbox_neglect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["exit", "alarm_only"])
async def test_tick_slow_alarms_but_never_kills(tmp_path, action):
    """tick_slow is alarm-only unconditionally -- watchdog_action is irrelevant.

    Parametrizing over both actions is the assertion: a slow tick is a symptom,
    not a wedge, so it must never reach the ladder even when the policy says
    "exit".
    """
    manager = provision_manager(
        tmp_path, watchdog_tick_warn_secs=0.05, watchdog_action=action
    )
    await manager.recover()
    escalations = []
    manager.subscribe_notices(escalations.append)
    manager._running = True
    manager._run_task = None
    started = time.monotonic() - 5.0
    manager._last_tick_started = started
    manager._last_tick_completed = None

    manager._last_pulse = time.monotonic()
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    try:
        # Keep the pulse fresh so only the tick check can fire.
        async def _fresh_pulse_until(predicate, timeout=5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                manager._last_pulse = time.monotonic()
                if predicate():
                    return True
                await asyncio.sleep(0.02)
            return False

        assert await _fresh_pulse_until(lambda: bool(escalations))
        assert exit_fn.codes == []
        assert not list(manager._manager_dir.glob("manager_death_*.yaml"))
        # The tick check returns without parking, unlike the ladder's
        # alarm_only branch -- that distinction is what "alarm only" means here.
        assert not dog._parked.is_set()

        # De-dup: the same tick stamp must not re-alarm.
        first = len(escalations)
        await _fresh_pulse_until(lambda: False, timeout=0.3)
        assert len(escalations) == first
        assert dog._tick_warned_for == started

        # A NEW slow tick alarms again -- the de-dup keys on the stamp, it does
        # not latch permanently.
        manager._last_tick_started = time.monotonic() - 5.0
        assert await _fresh_pulse_until(lambda: len(escalations) > first)
    finally:
        dog.stop()
        manager._running = False


@pytest.mark.asyncio
async def test_mailbox_neglect_kills_when_intake_goes_unserved(tmp_path):
    manager = provision_manager(tmp_path, watchdog_mailbox_stale_secs=0.2)
    await manager.recover()
    escalations = []
    manager.subscribe_notices(escalations.append)

    transport = transport_for(manager)
    intake = transport.fire_intake()
    intake.mkdir(parents=True, exist_ok=True)
    stale = intake / "stale.yaml"
    stale.write_text("correlation_id: x\n", encoding="utf-8")
    # Intake age uses wall clock; the arm-window gate uses monotonic. Both need
    # back-dating, by different mechanisms.
    old = time.time() - 600
    os.utime(stale, (old, old))

    manager._running = True
    manager._run_task = None
    manager._last_pulse = time.monotonic()
    exit_fn = ExitRecorder()
    dog = _make_watchdog(
        manager,
        asyncio.get_running_loop(),
        exit_fn,
        oldest_request_age=transport.oldest_request_age_secs,
    )
    dog.arm()
    dog._armed_at = time.monotonic() - 99.0
    try:
        async def _fresh_pulse_until(predicate, timeout=5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                manager._last_pulse = time.monotonic()
                if predicate():
                    return True
                await asyncio.sleep(0.02)
            return False

        assert await _fresh_pulse_until(lambda: bool(exit_fn.codes))
        assert exit_fn.codes == [70]
        records = list(manager._manager_dir.glob("manager_death_*.yaml"))
        assert len(records) == 1
        # Naming the detection catches a mis-fire: a stale pulse would write
        # check: pulse_stuck and this would fail rather than pass by accident.
        assert "mailbox_neglect" in records[0].read_text(encoding="utf-8")
        assert escalations
        assert stale.exists(), "the watchdog observes intake, it never consumes it"
    finally:
        dog.stop()
        manager._running = False


@pytest.mark.asyncio
async def test_fresh_intake_does_not_alarm(tmp_path):
    # Threshold must exceed the observation window below: no manager loop is
    # draining this intake, so any backlog ages in real time and would cross a
    # short threshold on its own.
    manager = provision_manager(tmp_path, watchdog_mailbox_stale_secs=5.0)
    await manager.recover()
    intake = transport_for(manager).fire_intake()
    intake.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        (intake / f"req{i}.yaml").write_text("correlation_id: x\n", encoding="utf-8")

    manager._running = True
    manager._run_task = None
    manager._last_pulse = time.monotonic()
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog._armed_at = time.monotonic() - 99.0
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            manager._last_pulse = time.monotonic()
            await asyncio.sleep(0.02)
        assert exit_fn.codes == []
    finally:
        dog.stop()
        manager._running = False


def test_mailbox_neglect_disabled_by_config(tmp_path):
    """Two independent ways to switch the check off, both pure construction.

    The second is now structural rather than a flag: with no request transport
    attached, nothing owns an intake backlog, so there is no such thing as
    neglecting one.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    loop = asyncio.new_event_loop()
    try:
        zeroed = provision_manager(tmp_path / "a", watchdog_mailbox_stale_secs=0)
        dog = _make_watchdog(
            zeroed, loop, ExitRecorder(), oldest_request_age=lambda: 999.0
        )
        assert dog._mailbox_stale_secs is None

        no_adapter = provision_manager(tmp_path / "b")
        assert _make_watchdog(no_adapter, loop, ExitRecorder())._mailbox_stale_secs is None
    finally:
        loop.close()
