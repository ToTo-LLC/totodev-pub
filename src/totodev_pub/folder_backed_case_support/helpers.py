# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Small, dependency-free helpers shared across the FolderBackedCase support modules:
aware-UTC time, datetime normalization, asset-path canonicalization, and the default
case-id slug generator. Kept separate so both the support classes and the main module
can use them without circular imports."""

from __future__ import annotations

import datetime
import os
from pathlib import PurePosixPath
from typing import Optional


def _utcnow() -> datetime.datetime:
    """Timezone-AWARE current UTC time. (Not datetime.utcnow(), which is naive and
    deprecated in 3.12+.) All case timestamps are aware-UTC so comparisons and
    serialization are unambiguous."""
    return datetime.datetime.now(datetime.timezone.utc)


def _to_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Normalize a datetime to aware UTC. A naive value is ASSUMED to already be UTC
    (the convention everywhere a case mints its own timestamps); an aware value is
    converted. None passes through. Used by CaseRecord's field validator so any stored
    or caller-supplied datetime lands as aware UTC regardless of how it arrived."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _norm_rel(relative_path: str) -> str:
    """Canonicalize an asset path to a clean, forward-slash RELATIVE string (the on-disk
    manifest form). Collapses '.' segments and OS separators; REJECTS absolute paths and
    any '..' that would escape the assets root. Raises ValueError on an empty/escaping
    path. Normalizing on both write and read makes 'a/b.txt', './a/b.txt', and 'a\\b.txt'
    compare equal in the keep manifest."""
    p = PurePosixPath(str(relative_path).replace(os.sep, "/"))
    if p.is_absolute():
        raise ValueError(f"asset path must be relative, got {relative_path!r}")
    parts = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"asset path may not escape the assets root: {relative_path!r}")
        parts.append(part)
    if not parts:
        raise ValueError(f"empty asset path: {relative_path!r}")
    return "/".join(parts)


def _new_time_slug() -> str:
    """A short, sortable, base-36 time slug suitable for use as a case_id."""
    import time as _t
    n = int(_t.time() * 1000)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = []
    while n:
        result.append(digits[n % 36])
        n //= 36
    return "".join(reversed(result)) or "0"
