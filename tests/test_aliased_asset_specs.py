"""Unit tests for AliasedAssetSpecs."""

import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.asset_dataclass_registry import (
    AssetDataclassRegistry,
)
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec, CALLABLE_SENTINEL
from totodev_pub.folder_backed_case_support.exceptions import (
    AssetNotTrustedInStateError,
    AssetSchemaError,
)
from totodev_pub.folder_backed_case_support.state_chain_parser import StateChainParser


class _Rec(BaseModel, FileMappedPydanticMixin):
    n: int = 0


def _fsm():
    return StateChainParser.parse(["^new==go-->open==done-->closed^"]).validate()


def test_from_declaration_strict_list_of_dicts():
    book = AliasedAssetSpecs.from_declaration(
        [
            {
                "path": "ticket.yaml",
                "loader": _Rec,
                "states": {"new", "open", "closed"},
                "keep": True,
            },
            {"path": "customer--chat.md", "loader": _Rec, "states": {"open"}},
        ],
        flexible=False,
    )
    assert book.aliases() == ["ticket", "chat"]
    spec = book.spec("ticket")
    assert spec.relative_path == "ticket.yaml"
    assert spec.loader is _Rec
    assert spec.states == frozenset({"new", "open", "closed"})
    assert spec.keep is True


def test_from_declaration_glob_requires_explicit_alias():
    with pytest.raises(AssetSchemaError, match="glob"):
        AliasedAssetSpecs.from_declaration(
            [{"path": "receipts/*.json", "loader": _Rec, "states": {"open"}}],
            flexible=False,
        )


def test_from_declaration_glob_with_alias():
    book = AliasedAssetSpecs.from_declaration(
        [
            {
                "path": "receipts/*.json",
                "alias": "receipts",
                "loader": _Rec,
                "states": {"open"},
            },
        ],
        flexible=False,
    )
    assert book.spec("receipts").relative_path == "receipts/*.json"


def test_from_declaration_empty_dict_and_list():
    assert AliasedAssetSpecs.from_declaration({}, flexible=False).aliases() == []
    assert AliasedAssetSpecs.from_declaration([], flexible=False).aliases() == []


def test_from_declaration_legacy_dict_rejected_in_strict_mode():
    with pytest.raises(AssetSchemaError, match="simple-dict"):
        AliasedAssetSpecs.from_declaration({"a/x.json": _Rec}, flexible=False)


def test_from_declaration_legacy_dict_accepted_in_flexible_mode():
    book = AliasedAssetSpecs.from_declaration({"a/x.json": _Rec}, flexible=True)
    assert book.spec("x").loader is _Rec


def test_from_declaration_assetspec_list_rejected_in_strict_mode():
    with pytest.raises(AssetSchemaError, match="AssetSpec"):
        AliasedAssetSpecs.from_declaration(
            [AssetSpec("x", "a/x.json", _Rec)], flexible=False,
        )


def test_from_declaration_duplicate_alias():
    with pytest.raises(AssetSchemaError, match="duplicate"):
        AliasedAssetSpecs.from_declaration(
            [
                {"path": "a/x.json", "loader": _Rec, "states": {"new"}},
                {"path": "b/x.json", "loader": _Rec, "states": {"new"}},
            ],
            flexible=False,
        )


def test_from_declaration_empty_states_rejected():
    with pytest.raises(AssetSchemaError, match="non-empty"):
        AliasedAssetSpecs.from_declaration(
            [{"path": "a/x.json", "loader": _Rec, "states": []}],
            flexible=False,
        )


def test_to_record_and_from_record_round_trip():
    book = AliasedAssetSpecs.from_declaration(
        [
            {"path": "typed.json", "loader": _Rec, "states": {"open", "new"}},
            {"path": "raw.json", "loader": (lambda p: p.read_text()), "states": {"new"}},
            {"path": "lazy.json", "loader": None, "states": {"new"}},
        ],
        flexible=True,
    )
    record = book.to_record()
    assert record["typed"]["loader"] == "_Rec"
    assert record["typed"]["states"] == ["new", "open"]
    assert record["raw"]["loader"] == CALLABLE_SENTINEL
    assert record["lazy"]["loader"] is None
    assert "states" not in record["lazy"] or record["lazy"].get("states") == ["new"]

    rebuilt = AliasedAssetSpecs.from_record(record, resolve_types=False)
    assert rebuilt.spec("typed").states == frozenset({"new", "open"})
    assert rebuilt.spec("raw").loader is None
    assert rebuilt.spec("lazy").loader is None


