"""Tests for FolderBackedCaseReader and CaseReadView preparatory properties."""

import asyncio
import json
import os
import time

import pytest
from pathlib import Path

from pydantic import BaseModel

from totodev_pub.folder_backed_case import FolderBackedCase, FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.constants import LEASE_NAME
from totodev_pub.folder_backed_case_support.asset_dataclass_registry import (
    asset_dataclass_registry,
)
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData


class SimpleCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]


def test_get_case_reader_factory(tmp_path):
    folder = tmp_path / "reader-001"
    _case = SimpleCase.create_case_in_folder(folder, case_id="r-001")
    try:
        pass
    finally:
        _case.case_detach()
    reader = FolderBackedCase.get_case_reader(folder)
    assert isinstance(reader, FolderBackedCaseReader)
    assert reader.case_folder == folder


def test_reader_identity_fields_match_peek(tmp_path):
    folder = tmp_path / "reader-002"
    _case = SimpleCase.create_case_in_folder(
        folder, case_id="r-002", external_key="ext-002", nickname="nick"
    )
    try:
        pass

    finally:
        _case.case_detach()
    reader = FolderBackedCaseReader(folder)
    record = FolderBackedCase.peek_case_record(folder)

    assert reader.case_id == record.case_id == "r-002"
    assert reader.case_external_key == record.external_key == "ext-002"
    assert reader.case_nickname == record.nickname == "nick"
    assert reader.case_object_type == record.case_object_type == "SimpleCase"
    assert reader.case_created == record.created
    assert reader.case_terminal_at is None


def test_reader_state_after_transition(tmp_path):
    folder = tmp_path / "reader-003"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        asyncio.run(case.begin())

    finally:
        case.case_detach()
    reader = FolderBackedCaseReader(folder)
    assert reader.case_state == "open"
    assert not reader.case_is_terminal
    assert reader.case_is_live
    assert reader.case_events.current_state == "open"
    assert (
        reader.case_events.current_state
        == FolderBackedCase.peek_case_events(folder).current_state
    )


def test_reader_terminal_case(tmp_path):
    folder = tmp_path / "reader-004"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        asyncio.run(case.begin())
        asyncio.run(case.finish())

    finally:
        case.case_detach()
    reader = FolderBackedCaseReader(folder)
    assert reader.case_state == "done"
    assert reader.case_is_terminal
    assert not reader.case_is_live
    assert reader.case_terminal_at is not None


def test_reader_dwell_secs_non_negative(tmp_path):
    folder = tmp_path / "reader-005"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        asyncio.run(case.begin())

    finally:
        case.case_detach()
    reader = FolderBackedCaseReader(folder)
    first = reader.case_dwell_secs
    assert first >= 0
    time.sleep(0.02)
    assert reader.case_dwell_secs >= first


def test_reader_assets_and_events(tmp_path):
    folder = tmp_path / "reader-006"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        asyncio.run(case.begin())
        (case.case_assets.folder / "note.txt").write_text("hi")

    finally:
        case.case_detach()
    reader = FolderBackedCaseReader(folder)
    assert "note.txt" in reader.case_assets.list_assets()
    assert reader.case_assets.asset_path("note.txt").read_text() == "hi"
    assert (
        reader.case_events.current_state
        == FolderBackedCase.peek_case_events(folder).current_state
    )


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

    # Lapsed lease (file present, past expiry) is negative — distinct from absent.
    past = time.time() - 30
    (folder / LEASE_NAME).touch()
    os.utime(folder / LEASE_NAME, (past, past))
    reader = FolderBackedCaseReader(folder)
    assert reader.case_lease_secs_left is not None
    assert reader.case_lease_secs_left < 0


def test_reader_does_not_acquire_lease(tmp_path):
    folder = tmp_path / "reader-008"
    _case = SimpleCase.create_case_in_folder(folder)
    try:
        pass

    finally:
        _case.case_detach()
    assert not (folder / LEASE_NAME).exists()
    _ = FolderBackedCaseReader(folder)
    assert not (folder / LEASE_NAME).exists()


def test_reader_has_no_write_surface(tmp_path):
    folder = tmp_path / "reader-009"
    _case = SimpleCase.create_case_in_folder(folder)
    try:
        pass

    finally:
        _case.case_detach()
    reader = FolderBackedCaseReader(folder)
    for name in (
        "case_advance",
        "case_detach",
        "case_heartbeat",
        "case_log_alert",
        "case_fetch_record",
    ):
        assert not hasattr(reader, name)


class SlowWorkCase(FolderBackedCase):
    """One auto edge whose perform sleeps — enough to observe a trigger in flight."""

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^new--work-->done^"]

    sleep_secs: float = 0.3

    async def perform_work(self, tctx):
        await asyncio.sleep(self.sleep_secs)


