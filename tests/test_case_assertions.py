# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the FolderBackedCase assertion facility (case_assertions spec)."""

from __future__ import annotations

from pathlib import Path

import pytest

from totodev_pub.folder_backed_case_support.case_journal import (
    CaseEventJournal, CaseEventJournalView,
)
from totodev_pub.folder_backed_case_support.constants import (
    ASSERTS_DIR_NAME, CASE_RESERVED_ARTIFACT_NAMES, EV_ASSERT_FAILED, EV_ASSERTED,
)


# ---------------------------------------------------------------------------
# Task 1: constants + journal surface
# ---------------------------------------------------------------------------

def test_assertions_dir_is_reserved_artifact():
    assert ASSERTS_DIR_NAME == "assertions"
    assert ASSERTS_DIR_NAME in CASE_RESERVED_ARTIFACT_NAMES


def test_journal_assert_writers_and_reader(tmp_path):
    journal = CaseEventJournal.for_folder(tmp_path)
    journal.log_state_entered("open")
    journal.log_assert_failed(
        "open.totals_balance",
        state="open", name="totals_balance", source="method", msg="totals differ",
    )
    journal.log_assert_failed(
        "broken.py",
        state="open", name=None, source="file:broken.py",
        msg="boom", error="SyntaxError",
    )
    journal.log_asserted("open", ran=3, failed=2, mode="full")

    fails = journal.assert_failures()
    assert len(fails) == 2
    assert fails[0].label == EV_ASSERT_FAILED          # most recent first
    d = fails[1].contents().as_dict()                  # the older one (method fail)
    assert d == {
        "state": "open", "name": "totals_balance",
        "source": "method", "msg": "totals differ",
    }
    d2 = fails[0].contents().as_dict()
    assert d2["error"] == "SyntaxError"
    assert d2["name"] is None

    summaries = list(journal.primitive.events(label_glob=EV_ASSERTED))
    assert len(summaries) == 1
    assert summaries[0].value == "open"
    assert summaries[0].contents().as_dict() == {"ran": 3, "failed": 2, "mode": "full"}


def test_journal_assert_failures_state_filter_and_view(tmp_path):
    journal = CaseEventJournal.for_folder(tmp_path)
    journal.log_assert_failed("a.x", state="a", name="x", source="method", msg="m1")
    journal.log_assert_failed("b.y", state="b", name="y", source="method", msg="m2")

    assert len(journal.assert_failures()) == 2
    only_a = journal.assert_failures(state="a")
    assert len(only_a) == 1
    assert only_a[0].value == "a.x"

    view = CaseEventJournalView.for_folder(tmp_path)
    assert len(view.assert_failures(state="b")) == 1


from totodev_pub.folder_backed_case_support.case_assertions import (
    ASSERT_METHOD_PREFIX, AssertionMode,
    get_case_assertion_mode, set_case_assertion_mode, match_assertion_name,
)


@pytest.fixture(autouse=True)
def _reset_assertion_mode():
    """The mode knob is process-global; every test starts and ends at FULL."""
    set_case_assertion_mode(AssertionMode.FULL)
    try:
        yield
    finally:
        set_case_assertion_mode(AssertionMode.FULL)


# ---------------------------------------------------------------------------
# Task 2: mode knob + name matching
# ---------------------------------------------------------------------------

def test_assertion_mode_knob_roundtrip_and_type_guard():
    assert get_case_assertion_mode() is AssertionMode.FULL
    set_case_assertion_mode(AssertionMode.SKIP)
    assert get_case_assertion_mode() is AssertionMode.SKIP
    with pytest.raises(TypeError):
        set_case_assertion_mode("skip")  # type: ignore[arg-type]


def test_match_assertion_name_greedy_longest_state_wins():
    states = ["open", "open_ticket", "closed"]
    # 'open_ticket' is the longest matching state; slug is what follows it.
    assert match_assertion_name("case_assert_open_ticket_check", states) == (
        "open_ticket", "check",
    )
    # Only 'open' matches here.
    assert match_assertion_name("case_assert_open_has_owner", states) == (
        "open", "has_owner",
    )


def test_match_assertion_name_rejections():
    states = ["open", "closed"]
    assert match_assertion_name("case_assert_nosuch_check", states) is None
    assert match_assertion_name("case_assert_open", states) is None      # empty slug
    assert match_assertion_name("case_assert_open_", states) is None     # empty slug
    assert match_assertion_name("unrelated_method", states) is None      # no prefix


from totodev_pub.folder_backed_case_support.case_assertions import (
    discover_class_assertions, validate_case_assertion_methods,
)
from totodev_pub.folder_backed_case_support.exceptions import FsmBindingError


# ---------------------------------------------------------------------------
# Task 3: bind-time validation + class-method discovery
# ---------------------------------------------------------------------------

STATES = ["new", "open", "open_ticket", "done"]


def test_validate_accepts_wellformed_sync_assertions():
    class Good:
        def case_assert_open_has_owner(self, ltx):
            return None

        def case_assert_open_ticket_check(self, ltx):
            return None

    validate_case_assertion_methods(Good, STATES)   # must not raise


