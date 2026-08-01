# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import (
    TicketCase,
    attach_adapter,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_client import CaseManagerClient


@pytest.mark.asyncio
async def test_adopt_mailbox(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    await manager.recover()
    attach_adapter(manager)
    await manager.start()
    staging = tmp_path / "staging" / "c1"
    staging.mkdir(parents=True)
    seed_detached_case(TicketCase, staging)
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_adopt(source_folder=staging, only_if_fresh=False)
    result = await client.wait_adopt(handle, timeout=5.0)
    assert result is not None
    assert result.status == "completed"
    await manager.stop()
