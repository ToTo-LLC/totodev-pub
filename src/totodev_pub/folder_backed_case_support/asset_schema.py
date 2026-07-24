# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Declarative asset aliases: AssetSpec and shared parsing helpers.

A class-level `asset_aliases` declaration is a ``dict[str, AssetSpec]``
(alias name → spec), normalized by AliasedAssetSpecs
(folder_backed_case_support.aliased_asset_specs). The alias is the dict key —
it is not a field on AssetSpec."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.exceptions import AssetSchemaError

_GLOB_CHARS = "*?["

# Persisted sentinel for a loader that is a plain callable (not a
# FileMappedPydanticMixin subclass): it documents on disk that the in-code loader
# was NOT a resolvable Pydantic type, so a reader cannot reconstruct it by name.
CALLABLE_SENTINEL = "Callable"

# Persisted name for the identity Path loader (``loader=Path``).
PATH_LOADER_SENTINEL = "Path"


@dataclass(frozen=True, kw_only=True)
class AssetSpec:
    """One declared on-disk data object: a `relative_path` under assets/ (an exact
    path OR a glob pattern), and a `loader` that is either a
    FileMappedPydanticMixin subclass, ``Path`` (identity — return path objects),
    any Callable[[Path], Any], or None (load generically via LazyLoadedFileData
    when flexible loading is enabled).

    The lookup alias is **not** on this object — it is the key in the class-level
    ``asset_aliases: dict[str, AssetSpec]`` map. Construct as
    ``AssetSpec(relative_path=..., loader=..., trust_states=..., keep=..., many=...)``.

    `trust_states` names the FSM states in which this asset is trustworthy
    (semantics #3); None means unconstrained (guard is a no-op). `keep` is
    declaration-only sugar for retention seeding at create — it is not
    persisted on the case record. `many=True` means the path is a glob and
    ``case_load_assets`` returns a list (empty when nothing matches); it requires
    a glob `relative_path`."""

    relative_path: str
    loader: type | Callable[[Path], Any] | None = None
    trust_states: frozenset[str] | None = None
    keep: bool = False
    many: bool = False


def _is_glob(path: str) -> bool:
    return any(ch in path for ch in _GLOB_CHARS)


def loader_name(loader) -> str | None:
    """Project a loader to its persisted value: the bare class __name__ for a
    FileMappedPydanticMixin subclass (resolvable by a reader via the asset-dataclass
    registry), PATH_LOADER_SENTINEL for ``Path``, CALLABLE_SENTINEL for a plain
    callable, or None when no loader was declared."""
    if loader is None:
        return None
    if loader is Path:
        return PATH_LOADER_SENTINEL
    if isinstance(loader, type) and issubclass(loader, FileMappedPydanticMixin):
        return loader.__name__
    return CALLABLE_SENTINEL


def validate_alias(alias: str, *, context: str) -> None:
    if not alias:
        raise AssetSchemaError(f"empty alias for {context}.")
    if "/" in alias or "\\" in alias:
        raise AssetSchemaError(
            f"alias {alias!r} for {context} must not contain a path separator."
        )
    if _is_glob(alias):
        raise AssetSchemaError(
            f"alias {alias!r} for {context} must not contain glob characters."
        )
