# Part of the totodev_pub library.

"""Tests for CaseManager.reclassify_case and the reclassify mailbox protocol."""

import pytest

from case_manager_test_utils import transport_for,  attach_adapter, adopt_into_live, seed_detached_case
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.exceptions import LiveCaseNotFoundError
from totodev_pub.case_manager_support.mailbox import RequestHandle, ReclassifyResult
from totodev_pub.folder_backed_case import FolderBackedCase, IncompatibleReclassError
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry,
    case_type_registry,
)
from totodev_pub.folder_backed_case_support.exceptions import UnregisteredCaseTypeError
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import Tier


class IntakeCase(FolderBackedCase):
    """Generic intake meant to be reclassified out of existence at 'sorted'."""
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = [
        "[*] --> start -- sort --> sorted",
        "sorted == never ==> never_reached --> [*]",
    ]

    async def perform_sort(self, tctx):
        pass


class RoutedCase(FolderBackedCase):
    """Specialized continuation; shares the 'sorted' handoff state (as initial)."""
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> sorted -- finish --> done --> [*]"]

    async def perform_finish(self, tctx):
        pass


class UnrelatedCase(FolderBackedCase):
    """No shared states with IntakeCase — reclassify must be refused."""
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> alpha -- go --> omega --> [*]"]

    async def perform_go(self, tctx):
        pass


class StrictRoutedCase(FolderBackedCase):
    """Shares 'sorted' with IntakeCase but rejects the handoff via assertions."""
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> sorted -- finish --> done --> [*]"]

    def case_assert_sorted_has_route(self, ltx):
        return "missing route"

    def case_assert_sorted_has_owner(self, ltx):
        return "missing owner"

    async def perform_finish(self, tctx):
        pass


def provision(tmp_path, **overrides) -> CaseManager:
    store = CaseManager.open_local_store(
        tmp_path / "cache",
        maintenance_interval_secs=0.01,
        **overrides,
    )
    case_type_registry.register_case_types(
        IntakeCase, RoutedCase, UnrelatedCase, StrictRoutedCase
    )
    return CaseManager(store)


