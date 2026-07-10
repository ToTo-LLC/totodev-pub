# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME


def _heartbeat(manager) -> str | None:
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    return manifest.heartbeat_at


@pytest.mark.asyncio
async def test_pulse_survives_a_blocked_tick(tmp_path, monkeypatch):
    # Speed the pulse up so the test stays fast.
    monkeypatch.setattr("totodev_pub.case_manager.PULSE_INTERVAL_SECS", 0.02)
    seq = iter(range(1, 100))

    def fake_utc_now_iso() -> str:
        n = next(seq)
        return f"2026-01-01T00:00:{n:02d}Z"

    monkeypatch.setattr(
        CaseManagerManifest,
        "utc_now_iso",
        staticmethod(fake_utc_now_iso),
    )
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def stuck_tick():
        await asyncio.sleep(3600)  # a legitimately long awaited tick

    monkeypatch.setattr(manager, "_maintenance_tick", stuck_tick)
    await manager.start()
    await asyncio.sleep(0.1)
    pulse_1, hb_1 = manager._last_pulse, _heartbeat(manager)
    await asyncio.sleep(0.15)
    pulse_2, hb_2 = manager._last_pulse, _heartbeat(manager)
    assert pulse_2 > pulse_1                     # loop turning under the stuck tick
    assert hb_1 is not None and hb_2 is not None # heartbeat decoupled from tick duration
    assert hb_2 != hb_1                          # pulse advances heartbeat on its own cadence
    await manager.stop()
    assert manager._pulse_task is None


@pytest.mark.asyncio
async def test_stop_manifest_not_overwritten_by_pulse(tmp_path, monkeypatch):
    monkeypatch.setattr("totodev_pub.case_manager.PULSE_INTERVAL_SECS", 0.02)
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    await asyncio.sleep(0.05)
    await manager.stop()
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    assert manifest.stopped_at is not None
    assert manifest.heartbeat_at is None  # stopped manifest, not running=True overwrite


@pytest.mark.asyncio
async def test_tick_stamps_recorded(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    assert manager._last_tick_started is None
    await manager.start()
    for _ in range(200):
        if manager._last_tick_completed is not None:
            break
        await asyncio.sleep(0.02)
    assert manager._last_tick_started is not None
    assert manager._last_tick_completed is not None
    assert manager._last_tick_completed >= manager._last_tick_started
    await manager.stop()


def test_fossil_last_maintenance_removed(tmp_path):
    manager = provision_manager(tmp_path)
    assert not hasattr(manager, "_last_maintenance")
