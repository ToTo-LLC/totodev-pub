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
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from totodev_pub.case_manager_support.constants import (
    FLEET_BOARD_DISABLED_PREFIX,
    FLEET_BOARD_DISABLED_SENTINEL,
    FLEET_BOARD_PROTOCOL_VERSION,
    FLEET_STATUS_FILENAME,
)
from totodev_pub.case_manager_support.exceptions import FleetStatusBoardDisabledError
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.constants import (
    EV_ALERTED,
    EV_ENTRY_EXCEPTION,
    EV_STATE_ENTERED,
    EV_TERMINATED,
    EV_TRANSITION_FAILED,
    EV_TRIGGER_SLOW,
    EV_TRIGGER_STARTED,
    EV_TRIGGER_TIMED_OUT,
)

logger = logging.getLogger(__name__)

# Events that resolve a CASE_TRIGGER_STARTED (mirrors CaseEventLogReader).
_RESOLUTION_LABELS = frozenset({
    EV_STATE_ENTERED, EV_TRANSITION_FAILED, EV_TRIGGER_TIMED_OUT, EV_ENTRY_EXCEPTION,
})

# Serialization order of the standard fields — deterministic bytes are load-bearing:
# the writer's skip-publish hash and the watcher's mtime short-circuit rely on
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

