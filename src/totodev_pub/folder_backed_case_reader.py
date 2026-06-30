# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""FolderBackedCaseReader: read-only OO façade over lock-free folder peeks."""

from __future__ import annotations

import datetime
from pathlib import Path

from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.constants import LEASE_NAME, RECORD_NAME
from totodev_pub.folder_backed_case_support.heartbeat_lease import HeartbeatLease
from totodev_pub.folder_backed_case_support.helpers import _utcnow


class FolderBackedCaseReader:
    """Read-only view of a case folder — no lease, no FSM, no write surface.

    Works in any process without importing a concrete case class. Each property
    reads from disk when accessed (no caching, no refresh API). Multiple reads
    on the same instance may see slightly different values if the case is
    advancing underneath; hold property values or sub-objects if you need a
    snapshot-consistent view.

    ``case_dwell_secs`` and ``case_lease_secs_left`` are ``now()``-relative and
    decay between accesses. Any caching policy belongs outside this class.
    """

    def __init__(self, case_folder: Path) -> None:
        self._folder = Path(case_folder)

    @staticmethod
    def _as_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
        """Read a naive (local) event-log mtime as aware UTC; pass None through."""
        return dt.astimezone(datetime.timezone.utc) if dt is not None else None

    def _peek_record(self) -> CaseRecord:
        return CaseRecord.open(str(self._folder / RECORD_NAME), without_lock=True)

    def _peek_events(self) -> CaseEventLogReader:
        return CaseEventLogReader.for_folder(self._folder)

    @property
    def case_id(self) -> str:
        return self._peek_record().case_id

    @property
    def case_external_key(self) -> str | None:
        return self._peek_record().external_key

    @property
    def case_nickname(self) -> str | None:
        return self._peek_record().nickname

    @property
    def case_object_type(self) -> str:
        return self._peek_record().case_object_type

    @property
    def case_folder(self) -> Path:
        return self._folder

    @property
    def case_created(self) -> datetime.datetime:
        return self._peek_record().created

    @property
    def case_closed_at(self) -> datetime.datetime | None:
        return self._peek_record().closed

    @property
    def case_state(self) -> str | None:
        return self._peek_events().current_state

    @property
    def case_is_closed(self) -> bool:
        return self._peek_events().is_closed

    @property
    def case_is_open(self) -> bool:
        return not self.case_is_closed

    @property
    def case_last_activity(self) -> datetime.datetime | None:
        record = self._peek_record()
        return self._as_utc(self._peek_events().last_activity) or record.created

    @property
    def case_transition_fail_count(self) -> int:
        return self._peek_events().transition_fail_count

    @property
    def case_dwell_secs(self) -> float:
        record = self._peek_record()
        entered_at = self._as_utc(self._peek_events().last_enter_state_mtime) or record.created
        return (_utcnow() - entered_at).total_seconds()

    @property
    def case_assets(self) -> CaseAssets:
        return CaseAssets(self._folder)

    @property
    def case_events(self) -> CaseEventLogReader:
        return self._peek_events()

    @property
    def case_lease_secs_left(self) -> float | None:
        return HeartbeatLease.secs_left(self._folder / LEASE_NAME)
