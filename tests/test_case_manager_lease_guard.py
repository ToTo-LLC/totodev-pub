# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""No relocation may move a case folder while its heartbeat lease is held.

Moving a folder out from under a live owner is a split-brain: the owner's open
handles follow the inode while every new path-based open fails. The precondition
itself is tested here; that every relocation *enforces* it is tested at the store
boundary (``test_case_store.py``), which is now the single place any relocation
goes through.
"""

from pathlib import Path

import pytest

from case_manager_test_utils import (
    TicketCase,
    provision_manager,
    seed_detached_case,
)
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
async def test_adopt_failure_path_releases_the_lease_before_quarantine(tmp_path):
    """rehydrate() takes the lease; a failure after that must not defer forever.

    Regression guard for the interaction between the lease precondition and
    adopt's own error handler: the handler quarantines the half-adopted case, and
    quarantine legitimately refuses to relocate a leased folder. Without the
    detach the quarantine would defer behind a lease nobody is left to drop.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)

    def rejecting_add(case):
        raise RuntimeError("driver said no")

    result = await adopt_case_folder(
        staging,
        store=manager._store,
        policy=manager._policy,
        manager_dir=manager._manager_dir,
        registry=manager._registry,
        driver_add=rejecting_add,
        case_id_exists=lambda cid: False,
        quarantine=manager._quarantine,
    )

    assert result.status == "error"
    assert "driver said no" in result.rejection_reason, "the ORIGINAL failure is reported"
    assert result.quarantine_folder is not None, "the case was quarantined, not stranded"
    assert Path(result.quarantine_folder).exists()
