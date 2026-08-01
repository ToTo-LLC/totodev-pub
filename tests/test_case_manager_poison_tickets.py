# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""A ticket that cannot be read must not strand its case forever.

Every ticket carries its own ``retry_count``, which works right up until the
ticket is the thing that is broken: one that will not parse can never record
that it was tried. Left alone it is retried every tick forever while the case it
describes sits removed from the pool, detached, and un-enqueueable — the manager
notices about it once per second and nothing ever changes.

These tests pin the escape: count attempts outside the file, and on exhaustion
retire the ticket and quarantine the case.
"""

from pathlib import Path

import pytest

from case_manager_test_utils import (
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_store import LIVE, QUARANTINED
from totodev_pub.case_manager_support.eject import eject_dir
from totodev_pub.case_manager_support.quarantine import (
    pending_quarantine_tickets,
    quarantine_case,
    quarantine_dir,
)
from totodev_pub.case_manager_support.termination import replay_pending, termination_dir
from totodev_pub.case_manager_support.ticket_attempts import TicketAttemptLedger
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry

_ATTEMPTS = 3   # provision_manager leaves termination_max_retries at its default


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


def _corrupt(path: Path) -> None:
    path.write_text("{[ this is not yaml: and never will be\x00", encoding="utf-8")


async def _terminal_case_with_ticket(manager, tmp_path, name="inbound"):
    """A case that has been enqueued for termination: removed, detached, ticketed."""
    staging = tmp_path / name
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager._driver.fire(case.case_folder, "finish")
    manager._reconcile_terminal_in_pool()
    pending = replay_pending(manager._manager_dir)
    assert len(pending) == 1
    return case, pending[0]


# --------------------------------------------------------------------- ledger


def test_the_ledger_counts_per_item_and_forgets():
    ledger = TicketAttemptLedger()
    a, b = Path("a.yaml"), Path("b.yaml")

    assert ledger.record_failure(a) == 1
    assert ledger.record_failure(a) == 2
    assert ledger.record_failure(b) == 1, "counts are per item, not global"
    assert ledger.attempts(a) == 2

    ledger.forget(a)
    assert ledger.attempts(a) == 0
    assert ledger.attempts(b) == 1, "forgetting one item leaves the others alone"
    assert len(ledger) == 1, "and the ledger does not grow for the process lifetime"


# ------------------------------------------------------------ termination


@pytest.mark.asyncio
async def test_an_unreadable_termination_ticket_is_retried_then_retired(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case, ticket_file = await _terminal_case_with_ticket(manager, tmp_path)
    _corrupt(ticket_file)

    notices = []
    manager.on_notice(notices.append)

    # Below the threshold it stays pending: a transient failure must not spend
    # the whole budget on its first occurrence.
    for _ in range(_ATTEMPTS - 1):
        await manager._maintenance_tick()
        assert ticket_file.exists(), "still being retried"

    await manager._maintenance_tick()

    assert not ticket_file.exists(), "the poison ticket was retired"
    assert (termination_dir(manager._manager_dir) / "failed" / ticket_file.name).exists()
    assert "TICKET_ABANDONED" in [n.kind.value for n in notices]


@pytest.mark.asyncio
async def test_the_case_behind_a_poison_ticket_is_quarantined(tmp_path):
    """The case is the point: without this it stays live, undriven, forever."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    case, ticket_file = await _terminal_case_with_ticket(manager, tmp_path)
    case_id = case.case_id
    _corrupt(ticket_file)

    assert manager._store.status_of(case_id) == LIVE
    for _ in range(_ATTEMPTS):
        await manager._maintenance_tick()

    assert manager._store.status_of(case_id) == QUARANTINED
    assert case_id in [r.case_id for r in manager.iter_quarantine()]


