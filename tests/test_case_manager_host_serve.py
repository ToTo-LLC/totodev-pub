# Part of the totodev_pub library.

import asyncio
import os
import signal
import threading

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support import case_manager_host
from totodev_pub.case_manager_support.case_manager_host import (
    EXIT_RESTART_REQUESTED,
    EXIT_WATCHDOG,
    serve,
)
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME


class _HardExit(BaseException):
    def __init__(self, code):
        self.code = code


@pytest.fixture
def hard_exit_recorder(monkeypatch):
    """Raising recorder. Correct only where _hard_exit runs on serve()'s own frame.

    Use hard_exit_codes instead when the exit comes from the watchdog daemon
    thread or from inside _manager_loop's except-block: a raise there lands
    somewhere pytest.raises around the serve task cannot see.
    """
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise _HardExit(code)

    monkeypatch.setattr(case_manager_host, "_hard_exit", fake_exit)
    return codes


@pytest.fixture
def hard_exit_codes(monkeypatch):
    """Non-raising recorder, for exits reached off serve()'s frame."""
    codes = []
    monkeypatch.setattr(case_manager_host, "_hard_exit", codes.append)
    return codes


@pytest.fixture(autouse=True)
def watchdogs(monkeypatch):
    """Record every watchdog serve() builds, and join them on teardown.

    Any serve() that ends via _hard_exit skips watchdog.stop(), leaking a live
    daemon thread into the rest of the session — which makes
    test_serve_respects_watchdog_enabled_false's "no manager-watchdog thread"
    assertion depend on declaration order. Joining here keeps that honest. The
    recorded list is also how a test asserts the watchdog was not silently
    downgraded to alarm_only by a coverage tracer.
    """
    built = []
    real = case_manager_host.ManagerWatchdog

    def factory(*args, **kwargs):
        dog = real(*args, **kwargs)
        built.append(dog)
        return dog

    monkeypatch.setattr(case_manager_host, "ManagerWatchdog", factory)
    yield built
    for dog in built:
        dog.stop()


@pytest.fixture
def no_tracer_downgrade(monkeypatch):
    """Force the watchdog to honour watchdog_action="exit" under a coverage tracer."""
    monkeypatch.setattr(case_manager_host, "_tracer_downgrade", lambda policy: None)


def _stopped_at(manager):
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    return manifest.stopped_at


@pytest.mark.asyncio
async def test_serve_rejects_prelifecycled_manager(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    with pytest.raises(ValueError):
        await serve(manager)


@pytest.mark.asyncio
async def test_serve_rejects_both_stop_when_and_stop_when_empty(tmp_path):
    manager = provision_manager(tmp_path)
    with pytest.raises(ValueError):
        await serve(manager, stop_when=lambda: True, stop_when_empty=True)


@pytest.mark.asyncio
async def test_sigterm_is_a_clean_exit_zero_stop(tmp_path):
    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.is_running:
            break
        await asyncio.sleep(0.02)
    assert manager.is_running
    assert any(t.name == "manager-watchdog" for t in threading.enumerate())
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10.0)   # returns None → process exit 0
    assert manager.is_running is False
    assert _stopped_at(manager) is not None


@pytest.mark.asyncio
async def test_serve_respects_watchdog_enabled_false(tmp_path):
    manager = provision_manager(tmp_path, watchdog_enabled=False)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.is_running:
            break
        await asyncio.sleep(0.02)
    assert not any(t.name == "manager-watchdog" for t in threading.enumerate())
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10.0)


@pytest.mark.asyncio
async def test_graceful_mailbox_shutdown_acks_then_exits_75(tmp_path, hard_exit_recorder):
    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.is_running:
            break
        await asyncio.sleep(0.02)
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_shutdown(graceful=True, reason="drain", only_if_fresh=False)
    with pytest.raises(_HardExit) as excinfo:
        await asyncio.wait_for(task, timeout=10.0)
    assert excinfo.value.code == EXIT_RESTART_REQUESTED
    assert hard_exit_recorder == [EXIT_RESTART_REQUESTED]
    ack = client.poll_result(handle)             # ack written before the drain
    assert ack is not None and ack.kind == "shutdown"


@pytest.mark.asyncio
async def test_immediate_mailbox_shutdown_exits_75_and_teaches(tmp_path, hard_exit_recorder, caplog):
    import logging

    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.is_running:
            break
        await asyncio.sleep(0.02)
    client = CaseManagerClient(tmp_path / "cache")
    with caplog.at_level(logging.WARNING):
        client.submit_shutdown(only_if_fresh=False)  # default: immediate
        with pytest.raises(_HardExit) as excinfo:
            await asyncio.wait_for(task, timeout=10.0)
    assert excinfo.value.code == EXIT_RESTART_REQUESTED
    assert _stopped_at(manager) is not None      # the one refinement over the hard path
    assert any("Immediate shutdown triggered" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stop_when_empty_self_completes_exit_zero(tmp_path):
    manager = provision_manager(tmp_path)
    # Empty manager: is_idle is True immediately, so serve() should self-stop.
    await asyncio.wait_for(
        serve(manager, stop_grace_secs=5.0, stop_when_empty=True), timeout=10.0
    )
    assert manager.is_running is False
    assert _stopped_at(manager) is not None      # clean stopped_at, like a signal stop


@pytest.mark.asyncio
async def test_stop_when_custom_predicate(tmp_path):
    manager = provision_manager(tmp_path)
    polls = {"n": 0}

    def done_after_five():
        polls["n"] += 1
        return polls["n"] >= 5

    await asyncio.wait_for(
        serve(manager, stop_grace_secs=5.0, stop_when=done_after_five), timeout=10.0
    )
    assert polls["n"] >= 5
    assert manager.is_running is False


# ---------------------------------------------------------------------------
# §1 promotion blockers: loop failure reaches an exit code, both watchdog modes
# ---------------------------------------------------------------------------


async def _poll(predicate, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def _death_records(manager):
    return sorted(manager._manager_dir.glob("manager_death_*.yaml"))


def _failing_tick(manager, monkeypatch):
    async def bad_tick():
        raise RuntimeError("boom")

    monkeypatch.setattr(manager, "_maintenance_tick", bad_tick)


async def _serve_until_exit(manager, codes, timeout=20.0):
    """serve()'s stop_event never fires on these paths: poll, then cancel."""
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=2.0))
    try:
        got = await _poll(lambda: bool(codes), timeout=timeout)
        assert got, f"no exit code recorded within {timeout}s"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, _HardExit):
            pass


