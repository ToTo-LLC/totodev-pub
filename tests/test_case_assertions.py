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