@pytest.mark.asyncio
async def test_a_quarantined_poison_case_is_recoverable(tmp_path):
    """Quarantine is a holding pattern, not a grave — reopen_case() gets it back."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    case, ticket_file = await _terminal_case_with_ticket(manager, tmp_path)
    case_id = case.case_id
    _corrupt(ticket_file)
    for _ in range(_ATTEMPTS):
        await manager._maintenance_tick()
    assert manager._store.status_of(case_id) == QUARANTINED

    await manager.reopen_case(case_id)

    assert manager._store.status_of(case_id) == LIVE
    assert case_id in [c.case_id for c in manager._driver]


@pytest.mark.asyncio
async def test_a_transient_failure_does_not_retire_a_good_ticket(tmp_path, monkeypatch):
    """The count resets on success, so intermittent trouble never accumulates."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    case, ticket_file = await _terminal_case_with_ticket(manager, tmp_path)

    import totodev_pub.case_manager as case_manager_module

    calls = {"n": 0}
    real = case_manager_module.process_pending_ticket

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise OSError("transient")
        return await real(*args, **kwargs)

    monkeypatch.setattr(case_manager_module, "process_pending_ticket", flaky)

    for _ in range(6):
        await manager._maintenance_tick()
        if not ticket_file.exists():
            break

    assert not (termination_dir(manager._manager_dir) / "failed" / ticket_file.name).exists()
    assert manager._store.status_of(case.case_id) == "terminated", "it finished normally"


@pytest.mark.asyncio
async def test_a_raise_before_the_tickets_own_counter_still_counts(tmp_path, monkeypatch):
    """The verify step reads the case record and can raise before retry_count += 1.

    Same trap as an unparseable ticket, reached a different way: the ticket's own
    counter never advances, so only an outside count can end it.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    case, ticket_file = await _terminal_case_with_ticket(manager, tmp_path)

    import totodev_pub.case_manager_support.termination as termination_module

    monkeypatch.setattr(
        termination_module,
        "verify_termination_peek",
        lambda folder: (_ for _ in ()).throw(OSError("record vanished mid-read")),
    )

    for _ in range(_ATTEMPTS):
        await manager._maintenance_tick()

    assert not ticket_file.exists()
    assert manager._store.status_of(case.case_id) == QUARANTINED


# ----------------------------------------------------------------- quarantine


@pytest.mark.asyncio
async def test_an_unreadable_quarantine_ticket_is_replaced_not_looped(tmp_path):
    """Retiring the poison ticket re-drives quarantine, which writes a fresh one.

    The replacement is parseable, so this converges rather than trading one
    unreadable ticket for another.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)   # attached: quarantine defers
    case_id = case.case_id

    try:
        await quarantine_case(
            manager._store, manager._manager_dir, case_id, case.case_folder, "will not die"
        )
        ticket_file = pending_quarantine_tickets(manager._manager_dir)[0]
        _corrupt(ticket_file)

        notices = []
        manager.on_notice(notices.append)
        for _ in range(_ATTEMPTS):
            await manager._maintenance_tick()

        assert "TICKET_ABANDONED" in [n.kind.value for n in notices]
        assert (quarantine_dir(manager._manager_dir) / "failed" / ticket_file.name).exists()
        replacement = pending_quarantine_tickets(manager._manager_dir)
        assert len(replacement) == 1, "a fresh, readable ticket took its place"
    finally:
        case.case_detach()

    await manager._maintenance_tick()
    assert manager._store.status_of(case_id) == QUARANTINED


# --------------------------------------------------------------------- eject


@pytest.mark.asyncio
async def test_an_unreadable_eject_ticket_settles_its_waiter(tmp_path):
    """A caller blocked in eject_from_pool() must learn the export is not coming."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager.start()

    from totodev_pub.case_manager_support.exceptions import EjectAbandonedError

    try:
        eject = manager.eject_from_pool(
            case.case_id, export_to_folder=tmp_path / "exported"
        )
        import asyncio

        task = asyncio.ensure_future(eject)
        await asyncio.sleep(0)   # let begin_eject write the ticket

        ticket_file = eject_dir(manager._manager_dir) / "pending" / f"{case.case_id}.yaml"
        assert ticket_file.exists()
        _corrupt(ticket_file)

        with pytest.raises(EjectAbandonedError):
            await asyncio.wait_for(task, timeout=10.0)
    finally:
        await manager.stop()
