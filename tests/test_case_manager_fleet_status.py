# Part of the totodev_pub library.

"""Fleet status board: observer, rows, case_ext_status_info, retention."""

import asyncio
import json

import pytest

from case_manager_test_utils import (
    ManualCase,
    TerminalCase,
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.constants import FLEET_STATUS_FILENAME
from totodev_pub.case_manager import CaseManager
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.case_manager_support.fleet_status import (
    build_live_row,
    collect_case_status_facts,
    parse_board_text,
    render_board_body,
)
from totodev_pub.case_manager_support.fleet_status_board import FleetStatusBoard
from totodev_pub.case_manager_support.fleet_status_events import FleetEventKind


def board_path(manager):
    return manager._manager_dir / FLEET_STATUS_FILENAME


def attach_board(manager, **kwargs) -> FleetStatusBoard:
    defaults = dict(
        publish_file=True,
        full_flush_interval_secs=0.0,
        terminal_retention_secs=120.0,
    )
    defaults.update(kwargs)
    board = FleetStatusBoard(manager, **defaults)
    board.attach()
    return board


async def wait_for(predicate, *, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# Facts collection and row building
# ---------------------------------------------------------------------------


def test_collect_facts_counts_alerts_in_current_dwell(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    case.case_emit_alert_event("first")
    case.case_emit_alert_event("second")
    facts = collect_case_status_facts(case.case_folder)
    assert facts["alert_count"] == 2
    assert facts["fail_count"] == 0
    assert facts["is_terminal"] is False
    assert facts["case_state"] == case.case_state
    assert facts["state_entered_at"] is not None
    case.case_detach()


def test_build_live_row_field_order_and_identity(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1", external_key="EXT-9")
    row = build_live_row(case)
    assert list(row) == [
        "case_id", "external_key", "case_type", "case_folder", "case_state",
        "state_entered_at", "is_terminal", "terminal_at", "active_transition",
        "last_transition_time", "alert_count", "slow_count", "fail_count", "ext",
    ]
    assert row["external_key"] == "EXT-9"
    assert row["case_type"] == "TicketCase"
    assert row["ext"] == {}
    case.case_detach()


def test_render_board_body_is_deterministic(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    row = build_live_row(case)
    case.case_detach()
    shuffled = dict(reversed(list(row.items())))
    shuffled["ext"] = {"b": 2, "a": 1}
    row["ext"] = {"a": 1, "b": 2}
    assert render_board_body([row]) == render_board_body([shuffled])
    parsed = json.loads(render_board_body([row]).splitlines()[0])
    assert list(parsed["ext"]) == ["a", "b"]


def test_parse_board_last_wins_and_skips_malformed():
    lines = [
        "# comment",
        "",
        '{"case_id":"a","case_type":"T","case_folder":"/x","case_state":"open"}',
        "{not json",
        '{"case_id":"a","case_type":"T","case_folder":"/x","case_state":"done"}',
    ]
    merged = parse_board_text("\n".join(lines))
    assert set(merged) == {"a"}
    assert merged["a"].case_state == "done"


# ---------------------------------------------------------------------------
# case_ext_status_info hook behavior
# ---------------------------------------------------------------------------


def test_case_ext_status_info_populates_ext(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    case.case_ext_status_info = lambda: {
        "nickname": "web-friendly",
        "state_echo": case.case_state,
    }
    row = build_live_row(case)
    case.case_detach()
    assert row["ext"] == {"nickname": "web-friendly", "state_echo": row["case_state"]}


def test_case_ext_status_info_none_return_means_vanilla(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    case.case_ext_status_info = lambda: None
    assert build_live_row(case)["ext"] == {}
    case.case_detach()


def test_case_ext_status_info_failure_yields_vanilla_row(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1")

    def broken():
        raise RuntimeError("boom")

    def unserializable():
        return {"obj": object()}

    def wrong_type():
        return ["not", "a", "dict"]

    case.case_ext_status_info = broken
    assert build_live_row(case)["ext"] == {}
    case.case_ext_status_info = unserializable
    assert build_live_row(case)["ext"] == {}
    case.case_ext_status_info = wrong_type
    assert build_live_row(case)["ext"] == {}
    case.case_detach()


# ---------------------------------------------------------------------------
# Board notify: skip-if-unchanged, append, full flush
# ---------------------------------------------------------------------------


def _jsonl_data_lines(path):
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_notify_skips_unchanged_row_unless_forced(tmp_path):
    manager = provision_manager(tmp_path)
    board = FleetStatusBoard(
        manager, full_flush_interval_secs=999.0, terminal_retention_secs=120.0
    )
    board._seeded = True
    if not board.board_path.parent.exists():
        board.board_path.parent.mkdir(parents=True)
    from totodev_pub.case_manager_support.fleet_status import publish_board
    publish_board(board.board_path, "")
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert board.notify(case) is True
    mtime1 = board.board_path.stat().st_mtime_ns
    size1 = board.board_path.stat().st_size
    assert board.notify(case) is False
    assert board.board_path.stat().st_mtime_ns == mtime1
    assert board.board_path.stat().st_size == size1
    assert board.notify(case, force=True) is True
    assert board.board_path.stat().st_mtime_ns != mtime1
    case.case_detach()


def test_notify_appends_changed_row_last_wins(tmp_path):
    manager = provision_manager(tmp_path)
    board = FleetStatusBoard(
        manager, full_flush_interval_secs=999.0, terminal_retention_secs=120.0
    )
    board._seeded = True
    from totodev_pub.case_manager_support.fleet_status import publish_board
    publish_board(board.board_path, "")
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert board.notify(case) is True
    case.case_emit_alert_event("ping")
    assert board.notify(case) is True
    lines = _jsonl_data_lines(board.board_path)
    assert len(lines) == 2
    merged = parse_board_text(board.board_path.read_text(encoding="utf-8"))
    assert merged[case.case_id].alert_count == 1
    case.case_detach()


def test_notify_full_flushes_when_interval_elapsed(tmp_path):
    manager = provision_manager(tmp_path)
    board = FleetStatusBoard(
        manager, full_flush_interval_secs=0.0, terminal_retention_secs=120.0
    )
    board._seeded = True
    from totodev_pub.case_manager_support.fleet_status import publish_board
    publish_board(board.board_path, "")
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert board.notify(case) is True
    case.case_emit_alert_event("one")
    assert board.notify(
        case,
        live_cases=[case],
        locate=lambda _cid: None,
    ) is True
    lines = _jsonl_data_lines(board.board_path)
    assert len(lines) == 1
    merged = parse_board_text(board.board_path.read_text(encoding="utf-8"))
    assert merged[case.case_id].alert_count == 1
    case.case_detach()


# ---------------------------------------------------------------------------
# Observer integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_without_board_does_not_write_fleet_file(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    try:
        await asyncio.sleep(0.15)
        assert not board_path(manager).exists()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_live_case_appears_on_board(tmp_path):
    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1", external_key="K-1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(manager)
    await manager.start()
    try:
        def on_board():
            return case.case_id in board.rows()

        assert await wait_for(on_board)
        row = board[case.case_id]
        assert row.case_state == "waiting"
        assert row.external_key == "K-1"
        assert row.case_type == "ManualCase"
        assert row.is_terminal is False
        file_rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
        assert case.case_id in file_rows
    finally:
        board.detach()
        await manager.stop()


@pytest.mark.asyncio
async def test_terminal_case_retained_then_expired(tmp_path):
    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TerminalCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(manager, terminal_retention_secs=0.6)
    await manager.start()
    try:
        await manager.fire(case_id=case.case_id, trigger="finish")

        def retained_terminal():
            row = board.rows().get(case.case_id)
            return row is not None and row.is_terminal and row.terminal_at is not None

        assert await wait_for(retained_terminal)

        def folder_reresolved():
            loc = manager.locate(case_id=case.case_id)
            if loc is None or loc.status != "terminated":
                return False
            row = board.rows().get(case.case_id)
            return row is not None and str(loc.case_folder) == row.case_folder

        assert await wait_for(folder_reresolved)

        def expired():
            return case.case_id not in board.rows()

        assert await wait_for(expired, timeout=5.0)
    finally:
        board.detach()
        await manager.stop()


@pytest.mark.asyncio
async def test_unchanged_fleet_skips_republish(tmp_path):
    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(manager, full_flush_interval_secs=999.0)
    await manager.start()
    try:
        def on_board():
            return case.case_id in board.rows()

        assert await wait_for(on_board)
        mtime1 = board_path(manager).stat().st_mtime_ns
        assert board.publish_full_if_due(
            list(manager._driver),
            locate=lambda cid: manager.locate(cid),
            force=False,
        ) is False
        assert board.publish_full(
            list(manager._driver),
            locate=lambda cid: manager.locate(cid),
        ) is False
        mtime2 = board_path(manager).stat().st_mtime_ns
        assert mtime1 == mtime2
    finally:
        board.detach()
        await manager.stop()


@pytest.mark.asyncio
async def test_board_case_ext_status_info_wiring(tmp_path):
    class ExtStatusCase(ManualCase):
        def case_ext_status_info(self):
            return {"from_hook": self.case_id.upper()}

    store = CaseManager.open_local_store(
        tmp_path / "cache",
        maintenance_interval_secs=0.05,
    )
    case_type_registry.register_case_types(
        TicketCase, TerminalCase, ManualCase, ExtStatusCase
    )
    manager = CaseManager(store)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ExtStatusCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(manager)
    await manager.start()
    try:
        def decorated():
            row = board.rows().get(case.case_id)
            return row is not None and row.ext.get("from_hook") == case.case_id.upper()

        assert await wait_for(decorated)
    finally:
        board.detach()
        await manager.stop()


@pytest.mark.asyncio
async def test_publish_file_false_updates_memory_only(tmp_path):
    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(manager, publish_file=False)
    await manager.start()
    try:
        assert await wait_for(lambda: case.case_id in board.rows())
        assert not board_path(manager).exists()
    finally:
        board.detach()
        await manager.stop()


@pytest.mark.asyncio
async def test_board_subscribe_emits_fleet_events(tmp_path):
    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TerminalCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(
        manager, publish_file=False, terminal_retention_secs=0.5
    )
    collected: list = []
    board.subscribe(lambda events: collected.extend(events))
    await manager.start()
    try:
        await manager.fire(case_id=case.case_id, trigger="finish")

        def saw_terminal():
            return any(e.kind == FleetEventKind.WENT_TERMINAL for e in collected)

        assert await wait_for(saw_terminal)

        def saw_disappeared():
            return any(
                e.kind == FleetEventKind.CASE_DISAPPEARED and e.case_id == case.case_id
                for e in collected
            )

        assert await wait_for(saw_disappeared, timeout=5.0)
    finally:
        board.detach()
        await manager.stop()


@pytest.mark.asyncio
async def test_raising_subscriber_does_not_break_board(tmp_path):
    manager = provision_manager(tmp_path, maintenance_interval_secs=0.05)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    board = attach_board(manager, publish_file=False)
    ok_events: list = []

    def boom(_events):
        raise RuntimeError("subscriber exploded")

    def ok(events):
        ok_events.extend(events)

    board.subscribe(boom)
    board.subscribe(ok)
    await manager.start()
    try:
        assert await wait_for(lambda: case.case_id in board.rows())
        # Force a row change so subscribers fire after baseline is set.
        case_obj = next(c for c in manager._driver if c.case_id == case.case_id)
        case_obj.case_emit_alert_event("ping")
        board.notify(
            case_obj,
            force=True,
            live_cases=list(manager._driver),
            locate=lambda cid: manager.locate(cid),
        )
        assert await wait_for(lambda: len(ok_events) > 0)
        assert case.case_id in board.rows()
    finally:
        board.detach()
        await manager.stop()
