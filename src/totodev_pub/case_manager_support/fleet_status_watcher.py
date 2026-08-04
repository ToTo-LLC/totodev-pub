# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""FleetStatusBoardWatcher: client-side snapshot-diff change events over the fleet
status board file.

Belongs in a LONG-LIVED observer process (the diff baseline lives in memory) —
e.g. the process that pushes SSE/websocket updates to browsers. Stateless
request handlers should call ``CaseManagerClient.read_fleet_status()`` instead.

Typed event helpers live in ``fleet_status_events`` so the in-process board and
this file watcher share one vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from totodev_pub.case_manager_support.fleet_status import FleetStatusRow, read_board
from totodev_pub.case_manager_support.fleet_status_events import (
    FleetEvent,
    FleetEventKind,
    diff_rows,
    diff_snapshots,
)
from totodev_pub.folder_backed_case_support.folder_backed_case_reader import FolderBackedCaseReader

__all__ = [
    "FleetEvent",
    "FleetEventKind",
    "FleetStatusBoardWatcher",
    "diff_rows",
    "diff_snapshots",
]


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
