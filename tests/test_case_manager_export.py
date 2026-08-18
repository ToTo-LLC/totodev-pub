# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""export_case: leave managed storage without going through the live-pool eject path."""

from datetime import datetime
from pathlib import Path

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case
from totodev_pub.case_manager_support.case_store import LocalCaseStore, QUARANTINED, TERMINATED
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


async def _adopt_then_archive(manager, staging: Path, *, status: str = TERMINATED) -> str:
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    folder = case.case_folder
    removed = manager._driver.remove(folder)
    removed.case_detach()
    await manager._store.set_status(case_id, status)
    return case_id


@pytest.mark.asyncio
async def test_export_case_keeps_a_terminated_folder_at_dest(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case_id = await _adopt_then_archive(manager, tmp_path / "a")
    dest = tmp_path / "kept"

    result = await manager.export_case(case_id, dest=dest)

    assert result == dest
    assert (dest / RECORD_NAME).exists()
    assert manager.locate(case_id) is None


@pytest.mark.asyncio
async def test_export_case_dest_none_destroys_terminated(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case_id = await _adopt_then_archive(manager, tmp_path / "a")

    result = await manager.export_case(case_id, dest=None)

    assert result is None
    assert manager.locate(case_id) is None


@pytest.mark.asyncio
async def test_export_case_dest_none_destroys_quarantined(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case_id = await _adopt_then_archive(
        manager, tmp_path / "a", status=QUARANTINED
    )

    result = await manager.export_case(case_id, dest=None)

    assert result is None
    assert manager.locate(case_id) is None


@pytest.mark.asyncio
async def test_export_case_refuses_a_live_case(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    seed_detached_case(TicketCase, tmp_path / "live")
    case = await adopt_into_live(manager, tmp_path / "live")

    with pytest.raises(ValueError, match="eject_from_pool"):
        await manager.export_case(case.case_id, dest=None)

    assert manager.locate(case.case_id) is not None
    assert manager.get_live(case.case_id) is case


@pytest.mark.asyncio
async def test_export_case_requires_dest_keyword(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    case_id = await _adopt_then_archive(manager, tmp_path / "a")

    with pytest.raises(TypeError):
        await manager.export_case(case_id)


@pytest.mark.asyncio
async def test_iter_terminal_then_export_case_retires_only_the_old_ids(
    tmp_path, monkeypatch
):
    """The documented cleanup recipe: snapshot age-filtered ids, then destroy."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def adopt_unpooled(staging: Path) -> str:
        seed_detached_case(TicketCase, staging)
        case = await adopt_into_live(manager, staging)
        case_id = case.case_id
        folder = case.case_folder
        removed = manager._driver.remove(folder)
        removed.case_detach()
        return case_id

    old_id = await adopt_unpooled(tmp_path / "old")
    new_id = await adopt_unpooled(tmp_path / "new")
    times = {
        old_id: datetime(2026, 7, 1, 12, 0, 0),
        new_id: datetime(2026, 8, 15, 12, 0, 0),
    }
    real_activity = LocalCaseStore._activity_at

    def fake_activity(self, folder):
        folder = Path(folder).resolve()
        for cid, when in times.items():
            found = self.find(cid)
            if found is not None and found.case_folder.resolve() == folder:
                return when
        return real_activity(self, folder)

    monkeypatch.setattr(LocalCaseStore, "_activity_at", fake_activity)
    await manager._store.set_status(old_id, TERMINATED)
    await manager._store.set_status(new_id, TERMINATED)

    cutoff = datetime(2026, 8, 1)
    old = [r.case_id for r in manager.iter_terminal(before=cutoff)]
    assert old == [old_id]

    for case_id in old:
        await manager.export_case(case_id, dest=None)

    assert manager.locate(old_id) is None
    assert manager.locate(new_id) is not None
