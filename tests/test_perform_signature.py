# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Unit + integration tests for perform_* signature-as-kwargs-contract."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.exceptions import (
    FsmBindingError,
    PerformParamsError,
)
from totodev_pub.folder_backed_case_support.perform_signature import (
    bind_perform_kwargs,
    format_perform_params,
    validate_perform_signature,
)


# ---------------------------------------------------------------------------
# validate_perform_signature
# ---------------------------------------------------------------------------


def test_validate_accepts_tctx_only():
    async def perform_go(self, tctx):
        pass

    assert validate_perform_signature(perform_go) == []


def test_validate_accepts_keyword_only_annotated():
    async def perform_go(self, tctx, *, path: str, force: bool = False):
        pass

    assert validate_perform_signature(perform_go) == []


def test_validate_rejects_positional_after_tctx():
    async def perform_go(self, tctx, path: str):
        pass

    problems = validate_perform_signature(perform_go)
    assert any("keyword-only" in p for p in problems)


def test_validate_rejects_missing_annotation():
    async def perform_go(self, tctx, *, path):
        pass

    problems = validate_perform_signature(perform_go)
    assert any("annotation" in p for p in problems)


def test_validate_rejects_varargs_and_varkw():
    async def with_args(self, tctx, *args):
        pass

    async def with_kwargs(self, tctx, **kwargs):
        pass

    assert any("*args" in p for p in validate_perform_signature(with_args))
    assert any("**kwargs" in p for p in validate_perform_signature(with_kwargs))


def test_validate_rejects_keyword_only_tctx():
    async def perform_go(self, *, tctx):
        pass

    problems = validate_perform_signature(perform_go)
    assert any("positional-or-keyword" in p for p in problems)


def test_validate_rejects_non_copyable_mutable_default():
    class Bag:
        __hash__ = None  # unhashable instance

    async def perform_go(self, tctx, *, bag: Bag = Bag()):
        pass

    problems = validate_perform_signature(perform_go)
    assert any("mutable default" in p for p in problems)


def test_validate_allows_list_dict_set_defaults():
    async def perform_go(
        self, tctx, *, tags: list = [], meta: dict = {}, ids: set = set(),
    ):
        pass

    assert validate_perform_signature(perform_go) == []


# ---------------------------------------------------------------------------
# bind_perform_kwargs
# ---------------------------------------------------------------------------


async def _sample(self, tctx, *, path: str, force: bool = False, note: str | None = None):
    pass


def test_bind_applies_defaults_and_accepts_required():
    bound = bind_perform_kwargs(_sample, {"path": "/a"})
    assert bound == {"path": "/a", "force": False, "note": None}


def test_bind_rejects_missing_required():
    with pytest.raises(PerformParamsError, match="missing required"):
        bind_perform_kwargs(_sample, {})


def test_bind_rejects_unexpected():
    with pytest.raises(PerformParamsError, match="unexpected"):
        bind_perform_kwargs(_sample, {"path": "/a", "extra": 1})


def test_bind_rejects_wrong_type():
    with pytest.raises(PerformParamsError, match="type checks"):
        bind_perform_kwargs(_sample, {"path": 123})


def test_bind_allows_any_unchecked():
    async def perform_go(self, tctx, *, blob: Any):
        pass

    assert bind_perform_kwargs(perform_go, {"blob": object()})["blob"] is not None


def test_bind_optional_union():
    async def perform_go(self, tctx, *, note: Optional[str] = None):
        pass

    assert bind_perform_kwargs(perform_go, {})["note"] is None
    assert bind_perform_kwargs(perform_go, {"note": "hi"})["note"] == "hi"
    with pytest.raises(PerformParamsError, match="type checks"):
        bind_perform_kwargs(perform_go, {"note": 1})


def test_bind_list_str_checks_origin_only():
    async def perform_go(self, tctx, *, paths: list[str]):
        pass

    assert bind_perform_kwargs(perform_go, {"paths": ["a", "b"]})["paths"] == ["a", "b"]
    # Origin-only: list of ints still passes as list.
    assert bind_perform_kwargs(perform_go, {"paths": [1, 2]})["paths"] == [1, 2]
    with pytest.raises(PerformParamsError, match="type checks"):
        bind_perform_kwargs(perform_go, {"paths": "nope"})


def test_bind_deepcopy_list_default_isolation():
    async def perform_go(self, tctx, *, tags: list = []):
        pass

    a = bind_perform_kwargs(perform_go, {})
    b = bind_perform_kwargs(perform_go, {})
    a["tags"].append("x")
    assert b["tags"] == []


def test_bind_empty_signature_rejects_any_kwargs():
    async def perform_go(self, tctx):
        pass

    assert bind_perform_kwargs(perform_go, {}) == {}
    with pytest.raises(PerformParamsError, match="unexpected"):
        bind_perform_kwargs(perform_go, {"x": 1})


def test_format_perform_params():
    lines = format_perform_params(_sample)
    assert "path: str" in lines
    assert any(s.startswith("force: bool") for s in lines)


# ---------------------------------------------------------------------------
# Define-time binding + runtime through a real case
# ---------------------------------------------------------------------------


class _ParamsCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = [
        "[*] --> new == examine ==> examined -- finish --> done --> [*]",
        "new == bare ==> bare_done --> [*]",
    ]
    seen: dict | None = None

    async def perform_examine(self, tctx, *, path: str, force: bool = False):
        self.seen = {"path": path, "force": force}

    async def perform_finish(self, tctx):
        pass


class _BadPositionalPerformCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new -- go --> done --> [*]"]

    async def perform_go(self, tctx, path: str):
        pass


class _NoPerformManualCase(FolderBackedCase):
    """Manual trigger with no perform_* — kwargs must not be validated."""

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new == nudge ==> done --> [*]"]


@pytest.mark.asyncio
async def test_binding_rejects_positional_perform_params(tmp_path):
    with pytest.raises(FsmBindingError) as ei:
        _BadPositionalPerformCase.create_case_in_folder(tmp_path / "bad", case_id="bad")
    assert ei.value.bad_perform_signatures
    assert "perform_go" in str(ei.value)


@pytest.mark.asyncio
async def test_runtime_binds_kwargs_into_perform(tmp_path):
    case = _ParamsCase.create_case_in_folder(tmp_path / "p", case_id="p")
    await case.examine(path="/tmp/x", force=True)
    assert case.seen == {"path": "/tmp/x", "force": True}
    assert case.case_state == "examined"


@pytest.mark.asyncio
async def test_runtime_rejects_extra_kwargs_on_no_arg_perform(tmp_path):
    case = _ParamsCase.create_case_in_folder(tmp_path / "p2", case_id="p2")
    await case.examine(path="/tmp/x")
    result = await case.case_advance(trigger="finish", trigger_kwargs={"nope": 1})
    assert result.failed
    assert any(isinstance(e, PerformParamsError) for e in result.exceptions)


@pytest.mark.asyncio
async def test_no_perform_skips_kwargs_validation(tmp_path):
    case = _NoPerformManualCase.create_case_in_folder(tmp_path / "n", case_id="n")
    # No perform_* — kwargs are accepted by transitions and ignored.
    await case.nudge(anything=123)
    assert case.case_state == "done"