def test_from_record_absent_states_is_unconstrained():
    book = AliasedAssetSpecs.from_record(
        {"x": {"path": "x.json", "loader": None}}, resolve_types=False,
    )
    assert book.states("x") is None
    assert book.is_trusted("x", None)


def test_from_record_with_type_resolution():
    reg = AssetDataclassRegistry()
    reg.register(_Rec)
    book = AliasedAssetSpecs.from_record(
        {"r": {"path": "r.json", "loader": "_Rec", "states": ["new"]}},
        resolve_types=True,
        registry=reg,
    )
    assert book.spec("r").loader is _Rec


def test_is_trusted_truth_table():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec, "states": {"open"}}],
        flexible=False,
    )
    unconstrained = AliasedAssetSpecs.from_declaration(
        [{"path": "b.json", "loader": _Rec}], flexible=True,
    )
    assert unconstrained.is_trusted("b", None)
    assert unconstrained.is_trusted("b", "anything")
    assert not book.is_trusted("a", None)
    assert book.is_trusted("a", "open")
    assert not book.is_trusted("a", "new")


def test_assert_trusted_raises_with_attributes():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec, "states": {"open"}}],
        flexible=False,
    )
    with pytest.raises(AssetNotTrustedInStateError) as exc:
        book.assert_trusted("a", "new")
    err = exc.value
    assert err.alias == "a"
    assert err.current_state == "new"
    assert err.valid_states == frozenset({"open"})


def test_unknown_alias_raises_keyerror_before_trust_check():
    book = AliasedAssetSpecs.from_declaration([], flexible=False)
    with pytest.raises(KeyError, match="nope"):
        book.assert_trusted("nope", "new")


def test_trusted_aliases():
    book = AliasedAssetSpecs.from_declaration(
        [
            {"path": "a.json", "loader": _Rec, "states": {"new"}},
            {"path": "b.json", "loader": _Rec, "states": {"open"}},
        ],
        flexible=False,
    )
    assert book.trusted_aliases("new") == ["a"]
    assert set(book.trusted_aliases("open")) == {"b"}


def test_get_path_and_loader_skips_guard_when_cur_state_omitted():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec, "states": {"open"}}],
        flexible=False,
    )
    path, loader = book.get_path_and_loader("a")
    assert path == "a.json"
    assert loader is _Rec


def test_get_path_and_loader_enforces_guard_when_cur_state_given():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec, "states": {"open"}}],
        flexible=False,
    )
    with pytest.raises(AssetNotTrustedInStateError):
        book.get_path_and_loader("a", cur_state="new")


def test_validate_against_fsm_unknown_state():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec, "states": {"opne"}}],
        flexible=False,
    )
    with pytest.raises(AssetSchemaError, match="opne"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_strict_missing_loader():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": None, "states": {"new"}}],
        flexible=True,
    )
    with pytest.raises(AssetSchemaError, match="no loader"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_strict_missing_states():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec}],
        flexible=True,
    )
    with pytest.raises(AssetSchemaError, match="no states"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_flexible_permits_omissions():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": None}],
        flexible=True,
    )
    book.validate_against_fsm(_fsm(), flexible=True)


def test_validate_against_fsm_terminal_without_keep():
    book = AliasedAssetSpecs.from_declaration(
        [{"path": "a.json", "loader": _Rec, "states": {"closed"}}],
        flexible=False,
    )
    with pytest.raises(AssetSchemaError, match="keep"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_terminal_with_keep():
    book = AliasedAssetSpecs.from_declaration(
        [
            {
                "path": "a.json",
                "loader": _Rec,
                "states": {"closed"},
                "keep": True,
            },
        ],
        flexible=False,
    )
    book.validate_against_fsm(_fsm(), flexible=False)
