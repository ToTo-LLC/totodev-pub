# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub


"""
PrimitiveStateMachineLog: Minimal state tracking on top of PrimitiveEventLog.

This module provides a single convenience class that layers basic state-machine
semantics on top of `PrimitiveEventLog`. It assumes a pair of log labels (entry
and error) and interprets them as transitions without writing any additional
metadata files.

Use this when you want a quick, file-backed record of "what state did we enter,
and when did we encounter errors while there?" It is intentionally small and
meant as a stepping stone for richer implementations.

Key Ideas:
- Append-only events: Uses `PrimitiveEventLog` for durability and ordering.
- Minimal mode only: Tracks a single linear progression of states via enter events.
- Error association: Errors are reported separately but tied back to their entry.
- Inspectable history: Read state transitions and payloads directly from event files.

Quick Start:
    ```python
    from pathlib import Path
    from totodev_pub.primitive_event_log import PrimitiveEventLog
    from totodev_pub.cached_file_folders_support.primitive_state_machine_log import (
        PrimitiveStateMachineLog,
    )

    log = PrimitiveEventLog(event_dir=Path("./events"))
    sm = PrimitiveStateMachineLog(log, init_state="READY")

    sm.log_enter("PROCESSING", {"step": 1})
    sm.log_error({"message": "Transient failure"})
    sm.log_enter("DONE")

    print(sm.cur_state().value)          # "DONE"
    print([evt.value for evt in sm.state_history()])  # ["READY", "PROCESSING", "DONE"]
    ```

When To Use:
- Demonstrating how `PrimitiveEventLog` can support state-style workflows.
- Managing small jobs where chronological inspection matters more than speed.
- Prototyping before investing in a full-featured state machine or database.

Limitations:
- No exit events: transitions are inferred solely from enter events.
- No automatic cleanup or compaction; the underlying log grows over time.
- For large logs or frequent queries, scanning events on each call may be slow.
"""

from collections import deque
from typing import List, Mapping, Optional

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.primitive_event_log import PrimitiveEventLog, PrimitiveEventProxy


DEFAULT_ENTER_LABEL = "ENTER_STATE"
DEFAULT_ERROR_LABEL = "ERROR_AT_STATE"


class PrimitiveStateMachineLog:
    """
    Lightweight adapter that interprets `PrimitiveEventLog` entries as a single
    linear state machine.

    Entry events record the current state, while error events capture incidents
    that occur while the state is active. The most recent entry value is treated
    as the "current" state, and the previous entry represents the prior state.
    """

    def __init__(
        self,
        event_log: PrimitiveEventLog,
        init_state: Optional[str] = None,
        entry_label: str = DEFAULT_ENTER_LABEL,
        error_label: str = DEFAULT_ERROR_LABEL,
    ):
        """Attach to an event log. Optionally seed an initial state."""
        self.event_log = event_log
        self._entry_label = entry_label
        self._error_label = error_label
        if init_state and self.cur_state() is None:
            self.event_log.create_event(self._entry_label, init_state)

    def log_enter(
        self,
        new_state: str,
        payload: Optional[FileMappedPydanticMixin | Mapping | dict] = None
    ) -> None:
        """Log entering a state, optionally recording a payload."""
        self.event_log.create_event(self._entry_label, new_state, payload)


    def log_error(
        self,
        payload: Optional[FileMappedPydanticMixin | Mapping | dict] = None
    ):
        """Logs that the current state has an error.
        Ambigious entry is created if cur_state() is None.
        """
        current_state = self.cur_state()
        state_value = current_state.value if current_state else ""
        self.event_log.create_event(self._error_label, state_value, payload)


    def cur_state(self) -> Optional[PrimitiveEventProxy]:
        """
        Current state of the state machine.

        Returns:
            The most recent ENTER_STATE value, or None if no state has been entered.
        """
        return next(self.event_log.events(label_glob=self._entry_label), None)

    def cur_state_errors(self) -> List[PrimitiveEventProxy]:
        """Return error events associated with the current state."""
        history = self.transition_history(depth=1)
        if not history:
            return []

        current_segment = history[-1]
        if not current_segment:
            return []

        current_entry = current_segment[0]
        return [
            event
            for event in current_segment[1:]
            if event.label == self._error_label
            and event.value == current_entry.value
        ]

    def transition_history(
        self,
        depth: int = 1,
    ) -> List[List[PrimitiveEventProxy]]:
        """
        Partition the event history into segments grouped by entry events.

        Args:
            depth: Number of most recent entry segments to return. Use 0 for all history.

        Returns:
            List of segments, each being a list of events starting with an entry event
            followed by any events that occurred before the next entry event.
        """
        if depth < 0:
            raise ValueError("depth must be >= 0")

        segment_iter = self.event_log.segment_events(self._entry_label)

        if depth == 0:
            return [list(segment) for segment in segment_iter]

        window: deque[tuple[PrimitiveEventProxy, ...]] = deque(maxlen=depth)
        for segment in segment_iter:
            window.append(segment)

        return [list(segment) for segment in window]

    def prior_state(self) -> Optional[PrimitiveEventProxy]:
        """
        Prior state of the state machine.

        Returns:
            The second most recent ENTER_STATE value, inferred from history.
        """
        enter_iter = self.event_log.events(label_glob=self._entry_label)
        latest_enter = next(enter_iter, None)
        second_enter = next(enter_iter, None)  # prior state (if any)
        if latest_enter is None or second_enter is None:
            return None
        return second_enter

    @property
    def entry_label(self) -> str:
        return self._entry_label

    @property
    def error_label(self) -> str:
        return self._error_label