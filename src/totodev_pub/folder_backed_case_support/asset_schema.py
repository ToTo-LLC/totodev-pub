# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Declarative asset aliases: AssetSpec and shared parsing helpers.

Class-level `asset_aliases` declarations are normalized by AliasedAssetSpecs
(folder_backed_case_support.aliased_asset_specs)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.exceptions import AssetSchemaError
from totodev_pub.folder_backed_case_support.helpers import _norm_rel

DEFAULT_ALIAS_DELIMITER = "--"
_GLOB_CHARS = "*?["

# Persisted sentinel for a loader that is a plain callable (not a
# FileMappedPydanticMixin subclass): it documents on disk that the in-code loader
# was NOT a resolvable Pydantic type, so a reader cannot reconstruct it by name.
CALLABLE_SENTINEL = "Callable"


@dataclass(frozen=True)
class AssetSpec:
    """One declared on-disk data object: a lookup `alias`, a `relative_path` under
    assets/ (an exact path OR a glob pattern), and a `loader` that is either a
    FileMappedPydanticMixin subclass, any Callable[[Path], Any], or None (load
    generically via LazyLoadedFileData when flexible loading is enabled).

    `states` names the FSM states in which this asset is trustworthy (semantics #3);
    None means unconstrained (guard is a no-op). `keep` is declaration-only sugar
    for retention seeding at create — it is not persisted on the case record."""

    alias: str
    relative_path: str
    loader: type | Callable[[Path], Any] | None = None
    states: frozenset[str] | None = None
    keep: bool = False


def _is_glob(path: str) -> bool:
    return any(ch in path for ch in _GLOB_CHARS)


def loader_name(loader) -> str | None:
    """Project a loader to its persisted value: the bare class __name__ for a
    FileMappedPydanticMixin subclass (resolvable by a reader via the asset-dataclass
    registry), CALLABLE_SENTINEL for a plain callable, or None when no loader was
    declared."""
    if loader is None:
        return None
    if isinstance(loader, type) and issubclass(loader, FileMappedPydanticMixin):
        return loader.__name__
    return CALLABLE_SENTINEL


def infer_alias(key: str, *, delimiter: str = DEFAULT_ALIAS_DELIMITER) -> str:
    """Derive an alias from a path's basename: the stem, or (if the delimiter appears)
    the token after the LAST delimiter. The result is always a literal substring of the
    filename."""
    base = PurePosixPath(key).name
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if delimiter and delimiter in stem:
        return stem.rsplit(delimiter, 1)[1]
    return stem


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
