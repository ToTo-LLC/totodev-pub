# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Single owner of the on-disk truncation format for CachedFileFolders.

A truncated entry stores only authoritative metadata (size, mtime, optional hash)
rather than the full file body. On disk it is a zero-byte file plus a YAML sidecar
in the entry's slave directory.

The authoritative "is this truncated?" test requires BOTH conditions to hold:
  1. The cached file is zero bytes.
  2. The sidecar exists and its first line matches MARKER_LINE exactly.

Any other combination is treated as a full (non-truncated) entry. This fails safe:
a full file body or a missing sidecar is always the conservative interpretation.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, ConfigDict

__all__ = ["TruncationInfo", "SIDECAR_FILENAME", "MARKER_LINE",
           "is_truncated", "read_truncation_info", "write_truncation_info",
           "delete_truncation_info"]

SIDECAR_FILENAME = "_truncation_info.yaml"

# Exact first-line marker. The /v1 suffix allows future format evolution while
# keeping the line grep-detectable without parsing YAML.
MARKER_LINE = "kind: cachedfile-truncation-info/v1"


class TruncationInfo(BaseModel):
    """Metadata stored in the truncation sidecar for a truncated cache entry.

    All payload fields are optional because different sources expose different
    cheap metadata. Only the sidecar's presence (plus a zero-byte body) is
    required to identify a truncated entry.
    """
    model_config = ConfigDict(frozen=True)

    size: Optional[int] = None
    """Pre-truncation byte count of the original file, if known."""

    mtime: Optional[float] = None
    """Source modification time as a POSIX timestamp, if known."""

    hash: Optional[str] = None
    """xxhash hex digest. Populated only under use_xxhash=True; absent otherwise."""

    retrieval_hint: Optional[Dict[str, Any]] = None
    """Informational blob recording how the original could be re-fetched."""

    manual_override: Optional[str] = None
    """If "FORCE_TRUNCATE", pins this entry truncated regardless of proxy recommendation."""


# ---------------------------------------------------------------------------
# Internal serialisation helpers
# ---------------------------------------------------------------------------

def _to_yaml_text(info: TruncationInfo) -> str:
    data: Dict[str, Any] = {}
    if info.size is not None:
        data["size"] = info.size
    if info.mtime is not None:
        data["mtime"] = info.mtime
    if info.hash is not None:
        data["hash"] = info.hash
    if info.retrieval_hint is not None:
        data["retrieval_hint"] = info.retrieval_hint
    if info.manual_override is not None:
        data["manual_override"] = info.manual_override

    lines = [MARKER_LINE]
    if data:
        lines.append(yaml.dump(data, default_flow_style=False).rstrip())
    return "\n".join(lines) + "\n"


def _from_yaml_text(text: str) -> Optional[TruncationInfo]:
    lines = text.splitlines()
    if not lines or lines[0] != MARKER_LINE:
        return None
    rest = "\n".join(lines[1:])
    data: Dict[str, Any] = {}
    if rest.strip():
        try:
            loaded = yaml.safe_load(rest)
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError:
            return None
    known = TruncationInfo.model_fields.keys()
    return TruncationInfo(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_truncated(file_path: Path, slave_dir_path: Path) -> bool:
    """Return True iff the entry is truncated (zero-byte file AND valid sidecar).

    Reads only the first line of the sidecar for efficiency — no full YAML parse.
    Any I/O error or marker mismatch returns False (fail-safe toward "full body").
    """
    try:
        if file_path.stat().st_size != 0:
            return False
    except OSError:
        return False
    sidecar_path = slave_dir_path / SIDECAR_FILENAME
    try:
        with sidecar_path.open(encoding="utf-8") as fh:
            first_line = fh.readline().rstrip("\n")
        return first_line == MARKER_LINE
    except (OSError, UnicodeDecodeError):
        return False


def read_truncation_info(slave_dir_path: Path) -> Optional[TruncationInfo]:
    """Read and parse the truncation sidecar. Returns None on any failure."""
    sidecar_path = slave_dir_path / SIDECAR_FILENAME
    try:
        text = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _from_yaml_text(text)


def write_truncation_info(slave_dir_path: Path, info: TruncationInfo) -> None:
    """Write (or overwrite) the truncation sidecar and fsync for crash safety.

    Write order contract: the caller must ensure the slave directory exists before
    calling this, and must not zero/create the body file until this returns. This
    guarantees the sidecar is durable before the zero-byte body appears.
    """
    slave_dir_path.mkdir(parents=True, exist_ok=True)
    sidecar_path = slave_dir_path / SIDECAR_FILENAME
    text = _to_yaml_text(info)
    sidecar_path.write_text(text, encoding="utf-8")
    # fsync the sidecar file itself
    with open(sidecar_path, "r", encoding="utf-8") as fh:
        os.fsync(fh.fileno())
    # fsync the directory entry so the rename/create is durable
    dir_fd = os.open(str(slave_dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def delete_truncation_info(slave_dir_path: Path) -> None:
    """Remove the truncation sidecar if present. Silently ignores a missing file."""
    sidecar_path = slave_dir_path / SIDECAR_FILENAME
    try:
        sidecar_path.unlink()
    except FileNotFoundError:
        pass
