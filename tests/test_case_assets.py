import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets


class _Doc(BaseModel, FileMappedPydanticMixin):
    name: str = ""
    n: int = 0


def _write_json(assets, rel, obj):
    assets.write(rel, json.dumps(obj).encode("utf-8"))


def test_load_dataclass_filemapped(tmp_path):
    assets = CaseAssets(
        tmp_path / "c1",
        asset_specs={"doc": AssetSpec(relative_path="sub/doc.json", loader=_Doc)},
    )
    _write_json(assets, "sub/doc.json", {"name": "hi", "n": 3})
    doc = assets.load_dataclass("doc")
    assert isinstance(doc, _Doc) and doc.name == "hi" and doc.n == 3
    assert assets.dataclass_path("doc") == assets.asset_path("sub/doc.json")


def test_load_dataclass_callable(tmp_path):
    assets = CaseAssets(
        tmp_path / "c2",
        asset_specs={
            "raw": AssetSpec(relative_path="raw.json", loader=lambda p: p.read_text(),
            )
        },
    )
    _write_json(assets, "raw.json", {"k": 1})
    assert assets.load_dataclass("raw") == '{"k": 1}'


def test_load_dataclass_path_loader(tmp_path):
    assets = CaseAssets(
        tmp_path / "c2b",
        asset_specs={
            "page": AssetSpec(relative_path="inbox/page.png", loader=Path),
        },
    )
    path = assets.write("inbox/page.png", b"png")
    assert assets.load_dataclass("page") == path


def test_load_dataclass_missing_file_raises(tmp_path):
    assets = CaseAssets(
        tmp_path / "c3",
        asset_specs={"doc": AssetSpec(relative_path="doc.json", loader=_Doc)},
    )
    with pytest.raises(FileNotFoundError):
        assets.load_dataclass("doc")


def test_unknown_alias_raises_keyerror(tmp_path):
    assets = CaseAssets(tmp_path / "c4", asset_specs={})
    with pytest.raises(KeyError):
        assets.load_dataclass("nope")


def test_glob_paths_and_file_loading(tmp_path):
    assets = CaseAssets(
        tmp_path / "c5",
        asset_specs={
            "scans": AssetSpec(relative_path="scans/*.json", loader=_Doc,
            ),
        },
    )
    _write_json(assets, "scans/a.json", {"name": "a"})
    _write_json(assets, "scans/b.json", {"name": "b"})
    paths = assets.dataclass_paths("scans")
    assert [p.name for p in paths] == ["a.json", "b.json"]
    with pytest.raises(ValueError):
        assets.dataclass_path("scans")
    loaded = [assets.load_dataclass_file(p.relative_to(assets.folder).as_posix()) for p in paths]
    assert sorted(d.name for d in loaded) == ["a", "b"]


def test_load_dataclasses_many_returns_list(tmp_path):
    assets = CaseAssets(
        tmp_path / "c5b",
        asset_specs={
            "scans": AssetSpec(relative_path="scans/*.json", loader=_Doc, many=True,
            ),
        },
    )
    _write_json(assets, "scans/a.json", {"name": "a"})
    _write_json(assets, "scans/b.json", {"name": "b"})
    loaded = assets.load_dataclasses("scans")
    assert [d.name for d in loaded] == ["a", "b"]
    with pytest.raises(ValueError, match="many=True"):
        assets.load_dataclass("scans")


def test_load_dataclasses_empty_when_no_matches(tmp_path):
    assets = CaseAssets(
        tmp_path / "c5c",
        asset_specs={
            "attachments": AssetSpec(relative_path="attachments/*", loader=Path,
                many=True,
            ),
        },
    )
    assert assets.load_dataclasses("attachments") == []


def test_load_dataclasses_rejects_singular_alias(tmp_path):
    assets = CaseAssets(
        tmp_path / "c5d",
        asset_specs={"doc": AssetSpec(relative_path="doc.json", loader=_Doc)},
    )
    with pytest.raises(ValueError, match="not many=True"):
        assets.load_dataclasses("doc")


def test_flexible_loading_returns_lazy(tmp_path):
    assets = CaseAssets(
        tmp_path / "c6",
        asset_specs={"cfg": AssetSpec(relative_path="cfg.json")},
        flexible_asset_alias_loading=True,
    )
    _write_json(assets, "cfg.json", {"feature": True})
    lazy = assets.load_dataclass("cfg")
    assert isinstance(lazy, LazyLoadedFileData)
    assert lazy.as_dict()["feature"] is True