def test_tracer_downgrade_is_the_only_thing_that_silences_the_watchdog(tmp_path, monkeypatch):
    policy = provision_manager(tmp_path)._policy
    policy.watchdog_action = "exit"

    monkeypatch.setattr(case_manager_host.sys, "gettrace", lambda: None)
    assert case_manager_host._tracer_downgrade(policy) is None

    monkeypatch.setattr(case_manager_host.sys, "gettrace", lambda: object())
    assert case_manager_host._tracer_downgrade(policy) == "alarm_only"

    policy.watchdog_action = "alarm_only"
    assert case_manager_host._tracer_downgrade(policy) is None


@pytest.mark.asyncio
async def test_three_loop_failures_exit_70_via_the_watchdog(
    tmp_path, monkeypatch, hard_exit_codes, no_tracer_downgrade, watchdogs
):
    """The highest-risk gap: loop failure -> ladder -> process exit 70."""
    manager = provision_manager(tmp_path)
    escalations = []
    manager.on_notice(escalations.append)
    _failing_tick(manager, monkeypatch)  # before serve(); recover() never calls it

    await _serve_until_exit(manager, hard_exit_codes)

    assert hard_exit_codes == [EXIT_WATCHDOG]
    assert watchdogs and watchdogs[0]._action == "exit", "watchdog was silently downgraded"
    records = _death_records(manager)
    assert len(records) == 1
    assert "loop_failure" in records[0].read_text(encoding="utf-8")
    assert escalations, "the ladder must escalate before it kills"


@pytest.mark.asyncio
async def test_three_loop_failures_exit_70_without_a_watchdog(
    tmp_path, monkeypatch, hard_exit_codes, watchdogs
):
    """watchdog_enabled=False must still fail loud rather than idle forever."""
    manager = provision_manager(tmp_path, watchdog_enabled=False)
    escalations = []
    manager.on_notice(escalations.append)
    _failing_tick(manager, monkeypatch)

    await _serve_until_exit(manager, hard_exit_codes)

    assert hard_exit_codes == [EXIT_WATCHDOG]
    assert watchdogs == [], "no watchdog should have been constructed"
    records = _death_records(manager)
    assert len(records) == 1
    assert "loop_failure" in records[0].read_text(encoding="utf-8")
    assert any("MANAGER_UNRESPONSIVE" in str(e.kind) for e in escalations)


@pytest.mark.asyncio
async def test_stop_that_will_not_settle_exits_70(
    tmp_path, monkeypatch, hard_exit_recorder, watchdogs
):
    """A deliberate stop that times out is a liveness failure, not a clean exit."""
    from totodev_pub.case_manager_support.exceptions import CaseManagerStopTimeoutError

    manager = provision_manager(tmp_path)

    async def timing_out_stop(*, timeout=None):
        raise CaseManagerStopTimeoutError(
            timeout_secs=timeout or 0.0, stuck=[], detail={}
        )

    monkeypatch.setattr(manager, "stop", timing_out_stop)

    with pytest.raises(_HardExit) as excinfo:
        await asyncio.wait_for(
            serve(manager, stop_grace_secs=0.5, stop_when=lambda: True), timeout=20.0
        )
    assert excinfo.value.code == EXIT_WATCHDOG
    assert hard_exit_recorder == [EXIT_WATCHDOG]
    # Host path, not the watchdog ladder: no death record is written.
    assert _death_records(manager) == []


@pytest.mark.asyncio
async def test_shutdown_mailbox_is_polled_with_mailbox_and_watchdog_both_off(
    tmp_path, hard_exit_recorder, watchdogs
):
    """The dead combination: nothing used to poll the shutdown mailbox at all."""
    from totodev_pub.case_manager_support.shutdown import (
        shutdown_intake_dir,
        write_shutdown_request,
    )

    manager = provision_manager(tmp_path, enable_mailbox=False, watchdog_enabled=False)
    intake = shutdown_intake_dir(manager._manager_dir, manager._policy)

    task = asyncio.ensure_future(serve(manager, stop_grace_secs=2.0))
    assert await _poll(lambda: manager.is_running)
    # Only now: recover() deliberately discards requests predating this run.
    write_shutdown_request(intake, graceful=True, reason="drain")

    with pytest.raises(_HardExit) as excinfo:
        await asyncio.wait_for(task, timeout=20.0)

    assert excinfo.value.code == EXIT_RESTART_REQUESTED
    assert not list(intake.glob("*.yaml")), "the request must be consumed"
