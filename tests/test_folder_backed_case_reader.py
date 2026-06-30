"""Tests for FolderBackedCaseReader and CaseReadView preparatory properties."""

import asyncio
import time

import pytest
from pathlib import Path

from totodev_pub.folder_backed_case import FolderBackedCase, FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.constants import LEASE_NAME


class SimpleCase(FolderBackedCase):
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]


def test_get_case_reader_factory(tmp_path):
    folder = tmp_path / "reader-001"
    with SimpleCase.create_case_in_folder(folder, case_id="r-001"):
        pass
    reader = FolderBackedCase.get_case_reader(folder)
    assert isinstance(reader, FolderBackedCaseReader)
    assert reader.case_folder == folder


def test_reader_identity_fields_match_peek(tmp_path):
    folder = tmp_path / "reader-002"
    with SimpleCase.create_case_in_folder(
        folder, case_id="r-002", external_key="ext-002", nickname="nick"
    ):
        pass

    reader = FolderBackedCaseReader(folder)
    record = FolderBackedCase.peek_case_record(folder)

    assert reader.case_id == record.case_id == "r-002"
    assert reader.case_external_key == record.external_key == "ext-002"
    assert reader.case_nickname == record.nickname == "nick"
    assert reader.case_object_type == record.case_object_type == "SimpleCase"
    assert reader.case_created == record.created
    assert reader.case_closed_at is None


def test_reader_state_after_transition(tmp_path):
    folder = tmp_path / "reader-003"
    with SimpleCase.create_case_in_folder(folder) as case:
        asyncio.run(case.begin())

    reader = FolderBackedCaseReader(folder)
    assert reader.case_state == "open"
    assert not reader.case_is_closed
    assert reader.case_is_open
    assert reader.case_events.current_state == "open"
    assert reader.case_events.current_state == FolderBackedCase.peek_case_events(folder).current_state


def test_reader_closed_case(tmp_path):
    folder = tmp_path / "reader-004"
    with SimpleCase.create_case_in_folder(folder) as case:
        asyncio.run(case.begin())
        asyncio.run(case.finish())

    reader = FolderBackedCaseReader(folder)
    assert reader.case_state == "done"
    assert reader.case_is_closed
    assert not reader.case_is_open
    assert reader.case_closed_at is not None


def test_reader_dwell_secs_non_negative(tmp_path):
    folder = tmp_path / "reader-005"
    with SimpleCase.create_case_in_folder(folder) as case:
        asyncio.run(case.begin())

    reader = FolderBackedCaseReader(folder)
    first = reader.case_dwell_secs
    assert first >= 0
    time.sleep(0.02)
    assert reader.case_dwell_secs >= first


def test_reader_assets_and_events(tmp_path):
    folder = tmp_path / "reader-006"
    with SimpleCase.create_case_in_folder(folder) as case:
        asyncio.run(case.begin())
        (case.case_assets.folder / "note.txt").write_text("hi")

    reader = FolderBackedCaseReader(folder)
    assert "note.txt" in reader.case_assets.list_assets()
    assert reader.case_assets.asset_path("note.txt").read_text() == "hi"
    assert reader.case_events.current_state == FolderBackedCase.peek_case_events(folder).current_state


def test_reader_lease_observability(tmp_path):
    folder = tmp_path / "reader-007"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        reader = FolderBackedCaseReader(folder)
        assert reader.case_lease_secs_left is not None
        assert reader.case_lease_secs_left > 0
        assert (folder / LEASE_NAME).exists()
    finally:
        case.case_detach()

    reader = FolderBackedCaseReader(folder)
    assert reader.case_lease_secs_left is None
    assert not (folder / LEASE_NAME).exists()


def test_reader_does_not_acquire_lease(tmp_path):
    folder = tmp_path / "reader-008"
    with SimpleCase.create_case_in_folder(folder):
        pass

    assert not (folder / LEASE_NAME).exists()
    _ = FolderBackedCaseReader(folder)
    assert not (folder / LEASE_NAME).exists()


def test_reader_has_no_write_surface(tmp_path):
    folder = tmp_path / "reader-009"
    with SimpleCase.create_case_in_folder(folder):
        pass

    reader = FolderBackedCaseReader(folder)
    for name in (
        "case_advance",
        "case_detach",
        "case_heartbeat",
        "case_log_alert",
        "case_fetch_record",
    ):
        assert not hasattr(reader, name)


def test_live_case_prep_properties(tmp_path):
    folder = tmp_path / "reader-010"
    with SimpleCase.create_case_in_folder(folder, case_id="r-010", nickname="live") as case:
        assert case.case_object_type == "SimpleCase"
        assert case.case_created == case._record.created
        assert case.case_closed_at is None
        assert case.case_events is case._journal.reader
        assert case.case_last_activity is not None
        asyncio.run(case.begin())
        assert case.case_state == "open"
