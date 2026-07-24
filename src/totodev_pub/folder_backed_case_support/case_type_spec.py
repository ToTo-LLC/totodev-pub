# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseTypeSpec: compiled class-behavior contract for FolderBackedCase subclasses."""

from __future__ import annotations

from dataclasses import dataclass

from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.state_chain_parser import FsmChainSpec


@dataclass(frozen=True)
class CaseTypeSpec:
    """Compiled class-behavior metadata for one concrete case type.

    Bundles the per-class singletons built at class-definition time (``compile_fsm()``,
    ``asset_aliases`` normalization). External consumers — pool drivers, validators,
    tooling — use this instead of reaching into private ``_fsm`` / ``_asset_book``.

    Shallow freeze only: nested ``FsmChainSpec`` remains mutable by design for
    ``compile_fsm()`` overrides; treat it as read-only after ``case_type_spec()``
    returns.
    """

    fsm: FsmChainSpec
    assets: AliasedAssetSpecs

    def declared_choke_resources(self) -> frozenset[str]:
        """Union of every resource name referenced by any trigger's choke set."""
        needed: set[str] = set()
        for names in self.fsm.trigger_chokes.values():
            needed.update(names)
        return frozenset(needed)
