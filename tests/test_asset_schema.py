import pytest

from totodev_pub.folder_backed_case_support.asset_schema import (
    AssetSpec,
    infer_alias,
    normalize_asset_schema,
)
from totodev_pub.folder_backed_case_support.exceptions import AssetSchemaError


class _Des:
    def __call__(self, path):  # pragma: no cover
        return path


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
    assert spec.deserializer is None


def test_normalize_simple_dict_infers_aliases():
    specs = normalize_asset_schema(
        {"receipts/Overall--rlist.json": _Des, "reconciliation.json": _Des},
        flexible=False,
    )
    assert list(specs) == ["rlist", "reconciliation"]
    assert specs["rlist"].relative_path == "receipts/Overall--rlist.json"
    assert specs["rlist"].deserializer is _Des


def test_normalize_assetspec_list_explicit_alias_and_glob():
    specs = normalize_asset_schema(
        [AssetSpec("scans", "receipts/scans/*.json", _Des)], flexible=False,
    )
    assert specs["scans"].relative_path == "receipts/scans/*.json"


def test_normalize_glob_in_dict_form_raises():
    with pytest.raises(AssetSchemaError):
        normalize_asset_schema({"receipts/*.json": _Des}, flexible=False)


def test_normalize_duplicate_alias_raises():
    with pytest.raises(AssetSchemaError):
        normalize_asset_schema(
            [AssetSpec("x", "a/x.json", _Des), AssetSpec("x", "b/x.json", _Des)],
            flexible=False,
        )


def test_normalize_missing_deserializer_requires_flexible():
    with pytest.raises(AssetSchemaError):
        normalize_asset_schema({"a/x.json": None}, flexible=False)
    specs = normalize_asset_schema({"a/x.json": None}, flexible=True)
    assert specs["x"].deserializer is None


def test_normalize_dict_value_must_be_deserializer_not_tuple():
    with pytest.raises(AssetSchemaError):
        normalize_asset_schema({"x": ("a/x.json", _Des)}, flexible=False)
