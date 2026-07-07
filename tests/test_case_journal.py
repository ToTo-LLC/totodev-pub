"""Standalone tests for CaseJournal — the domain-aware event-log read/write facade.

These exercise the journal directly over a tmp event folder (no live FolderBackedCase),
proving it owns the case family's log conventions: the CASE_ naming invariant on writes
and the dwell-scoped reads (@FAIL count, has-event-since-enter, current state)."""

import pytest

from totodev_pub.folder_backed_case_support import constants
from totodev_pub.folder_backed_case_support.constants import (
    CASE_BASE_EVENT_PREFIX,
    EV_ALERTED,
)
from totodev_pub.folder_backed_case_support.case_journal import CaseJournal
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader


def _journal(tmp_path) -> CaseJournal:
    return CaseJournal.for_folder(tmp_path)


def _data_of(event) -> dict:
    payload = event.contents()
    return payload.as_dict() if payload is not None else {}


# ---------------------------------------------------------------------------
# Naming invariant
# ---------------------------------------------------------------------------

def test_all_base_event_constants_carry_the_prefix():
    """Every EV_* base label is reserved to the CASE_ family prefix — the invariant a
    subclass relies on to separate its own custom events from base lifecycle ones."""
    ev_labels = [
        getattr(constants, name)
        for name in dir(constants)
        if name.startswith("EV_")
    ]
    assert ev_labels, "expected EV_* base event constants to exist"
    for label in ev_labels:
        assert label.startswith(CASE_BASE_EVENT_PREFIX), label


