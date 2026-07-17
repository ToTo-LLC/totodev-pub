from pathlib import Path

import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.asset_schema import (
    AssetSpec,
    CALLABLE_SENTINEL,
    PATH_LOADER_SENTINEL,
    loader_name,
)


class _Des:
    def __call__(self, path):  # pragma: no cover
        return path


class _Rec(BaseModel, FileMappedPydanticMixin):
    n: int = 0


def test_asset_spec_is_frozen():
    spec = AssetSpec(alias="rlist", relative_path="receipts/rlist.json")
    assert spec.alias == "rlist"
    assert spec.relative_path == "receipts/rlist.json"
    assert spec.loader is None
    assert spec.states is None
    assert spec.keep is False
    assert spec.many is False


def test_asset_spec_states_keep_and_many():
    spec = AssetSpec(
        alias="ticket", relative_path="ticket.yaml", loader=_Rec,
        states=frozenset({"new", "open"}), keep=True,
    )
    assert spec.states == frozenset({"new", "open"})
    assert spec.keep is True
    many = AssetSpec(
        alias="pages", relative_path="pages/*.png", loader=Path, many=True,
    )
    assert many.many is True
    assert many.loader is Path


def test_asset_spec_is_keyword_only():
    with pytest.raises(TypeError):
        AssetSpec("rlist", "receipts/rlist.json")  # noqa — deliberate positional


def test_asset_spec_requires_alias():
    with pytest.raises(TypeError):
        AssetSpec(relative_path="ticket.yaml", loader=_Rec)  # noqa — alias omitted


def test_loader_name_filemapped_class():
    assert loader_name(_Rec) == "_Rec"


def test_loader_name_path():
    assert loader_name(Path) == PATH_LOADER_SENTINEL


def test_loader_name_callable():
    assert loader_name(_Des()) == CALLABLE_SENTINEL


def test_loader_name_none():
    assert loader_name(None) is None
