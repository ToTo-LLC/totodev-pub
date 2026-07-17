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
