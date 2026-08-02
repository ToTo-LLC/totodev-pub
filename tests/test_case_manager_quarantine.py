# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Quarantine: immediate when the lease allows, deferred when it does not.

The deferral is the design, not a fallback. A case that keeps renewing its lease
while failing every interaction is exactly the one quarantine exists for, and
moving its folder anyway would be a split-brain. So the move waits, behind a
durable ticket the manager re-drives every tick.
"""

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_store import LIVE, QUARANTINED
from totodev_pub.case_manager_support.quarantine import (
    EV_QUARANTINED,
    QuarantineTicket,
    pending_quarantine_tickets,
    quarantine_case,
    quarantine_ticket_exists,
)
from totodev_pub.folder_backed_case_support.case_journal import CaseEventJournalView
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@pytest.mark.asyncio
async def test_operator_can_request_quarantine_of_a_live_case(tmp_path):
    """Park a live case for investigation without ejecting it from managed storage."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id

    dest = await manager.quarantine_case(case_id, reason="operator: investigate payload")

    assert dest is not None
    assert manager._store.status_of(case_id) == QUARANTINED
    assert case_id not in [c.case_id for c in manager._driver]
    assert case_id in [r.case_id for r in manager.iter_quarantine()]
    labels = [ev.label for ev in CaseEventJournalView.for_folder(dest).primitive.events()]
    assert EV_QUARANTINED in labels

    await manager.reopen_case(case_id)
    assert manager.get_live(case_id).case_id == case_id


@pytest.mark.asyncio
async def test_operator_quarantine_requires_a_reason(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)

    with pytest.raises(ValueError, match="reason"):
        await manager.quarantine_case(case.case_id, reason="   ")


@pytest.mark.asyncio
async def test_a_released_case_is_quarantined_immediately(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id, live_folder = case.case_id, case.case_folder
    case.case_detach()
    (live_folder / "payload.txt").write_text("keep me", encoding="utf-8")

    dest = await quarantine_case(
        manager._store, manager._manager_dir, case_id, live_folder, "verification failed"
    )

    assert dest is not None and dest != live_folder
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "keep me"
    assert manager._store.status_of(case_id) == QUARANTINED
    assert not quarantine_ticket_exists(manager._manager_dir, case_id)


@pytest.mark.asyncio
async def test_a_held_lease_defers_the_move_behind_a_ticket(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)   # adopt leaves the case attached
    case_id, live_folder = case.case_id, case.case_folder

    try:
        dest = await quarantine_case(
            manager._store, manager._manager_dir, case_id, live_folder, "will not die"
        )
        assert dest is None, "the move is deferred, not forced"
        assert quarantine_ticket_exists(manager._manager_dir, case_id)
        assert manager._store.status_of(case_id) == LIVE, "the folder has not moved"
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_the_deferred_move_lands_once_the_lease_lapses(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id

    await quarantine_case(
        manager._store, manager._manager_dir, case_id, case.case_folder, "will not die"
    )
    assert pending_quarantine_tickets(manager._manager_dir)

    # The lease lapses; the next tick re-drives the ticket.
    case.case_detach()
    await manager._maintenance_tick()

    assert manager._store.status_of(case_id) == QUARANTINED
    assert pending_quarantine_tickets(manager._manager_dir) == []


@pytest.mark.asyncio
async def test_waiting_out_a_lease_does_not_spend_retries(tmp_path):
    """A lease can outlive many ticks; that must never retire the ticket."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)

    try:
        await quarantine_case(
            manager._store, manager._manager_dir, case.case_id, case.case_folder, "held"
        )
        for _ in range(5):
            await manager._maintenance_tick()

        pending = pending_quarantine_tickets(manager._manager_dir)
        assert len(pending) == 1, "the ticket survives every attempt"
        assert QuarantineTicket.load(str(pending[0]), acquire_lock=False).retry_count == 0
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_the_reason_is_recorded_on_the_case_itself(tmp_path):
    """The reason belongs to the case's journal, which travels with the folder."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id, live_folder = case.case_id, case.case_folder
    case.case_detach()

    dest = await quarantine_case(
        manager._store, manager._manager_dir, case_id, live_folder, "record was garbage"
    )

    labels = [ev.label for ev in CaseEventJournalView.for_folder(dest).primitive.events()]
    assert EV_QUARANTINED in labels, "the reason survived the relocation with the case"


@pytest.mark.asyncio
async def test_an_orphan_with_no_store_entry_is_absorbed(tmp_path):
    """The rescue path: a folder on disk the index never knew about.

    Copying bytes alone is not enough — an unregistered folder is invisible to
    iter_quarantine() and to every other index-driven query the manager has.
    """
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()

    orphan = tmp_path / "orphan"
    case = seed_detached_case(TicketCase, orphan)
    assert manager._store.find(case.case_id) is None

    dest = await quarantine_case(
        manager._store, manager._manager_dir, case.case_id, orphan, "orphaned by termination"
    )

    assert dest is not None and (dest / RECORD_NAME).exists()
    assert manager._store.status_of(case.case_id) == QUARANTINED
    assert case.case_id in [reader.case_id for reader in manager.iter_quarantine()]
