# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import transport_for,  provision_manager
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.shutdown import ShutdownRequest


@pytest.mark.asyncio
async def test_client_submit_shutdown_writes_structured_request(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_shutdown(graceful=True, reason="drain", only_if_fresh=False)
    intake = transport_for(manager).shutdown_intake()
    files = [p for p in intake.iterdir() if not p.name.startswith(".")]
    assert len(files) == 1
    req = ShutdownRequest.load(str(files[0]), acquire_lock=False)
    assert req.graceful is True
    assert req.reason == "drain"
    assert req.correlation_id == handle.correlation_id


def test_the_manager_has_no_shutdown_surface(tmp_path):
    """Shutdown is process control, so the fleet has no say in it.

    A manager that nobody hosts cannot promise process-exit semantics, and used
    to warn about exactly that at pickup time. Now it simply never sees the
    request: the host polls the intake, always, and a manager without a host has
    no pickup path to misuse. Pickup itself is covered end-to-end in
    ``test_case_manager_host_serve.py``.
    """
    manager = provision_manager(tmp_path)
    assert not hasattr(manager, "on_shutdown_request")
    assert not hasattr(manager, "_notify_shutdown_request")


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
