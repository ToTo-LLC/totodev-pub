# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""AssetDataclassRegistry: name -> FileMappedPydanticMixin subclass catalog.

Lets a reader process turn the stringified deserializer names persisted in a case
record's `asset_aliases` back into real classes WITHOUT importing the concrete case
class. Mirrors CaseTypeRegistry: keyed by bare __name__ (the exact value stamped on
disk), explicit opt-in registration, a single process-wide singleton.

Only FileMappedPydanticMixin subclasses are registrable; a plain callable deserializer
is persisted as the non-resolvable CALLABLE_SENTINEL and is never looked up here."""

from __future__ import annotations

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.asset_schema import (
    AssetSpec,
    CALLABLE_SENTINEL,
)


class AssetDataclassRegistry:
    """The asset-dataclass catalog: name -> FileMappedPydanticMixin subclass."""

    def __init__(self) -> None:
        self._registry: dict[str, type[FileMappedPydanticMixin]] = {}

    def register(self, *dataclasses: type[FileMappedPydanticMixin]) -> None:
        """Explicit, opt-in registration of one or more asset dataclasses. Each is keyed
        by its bare __name__ — the EXACT value stamped into a record's asset_aliases
        deserializer field. Single or many: register(A) or register(A, B, C). Each class
        MUST be a FileMappedPydanticMixin subclass (the only thing a reader can load by
        name); anything else raises ValueError."""
        for dc in dataclasses:
            if not (isinstance(dc, type) and issubclass(dc, FileMappedPydanticMixin)):
                raise ValueError(
                    f"{dc!r} is not a FileMappedPydanticMixin subclass; only such classes "
                    "can be registered as asset dataclasses (a plain callable deserializer "
                    "is persisted as 'Callable' and is not resolvable by name)."
                )
            self._registry[dc.__name__] = dc

    def resolve(self, name: str | None) -> type[FileMappedPydanticMixin] | None:
        """Look up a class by its stored bare name; None if not registered."""
        if name is None:
            return None
        return self._registry.get(name)


# Process-wide singleton: the canonical catalog readers use directly.
asset_dataclass_registry = AssetDataclassRegistry()


def asset_specs_from_record(
    asset_aliases: dict[str, dict[str, str]],
    *,
    resolve_types: bool,
    registry: AssetDataclassRegistry | None = None,
) -> dict[str, AssetSpec]:
    """Rebuild AssetSpecs from a record's persisted asset_aliases mapping.

    Each entry is {"path": <relative path>, "deserializer": <class name or "Callable">}.
    With resolve_types=False every spec gets deserializer=None (the caller loads it
    generically via LazyLoadedFileData). With resolve_types=True, a deserializer name
    that resolves through the registry to a FileMappedPydanticMixin subclass is attached
    for typed loading; an unknown name or the CALLABLE_SENTINEL falls back to None for
    that alias (graceful per-alias degradation)."""
    reg = registry if registry is not None else asset_dataclass_registry
    specs: dict[str, AssetSpec] = {}
    for alias, entry in asset_aliases.items():
        path = entry["path"]
        deserializer: type | None = None
        if resolve_types:
            name = entry.get("deserializer")
            if name and name != CALLABLE_SENTINEL:
                deserializer = reg.resolve(name)
        specs[alias] = AssetSpec(alias, path, deserializer)
    return specs
