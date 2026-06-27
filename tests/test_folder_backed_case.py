"""Minimal smoke tests for FolderBackedCase. Kept intentionally thin while the
class is still evolving — expand once the API stabilises."""

import asyncio
import pytest
from pathlib import Path

from totodev_pub.folder_backed_case import (
    FolderBackedCase,
    CaseRecord,
    CaseAlreadyOpenError,
    UnregisteredCaseTypeError,
    MissingFsmError,
)
from totodev_pub.folder_backed_case_support.exceptions import FsmBindingError


# ---------------------------------------------------------------------------
# Minimal concrete subclass used across all tests
# ---------------------------------------------------------------------------

class SimpleCase(FolderBackedCase):
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_and_basic_properties(tmp_path):
    folder = tmp_path / "case-001"
    case = SimpleCase.create_in_folder(folder, case_id="c-001")
    try:
        assert case.case_id == "c-001"
        assert case.state == "new"
        assert case.is_open
        assert not case.is_closed
    finally:
        case.detach()


def test_context_manager_releases_lease(tmp_path):
    folder = tmp_path / "case-002"
    with SimpleCase.create_in_folder(folder, case_id="c-002") as case:
        assert case.is_open
    # Lease file should be gone after the with-block exits
    assert not (folder / ".case.lease").exists()


def test_second_open_raises(tmp_path):
    folder = tmp_path / "case-003"
    first = SimpleCase.create_in_folder(folder)
    try:
        with pytest.raises(CaseAlreadyOpenError):
            SimpleCase(folder)
    finally:
        first.detach()


def test_fsm_transitions(tmp_path):
    folder = tmp_path / "case-004"
    with SimpleCase.create_in_folder(folder) as case:
        asyncio.run(case.begin())
        assert case.state == "open"
        asyncio.run(case.finish())
        assert case.state == "done"
        assert case.is_closed


def test_peek_record_and_events(tmp_path):
    folder = tmp_path / "case-005"
    with SimpleCase.create_in_folder(folder, case_id="c-005") as case:
        asyncio.run(case.begin())

    record = FolderBackedCase.peek_record(folder)
    assert record.case_id == "c-005"
    assert record.case_object_type == "SimpleCase"

    events = FolderBackedCase.peek_events(folder)
    assert events.current_state == "open"
    assert not events.is_closed


def test_rehydrate_requires_registration(tmp_path):
    folder = tmp_path / "case-006"
    with SimpleCase.create_in_folder(folder):
        pass
    with pytest.raises(UnregisteredCaseTypeError):
        FolderBackedCase.rehydrate(folder)


def test_rehydrate_with_registration(tmp_path):
    FolderBackedCase.register_case_types(SimpleCase)
    folder = tmp_path / "case-007"
    with SimpleCase.create_in_folder(folder):
        pass
    with FolderBackedCase.rehydrate(folder) as case:
        assert isinstance(case, SimpleCase)
        assert case.state == "new"


def test_missing_fsm_raises_actionable_error(tmp_path):
    """A concrete subclass that forgets fsm_state_chains (and doesn't override
    compile_fsm) must fail loudly at construction, naming the corrective action."""
    class NoFsmCase(FolderBackedCase):
        pass

    folder = tmp_path / "case-008"
    with pytest.raises(MissingFsmError) as excinfo:
        NoFsmCase.create_in_folder(folder, case_id="c-008")
    msg = str(excinfo.value)
    assert "NoFsmCase" in msg
    assert "fsm_state_chains" in msg
    assert "compile_fsm" in msg
    # The folder/lease must not be left claimed by a half-built case.
    assert not (folder / ".case.lease").exists()


def test_create_requires_existing_parent(tmp_path):
    folder = tmp_path / "missing-parent" / "case-009"
    with pytest.raises(FileNotFoundError) as excinfo:
        SimpleCase.create_in_folder(folder, case_id="c-009")
    assert "Create/confirm the parent folder first" in str(excinfo.value)
    assert not folder.exists()


def test_create_reuses_existing_folder_with_unrelated_files(tmp_path):
    folder = tmp_path / "case-010"
    folder.mkdir()
    (folder / "notes.txt").write_text("unrelated")
    case = SimpleCase.create_in_folder(folder, case_id="c-010")
    try:
        assert case.case_id == "c-010"
    finally:
        case.detach()


