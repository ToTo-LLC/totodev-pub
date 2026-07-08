# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""FleetStatusBoardWatcher: client-side snapshot-diff change events over the fleet
status board (Fleet Status Board Spec §9.3).

Belongs in a LONG-LIVED observer process (the diff baseline lives in memory) —
e.g. the process that pushes SSE/websocket updates to browsers. Stateless
request handlers should call ``CaseManagerClient.read_fleet_status()`` instead.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from totodev_pub.case_manager_support.fleet_status import FleetStatusRow, read_board
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader


class FleetEventKind(str, enum.Enum):
    CASE_APPEARED = "CASE_APPEARED"
    CASE_DISAPPEARED = "CASE_DISAPPEARED"
    STATE_CHANGED = "STATE_CHANGED"
    WENT_TERMINAL = "WENT_TERMINAL"
    TRIGGER_STARTED = "TRIGGER_STARTED"
    TRIGGER_ENDED = "TRIGGER_ENDED"
    ALERTED = "ALERTED"
    WENT_SLOW = "WENT_SLOW"
    FAILED = "FAILED"
    EXT_CHANGED = "EXT_CHANGED"


@dataclass(frozen=True)
class FleetEvent:
    kind: FleetEventKind
    case_id: str
    row: FleetStatusRow | None            # current row (None for CASE_DISAPPEARED)
    prior: FleetStatusRow | None          # previous row (None for CASE_APPEARED)
    detail: dict[str, Any] = field(default_factory=dict)


def diff_rows(prior: FleetStatusRow, row: FleetStatusRow) -> list[FleetEvent]:
    """Typed events for one case between two snapshots. Order: state facts first,
    then trigger activity, then counter edges, then ext."""
    events: list[FleetEvent] = []
    cid = row.case_id

    new_dwell = (
        row.case_state != prior.case_state
        or row.state_entered_at != prior.state_entered_at
    )
    if new_dwell:
        events.append(FleetEvent(
            FleetEventKind.STATE_CHANGED, cid, row, prior,
            {"from_state": prior.case_state, "to_state": row.case_state},
        ))
    if row.is_terminal and not prior.is_terminal:
        events.append(FleetEvent(FleetEventKind.WENT_TERMINAL, cid, row, prior))

    if row.active_transition != prior.active_transition:
        if prior.active_transition is not None:
            events.append(FleetEvent(
                FleetEventKind.TRIGGER_ENDED, cid, row, prior,
                {"trigger": prior.active_transition},
            ))
        if row.active_transition is not None:
            events.append(FleetEvent(
                FleetEventKind.TRIGGER_STARTED, cid, row, prior,
                {"trigger": row.active_transition},
            ))

    # Counters are dwell-scoped: within the same dwell the edge is new-old; on a
    # new dwell the counters restarted at 0, so any nonzero value is fresh news.
    for kind, attr in (
        (FleetEventKind.ALERTED, "alert_count"),
        (FleetEventKind.WENT_SLOW, "slow_count"),
        (FleetEventKind.FAILED, "fail_count"),
    ):
        new_val = getattr(row, attr)
        delta = new_val if new_dwell else new_val - getattr(prior, attr)
        if delta > 0:
            events.append(FleetEvent(kind, cid, row, prior, {"count": delta}))

    if row.ext != prior.ext:
        events.append(FleetEvent(FleetEventKind.EXT_CHANGED, cid, row, prior))
    return events


def diff_snapshots(
    prior: dict[str, FleetStatusRow],
    current: dict[str, FleetStatusRow],
) -> list[FleetEvent]:
    events: list[FleetEvent] = []
    for case_id in sorted(current):
        row = current[case_id]
        before = prior.get(case_id)
        if before is None:
            events.append(FleetEvent(FleetEventKind.CASE_APPEARED, case_id, row, None))
        else:
            events.extend(diff_rows(before, row))
    for case_id in sorted(set(prior) - set(current)):
        events.append(
            FleetEvent(FleetEventKind.CASE_DISAPPEARED, case_id, None, prior[case_id])
        )
    return events


class FleetStatusBoardWatcher:
    """Poll-driven change detector plus a lightweight fleet collection.

    ``poll()`` stats the board file and returns ``[]`` untouched-cheap when
    ``st_mtime_ns`` is unchanged (the writer only republishes on real content
    change, so mtime is an honest signal). Otherwise it parses, diffs against
    the previous snapshot, and returns typed FleetEvents.

    Collection surface (over the latest snapshot):
        len(watcher), case_id in watcher, watcher[case_id],
        iter(watcher) → FleetStatusRow, watcher.iter_readers(),
        watcher.reader(case_id).

    Readers are constructed from the row's case_folder; construction is free
    (path capture only — every property peeks lazily on access).
    """

    def __init__(self, board_path: Path, *, emit_initial: bool = False) -> None:
        self._board_path = Path(board_path)
        self._emit_initial = emit_initial
        self._snapshot: dict[str, FleetStatusRow] = {}
        self._last_mtime_ns: int | None = None
        self._primed = False

    @property
    def board_path(self) -> Path:
        return self._board_path

    @property
    def snapshot(self) -> dict[str, FleetStatusRow]:
        """The latest merged snapshot (shallow copy)."""
        return dict(self._snapshot)

    def poll(self) -> list[FleetEvent]:
        try:
            mtime_ns = self._board_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None
        if self._primed and mtime_ns == self._last_mtime_ns:
            return []
        current = read_board(self._board_path) if mtime_ns is not None else {}
        self._last_mtime_ns = mtime_ns
        if not self._primed:
            self._primed = True
            self._snapshot = current
            if self._emit_initial:
                return [
                    FleetEvent(FleetEventKind.CASE_APPEARED, cid, current[cid], None)
                    for cid in sorted(current)
                ]
            return []
        events = diff_snapshots(self._snapshot, current)
        self._snapshot = current
        return events

    # ---- collection surface ----

    def __len__(self) -> int:
        return len(self._snapshot)

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._snapshot

    def __getitem__(self, case_id: str) -> FleetStatusRow:
        return self._snapshot[case_id]

    def __iter__(self) -> Iterator[FleetStatusRow]:
        for case_id in sorted(self._snapshot):
            yield self._snapshot[case_id]

    def rows(self) -> list[FleetStatusRow]:
        return list(self)

    def reader(self, case_id: str) -> FolderBackedCaseReader:
        return FolderBackedCaseReader(Path(self._snapshot[case_id].case_folder))

    def iter_readers(self) -> Iterator[tuple[FleetStatusRow, FolderBackedCaseReader]]:
        for row in self:
            yield row, FolderBackedCaseReader(Path(row.case_folder))
