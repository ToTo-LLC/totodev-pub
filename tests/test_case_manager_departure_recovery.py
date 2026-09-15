# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Departure recovery: what a restart does with a case that is already on its way out.

A *departing* case is one the manager has decided to stop driving — it reached a
terminal state, or it carries a terminate/eject/quarantine ticket. Ticket replay
owns those; recovery must not take them back. The failure this module pins down is
the opposite: recovery rehydrates every live-status folder, which takes a lease, and
a lease on a departing case blocks the very relocation the ticket was written to
perform. Finished work then fails *archive* and is quarantined for
``active lease present`` — a repair status applied to a case that needs no repair.

``readmit.py`` states the intended rule ("the departure-ticket check keeps it from
re-admitting a case already on its way out") and ``quarantine.py`` states the
intended guarantee ("without it, a restart re-admits a live-status case that
quarantine had already given up on"). Both are asserted here against the real
``recover()`` entry point, because that is where they are not yet true.

Every expected-failure test below is ``xfail(strict=True)``: it documents a known
gap and will *fail loudly the moment the gap closes*, which is the signal to delete
the marker. Deliberately not wrapped in ``very_lazy_test`` — a test that exists to
catch a regression in this area should run every time, not be cached as passed.
"""

from __future__ import annotations

import shutil

import pytest

from case_manager_test_utils import (
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_store import LIVE, QUARANTINED, TERMINATED
from totodev_pub.case_manager_support.quarantine import (
    quarantine_case,
    quarantine_ticket_exists,
)
from totodev_pub.case_manager_support.termination import (
    TerminationTicket,
    begin_termination,
    process_pending_ticket,
    ticket_path,
    write_ticket,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _clean_process_exit(manager) -> None:
    """Model a short-lived host that exited cleanly *without* draining its tickets.

    Only the filespace lease is released — the pending ticket and the live-status
    case are left exactly as a real ``stop()`` leaves them today. Releasing is also
    what keeps these tests fast: a lease left behind makes the successor wait out the
    manager-lease TTL before it can claim the working directory.
    """
    manager._release_filespace()


async def _closed_case_awaiting_archive(tmp_path) -> tuple[object, str]:
    """A finished case that is still LIVE in the store with a termination ticket pending.

    This is the exact state the briefing describes: the close path ran
    (``begin_termination`` removed it from the pool, detached it, and enqueued the
    archive), but the process stopped before the archive tick fired.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id

    await manager._driver.fire(case.case_folder, "finish")
    assert case.case_is_terminal, "precondition: the case really finished"

    assert begin_termination(
        case,
        manager_dir=manager._manager_dir,
        policy=manager._policy,
        driver_remove=manager._driver.remove,
    ), "precondition: the close path enqueued an archive ticket"

    assert manager._store.status_of(case_id) == LIVE
    folder = manager._store.find(case_id).case_folder
    assert FolderBackedCase.is_heartbeat_expired(folder) is None, (
        "precondition: begin_termination detached, so no lease is held. A lease seen "
        "later is one a LATER attach took, not a detach that failed to release."
    )
    _clean_process_exit(manager)
    return manager, case_id


# ----------------------------------------------------------------------------
# Gap 1 — recovery re-leases cases that are already on their way out
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gap 1: recover() rehydrates every live folder, so a ticketed terminal "
    "case is re-pooled and re-leased instead of being left to ticket replay.",
)
async def test_recover_does_not_repool_a_terminal_case_awaiting_archive(tmp_path):
    """The mechanism, isolated from its consequence: no pool slot, no lease."""
    _, case_id = await _closed_case_awaiting_archive(tmp_path)

    manager2 = provision_manager(tmp_path)
    await manager2.recover()

    pooled = [c.case_id for c in manager2._driver]
    folder = manager2._store.find(case_id).case_folder
    try:
        assert case_id not in pooled, "a case awaiting archive must not be driven again"
        assert FolderBackedCase.is_heartbeat_expired(folder) is not False, (
            "recovery took a lease on a case whose ticket exists to relocate it"
        )
    finally:
        _clean_process_exit(manager2)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gaps 1+3: the lease recovery takes fails verify_termination_peek, and "
    "'active lease present' spends the retry budget until the case is quarantined.",
)
async def test_a_closed_case_is_archived_after_a_restart(tmp_path):
    """The consequence, and the whole point: finished work must reach ``terminated/``."""
    _, case_id = await _closed_case_awaiting_archive(tmp_path)

    manager2 = provision_manager(tmp_path)
    await manager2.recover()
    try:
        for _ in range(manager2._policy.termination_max_retries + 3):
            await manager2._maintenance_tick()
            manager2._reconcile_terminal_in_pool()

        status = manager2._store.status_of(case_id)
        assert status != QUARANTINED, (
            "quarantine is a repair status; a case that finished its lifecycle "
            "needs archiving, not repair"
        )
        assert status == TERMINATED
    finally:
        _clean_process_exit(manager2)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gap 1: journal restore runs before readmit_orphans and pools every live "
    "folder, so readmit's departure-ticket guard never gets a say.",
)
async def test_recover_honours_a_pending_quarantine_ticket(tmp_path):
    """Quarantine's durable intent must survive a restart.

    ``test_a_departing_case_is_not_readmitted`` asserts this against
    ``_readmit_orphans()`` directly. Through the real ``recover()`` the guard is
    bypassed, and this case is *not* terminal — so it is not merely archived late,
    it is returned to active rotation after quarantine gave up on it.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id

    landed = await quarantine_case(
        manager._store, manager._manager_dir, case_id, case.case_folder, "will not die"
    )
    assert landed is None, "precondition: the move deferred behind the held lease"
    case.case_detach()
    assert manager._store.status_of(case_id) == LIVE
    _clean_process_exit(manager)

    manager2 = provision_manager(tmp_path)
    await manager2.recover()
    try:
        pooled = [c.case_id for c in manager2._driver]
        folder = manager2._store.find(case_id).case_folder
        assert case_id not in pooled, (
            "quarantine said stop driving this case; recovery must not undo that"
        )
        assert FolderBackedCase.is_heartbeat_expired(folder) is not False, (
            "the new lease blocks the relocation the quarantine ticket exists to do"
        )
    finally:
        _clean_process_exit(manager2)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gap 1: _has_departure_ticket checks eject and quarantine but not "
    "termination, unlike its sibling _departures_in_flight.",
)
async def test_a_termination_ticket_counts_as_a_departure_ticket(tmp_path):
    """"Already on its way out" has to mean all three ticket kinds, or it means nothing."""
    manager, case_id = await _closed_case_awaiting_archive(tmp_path)

    assert manager._departures_in_flight(), "precondition: a ticket is pending"
    assert manager._has_departure_ticket(case_id), (
        "a termination ticket is a departure; the docstring already says so"
    )


@pytest.mark.asyncio
async def test_a_crashed_terminal_case_with_no_ticket_is_archived(tmp_path):
    """Guard rail, not a gap: terminal on disk, live in the store, *no* ticket.

    A crash before ``begin_termination`` ever ran. This works today by the route
    ``readmit.py`` documents — re-admit like any other orphan, let the reconciler
    archive it — and it must keep working, because the narrower fix for the ticketed
    case (leave ticketed folders alone at recovery) deliberately does not touch it.

    Worth stating why this is a guard rather than a gap. Refusing to attach here too
    would be tidier, but it would force the archive to be driven entirely from a
    reader, and a reader cannot see everything: ``case_is_terminal`` is read from the
    event journal while ``case_terminal_at`` is read from the record, and the
    terminating edge writes the journal bookend before running ``on_terminating()``,
    the assertion sweep, and the purge that precede the record seal. A crash in that
    window is journal-terminal with an unstamped record — a case a live object could
    in principle finish, and a reader-only path never could.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    await manager._driver.fire(case.case_folder, "finish")
    assert case.case_is_terminal
    case.case_detach()
    assert manager._store.status_of(case_id) == LIVE
    _clean_process_exit(manager)

    manager2 = provision_manager(tmp_path)
    await manager2.recover()
    try:
        for _ in range(4):
            await manager2._maintenance_tick()
            manager2._reconcile_terminal_in_pool()

        status = manager2._store.status_of(case_id)
        assert status != QUARANTINED, "finished work is archived, never quarantined"
        assert status == TERMINATED
    finally:
        _clean_process_exit(manager2)


