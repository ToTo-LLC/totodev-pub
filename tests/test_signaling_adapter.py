# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The adapter drives the manager; the manager knows nothing about transport.

These pin the layering itself, not just the request paths: a manager with no
adapter attached still works, the adapter reaches the manager only through
public methods, and one bad request cannot take the batch down with it.
"""

import asyncio
from pathlib import Path

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    attach_adapter,
    provision_manager,
    seed_detached_case,
    transport_for,
)
from totodev_pub.case_manager_support.mailbox import (
    MailboxTransport,
    ReclassifyResult,
    RequestHandle,
)
from totodev_pub.case_manager_support.adopt import AdoptResult
from totodev_pub.case_manager_support.advance_result_serializable import (
    AdvanceResultSerializable,
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


async def _live_case(manager, tmp_path, name="inbound"):
    staging = tmp_path / name
    seed_detached_case(TicketCase, staging)
    return await adopt_into_live(manager, staging)


# ------------------------------------------------------------------ layering


def test_the_manager_has_no_transport_surface(tmp_path):
    """The fleet no longer owns a mailbox — that was the point of the extraction."""
    manager = provision_manager(tmp_path)
    assert not hasattr(manager, "_mailbox")


@pytest.mark.asyncio
async def test_a_manager_with_no_adapter_runs_normally(tmp_path):
    """No transport attached is a supported shape, not a degraded one.

    It is what an embedded host or a test harness looks like, and it is the
    reason the manager's own methods have to be a complete API.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    try:
        case = await _live_case(manager, tmp_path)
        result = await manager.fire(case_id=case.case_id, trigger="work")
        assert result.progressed
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_the_adapter_only_touches_public_manager_methods(tmp_path, monkeypatch):
    """Inverting the dependency is only real if the adapter cannot cheat.

    Handed the whole manager, a transport reaches into its internals and the
    public API never has to be complete. Here the private surface is booby-
    trapped, so any such reach fails loudly.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    case = await _live_case(manager, tmp_path)
    adapter = attach_adapter(manager)
    await manager.start()

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"the adapter reached into manager._driver.{name}")

    monkeypatch.setattr(manager, "_driver", Forbidden(), raising=False)
    try:
        adapter.transport.submit_fire(case_id=case.case_id, trigger="work")
        await adapter.maintenance_tick()   # must not touch manager._driver
    finally:
        monkeypatch.undo()
        await manager.stop()


# -------------------------------------------------------- per-request isolation


@pytest.mark.asyncio
async def test_one_exploding_request_does_not_poison_the_batch(tmp_path, monkeypatch):
    """Requests behind a bad one must still be served.

    A poisoned batch means their submitters wait out their whole timeout with no
    way to tell "still queued" from "dropped".
    """
    manager = provision_manager(tmp_path)
    await manager.recover()
    adapter = attach_adapter(manager)
    transport = adapter.transport

    good = tmp_path / "good"
    seed_detached_case(TicketCase, good)
    bad = tmp_path / "bad"
    seed_detached_case(TicketCase, bad)

    real = manager.adopt_case
    calls = {"n": 0}

    async def explode_once(source_folder, **kwargs):
        calls["n"] += 1
        if Path(source_folder).name == "bad":
            raise RuntimeError("adopt blew up")
        return await real(source_folder, **kwargs)

    monkeypatch.setattr(manager, "adopt_case", explode_once)

    bad_handle = transport.submit_adopt(source_folder=bad)
    good_handle = transport.submit_adopt(source_folder=good)

    await adapter.maintenance_tick()

    bad_result = transport.poll_result(bad_handle)
    good_result = transport.poll_result(good_handle)
    assert bad_result is not None, "the failing request still gets an answer"
    assert bad_result.status == "error"
    assert "adopt blew up" in bad_result.rejection_reason
    assert good_result is not None and good_result.status == "completed", (
        "the request behind it was still served"
    )


@pytest.mark.asyncio
async def test_a_malformed_request_gets_an_error_result_not_silence(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    adapter = attach_adapter(manager)
    transport = adapter.transport

    corr = "garbage-request"
    (transport.fire_intake() / f"{corr}.yaml").write_text("{[ not yaml", encoding="utf-8")

    await adapter.maintenance_tick()

    result = transport.poll_result(RequestHandle(corr, transport.result_path(corr)))
    assert isinstance(result, AdvanceResultSerializable)
    assert result.status == "error"
    assert not (transport.fire_intake() / f"{corr}.yaml").exists(), "dead-lettered"
    assert (transport.fire_stage("malformed") / f"{corr}.yaml").exists()


# ------------------------------------------------------------- correlation ids


@pytest.mark.asyncio
async def test_a_redelivered_adopt_is_answered_from_the_existing_result(tmp_path):
    """Idempotency is transport business; the fleet has no opinion about retries."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    adapter = attach_adapter(manager)
    transport = adapter.transport

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    handle = transport.submit_adopt(source_folder=staging, correlation_id="corr-1")
    await adapter.maintenance_tick()
    first = transport.poll_result(handle)
    assert first is not None and first.status == "completed"

    # Same correlation id again: no second adopt, same answer.
    transport.submit_adopt(source_folder=staging, correlation_id="corr-1")
    await adapter.maintenance_tick()
    second = transport.poll_result(handle)
    assert second is not None
    assert second.case_folder == first.case_folder