def test_validate_rejects_unknown_state_with_teaching_message():
    class BadState:
        def case_assert_oepn_has_owner(self, ltx):  # typo'd state
            return None

    with pytest.raises(FsmBindingError) as ei:
        validate_case_assertion_methods(BadState, STATES)
    msg = str(ei.value)
    assert "case_assert_oepn_has_owner" in msg
    assert "case_assert_<state>_<slug>" in msg      # teaches the convention
    assert "open" in msg                            # lists known states
    assert ei.value.bad_assertions


def test_validate_rejects_missing_slug():
    class NoSlug:
        def case_assert_open(self, ltx):
            return None

    with pytest.raises(FsmBindingError):
        validate_case_assertion_methods(NoSlug, STATES)


def test_validate_rejects_async_assertion():
    class BadAsync:
        async def case_assert_open_has_owner(self, ltx):
            return None

    with pytest.raises(FsmBindingError) as ei:
        validate_case_assertion_methods(BadAsync, STATES)
    assert "synchronous" in str(ei.value)


def test_validate_rejects_bad_arity():
    class BadArity:
        def case_assert_open_has_owner(self):       # missing ltx
            return None

    with pytest.raises(FsmBindingError) as ei:
        validate_case_assertion_methods(BadArity, STATES)
    assert "ltx" in str(ei.value)


def test_discover_class_assertions_groups_and_sorts():
    class Multi:
        def case_assert_open_b_second(self, ltx):
            return None

        def case_assert_open_a_first(self, ltx):
            return None

        def case_assert_done_final(self, ltx):
            return None

    found = discover_class_assertions(Multi, STATES)
    assert found["open"] == [
        ("a_first", "case_assert_open_a_first"),
        ("b_second", "case_assert_open_b_second"),
    ]
    assert found["done"] == [("final", "case_assert_done_final")]
    assert "new" not in found


import asyncio

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_assertions import _CaseAssertionRunner
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


# ---------------------------------------------------------------------------
# Task 4: runner sweep core (class channel + modes)
# ---------------------------------------------------------------------------

class SweepCase(FolderBackedCase):
    asset_aliases = []
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]

    hook_calls: list  # set per-instance in tests

    def case_assert_open_passes(self, ltx):
        return None

    def case_assert_open_fails(self, ltx):
        return "totals do not balance"

    def case_assert_open_raises(self, ltx):
        raise ValueError("kaboom")

    def case_assert_done_truthy_nonstring(self, ltx):
        return 42

    def on_assertion_failed(self, state, name, msg):
        self.hook_calls.append((state, name, msg))


def _make_swept_case(tmp_path, name="sweep-case"):
    case = SweepCase.create_case_in_folder(tmp_path / name)
    case.hook_calls = []
    return case


def test_sweep_runs_class_assertions_and_journals(tmp_path):
    case = _make_swept_case(tmp_path)
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")

        fails = case._journal.assert_failures(state="open")
        assert {f.value for f in fails} == {"open.fails", "open.raises"}
        by_value = {f.value: f.contents().as_dict() for f in fails}
        assert by_value["open.fails"]["msg"] == "totals do not balance"
        assert by_value["open.fails"]["source"] == "method"
        assert "error" not in by_value["open.fails"]
        assert by_value["open.raises"]["error"] == "ValueError"
        assert "kaboom" in by_value["open.raises"]["msg"]

        summaries = list(case._journal.primitive.events(label_glob=EV_ASSERTED))
        assert len(summaries) == 1
        assert summaries[0].value == "open"
        assert summaries[0].contents().as_dict() == {
            "ran": 3, "failed": 2, "mode": "full",
        }
        # the on_assertion_failed hook fired once per failure
        assert sorted(n for _, n, _ in case.hook_calls) == ["fails", "raises"]
    finally:
        case.case_detach()


def test_sweep_truthy_nonstring_fails_with_str_message(tmp_path):
    case = _make_swept_case(tmp_path)
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("done")
        fails = case._journal.assert_failures(state="done")
        assert len(fails) == 1
        assert fails[0].contents().as_dict()["msg"] == "42"
    finally:
        case.case_detach()


def test_sweep_skip_mode_writes_summary_only(tmp_path):
    case = _make_swept_case(tmp_path)
    try:
        set_case_assertion_mode(AssertionMode.SKIP)
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")
        assert case._journal.assert_failures() == []
        summaries = list(case._journal.primitive.events(label_glob=EV_ASSERTED))
        assert len(summaries) == 1
        assert summaries[0].contents().as_dict() == {
            "ran": 0, "failed": 0, "mode": "skip",
        }
    finally:
        case.case_detach()


def test_sweep_misbehaving_hook_never_breaks_sweep(tmp_path):
    case = _make_swept_case(tmp_path)
    try:
        def _bad_hook(state, name, msg):
            raise RuntimeError("hook bug")
        case.on_assertion_failed = _bad_hook
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")                      # must not raise
        assert len(case._journal.assert_failures(state="open")) == 2
    finally:
        case.case_detach()


