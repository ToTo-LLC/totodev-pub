# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseEventLogReader: read-oriented interpreter of case conventions over the
domain-agnostic PrimitiveEventLog."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from totodev_pub.primitive_event_log import PrimitiveEventLog
from totodev_pub.primitive_event_log_support.event_proxy import PrimitiveEventProxy
from totodev_pub.folder_backed_case_support.constants import (
    CASE_BASE_EVENT_PREFIX,
    EV_STATE_ENTERED,
    EV_TERMINATED,
    EV_ENTRY_EXCEPTION,
    EV_TRANSITION_FAILED,
    EV_TRIGGER_STARTED,
    EV_TRIGGER_TIMED_OUT,
    EVENTS_DIR_NAME,
)

# The events that RESOLVE a CASE_TRIGGER_STARTED: the attempt committed (STATE_ENTERED) or
# failed in one of its recorded ways. Anything else logged mid-work (an alert, a slow
# warning, a subclass's custom event) leaves the START unresolved — still in flight.
_TRIGGER_START_RESOLUTION_LABELS = frozenset({
    EV_STATE_ENTERED, EV_TRANSITION_FAILED, EV_TRIGGER_TIMED_OUT, EV_ENTRY_EXCEPTION,
})


class CaseEventLogReader:
    """Read-oriented wrapper over the domain-agnostic PrimitiveEventLog that owns
    the INTERPRETATION of case conventions in one place. Writes are not its job:
    callers that must append go through `.primitive` (the underlying log)."""

    def __init__(self, event_dir: Path):
        self._log = PrimitiveEventLog(event_dir=event_dir)

    @classmethod
    def for_folder(cls, folder: Path) -> "CaseEventLogReader":
        return cls(folder / EVENTS_DIR_NAME)

    @property
    def primitive(self) -> PrimitiveEventLog:
        """The underlying log — the escape hatch for bespoke reads/writes that the
        journal does not model. The base-class write path should use CaseJournal."""
        return self._log

    @staticmethod
    def is_base_event_label(label: str) -> bool:
        """True when `label` belongs to the base-class lifecycle namespace (the
        CASE_BASE_EVENT_PREFIX family). The read-side companion to CaseJournal's
        write-side prefix enforcement: both share one definition of the reserved
        prefix, so an observer can split base events from a subclass's custom ones."""
        return label.startswith(CASE_BASE_EVENT_PREFIX)

    # ---- convention-aware reads ----

    @property
    def current_state(self) -> Optional[str]:
        """Latest CASE_STATE_ENTERED value == fine-grained current state."""
        ev = next(self._log.events(label_glob=EV_STATE_ENTERED), None)  # recent_first=True default
        return ev.value if ev else None

    @property
    def is_terminal(self) -> bool:
        """True when a CASE_TERMINATED bookend event is present."""
        return bool(self._log.has_event(EV_TERMINATED))

    @property
    def status(self) -> str:
        """Coarse 'live' / 'terminal'."""
        return "terminal" if self.is_terminal else "live"

    @property
    def last_activity(self) -> Optional[datetime.datetime]:
        """Modification time of the most recent event, or None if the log is empty."""
        ev = next(self._log.events(), None)  # most recent event (recent_first=True)
        return ev.mtime if ev else None

    @property
    def last_state_entered_mtime(self) -> Optional[datetime.datetime]:
        """Mtime of the latest CASE_STATE_ENTERED event (the dwell anchor), or None when
        the case has not entered a state yet (brand-new). Naive/local, like all
        event-log mtimes; the caller converts to aware UTC."""
        ev = next(self._log.events(label_glob=EV_STATE_ENTERED), None)
        return ev.mtime if ev is not None else None

    @property
    def unresolved_trigger_started(self) -> Optional[PrimitiveEventProxy]:
        """The latest CASE_TRIGGER_STARTED not yet resolved by a completion event, or None.

        Walks recent-first: the first resolution label hit (CASE_STATE_ENTERED /
        CASE_TRANSITION_FAILED / CASE_TRIGGER_TIMED_OUT / CASE_ENTRY_EXCEPTION) means the
        latest attempt concluded — None. Hitting a CASE_TRIGGER_STARTED first means that
        attempt has no recorded outcome. This is a pure LOG fact: it cannot tell
        "in flight right now" from "owner crashed mid-work" — cross-check lease
        liveness for that (see FolderBackedCaseReader.case_active_trigger)."""
        for ev in self._log.events(recent_first=True):
            if ev.label == EV_TRIGGER_STARTED:
                return ev
            if ev.label in _TRIGGER_START_RESOLUTION_LABELS:
                return None
        return None

    @property
    def transition_fail_count(self) -> int:
        """How many transition attempts have FAILED while the case has been in its CURRENT
        state. Counts both a transition whose work raised (CASE_TRANSITION_FAILED) and one
        whose work was hard-aborted by a trigger timeout (CASE_TRIGGER_TIMED_OUT) — a timeout
        is a failed attempt. STATE-scoped, not a lifetime total: the walk stops at the
        latest CASE_STATE_ENTERED (the dwell boundary), so the count resets to 0 whenever the
        case enters a new state. The fact the `@FAIL` guard compares against."""
        n = 0
        for ev in self._log.events(recent_first=True):
            if ev.label == EV_STATE_ENTERED:
                break                       # reached the boundary of the current dwell
            if ev.label in (EV_TRANSITION_FAILED, EV_TRIGGER_TIMED_OUT):
                n += 1
        return n
