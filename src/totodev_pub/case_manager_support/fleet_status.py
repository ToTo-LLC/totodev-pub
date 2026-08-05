# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Fleet status board: row model, facts collection, deterministic rendering,
atomic publishing, and board parsing (Fleet Status Board Spec)."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from totodev_pub.case_manager_support.constants import (
    FLEET_BOARD_PROTOCOL_VERSION,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_journal import (
    CaseEventJournalView,
    _TRIGGER_START_RESOLUTION_LABELS,
)
from totodev_pub.folder_backed_case_support.helpers import _local_mtime_as_utc
from totodev_pub.folder_backed_case_support.constants import (
    EV_ALERTED,
    EV_STATE_ENTERED,
    EV_TERMINATED,
    EV_TRANSITION_FAILED,
    EV_TRIGGER_SLOW,
    EV_TRIGGER_STARTED,
    EV_TRIGGER_TIMED_OUT,
)

logger = logging.getLogger(__name__)

# Serialization order of the standard fields — deterministic bytes are load-bearing:
# the board's skip-publish hash and the watcher's mtime short-circuit rely on
# identical fleet state producing identical file content.
ROW_FIELD_ORDER = (
    "case_id",
    "external_key",
    "case_type",
    "case_folder",
    "case_state",
    "state_entered_at",
    "is_terminal",
    "terminal_at",
    "active_transition",
    "last_transition_time",
    "alert_count",
    "slow_count",
    "fail_count",
    "ext",
)


class FleetStatusRow(BaseModel):
    """Typed view over one board row (spec §5). Unknown top-level keys from newer
    protocol versions are ignored; user extensions live in ``ext``."""

    model_config = ConfigDict(extra="ignore")

    case_id: str
    external_key: Optional[str] = None
    case_type: str
    case_folder: str
    case_state: Optional[str] = None
    state_entered_at: Optional[str] = None
    is_terminal: bool = False
    terminal_at: Optional[str] = None
    active_transition: Optional[str] = None
    last_transition_time: Optional[str] = None
    alert_count: int = 0
    slow_count: int = 0
    fail_count: int = 0
    ext: dict[str, Any] = Field(default_factory=dict)


def _iso_utc(dt: datetime.datetime | None) -> str | None:
    """Naive (local) event-log mtime, converted via `_local_mtime_as_utc`, → ISO-8601
    UTC 'Z' string. None passes through."""
    utc_dt = _local_mtime_as_utc(dt)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if utc_dt is not None else None