def test_create_rejects_existing_case_artifacts(tmp_path):
    folder = tmp_path / "case-011"
    folder.mkdir()
    (folder / "events").mkdir()
    with pytest.raises(FileExistsError) as excinfo:
        SimpleCase.create_in_folder(folder, case_id="c-011")
    assert "existing case artifacts" in str(excinfo.value)
    assert "events" in str(excinfo.value)


def test_assets_folder_and_asset_path(tmp_path):
    folder = tmp_path / "case-012"
    case = SimpleCase.create_in_folder(folder, case_id="c-012")
    try:
        assert case.assets.folder == folder / "assets"
        assert case.assets.asset_path("a/b.txt") == folder / "assets" / "a" / "b.txt"
    finally:
        case.detach()


def test_assets_relative_path_from_absolute_and_relative(tmp_path):
    folder = tmp_path / "case-013"
    case = SimpleCase.create_in_folder(folder, case_id="c-013")
    try:
        abs_inside = folder / "assets" / "sub" / "x.txt"
        assert case.assets.relative_path(abs_inside) == "sub/x.txt"
        assert case.assets.relative_path("sub/./x.txt") == "sub/x.txt"
        with pytest.raises(ValueError):
            case.assets.relative_path(tmp_path / "outside.txt")
    finally:
        case.detach()


def test_guard_method_convention_constructs(tmp_path):
    """A `<token>#trigger` guard binds to `guard_<token>`; a correctly named async guard
    lets the case construct cleanly."""
    class GuardedCase(FolderBackedCase):
        fsm_state_chains = ["^new==funded#finish-->done^"]

        async def guard_funded(self, event):
            return True

    folder = tmp_path / "case-g1"
    case = GuardedCase.create_in_folder(folder, case_id="g-1")
    try:
        assert case.state == "new"
    finally:
        case.detach()


def test_orphan_guard_method_fails_construction(tmp_path):
    """A `guard_`-prefixed method whose token maps to no chain guard is treated as a typo
    and fails the build (orphan_detection defaults to error)."""
    class TypoGuardCase(FolderBackedCase):
        fsm_state_chains = ["^new==funded#finish-->done^"]

        async def guard_funded(self, event):
            return True

        async def guard_fundedd(self, event):  # typo: funded
            return True

    folder = tmp_path / "case-g2"
    with pytest.raises(FsmBindingError) as excinfo:
        TypoGuardCase.create_in_folder(folder, case_id="g-2")
    msg = str(excinfo.value)
    assert "guard_fundedd" in msg
    # Binding runs before any disk/lease I/O, so nothing is left claimed.
    assert not (folder / ".case.lease").exists()


def test_perform_hook_convention_wires_and_runs(tmp_path):
    """An auto edge (`--`) requires `perform_<trigger>`; the method binds to the
    transition's `before` and runs when the trigger fires."""
    class PerformCase(FolderBackedCase):
        fsm_state_chains = ["^new--begin-->open==finish-->done^"]
        performed = False

        async def perform_begin(self, event):
            self.performed = True

    folder = tmp_path / "case-p1"
    with PerformCase.create_in_folder(folder, case_id="p-1") as case:
        assert case.performed is False
        asyncio.run(case.begin())
        assert case.state == "open"
        assert case.performed is True


def test_legacy_underscore_perform_hook_is_rejected(tmp_path):
    """The trigger action hook dropped its leading underscore. An auto edge whose action
    method is still named `_perform_<trigger>` no longer satisfies the required
    `perform_<trigger>`, so the build fails — a deliberate pre-release breaking change."""
    class LegacyCase(FolderBackedCase):
        fsm_state_chains = ["^new--begin-->done^"]

        async def _perform_begin(self, event):  # old name, no longer recognized
            return None

    folder = tmp_path / "case-legacy"
    with pytest.raises(FsmBindingError) as excinfo:
        LegacyCase.create_in_folder(folder, case_id="legacy-1")
    msg = str(excinfo.value)
    assert "perform_begin" in msg
    # Binding runs before any disk/lease I/O, so nothing is left claimed.
    assert not (folder / ".case.lease").exists()


def test_generate_case_id_auto_bumps_on_same_millisecond(monkeypatch):
    fixed_seconds = 1_700_000_000.123
    monkeypatch.setattr("totodev_pub.folder_backed_case.time.time", lambda: fixed_seconds)
    first = SimpleCase.generate_case_id()
    second = SimpleCase.generate_case_id()
    assert second != first
    assert int(second, 36) == int(first, 36) + 1
