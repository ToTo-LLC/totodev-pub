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
        """Normalize a class-level `asset_aliases` declaration: a list (or tuple) of
        AssetSpec instances, `[]` when the case has no protocol-elevated data objects.
        `flexible` is accepted for symmetry with `validate_against_fsm` and is not
        otherwise used here — omitted loader/states are always legal per-entry;
        strict-mode enforcement of their presence happens at FSM-binding time."""
        if not isinstance(raw, (list, tuple)):
            raise AssetSchemaError(
                "asset_aliases must be a list (or tuple) of AssetSpec instances; "
                f"got {type(raw).__name__}. Use [] when the case declares none."
            )

        specs: dict[str, AssetSpec] = {}

        def _add(spec: AssetSpec) -> None:
            if spec.alias in specs:
                raise AssetSchemaError(
                    f"duplicate alias {spec.alias!r}: both "
                    f"{specs[spec.alias].relative_path!r} and "
                    f"{spec.relative_path!r} resolve to it."
                )
            specs[spec.alias] = spec

        for index, entry in enumerate(raw):
            if not isinstance(entry, AssetSpec):
                raise AssetSchemaError(
                    "asset_aliases list entries must be AssetSpec instances; got "
                    f"{type(entry).__name__} at index {index}. Construct with "
                    "AssetSpec(relative_path=..., loader=..., states=..., ...)."
                )
            rel = _norm_rel(entry.relative_path)
            if entry.alias is not None:
                alias = entry.alias
            else:
                if _is_glob(rel):
                    raise AssetSchemaError(
                        f"asset_aliases[{index}] ({rel!r}) is a glob; "
                        "provide an explicit 'alias' on the AssetSpec."
                    )
                alias = infer_alias(rel, delimiter=delimiter)
            validate_alias(alias, context=f"AssetSpec({entry.relative_path!r})")
            states = _normalize_states(entry.states, context=f"alias {alias!r}")
            _add(
                AssetSpec(
                    alias=alias,
                    relative_path=rel,
                    loader=entry.loader,
                    states=states,
                    keep=bool(entry.keep),
                )
            )
        return cls(specs)

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
            specs[alias] = AssetSpec(
                alias=alias, relative_path=path, loader=loader, states=states,
            )
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
        terminal_states = set(fsm.terminal_states)
        for alias, spec in self._specs.items():
            if not flexible:
                if spec.loader is None:
                    raise AssetSchemaError(
                        f"alias {alias!r} ({spec.relative_path!r}) has no loader. "
                        "Give it a FileMappedPydanticMixin subclass or a "
                        "Callable[[Path], Any], or enable flexible_asset_alias_loading."
                    )
                if spec.states is None:
                    raise AssetSchemaError(
                        f"alias {alias!r} ({spec.relative_path!r}) has no states. "
                        "Declare the FSM states in which this asset is trustworthy, "
                        "or enable flexible_asset_alias_loading."
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
                if spec.states & terminal_states and not spec.keep:
                    terminal = sorted(spec.states & terminal_states)
                    raise AssetSchemaError(
                        f"alias {alias!r} is valid in terminal state(s) "
                        f"{terminal!r} but keep is not True — it would be purged at "
                        "termination, breaking the semantics-#3 promise. Set keep=True on the "
                        "declaration."
                    )
