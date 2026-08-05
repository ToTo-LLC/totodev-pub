# Part of the totodev_pub library.

from datetime import datetime, timedelta, timezone

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.cli.manager_health import main


def test_missing_manifest_exits_3(tmp_path):
    assert main([str(tmp_path / "nowhere")]) == 3


def test_unreadable_manifest_exits_3(tmp_path):
    mgr_dir = tmp_path / "cache" / ".case_manager"
    mgr_dir.mkdir(parents=True)
    (mgr_dir / "manifest.yaml").write_text(":::not yaml{{", encoding="utf-8")
    assert main([str(tmp_path / "cache")]) == 3


@pytest.mark.asyncio
async def test_fresh_manager_exits_0(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    try:
        assert main([str(tmp_path / "cache")]) == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stopped_manager_exits_2(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    await manager.stop()   # writes stopped_at
    assert main([str(tmp_path / "cache")]) == 2


def test_stale_heartbeat_exits_1(tmp_path):
    mgr_dir = tmp_path / "cache" / ".case_manager"
    mgr_dir.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (mgr_dir / "manifest.yaml").write_text(
        f"heartbeat_at: '{old}'\nmanifest_stale_secs: 30\n", encoding="utf-8"
    )
    assert main([str(tmp_path / "cache")]) == 1
