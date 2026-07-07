# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_eject_from_pool(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    export_to = tmp_path / "exported"
    await manager.start()
    result = await manager.eject_from_pool(case.case_id, export_to_folder=export_to)
    assert result.export_folder.exists()
    assert manager.locate(case_id=case.case_id) is None
    await manager.stop()
