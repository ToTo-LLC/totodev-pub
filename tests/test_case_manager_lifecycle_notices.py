# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Case departures are announced, not catalogued.

The manager does not become the librarian of departed cases: a case leaving the
pool emits a lifecycle notice and nothing durable is kept. An application that
needs history subscribes and keeps its own record. Delivery is at-most-once —
in-process pub/sub does not survive a crash — which is why these are operator
convenience and not an audit log.
"""

import pytest

from case_manager_test_utils import (
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.escalation import CaseEscalationKind
from totodev_pub.case_manager_support.termination import termination_dir
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


def _kinds(notices):
    return [n.kind for n in notices]


def test_lifecycle_kinds_are_distinguishable_from_problems():
    """Subscribers filter on this; a paging handler wants the problems only."""
    assert CaseEscalationKind.CASE_TERMINATED.is_lifecycle
    assert CaseEscalationKind.CASE_QUARANTINED.is_lifecycle
    assert CaseEscalationKind.CASE_EJECTED.is_lifecycle
    assert not CaseEscalationKind.MANAGER_UNRESPONSIVE.is_lifecycle
    assert not CaseEscalationKind.READMIT_ANOMALY.is_lifecycle


@pytest.mark.asyncio
async def test_termination_announces_the_departure(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    await manager._driver.fire(case.case_folder, "finish")

    notices = []
    manager.on_escalation(notices.append)
    manager._reconcile_terminal_in_pool()
    await manager._maintenance_tick()

    departures = [n for n in notices if n.kind is CaseEscalationKind.CASE_TERMINATED]
    assert len(departures) == 1
    assert departures[0].case_id == case_id
    assert departures[0].case_folder is not None


@pytest.mark.asyncio
async def test_quarantine_announces_the_departure(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case.case_detach()

    notices = []
    manager.on_escalation(notices.append)
    await manager._quarantine(case.case_id, case.case_folder, "unreadable record")

    assert CaseEscalationKind.CASE_QUARANTINED in _kinds(notices)


@pytest.mark.asyncio
async def test_eject_announces_the_departure(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager.start()

    notices = []
    manager.on_escalation(notices.append)
    try:
        await manager.eject_from_pool(
            case.case_id, export_to_folder=tmp_path / "exported", timeout=5.0
        )
    finally:
        await manager.stop()

    assert CaseEscalationKind.CASE_EJECTED in _kinds(notices)


@pytest.mark.asyncio
async def test_a_completed_termination_leaves_no_receipt_behind(tmp_path):
    """Idempotency comes from stored status, so no per-case file accumulates.

    While ``done/`` was the guard, every terminated case left one YAML forever
    and nothing swept them.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager._driver.fire(case.case_folder, "finish")

    manager._reconcile_terminal_in_pool()
    await manager._maintenance_tick()

    assert manager._store.status_of(case.case_id) == "terminated"
    leftovers = list(termination_dir(manager._manager_dir).rglob("*.yaml"))
    assert leftovers == [], f"termination left receipts behind: {leftovers}"


@pytest.mark.asyncio
async def test_an_already_departed_case_is_not_re_enqueued(tmp_path):
    """Stored status is the receipt: a terminated case needs no ticket to prove it."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    staging = tmp_path / "inbound"
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager._driver.fire(case.case_folder, "finish")

    manager._reconcile_terminal_in_pool()
    await manager._maintenance_tick()

    # Force the reconciler to see it again; its status must be enough to skip it.
    manager._driver.terminal_cases = lambda: [case]
    assert manager._reconcile_terminal_in_pool() == 0
    assert list(termination_dir(manager._manager_dir).rglob("*.yaml")) == []
