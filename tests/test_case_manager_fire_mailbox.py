# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case
from totodev_pub.case_manager_client import CaseManagerClient


@pytest.mark.asyncio
async def test_fire_mailbox_submit(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    await manager.start()
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_fire(case_id=case.case_id, trigger="work", only_if_fresh=False)
    result = await client.wait_result(handle, timeout=5.0)
    assert result is not None
    await manager.stop()
