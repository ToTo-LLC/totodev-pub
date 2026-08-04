# Part of the totodev_pub library.

"""FleetStatusBoardWatcher: snapshot-diff events, collection surface, client reads."""

import asyncio

import pytest

from case_manager_test_utils import (
    ManualCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.constants import FLEET_STATUS_FILENAME
from totodev_pub.case_manager_support.fleet_status import FleetStatusRow
from totodev_pub.case_manager_support.fleet_status_events import (
    FleetEventKind,
    diff_snapshots,
)
from totodev_pub.case_manager_support.fleet_status_watcher import (
    FleetStatusBoardWatcher,
)


def row(case_id="c1", **overrides):
    base = dict(
        case_id=case_id,
        case_type="T",
        case_folder=f"/x/{case_id}",
        case_state="open",
        state_entered_at="2026-07-07T00:00:00.000000Z",
        is_terminal=False,
        active_transition=None,
        alert_count=0,
        slow_count=0,
        fail_count=0,
        ext={},
    )
    base.update(overrides)
    return FleetStatusRow.model_validate(base)


def kinds(events):
    return [e.kind for e in events]


# ---------------------------------------------------------------------------
# Pure diff semantics
# ---------------------------------------------------------------------------


def test_diff_appear_and_disappear():
    events = diff_snapshots({}, {"c1": row()})
    assert kinds(events) == [FleetEventKind.CASE_APPEARED]
    events = diff_snapshots({"c1": row()}, {})
    assert kinds(events) == [FleetEventKind.CASE_DISAPPEARED]
    assert events[0].prior is not None and events[0].row is None


def test_diff_state_change_and_terminal():
    before = {"c1": row()}
    after = {"c1": row(case_state="done", is_terminal=True,
                       state_entered_at="2026-07-07T00:00:05.000000Z")}
    events = diff_snapshots(before, after)
    assert kinds(events) == [FleetEventKind.STATE_CHANGED, FleetEventKind.WENT_TERMINAL]
    assert events[0].detail == {"from_state": "open", "to_state": "done"}


def test_diff_detects_reentered_same_state():
    # A -> B -> A between polls: same state value, new dwell anchor.
    before = {"c1": row(state_entered_at="2026-07-07T00:00:00.000000Z")}
    after = {"c1": row(state_entered_at="2026-07-07T00:00:09.000000Z")}
    events = diff_snapshots(before, after)
    assert FleetEventKind.STATE_CHANGED in kinds(events)


def test_diff_counter_edges_same_dwell_and_new_dwell():
    # Same dwell: delta is new - old.
    events = diff_snapshots(
        {"c1": row(fail_count=1)}, {"c1": row(fail_count=3)}
    )
    failed = [e for e in events if e.kind == FleetEventKind.FAILED]
    assert len(failed) == 1 and failed[0].detail == {"count": 2}
    # New dwell: counters restarted; nonzero value is fresh news even if numerically lower.
    events = diff_snapshots(
        {"c1": row(fail_count=3)},
        {"c1": row(state_entered_at="2026-07-07T00:00:09.000000Z", fail_count=1)},
    )
    failed = [e for e in events if e.kind == FleetEventKind.FAILED]
    assert len(failed) == 1 and failed[0].detail == {"count": 1}
    # Same dwell, unchanged counter: no event.
    events = diff_snapshots(
        {"c1": row(slow_count=2)}, {"c1": row(slow_count=2)}
    )
    assert FleetEventKind.WENT_SLOW not in kinds(events)


def test_diff_trigger_start_end_and_ext():
    events = diff_snapshots(
        {"c1": row()}, {"c1": row(active_transition="work")}
    )
    assert FleetEventKind.TRIGGER_STARTED in kinds(events)
    events = diff_snapshots(
        {"c1": row(active_transition="work")},
        {"c1": row(active_transition="push")},
    )
    assert kinds(events) == [FleetEventKind.TRIGGER_ENDED, FleetEventKind.TRIGGER_STARTED]
    events = diff_snapshots(
        {"c1": row(ext={"a": 1})}, {"c1": row(ext={"a": 2})}
    )
    assert kinds(events) == [FleetEventKind.EXT_CHANGED]


# ---------------------------------------------------------------------------
# Watcher over a real board file
# ---------------------------------------------------------------------------


def write_board(path, rows):
    from totodev_pub.case_manager_support.fleet_status import publish_board, render_board_body
    publish_board(path, render_board_body([r.model_dump() for r in rows]))


def test_watcher_priming_and_mtime_short_circuit(tmp_path):
    path = tmp_path / FLEET_STATUS_FILENAME
    write_board(path, [row("c1")])
    watcher = FleetStatusBoardWatcher(path)
    assert watcher.poll() == []          # priming poll establishes the baseline
    assert watcher.poll() == []          # unchanged mtime: no parse, no events
    write_board(path, [row("c1", case_state="done",
                           state_entered_at="2026-07-07T00:00:09.000000Z")])
    events = watcher.poll()
    assert FleetEventKind.STATE_CHANGED in kinds(events)


def test_watcher_emit_initial(tmp_path):
    path = tmp_path / FLEET_STATUS_FILENAME
    write_board(path, [row("c1"), row("c2")])
    watcher = FleetStatusBoardWatcher(path, emit_initial=True)
    events = watcher.poll()
    assert kinds(events) == [FleetEventKind.CASE_APPEARED, FleetEventKind.CASE_APPEARED]
    assert [e.case_id for e in events] == ["c1", "c2"]


def test_watcher_collection_surface(tmp_path):
    path = tmp_path / FLEET_STATUS_FILENAME
    case_folder = tmp_path / "case1"
    from case_manager_test_utils import TicketCase
    case = TicketCase.create_case_in_folder(case_folder)
    case.case_detach()
    write_board(path, [row("c1", case_folder=str(case_folder)), row("c2")])
    watcher = FleetStatusBoardWatcher(path)
    watcher.poll()
    assert len(watcher) == 2
    assert "c1" in watcher and "zzz" not in watcher
    assert watcher["c2"].case_id == "c2"
    assert [r.case_id for r in watcher] == ["c1", "c2"]
    # Readers construct free of I/O and peek lazily; the real folder resolves.
    reader = watcher.reader("c1")
    assert reader.case_id == case.case_id
    pairs = list(watcher.iter_readers())
    assert [r.case_id for r, _ in pairs] == ["c1", "c2"]


# ---------------------------------------------------------------------------
# Client surface end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_reads_and_watches_board(tmp_path):
    from totodev_pub.case_manager_support.fleet_status_board import FleetStatusBoard

    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = FleetStatusBoard(
        manager, publish_file=True, full_flush_interval_secs=0.0
    )
    board.attach()
    await manager.start()
    try:
        client = CaseManagerClient(tmp_path / "cache")
        watcher = client.fleet_status_watcher(emit_initial=True)

        async def poll_until_appeared():
            collected = []
            for _ in range(60):
                collected.extend(watcher.poll())
                if any(e.kind == FleetEventKind.CASE_APPEARED for e in collected):
                    return collected
                await asyncio.sleep(0.05)
            return collected

        events = await poll_until_appeared()
        assert any(
            e.kind == FleetEventKind.CASE_APPEARED and e.case_id == case.case_id
            for e in events
        )
        rows = client.read_fleet_status(only_if_fresh=False)
        assert rows[case.case_id].case_state == "waiting"
    finally:
        board.detach()
        await manager.stop()


def test_client_read_raises_when_board_unpublished(tmp_path):
    provision_manager(tmp_path)  # creates cache layout; no board attached
    client = CaseManagerClient(tmp_path / "cache")
    with pytest.raises(FileNotFoundError):
        client.read_fleet_status(only_if_fresh=False)
