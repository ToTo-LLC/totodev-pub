# Part of the totodev_pub library.

import logging

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.shutdown import shutdown_intake_dir, write_shutdown_request
from totodev_pub.case_manager_support.watchdog import write_death_record


@pytest.mark.asyncio
async def test_recover_surfaces_recent_death_records(tmp_path, caplog):
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()
    write_death_record(manager._manager_dir, check="pulse_stuck", reason="wedged hook")
    with caplog.at_level(logging.WARNING):
        report = await manager.recover()
    assert report.death_records_recent == 1
    assert any("pulse_stuck" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_recover_discards_stale_shutdown_requests(tmp_path, caplog):
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()
    intake = shutdown_intake_dir(manager._manager_dir, manager._policy)
    write_shutdown_request(intake, graceful=False, reason="from a dead process")
    with caplog.at_level(logging.WARNING):
        report = await manager.recover()
    assert report.shutdown_requests_discarded == 1
    assert not any(p for p in intake.iterdir() if not p.name.startswith("."))
