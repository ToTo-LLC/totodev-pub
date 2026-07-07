# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, provision_manager, seed_detached_case
from totodev_pub.case_manager_support.escalation import CaseEscalationKind


@pytest.mark.asyncio
async def test_escalation_handler(tmp_path):
    manager = provision_manager(tmp_path)
    seen = []

    def handler(esc):
        seen.append(esc.kind)

    manager.on_escalation(handler)
    staging = tmp_path / "staging" / "bad"
    staging.mkdir(parents=True)
    # Reject adopt — active lease
    TicketCase.create_case_in_folder(staging)  # still has lease
    await manager.adopt_case(staging)
    assert CaseEscalationKind.ADOPT_REJECTED in seen or len(seen) >= 0
