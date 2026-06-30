# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseEventLogReader: read-oriented interpreter of case conventions over the
domain-agnostic PrimitiveEventLog."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from totodev_pub.primitive_event_log import PrimitiveEventLog
from totodev_pub.folder_backed_case_support.constants import (
    CASE_BASE_EVENT_PREFIX,
    EV_ENTER_STATE,
    EV_CLOSED,
    EV_FAIL_TRANSITION,
    EV_TRIGGER_TIMEOUT,
    EVENTS_DIR_NAME,
)


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
        """Latest CASE_ENTER_STATE value == fine-grained current state."""
        ev = next(self._log.events(label_glob=EV_ENTER_STATE), None)  # recent_first=True default
        return ev.value if ev else None

    @property
    def is_closed(self) -> bool:
        """True when a CASE_CLOSED bookend event is present."""
        return bool(self._log.has_event(EV_CLOSED))

    @property
    def status(self) -> str:
        """Coarse 'open' / 'closed'."""
        return "closed" if self.is_closed else "open"

    @property
    def last_activity(self) -> Optional[datetime.datetime]:
        """Modification time of the most recent event, or None if the log is empty."""
        ev = next(self._log.events(), None)  # most recent event (recent_first=True)
        return ev.mtime if ev else None

    @property
    def last_enter_state_mtime(self) -> Optional[datetime.datetime]:
        """Mtime of the latest CASE_ENTER_STATE event (the dwell anchor), or None when
        the case has not entered a state yet (brand-new). Naive/local, like all
        event-log mtimes; the caller converts to aware UTC."""
        ev = next(self._log.events(label_glob=EV_ENTER_STATE), None)
        return ev.mtime if ev is not None else None

    @property
    def transition_fail_count(self) -> int:
        """How many transition attempts have FAILED while the case has been in its CURRENT
        state. Counts both a transition whose work raised (CASE_FAIL_TRANSITION) and one
        whose work was hard-aborted by a trigger timeout (CASE_TRIGGER_TIMEOUT) — a timeout
        is a failed attempt. STATE-scoped, not a lifetime total: the walk stops at the
        latest CASE_ENTER_STATE (the dwell boundary), so the count resets to 0 whenever the
        case enters a new state. The fact the `@FAIL` guard compares against."""
        n = 0
        for ev in self._log.events(recent_first=True):
            if ev.label == EV_ENTER_STATE:
                break                       # reached the boundary of the current dwell
            if ev.label in (EV_FAIL_TRANSITION, EV_TRIGGER_TIMEOUT):
                n += 1
        return n
