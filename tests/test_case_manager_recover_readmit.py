# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Startup orphan recovery: one rule, applied to every undriven live case.

A live-status case that is not in the pool and whose lease has expired gets
re-admitted; if it cannot be rehydrated, it escalates. Terminal orphans are *not*
special-cased — they are re-admitted like any other and the per-tick reconciler
archives them next tick, which is what leaves a single archive-label computation
in the codebase rather than two that could disagree.
"""

import logging

import pytest

from case_manager_test_utils import (
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_store import LIVE, TERMINATED
from totodev_pub.case_manager_support.exceptions import RecoveryIntegrityError
from totodev_pub.case_manager_support.quarantine import quarantine_case
from totodev_pub.folder_backed_case import FolderBackedCase
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
    report = await manager2._readmit_orphans()
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
        report = await manager2._readmit_orphans()
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
    report = await manager2._readmit_orphans()

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
    manager2.subscribe_notices(notices.append)
    def unreadable(folder):
        raise ValueError("record is unreadable")

    monkeypatch.setattr(manager2._registry, "rehydrate", unreadable)
    report = await manager2._readmit_orphans()

    assert report.readmitted == []
    assert report.anomalies == [case_id]
    assert [n.kind.value for n in notices] == ["READMIT_ANOMALY"]


@pytest.mark.asyncio
async def test_a_failed_readmit_releases_the_lease_it_took(tmp_path, monkeypatch):
    """rehydrate() takes the lease before driver.add() gets a say.

    Holding it after a failure makes the orphan look *owned* to every later pass,
    including the next restart's — so a case that failed to re-admit once would
    never be looked at again.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    case.case_detach()

    manager2 = provision_manager(tmp_path)

    def rejecting_add(admitted):
        raise RuntimeError("driver said no")

    monkeypatch.setattr(manager2._driver, "add", rejecting_add)
    report = await manager2._readmit_orphans()
    assert report.anomalies == [case_id]

    folder = manager2._store.find(case_id).case_folder
    assert FolderBackedCase.is_heartbeat_expired(folder) is not False, (
        "the lease must be released, or the orphan reads as owned from here on"
    )

    manager3 = provision_manager(tmp_path)
    assert (await manager3._readmit_orphans()).readmitted == [case_id], "still reachable"


@pytest.mark.asyncio
async def test_a_pooled_case_the_store_no_longer_calls_live_is_evicted(tmp_path, caplog):
    """The reverse direction: a pooled object addressing a folder that has moved.

    Driving it could only fail, and failing later at tick time would report the
    symptom far from the cause, so it is evicted here rather than merely noted.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    case.case_detach()

    await manager._store.set_status(case_id, TERMINATED)
    assert case_id in {c.case_id for c in manager._driver}, "precondition: still pooled"

    notices = []
    manager.subscribe_notices(notices.append)
    with caplog.at_level(logging.ERROR):
        report = await manager._readmit_orphans()

    assert report.stale_pool_entries == [case_id]
    assert case_id not in {c.case_id for c in manager._driver}, "evicted from the pool"
    assert [n.kind.value for n in notices] == ["READMIT_ANOMALY"]
    # The status is named, not just the id — "terminated" and "gone entirely" are
    # very different pages at 3am.
    assert any(
        case_id in r.getMessage() and "terminated" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ), caplog.text


@pytest.mark.asyncio
async def test_strict_recovery_raises_on_an_integrity_problem(tmp_path):
    """Contained by default, fail-fast on request: same detection, different landing."""
    manager = provision_manager(tmp_path, strict_recovery=True)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    case.case_detach()
    await manager._store.set_status(case_id, TERMINATED)

    with pytest.raises(RecoveryIntegrityError) as excinfo:
        await manager.recover()
    assert case_id in excinfo.value.stale_pool_entries
    assert case_id in str(excinfo.value)


@pytest.mark.asyncio
async def test_recovery_always_logs_a_summary(tmp_path, caplog):
    """A revived fleet that says nothing looks exactly like one with nothing to do."""
    manager = provision_manager(tmp_path)
    with caplog.at_level(logging.INFO):
        await manager.recover()
    assert any("Recovery complete:" in r.getMessage() for r in caplog.records), caplog.text