def test_append_base_rejects_non_prefixed_label(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal._append_base("CUSTOM_EVENT", "value")


def test_append_base_accepts_prefixed_label(tmp_path):
    journal = _journal(tmp_path)
    ev = journal._append_base(f"{CASE_BASE_EVENT_PREFIX}SOMETHING", "v")
    assert ev.label == f"{CASE_BASE_EVENT_PREFIX}SOMETHING"


# ---------------------------------------------------------------------------
# Domain writes
# ---------------------------------------------------------------------------

def test_log_created_writes_label_value_and_payload(tmp_path):
    journal = _journal(tmp_path)
    journal.log_created("TicketCase", case_id="c-1", external_key="ext-9")
    ev = next(journal.primitive.events(label_glob="CASE_CREATED"))
    assert ev.value == "TicketCase"
    data = _data_of(ev)
    assert data["case_id"] == "c-1"
    assert data["external_key"] == "ext-9"


def test_log_state_entered_sets_current_state(tmp_path):
    journal = _journal(tmp_path)
    assert journal.current_state is None
    journal.log_state_entered("open")
    assert journal.current_state == "open"
    journal.log_state_entered("closed")
    assert journal.current_state == "closed"


def test_log_terminated_carries_from_state(tmp_path):
    journal = _journal(tmp_path)
    journal.log_terminated("done", from_state="open")
    ev = next(journal.primitive.events(label_glob="CASE_TERMINATED"))
    assert ev.value == "done"
    assert _data_of(ev)["from"] == "open"


def test_log_reclassified_carries_from_and_at_state(tmp_path):
    journal = _journal(tmp_path)
    journal.log_reclassified("NewCase", from_type="OldCase", at_state="new")
    ev = next(journal.primitive.events(label_glob="CASE_RECLASSIFIED"))
    assert ev.value == "NewCase"
    data = _data_of(ev)
    assert data["from"] == "OldCase"
    assert data["at_state"] == "new"


def test_log_alerted_defaults_and_payload(tmp_path):
    journal = _journal(tmp_path)
    journal.log_alerted("open", msg="needs a human")
    ev = next(journal.primitive.events(label_glob="CASE_ALERTED"))
    assert ev.value == "open"
    assert _data_of(ev)["msg"] == "needs a human"


def test_log_trigger_slow_value_is_rounded_seconds(tmp_path):
    journal = _journal(tmp_path)
    journal.log_trigger_slow("work", elapsed=12.4, warn=5.0, state="open")
    ev = next(journal.primitive.events(label_glob="CASE_TRIGGER_SLOW"))
    assert ev.value == "12"
    data = _data_of(ev)
    assert data["trigger"] == "work"
    assert data["warn_secs"] == 5.0
    assert data["state"] == "open"


def test_log_trigger_started_value_is_trigger_name(tmp_path):
    journal = _journal(tmp_path)
    journal.log_trigger_started("go", state="open", warn=5.0, kill=10.0)
    ev = next(journal.primitive.events(label_glob="CASE_TRIGGER_STARTED"))
    assert ev.value == "go"
    data = _data_of(ev)
    assert data["state"] == "open"
    assert data["warn_secs"] == 5.0
    assert data["kill_secs"] == 10.0


def test_log_state_entered_carries_trigger_payload(tmp_path):
    journal = _journal(tmp_path)
    journal.log_state_entered("open", trigger="go", from_state="new")
    ev = next(journal.primitive.events(label_glob="CASE_STATE_ENTERED"))
    assert ev.value == "open"
    data = _data_of(ev)
    assert data["trigger"] == "go"
    assert data["from"] == "new"


def test_log_state_entered_without_trigger_has_no_payload(tmp_path):
    journal = _journal(tmp_path)
    journal.log_state_entered("new")
    ev = next(journal.primitive.events(label_glob="CASE_STATE_ENTERED"))
    assert ev.value == "new"
    assert _data_of(ev) == {}


# ---------------------------------------------------------------------------
# Dwell-scoped domain reads
# ---------------------------------------------------------------------------

def test_count_fails_this_dwell_counts_fail_and_timeout(tmp_path):
    journal = _journal(tmp_path)
    journal.log_state_entered("open")
    assert journal.count_fails_this_dwell() == 0
    journal.log_transition_failed("go", {"trigger": "go"})
    journal.log_trigger_timed_out("go", {"trigger": "go"})
    assert journal.count_fails_this_dwell() == 2
    # Entry exceptions are NOT counted by @FAIL.
    journal.log_entry_exception("open", {"trigger": "go"})
    assert journal.count_fails_this_dwell() == 2


def test_count_fails_this_dwell_resets_at_next_enter(tmp_path):
    journal = _journal(tmp_path)
    journal.log_state_entered("open")
    journal.log_transition_failed("go", {"trigger": "go"})
    assert journal.count_fails_this_dwell() == 1
    journal.log_state_entered("open2")
    assert journal.count_fails_this_dwell() == 0


def test_has_event_since_enter(tmp_path):
    journal = _journal(tmp_path)
    journal.log_state_entered("open")
    assert journal.has_event_since_enter(EV_ALERTED) is False
    journal.log_alerted("open", msg="x")
    assert journal.has_event_since_enter(EV_ALERTED) is True
    # A fresh dwell clears the per-dwell view.
    journal.log_state_entered("open2")
    assert journal.has_event_since_enter(EV_ALERTED) is False


def test_last_state_entered_mtime_none_when_empty(tmp_path):
    journal = _journal(tmp_path)
    assert journal.last_state_entered_mtime() is None
    journal.log_state_entered("open")
    assert journal.last_state_entered_mtime() is not None


# ---------------------------------------------------------------------------
# In-flight trigger detection (unresolved CASE_TRIGGER_STARTED)
# ---------------------------------------------------------------------------

def test_unresolved_trigger_started_detection(tmp_path):
    journal = _journal(tmp_path)
    reader = journal.reader
    assert reader.unresolved_trigger_started is None
    journal.log_state_entered("new")
    assert reader.unresolved_trigger_started is None
    journal.log_trigger_started("go", state="new", warn=5.0, kill=10.0)
    ev = reader.unresolved_trigger_started
    assert ev is not None
    assert ev.value == "go"
    # A non-completion event logged mid-work (e.g. an alert) does not resolve it.
    journal.log_alerted("new", msg="still working")
    assert reader.unresolved_trigger_started is not None
    # Nor does the slow warning (written after the work, before the commit record).
    journal.log_trigger_slow("go", elapsed=6.0, warn=5.0, state="new")
    assert reader.unresolved_trigger_started is not None
    # The committed state entry resolves it.
    journal.log_state_entered("open", trigger="go", from_state="new")
    assert reader.unresolved_trigger_started is None


def test_unresolved_trigger_started_resolved_by_failure_events(tmp_path):
    journal = _journal(tmp_path)
    reader = journal.reader
    journal.log_state_entered("open")
    journal.log_trigger_started("go", state="open", warn=5.0, kill=10.0)
    journal.log_transition_failed("go", {"trigger": "go"})
    assert reader.unresolved_trigger_started is None
    journal.log_trigger_started("go", state="open", warn=5.0, kill=10.0)
    journal.log_trigger_timed_out("go", {"trigger": "go"})
    assert reader.unresolved_trigger_started is None
    journal.log_trigger_started("go", state="open", warn=5.0, kill=10.0)
    journal.log_entry_exception("done", {"trigger": "go"})
    assert reader.unresolved_trigger_started is None


# ---------------------------------------------------------------------------
# Reader alignment
# ---------------------------------------------------------------------------

def test_reader_is_base_event_label_shares_the_prefix():
    assert CaseEventLogReader.is_base_event_label("CASE_CREATED") is True
    assert CaseEventLogReader.is_base_event_label("MY_CUSTOM") is False
