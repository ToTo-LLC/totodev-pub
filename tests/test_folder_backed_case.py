"""Minimal smoke tests for FolderBackedCase. Kept intentionally thin while the
class is still evolving — expand once the API stabilises."""

import asyncio
import os
import time
import pytest
from pathlib import Path

from totodev_pub.folder_backed_case import (
    FolderBackedCase,
    CaseRecord,
    CaseAlreadyOpenError,
    CaseTypeMismatchError,
    MissingFsmError,
)
import totodev_pub.folder_backed_case as _fbc
import totodev_pub.folder_backed_case_support.case_machine_factory as _cmf
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry,
    case_type_registry,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    FsmBindingError,
    OwnershipLostError,
    UnregisteredCaseTypeError,
)
from totodev_pub.folder_backed_case_support.constants import LEASE_NAME
from totodev_pub.folder_backed_case_support.heartbeat_lease import HeartbeatLease


# ---------------------------------------------------------------------------
# Registry isolation: the module-level singleton is process-wide mutable state.
# Snapshot and restore it around every test so registrations don't leak across
# tests (order-independence).
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
# Minimal concrete subclass used across all tests
# ---------------------------------------------------------------------------

class SimpleCase(FolderBackedCase):
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]


class TypedRecord(CaseRecord):
    """A CaseRecord subclass with an extra field, to verify typed peek resolution."""
    flavor: str = "vanilla"


class TypedCase(FolderBackedCase):
    fsm_state_chains = ["^new==begin-->done^"]
    _record_cls = TypedRecord


