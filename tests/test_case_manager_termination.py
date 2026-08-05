# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import TerminalCase, adopt_into_live, provision_manager, run, seed_detached_case


@pytest.mark.asyncio
async def test_termination_moves_to_terminal(tmp_path):
    manager = provision_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TerminalCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    await manager.start()
    await manager.fire(case_id=case.case_id, trigger="finish")
    # Allow maintenance to process termination ticket
    for _ in range(20):
        await asyncio.sleep(0.05)
        loc = manager.locate(case_id=case.case_id)
        if loc and loc.terminal and not loc.in_pool:
            if loc.status == "terminated":
                break
    await manager.stop()
    loc = manager.locate(case_id=case.case_id)
    assert loc is not None
    assert loc.terminal
    assert loc.status == "terminated"
