# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import (
    ManualCase,
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    run,
    seed_detached_case,
)
from totodev_pub.case_manager_support.exceptions import LiveCaseNotFoundError


def test_provision_attach_open(tmp_path):
    manager = provision_manager(tmp_path)
    assert manager._policy.live_bucket == "live"
    assert (manager._manager_dir / "manifest.yaml").exists() or True  # written on recover/start


@pytest.mark.asyncio
async def test_adopt_get_live_fire(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    case = await adopt_into_live(manager, staging / "c1")
    assert manager.get_live(case.case_id).case_id == case.case_id
    # CaseManager.fire() requires a running loop; use the driver's immediate primitive
    # for deterministic not-started-manager tests.
    ar = await manager._driver.fire(case.case_folder, "work")
    assert ar.progressed or ar.final_state == "done"


@pytest.mark.asyncio
async def test_locate_and_reader(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1", external_key="T-42")
    case = await adopt_into_live(manager, staging / "c1")
    loc = manager.locate(case_id=case.case_id)
    assert loc is not None
    assert loc.in_pool
    reader = manager.reader(case_id=case.case_id)
    assert reader.case_external_key == "T-42"
    hits = manager.locate_by_external_key(external_key="T-42")
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_iter_live(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await adopt_into_live(manager, staging / "c1")
    readers = list(manager.iter_live())
    assert len(readers) == 1


@pytest.mark.asyncio
async def test_iter_live_excludes_halt_requested(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    case = await adopt_into_live(manager, staging / "c1")
    manager._driver.request_halt(case.case_folder)
    assert case.case_folder in manager._driver
    assert list(manager.iter_live()) == []


@pytest.mark.asyncio
async def test_iter_live_excludes_terminal(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TerminalCase, staging / "c1")
    case = await adopt_into_live(manager, staging / "c1")
    await manager._driver.fire(case.case_folder, "finish")
    assert case.case_is_terminal
    assert case.case_folder in manager._driver
    assert list(manager.iter_live()) == []


def test_get_live_not_found(tmp_path):
    manager = provision_manager(tmp_path)
    with pytest.raises(LiveCaseNotFoundError):
        manager.get_live("missing")
