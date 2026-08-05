# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import provision_manager


@pytest.mark.asyncio
async def test_loop_failure_invokes_callback_after_three_consecutive(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    boom = RuntimeError("tick exploded")

    async def bad_tick():
        raise boom

    monkeypatch.setattr(manager, "_maintenance_tick", bad_tick)
    failures = []
    manager.on_loop_failure(failures.append)
    await manager.start()
    for _ in range(300):
        if failures:
            break
        await asyncio.sleep(0.02)
    assert failures == [boom]
    assert manager._run_task.done()  # loop returned after handing off
    await manager.stop()


@pytest.mark.asyncio
async def test_loop_failure_without_callback_reraises_loudly(tmp_path, monkeypatch, caplog):
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def bad_tick():
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(manager, "_maintenance_tick", bad_tick)
    await manager.start()
    for _ in range(300):
        if manager._run_task.done():
            break
        await asyncio.sleep(0.02)
    assert manager._run_task.done()
    assert isinstance(manager._run_task.exception(), RuntimeError)
    assert any("Manager loop iteration failed" in r.message for r in caplog.records)
    # A dead loop task must not abort a deliberate stop().
    await manager.stop()


@pytest.mark.asyncio
async def test_loop_recovers_from_transient_failures(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    calls = {"n": 0}
    original_tick = manager._maintenance_tick

    async def flaky_tick():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient")
        await original_tick()

    monkeypatch.setattr(manager, "_maintenance_tick", flaky_tick)
    failures = []
    manager.on_loop_failure(failures.append)
    await manager.start()
    for _ in range(300):
        if calls["n"] >= 5:
            break
        await asyncio.sleep(0.02)
    assert calls["n"] >= 5          # loop kept going past the two failures
    assert failures == []           # a successful iteration reset the count
    assert not manager._run_task.done()
    await manager.stop()
