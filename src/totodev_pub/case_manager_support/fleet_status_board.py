# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""FleetStatusBoard: in-process observer of a CaseManager's live fleet status.

Attaches to pool events and ``on_tick_completed``. Maintains an in-memory
projection (including short terminal retention), optionally publishes
``fleet_status.jsonl``, and pushes ``FleetEvent`` notifications to subscribers.
"""

from __future__ import annotations

import datetime
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Hashable, Iterable

from totodev_pub.case_manager_support.constants import FLEET_STATUS_FILENAME
from totodev_pub.case_manager_support.fleet_status import (
    FleetStatusRow,
    ROW_FIELD_ORDER,
    _iso_utc,
    append_board_row,
    board_body_hash,
    build_live_row,
    collect_case_status_facts,
    parse_iso_utc,
    publish_board,
    read_board,
    render_board_body,
)
from totodev_pub.case_manager_support.fleet_status_events import (
    FleetEvent,
    diff_snapshots,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_pool_driver import (
    CasePoolEvent,
    CasePoolEventNames,
)

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)


class FleetStatusBoard:
    """Observer: in-memory fleet status projection ± optional file publish."""

    def __init__(
        self,
        manager: "CaseManager",
        *,
        publish_file: bool = True,
        full_flush_interval_secs: float = 1.0,
        terminal_retention_secs: float = 120.0,
    ) -> None:
        self._manager = manager
        self._publish_file = publish_file
        self._full_flush_interval_secs = full_flush_interval_secs
        self._terminal_retention_secs = terminal_retention_secs
        self._board_path = manager._manager_dir / FLEET_STATUS_FILENAME
        self._last_full_flush_monotonic: float | None = None
        self._last_hash: str | None = None
        self._last_rows: dict[str, dict[str, Any]] = {}
        self._retained: dict[str, tuple[dict[str, Any], datetime.datetime]] = {}
        self._ext_status_warned: set[str] = set()
        self._seeded = False
        self._attached = False
        self._pool_event_handle: Hashable | None = None
        self._change_subs: dict[Hashable, Callable[[list[FleetEvent]], None]] = {}
        self._notify_baseline: dict[str, FleetStatusRow] | None = None

    @property
    def board_path(self) -> Path:
        return self._board_path

    @property
    def publish_file(self) -> bool:
        return self._publish_file

    def rows(self) -> dict[str, FleetStatusRow]:
        """Committed projection snapshot (live ∪ retained)."""
        return {
            cid: FleetStatusRow.model_validate(row)
            for cid, row in self._last_rows.items()
        }

    def __getitem__(self, case_id: str) -> FleetStatusRow:
        return FleetStatusRow.model_validate(self._last_rows[case_id])

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._last_rows

    def subscribe(
        self, callback: Callable[[list[FleetEvent]], None]
    ) -> Hashable:
        handle = uuid.uuid4()
        self._change_subs[handle] = callback
        return handle

    def unsubscribe(self, handle: Hashable) -> None:
        self._change_subs.pop(handle, None)

    def attach(self) -> None:
        if self._attached:
            return
        self._attached = True
        if self._publish_file:
            if not self._board_path.exists():
                publish_board(self._board_path, "")
            self._seed_from_disk()
        else:
            self._seeded = True
        self._pool_event_handle = self._manager._driver.case_event_subscribe(
            {
                CasePoolEventNames.ADMITTED,
                CasePoolEventNames.ALERTED,
                CasePoolEventNames.ADVANCED,
                CasePoolEventNames.FAILED,
                CasePoolEventNames.REMOVED,
                CasePoolEventNames.EVICTED,
                CasePoolEventNames.TERMINATED,
            },
            self._on_pool_event,
        )
        self._manager.on_tick_completed(self._on_tick_completed)
        self.publish_full_if_due(
            list(self._manager._driver),
            locate=self._locate(),
            force=True,
        )

    def detach(self) -> None:
        if not self._attached:
            return
        self._attached = False
        if self._pool_event_handle is not None:
            try:
                self._manager._driver.case_event_unsubscribe(self._pool_event_handle)
            except KeyError:
                pass
            self._pool_event_handle = None
        self._change_subs.clear()
        self._notify_baseline = None

    def _locate(self) -> Callable[[str], Any]:
        return lambda cid: self._manager.locate(cid)

    async def _on_tick_completed(self) -> None:
        if not self._attached:
            return
        try:
            self.publish_full_if_due(
                list(self._manager._driver),
                locate=self._locate(),
                force=False,
            )
        except Exception:
            logger.warning("fleet status board tick flush failed", exc_info=True)

    def _on_pool_event(self, event: CasePoolEvent) -> None:
        if not self._attached:
            return
        try:
            name = event.event
            if name in (CasePoolEventNames.REMOVED, CasePoolEventNames.EVICTED,
                        CasePoolEventNames.REMOVED.value, CasePoolEventNames.EVICTED.value):
                self.publish_full_if_due(
                    list(self._manager._driver),
                    locate=self._locate(),
                    force=True,
                )
                return
            if name in (CasePoolEventNames.TERMINATED, CasePoolEventNames.TERMINATED.value):
                self.note_terminal(event.case)
                self.notify(
                    event.case,
                    force=True,
                    live_cases=list(self._manager._driver),
                    locate=self._locate(),
                )
                return
            if name in (CasePoolEventNames.ADVANCED, CasePoolEventNames.ADVANCED.value):
                ar = event.advance_result
                if ar is None or not ar.progressed:
                    return
            self.notify(
                event.case,
                live_cases=list(self._manager._driver),
                locate=self._locate(),
            )
        except Exception:
            logger.warning("fleet status board pool-event update failed", exc_info=True)

    def notify(
        self,
        case: FolderBackedCase,
        *,
        force: bool = False,
        live_cases: Iterable[FolderBackedCase] | None = None,
        locate: Callable[[str], Any] | None = None,
    ) -> bool:
        if not self._seeded:
            self._seed_from_disk()
        try:
            row = build_live_row(case, warned_case_ids=self._ext_status_warned)
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
            self._last_rows[row["case_id"]] = row
            if row.get("is_terminal"):
                self.note_terminal(case)
            return self.publish_full(live_cases, locate=locate)

        self._last_rows[row["case_id"]] = row
        if row.get("is_terminal"):
            self.note_terminal(case)
        if self._publish_file:
            append_board_row(self._board_path, row)
        self._recompute_hash_from_memory()
        self._emit_change_notifications()
        return True

    def publish_full(
        self,
        live_cases: Iterable[FolderBackedCase],
        *,
        locate: Callable[[str], Any],
    ) -> bool:
        if not self._seeded:
            self._seed_from_disk()
        rows: dict[str, dict[str, Any]] = {}
        for case in live_cases:
            try:
                row = build_live_row(case, warned_case_ids=self._ext_status_warned)
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
        wrote = False
        if digest != self._last_hash:
            if self._publish_file:
                publish_board(self._board_path, body)
                wrote = True
            self._last_hash = digest
            self._emit_change_notifications()
        return wrote

    def publish_full_if_due(
        self,
        live_cases: Iterable[FolderBackedCase],
        *,
        locate: Callable[[str], Any],
        force: bool = False,
    ) -> bool:
        now = time.monotonic()
        if (
            not force
            and self._last_full_flush_monotonic is not None
            and (now - self._last_full_flush_monotonic) < self._full_flush_interval_secs
        ):
            return False
        return self.publish_full(live_cases, locate=locate)

    def note_terminal(self, case: FolderBackedCase) -> None:
        case_id = case.case_id
        prior = self._last_rows.get(case_id)
        row: dict[str, Any] = {
            "case_id": case_id,
            "external_key": case.case_external_key,
            "case_type": type(case).__name__,
            "case_folder": str(case.case_folder),
        }
        try:
            row.update(collect_case_status_facts(case.case_folder))
        except OSError:
            if prior is None:
                return
            row = dict(prior)
        row["ext"] = (prior or {}).get("ext") or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        terminal_at = parse_iso_utc(row.get("terminal_at")) or now
        row["is_terminal"] = True
        if row.get("terminal_at") is None:
            row["terminal_at"] = _iso_utc(terminal_at)
        frozen = {key: row.get(key) for key in ROW_FIELD_ORDER}
        self._retained[case_id] = (frozen, terminal_at)
        self._last_rows[case_id] = frozen

    def _emit_change_notifications(self) -> None:
        current = self.rows()
        if self._notify_baseline is None:
            self._notify_baseline = current
            return
        events = diff_snapshots(self._notify_baseline, current)
        self._notify_baseline = current
        if not events:
            return
        for handle, callback in list(self._change_subs.items()):
            try:
                callback(events)
            except Exception:
                logger.warning(
                    "fleet status change subscriber %r failed", handle, exc_info=True
                )

    def _recompute_hash_from_memory(self) -> None:
        body = render_board_body(self._last_rows.values())
        self._last_hash = board_body_hash(body)

    def _seed_from_disk(self) -> None:
        self._seeded = True
        if not self._publish_file or not self._board_path.exists():
            return
        try:
            prior = read_board(self._board_path)
        except OSError:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        for case_id, row_model in prior.items():
            row = row_model.model_dump()
            self._last_rows[case_id] = row
            if row_model.is_terminal:
                terminal_at = parse_iso_utc(row_model.terminal_at) or now
                self._retained[case_id] = (row, terminal_at)
        self._recompute_hash_from_memory()

    def _absorb_departures(
        self, pool_ids: set[str], locate: Callable[[str], Any]
    ) -> None:
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
            row["ext"] = prior.get("ext") or {}
            terminal_at = parse_iso_utc(row.get("terminal_at")) or datetime.datetime.now(
                datetime.timezone.utc
            )
            if row.get("terminal_at") is None:
                row["terminal_at"] = _iso_utc(terminal_at)
            row["is_terminal"] = True
            self._retained[case_id] = (
                {key: row.get(key) for key in ROW_FIELD_ORDER},
                terminal_at,
            )

    def _maintain_retained(
        self, pool_ids: set[str], locate: Callable[[str], Any]
    ) -> None:
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
