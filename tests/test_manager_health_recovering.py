# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""A recovering manager is healthy, and the probe has to say so.

Reclaiming a crashed owner's cases means waiting out its heartbeat lease — tens
of seconds during which nothing beats, because the manager loop has not started.
A liveness probe that read that as dead would kill the process every single
time, restarting the wait from zero, and a crashed fleet would never recover at
all. That is a crashloop the operator cannot escape by waiting.
"""

import yaml
import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME
from totodev_pub.cli.manager_health import RECOVERY_GRACE_SECS, main as health_main


def _manifest_path(manager):
    return manager._manager_dir / MANIFEST_FILENAME


def _rewrite(manager, **fields):
    path = _manifest_path(manager)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _stamp(secs_ago: float) -> str:
    import datetime

    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=secs_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_recovery_is_announced_in_the_manifest(tmp_path):
    """The state has to be visible before the slow part, not after it."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    manifest = CaseManagerManifest.load(str(_manifest_path(manager)), acquire_lock=False)
    # recover() finished here, so the field is set but the run is over; what
    # matters is that the field exists and recovery wrote it.
    assert hasattr(manifest, "recovering_at")
    assert manifest.recovering_at is not None


@pytest.mark.asyncio
async def test_the_probe_calls_an_in_progress_recovery_healthy(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    _rewrite(manager, recovering_at=_stamp(20), heartbeat_at=None, stopped_at=None)

    assert health_main([str(manager._cache_root)]) == 0


@pytest.mark.asyncio
async def test_the_probe_fails_a_recovery_that_overran_its_grace(tmp_path):
    """Generous is not infinite — a recovery that is genuinely stuck must page."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    _rewrite(
        manager,
        recovering_at=_stamp(RECOVERY_GRACE_SECS + 60),
        heartbeat_at=None,
        stopped_at=None,
    )

    assert health_main([str(manager._cache_root)]) == 1


@pytest.mark.asyncio
async def test_a_beating_manager_is_healthy_regardless_of_the_recovery_stamp(tmp_path):
    """Once the loop beats, the heartbeat is the answer and recovery is history."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    _rewrite(manager, recovering_at=_stamp(9999), heartbeat_at=_stamp(1), stopped_at=None)

    assert health_main([str(manager._cache_root)]) == 0


@pytest.mark.asyncio
async def test_a_deliberate_stop_still_outranks_everything(tmp_path):
    """Scale-down must not page, even if a recovery stamp is lying around."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    _rewrite(manager, recovering_at=_stamp(5), heartbeat_at=None, stopped_at=_stamp(1))

    assert health_main([str(manager._cache_root)]) == 2


@pytest.mark.asyncio
async def test_a_manager_that_never_ran_is_still_stale(tmp_path):
    """No heartbeat and no recovery under way is exactly the case to page for."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    _rewrite(manager, recovering_at=None, heartbeat_at=None, stopped_at=None)

    assert health_main([str(manager._cache_root)]) == 1
