"""Tests for PoolMembershipJournal — the crash-recovery membership journal (design Section 8b).

Two layers are exercised:

1. Journal mechanics in isolation — record shape, replay-to-membership, torn-final-line
   tolerance, and the removal-triggered compaction — driven through synthetic pool events and
   raw file fixtures (no live cases needed).
2. End-to-end recovery against a real ``TieredCasePoolDriver`` — admit / remove / evict drive
   the journal through the live event stream, and ``restore_pool_from_journal`` rebuilds a
   fresh driver from the journal, reconciling each path against the folder on disk.
"""

import asyncio
import json
import os
import shutil
import time
import types
from pathlib import Path

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolEvent, CasePoolEventNames
from totodev_pub.folder_backed_case_support.constants import LEASE_NAME
from totodev_pub.folder_backed_case_support.tiered_case_pool_driver import TieredCasePoolDriver
from totodev_pub.folder_backed_case_support.pool_membership_journal import (
    DroppedMember,
    LeaseReclaimTimings,
    PoolMembershipJournal,
    PoolRestartConflictError,
    RebuildReport,
    restore_pool_from_journal,
)


_INSTANT = LeaseReclaimTimings(freeze_observe_secs=0.0, poll_secs=0.0, max_total_secs=5.0)


# ---------------------------------------------------------------------------
# Registry isolation (the singleton is process-wide mutable state)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


# ---------------------------------------------------------------------------
# Case fixtures
# ---------------------------------------------------------------------------

class AutoCase(FolderBackedCase):

    asset_aliases = {}
    """Two auto edges to a terminal: progresses on every step, then closes."""
    fsm_state_chains = ["^s0--step-->s1--step2-->s2^"]

    async def perform_step(self, tctx):
        pass

    async def perform_step2(self, tctx):
        pass


class BlockingCase(FolderBackedCase):


    asset_aliases = {}
    """Auto step that blocks on an injected gate, to hold a case in-flight."""
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        await self._gate.wait()


class UnregisteredCase(FolderBackedCase):


    asset_aliases = {}
    """A type deliberately left out of the registry to exercise the bad-type drop path."""
    fsm_state_chains = ["^s0--step-->s1^"]

    async def perform_step(self, tctx):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make(case_cls, tmp_path, name, **kw):
    return case_cls.create_case_in_folder(tmp_path / name, **kw)


def _emit(journal, name, folder):
    """Feed a synthetic pool event straight into the journal's subscriber callback."""
    case = types.SimpleNamespace(case_folder=Path(folder))
    journal._on_pool_event(
        CasePoolEvent(event=name, case=case, handle="h", advance_result=None)
    )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Record shape + event mapping
# ---------------------------------------------------------------------------

def test_admitted_writes_add_record(tmp_path):
    jpath = tmp_path / "journal.jsonl"
    journal = PoolMembershipJournal(jpath, clock=lambda: 123.0)
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/a")
    recs = _records(jpath)
    assert recs == [{"op": "add", "path": "/cases/a", "ts": 123.0}]
    assert journal.live_paths() == {Path("/cases/a")}


def test_removed_and_evicted_write_remove_record(tmp_path):
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/a")
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/b")
    _emit(journal, CasePoolEventNames.REMOVED, "/cases/a")
    _emit(journal, CasePoolEventNames.EVICTED, "/cases/b")
    ops = [(r["op"], r["path"]) for r in _records(journal.path)]
    assert ops == [
        ("add", "/cases/a"), ("add", "/cases/b"),
        ("remove", "/cases/a"), ("remove", "/cases/b"),
    ]
    assert journal.live_paths() == set()


def test_unrelated_events_are_ignored(tmp_path):
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    _emit(journal, CasePoolEventNames.ADVANCED, "/cases/a")
    _emit(journal, CasePoolEventNames.ALERTED, "/cases/a")
    _emit(journal, CasePoolEventNames.HALTED, "/cases/a")
    assert not journal.path.exists()
    assert journal.live_paths() == set()


# ---------------------------------------------------------------------------
# Replay to membership
# ---------------------------------------------------------------------------

def test_replay_membership_last_op_wins(tmp_path):
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/a")
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/b")
    _emit(journal, CasePoolEventNames.REMOVED, "/cases/a")
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/a")   # re-added after removal
    members = journal._read_members()
    # First-seen order is preserved; "a" ends on an add, so it remains a member.
    assert members == [Path("/cases/a"), Path("/cases/b")]


def test_replay_empty_when_no_file(tmp_path):
    journal = PoolMembershipJournal(tmp_path / "missing.jsonl")
    assert journal._read_members() == []