FleetStatusDecorator = Callable[
    [FolderBackedCase, Mapping[str, Any]], Optional[dict[str, Any]]
]


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
    """Naive event-log mtime (read as local, like FolderBackedCaseReader._as_utc)
    or aware datetime → ISO-8601 UTC 'Z' string."""
    if dt is None:
        return None
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    events = CaseEventLogReader.for_folder(case_folder)
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
            elif label in _RESOLUTION_LABELS:
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
    decorator: FleetStatusDecorator | None = None,
    warned_case_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Standard row for a live in-pool case, plus decorator-provided ``ext``."""
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "external_key": case.case_external_key,
        "case_type": case.case_object_type,
        "case_folder": str(case.case_folder),
    }
    row.update(collect_case_status_facts(case.case_folder))
    row["ext"] = _run_decorator(case, row, decorator, warned_case_ids)
    return {key: row[key] for key in ROW_FIELD_ORDER}


def _run_decorator(
    case: FolderBackedCase,
    standard: dict[str, Any],
    decorator: FleetStatusDecorator | None,
    warned_case_ids: set[str] | None,
) -> dict[str, Any]:
    """Contained decorator invocation (spec §10.1). The decorator RETURNS the ext
    dict (None/{} means "nothing to add"). An exception, a non-dict return, or an
    unserializable ext logs a throttled warning and yields a vanilla ``{}`` — the
    publish never fails and the beat never stalls because of a decorator."""
    if decorator is None:
        return {}
    warned = warned_case_ids if warned_case_ids is not None else set()
    try:
        returned = decorator(case, MappingProxyType(standard))
        if returned is None:
            returned = {}
        if not isinstance(returned, dict):
            raise TypeError(
                f"fleet_status_decorator must return dict | None, got {type(returned).__name__}"
            )
        ext = dict(sorted(returned.items()))
        json.dumps(ext)  # serializability gate, before the board render
    except Exception:
        if case.case_id not in warned:
            warned.add(case.case_id)
            logger.warning(
                "fleet_status_decorator failed for case %s; publishing vanilla row",
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


def write_disabled_sentinel(path: Path) -> None:
    _atomic_write(path, FLEET_BOARD_DISABLED_SENTINEL + "\n")


def ensure_board_file(manager_dir: Path, *, enabled: bool) -> None:
    """Keep the board at its known location in the state the policy calls for
    (spec §3). Disabled → sentinel comment; enabled → an empty board appears if
    nothing is there yet (the writer's notify / publish_full path takes over)."""
    path = manager_dir / FLEET_STATUS_FILENAME
    first_line = ""
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
    is_sentinel = first_line.startswith(FLEET_BOARD_DISABLED_PREFIX)
    if enabled and (not path.exists() or is_sentinel):
        publish_board(path, "")
    elif not enabled and (not path.exists() or not is_sentinel):
        write_disabled_sentinel(path)


def append_board_row(path: Path, row: dict[str, Any]) -> None:
    """Append one deterministic JSONL row (last-wins with prior lines)."""
    ordered = {key: row.get(key) for key in ROW_FIELD_ORDER}
    ordered["ext"] = dict(sorted((ordered.get("ext") or {}).items()))
    line = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def parse_board_text(text: str) -> dict[str, FleetStatusRow]:
    """Last-wins merge by case_id (spec §6). Blank lines and comments are skipped;
    malformed JSON lines are skipped (never fail the whole read); the disabled
    sentinel raises FleetStatusBoardDisabledError."""
    merged: dict[str, FleetStatusRow] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith(FLEET_BOARD_DISABLED_PREFIX):
                raise FleetStatusBoardDisabledError()
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


class FleetStatusBoardWriter:
    """Sole editor of one fleet status board file.

    Callers (typically CaseManager) only *notify*; this writer decides whether to
    skip, append one JSONL row, or atomically rewrite the whole board. It assumes
    exclusive ownership of the board path — concurrent writers are unsupported.

    - ``notify(case)`` builds a row and, when the logical row changed (or
      ``force=True``), either appends or full-flushes based on
      ``full_flush_interval_secs`` since the last full publish.
    - ``publish_full(...)`` rebuilds from the live pool + retained terminals and
      publishes atomically, skipping the write when the body hash is unchanged.
    """

    def __init__(
        self,
        manager_dir: Path,
        *,
        full_flush_interval_secs: float,
        terminal_retention_secs: float,
        decorator: FleetStatusDecorator | None = None,
    ) -> None:
        self._board_path = manager_dir / FLEET_STATUS_FILENAME
        self._full_flush_interval_secs = full_flush_interval_secs
        self._terminal_retention_secs = terminal_retention_secs
        self._decorator = decorator
        self._last_full_flush_monotonic: float | None = None
        self._last_hash: str | None = None
        self._last_rows: dict[str, dict[str, Any]] = {}
        # case_id → (frozen row, terminal_at) for the retention window
        self._retained: dict[str, tuple[dict[str, Any], datetime.datetime]] = {}
        self._decorator_warned: set[str] = set()
        self._seeded = False

    @property
    def board_path(self) -> Path:
        return self._board_path

    def notify(
        self,
        case: FolderBackedCase,
        *,
        force: bool = False,
        live_cases: Iterable[FolderBackedCase] | None = None,
        locate: Callable[[str], Any] | None = None,
    ) -> bool:
        """Notify that ``case`` may have a new status row.

        Returns True when the board file was written (append or full flush).
        Unchanged rows are skipped silently unless ``force=True``.
        When a write is due and the full-flush interval has elapsed, performs
        ``publish_full`` (requires ``live_cases`` / ``locate``); otherwise appends.
        """
        if not self._seeded:
            self._seed_from_disk()
        try:
            row = build_live_row(
                case, decorator=self._decorator, warned_case_ids=self._decorator_warned
            )
        except Exception:
            logger.warning(
                "fleet board: failed to build row for case %s; skipping notify",
                getattr(case, "case_id", "?"),
                exc_info=True,
            )
            return False

        prior = self._last_rows.get(row["case_id"])
        if not force and prior is not None and prior == row:
            return False

        now = time.monotonic()
        due_for_full = (
            self._last_full_flush_monotonic is None
            or (now - self._last_full_flush_monotonic) >= self._full_flush_interval_secs
        )
        if due_for_full and live_cases is not None and locate is not None:
            # Fold this row into memory first so publish_full sees the fresh facts
            # even if rebuilding the same case from the pool races with detach.
            self._last_rows[row["case_id"]] = row
            if row.get("is_terminal"):
                self.note_terminal(case)
            return self.publish_full(live_cases, locate=locate)

        self._last_rows[row["case_id"]] = row
        if row.get("is_terminal"):
            self.note_terminal(case)
        append_board_row(self._board_path, row)
        self._recompute_hash_from_memory()
        return True

    def publish_full(
        self,
        live_cases: Iterable[FolderBackedCase],
        *,
        locate: Callable[[str], Any],
    ) -> bool:
        """Rebuild + publish the whole board. Returns True when a new file was written."""
        if not self._seeded:
            self._seed_from_disk()
        rows: dict[str, dict[str, Any]] = {}
        for case in live_cases:
            try:
                row = build_live_row(
                    case, decorator=self._decorator, warned_case_ids=self._decorator_warned
                )
            except Exception:
                logger.warning(
                    "fleet board: failed to build row for case %s; keeping prior row",
                    getattr(case, "case_id", "?"),
                    exc_info=True,
                )
                prior = self._last_rows.get(getattr(case, "case_id", ""))
                if prior is None:
                    continue
                row = prior
            rows[row["case_id"]] = row

        self._absorb_departures(set(rows), locate)
        self._maintain_retained(set(rows), locate)
        rows.update({cid: row for cid, (row, _t) in self._retained.items()})

        self._last_rows = rows
        body = render_board_body(rows.values())
        digest = board_body_hash(body)
        self._last_full_flush_monotonic = time.monotonic()
        if digest == self._last_hash:
            return False
        publish_board(self._board_path, body)
        self._last_hash = digest
        return True

    def publish_full_if_due(
        self,
        live_cases: Iterable[FolderBackedCase],
        *,
        locate: Callable[[str], Any],
        force: bool = False,
    ) -> bool:
        """Full rebuild when ``force`` or the flush interval since the last full publish."""
        now = time.monotonic()
        if (
            not force
            and self._last_full_flush_monotonic is not None
            and (now - self._last_full_flush_monotonic) < self._full_flush_interval_secs
        ):
            return False
        return self.publish_full(live_cases, locate=locate)

    def note_terminal(self, case: FolderBackedCase) -> None:
        """Capture a frozen retained row as a case leaves via termination.

        Called before the termination pipeline moves the folder. A fast
        auto-terminating case may never have appeared on the board as live.
        """
        case_id = case.case_id
        prior = self._last_rows.get(case_id)
        row: dict[str, Any] = {
            "case_id": case_id,
            "external_key": case.case_external_key,
            "case_type": case.case_object_type,
            "case_folder": str(case.case_folder),
        }
        try:
            row.update(collect_case_status_facts(case.case_folder))
        except OSError:
            if prior is None:
                return
            row = dict(prior)
        row["ext"] = (prior or {}).get("ext") or {}   # frozen last decoration
        now = datetime.datetime.now(datetime.timezone.utc)
        terminal_at = parse_iso_utc(row.get("terminal_at")) or now
        row["is_terminal"] = True
        if row.get("terminal_at") is None:
            row["terminal_at"] = _iso_utc(terminal_at)
        frozen = {key: row.get(key) for key in ROW_FIELD_ORDER}
        self._retained[case_id] = (frozen, terminal_at)
        self._last_rows[case_id] = frozen

    # ------------------------------------------------------------------

    def _recompute_hash_from_memory(self) -> None:
        body = render_board_body(self._last_rows.values())
        self._last_hash = board_body_hash(body)

    def _seed_from_disk(self) -> None:
        """Carry 'just finished' rows across a manager restart when possible."""
        self._seeded = True
        if not self._board_path.exists():
            return
        try:
            prior = read_board(self._board_path)
        except (FleetStatusBoardDisabledError, OSError):
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        for case_id, row_model in prior.items():
            row = row_model.model_dump()
            self._last_rows[case_id] = row
            if row_model.is_terminal:
                terminal_at = parse_iso_utc(row_model.terminal_at) or now
                self._retained[case_id] = (row, terminal_at)
        self._recompute_hash_from_memory()

    def _absorb_departures(self, pool_ids: set[str], locate: Callable[[str], Any]) -> None:
        """Cases on the last board but no longer in the pool: terminal → retained
        frozen; eject/aberrant/vanished → dropped (spec §8)."""
        for case_id in set(self._last_rows) - pool_ids - set(self._retained):
            prior = self._last_rows[case_id]
            loc = locate(case_id)
            if loc is None or not getattr(loc, "terminal", False):
                continue
            row = dict(prior)
            row["case_folder"] = str(loc.case_folder)
            try:
                row.update(collect_case_status_facts(Path(loc.case_folder)))
            except OSError:
                pass
            row["ext"] = prior.get("ext") or {}   # frozen last decoration
            terminal_at = parse_iso_utc(row.get("terminal_at")) or datetime.datetime.now(
                datetime.timezone.utc
            )
            if row.get("terminal_at") is None:
                row["terminal_at"] = _iso_utc(terminal_at)
            row["is_terminal"] = True
            self._retained[case_id] = ({key: row.get(key) for key in ROW_FIELD_ORDER}, terminal_at)

    def _maintain_retained(self, pool_ids: set[str], locate: Callable[[str], Any]) -> None:
        """Expire rows past retention, drop rows for reopened cases, and re-resolve
        case_folder when the termination pipeline has moved the folder."""
        now = datetime.datetime.now(datetime.timezone.utc)
        for case_id in list(self._retained):
            row, terminal_at = self._retained[case_id]
            if case_id in pool_ids:
                del self._retained[case_id]
                continue
            if (now - terminal_at).total_seconds() > self._terminal_retention_secs:
                del self._retained[case_id]
                continue
            if not Path(row["case_folder"]).exists():
                loc = locate(case_id)
                if loc is None:
                    del self._retained[case_id]
                else:
                    row = dict(row)
                    row["case_folder"] = str(loc.case_folder)
                    self._retained[case_id] = (row, terminal_at)
