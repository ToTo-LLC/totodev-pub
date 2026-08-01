# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""A case archives by the month it closed, not the month it was archived.

Two code paths compute a terminal case's destination bucket: the normal path via
``archive_grouping_label()`` on the live case, and the disk path via
``destination_key_from_record()`` when only the folder is available. They must
agree, or the same case lands in different buckets depending on which path ran.
"""

import datetime

import pytest

from case_manager_test_utils import (
    TerminalCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.termination import (
    destination_key_for_case,
    destination_key_from_record,
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
async def test_both_destination_paths_agree(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case = await _closed_case(manager, tmp_path, "inbound2")

    terminal_at = FolderBackedCaseReader(case.case_folder).case_terminal_at
    much_later = terminal_at + datetime.timedelta(days=400)
    monkeypatch.setattr(
        "totodev_pub.folder_backed_case._utcnow", lambda: much_later
    )

    from_case = destination_key_for_case(case, manager._policy)
    from_disk = destination_key_from_record(case.case_folder, manager._policy)
    assert from_case == from_disk
