import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.asset_dataclass_registry import (
    AssetDataclassRegistry,
    asset_specs_from_record,
)


class _Rec(BaseModel, FileMappedPydanticMixin):
    n: int = 0


def test_register_and_resolve():
    reg = AssetDataclassRegistry()
    reg.register(_Rec)
    assert reg.resolve("_Rec") is _Rec
    assert reg.resolve("Unknown") is None
    assert reg.resolve(None) is None


def test_register_rejects_non_filemapped():
    reg = AssetDataclassRegistry()

    class Plain:
        pass

    with pytest.raises(ValueError):
        reg.register(Plain)


def test_specs_from_record_without_resolution():
    aliases = {
        "rlist": {"path": "receipts/rlist.json", "deserializer": "_Rec"},
        "raw": {"path": "raw.json", "deserializer": "Callable"},
    }
    specs = asset_specs_from_record(aliases, resolve_types=False)
    assert specs["rlist"].relative_path == "receipts/rlist.json"
    assert specs["rlist"].deserializer is None
    assert specs["raw"].deserializer is None


def test_specs_from_record_with_resolution():
    reg = AssetDataclassRegistry()
    reg.register(_Rec)
    aliases = {
        "rlist": {"path": "receipts/rlist.json", "deserializer": "_Rec"},
        "raw": {"path": "raw.json", "deserializer": "Callable"},
        "missing": {"path": "m.json", "deserializer": "NotRegistered"},
    }
    specs = asset_specs_from_record(aliases, resolve_types=True, registry=reg)
    assert specs["rlist"].deserializer is _Rec
    assert specs["raw"].deserializer is None          # sentinel -> lazy fallback
    assert specs["missing"].deserializer is None       # unknown -> lazy fallback