class ReclassTarget(FolderBackedCase):
    """Shares the 'new' state with SimpleCase, so reclassify from a fresh case is legal."""
    fsm_state_chains = ["^new==go-->finished^"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_and_basic_properties(tmp_path):
    folder = tmp_path / "case-001"
    case = SimpleCase.create_case_in_folder(folder, case_id="c-001")
    try:
        assert case.case_id == "c-001"
        assert case.case_state == "new"
        assert case.case_is_open
        assert not case.case_is_closed
    finally:
        case.case_detach()


def test_context_manager_releases_lease(tmp_path):
    folder = tmp_path / "case-002"
    with SimpleCase.create_case_in_folder(folder, case_id="c-002") as case:
        assert case.case_is_open
    # Lease file should be gone after the with-block exits
    assert not (folder / ".case.lease").exists()


def test_second_open_raises(tmp_path):
    folder = tmp_path / "case-003"
    first = SimpleCase.create_case_in_folder(folder)
    try:
        with pytest.raises(CaseAlreadyOpenError):
            SimpleCase(folder)
    finally:
        first.case_detach()


def test_fsm_transitions(tmp_path):
    folder = tmp_path / "case-004"
    with SimpleCase.create_case_in_folder(folder) as case:
        asyncio.run(case.begin())
        assert case.case_state == "open"
        asyncio.run(case.finish())
        assert case.case_state == "done"
        assert case.case_is_closed


def test_peek_record_and_events(tmp_path):
    folder = tmp_path / "case-005"
    with SimpleCase.create_case_in_folder(folder, case_id="c-005") as case:
        asyncio.run(case.begin())

    record = FolderBackedCase.peek_case_record(folder)
    assert record.case_id == "c-005"
    assert record.case_object_type == "SimpleCase"

    events = FolderBackedCase.peek_case_events(folder)
    assert events.current_state == "open"
    assert not events.is_closed


def test_rehydrate_requires_registration(tmp_path):
    folder = tmp_path / "case-006"
    with SimpleCase.create_case_in_folder(folder):
        pass
    with pytest.raises(UnregisteredCaseTypeError):
        case_type_registry.rehydrate(folder)


def test_rehydrate_with_registration(tmp_path):
    case_type_registry.register_case_types(SimpleCase)
    folder = tmp_path / "case-007"
    with SimpleCase.create_case_in_folder(folder):
        pass
    with case_type_registry.rehydrate(folder) as case:
        assert isinstance(case, SimpleCase)
        assert case.case_state == "new"


# ---------------------------------------------------------------------------
# CaseTypeRegistry: direct coverage of the extracted catalog/peek surface
# ---------------------------------------------------------------------------

def test_register_decorator_returns_class_and_registers():
    reg = CaseTypeRegistry()

    @reg.register
    class Decorated(FolderBackedCase):
        fsm_state_chains = ["^new==begin-->done^"]

    # Decorator returns the class unchanged...
    assert issubclass(Decorated, FolderBackedCase)
    # ...and the class is now resolvable by its bare name.
    assert reg.resolve_case_type("Decorated") is Decorated


def test_register_case_types_multiple():
    reg = CaseTypeRegistry()
    reg.register_case_types(SimpleCase, TypedCase)
    assert reg.resolve_case_type("SimpleCase") is SimpleCase
    assert reg.resolve_case_type("TypedCase") is TypedCase


def test_resolve_case_type_none_unknown_and_override():
    reg = CaseTypeRegistry()
    # None in -> None out (no lookup attempted).
    assert reg.resolve_case_type(None) is None
    # Unknown name -> None (graceful miss).
    assert reg.resolve_case_type("DoesNotExist") is None
    # Explicit registry override bypasses the instance catalog entirely.
    override = {"SimpleCase": SimpleCase}
    assert reg.resolve_case_type("SimpleCase", registry=override) is SimpleCase
    # The override did not mutate the instance catalog.
    assert reg.resolve_case_type("SimpleCase") is None


def test_peek_case_record_typed_vs_fallback(tmp_path):
    folder = tmp_path / "case-typed"
    with TypedCase.create_case_in_folder(folder, case_id="t-typed"):
        pass

    # Caller supplies the case class -> its _record_cls -> the CORRECT typed record.
    typed = FolderBackedCase.peek_case_record(folder, case_cls=TypedCase)
    assert isinstance(typed, TypedRecord)
    assert typed.case_object_type == "TypedCase"
    assert typed.flavor == "vanilla"

    # Nothing supplied -> graceful base CaseRecord (no exception, no registry consulted).
    base = FolderBackedCase.peek_case_record(folder)
    assert type(base) is CaseRecord
    assert base.case_id == "t-typed"


def test_peek_case_record_explicit_record_cls(tmp_path):
    folder = tmp_path / "case-explicit"
    with SimpleCase.create_case_in_folder(folder, case_id="s-explicit"):
        pass
    # record_cls given -> used directly (and wins over case_cls).
    rec = FolderBackedCase.peek_case_record(folder, record_cls=TypedRecord, case_cls=SimpleCase)
    assert isinstance(rec, TypedRecord)
    assert rec.case_id == "s-explicit"


def test_peek_case_record_is_static_and_registry_free(tmp_path):
    folder = tmp_path / "case-static"
    with SimpleCase.create_case_in_folder(folder, case_id="s-static"):
        pass
    # A static on FolderBackedCase: callable with no instance or registry in play.
    rec = FolderBackedCase.peek_case_record(folder)
    assert type(rec) is CaseRecord
    assert rec.case_object_type == "SimpleCase"


def test_peek_class_returns_name_registry_free(tmp_path):
    folder = tmp_path / "case-name"
    with SimpleCase.create_case_in_folder(folder, case_id="n-1"):
        pass
    # Default: the bare type NAME, sniffed from disk; no registration required.
    assert case_type_registry.peek_class(folder) == "SimpleCase"


def test_peek_class_resolves_registered_class_object(tmp_path):
    folder = tmp_path / "case-obj"
    with SimpleCase.create_case_in_folder(folder):
        pass
    cls = case_type_registry.peek_class(
        folder, return_class_object=True, registry={"SimpleCase": SimpleCase}
    )
    assert cls is SimpleCase


def test_peek_class_unregistered_raises_for_class_object(tmp_path):
    folder = tmp_path / "case-unreg-obj"
    with SimpleCase.create_case_in_folder(folder):
        pass
    # Strict: a class object cannot be produced for an unregistered name.
    with pytest.raises(UnregisteredCaseTypeError) as excinfo:
        case_type_registry.peek_class(folder, return_class_object=True, registry={})
    assert excinfo.value.type_name == "SimpleCase"
    # But the NAME form degrades to a plain string with no registration.
    assert case_type_registry.peek_class(folder, registry={}) == "SimpleCase"


def test_peek_class_uninitialized_folder_raises(tmp_path):
    folder = tmp_path / "case-empty-peek"
    folder.mkdir()
    with pytest.raises(FileNotFoundError):
        case_type_registry.peek_class(folder)


def test_peek_class_then_peek_case_record_roundtrip(tmp_path):
    # The deduce-then-read path: resolve the class via the registry, then read the record.
    folder = tmp_path / "case-roundtrip"
    with TypedCase.create_case_in_folder(folder, case_id="rt-1"):
        pass
    cls = case_type_registry.peek_class(
        folder, return_class_object=True, registry={"TypedCase": TypedCase}
    )
    rec = FolderBackedCase.peek_case_record(folder, case_cls=cls)
    assert isinstance(rec, TypedRecord)
    assert rec.flavor == "vanilla"


def test_peek_case_record_does_not_acquire_lease(tmp_path):
    folder = tmp_path / "case-nolease"
    with SimpleCase.create_case_in_folder(folder, case_id="s-nolease"):
        pass
    assert not (folder / ".case.lease").exists()
    FolderBackedCase.peek_case_record(folder)
    # Peek is lock-free: it must never leave a lease behind.
    assert not (folder / ".case.lease").exists()


def test_peek_case_assets_without_live_case(tmp_path):
    folder = tmp_path / "case-assets-peek"
    with SimpleCase.create_case_in_folder(folder, case_id="s-assets"):
        pass
    assets = FolderBackedCase.peek_case_assets(folder)
    assert assets.folder == folder / "assets"
    assert not (folder / ".case.lease").exists()


def test_rehydrate_unregistered_carries_type_name(tmp_path):
    folder = tmp_path / "case-unreg"
    with SimpleCase.create_case_in_folder(folder):
        pass
    # Isolated registry that does not know SimpleCase -> strict failure with the name.
    with pytest.raises(UnregisteredCaseTypeError) as excinfo:
        case_type_registry.rehydrate(folder, registry={})
    assert excinfo.value.type_name == "SimpleCase"


def test_fresh_registry_isolated_from_singleton():
    reg = CaseTypeRegistry()
    reg.register_case_types(TypedCase)
    # Registering on a fresh instance must not leak into the module singleton.
    assert reg.resolve_case_type("TypedCase") is TypedCase
    assert case_type_registry.resolve_case_type("TypedCase") is None


def test_constructing_wrong_class_raises_and_leaves_no_lease(tmp_path):
    folder = tmp_path / "case-wrongclass"
    with SimpleCase.create_case_in_folder(folder, case_id="w-1"):
        pass
    # TypedCase also has a 'new' state, so without the gate this would silently succeed.
    with pytest.raises(CaseTypeMismatchError) as excinfo:
        TypedCase(folder)
    assert excinfo.value.on_disk == "SimpleCase"
    assert excinfo.value.loading_class == "TypedCase"
    # Rejected before lease acquisition: nothing left claimed.
    assert not (folder / ".case.lease").exists()


def test_init_on_uninitialized_folder_points_to_create(tmp_path):
    folder = tmp_path / "case-empty"
    folder.mkdir()
    # Binding (__init__) is the load path, not inception: a folder with no record must
    # fail loudly and name the method the caller actually wanted.
    with pytest.raises(FileNotFoundError) as excinfo:
        SimpleCase(folder)
    msg = str(excinfo.value)
    assert "create_case_in_folder()" in msg
    assert "rehydrate" in msg
    assert not (folder / ".case.lease").exists()


def test_reclassify_to_succeeds_through_type_gate(tmp_path):
    folder = tmp_path / "case-reclass"
    case = SimpleCase.create_case_in_folder(folder, case_id="r-1")
    fresh = case.case_reclassify_to(ReclassTarget)
    try:
        assert isinstance(fresh, ReclassTarget)
        assert fresh.case_state == "new"
        # New name is stamped in memory and persisted to disk.
        assert fresh._record.case_object_type == "ReclassTarget"
        assert FolderBackedCase.peek_case_record(folder).case_object_type == "ReclassTarget"
    finally:
        fresh.case_detach()


def test_missing_fsm_raises_actionable_error(tmp_path):
    """A concrete subclass that forgets fsm_state_chains (and doesn't override
    compile_fsm) must fail loudly at construction, naming the corrective action."""
    class NoFsmCase(FolderBackedCase):
        pass

    folder = tmp_path / "case-008"
    with pytest.raises(MissingFsmError) as excinfo:
        NoFsmCase.create_case_in_folder(folder, case_id="c-008")
    msg = str(excinfo.value)
    assert "NoFsmCase" in msg
    assert "fsm_state_chains" in msg
    assert "compile_fsm" in msg
    # The folder/lease must not be left claimed by a half-built case.
    assert not (folder / ".case.lease").exists()


def test_create_requires_existing_parent(tmp_path):
    folder = tmp_path / "missing-parent" / "case-009"
    with pytest.raises(FileNotFoundError) as excinfo:
        SimpleCase.create_case_in_folder(folder, case_id="c-009")
    assert "Create/confirm the parent folder first" in str(excinfo.value)
    assert not folder.exists()


def test_create_reuses_existing_folder_with_unrelated_files(tmp_path):
    folder = tmp_path / "case-010"
    folder.mkdir()
    (folder / "notes.txt").write_text("unrelated")
    case = SimpleCase.create_case_in_folder(folder, case_id="c-010")
    try:
        assert case.case_id == "c-010"
    finally:
        case.case_detach()


def test_create_rejects_existing_case_artifacts(tmp_path):
    folder = tmp_path / "case-011"
    folder.mkdir()
    (folder / "events").mkdir()
    with pytest.raises(FileExistsError) as excinfo:
        SimpleCase.create_case_in_folder(folder, case_id="c-011")
    assert "existing case artifacts" in str(excinfo.value)
    assert "events" in str(excinfo.value)


def test_assets_folder_and_asset_path(tmp_path):
    folder = tmp_path / "case-012"
    case = SimpleCase.create_case_in_folder(folder, case_id="c-012")
    try:
        assert case.case_assets.folder == folder / "assets"
        assert case.case_assets.asset_path("a/b.txt") == folder / "assets" / "a" / "b.txt"
    finally:
        case.case_detach()


def test_assets_relative_path_from_absolute_and_relative(tmp_path):
    folder = tmp_path / "case-013"
    case = SimpleCase.create_case_in_folder(folder, case_id="c-013")
    try:
        abs_inside = folder / "assets" / "sub" / "x.txt"
        assert case.case_assets.relative_path(abs_inside) == "sub/x.txt"
        assert case.case_assets.relative_path("sub/./x.txt") == "sub/x.txt"
        with pytest.raises(ValueError):
            case.case_assets.relative_path(tmp_path / "outside.txt")
    finally:
        case.case_detach()


def test_guard_method_convention_constructs(tmp_path):
    """A `<token>#trigger` guard binds to `guard_<token>`; a correctly named async guard
    lets the case construct cleanly."""
    class GuardedCase(FolderBackedCase):
        fsm_state_chains = ["^new==funded#finish-->done^"]

        async def guard_funded(self, tctx):
            return True

    folder = tmp_path / "case-g1"
    case = GuardedCase.create_case_in_folder(folder, case_id="g-1")
    try:
        assert case.case_state == "new"
    finally:
        case.case_detach()


def test_orphan_guard_method_fails_construction(tmp_path):
    """A `guard_`-prefixed method whose token maps to no chain guard is treated as a typo
    and fails the build (orphan_detection defaults to error)."""
    class TypoGuardCase(FolderBackedCase):
        fsm_state_chains = ["^new==funded#finish-->done^"]

        async def guard_funded(self, tctx):
            return True

        async def guard_fundedd(self, tctx):  # typo: funded
            return True

    folder = tmp_path / "case-g2"
    with pytest.raises(FsmBindingError) as excinfo:
        TypoGuardCase.create_case_in_folder(folder, case_id="g-2")
    msg = str(excinfo.value)
    assert "guard_fundedd" in msg
    # Binding runs before any disk/lease I/O, so nothing is left claimed.
    assert not (folder / ".case.lease").exists()


def test_hook_missing_tctx_param_fails_construction(tmp_path):
    """Every hook is dispatched with one `tctx` argument (send_event=True); a hook declared
    without it is rejected at first construction rather than exploding at first transition."""
    class NoTctxCase(FolderBackedCase):
        fsm_state_chains = ["^new--begin-->open^"]

        async def perform_begin(self):  # missing tctx
            return None

    folder = tmp_path / "case-arity"
    with pytest.raises(FsmBindingError) as excinfo:
        NoTctxCase.create_case_in_folder(folder, case_id="a-1")
    msg = str(excinfo.value)
    assert "perform_begin" in msg
    assert "tctx" in msg
    # Binding runs before any disk/lease I/O, so nothing is left claimed.
    assert not (folder / ".case.lease").exists()


def test_perform_hook_convention_wires_and_runs(tmp_path):
    """An auto edge (`--`) requires `perform_<trigger>`; the method binds to the
    transition's `before` and runs when the trigger fires."""
    class PerformCase(FolderBackedCase):
        fsm_state_chains = ["^new--begin-->open==finish-->done^"]
        performed = False

        async def perform_begin(self, tctx):
            self.performed = True

    folder = tmp_path / "case-p1"
    with PerformCase.create_case_in_folder(folder, case_id="p-1") as case:
        assert case.performed is False
        asyncio.run(case.begin())
        assert case.case_state == "open"
        assert case.performed is True


def test_legacy_underscore_perform_hook_is_rejected(tmp_path):
    """The trigger action hook dropped its leading underscore. An auto edge whose action
    method is still named `_perform_<trigger>` no longer satisfies the required
    `perform_<trigger>`, so the build fails — a deliberate pre-release breaking change."""
    class LegacyCase(FolderBackedCase):
        fsm_state_chains = ["^new--begin-->done^"]

        async def _perform_begin(self, tctx):  # old name, no longer recognized
            return None

    folder = tmp_path / "case-legacy"
    with pytest.raises(FsmBindingError) as excinfo:
        LegacyCase.create_case_in_folder(folder, case_id="legacy-1")
    msg = str(excinfo.value)
    assert "perform_begin" in msg
    # Binding runs before any disk/lease I/O, so nothing is left claimed.
    assert not (folder / ".case.lease").exists()


def test_sealed_member_override_fails_construction(tmp_path):
    """A subclass that redefines a sealed base member (a reserved part of the core call
    surface, e.g. `case_state`) is rejected at first construction with FsmBindingError —
    the defensive guard that protects the base namespace from accidental shadowing."""
    class ClobberCase(FolderBackedCase):
        fsm_state_chains = ["^new==begin-->done^"]

        def case_state(self):  # clobbers the sealed base member
            return "nope"

    folder = tmp_path / "case-sealed"
    with pytest.raises(FsmBindingError) as excinfo:
        ClobberCase.create_case_in_folder(folder, case_id="sealed-1")
    msg = str(excinfo.value)
    assert "case_state" in msg
    assert "sealed" in msg
    assert ("case_state", "ClobberCase") in excinfo.value.sealed
    # Binding runs before any disk/lease I/O, so nothing is left claimed.
    assert not (folder / ".case.lease").exists()


def test_generate_case_id_auto_bumps_on_same_millisecond(monkeypatch):
    fixed_seconds = 1_700_000_000.123
    monkeypatch.setattr("totodev_pub.folder_backed_case.time.time", lambda: fixed_seconds)
    first = SimpleCase.generate_case_id()
    second = SimpleCase.generate_case_id()
    assert second != first
    assert int(second, 36) == int(first, 36) + 1


# ---------------------------------------------------------------------------
# In-flight lease keepalive (the _LeaseKeepalive pulse), end-to-end via the case API.
# Real-time tests that shrink the fixed TTL to ~0.3s so the work outlives the un-pulsed window.
# ---------------------------------------------------------------------------

def _use_short_ttl(monkeypatch, ttl=0.3):
    """Shrink the (now-fixed) lease TTL for real-time keepalive tests. The TTL is no longer a
    per-case seam, so patch BOTH module bindings the running code reads: the lease window
    (folder_backed_case) and the pulse cadence (the factory). Pulse beats ~every ttl / 3."""
    monkeypatch.setattr(_fbc, "DEFAULT_LEASE_TTL_SECS", ttl)
    monkeypatch.setattr(_cmf, "DEFAULT_LEASE_TTL_SECS", ttl)


class _SlowKeepaliveCase(FolderBackedCase):
    """Slow work behind both an AUTO (`go`) and a MANUAL (`step`) edge. The tests pair this with
    _use_short_ttl so the keepalive pulse is what keeps the lease from lapsing during the step."""

    fsm_state_chains = ["^new--go-->open==step-->done^"]
    sleep_secs: float = 0.0

    async def perform_go(self, tctx):
        if self.sleep_secs:
            await asyncio.sleep(self.sleep_secs)

    async def perform_step(self, tctx):
        if self.sleep_secs:
            await asyncio.sleep(self.sleep_secs)


def test_case_advance_keeps_lease_alive_during_slow_auto_step(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _SlowKeepaliveCase.create_case_in_folder(
            tmp_path / "adv", case_id="adv-1"
        ) as case:
            case.sleep_secs = 0.8                   # outlives the 0.3s TTL
            lease_path = case.case_folder / LEASE_NAME
            task = asyncio.create_task(case.case_advance())
            await asyncio.sleep(0.5)                # past one un-pulsed TTL window
            assert HeartbeatLease.is_expired(lease_path) is False
            result = await task
            assert result.progressed
            assert case.case_state == "open"
            assert HeartbeatLease.is_expired(lease_path) is False

    asyncio.run(scenario())


def test_case_advance_raises_ownership_lost_not_folded(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _SlowKeepaliveCase.create_case_in_folder(
            tmp_path / "lost", case_id="lost-1"
        ) as case:
            case.sleep_secs = 1.0
            lease_path = case.case_folder / LEASE_NAME
            task = asyncio.create_task(case.case_advance())
            await asyncio.sleep(0.15)              # let the first pulse re-stamp our token
            foreign = time.time() + 999            # another owner overwrites the lease
            os.utime(lease_path, (foreign, foreign))
            # Surfaced as a raise, NOT folded into AdvanceResult.exceptions.
            with pytest.raises(OwnershipLostError):
                await task

    asyncio.run(scenario())


def test_manual_trigger_keeps_lease_alive_during_slow_work(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _SlowKeepaliveCase.create_case_in_folder(
            tmp_path / "man", case_id="man-1"
        ) as case:
            # Advance onto `open` first (fast), then fire the slow MANUAL edge directly.
            case.sleep_secs = 0.0
            await case.case_advance()
            assert case.case_state == "open"
            case.sleep_secs = 0.8
            lease_path = case.case_folder / LEASE_NAME
            task = asyncio.create_task(case.step())
            await asyncio.sleep(0.5)
            assert HeartbeatLease.is_expired(lease_path) is False
            await task
            assert case.case_state == "done"

    asyncio.run(scenario())
