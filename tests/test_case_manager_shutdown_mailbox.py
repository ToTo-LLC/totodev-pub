# Part of the totodev_pub library.

import asyncio
import logging

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.shutdown import ShutdownRequest


@pytest.mark.asyncio
async def test_client_submit_shutdown_writes_structured_request(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_shutdown(graceful=True, reason="drain", only_if_fresh=False)
    intake = manager._mailbox.shutdown_intake()
    files = [p for p in intake.iterdir() if not p.name.startswith(".")]
    assert len(files) == 1
    req = ShutdownRequest.load(str(files[0]), acquire_lock=False)
    assert req.graceful is True
    assert req.reason == "drain"
    assert req.correlation_id == handle.correlation_id


@pytest.mark.asyncio
async def test_cooperative_pickup_invokes_registered_callback(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    seen = []
    manager.on_shutdown_request(seen.append)
    await manager.start()
    client = CaseManagerClient(tmp_path / "cache")
    client.submit_shutdown(graceful=False, reason="stop now", only_if_fresh=False)
    for _ in range(300):
        if seen:
            break
        await asyncio.sleep(0.02)
    await manager.stop()
    assert seen and seen[0].graceful is False and seen[0].reason == "stop now"
    # The request file was consumed at pickup.
    intake = manager._mailbox.shutdown_intake()
    assert not any(p for p in intake.iterdir() if not p.name.startswith("."))


@pytest.mark.asyncio
async def test_pickup_without_host_warns_and_discards(tmp_path, caplog):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    client = CaseManagerClient(tmp_path / "cache")
    client.submit_shutdown(only_if_fresh=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(300):
            if any("no host is registered" in r.getMessage() for r in caplog.records):
                break
            await asyncio.sleep(0.02)
    await manager.stop()
    assert any("no host is registered" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_manifest_advertises_shutdown_intake(tmp_path):
    from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
    from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME

    manager = provision_manager(tmp_path)
    await manager.recover()
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    assert manifest.paths.shutdown_mailbox_intake is not None
    assert "shutdown_mailbox" in manifest.paths.shutdown_mailbox_intake
