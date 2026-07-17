"""End-to-end tests for asset state validity and case_load_asset."""

import asyncio
import json
import logging

import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.exceptions import (
    AssetNotTrustedInStateError,
    AssetSchemaError,
)


class TicketForm(BaseModel, FileMappedPydanticMixin):
    title: str = ""


class ChatLog(BaseModel, FileMappedPydanticMixin):
    lines: int = 0


class TicketCase(FolderBackedCase):
    fsm_state_chains = ["^new==open_ticket-->open==close_ticket-->closed^"]
    asset_aliases = [
        AssetSpec(
            relative_path="ticket.yaml",
            loader=TicketForm,
            states={"new", "open", "closed"},
            keep=True,
        ),
        AssetSpec(
            relative_path="customer--conversation.json",
            loader=ChatLog,
            states={"open"},
        ),
    ]
    fsm_trigger_chokes = {}

    async def perform_open_ticket(self, tctx):
        pass

    async def perform_close_ticket(self, tctx):
        pass


class FlexibleCase(FolderBackedCase):
    flexible_asset_alias_loading = True
    fsm_state_chains = ["^new==go-->done^"]
    asset_aliases = [
        AssetSpec(relative_path="unguarded.json"),
        AssetSpec(relative_path="guarded.json", loader=TicketForm, states={"new"}),
    ]
    fsm_trigger_chokes = {}

    async def perform_go(self, tctx):
        pass


class ReclassSource(FolderBackedCase):
    fsm_state_chains = ["^new==go-->shared^"]
    asset_aliases = [
        AssetSpec(
            relative_path="old.yaml",
            loader=TicketForm,
            states={"new", "shared"},
            keep=True,
        ),
    ]
    fsm_trigger_chokes = {}

    async def perform_go(self, tctx):
        pass


class ReclassTarget(FolderBackedCase):
    fsm_state_chains = ["^new==go-->shared^"]
    asset_aliases = [
        AssetSpec(relative_path="new.yaml", loader=ChatLog, states={"shared"}, keep=True),
    ]
    fsm_trigger_chokes = {}

    async def perform_go(self, tctx):
        pass


def test_empty_declaration_warns(caplog):
    class EmptyAliasesCase(FolderBackedCase):
        asset_aliases = []
        fsm_trigger_chokes = {}
        fsm_state_chains = ["^new--begin-->done^"]

        async def perform_begin(self, tctx):
            pass

    with caplog.at_level(logging.WARNING):
        assert EmptyAliasesCase._asset_book.aliases() == []
    assert any("empty" in r.message.lower() for r in caplog.records)


def test_build_time_validation_at_class_definition():
    with pytest.raises(AssetSchemaError, match="opne"):

        class BadStateCase(FolderBackedCase):
            asset_aliases = [
                AssetSpec(relative_path="a.json", loader=TicketForm, states={"opne"}),
            ]
            fsm_trigger_chokes = {}
            fsm_state_chains = ["^new--begin-->done^"]

            async def perform_begin(self, tctx):
                pass


def test_guarded_load_allowed_in_listed_state(tmp_path):
    folder = tmp_path / "ticket-1"
    case = TicketCase.create_case_in_folder(folder)
    try:
        case.case_assets.write(
            "ticket.yaml",
            b"title: hello\n",
        )
        obj = case.case_load_asset("ticket")
        assert isinstance(obj, TicketForm)
        assert obj.title == "hello"
    finally:
        case.case_detach()


def test_guarded_load_denied_in_unlisted_state(tmp_path):
    folder = tmp_path / "ticket-2"
    case = TicketCase.create_case_in_folder(folder)
    try:
        asyncio.run(case.open_ticket())
        asyncio.run(case.close_ticket())
        case.case_assets.write("customer--conversation.json", b"lines: 1\n")
        with pytest.raises(AssetNotTrustedInStateError):
            case.case_load_asset("conversation")
    finally:
        case.case_detach()


def test_guarded_load_denied_initial_state_for_constrained_alias(tmp_path):
    folder = tmp_path / "ticket-3"
    case = TicketCase.create_case_in_folder(folder)
    try:
        case.case_assets.write("customer--conversation.json", b"lines: 1\n")
        with pytest.raises(AssetNotTrustedInStateError):
            case.case_load_asset("conversation")
    finally:
        case.case_detach()


def test_guarded_load_keyerror_unknown_alias(tmp_path):
    folder = tmp_path / "ticket-4"
    case = TicketCase.create_case_in_folder(folder)
    try:
        with pytest.raises(KeyError):
            case.case_load_asset("missing")
    finally:
        case.case_detach()


def test_guarded_load_file_not_found_when_trusted(tmp_path):
    folder = tmp_path / "ticket-5"
    case = TicketCase.create_case_in_folder(folder)
    try:
        with pytest.raises(FileNotFoundError):
            case.case_load_asset("ticket")
    finally:
        case.case_detach()


