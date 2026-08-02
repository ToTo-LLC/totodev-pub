# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseStore: location is the projection of pool-activity-status.

These are the boundary's own tests — no CaseManager, no pool driver. They pin
the store's contract directly: what statuses it can write, what it refuses, what
its lookups cost in reads, and the point-in-time nature of everything it hands
back.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from case_manager_test_utils import TicketCase, seed_detached_case
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.case_store import (
    LIVE,
    QUARANTINED,
    TERMINATED,
    LocalCaseStore,
)
from totodev_pub.case_manager_support.exceptions import (
    CaseLeaseHeldError,
    CaseNotInStoreError,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    case_type_registry.register_case_types(TicketCase)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@pytest.fixture
def store(tmp_path) -> LocalCaseStore:
    return LocalCaseStore.provision(tmp_path / "cache", CaseManagerPolicy())


async def _seed_live(store: LocalCaseStore, tmp_path: Path, case_id: str = "c-1") -> Path:
    """Put a real, detached case into the store at live status.

    The store's key and the case record's own ``case_id`` must agree — adopt
    derives one from the other — so the seeded case is created with the id the
    store will be asked about.
    """
    source = tmp_path / f"src-{case_id}"
    case = TicketCase.create_case_in_folder(source, case_id=case_id)
    case.case_detach()
    return await store.absorb_orphan(case_id, source, status=LIVE)


# ----------------------------------------------------------------- the boundary


def test_no_storage_mechanics_leak_out_of_the_store():
    """The rule that makes this a boundary rather than a pass-through.

    A concrete wrapper makes it very easy to let cache mechanics creep back into
    public methods, producing a chokepoint that is not an abstraction. This is
    the check that would otherwise have to be remembered on every change.
    """
    import totodev_pub.case_manager_support as support_pkg

    package_dir = Path(support_pkg.__file__).parent
    forbidden = ("CachedFileFolders", "ref_path", "grouping_key", "slave_dir", "PLACEHOLDER")
    # These two *declare* the layout facts — the persisted policy and its
    # defaults — which is different from using them. The store is the only
    # module allowed to act on them.
    allowed = {"case_store.py", "constants.py", "case_manager_policy.py"}

    offenders: dict[str, list[str]] = {}
    sources = [Path(support_pkg.__file__).parents[1] / "case_manager.py"]
    sources += [p for p in package_dir.rglob("*.py") if p.name not in allowed]
    for path in sources:
        hits = [term for term in forbidden if term in path.read_text(encoding="utf-8")]
        if hits:
            offenders[path.name] = hits

    assert offenders == {}, (
        "storage mechanics escaped the CaseStore boundary: "
        f"{offenders}. Express it in case_id / status / Path instead."
    )


# ---------------------------------------------------------------- provisioning


def test_provision_creates_the_live_bucket(tmp_path):
    store = LocalCaseStore.provision(tmp_path / "cache", CaseManagerPolicy())
    assert (store.root_dir / "live").is_dir()


def test_provision_is_idempotent(tmp_path):
    policy = CaseManagerPolicy()
    first = LocalCaseStore.provision(tmp_path / "cache", policy)
    second = LocalCaseStore.provision(tmp_path / "cache", policy)
    assert first.root_dir == second.root_dir


def test_provision_rejects_a_different_layout_at_the_same_root(tmp_path):
    LocalCaseStore.provision(tmp_path / "cache", CaseManagerPolicy())
    with pytest.raises(Exception):
        LocalCaseStore.provision(
            tmp_path / "cache", CaseManagerPolicy(grouping_pattern="{a}/{b}/")
        )


# ----------------------------------------------------------------- status moves


@pytest.mark.asyncio
async def test_a_new_case_starts_live(store, tmp_path):
    folder = await store.create_location("c-1")
    entry = store.find("c-1")
    assert entry is not None
    assert entry.status == LIVE
    assert "partition" not in entry.__dataclass_fields__
    assert entry.case_folder == folder


@pytest.mark.asyncio
async def test_set_status_moves_the_folder_and_reports_the_new_one(store, tmp_path):
    old = await _seed_live(store, tmp_path)
    new = await store.set_status("c-1", TERMINATED)

    assert new != old, "location is the projection of status; it must move"
    assert not old.exists()
    assert (new / "case_record.yaml").exists(), "case content came along"
    entry = store.find("c-1")
    assert entry.status == TERMINATED
    assert entry.activity_at is not None
    # Non-live path encodes YYYY-MM/YYYY-MM-DD under the status grouping.
    assert TERMINATED in new.parts
    rel = new.relative_to(store.root_dir / TERMINATED)
    assert len(rel.parts) >= 2
    assert rel.parts[0].count("-") == 1  # YYYY-MM


@pytest.mark.asyncio
async def test_quarantined_uses_quarantined_bucket(store, tmp_path):
    await _seed_live(store, tmp_path)
    new = await store.set_status("c-1", QUARANTINED)
    assert (store.root_dir / "quarantined").is_dir()
    assert "quarantined" in new.parts
    assert not (store.root_dir / "aberrant").exists()


@pytest.mark.asyncio
async def test_unknown_non_live_status_gets_timed_layout(store, tmp_path):
    await _seed_live(store, tmp_path)
    new = await store.set_status("c-1", "hibernating")
    assert store.status_of("c-1") == "hibernating"
    assert "hibernating" in new.parts


@pytest.mark.asyncio
async def test_set_status_is_idempotent(store, tmp_path):
    await _seed_live(store, tmp_path)
    first = await store.set_status("c-1", QUARANTINED)
    second = await store.set_status("c-1", QUARANTINED)
    assert first == second, "a re-driven relocation converges instead of failing"


@pytest.mark.asyncio
async def test_set_status_on_an_unknown_case_raises(store):
    with pytest.raises(CaseNotInStoreError):
        await store.set_status("nobody", QUARANTINED)


# ------------------------------------------------------------------- the lease


@pytest.mark.asyncio
async def test_set_status_refuses_while_the_lease_is_held(store, tmp_path):
    folder = await _seed_live(store, tmp_path)
    case = case_type_registry.rehydrate(folder)   # takes the lease
    try:
        with pytest.raises(CaseLeaseHeldError):
            await store.set_status("c-1", QUARANTINED)
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_force_despite_lease_moves_anyway(store, tmp_path):
    folder = await _seed_live(store, tmp_path)
    case = case_type_registry.rehydrate(folder)
    try:
        moved = await store.set_status("c-1", QUARANTINED, force_despite_lease=True)
        assert moved.exists()
        assert store.status_of("c-1") == QUARANTINED
    finally:
        case.case_detach()


@pytest.mark.asyncio
async def test_export_refuses_while_the_lease_is_held(store, tmp_path):
    folder = await _seed_live(store, tmp_path)
    case = case_type_registry.rehydrate(folder)
    try:
        with pytest.raises(CaseLeaseHeldError):
            await store.export("c-1", tmp_path / "out")
    finally:
        case.case_detach()


# --------------------------------------------------------------------- lookups


@pytest.mark.asyncio
async def test_find_locates_a_case_at_every_status(store, tmp_path):
    await _seed_live(store, tmp_path)
    assert store.find("c-1").status == LIVE
    await store.set_status("c-1", TERMINATED)
    assert store.find("c-1").status == TERMINATED
    await store.set_status("c-1", QUARANTINED)
    assert store.find("c-1").status == QUARANTINED


@pytest.mark.asyncio
async def test_finding_a_live_case_reads_no_case_records(store, tmp_path, monkeypatch):
    """The hot path is one index lookup — never a fleet scan.

    Every addressed fire() resolves through here, so a record read per case in
    the fleet would put an O(fleet) cost on the mailbox path.
    """
    for n in range(5):
        await _seed_live(store, tmp_path, f"c-{n}")

    import totodev_pub.case_manager_support.case_store as case_store_module

    reads: list[Path] = []
    real = case_store_module.read_case_id_from_folder
    monkeypatch.setattr(
        case_store_module,
        "read_case_id_from_folder",
        lambda folder: reads.append(folder) or real(folder),
    )

    assert store.find("c-4").case_id == "c-4"
    assert reads == [], "no case_record.yaml was opened to answer the lookup"


@pytest.mark.asyncio
async def test_find_returns_none_for_an_unknown_case(store):
    assert store.find("nobody") is None
    assert store.contains("nobody") is False
    assert store.status_of("nobody") is None


@pytest.mark.asyncio
async def test_resolve_path_matches_find(store, tmp_path):
    folder = await _seed_live(store, tmp_path)
    assert await store.resolve_path("c-1") == folder
    assert await store.resolve_path("nobody") is None


@pytest.mark.asyncio
async def test_case_id_at_resolves_a_path_inside_the_case_tree(store, tmp_path):
    folder = await _seed_live(store, tmp_path)
    assert store.case_id_at(folder) == "c-1"
    nested = folder / "assets" / "deep"
    nested.mkdir(parents=True)
    assert store.case_id_at(nested) == "c-1", "sub-paths address their owning case"


def test_case_id_at_ignores_an_unmanaged_case_folder(store, tmp_path):
    """A case record outside the store is not the store's case."""
    outside = tmp_path / "elsewhere"
    seed_detached_case(TicketCase, outside)
    assert store.case_id_at(outside) is None


# ------------------------------------------------------------------- iterators


@pytest.mark.asyncio
async def test_iterators_partition_the_fleet_by_status(store, tmp_path):
    for n in range(3):
        await _seed_live(store, tmp_path, f"c-{n}")
    await store.set_status("c-0", TERMINATED)
    await store.set_status("c-1", TERMINATED)
    await store.set_status("c-2", QUARANTINED)

    terminated = {e.case_id for e in store.iter_by_status(TERMINATED)}
    assert terminated == {"c-0", "c-1"}
    assert [e.case_id for e in store.iter_by_status(QUARANTINED)] == ["c-2"]
    assert list(store.iter_by_status(LIVE)) == []
    assert {e.case_id for e in store.iter_all()} == {"c-0", "c-1", "c-2"}


@pytest.mark.asyncio
async def test_live_iter_orders_by_case_id_oldest_first(store, tmp_path):
    for case_id in ("c-2", "c-0", "c-1"):
        await _seed_live(store, tmp_path, case_id)
    assert [e.case_id for e in store.iter_by_status(LIVE)] == ["c-0", "c-1", "c-2"]
    assert [e.case_id for e in store.iter_by_status(LIVE, reverse=True)] == [
        "c-2",
        "c-1",
        "c-0",
    ]


@pytest.mark.asyncio
async def test_non_live_iter_orders_by_timed_ref(store, tmp_path, monkeypatch):
    await _seed_live(store, tmp_path, "c-early")
    await _seed_live(store, tmp_path, "c-late")

    base = datetime(2026, 8, 2, 10, 0, 0)
    times = {"c-early": base, "c-late": base + timedelta(hours=2)}

    real_activity = LocalCaseStore._activity_at

    def fake_activity(self, folder):
        entry = None
        for e in self.iter_by_status(LIVE):
            if e.case_folder.resolve() == Path(folder).resolve():
                entry = e
                break
        if entry is not None and entry.case_id in times:
            return times[entry.case_id]
        return real_activity(self, folder)

    monkeypatch.setattr(LocalCaseStore, "_activity_at", fake_activity)
    await store.set_status("c-early", TERMINATED)
    await store.set_status("c-late", TERMINATED)

    assert [e.case_id for e in store.iter_by_status(TERMINATED)] == ["c-early", "c-late"]
    assert [e.case_id for e in store.iter_by_status(TERMINATED, reverse=True)] == [
        "c-late",
        "c-early",
    ]


@pytest.mark.asyncio
async def test_date_bounds_filter_non_live(store, tmp_path, monkeypatch):
    await _seed_live(store, tmp_path, "c-old")
    await _seed_live(store, tmp_path, "c-new")
    times = {
        "c-old": datetime(2026, 7, 1, 12, 0, 0),
        "c-new": datetime(2026, 8, 15, 12, 0, 0),
    }
    real_activity = LocalCaseStore._activity_at

    def fake_activity(self, folder):
        for e in list(self.iter_by_status(LIVE)) + list(self.iter_by_status(TERMINATED)):
            if e.case_folder.resolve() == Path(folder).resolve() and e.case_id in times:
                return times[e.case_id]
        return real_activity(self, folder)

    monkeypatch.setattr(LocalCaseStore, "_activity_at", fake_activity)
    await store.set_status("c-old", TERMINATED)
    await store.set_status("c-new", TERMINATED)

    ids = [e.case_id for e in store.iter_by_status(
        TERMINATED, after=datetime(2026, 8, 1), before=datetime(2026, 8, 31, 23, 59)
    )]
    assert ids == ["c-new"]


@pytest.mark.asyncio
async def test_date_bounds_filter_live(store, tmp_path, monkeypatch):
    await _seed_live(store, tmp_path, "c-old")
    await _seed_live(store, tmp_path, "c-new")
    times = {
        "c-old": datetime(2026, 7, 1, 12, 0, 0),
        "c-new": datetime(2026, 8, 15, 12, 0, 0),
    }
    real_activity = LocalCaseStore._activity_at

    def fake_activity(self, folder):
        case_id = store.case_id_at(folder)
        if case_id in times:
            return times[case_id]
        return real_activity(self, folder)

    monkeypatch.setattr(LocalCaseStore, "_activity_at", fake_activity)
    ids = [e.case_id for e in store.iter_by_status(
        LIVE, after=datetime(2026, 8, 1), before=datetime(2026, 8, 31, 23, 59)
    )]
    assert ids == ["c-new"]


def test_inverted_date_bounds_raise(store):
    with pytest.raises(ValueError, match="after"):
        list(store.iter_by_status(
            LIVE,
            after=datetime(2026, 8, 10),
            before=datetime(2026, 8, 1),
        ))


@pytest.mark.asyncio
async def test_populated_statuses(store, tmp_path):
    assert "live" in store.populated_statuses()
    await _seed_live(store, tmp_path)
    await store.set_status("c-1", QUARANTINED)
    statuses = store.populated_statuses()
    assert "quarantined" in statuses


# ---------------------------------------------------------------------- export


@pytest.mark.asyncio
async def test_export_removes_the_case_from_the_store(store, tmp_path):
    await _seed_live(store, tmp_path)
    out = tmp_path / "exported"
    result = await store.export("c-1", out)

    assert result == out
    assert (out / "case_record.yaml").exists()
    assert store.find("c-1") is None, "an exported case has left the store"


# -------------------------------------------------------------- orphan absorb


@pytest.mark.asyncio
async def test_absorb_orphan_registers_a_folder_the_index_never_knew(store, tmp_path):
    """The rescue path for a relocation that died between its two halves."""
    source = tmp_path / "orphan"
    seed_detached_case(TicketCase, source)

    dest = await store.absorb_orphan("c-9", source, status=QUARANTINED)

    assert (dest / "case_record.yaml").exists()
    entry = store.find("c-9")
    assert entry is not None and entry.status == QUARANTINED
    assert entry.case_folder == dest, "and it is visible to every index-driven query"
    assert [e.case_id for e in store.iter_by_status(QUARANTINED)] == ["c-9"]


@pytest.mark.asyncio
async def test_absorb_orphan_is_re_drivable(store, tmp_path):
    source = tmp_path / "orphan"
    seed_detached_case(TicketCase, source)
    first = await store.absorb_orphan("c-9", source, status=QUARANTINED)
    second = await store.absorb_orphan("c-9", first, status=QUARANTINED)
    assert first == second, "re-absorbing the destination itself is a no-op"
