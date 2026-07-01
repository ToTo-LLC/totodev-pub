# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseRecord: the skinny, near-immutable identity card persisted as case_record.yaml."""

from __future__ import annotations

import datetime
from typing import Any, Optional

from pydantic import BaseModel, field_validator

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.helpers import _to_utc

_RECORD_ALIAS_KEYS = frozenset({"path", "loader", "states"})


class CaseRecord(BaseModel, FileMappedPydanticMixin):
    """Deliberately SKINNY. An identity card, not a state store. No volatile data:
    status, last-activity time, and the retained-file set are all DERIVED from the
    event log (and cached in memory while the case is live), never persisted here.

    Serialized as YAML (FileMappedPydanticMixin). Fields emit in definition order
    (sort_keys=False) for clean, churn-free diffs.

    Subclasses may add typed fields (see FolderBackedCase._record_cls). Any added
    field MUST have a default / be Optional so that older on-disk records (which lack
    the field) can still be loaded after a class upgrade.

    DELIBERATE EXCEPTIONS to the "added fields must be defaulted" rule above:
    `asset_aliases` and `fsm_state_chains` are both REQUIRED (no default). A case must
    declare the data objects it serializes AND its state machine (FolderBackedCase
    enforces the class-level declarations); records predating these fields will not load,
    by design.

    `asset_aliases` mirrors the in-code AssetSpec on disk: each alias maps to a dict of
    {"path": <relative path under assets/, may be a glob>, "loader": <bare class
    __name__ for a FileMappedPydanticMixin subclass, null when none declared, or
    "Callable" for a plain callable that a reader cannot resolve by name>,
    "states": <optional sorted list of FSM state names in which the asset is trustworthy>}.

    `fsm_state_chains` mirrors the concrete class's `fsm_state_chains` DSL declaration on
    disk (the raw chain strings, verbatim). It is stamped ONCE at create (and re-stamped
    on reclassify, alongside `asset_aliases`); no attempt is made to detect or reconcile
    later edits to the class. It lets a reader inspect a case's declared lifecycle — and
    per-alias state validity via `asset_aliases` — WITHOUT importing the concrete case
    class or compiling its FSM.
    """

    case_object_type: str              # bare class __name__; resolved via the registry at hydration
    case_id: str                       # natural internal id (default: time-based base36 slug)
    external_key: Optional[str] = None # caller-supplied id in an external system
    nickname: Optional[str] = None     # optional human-friendly label for listings
    created: datetime.datetime         # immutable
    closed: Optional[datetime.datetime] = None  # stamped once on terminal entry
    asset_aliases: dict[str, dict[str, Any]]  # alias -> {path, loader, states?}
    fsm_state_chains: list[str]        # the concrete class's raw state-chain DSL, verbatim

    @field_validator("asset_aliases")
    @classmethod
    def _validate_asset_aliases(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for alias, entry in v.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"asset_aliases[{alias!r}] must be a dict; got {type(entry).__name__}."
                )
            unknown = set(entry) - _RECORD_ALIAS_KEYS
            if unknown:
                raise ValueError(
                    f"asset_aliases[{alias!r}] has unknown key(s) {sorted(unknown)!r}; "
                    f"allowed: {sorted(_RECORD_ALIAS_KEYS)}."
                )
            if "path" not in entry:
                raise ValueError(f"asset_aliases[{alias!r}] is missing required key 'path'.")
            path = entry["path"]
            if not isinstance(path, str) or not path:
                raise ValueError(
                    f"asset_aliases[{alias!r}]['path'] must be a non-empty string."
                )
            if "loader" in entry:
                loader = entry["loader"]
                if loader is not None and not isinstance(loader, str):
                    raise ValueError(
                        f"asset_aliases[{alias!r}]['loader'] must be a string or null."
                    )
            if "states" in entry:
                states = entry["states"]
                if states is not None:
                    if not isinstance(states, list) or not all(
                        isinstance(s, str) for s in states
                    ):
                        raise ValueError(
                            f"asset_aliases[{alias!r}]['states'] must be a list of "
                            "strings or null."
                        )
        return v

    @field_validator("created", "closed")
    @classmethod
    def _normalize_to_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        """Coerce every timestamp to aware UTC on construction AND on load from disk.
        Pydantic first parses strings/datetimes to a datetime; this then tags a naive
        value as UTC and converts any aware value to UTC, so the in-memory model is
        always aware-UTC and round-trips cleanly through YAML. Subclasses that add their
        OWN datetime fields should apply the same validator (or reuse _to_utc)."""
        return _to_utc(v)
