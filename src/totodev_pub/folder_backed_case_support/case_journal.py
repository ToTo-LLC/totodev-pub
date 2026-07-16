# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Domain-aware event-log protocol for the FolderBackedCase family.

`CaseEventJournal` is the ONE place that knows how *this* case family reads and writes
its event log:

  - WRITES funnel through a single chokepoint (`_append_base`) that enforces the
    family invariant — every base-class event label starts with
    `CASE_BASE_EVENT_PREFIX` — so a derived class can always separate its own custom
    events from the lifecycle events the base class generates. The domain-named
    `log_*` methods encode what label / value / payload each lifecycle fact carries.
  - DOMAIN READS (current state, the dwell anchor, the @FAIL count, "has this event
    fired since we entered the state") are the case-specific *interpretations* of
    the generic log.

`CaseEventJournalView` is a read-only facade over the same protocol — for observers
that must not append lifecycle facts (peek paths, fleet scans, FolderBackedCaseReader).
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from totodev_pub.primitive_event_log_support.event_proxy import PrimitiveEventProxy
from totodev_pub.primitive_event_log import PrimitiveEventLog
from totodev_pub.folder_backed_case_support.constants import (
    CASE_BASE_EVENT_PREFIX,
    EV_STATE_ENTERED,
    EV_CREATED,
    EV_TERMINATED,
    EV_RECLASSIFIED,
    EV_ALERTED,
    EV_TRANSITION_FAILED,
    EV_ENTRY_EXCEPTION,
    EV_TRIGGER_SLOW,
    EV_TRIGGER_TIMED_OUT,
    EV_TRIGGER_STARTED,
    EVENTS_DIR_NAME,
)

# Events that RESOLVE a CASE_TRIGGER_STARTED: the attempt committed (STATE_ENTERED) or
# failed in one of its recorded ways. Anything else logged mid-work (an alert, a slow
# warning, a subclass's custom event) leaves the START unresolved — still in flight.
_TRIGGER_START_RESOLUTION_LABELS = frozenset({
    EV_STATE_ENTERED, EV_TRANSITION_FAILED, EV_TRIGGER_TIMED_OUT, EV_ENTRY_EXCEPTION,
})


class CaseEventJournal:
    """The case family's authoritative event-log protocol: convention-aware reads
    plus the sanctioned write surface, both over one PrimitiveEventLog."""

    def __init__(self, folder: Path) -> None:
        self._folder = Path(folder)
        self._log = PrimitiveEventLog(event_dir=self._folder / EVENTS_DIR_NAME)

    @classmethod
    def for_folder(cls, folder: Path) -> "CaseEventJournal":
        """Build a journal over a case folder's event log."""
        return cls(Path(folder))

    # ---- escape hatches ----

    @property
    def primitive(self) -> PrimitiveEventLog:
        """The raw, domain-agnostic log — an escape hatch for bespoke behavior not
        covered by journal methods. Prefer `log_*` for base lifecycle writes."""
        return self._log

    def view(self) -> "CaseEventJournalView":
        """A fresh read-only facade over this folder's event log."""
        return CaseEventJournalView.for_folder(self._folder)

    @staticmethod
    def is_base_event_label(label: str) -> bool:
        """True when `label` belongs to the base-class lifecycle namespace (the
        CASE_BASE_EVENT_PREFIX family). The read-side companion to write-side prefix
        enforcement: both share one definition of the reserved prefix, so an observer
        can split base events from a subclass's custom ones."""
        return label.startswith(CASE_BASE_EVENT_PREFIX)

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
        return self._log.create_event(label, value, data)

    # ---- domain writes (lifecycle facts) ----

    def log_created(
        self, case_type: str, *, case_id: str, external_key: str | None
    ) -> PrimitiveEventProxy:
        """Inception bookend (CASE_CREATED)."""
        return self._append_base(
            EV_CREATED, case_type, {"case_id": case_id, "external_key": external_key}
        )

    def log_state_entered(
        self, state: str, *, trigger: str | None = None, from_state: str | None = None,
    ) -> PrimitiveEventProxy:
        """Current fine-grained state entry (CASE_STATE_ENTERED; value = state name).

        When the entry was produced by a transition, `trigger` (and `from_state`)
        ride in the data payload so history can answer "which trigger produced this
        state" without the compiled FSM. The inception entry has no trigger and
        stays a payload-free marker; readers treat a missing payload as
        trigger-unknown (also true of folders written before this payload existed)."""
        data = {"trigger": trigger, "from": from_state} if trigger is not None else None
        return self._append_base(EV_STATE_ENTERED, state, data)

    def log_terminated(self, terminal_state: str, *, from_state: str) -> PrimitiveEventProxy:
        """Terminal bookend (CASE_TERMINATED; value = the terminal state entered)."""
        return self._append_base(EV_TERMINATED, terminal_state, {"from": from_state})

    def log_reclassified(
        self, new_type: str, *, from_type: str, at_state: str
    ) -> PrimitiveEventProxy:
        """Rebind to a different case subclass (CASE_RECLASSIFIED)."""
        return self._append_base(
            EV_RECLASSIFIED, new_type, {"from": from_type, "at_state": at_state}
        )

    def log_alerted(self, where: str, *, msg: str = "") -> PrimitiveEventProxy:
        """Needs-a-human escalation marker (CASE_ALERTED)."""
        return self._append_base(EV_ALERTED, where, {"msg": msg})

    def log_transition_failed(self, value: str, detail: dict) -> PrimitiveEventProxy:
        """Pre-commit attempt failed (CASE_TRANSITION_FAILED; counted by @FAIL)."""
        return self._append_base(EV_TRANSITION_FAILED, value, detail)

    def log_entry_exception(self, value: str, detail: dict) -> PrimitiveEventProxy:
        """Post-commit on_enter/after raised (CASE_ENTRY_EXCEPTION; NOT counted)."""
        return self._append_base(EV_ENTRY_EXCEPTION, value, detail)

    def log_trigger_timed_out(self, value: str, detail: dict) -> PrimitiveEventProxy:
        """A trigger's work was hard-aborted at the kill ceiling (CASE_TRIGGER_TIMED_OUT;
        @FAIL-counted, but visually distinct from an ordinary failed transition)."""
        return self._append_base(EV_TRIGGER_TIMED_OUT, value, detail)

    def log_trigger_started(
        self, trigger: str, *, state: str, warn: float, kill: float,
    ) -> PrimitiveEventProxy:
        """A trigger's work slot began (CASE_TRIGGER_STARTED; value = trigger name, so an
        in-flight trigger is glob-scannable in the events folder). Written just before
        the timed `perform_` work runs — only for edges that HAVE work; instantaneous
        pure-routing transitions log nothing. Resolved by whichever completion fact
        follows (CASE_STATE_ENTERED on success; CASE_TRANSITION_FAILED / CASE_TRIGGER_TIMED_OUT /
        CASE_ENTRY_EXCEPTION on failure): a dangling START with a live lease means the
        work is in flight NOW; with a dead lease, the owner crashed mid-work. See
        `unresolved_trigger_started` for the read side."""
        return self._append_base(
            EV_TRIGGER_STARTED, trigger,
            {"state": state, "warn_secs": warn, "kill_secs": kill},
        )

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
        """Current state = the most recent CASE_STATE_ENTERED value."""
        ev = next(self._log.events(label_glob=EV_STATE_ENTERED), None)
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
    def last_activity_at(self) -> Optional[datetime.datetime]:
        """Modification time of the most recent event, or None if the log is empty."""
        ev = next(self._log.events(), None)
        return ev.mtime if ev else None

    def last_state_entered_mtime(self) -> Optional[datetime.datetime]:
        """Mtime of the latest CASE_STATE_ENTERED event (the dwell anchor), or None when
        the case has not entered a state yet (brand-new). Naive/local, like all
        event-log mtimes; the caller converts to aware UTC."""
        ev = next(self._log.events(label_glob=EV_STATE_ENTERED), None)
        return ev.mtime if ev is not None else None

    def count_fails_this_dwell(self) -> int:
        """Count of failed pre-commit attempts since the current state was entered — the
        fact the `@FAIL` guard compares against. Counts BOTH CASE_TRANSITION_FAILED (the
        work raised) and CASE_TRIGGER_TIMED_OUT (the work was hard-aborted): a timeout IS a
        failed attempt, and counting it here is what stops a timing-out trigger from
        re-firing forever under the implicit `@FAIL<1` cap. STATE-scoped: every failed
        attempt in this dwell counts, regardless of which trigger raised. Derived from the
        event log (no stored counter), so it is correct across process restarts and resets
        naturally at the next CASE_STATE_ENTERED."""
        n = 0
        for ev in self._log.events(recent_first=True):
            if ev.label == EV_STATE_ENTERED:
                break
            if ev.label in (EV_TRANSITION_FAILED, EV_TRIGGER_TIMED_OUT):
                n += 1
        return n

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

    def has_event_since_enter(self, label: str) -> bool:
        """True if an event with `label` has been logged since the current state was
        entered. Used to keep blocked-state alerts to one per dwell."""
        for ev in self._log.events(recent_first=True):
            if ev.label == EV_STATE_ENTERED:
                return False
            if ev.label == label:
                return True
        return False


class CaseEventJournalView:
    """Read-only facade over a case folder's event log.

    Observers use this type; lifecycle writes go through CaseEventJournal on the owning case.
    Instances are lightweight handles — create freely via ``for_folder`` or
    ``CaseEventJournal.view()``.
    """

    def __init__(self, folder: Path) -> None:
        self._folder = Path(folder)
        self._journal = CaseEventJournal.for_folder(self._folder)

    @classmethod
    def for_folder(cls, folder: Path) -> "CaseEventJournalView":
        """Build a read-only view over a case folder's event log."""
        return cls(Path(folder))

    @property
    def primitive(self) -> PrimitiveEventLog:
        """The underlying log — escape hatch for bespoke reads the view does not model."""
        return self._journal.primitive

    @property
    def current_state(self) -> Optional[str]:
        return self._journal.current_state

    @property
    def is_terminal(self) -> bool:
        return self._journal.is_terminal

    @property
    def status(self) -> str:
        return self._journal.status

    @property
    def last_activity_at(self) -> Optional[datetime.datetime]:
        return self._journal.last_activity_at

    def last_state_entered_mtime(self) -> Optional[datetime.datetime]:
        return self._journal.last_state_entered_mtime()

    def count_fails_this_dwell(self) -> int:
        return self._journal.count_fails_this_dwell()

    @property
    def unresolved_trigger_started(self) -> Optional[PrimitiveEventProxy]:
        return self._journal.unresolved_trigger_started