def test_adopt_case_carries_no_correlation_id(tmp_path):
    """The manager's own adopt is about the fleet, not about a client's retries."""
    import inspect

    manager = provision_manager(tmp_path)
    params = inspect.signature(manager.adopt_case).parameters
    assert "correlation_id" not in params


# ------------------------------------------------------------------- results


@pytest.mark.asyncio
async def test_results_are_identified_by_kind_not_by_sniffing(tmp_path):
    """Every result carries a discriminator, so reading one is asking, not guessing.

    Substring sniffing works right up until a case_id or folder name happens to
    contain the word being sniffed for.
    """
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    transport.ensure_dirs()

    # A folder name containing every substring the old sniffing keyed on.
    transport.publish_result(
        "c1",
        AdoptResult(
            status="completed",
            case_id="reclassify-shutdown-adopt",
            source_folder="/tmp/kind: shutdown/adopt",
            correlation_id="c1",
        ),
    )
    result = transport.poll_result(RequestHandle("c1", transport.result_path("c1")))
    assert isinstance(result, AdoptResult), f"misidentified as {type(result).__name__}"

    transport.publish_result(
        "c2", ReclassifyResult(status="completed", correlation_id="c2")
    )
    assert isinstance(
        transport.poll_result(RequestHandle("c2", transport.result_path("c2"))),
        ReclassifyResult,
    )


def test_polling_an_unwritten_result_is_not_an_error(tmp_path):
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    handle = RequestHandle("nobody", transport.result_path("nobody"))
    assert transport.poll_result(handle) is None


def test_polling_a_half_written_result_reads_as_not_ready(tmp_path):
    """A partial file is indistinguishable from a missing one, from a poller's view."""
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    transport.ensure_dirs()
    transport.result_path("torn").write_text("kind: adopt\nstatus: comp", encoding="utf-8")
    assert transport.poll_result(RequestHandle("torn", transport.result_path("torn"))) is None


# ------------------------------------------------------------------- recovery


@pytest.mark.asyncio
async def test_recovery_dead_letters_a_fire_caught_mid_flight(tmp_path):
    """A request in firing/ was executing when the process died; its outcome is
    unknown, so the submitter is told so rather than left waiting."""
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    transport.ensure_dirs()

    handle = transport.submit_fire(case_id="c-1", trigger="work", correlation_id="mid")
    firing = transport.fire_stage("firing", "c-1")
    firing.mkdir(parents=True, exist_ok=True)
    (transport.fire_intake() / "mid.yaml").replace(firing / "mid.yaml")

    report = SignalingAdapter(manager).recover()

    assert report.fire_replayed == 1
    result = transport.poll_result(handle)
    assert result is not None and result.status == "error"
    assert "dead-letter" in result.exception_messages[0]


@pytest.mark.asyncio
async def test_recovery_requeues_a_fire_that_never_launched(tmp_path):
    """Attached but never launched is safe to simply run again."""
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    transport.ensure_dirs()

    transport.submit_fire(case_id="c-1", trigger="work", correlation_id="queued")
    pending = transport.fire_stage("pending", "c-1")
    pending.mkdir(parents=True, exist_ok=True)
    (transport.fire_intake() / "queued.yaml").replace(pending / "queued.yaml")

    report = SignalingAdapter(manager).recover()

    assert report.fire_replayed == 1
    assert (transport.fire_intake() / "queued.yaml").exists(), "back in the queue"


@pytest.mark.asyncio
async def test_recovery_settles_a_reclassify_caught_mid_execution(tmp_path):
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    transport.ensure_dirs()

    handle = transport.submit_reclassify(
        case_id="c-1", target_type="TicketCase", correlation_id="mid-reclass"
    )
    executing = transport.reclassify_stage("executing", "c-1")
    executing.mkdir(parents=True, exist_ok=True)
    (transport.reclassify_intake() / "mid-reclass.yaml").replace(executing / "mid-reclass.yaml")

    report = SignalingAdapter(manager).recover()

    assert report.reclassify_dead_lettered == 1
    result = transport.poll_result(handle)
    assert isinstance(result, ReclassifyResult)
    assert result.status == "error" and "dead-letter" in result.error


# --------------------------------------------------------------- backlog age


def test_the_backlog_age_excludes_shutdown(tmp_path):
    """Shutdown has its own pickup path; counting it would report neglect the
    instant a shutdown is submitted."""
    manager = provision_manager(tmp_path)
    transport = transport_for(manager)
    transport.ensure_dirs()

    assert transport.oldest_request_age_secs() is None
    transport.submit_shutdown()
    assert transport.oldest_request_age_secs() is None, "shutdown is not a backlog"

    transport.submit_fire(case_id="c-1", trigger="work")
    age = transport.oldest_request_age_secs()
    assert age is not None and age >= 0
