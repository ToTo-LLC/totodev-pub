# Part of the totodev_pub library.

"""Fleet status board: writer, rows, decorator, retention (Fleet Status Board Spec)."""

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
from totodev_pub.case_manager_support.constants import (
    FLEET_BOARD_DISABLED_PREFIX,
    FLEET_STATUS_FILENAME,
)
from totodev_pub.case_manager_support.exceptions import FleetStatusBoardDisabledError
from totodev_pub.case_manager_support.fleet_status import (
    FleetStatusBoardWriter,
    build_live_row,
    collect_case_status_facts,
    parse_board_text,
    render_board_body,
)


def board_path(manager):
    return manager._manager_dir / FLEET_STATUS_FILENAME


def provision_fleet_manager(tmp_path, **overrides):
    return provision_manager(
        tmp_path,
        enable_fleet_status_board=True,
        fleet_status_full_flush_interval_secs=0.0,
        **overrides,
    )


async def wait_for(predicate, *, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# Known location / disabled sentinel
# ---------------------------------------------------------------------------


def test_disabled_board_holds_sentinel(tmp_path):
    manager = provision_manager(tmp_path, enable_fleet_status_board=False)
    path = board_path(manager)
    assert path.exists()
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith(FLEET_BOARD_DISABLED_PREFIX)
    assert "enable_fleet_status_board" in first
    with pytest.raises(FleetStatusBoardDisabledError):
        parse_board_text(path.read_text(encoding="utf-8"))


def test_enabled_board_replaces_sentinel(tmp_path):
    manager = provision_fleet_manager(tmp_path)
    text = board_path(manager).read_text(encoding="utf-8")
    assert not any(
        line.startswith(FLEET_BOARD_DISABLED_PREFIX) for line in text.splitlines()
    )
    assert parse_board_text(text) == {}


# ---------------------------------------------------------------------------
# Facts collection and row building
# ---------------------------------------------------------------------------


def test_collect_facts_counts_alerts_in_current_dwell(tmp_path):
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    case.case_log_alert("first")
    case.case_log_alert("second")
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
# Decorator (visitor) behavior
# ---------------------------------------------------------------------------


def test_decorator_populates_ext_and_cannot_touch_standard(tmp_path):
    seen = {}

    def decorator(case, standard):
        seen["standard_is_readonly"] = False
        try:
            standard["case_id"] = "hacked"
        except TypeError:
            seen["standard_is_readonly"] = True
        return {"nickname": "web-friendly", "state_echo": standard["case_state"]}

    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    row = build_live_row(case, decorator=decorator)
    case.case_detach()
    assert row["ext"] == {"nickname": "web-friendly", "state_echo": row["case_state"]}
    assert row["case_id"] != "hacked"
    assert seen["standard_is_readonly"] is True


def test_decorator_none_return_means_vanilla(tmp_path):
    def quiet(case, standard):
        return None  # bare `return` — the natural "nothing to add"

    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert build_live_row(case, decorator=quiet)["ext"] == {}
    case.case_detach()


def test_decorator_failure_yields_vanilla_row(tmp_path):
    def broken(case, standard):
        raise RuntimeError("boom")

    def unserializable(case, standard):
        return {"obj": object()}

    def wrong_type(case, standard):
        return ["not", "a", "dict"]

    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert build_live_row(case, decorator=broken)["ext"] == {}
    assert build_live_row(case, decorator=unserializable)["ext"] == {}
    assert build_live_row(case, decorator=wrong_type)["ext"] == {}
    case.case_detach()


# ---------------------------------------------------------------------------
# Writer notify: skip-if-unchanged, append, full flush
# ---------------------------------------------------------------------------


def _jsonl_data_lines(path):
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_notify_skips_unchanged_row_unless_forced(tmp_path):
    mgr_dir = tmp_path / "mgr"
    mgr_dir.mkdir()
    writer = FleetStatusBoardWriter(
        mgr_dir, full_flush_interval_secs=999.0, terminal_retention_secs=120.0
    )
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert writer.notify(case) is True  # first sighting
    mtime1 = writer.board_path.stat().st_mtime_ns
    size1 = writer.board_path.stat().st_size
    assert writer.notify(case) is False  # unchanged → silent skip
    assert writer.board_path.stat().st_mtime_ns == mtime1
    assert writer.board_path.stat().st_size == size1
    assert writer.notify(case, force=True) is True
    assert writer.board_path.stat().st_mtime_ns != mtime1
    case.case_detach()


def test_notify_appends_changed_row_last_wins(tmp_path):
    mgr_dir = tmp_path / "mgr"
    mgr_dir.mkdir()
    writer = FleetStatusBoardWriter(
        mgr_dir, full_flush_interval_secs=999.0, terminal_retention_secs=120.0
    )
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert writer.notify(case) is True
    case.case_log_alert("ping")
    assert writer.notify(case) is True
    lines = _jsonl_data_lines(writer.board_path)
    assert len(lines) == 2  # two appends, not compacted
    merged = parse_board_text(writer.board_path.read_text(encoding="utf-8"))
    assert merged[case.case_id].alert_count == 1
    case.case_detach()


def test_notify_full_flushes_when_interval_elapsed(tmp_path):
    mgr_dir = tmp_path / "mgr"
    mgr_dir.mkdir()
    writer = FleetStatusBoardWriter(
        mgr_dir, full_flush_interval_secs=0.0, terminal_retention_secs=120.0
    )
    case = TicketCase.create_case_in_folder(tmp_path / "c1")
    assert writer.notify(case) is True
    case.case_log_alert("one")
    assert writer.notify(
        case,
        live_cases=[case],
        locate=lambda _cid: None,
    ) is True
    lines = _jsonl_data_lines(writer.board_path)
    assert len(lines) == 1  # compacted full rewrite
    merged = parse_board_text(writer.board_path.read_text(encoding="utf-8"))
    assert merged[case.case_id].alert_count == 1
    case.case_detach()


# ---------------------------------------------------------------------------
# Manager integration: refresh, terminal retention, folder re-resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_case_appears_on_board(tmp_path):
    manager = provision_fleet_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1", external_key="K-1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    await manager.start()
    try:
        def on_board():
            rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
            return case.case_id in rows
        assert await wait_for(on_board)
        rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
        row = rows[case.case_id]
        assert row.case_state == "waiting"
        assert row.external_key == "K-1"
        assert row.case_type == "ManualCase"
        assert row.is_terminal is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_terminal_case_retained_then_expired(tmp_path):
    manager = provision_fleet_manager(
        tmp_path, fleet_status_terminal_retention_secs=0.6
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TerminalCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    await manager.start()
    try:
        await manager.fire(case_id=case.case_id, trigger="finish")

        def retained_terminal():
            rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
            row = rows.get(case.case_id)
            return row is not None and row.is_terminal and row.terminal_at is not None
        assert await wait_for(retained_terminal)

        # After the termination pipeline moves the folder, the row re-resolves.
        def folder_reresolved():
            loc = manager.locate(case_id=case.case_id)
            if loc is None or not loc.grouping_key[0].startswith("terminal_"):
                return False
            rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
            row = rows.get(case.case_id)
            return row is not None and str(loc.case_folder) == row.case_folder
        assert await wait_for(folder_reresolved)

        def expired():
            rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
            return case.case_id not in rows
        assert await wait_for(expired, timeout=5.0)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unchanged_fleet_skips_republish(tmp_path):
    manager = provision_fleet_manager(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    await manager.start()
    try:
        def on_board():
            rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
            return case.case_id in rows
        assert await wait_for(on_board)
        mtime1 = board_path(manager).stat().st_mtime_ns
        await asyncio.sleep(0.3)  # many maintenance ticks, no fleet change
        mtime2 = board_path(manager).stat().st_mtime_ns
        assert mtime1 == mtime2
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_decorator_wiring(tmp_path):
    def decorator(case, standard):
        return {"from_decorator": case.case_id.upper()}

    manager = provision_fleet_manager(tmp_path, fleet_status_decorator=decorator)
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(ManualCase, staging / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, staging / "c1")
    await manager.start()
    try:
        def decorated():
            rows = parse_board_text(board_path(manager).read_text(encoding="utf-8"))
            row = rows.get(case.case_id)
            return row is not None and row.ext.get("from_decorator") == case.case_id.upper()
        assert await wait_for(decorated)
    finally:
        await manager.stop()
