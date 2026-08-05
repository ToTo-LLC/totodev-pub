# Part of the totodev_pub library.

import asyncio
import os

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    attach_adapter,
    transport_for,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.exceptions import ManagerNotRunningError
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.mark.asyncio
async def test_fire_mailbox_submit(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    attach_adapter(manager)
    await manager.start()
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_fire(case_id=case.case_id, trigger="work", only_if_fresh=False)
    result = await client.wait_result(handle, timeout=5.0)
    assert result is not None
    await manager.stop()


class ChainCase(FolderBackedCase):
    """Three manual edges in sequence: only the FIFO order a, b, c can walk to s3."""
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> s0 == a ==> s1 == b ==> s2 == c ==> s3 --> [*]"]


@pytest.mark.asyncio
async def test_fire_intake_drains_fifo_by_arrival(tmp_path):
    """Intake filenames are random correlation-id UUIDs; the drain must be FIFO by
    arrival time, not filename-lexicographic. Correlation ids here are chosen in
    REVERSE lexicographic order of submission, so a name-sorted drain would try the
    triggers backwards (c from s0) and never reach s3."""
    store = CaseManager.open_local_store(
        tmp_path / "cache",
        maintenance_interval_secs=0.01,
        enable_mailbox=True,
    )
    case_type_registry.register_case_types(ChainCase)
    manager = CaseManager(store)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ChainCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")

    transport = transport_for(manager)
    intake = transport.fire_intake()
    base = 1_000_000_000.0
    for i, (corr, trig) in enumerate([("zz", "a"), ("mm", "b"), ("aa", "c")]):
        transport.submit_fire(case_id=case.case_id, trigger=trig, correlation_id=corr)
        os.utime(intake / f"{corr}.yaml", (base + i, base + i))   # pin arrival order

    attach_adapter(manager)
    await manager.start()
    try:
        results_dir = transport.results_dir()
        for _ in range(200):
            if all((results_dir / f"{c}.yaml").exists() for c in ("zz", "mm", "aa")):
                break
            await asyncio.sleep(0.05)
        assert case.case_state == "s3"
        for corr in ("zz", "mm", "aa"):
            text = (results_dir / f"{corr}.yaml").read_text()
            assert "status: completed" in text
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_fire_requires_running(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    with pytest.raises(ManagerNotRunningError):
        await manager.fire(case_id=case.case_id, trigger="work")


@pytest.mark.asyncio
async def test_manager_fire_through_running_loop(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    attach_adapter(manager)
    await manager.start()
    try:
        result = await manager.fire(case_id=case.case_id, trigger="work")
        assert result.progressed
        assert case.case_state == "done"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_fire_wait_false_returns_immediately(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    attach_adapter(manager)
    await manager.start()
    completed: list[object] = []
    try:
        result = await manager.fire(
            case_id=case.case_id,
            trigger="work",
            wait=False,
            on_complete=lambda r, e: completed.append((r, e)),
        )
        assert result is None
        for _ in range(200):
            if completed:
                break
            await asyncio.sleep(0.05)
        assert len(completed) == 1
        advance, error = completed[0]
        assert error is None
        assert advance is not None and advance.progressed
        assert case.case_state == "done"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_fire_wait_true_additive_on_complete(tmp_path):
    manager = provision_manager(tmp_path, enable_mailbox=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    attach_adapter(manager)
    await manager.start()
    seen: list[object] = []
    try:
        result = await manager.fire(
            case_id=case.case_id,
            trigger="work",
            on_complete=lambda r, e: seen.append((r, e)),
        )
        assert result is not None and result.progressed
        assert len(seen) == 1
        cb_result, cb_error = seen[0]
        assert cb_error is None
        assert cb_result is result
        assert case.case_state == "done"
    finally:
        await manager.stop()
