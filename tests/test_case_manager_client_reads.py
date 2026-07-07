# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case
from totodev_pub.case_manager_client import CaseManagerClient


@pytest.mark.asyncio
async def test_client_reads_without_manager_running(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1", external_key="EXT-1")
    case = await adopt_into_live(manager, staging / "c1")
    client = CaseManagerClient(tmp_path / "cache")
    loc = client.locate(case_id=case.case_id)
    assert loc is not None
    reader = client.reader(case_id=case.case_id)
    assert reader.case_id == case.case_id
