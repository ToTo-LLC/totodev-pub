# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseRecord: the skinny, near-immutable identity card persisted as case_record.yaml."""

from __future__ import annotations

import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.helpers import _to_utc


class CaseRecord(BaseModel, FileMappedPydanticMixin):
    """Deliberately SKINNY. An identity card, not a state store. No volatile data:
    status, last-activity time, and the retained-file set are all DERIVED from the
    event log (and cached in memory while the case is live), never persisted here.

    Serialized as YAML (FileMappedPydanticMixin). Fields emit in definition order
    (sort_keys=False) for clean, churn-free diffs.

    Subclasses may add typed fields (see FolderBackedCase._record_cls). Any added
    field MUST have a default / be Optional so that older on-disk records (which lack
    the field) can still be loaded after a class upgrade.
    """

    case_object_type: str              # bare class __name__; resolved via the registry at hydration
    case_id: str                       # natural internal id (default: time-based base36 slug)
    external_key: Optional[str] = None # caller-supplied id in an external system
    nickname: Optional[str] = None     # optional human-friendly label for listings
    created: datetime.datetime         # immutable
    closed: Optional[datetime.datetime] = None  # stamped once on terminal entry

    @field_validator("created", "closed")
    @classmethod
    def _normalize_to_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        """Coerce every timestamp to aware UTC on construction AND on load from disk.
        Pydantic first parses strings/datetimes to a datetime; this then tags a naive
        value as UTC and converts any aware value to UTC, so the in-memory model is
        always aware-UTC and round-trips cleanly through YAML. Subclasses that add their
        OWN datetime fields should apply the same validator (or reuse _to_utc)."""
        return _to_utc(v)
