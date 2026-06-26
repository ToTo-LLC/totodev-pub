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
    EV_ENTER_STATE,
    EV_CLOSED,
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
        """The underlying log — the escape hatch for everything bespoke, INCLUDING
        writes (e.g. the live case does `reader.primitive.create_event(...)`)."""
        return self._log

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