async def seed_parked_intake(manager: CaseManager, tmp_path):
    """Adopt an IntakeCase and drive it to its parked 'sorted' handoff state."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    seed_detached_case(IntakeCase, staging / "c1")
    case = await adopt_into_live(manager, staging / "c1")
    # Immediate driver primitive — manager.fire() requires a running loop.
    result = await manager._driver.fire(case.case_folder, None)
    assert result.progressed and result.final_state == "sorted"
    return manager.get_live(case.case_id)


@pytest.mark.asyncio
async def test_reclassify_case_swaps_type_and_admits_hot(tmp_path):
    manager = provision(tmp_path)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    case_id, folder = case.case_id, case.case_folder

    fresh = await manager.reclassify_case(case_id=case_id, target_type="RoutedCase")

    assert isinstance(fresh, RoutedCase)
    assert fresh.case_id == case_id                      # identity preserved
    assert fresh.case_state == "sorted"                  # state preserved
    assert isinstance(manager.get_live(case_id), RoutedCase)   # pool serves new class
    slot = manager._driver._by_folder[folder]
    assert slot.tier is Tier.HOT                         # fresh admission slot is HOT
    assert slot.skip_countdown == 1                      # boosted: fires next beat

    # The newly available auto path is actually taken.
    result = await manager._driver.fire(folder, None)
    assert result.progressed and result.final_state == "done"


@pytest.mark.asyncio
async def test_reclassify_case_accepts_class_object(tmp_path):
    manager = provision(tmp_path)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    fresh = await manager.reclassify_case(case_id=case.case_id, target_type=RoutedCase)
    assert isinstance(fresh, RoutedCase)


@pytest.mark.asyncio
async def test_reclassify_case_incompatible_state_leaves_pool_untouched(tmp_path):
    manager = provision(tmp_path)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    with pytest.raises(IncompatibleReclassError):
        await manager.reclassify_case(case_id=case.case_id, target_type="UnrelatedCase")
    still = manager.get_live(case.case_id)               # never removed from the pool
    assert isinstance(still, IntakeCase)
    assert still.case_state == "sorted"


@pytest.mark.asyncio
async def test_reclassify_case_unregistered_type_rejected(tmp_path):
    manager = provision(tmp_path)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    with pytest.raises(UnregisteredCaseTypeError):
        await manager.reclassify_case(case_id=case.case_id, target_type="NoSuchCase")


@pytest.mark.asyncio
async def test_reclassify_case_quarantines_when_target_assertions_fail(tmp_path):
    from totodev_pub.case_manager_support.case_store import QUARANTINED
    from totodev_pub.case_manager_support.quarantine import EV_QUARANTINED
    from totodev_pub.folder_backed_case_support.folder_backed_case_reader import FolderBackedCaseReader
    from totodev_pub.folder_backed_case_support.exceptions import ReclassifyAssertionError

    manager = provision(tmp_path)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    case_id = case.case_id

    with pytest.raises(ReclassifyAssertionError) as ei:
        await manager.reclassify_case(case_id=case_id, target_type="StrictRoutedCase")
    assert [f.name for f in ei.value.failures] == ["has_owner", "has_route"]

    assert case_id not in [c.case_id for c in manager._driver]
    assert case_id in [r.case_id for r in manager.iter_quarantine()]
    assert manager._store.status_of(case_id) == QUARANTINED
    loc = manager.locate(case_id=case_id)
    assert loc is not None
    reader = FolderBackedCaseReader(loc.case_folder)
    assert reader.case_object_type == "StrictRoutedCase"
    labels = [ev.label for ev in reader.case_event_journal.primitive.events()]
    assert EV_QUARANTINED in labels
    quarantine_ev = next(
        ev for ev in reader.case_event_journal.primitive.events()
        if ev.label == EV_QUARANTINED
    )
    assert "missing route" in quarantine_ev.value
    assert "missing owner" in quarantine_ev.value


@pytest.mark.asyncio
async def test_reclassify_mailbox_submit_completes(tmp_path):
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    attach_adapter(manager)
    await manager.start()
    try:
        client = CaseManagerClient(tmp_path / "cache")
        handle = client.submit_reclassify(
            case_id=case.case_id, target_type="RoutedCase", only_if_fresh=False,
        )
        result = await client.wait_result(handle, timeout=5.0)
        assert isinstance(result, ReclassifyResult)
        assert result.status == "completed"
        assert result.from_type == "IntakeCase"
        assert result.to_type == "RoutedCase"
        assert result.case_state == "sorted"
        reader = client.reader(case_id=case.case_id)
        assert reader.case_object_type == "RoutedCase"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reclassify_mailbox_error_result_for_incompatible_target(tmp_path):
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    attach_adapter(manager)
    await manager.start()
    try:
        client = CaseManagerClient(tmp_path / "cache")
        # Bypass client-side preflight so the (still-authoritative) manager-side
        # rejection is what's under test here.
        handle = client.submit_reclassify(
            case_id=case.case_id, target_type="UnrelatedCase", only_if_fresh=False,
            preflight=False,
        )
        result = await client.wait_result(handle, timeout=5.0)
        assert isinstance(result, ReclassifyResult)
        assert result.status == "error"
        assert result.error
        # The case is untouched and still managed.
        reader = client.reader(case_id=case.case_id)
        assert reader.case_object_type == "IntakeCase"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reclassify_mailbox_malformed_request_gets_error_result_not_silence(tmp_path):
    """A request that fails to parse must still resolve the caller's wait_result —
    the correlation id lives in the filename, so an error result is derivable even when
    the body is garbage.

    The *shape* of that error now depends on how broken the request is, which is a
    consequence of moving the action out of the folder path and into the file. A
    body that still parses as YAML has a readable ``op``, so the submitter gets the
    result type it is polling for. See the companion test below for the case where
    even that is unreadable.
    """
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    mailbox = transport_for(manager)
    mailbox.ensure_dirs()
    corr = "malformed-corr-id"
    # Valid YAML naming its op, but a payload that will not validate.
    (mailbox.queued() / f"{corr}.yaml").write_text(
        f"op: reclassify\nid: {corr}\nrequested_at: 'x'\npayload: not-a-mapping\n",
        encoding="utf-8",
    )
    attach_adapter(manager)
    await manager.start()
    try:
        client = CaseManagerClient(tmp_path / "cache")
        handle = RequestHandle(corr, mailbox.results_dir() / f"{corr}.yaml")
        result = await client.wait_result(handle, timeout=5.0)
        assert isinstance(result, ReclassifyResult)
        assert result.status == "error"
        assert "malformed" in result.error
        # The bad file was quarantined, not left in the queue.
        assert not (mailbox.queued() / f"{corr}.yaml").exists()
        assert (mailbox.failed() / f"{corr}.yaml").exists()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_an_unparseable_request_gets_the_generic_error_shape(tmp_path):
    """A file too broken to name its own op is answered generically, on purpose.

    This is the one thing the request queue gives up relative to four mailboxes:
    the folder used to carry the action, so even total garbage could be answered
    in the right shape. Now the action lives inside the file, and a file that will
    not parse as YAML has no readable ``op``.

    Answering generically is the honest outcome — inventing an op would answer a
    question nobody can prove was asked. What still holds, and matters more, is
    that the submitter gets a terminal error rather than silence.
    """
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    mailbox = transport_for(manager)
    mailbox.ensure_dirs()
    corr = "unparseable-corr-id"
    (mailbox.queued() / f"{corr}.yaml").write_text("{[ not yaml at all", encoding="utf-8")
    attach_adapter(manager)
    await manager.start()
    try:
        client = CaseManagerClient(tmp_path / "cache")
        handle = RequestHandle(corr, mailbox.results_dir() / f"{corr}.yaml")
        result = await client.wait_result(handle, timeout=5.0)
        assert result is not None, "silence is the one unacceptable answer"
        assert result.status == "error"
        assert (mailbox.failed() / f"{corr}.yaml").exists()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_client_preflight_rejects_unknown_case_without_touching_mailbox(tmp_path):
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    client = CaseManagerClient(tmp_path / "cache")
    with pytest.raises(LiveCaseNotFoundError):
        client.submit_reclassify(
            case_id="does-not-exist", target_type="RoutedCase", only_if_fresh=False,
        )
    assert list(transport_for(manager).queued().glob("*.yaml")) == []


@pytest.mark.asyncio
async def test_client_preflight_rejects_incompatible_state_without_touching_mailbox(tmp_path):
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    client = CaseManagerClient(tmp_path / "cache")
    with pytest.raises(IncompatibleReclassError):
        client.submit_reclassify(
            case_id=case.case_id, target_type="UnrelatedCase", only_if_fresh=False,
        )
    # Rejected before ever reaching the mailbox.
    assert list(transport_for(manager).queued().glob("*.yaml")) == []
    reader = client.reader(case_id=case.case_id)
    assert reader.case_object_type == "IntakeCase"


@pytest.mark.asyncio
async def test_client_preflight_defers_when_type_unresolvable_in_this_process(tmp_path):
    """Simulates the common two-process split: the client's registry never saw the case
    classes, so the class/state check is skipped rather than raising a false positive,
    and the (still-invalid) request is queued for the manager's authoritative check."""
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    client = CaseManagerClient(tmp_path / "cache")
    client._manager._registry = CaseTypeRegistry()      # empty: knows no case types at all
    handle = client.submit_reclassify(
        case_id=case.case_id, target_type="UnrelatedCase", only_if_fresh=False,
    )
    assert list(transport_for(manager).queued().glob("*.yaml"))   # queued, not rejected
    assert handle.correlation_id


@pytest.mark.asyncio
async def test_client_preflight_strict_rejects_unresolvable_type_locally(tmp_path):
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    client = CaseManagerClient(tmp_path / "cache")
    client._manager._registry = CaseTypeRegistry()
    with pytest.raises(UnregisteredCaseTypeError):
        client.submit_reclassify(
            case_id=case.case_id, target_type="RoutedCase", only_if_fresh=False,
            preflight="strict",
        )
    assert list(transport_for(manager).queued().glob("*.yaml")) == []


@pytest.mark.asyncio
async def test_client_preflight_false_submits_unconditionally(tmp_path):
    manager = provision(tmp_path, enable_mailbox=True)
    await manager.recover()
    case = await seed_parked_intake(manager, tmp_path)
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_reclassify(
        case_id=case.case_id, target_type="UnrelatedCase", only_if_fresh=False,
        preflight=False,
    )
    assert list(transport_for(manager).queued().glob("*.yaml"))
    assert handle.correlation_id
