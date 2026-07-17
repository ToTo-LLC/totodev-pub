# Part of the totodev_pub library.

import asyncio
import os
import signal
import threading

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub import case_manager_host
from totodev_pub.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_host import EXIT_RESTART_REQUESTED, EXIT_WATCHDOG, serve
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME


class _HardExit(BaseException):
    def __init__(self, code):
        self.code = code


@pytest.fixture
def hard_exit_recorder(monkeypatch):
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise _HardExit(code)

    monkeypatch.setattr(case_manager_host, "_hard_exit", fake_exit)
    return codes


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