def test_reader_parity_with_live_case(tmp_path):
    folder = tmp_path / "ticket-6"
    case = TicketCase.create_case_in_folder(folder)
    try:
        case.case_assets.write("ticket.yaml", b"title: from-disk\n")
        asyncio.run(case.open_ticket())
        case.case_assets.write(
            "customer--conversation.json", json.dumps({"lines": 2}).encode()
        )
    finally:
        case.case_detach()

    reader = FolderBackedCaseReader(folder)
    assert reader.case_state == "open"
    reader.case_load_asset("ticket")
    reader.case_load_asset("conversation")

    live = TicketCase(folder)
    try:
        assert live.case_state == "open"
        live.case_load_asset("ticket")
        live.case_load_asset("conversation")

    finally:
        live.case_detach()
    live = TicketCase(folder)
    try:
        asyncio.run(live.close_ticket())
        with pytest.raises(AssetNotTrustedInStateError):
            live.case_load_asset("conversation")

    finally:
        live.case_detach()
    reader_closed = FolderBackedCaseReader(folder)
    with pytest.raises(AssetNotTrustedInStateError):
        reader_closed.case_load_asset("conversation")


def test_flexible_mode_unconstrained_alias_bypasses_guard(tmp_path):
    folder = tmp_path / "flex-1"
    case = FlexibleCase.create_case_in_folder(folder)
    try:
        case.case_assets.write("unguarded.json", json.dumps({"x": 1}).encode())
        obj = case.case_load_asset("unguarded")
        assert obj is not None
    finally:
        case.case_detach()


def test_flexible_mode_declared_states_still_guarded(tmp_path):
    folder = tmp_path / "flex-2"
    case = FlexibleCase.create_case_in_folder(folder)
    try:
        asyncio.run(case.go())
        case.case_assets.write("guarded.json", b"title: x\n")
        with pytest.raises(AssetNotTrustedInStateError):
            case.case_load_asset("guarded")
    finally:
        case.case_detach()


def test_keep_true_seeds_manifest_at_create(tmp_path):
    folder = tmp_path / "ticket-keep"
    case = TicketCase.create_case_in_folder(folder)
    try:
        assert "ticket.yaml" in case.case_assets.keep_list()
        assert "assets/ticket.yaml" in case.case_assets.keep_manifest.list_rules()
        case.case_assets.write("ticket.yaml", b"title: kept\n")
        case.case_assets.write("scratch.txt", b"ephemeral")
        purged = case._keep_manifest.purge()
        # logs/case.log is purged too — one chokepoint for ALL case ephemera, logs
        # included, unless a keep rule covers them (not the concern of this test).
        assert purged == ["assets/scratch.txt", "logs/case.log"]
        assert case.case_assets.asset_path("ticket.yaml").exists()
    finally:
        case.case_detach()


def test_reclassify_restamps_states_and_keep(tmp_path):
    folder = tmp_path / "reclass"
    case = ReclassSource.create_case_in_folder(folder)
    try:
        asyncio.run(case.go())
        assert "old.yaml" in case.case_assets.keep_list()
        fresh = case.case_reclassify_to(ReclassTarget)
        assert fresh._record.asset_aliases["new"]["states"] == ["shared"]
        assert "new.yaml" in fresh.case_assets.keep_list()

    finally:
        case.case_detach()


def test_bypass_via_case_assets_ignores_gate(tmp_path):
    folder = tmp_path / "bypass"
    case = TicketCase.create_case_in_folder(folder)
    try:
        case.case_assets.write(
            "customer--conversation.json", json.dumps({"lines": 9}).encode()
        )
        obj = case.case_assets.load_dataclass("conversation")
        assert isinstance(obj, ChatLog)
        assert obj.lines == 9
    finally:
        case.case_detach()


def test_case_keep_files_retains_custom_file(tmp_path):
    folder = tmp_path / "custom-keep"
    case = TicketCase.create_case_in_folder(folder)
    try:
        export = folder / "exports" / "summary.pdf"
        export.parent.mkdir(parents=True)
        export.write_bytes(b"%PDF-summary")
        case.case_keep_files("exports/summary.pdf")
        case.case_assets.write("scratch.txt", b"ephemeral")
        purged = case._keep_manifest.purge()
        assert "assets/scratch.txt" in purged
        assert export.exists()
        assert export.read_bytes() == b"%PDF-summary"
    finally:
        case.case_detach()


def test_close_purge_retains_framework_and_kept_assets(tmp_path):
    folder = tmp_path / "close-purge"
    case = TicketCase.create_case_in_folder(folder, case_id="t-close")
    try:
        case.case_assets.write("ticket.yaml", b"title: kept\n")
        case.case_assets.write("ephemeral.txt", b"gone")
        asyncio.run(case.open_ticket())
        asyncio.run(case.close_ticket())
        assert case.case_is_terminal
    finally:
        case.case_detach()
    assert (folder / "case_record.yaml").exists()
    assert list((folder / "events").rglob("*"))  # event log present
    assert (folder / "assets" / "ticket.yaml").exists()
    assert not (folder / "assets" / "ephemeral.txt").exists()
    assert (folder / "_keep.txt").exists()
