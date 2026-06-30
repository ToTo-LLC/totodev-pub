# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Declarative asset schema: AssetSpec + the parser that turns a class-level
`asset_schema` declaration into an ordered dict[str, AssetSpec]."""

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

# Persisted sentinel for a deserializer that is a plain callable (not a
# FileMappedPydanticMixin subclass): it documents on disk that the in-code deserializer
# was NOT a resolvable Pydantic type, so a reader cannot reconstruct it by name.
CALLABLE_SENTINEL = "Callable"


@dataclass(frozen=True)
class AssetSpec:
    """One declared on-disk data object: a lookup `alias`, a `relative_path` under
    assets/ (an exact path OR a glob pattern), and a `deserializer` that is either a
    FileMappedPydanticMixin subclass, any Callable[[Path], Any], or None (load
    generically via LazyLoadedFileData when flexible loading is enabled)."""

    alias: str
    relative_path: str
    deserializer: type | Callable[[Path], Any] | None = None


def _is_glob(path: str) -> bool:
    return any(ch in path for ch in _GLOB_CHARS)


def deserializer_name(deserializer) -> str:
    """Project a deserializer to its persisted string: the bare class __name__ for a
    FileMappedPydanticMixin subclass (resolvable by a reader via the asset-dataclass
    registry), else CALLABLE_SENTINEL (a plain callable or None — not resolvable)."""
    if isinstance(deserializer, type) and issubclass(deserializer, FileMappedPydanticMixin):
        return deserializer.__name__
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


def _validate_alias(alias: str, *, context: str) -> None:
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


def normalize_asset_schema(
    raw,
    *,
    flexible: bool,
    delimiter: str = DEFAULT_ALIAS_DELIMITER,
) -> dict[str, AssetSpec]:
    """Turn a class `asset_schema` declaration into an ordered dict[alias, AssetSpec].

    Accepts either a simple dict {path_or_filename: deserializer} (alias inferred) or a
    list of AssetSpec (explicit; required for globs and custom aliases). Raises
    AssetSchemaError on any malformed declaration. When flexible is False, a None
    deserializer is rejected (the case developer must define one)."""
    specs: dict[str, AssetSpec] = {}

    def _add(spec: AssetSpec) -> None:
        if spec.alias in specs:
            raise AssetSchemaError(
                f"duplicate alias {spec.alias!r}: both {specs[spec.alias].relative_path!r} "
                f"and {spec.relative_path!r} resolve to it."
            )
        if spec.deserializer is None and not flexible:
            raise AssetSchemaError(
                f"alias {spec.alias!r} ({spec.relative_path!r}) has no deserializer. Give it "
                "a FileMappedPydanticMixin subclass or a Callable[[Path], Any], or enable "
                "flexible_dataclass_loading."
            )
        specs[spec.alias] = spec

    if isinstance(raw, dict):
        for key, deserializer in raw.items():
            if isinstance(deserializer, (AssetSpec, tuple, list)):
                raise AssetSchemaError(
                    f"value for {key!r} must be a deserializer (a class or callable) or None; "
                    "use a list of AssetSpec for explicit aliases or globs."
                )
            rel = _norm_rel(str(key))
            if _is_glob(rel):
                raise AssetSchemaError(
                    f"{key!r} is a glob; the simple-dict form cannot infer an alias from a "
                    "glob. Use AssetSpec(alias, pattern, deserializer) in a list instead."
                )
            alias = infer_alias(rel, delimiter=delimiter)
            _validate_alias(alias, context=repr(key))
            _add(AssetSpec(alias, rel, deserializer))
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if not isinstance(entry, AssetSpec):
                raise AssetSchemaError(
                    "asset_schema list entries must be AssetSpec instances; got "
                    f"{type(entry).__name__}."
                )
            rel = _norm_rel(entry.relative_path)
            _validate_alias(entry.alias, context=f"AssetSpec({entry.relative_path!r})")
            _add(AssetSpec(entry.alias, rel, entry.deserializer))
    else:
        raise AssetSchemaError(
            "asset_schema must be a dict {path: deserializer} or a list of AssetSpec; got "
            f"{type(raw).__name__}."
        )
    return specs
