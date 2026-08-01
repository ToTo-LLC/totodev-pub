# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Startup orphan recovery: one rule, applied to every undriven live case.

A live-status case that is not in the pool and whose lease has expired gets
re-admitted; if it cannot be rehydrated, it escalates. Terminal orphans are *not*
special-cased — they are re-admitted like any other and the per-tick reconciler
archives them next tick, which is what leaves a single archive-label computation
in the codebase rather than two that could disagree.
"""

import pytest

from case_manager_test_utils import (
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_store import LIVE, TERMINATED
from totodev_pub.case_manager_support.quarantine import quarantine_case
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.pytest_tools import very_lazy_test


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@pytest.mark.asyncio
@pytest.mark.slow
@very_lazy_test(
    [
        "case_manager_test_utils.py",
        "totodev_pub.case_manager",
        "totodev_pub.case_manager_support.recover",
        "totodev_pub.case_manager_support.readmit",
        "totodev_pub.folder_backed_case_support.pool_membership_journal",
    ],
    reverify_days=21,
)
async def test_recover_restores_pool(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    case = await adopt_into_live(manager, staging / "c1")
    case_id = case.case_id
    case.case_detach()

    manager2 = provision_manager(tmp_path)
    report = await manager2.recover()

    assert manager2.locate(case_id=case_id) is not None
    assert case_id in [c.case_id for c in manager2._driver], "the orphan is driven again"
    assert report.orphans_readmitted + report.pool_restored >= 1


@pytest.mark.asyncio
async def test_a_terminal_orphan_is_readmitted_then_archived_next_tick(tmp_path):
    """No special case for terminal orphans — re-admit, then let the reconciler run."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    await manager._driver.fire(case.case_folder, "finish")
    assert case.case_is_terminal
    case.case_detach()

    # A crash: the case is terminal on disk, still live in the store, undriven.
    manager2 = provision_manager(tmp_path)
    report = await manager2.readmit_orphans()
    assert report.readmitted == [case_id], "terminal or not, an orphan is re-admitted"

    manager2._reconcile_terminal_in_pool()
    await manager2._maintenance_tick()

    assert manager2._store.status_of(case_id) == TERMINATED, "archived on the next pass"


@pytest.mark.asyncio
async def test_a_leased_orphan_is_left_alone(tmp_path):
    """A held lease means another owner is working it. Stealing it is split-brain."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)   # stays attached: lease held

    try:
        manager2 = provision_manager(tmp_path)
        report = await manager2.readmit_orphans()
        assert report.readmitted == []
        assert report.anomalies == []
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_a_departing_case_is_not_readmitted(tmp_path):
    """A quarantine ticket says "stop driving this"; recovery must not undo that."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id

    # Quarantine defers behind the held lease, leaving a ticket.
    await quarantine_case(
        manager._store, manager._manager_dir, case_id, case.case_folder, "will not die"
    )
    case.case_detach()

    manager2 = provision_manager(tmp_path)
    report = await manager2.readmit_orphans()

    assert report.readmitted == [], "the departure ticket wins over re-admission"
    assert manager2._store.status_of(case_id) == LIVE


@pytest.mark.asyncio
async def test_an_unrehydratable_orphan_escalates(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    case.case_detach()

    manager2 = provision_manager(tmp_path)
    notices = []
    manager2.on_escalation(notices.append)
    def unreadable(folder):
        raise ValueError("record is unreadable")

    monkeypatch.setattr(manager2._registry, "rehydrate", unreadable)
    report = await manager2.readmit_orphans()

    assert report.readmitted == []
    assert report.anomalies == [case_id]
    assert [n.kind.value for n in notices] == ["READMIT_ANOMALY"]


@pytest.mark.asyncio
async def test_a_pooled_case_the_store_no_longer_calls_live_is_reported(tmp_path):
    """The reverse direction: a pooled object addressing a folder that has moved.

    Under an exclusively-owned filespace this should never happen; it is a
    consistency assertion, not a routine repair.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    case.case_detach()

    await manager._store.set_status(case_id, TERMINATED, partition="2026-08")

    notices = []
    manager.on_escalation(notices.append)
    report = await manager.readmit_orphans()

    assert report.stale_pool_entries == [case_id]
    assert [n.kind.value for n in notices] == ["READMIT_ANOMALY"]