def test_reader_active_trigger_visible_while_work_runs(tmp_path):
    folder = tmp_path / "reader-011"

    async def scenario():
        case = SlowWorkCase.create_case_in_folder(folder)
        try:
            reader = FolderBackedCaseReader(folder)
            assert reader.case_active_trigger is None  # nothing in flight yet

            task = asyncio.create_task(case.case_advance())
            await asyncio.sleep(0.1)  # work is mid-sleep
            active = reader.case_active_trigger
            assert active is not None
            assert active.trigger == "work"
            assert active.elapsed_secs >= 0

            result = await task
            assert result.progressed
            assert reader.case_active_trigger is None  # resolved by the state entry

        finally:
            case.case_detach()

    asyncio.run(scenario())


def test_reader_active_trigger_requires_live_lease(tmp_path):
    """A dangling CASE_TRIGGER_STARTED whose owner is gone (lease absent/expired) reads
    as NOT active — that folder crashed mid-work; it is not in flight."""
    folder = tmp_path / "reader-012"
    case = SlowWorkCase.create_case_in_folder(folder)
    try:
        case._journal.log_trigger_started("work", state="new", warn=5.0, kill=10.0)
        # While the owner is live (lease held), the dangling START reads as active.
        assert FolderBackedCaseReader(folder).case_active_trigger is not None
    finally:
        case.case_detach()
    # Owner detached: same log contents, no live lease -> not active.
    assert FolderBackedCaseReader(folder).case_active_trigger is None


def test_live_case_prep_properties(tmp_path):
    folder = tmp_path / "reader-010"
    case = SimpleCase.create_case_in_folder(folder, case_id="r-010", nickname="live")
    try:
        assert case.case_object_type == "SimpleCase"
        assert case.case_created == case._record.created
        assert case.case_terminal_at is None
        assert case.case_events is case._journal.reader
        assert case.case_last_activity is not None
        asyncio.run(case.begin())
        assert case.case_state == "open"

    finally:
        case.case_detach()


class ReceiptCase(FolderBackedCase):
    flexible_asset_alias_loading = True
    asset_aliases = {"receipts/rlist.json": (lambda p: p.read_text())}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^new--begin-->done^"]

    async def perform_begin(self, tctx):
        pass


def test_reader_loads_declared_object_flexibly(tmp_path):
    (tmp_path / "case").mkdir()
    folder = tmp_path / "case" / "r1"
    case = ReceiptCase.create_case_in_folder(folder)
    try:
        case.case_assets.write("receipts/rlist.json", json.dumps({"total": 9}).encode())
    finally:
        case.case_detach()

    reader = FolderBackedCaseReader(folder)
    assets = reader.case_assets
    assert assets.registered_aliases() == ["rlist"]
    obj = assets.load_dataclass("rlist")
    assert isinstance(obj, LazyLoadedFileData)
    assert obj.as_dict()["total"] == 9


class ReceiptListRecord(BaseModel, FileMappedPydanticMixin):
    total: int = 0


class TypedReceiptCase(FolderBackedCase):
    flexible_asset_alias_loading = True
    asset_aliases = {"receipts/rlist.json": ReceiptListRecord}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^new--begin-->done^"]

    async def perform_begin(self, tctx):
        pass


def test_reader_typed_resolution_opt_in(tmp_path):
    (tmp_path / "case").mkdir()
    folder = tmp_path / "case" / "typed"
    case = TypedReceiptCase.create_case_in_folder(folder)
    try:
        # Persisted loader name mirrors the in-code class.
        assert case._record.asset_aliases["rlist"]["loader"] == "ReceiptListRecord"
        case.case_assets.write(
            "receipts/rlist.json", json.dumps({"total": 42}).encode()
        )
    finally:
        case.case_detach()

    # Default: generic lazy load (zero dependency).
    assert isinstance(
        FolderBackedCaseReader(folder).case_assets.load_dataclass("rlist"),
        LazyLoadedFileData,
    )

    # Opt-in without registration: still falls back to lazy.
    opted = FolderBackedCaseReader(folder, resolve_asset_types=True)
    assert isinstance(opted.case_assets.load_dataclass("rlist"), LazyLoadedFileData)

    # Opt-in with registration: typed load.
    asset_dataclass_registry.register(ReceiptListRecord)
    try:
        obj = FolderBackedCaseReader(
            folder, resolve_asset_types=True
        ).case_assets.load_dataclass("rlist")
        assert isinstance(obj, ReceiptListRecord)
        assert obj.total == 42
    finally:
        asset_dataclass_registry._registry.pop("ReceiptListRecord", None)


def test_reader_typed_resolution_callable_sentinel_falls_back(tmp_path):
    (tmp_path / "case").mkdir()
    folder = tmp_path / "case" / "sentinel"
    case = ReceiptCase.create_case_in_folder(folder)  # lambda loader -> "Callable"
    try:
        assert case._record.asset_aliases["rlist"]["loader"] == "Callable"
        case.case_assets.write("receipts/rlist.json", json.dumps({"total": 1}).encode())
    finally:
        case.case_detach()

    reader = FolderBackedCaseReader(folder, resolve_asset_types=True)
    assert isinstance(reader.case_assets.load_dataclass("rlist"), LazyLoadedFileData)