def test_tolerates_torn_final_line(tmp_path):
    jpath = tmp_path / "journal.jsonl"
    good = json.dumps({"op": "add", "path": "/cases/a", "ts": 1.0})
    jpath.write_text(good + "\n" + '{"op": "add", "path": "/cases/b"')  # torn, no newline
    journal = PoolMembershipJournal(jpath)
    assert journal._read_members() == [Path("/cases/a")]


def test_malformed_non_final_line_raises(tmp_path):
    jpath = tmp_path / "journal.jsonl"
    jpath.write_text("not-json\n" + json.dumps({"op": "add", "path": "/cases/a", "ts": 1.0}) + "\n")
    journal = PoolMembershipJournal(jpath)
    with pytest.raises(json.JSONDecodeError):
        journal._read_members()


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

def test_compaction_triggers_after_threshold_removals(tmp_path):
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl", compaction_threshold=2)
    for name in ("a", "b", "c"):
        _emit(journal, CasePoolEventNames.ADMITTED, f"/cases/{name}")
    _emit(journal, CasePoolEventNames.REMOVED, "/cases/a")
    _emit(journal, CasePoolEventNames.REMOVED, "/cases/b")   # second removal -> compaction
    recs = _records(journal.path)
    assert all(r["op"] == "add" for r in recs)
    assert {r["path"] for r in recs} == {"/cases/c"}
    assert journal._removals_since_compaction == 0


def test_compact_from_live_rewrites_atomically(tmp_path):
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    _emit(journal, CasePoolEventNames.ADMITTED, "/cases/a")
    _emit(journal, CasePoolEventNames.REMOVED, "/cases/a")
    journal.compact_from_live([Path("/cases/x"), Path("/cases/y")])
    recs = _records(journal.path)
    assert all(r["op"] == "add" for r in recs)
    assert {r["path"] for r in recs} == {"/cases/x", "/cases/y"}
    assert journal.live_paths() == {Path("/cases/x"), Path("/cases/y")}
    assert not journal.path.with_name(journal.path.name + ".tmp").exists()


# ---------------------------------------------------------------------------
# Reconciliation outcomes (rebuild against folders on disk)
# ---------------------------------------------------------------------------

