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
)


# ---------------------------------------------------------------------------
# Minimal concrete subclass used across all tests
# ---------------------------------------------------------------------------

class SimpleCase(FolderBackedCase):
    fsm_state_chains = ["^new--begin-->open--finish-->done^"]


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
        asyncio.get_event_loop().run_until_complete(case.begin())
        assert case.state == "open"
        asyncio.get_event_loop().run_until_complete(case.finish())
        assert case.state == "done"
        assert case.is_closed


def test_peek_record_and_events(tmp_path):
    folder = tmp_path / "case-005"
    with SimpleCase.create_in_folder(folder, case_id="c-005") as case:
        asyncio.get_event_loop().run_until_complete(case.begin())

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