# ----------------------------------------------------------------------------
# Gap 3 — "active lease present" is a wait, not a failure
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gap 3: verify_termination_peek returns a bare False for a held lease, so "
    "it spends the same retry budget as a genuinely malformed record.",
)
async def test_a_held_lease_does_not_spend_the_termination_retry_budget(tmp_path):
    """``quarantine.py`` treats a held lease as "wait, this is the protocol". Termination
    must agree: waiting for a lease is not a failing archive.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    await manager._driver.fire(case.case_folder, "finish")
    assert case.case_is_terminal
    # Deliberately NOT detached: the terminal edge keeps the lease (force-beating a
    # full TTL so an owner can harvest), which is the state recovery leaves behind.
    folder = case.case_folder
    assert FolderBackedCase.is_heartbeat_expired(folder) is False

    path = ticket_path(manager._manager_dir, case_id)
    write_ticket(
        TerminationTicket(
            case_id=case_id, case_folder=str(folder), enqueued_at="2026-09-15T00:00:00Z"
        ),
        path,
    )

    try:
        for _ in range(manager._policy.termination_max_retries + 2):
            if not path.exists():
                break       # retired to failed/; the assertions below say why that is wrong
            await process_pending_ticket(
                TerminationTicket.load(str(path), acquire_lock=False),
                path,
                store=manager._store,
                policy=manager._policy,
                manager_dir=manager._manager_dir,
                quarantine=manager._quarantine,
            )

        # Quarantining is asserted via the ticket, not the status: the relocation
        # defers behind this very lease, so the case stays LIVE even though the
        # manager has already decided to give up on it.
        assert not quarantine_ticket_exists(manager._manager_dir, case_id), (
            "a finished case was sent to quarantine for holding the lease its own "
            "terminal edge is designed to hold"
        )
        assert manager._store.status_of(case_id) != QUARANTINED
        assert path.exists(), "the ticket should still be pending, waiting on the lease"
    finally:
        case.case_detach()
        _clean_process_exit(manager)


@pytest.mark.asyncio
async def test_a_ticket_for_a_non_terminal_record_is_still_quarantined(tmp_path):
    """Guard rail, not a gap: this passes today and must keep passing.

    Softening the held-lease path must not soften this one. A ticket naming a record
    that is not terminal is a real anomaly — no amount of waiting fixes it.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    assert not case.case_is_terminal
    folder = case.case_folder
    manager._driver.remove(folder)
    case.case_detach()

    path = ticket_path(manager._manager_dir, case_id)
    write_ticket(
        TerminationTicket(
            case_id=case_id, case_folder=str(folder), enqueued_at="2026-09-15T00:00:00Z"
        ),
        path,
    )

    try:
        for _ in range(manager._policy.termination_max_retries + 1):
            if not path.exists():
                break
            await process_pending_ticket(
                TerminationTicket.load(str(path), acquire_lock=False),
                path,
                store=manager._store,
                policy=manager._policy,
                manager_dir=manager._manager_dir,
                quarantine=manager._quarantine,
            )

        assert manager._store.status_of(case_id) == QUARANTINED, (
            "a ticket naming a non-terminal record must still end in quarantine"
        )
    finally:
        _clean_process_exit(manager)


