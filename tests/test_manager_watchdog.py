# Part of the totodev_pub library.

import asyncio
import os
import time

import pytest

from case_manager_test_utils import provision_manager
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
    manager.on_escalation(escalations.append)
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
    manager.on_escalation(escalations.append)
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
