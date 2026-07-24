"""Tests for CaseTypeSpec and FsmChainSpec scheduling query helpers."""

from dataclasses import FrozenInstanceError

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_spec import CaseTypeSpec
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


class SimpleCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new -- step --> open == finish ==> done --> [*]"]

    async def perform_step(self, tctx):
        pass

    async def perform_finish(self, tctx):
        pass


class MultiAutoCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {
        "fast": {"cpu"},
        "slow": {"cpu", "ms-graph-api"},
    }
    fsm_state_chains = ["[*] --> fork -- fast --> a --> [*]", "[*] --> fork -- slow --> b --> [*]"]

    async def perform_fast(self, tctx):
        pass

    async def perform_slow(self, tctx):
        pass


class ChokedCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {
        "analyze": {"cpu", "ms-graph-api"},
    }
    fsm_state_chains = ["[*] --> new -- analyze --> done --> [*]"]

    async def perform_analyze(self, tctx):
        pass


def test_case_type_spec_returns_cached_singletons():
    spec = SimpleCase.case_type_spec()
    assert spec.fsm is SimpleCase._fsm
    assert spec.assets is SimpleCase._resolve_asset_book()
    assert spec.assets.aliases() == []


def test_case_type_spec_is_frozen():
    spec = SimpleCase.case_type_spec()
    with pytest.raises(FrozenInstanceError):
        spec.fsm = SimpleCase._fsm  # type: ignore[misc]


def test_declared_choke_resources_empty():
    assert SimpleCase.case_type_spec().declared_choke_resources() == frozenset()


def test_declared_choke_resources_union():
    expected = frozenset({"cpu", "ms-graph-api"})
    assert MultiAutoCase.case_type_spec().declared_choke_resources() == expected


def test_auto_triggers_from_and_has_auto_exits():
    fsm = SimpleCase._fsm
    assert fsm.auto_triggers_from("new") == ("step",)
    assert fsm.has_auto_exits("new") is True
    assert fsm.auto_triggers_from("open") == ()
    assert fsm.has_auto_exits("open") is False


def test_auto_edges_from_declared_order():
    fsm = MultiAutoCase._fsm
    assert fsm.auto_edges_from("fork") == [("fast", "a"), ("slow", "b")]
    assert fsm.auto_triggers_from("fork") == ("fast", "slow")


def test_pending_chokes_for_union_over_auto_exits():
    fsm = MultiAutoCase._fsm
    assert fsm.pending_chokes_for("fork") == frozenset({"cpu", "ms-graph-api"})
    assert fsm.pending_chokes_for("a") == frozenset()


def test_pending_chokes_for_integration_smoke():
    spec = ChokedCase.case_type_spec()
    assert spec.fsm.pending_chokes_for("new") == frozenset({"cpu", "ms-graph-api"})


def test_auto_edges_from_on_compiled_fsm(tmp_path):
    folder = tmp_path / "case"
    folder.mkdir()
    case = SimpleCase.create_case_in_folder(folder)
    try:
        assert case.case_type_spec().fsm.auto_edges_from("new") == [("step", "open")]
    finally:
        case.case_detach()
