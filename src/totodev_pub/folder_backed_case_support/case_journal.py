# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Domain-aware event-log read/write facade for the FolderBackedCase family.

`CaseJournal` wraps the generic, read-oriented `CaseEventLogReader` and becomes the
ONE place that knows how *this* case family reads and writes its event log:

  - WRITES funnel through a single chokepoint (`_append_base`) that enforces the
    family invariant — every base-class event label starts with
    `CASE_BASE_EVENT_PREFIX` — so a derived class can always separate its own custom
    events from the lifecycle events the base class generates. The domain-named
    `log_*` methods encode what label / value / payload each lifecycle fact carries.
  - DOMAIN READS (current state, the dwell anchor, the @FAIL count, "has this event
    fired since we entered the state") are the case-specific *interpretations* of the
    generic log that previously lived as private methods on FolderBackedCase.

Writes route through `.primitive` underneath: the journal owns the case
conventions, the PrimitiveEventLog owns storage. For base-class lifecycle writes,
journal `log_*` methods are the intended surface (they enforce naming policy and
payload conventions). Direct `.primitive` writes are an escape hatch for truly
bespoke behavior outside those base conventions.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from totodev_pub.primitive_event_log_support.event_proxy import PrimitiveEventProxy
from totodev_pub.primitive_event_log import PrimitiveEventLog
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.constants import (
    CASE_BASE_EVENT_PREFIX,
    EV_ENTER_STATE,
    EV_NEW,
    EV_CLOSED,
    EV_RECLASSIFY,
    EV_ALERT,
    EV_FAIL_TRANSITION,
    EV_ENTRY_EXCEPTION,
    EV_TRIGGER_SLOW,
    EV_TRIGGER_TIMEOUT,
)


class CaseJournal:
    """The case family's domain-aware view of its event log: convention-aware reads
    plus the sanctioned write surface, both over one `CaseEventLogReader`."""

    def __init__(self, reader: CaseEventLogReader) -> None:
        self._reader = reader

    @classmethod
    def for_folder(cls, folder: Path) -> "CaseJournal":
        """Build a journal over a case folder's event log."""
        return cls(CaseEventLogReader.for_folder(Path(folder)))

    # ---- escape hatches ----

    @property
    def reader(self) -> CaseEventLogReader:
        """The underlying convention-aware reader (for anything not modeled here)."""
        return self._reader

    @property
    def primitive(self) -> PrimitiveEventLog:
        """The raw, domain-agnostic log — an escape hatch for bespoke behavior not
        covered by journal methods. Prefer `log_*` for base lifecycle writes."""
        return self._reader.primitive

    # ---- write chokepoint: the naming invariant lives here ----

    def _append_base(
        self, label: str, value: str, data: Optional[dict] = None
    ) -> PrimitiveEventProxy:
        """Append a base-class lifecycle event. ENFORCES the family invariant: a base
        label MUST start with CASE_BASE_EVENT_PREFIX. Every base write goes through
        here, so a misnamed lifecycle label fails fast at the source."""
        if not label.startswith(CASE_BASE_EVENT_PREFIX):
            raise ValueError(
                f"base event label {label!r} must start with "
                f"{CASE_BASE_EVENT_PREFIX!r}; base-class events are reserved to that "
                "prefix so subclasses can isolate their own custom events."
            )
        return self._reader.primitive.create_event(label, value, data)

    # ---- domain writes (lifecycle facts) ----

    def log_new(
        self, case_type: str, *, case_id: str, external_key: str | None
    ) -> PrimitiveEventProxy:
        """Inception bookend (CASE_NEW)."""
        return self._append_base(
            EV_NEW, case_type, {"case_id": case_id, "external_key": external_key}
        )

    def log_enter_state(self, state: str) -> PrimitiveEventProxy:
        """Current fine-grained state entry (CASE_ENTER_STATE; value = state name)."""
        return self._append_base(EV_ENTER_STATE, state)

    def log_closed(self, closing_state: str, *, from_state: str) -> PrimitiveEventProxy:
        """Terminal bookend (CASE_CLOSED; value = closing state)."""
        return self._append_base(EV_CLOSED, closing_state, {"from": from_state})

    def log_reclassify(
        self, new_type: str, *, from_type: str, at_state: str
    ) -> PrimitiveEventProxy:
        """Rebind to a different case subclass (CASE_RECLASSIFY)."""
        return self._append_base(
            EV_RECLASSIFY, new_type, {"from": from_type, "at_state": at_state}
        )

    def log_alert(self, where: str, *, msg: str = "") -> PrimitiveEventProxy:
        """Needs-a-human escalation marker (CASE_ALERT)."""
        return self._append_base(EV_ALERT, where, {"msg": msg})

    def log_fail_transition(self, value: str, detail: dict) -> PrimitiveEventProxy:
        """Pre-commit attempt failed (CASE_FAIL_TRANSITION; counted by @FAIL)."""
        return self._append_base(EV_FAIL_TRANSITION, value, detail)

    def log_entry_exception(self, value: str, detail: dict) -> PrimitiveEventProxy:
        """Post-commit on_enter/after raised (CASE_ENTRY_EXCEPTION; NOT counted)."""
        return self._append_base(EV_ENTRY_EXCEPTION, value, detail)

    def log_trigger_timeout(self, value: str, detail: dict) -> PrimitiveEventProxy:
        """A trigger's work was hard-aborted at the kill ceiling (CASE_TRIGGER_TIMEOUT;
        @FAIL-counted, but visually distinct from an ordinary failed transition)."""
        return self._append_base(EV_TRIGGER_TIMEOUT, value, detail)

    def log_trigger_slow(
        self, trigger: str, *, elapsed: float, warn: float, state: str
    ) -> PrimitiveEventProxy:
        """A trigger's work outran its soft timeout (CASE_TRIGGER_SLOW; a warning). The
        elapsed whole-seconds go in the VALUE so it shows in the filename and is
        glob-scannable (e.g. `CASE_TRIGGER_SLOW@12s`); the precise figures ride in data."""
        return self._append_base(
            EV_TRIGGER_SLOW, str(round(elapsed)),
            {"trigger": trigger, "elapsed_secs": round(elapsed, 3),
             "warn_secs": warn, "state": state},
        )

    # ---- domain reads (case-specific interpretations of the generic log) ----

    @property
    def current_state(self) -> Optional[str]:
        """Current state = the most recent CASE_ENTER_STATE value."""
        return self._reader.current_state

    @property
    def last_activity(self) -> Optional[datetime.datetime]:
        """Modification time of the most recent event, or None if the log is empty."""
        return self._reader.last_activity

    def last_enter_state_mtime(self) -> Optional[datetime.datetime]:
        """Mtime of the latest CASE_ENTER_STATE event (the dwell anchor), or None when
        the case has not entered a state yet (brand-new). Naive/local, like all
        event-log mtimes; the caller converts to aware UTC."""
        ev = next(self._reader.primitive.events(label_glob=EV_ENTER_STATE), None)
        return ev.mtime if ev is not None else None

    def count_fails_this_dwell(self) -> int:
        """Count of failed pre-commit attempts since the current state was entered — the
        fact the `@FAIL` guard compares against. Counts BOTH CASE_FAIL_TRANSITION (the
        work raised) and CASE_TRIGGER_TIMEOUT (the work was hard-aborted): a timeout IS a
        failed attempt, and counting it here is what stops a timing-out trigger from
        re-firing forever under the implicit `@FAIL<1` cap. STATE-scoped: every failed
        attempt in this dwell counts, regardless of which trigger raised. Derived from the
        event log (no stored counter), so it is correct across process restarts and resets
        naturally at the next CASE_ENTER_STATE."""
        n = 0
        for ev in self._reader.primitive.events(recent_first=True):
            if ev.label == EV_ENTER_STATE:
                break                       # reached the boundary of the current dwell
            if ev.label in (EV_FAIL_TRANSITION, EV_TRIGGER_TIMEOUT):
                n += 1
        return n

    def has_event_since_enter(self, label: str) -> bool:
        """True if an event with `label` has been logged since the current state was
        entered. Used to keep blocked-state alerts to one per dwell."""
        for ev in self._reader.primitive.events(recent_first=True):
            if ev.label == EV_ENTER_STATE:
                return False
            if ev.label == label:
                return True
        return False
