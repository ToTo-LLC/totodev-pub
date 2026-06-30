from pathlib import Path

import json
import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets


def _write_text(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_add_keep_rules_accepts_absolute_exact_path(tmp_path):
    assets = CaseAssets(tmp_path / "case-abs-exact")
    _write_text(assets.asset_path("keep/me.txt"))
    _write_text(assets.asset_path("drop/me.txt"))

    absolute_keep = assets.asset_path("keep/me.txt")
    assets.add_keep_rules(str(absolute_keep))

    assert assets.keep_list() == ["keep/me.txt"]
    purged = assets.purge_ephemeral()

    assert purged == ["drop/me.txt"]
    assert assets.asset_path("keep/me.txt").exists()
    assert not assets.asset_path("drop/me.txt").exists()


def test_add_keep_rules_accepts_absolute_glob_path(tmp_path):
    assets = CaseAssets(tmp_path / "case-abs-glob")
    _write_text(assets.asset_path("results/a.json"))
    _write_text(assets.asset_path("results/b.txt"))
    _write_text(assets.asset_path("other/c.json"))

    absolute_glob = f"{assets.folder.as_posix()}/results/*.json"
    assets.add_keep_rules(absolute_glob)

    assert assets.keep_list() == ["results/*.json"]
    purged = assets.purge_ephemeral()

    assert purged == ["other/c.json", "results/b.txt"]
    assert assets.asset_path("results/a.json").exists()
    assert not assets.asset_path("results/b.txt").exists()
    assert not assets.asset_path("other/c.json").exists()


def test_remove_keep_rules_normalizes_absolute_rules(tmp_path):
    assets = CaseAssets(tmp_path / "case-remove-abs")
    assets.add_keep_rules("reports/*.csv")
    absolute_glob = f"{assets.folder.as_posix()}/reports/*.csv"

    assets.remove_keep_rules(absolute_glob)

    assert assets.keep_list() == []


class _Doc(BaseModel, FileMappedPydanticMixin):
    name: str = ""
    n: int = 0


def _write_json(assets, rel, obj):
    assets.write(rel, json.dumps(obj).encode("utf-8"))


def test_load_dataclass_filemapped(tmp_path):
    assets = CaseAssets(
        tmp_path / "c1",
        asset_specs={"doc": AssetSpec("doc", "sub/doc.json", _Doc)},
    )
    _write_json(assets, "sub/doc.json", {"name": "hi", "n": 3})
    doc = assets.load_dataclass("doc")
    assert isinstance(doc, _Doc) and doc.name == "hi" and doc.n == 3
    assert assets.dataclass_path("doc") == assets.asset_path("sub/doc.json")


def test_load_dataclass_callable(tmp_path):
    assets = CaseAssets(
        tmp_path / "c2",
        asset_specs={"raw": AssetSpec("raw", "raw.json", lambda p: p.read_text())},
    )
    _write_json(assets, "raw.json", {"k": 1})
    assert assets.load_dataclass("raw") == '{"k": 1}'


def test_load_dataclass_missing_file_raises(tmp_path):
    assets = CaseAssets(
        tmp_path / "c3",
        asset_specs={"doc": AssetSpec("doc", "doc.json", _Doc)},
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
        asset_specs={"scans": AssetSpec("scans", "scans/*.json", _Doc)},
    )
    _write_json(assets, "scans/a.json", {"name": "a"})
    _write_json(assets, "scans/b.json", {"name": "b"})
    paths = assets.dataclass_paths("scans")
    assert [p.name for p in paths] == ["a.json", "b.json"]
    with pytest.raises(ValueError):
        assets.dataclass_path("scans")
    loaded = [assets.load_dataclass_file(p.relative_to(assets.folder).as_posix()) for p in paths]
    assert sorted(d.name for d in loaded) == ["a", "b"]


def test_flexible_loading_returns_lazy(tmp_path):
    assets = CaseAssets(
        tmp_path / "c6",
        asset_specs={"cfg": AssetSpec("cfg", "cfg.json", None)},
        flexible_dataclass_loading=True,
    )
    _write_json(assets, "cfg.json", {"feature": True})
    lazy = assets.load_dataclass("cfg")
    assert isinstance(lazy, LazyLoadedFileData)
    assert lazy.as_dict()["feature"] is True