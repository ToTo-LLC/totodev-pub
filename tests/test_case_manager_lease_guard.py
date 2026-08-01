# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""No relocation may move a case folder while its heartbeat lease is held.

Moving a folder out from under a live owner is a split-brain: the owner's open
handles follow the inode while every new path-based open fails. These tests pin
the refusal at each relocation site.
"""

from pathlib import Path

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.aberrant import move_case_to_aberrant
from totodev_pub.case_manager_support.adopt import adopt_case_folder
from totodev_pub.case_manager_support.exceptions import CaseLeaseHeldError
from totodev_pub.case_manager_support.layout import assert_case_folder_movable
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


def test_guard_allows_a_folder_with_no_lease_file(tmp_path):
    """is_heartbeat_expired() is tri-state; an absent lease must not block a move."""
    folder = tmp_path / "no-lease"
    folder.mkdir()
    assert_case_folder_movable(folder, "case-x", "quarantine")


def test_guard_allows_a_detached_case(tmp_path):
    folder = tmp_path / "detached"
    case = seed_detached_case(TicketCase, folder)
    assert_case_folder_movable(folder, case.case_id, "quarantine")


def test_guard_refuses_an_attached_case(tmp_path):
    folder = tmp_path / "attached"
    case = TicketCase.create_case_in_folder(folder)
    try:
        with pytest.raises(CaseLeaseHeldError) as excinfo:
            assert_case_folder_movable(folder, case.case_id, "quarantine")
        assert excinfo.value.case_id == case.case_id
        assert excinfo.value.operation == "quarantine"
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_quarantine_refuses_while_the_lease_is_held(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)  # adopt leaves the case attached

    try:
        with pytest.raises(CaseLeaseHeldError):
            await move_case_to_aberrant(
                manager._cache, manager._policy, case.case_id, case.case_folder, "boom"
            )
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_quarantine_proceeds_once_the_lease_is_released(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound2"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    folder = case.case_folder
    case.case_detach()

    dest = await move_case_to_aberrant(
        manager._cache, manager._policy, case.case_id, folder, "boom"
    )
    assert dest.exists()


@pytest.mark.asyncio
async def test_adopt_failure_path_releases_the_lease_before_quarantine(tmp_path):
    """rehydrate() takes the lease; a failure after that must not deadlock quarantine.

    Regression guard for the interaction between the lease precondition and
    adopt's own error handler: the handler quarantines the half-adopted case,
    and quarantine legitimately refuses to relocate a leased folder.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)

    def rejecting_add(case):
        raise RuntimeError("driver said no")

    result = await adopt_case_folder(
        staging,
        cache=manager._cache,
        policy=manager._policy,
        manager_dir=manager._manager_dir,
        registry=manager._registry,
        driver_add=rejecting_add,
        case_id_exists=lambda cid: False,
        move_to_aberrant=manager._move_to_aberrant,
    )

    assert result.status == "error"
    assert "driver said no" in result.rejection_reason, "the ORIGINAL failure is reported"
    assert result.aberrant_folder is not None, "the case was quarantined, not stranded"
    assert Path(result.aberrant_folder).exists()
