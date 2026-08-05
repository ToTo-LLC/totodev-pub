# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Fleet status change events: typed snapshot diffs shared by the in-process
board and the out-of-process file watcher."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from totodev_pub.case_manager_support.fleet_status import FleetStatusRow


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