def parse_iso_utc(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_case_status_facts(case_folder: Path) -> dict[str, Any]:
    """One recent-first event-log walk + one lease peek → all status fields (spec §10).

    The walk touches filenames only (PrimitiveEventProxy metadata is parsed from
    the directory listing); no event file contents are read.
    """
    events = CaseEventJournalView.for_folder(case_folder)
    case_state: str | None = None
    state_entered_at: datetime.datetime | None = None
    terminal_at: datetime.datetime | None = None
    alert_count = slow_count = fail_count = 0
    unresolved_start = None      # first CASE_TRIGGER_STARTED seen before any resolution
    first_resolution_mtime: datetime.datetime | None = None
    dwell_boundary_seen = False
    resolution_seen = False

    for ev in events.primitive.events(recent_first=True):
        label = ev.label
        if label == EV_TERMINATED and terminal_at is None:
            terminal_at = ev.mtime
        if not resolution_seen:
            if label == EV_TRIGGER_STARTED and unresolved_start is None:
                unresolved_start = ev
            elif label in _TRIGGER_START_RESOLUTION_LABELS:
                resolution_seen = True
                first_resolution_mtime = ev.mtime
        if not dwell_boundary_seen:
            if label == EV_STATE_ENTERED:
                case_state = ev.value
                state_entered_at = ev.mtime
                dwell_boundary_seen = True
            elif label == EV_ALERTED:
                alert_count += 1
            elif label == EV_TRIGGER_SLOW:
                slow_count += 1
            elif label in (EV_TRANSITION_FAILED, EV_TRIGGER_TIMED_OUT):
                fail_count += 1

    active_transition: str | None = None
    last_transition_time: datetime.datetime | None = first_resolution_mtime
    if unresolved_start is not None:
        lease_left = FolderBackedCase.peek_lease_secs_left(case_folder)
        if lease_left is not None and lease_left > 0:
            active_transition = unresolved_start.value
            last_transition_time = unresolved_start.mtime

    return {
        "case_state": case_state,
        "state_entered_at": _iso_utc(state_entered_at),
        "is_terminal": terminal_at is not None,
        "terminal_at": _iso_utc(terminal_at),
        "active_transition": active_transition,
        "last_transition_time": _iso_utc(last_transition_time),
        "alert_count": alert_count,
        "slow_count": slow_count,
        "fail_count": fail_count,
    }


def build_live_row(
    case: FolderBackedCase,
    *,
    warned_case_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Standard row for a live in-pool case, plus case-provided ``ext``."""
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "external_key": case.case_external_key,
        "case_type": type(case).__name__,
        "case_folder": str(case.case_folder),
    }
    row.update(collect_case_status_facts(case.case_folder))
    row["ext"] = _collect_case_ext_status_info(case, warned_case_ids)
    return {key: row[key] for key in ROW_FIELD_ORDER}


def _collect_case_ext_status_info(
    case: FolderBackedCase,
    warned_case_ids: set[str] | None,
) -> dict[str, Any]:
    """Contained case_ext_status_info invocation (spec §10.1). The hook RETURNS the ext
    dict (None/{} means "nothing to add"). An exception, a non-dict return, or an
    unserializable ext logs a throttled warning and yields a vanilla ``{}`` — the
    publish never fails and the beat never stalls because of a subclass hook."""
    warned = warned_case_ids if warned_case_ids is not None else set()
    try:
        returned = case.case_ext_status_info()
        if returned is None:
            returned = {}
        if not isinstance(returned, dict):
            raise TypeError(
                f"case_ext_status_info must return dict | None, got {type(returned).__name__}"
            )
        ext = dict(sorted(returned.items()))
        json.dumps(ext)  # serializability gate, before the board render
    except Exception:
        if case.case_id not in warned:
            warned.add(case.case_id)
            logger.warning(
                "case_ext_status_info failed for case %s; publishing vanilla row",
                case.case_id,
                exc_info=True,
            )
        return {}
    warned.discard(case.case_id)
    return ext


def render_board_body(rows: Iterable[dict[str, Any]]) -> str:
    """Deterministic body: rows sorted by case_id, fields in schema order, ext keys
    pre-sorted by the builder. Excludes header comments (they carry a timestamp and
    must not defeat the change hash)."""
    lines = []
    for row in sorted(rows, key=lambda r: r["case_id"]):
        ordered = {key: row.get(key) for key in ROW_FIELD_ORDER}
        ordered["ext"] = dict(sorted((ordered.get("ext") or {}).items()))
        lines.append(json.dumps(ordered, separators=(",", ":"), ensure_ascii=False))
    return "".join(line + "\n" for line in lines)


def board_body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def publish_board(path: Path, body: str) -> None:
    header = (
        f"# Fleet status board — protocol_version {FLEET_BOARD_PROTOCOL_VERSION}\n"
        f"# Generated: {_iso_utc(datetime.datetime.now(datetime.timezone.utc))}\n"
    )
    _atomic_write(path, header + body)


def append_board_row(path: Path, row: dict[str, Any]) -> None:
    """Append one deterministic JSONL row (last-wins with prior lines)."""
    ordered = {key: row.get(key) for key in ROW_FIELD_ORDER}
    ordered["ext"] = dict(sorted((ordered.get("ext") or {}).items()))
    line = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def parse_board_text(text: str) -> dict[str, FleetStatusRow]:
    """Last-wins merge by case_id. Blank lines and comments are skipped;
    malformed JSON lines are skipped (never fail the whole read)."""
    merged: dict[str, FleetStatusRow] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
            row = FleetStatusRow.model_validate(obj)
        except Exception:
            continue
        merged[row.case_id] = row
    return merged


def read_board(path: Path) -> dict[str, FleetStatusRow]:
    return parse_board_text(path.read_text(encoding="utf-8"))
