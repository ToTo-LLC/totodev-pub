# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The examples have to actually run.

An example nobody executes is a claim about the API, not a fact about it — and
it rots silently, because nothing fails when the API moves underneath it. These
drive the examples' own entry points rather than reimplementing them.
"""

import asyncio
from pathlib import Path

import pytest

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.case_manager_host import serve
from totodev_pub.case_manager_support.mailbox import MailboxTransport
from totodev_pub.case_manager_support.examples import (
    example_02_bag_runner,
    example_03_request_serving_host,
    example_04_request_queue_stages,
)
from totodev_pub.case_manager_support.examples.example_cases import (
    EscalationCase,
    InquiryCase,
)
from totodev_pub.case_manager_support.signaling_adapter import SignalingAdapter
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@pytest.mark.asyncio
async def test_the_example_cases_are_what_the_examples_claim(tmp_path):
    """InquiryCase self-completes; EscalationCase waits to be told.

    Both examples lean on that difference to make their point, so a change to
    either FSM should fail here rather than quietly making the prose wrong.
    """
    inquiry = InquiryCase.create_case_in_folder(tmp_path / "inq")
    escalation = EscalationCase.create_case_in_folder(tmp_path / "esc")
    try:
        assert inquiry.case_state == "received"
        assert escalation.case_state == "waiting"
        assert not escalation.case_is_terminal
    finally:
        inquiry.case_detach()
        escalation.case_detach()


@pytest.mark.asyncio
async def test_the_bag_runner_drives_a_seeded_bag_to_completion(tmp_path):
    """Example 02, end to end: seed, load, run until the pool drains, exit 0."""
    bag = example_02_bag_runner.seed_bag(tmp_path / "bag", count=3)
    assert len(list(bag.iterdir())) == 3

    from totodev_pub.case_manager_support.bag_loading import load_case_bag

    case_type_registry.register_case_types(InquiryCase)
    manager, report = await load_case_bag(
        bag,
        tmp_path / "fleet",
        maintenance_interval_secs=0.01,
    )
    assert len(report.adopted) == 3 and report.all_adopted

    # stop_when_empty is what makes this a job rather than a service: serve()
    # returns (process exit 0) once every case reaches a terminal state.
    await asyncio.wait_for(serve(manager, stop_when_empty=True), timeout=60.0)

    assert len(manager._driver) == 0
    assert [r.case_id for r in manager.iter_terminal()], "the cases were archived"


@pytest.mark.asyncio
async def test_a_minimal_host_serves_no_requests_but_still_stops(tmp_path):
    """Example 01's shape: no adapter attached is supported, not degraded.

    Shutdown still works because the host owns it — which is the whole reason
    that combination is safe to ship.
    """
    store = CaseManager.open_local_store(tmp_path / "fleet", maintenance_interval_secs=0.01)
    case_type_registry.register_case_types(InquiryCase, EscalationCase)
    manager = CaseManager(store)
    task = asyncio.ensure_future(serve(manager, stop_when=lambda: manager.is_running))
    try:
        await asyncio.wait_for(task, timeout=30.0)
    finally:
        if not task.done():
            task.cancel()
    assert not manager.is_running


@pytest.mark.asyncio
async def test_the_request_serving_host_completes_the_client_round_trip(tmp_path):
    """Example 03, end to end: adopt then fire, both across the transport.

    EscalationCase only moves when told, so a passing assertion here means the
    request really travelled — it could not have advanced on its own.
    """
    root = tmp_path / "fleet"
    root.parent.mkdir(parents=True, exist_ok=True)

    store = CaseManager.open_local_store(root, maintenance_interval_secs=0.01)
    case_type_registry.register_case_types(InquiryCase, EscalationCase)
    manager = CaseManager(store)
    # Stop by asking, not by cancelling: a cancelled serve() never reaches its
    # own teardown, which leaks the watchdog thread into every later test.
    done = {"v": False}
    host = asyncio.ensure_future(
        serve(manager, adapter=SignalingAdapter(manager), stop_when=lambda: done["v"])
    )
    try:
        for _ in range(300):  # wait for the host to come up
            if manager.is_running:
                break
            await asyncio.sleep(0.02)
        assert manager.is_running

        case_id = await asyncio.wait_for(
            example_03_request_serving_host.run_client(root), timeout=60.0
        )
        assert case_id, "the adopt request completed"

        # `approved` is terminal, so the case departs the pool on its own; the
        # store is where the outcome is durable.
        client = CaseManagerClient(root)
        located = client.locate(case_id=case_id)
        assert located is not None, "the fire request reached a manual edge and moved it"
        assert located.terminal, "a manual edge only moves when a request reaches it"
    finally:
        done["v"] = True
        await asyncio.wait_for(host, timeout=30.0)
    assert not manager.is_running


@pytest.mark.asyncio
async def test_the_queue_stages_example_drains_every_fire_through_one_choke(tmp_path):
    """Example 04, end to end: three fires, one permit, all answered.

    The example's value is its printed table, which a test cannot assert on. What
    it *can* assert is the property the table illustrates — that a one-permit
    choke serialises three concurrent fires and still answers all of them, with
    every stage move made by the adapter rather than by the script.

    Run with a short step and a fast tick: the example's own timings exist to make
    the stages watchable by a human, which is exactly the wrong trade in CI.
    """
    example = example_04_request_queue_stages
    monkey_step = 0.05
    original = example.STEP_SECS
    example.STEP_SECS = monkey_step
    try:
        await asyncio.wait_for(example.main(tmp_path / "fleet", case_count=3), timeout=60.0)
    finally:
        example.STEP_SECS = original

    # Every case reached the terminal state, which for a manual edge is only
    # reachable by a fire that actually travelled through the queue.
    client = CaseManagerClient(tmp_path / "fleet")
    transport = MailboxTransport(client._manager._manager_dir, client._manager._policy)
    assert len(list(transport.results_dir().glob("*.yaml"))) == 3, "all three answered"
    assert list(transport.queued().glob("*.yaml")) == [], "queue fully drained"
    assert list(transport.claimed().rglob("*.yaml")) == [], "nothing stranded in claimed/"
    assert list(transport.running().rglob("*.yaml")) == [], "nothing stranded in running/"


def test_the_stage_counter_sees_per_case_subdirectories(tmp_path):
    """``claimed/`` and ``running/`` nest one directory per case.

    A flat ``glob`` there would report zero forever, so the example's table would
    show an empty queue no matter what the fleet was doing — a demo that lies
    rather than one that fails.
    """
    manager = CaseManager(CaseManager.open_local_store(tmp_path / "fleet"))
    transport = MailboxTransport(manager._manager_dir, manager._policy)
    transport.ensure_dirs()
    nested = transport.claimed("some-case")
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "req.yaml").write_text("op: fire\n", encoding="utf-8")

    counts = example_04_request_queue_stages.stage_counts(transport)

    assert counts["claimed"] == 1, "a per-case subdirectory must be counted"
    assert counts["queued"] == 0


def test_the_readme_lists_every_example(tmp_path):
    """A README that has drifted from the directory is worse than none."""
    examples_dir = Path(example_02_bag_runner.__file__).parent
    readme = (examples_dir / "README-case_manager.md").read_text(encoding="utf-8")
    scripts = sorted(p.stem for p in examples_dir.glob("example_0*.py"))
    missing = [name for name in scripts if name not in readme]
    assert missing == [], f"README does not mention: {missing}"
