# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""A case archives by the month it closed, not the month it was archived.

There is exactly one computation of a terminal case's archive partition, decided
once at enqueue time from the case's own label. These tests pin both halves of
that: the label follows the close, and the ticket carries the decision so a
restart across a month boundary cannot file the same case twice.
"""

import datetime

import pytest

from case_manager_test_utils import (
    TerminalCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_store import TERMINATED
from totodev_pub.case_manager_support.termination import (
    TerminationTicket,
    replay_pending,
)
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


async def _closed_case(manager, tmp_path, name):
    staging = tmp_path / name
    seed_detached_case(TerminalCase, staging)
    case = await adopt_into_live(manager, staging)
    await manager._driver.fire(case.case_folder, "finish")
    assert case.case_is_terminal
    return case


@pytest.mark.asyncio
async def test_archive_label_follows_the_close_month_not_the_clock(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case = await _closed_case(manager, tmp_path, "inbound")

    terminal_at = FolderBackedCaseReader(case.case_folder).case_terminal_at
    assert terminal_at is not None
    expected = terminal_at.strftime("%Y-%m")

    # Archive long after the close. The label must not drift to "now".
    much_later = terminal_at + datetime.timedelta(days=400)
    monkeypatch.setattr(
        "totodev_pub.folder_backed_case._utcnow", lambda: much_later
    )
    assert case.archive_grouping_label() == expected


@pytest.mark.asyncio
async def test_the_partition_is_decided_once_and_carried_on_the_ticket(tmp_path, monkeypatch):
    """The ticket records the destination, so a later replay cannot re-decide it.

    Deciding again at move time would let a restart across a month boundary file
    the same case under a different label than the one it was enqueued for.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    case = await _closed_case(manager, tmp_path, "inbound2")
    case_id = case.case_id

    terminal_at = FolderBackedCaseReader(case.case_folder).case_terminal_at
    expected = terminal_at.strftime("%Y-%m")

    manager._reconcile_terminal_in_pool()
    pending = replay_pending(manager._manager_dir)
    assert len(pending) == 1
    ticket = TerminationTicket.load(str(pending[0]), acquire_lock=False)
    assert ticket.destination_partition == expected

    # A restart a year later must still archive it where the ticket says.
    monkeypatch.setattr(
        "totodev_pub.folder_backed_case._utcnow",
        lambda: terminal_at + datetime.timedelta(days=400),
    )
    await manager._maintenance_tick()

    entry = manager._store.find(case_id)
    assert (entry.status, entry.partition) == (TERMINATED, expected)