# ----------------------------------------------------------------------------
# Gap 4 — hosts cannot wait for archive completion
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gap 4: _departures_in_flight is private and is_idle also demands an empty "
    "pool, so a host with live work has no way to wait for archive completion.",
)
async def test_a_host_can_wait_for_departures_without_an_empty_pool(tmp_path):
    """A fleet that always has live work still needs to know its tickets have filed.

    ``is_idle`` conflates "nothing left to archive" with "nothing left to do", so a
    busy pool can never observe the first.

    Unavoidably pins a proposed *name*, which no other test here does — the gap is a
    request for a public surface, so some name has to be chosen. Rename freely; the
    assertions that matter are that the signal is true while a ticket is pending and
    false once it is filed, with no reference to pool occupancy.
    """
    manager, case_id = await _closed_case_awaiting_archive(tmp_path)

    assert hasattr(manager, "departures_in_flight"), "a public departure signal"
    assert hasattr(manager, "wait_for_departures"), "and a way to await it"
    assert manager.departures_in_flight is True, "a ticket is pending"

    await manager._maintenance_tick()       # file the ticket
    assert manager._store.status_of(case_id) == TERMINATED
    assert manager.departures_in_flight is False, "nothing left to archive"


# ----------------------------------------------------------------------------
# Gap 5 — a detach must not resurrect a folder that has moved
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Gap 5: write_detach_banner runs before disable_case_file_tee, and the log "
    "handler mkdir(parents=True)s its target, recreating the vanished folder.",
)
async def test_detaching_does_not_recreate_a_folder_that_has_moved(tmp_path):
    """After a status move, a stale in-memory instance must not write at the old path.

    ``disable_case_file_tee`` already states the invariant: a detached object "must
    never write into a folder another process may now own by then". Recreating the
    tree to log a banner is the same violation, and it is what leaves a ghost beside
    every relocated case.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    folder = case.case_folder

    # The folder moves out from under a still-bound instance, exactly as a relocation
    # performed once the lease lapsed would leave it.
    moved = tmp_path / "relocated"
    shutil.move(str(folder), str(moved))
    assert not folder.exists()

    try:
        case.case_detach()
        assert not folder.exists(), (
            f"detach recreated the stale path: "
            f"{sorted(p.name for p in folder.rglob('*'))}"
        )
    finally:
        _clean_process_exit(manager)
