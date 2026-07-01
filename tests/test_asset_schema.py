import pytest

from totodev_pub.folder_backed_case_support.asset_schema import (
    AssetSpec,
    CALLABLE_SENTINEL,
    infer_alias,
    loader_name,
)
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from pydantic import BaseModel


class _Des:
    def __call__(self, path):  # pragma: no cover
        return path


class _Rec(BaseModel, FileMappedPydanticMixin):
    n: int = 0


def test_infer_alias_stem_when_no_delimiter():
    assert infer_alias("myfiles/rlist.xyz") == "rlist"
    assert infer_alias("reconciliation.abc") == "reconciliation"


def test_infer_alias_after_last_delimiter():
    assert infer_alias("somestuff--alias1.pdq") == "alias1"
    assert infer_alias("a/b/Overall_Receipts--rlist.json") == "rlist"


def test_infer_alias_is_always_substring_of_filename():
    for key in ["myfiles/rlist.xyz", "reconciliation.abc", "somestuff--alias1.pdq"]:
        assert infer_alias(key) in key


def test_asset_spec_is_frozen():
    spec = AssetSpec("rlist", "receipts/rlist.json", None)
    assert spec.alias == "rlist"
    assert spec.relative_path == "receipts/rlist.json"
    assert spec.loader is None
    assert spec.states is None
    assert spec.keep is False


def test_asset_spec_states_and_keep():
    spec = AssetSpec(
        "ticket", "ticket.yaml", _Rec, states=frozenset({"new", "open"}), keep=True,
    )
    assert spec.states == frozenset({"new", "open"})
    assert spec.keep is True


def test_loader_name_filemapped_class():
    assert loader_name(_Rec) == "_Rec"


def test_loader_name_callable():
    assert loader_name(_Des()) == CALLABLE_SENTINEL


def test_loader_name_none():
    assert loader_name(None) is None