def test_rebuild_readmits_open_case(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    case = _make(AutoCase, tmp_path, "open")
    folder = case.case_folder
    _emit(journal, CasePoolEventNames.ADMITTED, folder)
    case.case_detach()                       # free the lease so rehydrate can take it

    readded = []
    report = journal.rebuild(readded.append)
    assert report.readded == [folder]
    assert len(readded) == 1 and not readded[0].case_is_detached
    assert not readded[0].case_is_closed
    readded[0].case_detach()


def test_rebuild_readmits_closed_case(tmp_path):
    async def body():
        case_type_registry.register_case_types(AutoCase)
        journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
        case = _make(AutoCase, tmp_path, "closed")
        folder = case.case_folder
        await case.case_advance()
        await case.case_advance()
        assert case.case_is_closed
        _emit(journal, CasePoolEventNames.ADMITTED, folder)
        case.case_detach()

        readded = []
        report = journal.rebuild(readded.append)
        assert report.readded == [folder]
        assert readded[0].case_is_closed
        readded[0].case_detach()

    _run(body())


def test_rebuild_drops_missing_folder(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    case = _make(AutoCase, tmp_path, "gone")
    folder = case.case_folder
    _emit(journal, CasePoolEventNames.ADMITTED, folder)
    case.case_detach()
    shutil.rmtree(folder)

    report = journal.rebuild(lambda c: pytest.fail("should not re-add a missing folder"))
    assert report.dropped_missing == [folder]
    assert journal.live_paths() == set()


def test_rebuild_drops_owned_elsewhere(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    case = _make(AutoCase, tmp_path, "owned")          # stays live: lease held
    folder = case.case_folder
    _emit(journal, CasePoolEventNames.ADMITTED, folder)
    try:
        report = journal.rebuild(lambda c: pytest.fail("should not re-add a held case"))
        assert report.dropped_owned_elsewhere == [folder]
    finally:
        case.case_detach()


def test_rebuild_drops_unregistered_type(tmp_path):
    # UnregisteredCase intentionally NOT registered -> rehydrate raises UnregisteredCaseTypeError.
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    case = _make(UnregisteredCase, tmp_path, "weird")
    folder = case.case_folder
    _emit(journal, CasePoolEventNames.ADMITTED, folder)
    case.case_detach()

    report = journal.rebuild(lambda c: pytest.fail("should not re-add an unregistered type"))
    assert report.dropped_bad_type == [folder]


def test_rebuild_compacts_to_readded_set(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    keep = _make(AutoCase, tmp_path, "keep")
    drop = _make(AutoCase, tmp_path, "drop")
    _emit(journal, CasePoolEventNames.ADMITTED, keep.case_folder)
    _emit(journal, CasePoolEventNames.ADMITTED, drop.case_folder)
    keep.case_detach()
    drop.case_detach()
    shutil.rmtree(drop.case_folder)

    readded = []
    journal.rebuild(readded.append)
    recs = _records(journal.path)
    assert all(r["op"] == "add" for r in recs)
    assert {r["path"] for r in recs} == {str(keep.case_folder)}
    for c in readded:
        c.case_detach()


# ---------------------------------------------------------------------------
# End-to-end: attach to a real driver, then recover into a fresh one
# ---------------------------------------------------------------------------

def test_attach_records_driver_membership(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    driver = TieredCasePoolDriver()
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    journal.attach(driver)
    a = _make(AutoCase, tmp_path, "a")
    b = _make(AutoCase, tmp_path, "b")
    try:
        driver.add(a)
        driver.add(b)
        driver.remove(b.case_folder)
        assert journal.live_paths() == {a.case_folder}
    finally:
        journal.detach(driver)
        a.case_detach()
        b.case_detach()


def test_detach_stops_recording(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    driver = TieredCasePoolDriver()
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    journal.attach(driver)
    journal.detach(driver)
    a = _make(AutoCase, tmp_path, "a")
    try:
        driver.add(a)
        assert journal.live_paths() == set()
    finally:
        a.case_detach()


def test_evicted_case_recorded_as_remove(tmp_path):
    async def body():
        case_type_registry.register_case_types(AutoCase)
        driver = TieredCasePoolDriver()
        journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
        journal.attach(driver)
        case = _make(AutoCase, tmp_path, "evict")
        folder = case.case_folder
        driver.add(case)
        assert journal.live_paths() == {folder}
        case.case_detach()
        shutil.rmtree(folder)             # rehydrate impossible -> eviction on next beat
        driver.boost(folder)
        await driver.advance(suggested_interval_secs=0.0)
        await driver.settle()
        assert folder not in driver
        assert journal.live_paths() == set()
        journal.detach(driver)

    _run(body())


def test_full_recovery_into_fresh_driver(tmp_path):
    async def body():
        case_type_registry.register_case_types(AutoCase)

        # --- original driver run (the process that later "crashes") ---
        driver1 = TieredCasePoolDriver()
        journal_path = tmp_path / "journal.jsonl"
        journal1 = PoolMembershipJournal(journal_path)
        journal1.attach(driver1)
        a = _make(AutoCase, tmp_path, "a")
        b = _make(AutoCase, tmp_path, "b")
        driver1.add(a)
        driver1.add(b)
        driver1.remove(b.case_folder)         # b legitimately left the pool
        b.case_detach()
        # Simulate a crash: drop driver1 without removing a, freeing a's lease.
        a.case_detach()

        # --- fresh process restarts and recovers from the journal ---
        driver2 = TieredCasePoolDriver()
        journal2 = PoolMembershipJournal(journal_path)
        report = await restore_pool_from_journal(driver2, journal2)

        assert a.case_folder in driver2
        assert b.case_folder not in driver2
        assert report.readded == [a.case_folder]

        # Newly recovered membership is recorded for the next crash, too.
        recovered = driver2[a.case_folder]
        assert not recovered.case_is_detached
        assert journal2.live_paths() == {a.case_folder}

        # A subsequent removal flows through the now-attached journal.
        driver2.remove(a.case_folder)
        assert journal2.live_paths() == set()
        recovered.case_detach()

    _run(body())


def test_restore_waits_for_frozen_lease_then_acquires(tmp_path):
    """A dead pool's cases hold frozen leases. The gate sees no movement (dead), then Phase 2
    waits for the lease to lapse and admits the case once it is free."""
    async def body():
        case_type_registry.register_case_types(AutoCase)
        journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
        case = _make(AutoCase, tmp_path, "frozen")
        folder = case.case_folder
        _emit(journal, CasePoolEventNames.ADMITTED, folder)
        lease_file = folder / LEASE_NAME

        calls = {"n": 0}

        async def fake_sleep(_secs):
            # Call #1 is the gate's observe window (keep the lease frozen so it reads dead);
            # the next poll sleep frees the lease so the reclaim can take it.
            calls["n"] += 1
            if calls["n"] >= 2:
                past = time.time() - 1.0
                os.utime(lease_file, (past, past))

        driver = TieredCasePoolDriver()
        report = await restore_pool_from_journal(
            driver, journal, sleep=fake_sleep, timings=_INSTANT,
        )
        assert report.readded == [folder]
        assert folder in driver
        assert not driver[folder].case_is_detached
        driver[folder].case_detach()

    _run(body())


def test_restore_aborts_on_live_competitor(tmp_path):
    """If a held lease is actively renewed during the observe window, a live owner exists: the
    gate aborts wholesale, instantiating nothing."""
    async def body():
        case_type_registry.register_case_types(AutoCase)
        journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
        case = _make(AutoCase, tmp_path, "live")
        folder = case.case_folder
        _emit(journal, CasePoolEventNames.ADMITTED, folder)

        async def fake_sleep(_secs):
            case.case_heartbeat(min_update_secs=0.0)   # the live owner beats its lease

        driver = TieredCasePoolDriver()
        with pytest.raises(PoolRestartConflictError) as excinfo:
            await restore_pool_from_journal(
                driver, journal, sleep=fake_sleep, timings=_INSTANT,
            )
        assert [c.path for c in excinfo.value.conflicts] == [folder]
        assert all(c.kind == "live" for c in excinfo.value.conflicts)
        assert excinfo.value.recovered == []
        assert folder not in driver
        case.case_detach()

    _run(body())


def test_restore_reports_lease_resurrected_during_phase2(tmp_path):
    """Belt-and-suspenders: a lease that reads dead at the gate but springs back to life during
    the incremental reclaim is surfaced as a conflict, not fought over."""
    async def body():
        case_type_registry.register_case_types(AutoCase)
        journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
        case = _make(AutoCase, tmp_path, "zombie")
        folder = case.case_folder
        _emit(journal, CasePoolEventNames.ADMITTED, folder)

        calls = {"n": 0}

        async def fake_sleep(_secs):
            calls["n"] += 1
            if calls["n"] >= 2:                          # after the gate, the owner resurrects
                case.case_heartbeat(min_update_secs=0.0)

        driver = TieredCasePoolDriver()
        with pytest.raises(PoolRestartConflictError) as excinfo:
            await restore_pool_from_journal(
                driver, journal, sleep=fake_sleep, timings=_INSTANT,
            )
        assert [c.path for c in excinfo.value.conflicts] == [folder]
        assert folder not in driver
        case.case_detach()

    _run(body())


def test_restore_records_benign_drops_without_conflict(tmp_path):
    """A missing folder is a benign partial drop during reclaim — recorded, not an abort."""
    async def body():
        case_type_registry.register_case_types(AutoCase)
        journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
        gone = _make(AutoCase, tmp_path, "gone")
        folder = gone.case_folder
        _emit(journal, CasePoolEventNames.ADMITTED, folder)
        gone.case_detach()
        shutil.rmtree(folder)

        driver = TieredCasePoolDriver()
        report = await restore_pool_from_journal(driver, journal, timings=_INSTANT)
        assert report.dropped_missing == [folder]
        assert report.readded == []
        assert folder not in driver

    _run(body())


def test_rebuild_report_defaults_are_empty():
    report = RebuildReport()
    assert report.readded == []
    assert report.dropped_missing == []
    assert report.dropped_owned_elsewhere == []
    assert report.dropped_bad_type == []
    assert report.failures == []
    assert report.dropped == []


# ---------------------------------------------------------------------------
# Failure diagnostics (clarity around a jammed restart)
# ---------------------------------------------------------------------------

def test_failures_capture_exception_per_dropped_path(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")

    missing = _make(AutoCase, tmp_path, "missing")
    bad_type = _make(UnregisteredCase, tmp_path, "weird")   # type not registered
    for case in (missing, bad_type):
        _emit(journal, CasePoolEventNames.ADMITTED, case.case_folder)
    missing.case_detach()
    bad_type.case_detach()
    shutil.rmtree(missing.case_folder)

    report = journal.rebuild(lambda c: pytest.fail("nothing should re-add"))

    by_path = {f.path: f for f in report.failures}
    assert set(by_path) == {missing.case_folder, bad_type.case_folder}
    assert isinstance(by_path[missing.case_folder], DroppedMember)
    assert by_path[missing.case_folder].reason == "missing"
    assert by_path[missing.case_folder].error_type == "FileNotFoundError"
    assert by_path[bad_type.case_folder].reason == "bad_type"
    assert by_path[bad_type.case_folder].error_type == "UnregisteredCaseTypeError"
    assert by_path[bad_type.case_folder].detail                    # non-empty message
    assert set(report.dropped) == {missing.case_folder, bad_type.case_folder}


def test_failures_capture_owned_elsewhere_reason(tmp_path):
    case_type_registry.register_case_types(AutoCase)
    journal = PoolMembershipJournal(tmp_path / "journal.jsonl")
    case = _make(AutoCase, tmp_path, "held")            # stays live: lease held
    _emit(journal, CasePoolEventNames.ADMITTED, case.case_folder)
    try:
        report = journal.rebuild(lambda c: pytest.fail("held case must not re-add"))
        assert [f.reason for f in report.failures] == ["owned_elsewhere"]
        assert report.failures[0].error_type == "CaseAlreadyOpenError"
    finally:
        case.case_detach()
