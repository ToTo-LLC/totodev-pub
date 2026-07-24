"""Unit tests for AliasedAssetSpecs."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.asset_dataclass_registry import (
    AssetDataclassRegistry,
)
from totodev_pub.folder_backed_case_support.asset_schema import (
    AssetSpec,
    CALLABLE_SENTINEL,
    PATH_LOADER_SENTINEL,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    AssetNotTrustedInStateError,
    AssetSchemaError,
)
from totodev_pub.folder_backed_case_support.state_chain_parser import StateChainParser


class _Rec(BaseModel, FileMappedPydanticMixin):
    n: int = 0


def _fsm():
    return StateChainParser.parse(["[*] --> new == go ==> open == done ==> closed --> [*]"]).validate()


def test_from_declaration_dict_of_assetspec():
    book = AliasedAssetSpecs.from_declaration(
        {
            "ticket": AssetSpec(
                relative_path="ticket.yaml", loader=_Rec,
                trust_states={"new", "open", "closed"}, keep=True,
            ),
            "chat": AssetSpec(
                relative_path="customer--chat.md", loader=_Rec,
                trust_states={"open"},
            ),
        },
        flexible=False,
    )
    assert book.aliases() == ["ticket", "chat"]
    spec = book.spec("ticket")
    assert spec.relative_path == "ticket.yaml"
    assert spec.loader is _Rec
    assert spec.trust_states == frozenset({"new", "open", "closed"})
    assert spec.keep is True


def test_from_declaration_glob_alias():
    book = AliasedAssetSpecs.from_declaration(
        {
            "receipts": AssetSpec(
                relative_path="receipts/*.json", loader=_Rec, trust_states={"open"},
            ),
        },
        flexible=False,
    )
    assert book.spec("receipts").relative_path == "receipts/*.json"
    assert book.spec("receipts").many is False


def test_from_declaration_many_requires_glob():
    with pytest.raises(AssetSchemaError, match="many=True"):
        AliasedAssetSpecs.from_declaration(
            {
                "a": AssetSpec(
                    relative_path="a.json", loader=_Rec, trust_states={"open"}, many=True,
                ),
            },
            flexible=False,
        )


def test_from_declaration_many_with_glob():
    book = AliasedAssetSpecs.from_declaration(
        {
            "attachments": AssetSpec(
                relative_path="attachments/*", loader=Path,
                trust_states={"open"}, many=True,
            ),
        },
        flexible=False,
    )
    assert book.spec("attachments").many is True
    assert book.spec("attachments").loader is Path


def test_from_declaration_empty_dict():
    assert AliasedAssetSpecs.from_declaration({}, flexible=False).aliases() == []


def test_from_declaration_list_rejected():
    with pytest.raises(AssetSchemaError, match="dict"):
        AliasedAssetSpecs.from_declaration(
            [AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"new"})],
            flexible=False,
        )


def test_from_declaration_non_assetspec_value_rejected():
    with pytest.raises(AssetSchemaError, match="AssetSpec"):
        AliasedAssetSpecs.from_declaration(
            {"a": {"path": "a/x.json", "loader": _Rec, "trust_states": {"new"}}},
            flexible=False,
        )


def test_from_declaration_invalid_alias_key():
    with pytest.raises(AssetSchemaError, match="path separator"):
        AliasedAssetSpecs.from_declaration(
            {"a/x": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"new"})},
            flexible=False,
        )


def test_from_declaration_states_normalized_from_plain_set():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"open", "new"})},
        flexible=False,
    )
    assert book.spec("a").trust_states == frozenset({"open", "new"})


def test_from_declaration_empty_states_rejected():
    with pytest.raises(AssetSchemaError, match="non-empty"):
        AliasedAssetSpecs.from_declaration(
            {"x": AssetSpec(relative_path="a/x.json", loader=_Rec, trust_states=[])},
            flexible=False,
        )


def test_to_record_and_from_record_round_trip():
    book = AliasedAssetSpecs.from_declaration(
        {
            "typed": AssetSpec(
                relative_path="typed.json", loader=_Rec, trust_states={"open", "new"},
            ),
            "raw": AssetSpec(
                relative_path="raw.json",
                loader=(lambda p: p.read_text()), trust_states={"new"},
            ),
            "lazy": AssetSpec(relative_path="lazy.json", loader=None, trust_states={"new"}),
            "attachments": AssetSpec(
                relative_path="attachments/*", loader=Path,
                trust_states={"open"}, many=True,
            ),
        },
        flexible=True,
    )
    record = book.to_record()
    assert record["typed"]["loader"] == "_Rec"
    assert record["typed"]["trust_states"] == ["new", "open"]
    assert record["raw"]["loader"] == CALLABLE_SENTINEL
    assert record["lazy"]["loader"] is None
    assert "trust_states" not in record["lazy"] or record["lazy"].get("trust_states") == ["new"]
    assert record["attachments"]["loader"] == PATH_LOADER_SENTINEL
    assert record["attachments"]["many"] is True
    assert "many" not in record["typed"]

    rebuilt = AliasedAssetSpecs.from_record(record, resolve_types=False)
    assert rebuilt.spec("typed").trust_states == frozenset({"new", "open"})
    assert rebuilt.spec("raw").loader is None
    assert rebuilt.spec("lazy").loader is None
    assert rebuilt.spec("attachments").loader is Path
    assert rebuilt.spec("attachments").many is True


def test_from_record_absent_states_is_unconstrained():
    book = AliasedAssetSpecs.from_record(
        {"x": {"path": "x.json", "loader": None}}, resolve_types=False,
    )
    assert book.trust_states("x") is None
    assert book.is_trusted("x", None)


def test_from_record_with_type_resolution():
    reg = AssetDataclassRegistry()
    reg.register(_Rec)
    book = AliasedAssetSpecs.from_record(
        {"r": {"path": "r.json", "loader": "_Rec", "trust_states": ["new"]}},
        resolve_types=True,
        registry=reg,
    )
    assert book.spec("r").loader is _Rec


def test_is_trusted_truth_table():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"open"})},
        flexible=False,
    )
    unconstrained = AliasedAssetSpecs.from_declaration(
        {"b": AssetSpec(relative_path="b.json", loader=_Rec)}, flexible=True,
    )
    assert unconstrained.is_trusted("b", None)
    assert unconstrained.is_trusted("b", "anything")
    assert not book.is_trusted("a", None)
    assert book.is_trusted("a", "open")
    assert not book.is_trusted("a", "new")


def test_assert_trusted_raises_with_attributes():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"open"})},
        flexible=False,
    )
    with pytest.raises(AssetNotTrustedInStateError) as exc:
        book.assert_trusted("a", "new")
    err = exc.value
    assert err.alias == "a"
    assert err.current_state == "new"
    assert err.valid_states == frozenset({"open"})


def test_unknown_alias_raises_keyerror_before_trust_check():
    book = AliasedAssetSpecs.from_declaration({}, flexible=False)
    with pytest.raises(KeyError, match="nope"):
        book.assert_trusted("nope", "new")


def test_trusted_aliases():
    book = AliasedAssetSpecs.from_declaration(
        {
            "a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"new"}),
            "b": AssetSpec(relative_path="b.json", loader=_Rec, trust_states={"open"}),
        },
        flexible=False,
    )
    assert book.trusted_aliases("new") == ["a"]
    assert set(book.trusted_aliases("open")) == {"b"}


def test_get_path_and_loader_skips_guard_when_cur_state_omitted():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"open"})},
        flexible=False,
    )
    path, loader = book.get_path_and_loader("a")
    assert path == "a.json"
    assert loader is _Rec


def test_get_path_and_loader_enforces_guard_when_cur_state_given():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"open"})},
        flexible=False,
    )
    with pytest.raises(AssetNotTrustedInStateError):
        book.get_path_and_loader("a", cur_state="new")


def test_validate_against_fsm_unknown_state():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"opne"})},
        flexible=False,
    )
    with pytest.raises(AssetSchemaError, match="opne"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_strict_missing_loader():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=None, trust_states={"new"})},
        flexible=True,
    )
    with pytest.raises(AssetSchemaError, match="no loader"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_strict_missing_states():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec)},
        flexible=True,
    )
    with pytest.raises(AssetSchemaError, match="no trust_states"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_flexible_permits_omissions():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=None)},
        flexible=True,
    )
    book.validate_against_fsm(_fsm(), flexible=True)


def test_validate_against_fsm_terminal_without_keep():
    book = AliasedAssetSpecs.from_declaration(
        {"a": AssetSpec(relative_path="a.json", loader=_Rec, trust_states={"closed"})},
        flexible=False,
    )
    with pytest.raises(AssetSchemaError, match="keep"):
        book.validate_against_fsm(_fsm(), flexible=False)


def test_validate_against_fsm_terminal_with_keep():
    book = AliasedAssetSpecs.from_declaration(
        {
            "a": AssetSpec(
                relative_path="a.json", loader=_Rec, trust_states={"closed"}, keep=True,
            ),
        },
        flexible=False,
    )
    book.validate_against_fsm(_fsm(), flexible=False)
