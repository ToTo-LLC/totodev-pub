# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""FolderBackedCaseReader: read-only OO façade over lock-free folder peeks."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import NamedTuple

from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.case_journal import CaseJournalView
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.helpers import _utcnow


class ActiveTrigger(NamedTuple):
    """Trigger executing its work slot (see ``case_active_trigger``)."""
    trigger: str
    elapsed_secs: float


class FolderBackedCaseReader:
    """Read-only ``CaseReadProtocol`` over a case folder — no lease, no FSM, no writes.

    Properties read from disk on access (no refresh API). ``case_assets`` memoizes
    on first use; asset file content stays live per ``CaseAssets`` call.

    ``case_dwell_secs`` and ``case_lease_secs_left`` grow/shrink between reads
    (``now()``-relative). ``case_assets`` uses ``flexible_asset_alias_loading=True``.

    Pass ``resolve_asset_types=True`` to load aliases via the asset-dataclass registry;
    otherwise ``LazyLoadedFileData`` is used.
    """

    def __init__(self, case_folder: Path, *, resolve_asset_types: bool = False) -> None:
        self._folder = Path(case_folder)
        self._resolve_asset_types = resolve_asset_types
        # Deliberate memoization of asset alias specs / CaseAssets (variation from otherwise no-caching).
        self._assets: CaseAssets | None = None
        self._asset_book: AliasedAssetSpecs | None = None
        self._events_view: CaseJournalView | None = None

    @staticmethod
    def _as_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
        """Read a naive (local) event-log mtime as aware UTC; pass None through."""
        return dt.astimezone(datetime.timezone.utc) if dt is not None else None

    @staticmethod
    def _folder_backed_case():
        from totodev_pub.folder_backed_case import FolderBackedCase

        return FolderBackedCase

    def _peek_record(self) -> CaseRecord:
        return self._folder_backed_case().peek_case_record(self._folder)

    def _peek_events(self) -> CaseJournalView:
        if self._events_view is None:
            self._events_view = self._folder_backed_case().peek_case_events(self._folder)
        return self._events_view

    def _resolve_asset_book(self) -> AliasedAssetSpecs:
        if self._asset_book is None:
            record = self._peek_record()
            self._asset_book = AliasedAssetSpecs.from_record(
                record.asset_aliases, resolve_types=self._resolve_asset_types,
            )
        return self._asset_book

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
    def case_terminal_at(self) -> datetime.datetime | None:
        return self._peek_record().terminal

    @property
    def case_terminal_state(self) -> str | None:
        return self._peek_record().terminal_state

    @property
    def case_state(self) -> str | None:
        return self._peek_events().current_state

    @property
    def case_is_terminal(self) -> bool:
        return self._peek_events().is_terminal

    @property
    def case_is_live(self) -> bool:
        return not self.case_is_terminal

    @property
    def case_last_activity(self) -> datetime.datetime | None:
        record = self._peek_record()
        return self._as_utc(self._peek_events().last_activity) or record.created

    @property
    def case_transition_fail_count(self) -> int:
        return self._peek_events().count_fails_this_dwell()

    @property
    def case_dwell_secs(self) -> float:
        record = self._peek_record()
        entered_at = self._as_utc(self._peek_events().last_state_entered_mtime()) or record.created
        return (_utcnow() - entered_at).total_seconds()

    @property
    def case_assets(self) -> CaseAssets:
        """Memoized ``CaseAssets`` from ``peek_case_assets`` (first access)."""
        if self._assets is None:
            self._assets = self._folder_backed_case().peek_case_assets(
                self._folder, resolve_asset_types=self._resolve_asset_types,
            )
        return self._assets

    def case_load_asset(self, alias: str) -> object:
        """Load alias after persisted state trust check.

        Raises ``AssetNotTrustedInStateError`` before disk I/O when not trusted.
        """
        self._resolve_asset_book().assert_trusted(alias, self.case_state)
        return self.case_assets.load_dataclass(alias)

    @property
    def case_events(self) -> CaseJournalView:
        return self._peek_events()

    @property
    def case_lease_secs_left(self) -> float | None:
        """>0 held; <0 lapsed but file present; None absent. ``case_active_trigger`` treats <=0 as not in flight."""
        return self._folder_backed_case().peek_lease_secs_left(self._folder)

    @property
    def case_active_trigger(self) -> ActiveTrigger | None:
        """Unresolved ``CASE_TRIGGER_STARTED`` with live lease, or None.

        See ``CaseJournalView.unresolved_trigger_started``. ``elapsed_secs`` grows
        between reads (wall-clock from start event mtime).
        """
        lease_left = self.case_lease_secs_left
        if lease_left is None or lease_left <= 0:
            return None
        ev = self._peek_events().unresolved_trigger_started
        if ev is None:
            return None
        started = self._as_utc(ev.mtime)
        return ActiveTrigger(ev.value, (_utcnow() - started).total_seconds())
