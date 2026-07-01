# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""AliasedAssetSpecs: the seam between class declarations and persisted asset_aliases."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.asset_dataclass_registry import (
    AssetDataclassRegistry,
    asset_dataclass_registry,
)
from totodev_pub.folder_backed_case_support.asset_schema import (
    CALLABLE_SENTINEL,
    DEFAULT_ALIAS_DELIMITER,
    AssetSpec,
    _is_glob,
    infer_alias,
    loader_name,
    validate_alias,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    AssetNotTrustedInStateError,
    AssetSchemaError,
)
from totodev_pub.folder_backed_case_support.helpers import _norm_rel

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case_support.state_chain_parser import FsmChainSpec

_UNSET = object()

_DECL_KEYS = frozenset({"path", "loader", "states", "alias", "keep"})


def _normalize_states(raw, *, context: str) -> frozenset[str] | None:
    if raw is None:
        return None
    if isinstance(raw, (set, frozenset, list, tuple)):
        if len(raw) == 0:
            raise AssetSchemaError(
                f"{context}: states must be non-empty when declared; got []."
            )
        return frozenset(str(s) for s in raw)
    raise AssetSchemaError(
        f"{context}: states must be a set, list, or tuple of state names; "
        f"got {type(raw).__name__}."
    )


class AliasedAssetSpecs:
    """Owns every representation of a case's declared alias set."""

    def __init__(self, specs: dict[str, AssetSpec]) -> None:
        self._specs = dict(specs)

    @classmethod
    def from_declaration(
        cls,
        raw,
        *,
        flexible: bool,
        delimiter: str = DEFAULT_ALIAS_DELIMITER,
    ) -> AliasedAssetSpecs:
        specs: dict[str, AssetSpec] = {}

        def _add(spec: AssetSpec) -> None:
            if spec.alias in specs:
                raise AssetSchemaError(
                    f"duplicate alias {spec.alias!r}: both "
                    f"{specs[spec.alias].relative_path!r} and "
                    f"{spec.relative_path!r} resolve to it."
                )
            specs[spec.alias] = spec

        if isinstance(raw, dict):
            if not raw:
                return cls({})
            if not flexible:
                raise AssetSchemaError(
                    "asset_aliases simple-dict form is only valid under "
                    "flexible_dataclass_loading=True or when empty; use a list of dicts "
                    "with path, loader, and states."
                )
            for key, loader in raw.items():
                if isinstance(loader, (AssetSpec, tuple, list, dict)):
                    raise AssetSchemaError(
                        f"value for {key!r} must be a loader (a class or callable) or "
                        "None; use a list of dicts for explicit aliases, states, or globs."
                    )
                rel = _norm_rel(str(key))
                if _is_glob(rel):
                    raise AssetSchemaError(
                        f"{key!r} is a glob; the simple-dict form cannot infer an alias "
                        "from a glob. Use a list-of-dicts entry with an explicit alias."
                    )
                alias = infer_alias(rel, delimiter=delimiter)
                validate_alias(alias, context=repr(key))
                _add(AssetSpec(alias, rel, loader))
            return cls(specs)

        if isinstance(raw, (list, tuple)):
            if not raw:
                return cls({})
            for index, entry in enumerate(raw):
                if isinstance(entry, dict):
                    unknown = set(entry) - _DECL_KEYS
                    if unknown:
                        raise AssetSchemaError(
                            f"asset_aliases[{index}] has unknown key(s) "
                            f"{sorted(unknown)!r}; allowed: {sorted(_DECL_KEYS)}."
                        )
                    if "path" not in entry:
                        raise AssetSchemaError(
                            f"asset_aliases[{index}] is missing required key 'path'."
                        )
                    rel = _norm_rel(str(entry["path"]))
                    if "alias" in entry:
                        alias = str(entry["alias"])
                    else:
                        if _is_glob(rel):
                            raise AssetSchemaError(
                                f"asset_aliases[{index}] ({rel!r}) is a glob; "
                                "provide an explicit 'alias' key."
                            )
                        alias = infer_alias(rel, delimiter=delimiter)
                    validate_alias(alias, context=f"asset_aliases[{index}]")
                    states = _normalize_states(
                        entry.get("states"), context=f"alias {alias!r}"
                    )
                    keep = bool(entry.get("keep", False))
                    loader = entry.get("loader")
                    _add(
                        AssetSpec(
                            alias,
                            rel,
                            loader,
                            states=states,
                            keep=keep,
                        )
                    )
                elif isinstance(entry, AssetSpec):
                    if not flexible:
                        raise AssetSchemaError(
                            "asset_aliases list of AssetSpec instances is only valid "
                            "under flexible_dataclass_loading=True; use list-of-dicts "
                            "with path, loader, and states."
                        )
                    rel = _norm_rel(entry.relative_path)
                    validate_alias(
                        entry.alias, context=f"AssetSpec({entry.relative_path!r})"
                    )
                    _add(
                        AssetSpec(
                            entry.alias,
                            rel,
                            entry.loader,
                            states=entry.states,
                            keep=entry.keep,
                        )
                    )
                else:
                    raise AssetSchemaError(
                        "asset_aliases list entries must be dicts "
                        f"({{path, loader, states, ...}}) or AssetSpec instances; "
                        f"got {type(entry).__name__} at index {index}."
                    )
            return cls(specs)

        raise AssetSchemaError(
            "asset_aliases must be a dict {path: loader}, a list of dicts "
            "({path, loader, states, ...}), or a list of AssetSpec; got "
            f"{type(raw).__name__}."
        )

    @classmethod
    def from_record(
        cls,
        asset_aliases: dict[str, dict[str, Any]],
        *,
        resolve_types: bool = False,
        registry: AssetDataclassRegistry | None = None,
    ) -> AliasedAssetSpecs:
        reg = registry if registry is not None else asset_dataclass_registry
        specs: dict[str, AssetSpec] = {}
        for alias, entry in asset_aliases.items():
            path = entry["path"]
            loader: type | Callable[[Path], Any] | None = None
            if resolve_types:
                name = entry.get("loader")
                if name and name != CALLABLE_SENTINEL:
                    resolved = reg.resolve(name)
                    if resolved is not None:
                        loader = resolved
            states_raw = entry.get("states")
            states = (
                frozenset(states_raw) if states_raw is not None else None
            )
            specs[alias] = AssetSpec(alias, path, loader, states=states, keep=False)
        return cls(specs)

    def to_record(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for alias, spec in self._specs.items():
            entry: dict[str, Any] = {
                "path": spec.relative_path,
                "loader": loader_name(spec.loader),
            }
            if spec.states is not None:
                entry["states"] = sorted(spec.states)
            result[alias] = entry
        return result

    def aliases(self) -> list[str]:
        return list(self._specs)

    def spec_map(self) -> dict[str, AssetSpec]:
        return dict(self._specs)

    def spec(self, alias: str) -> AssetSpec:
        try:
            return self._specs[alias]
        except KeyError:
            known = ", ".join(self._specs) or "(none)"
            raise KeyError(
                f"No asset alias {alias!r} is registered. Known aliases: {known}."
            ) from None

    def states(self, alias: str) -> frozenset[str] | None:
        return self.spec(alias).states

    def is_trusted(self, alias: str, cur_state: str | None) -> bool:
        alias_states = self.states(alias)
        if alias_states is None:
            return True
        if cur_state is None:
            return False
        return cur_state in alias_states

    def trusted_aliases(self, cur_state: str | None) -> list[str]:
        return [a for a in self._specs if self.is_trusted(a, cur_state)]

    def assert_trusted(self, alias: str, cur_state: str | None) -> None:
        spec = self.spec(alias)
        if not self.is_trusted(alias, cur_state):
            raise AssetNotTrustedInStateError(
                alias,
                current_state=cur_state,
                valid_states=spec.states,
            )

    def get_path_and_loader(
        self,
        alias: str,
        cur_state: Any = _UNSET,
    ) -> tuple[str, type | Callable[[Path], Any] | None]:
        if cur_state is not _UNSET:
            self.assert_trusted(alias, cur_state)
        spec = self.spec(alias)
        return spec.relative_path, spec.loader

    def validate_against_fsm(self, fsm: FsmChainSpec, *, flexible: bool) -> None:
        fsm_states = set(fsm.states)
        closed = set(fsm.closed_states)
        for alias, spec in self._specs.items():
            if not flexible:
                if spec.loader is None:
                    raise AssetSchemaError(
                        f"alias {alias!r} ({spec.relative_path!r}) has no loader. "
                        "Give it a FileMappedPydanticMixin subclass or a "
                        "Callable[[Path], Any], or enable flexible_dataclass_loading."
                    )
                if spec.states is None:
                    raise AssetSchemaError(
                        f"alias {alias!r} ({spec.relative_path!r}) has no states. "
                        "Declare the FSM states in which this asset is trustworthy, "
                        "or enable flexible_dataclass_loading."
                    )
            if spec.states is not None:
                if not spec.states:
                    raise AssetSchemaError(
                        f"alias {alias!r}: states must be non-empty when declared."
                    )
                unknown = spec.states - fsm_states
                if unknown:
                    raise AssetSchemaError(
                        f"alias {alias!r}: state(s) {sorted(unknown)!r} are not in "
                        f"this class's FSM ({sorted(fsm_states)!r})."
                    )
                if spec.states & closed and not spec.keep:
                    terminal = sorted(spec.states & closed)
                    raise AssetSchemaError(
                        f"alias {alias!r} is valid in terminal state(s) "
                        f"{terminal!r} but keep is not True — it would be purged at "
                        "close, breaking the semantics-#3 promise. Set keep=True on the "
                        "declaration."
                    )