def test_sweep_state_with_no_assertions_still_summarizes(tmp_path):
    case = _make_swept_case(tmp_path)
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("new")
        summaries = list(case._journal.primitive.events(label_glob=EV_ASSERTED))
        assert len(summaries) == 1
        assert summaries[0].contents().as_dict() == {
            "ran": 0, "failed": 0, "mode": "full",
        }
    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# Task 5: per-case assertion files (assertions/*.py)
# ---------------------------------------------------------------------------

PASSING_AND_FAILING_FILE = '''
def case_assert_open_reader_sees_disk(case_reader, ltx):
    # the reader must expose the on-disk record; ltx carries the swept state
    if case_reader.case_id != "file-case":
        return f"unexpected case_id {case_reader.case_id!r}"
    return None

def case_assert_open_always_fails(case_reader, ltx):
    return "file says no"

def case_assert_done_other_state(case_reader, ltx):
    # tied to another KNOWN state: silently out of scope for an 'open' sweep
    return "should not run during open sweep"
'''

UNKNOWN_STATE_FILE = '''
def case_assert_nosuchstate_check(case_reader, ltx):
    return "never runs"
'''

BROKEN_FILE = "this is not python ("


def _make_file_case(tmp_path, files: dict):
    case = SweepCase.create_case_in_folder(tmp_path / "c", case_id="file-case")
    case.hook_calls = []
    asserts_dir = case.case_folder / ASSERTS_DIR_NAME
    asserts_dir.mkdir()
    for fname, body in files.items():
        (asserts_dir / fname).write_text(body)
    return case


def test_file_assertions_run_with_reader_and_state_scope(tmp_path):
    case = _make_file_case(tmp_path, {"checks.py": PASSING_AND_FAILING_FILE})
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")

        fails = case._journal.assert_failures(state="open")
        values = {f.value for f in fails}
        # class-channel fails (open.fails/open.raises) + the one file fail
        assert "open.always_fails" in values
        assert "open.other_state" not in values          # done-scoped fn did not run
        file_fail = [f for f in fails if f.value == "open.always_fails"][0]
        assert file_fail.contents().as_dict()["source"] == "file:checks.py"

        summary = list(case._journal.primitive.events(label_glob=EV_ASSERTED))[0]
        # 3 class assertions + 2 file assertions matched "open"
        assert summary.contents().as_dict()["ran"] == 5
    finally:
        case.case_detach()


def test_class_only_mode_never_imports_folder_code(tmp_path):
    case = _make_file_case(tmp_path, {"checks.py": PASSING_AND_FAILING_FILE})
    try:
        set_case_assertion_mode(AssertionMode.CLASS_ONLY)
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")
        values = {f.value for f in case._journal.assert_failures(state="open")}
        assert "open.always_fails" not in values
        summary = list(case._journal.primitive.events(label_glob=EV_ASSERTED))[0]
        assert summary.contents().as_dict() == {"ran": 3, "failed": 2,
                                                "mode": "class_only"}
    finally:
        case.case_detach()


def test_broken_assertion_file_logs_import_failure_and_continues(tmp_path):
    case = _make_file_case(
        tmp_path, {"aaa_broken.py": BROKEN_FILE, "checks.py": PASSING_AND_FAILING_FILE},
    )
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")
        fails = case._journal.assert_failures(state="open")
        broken = [f for f in fails if f.value == "aaa_broken.py"]
        assert len(broken) == 1
        d = broken[0].contents().as_dict()
        assert d["name"] is None
        assert d["source"] == "file:aaa_broken.py"
        assert d["error"]                              # exception type captured
        # the OTHER file still ran despite the broken sibling
        assert any(f.value == "open.always_fails" for f in fails)
    finally:
        case.case_detach()


def test_unknown_state_file_function_warns_and_skips(tmp_path):
    case = _make_file_case(tmp_path, {"odd.py": UNKNOWN_STATE_FILE})
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")
        runner.sweep("open")                           # second sweep: no double warn
        assert all(
            f.value != "nosuchstate.check"
            for f in case._journal.assert_failures()
        )
        assert len(runner._warned_unknown) == 1
    finally:
        case.case_detach()


def test_module_cache_reimports_on_mtime_change(tmp_path):
    import os
    case = _make_file_case(tmp_path, {"checks.py": PASSING_AND_FAILING_FILE})
    try:
        runner = _CaseAssertionRunner(case, type(case)._fsm, case._journal)
        runner.sweep("open")
        path = case.case_folder / ASSERTS_DIR_NAME / "checks.py"
        cached_module_1 = runner._module_cache[path][1]
        runner.sweep("open")
        assert runner._module_cache[path][1] is cached_module_1   # cache hit
        path.write_text(PASSING_AND_FAILING_FILE + "\n# touched\n")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        runner.sweep("open")
        assert runner._module_cache[path][1] is not cached_module_1  # re-imported
    finally:
        case.case_detach()
