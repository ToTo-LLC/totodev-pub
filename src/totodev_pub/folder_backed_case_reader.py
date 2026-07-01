# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""FolderBackedCaseReader: read-only OO façade over lock-free folder peeks."""

from __future__ import annotations

import datetime
from pathlib import Path

from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.constants import LEASE_NAME, RECORD_NAME
from totodev_pub.folder_backed_case_support.heartbeat_lease import HeartbeatLease
from totodev_pub.folder_backed_case_support.helpers import _utcnow


class FolderBackedCaseReader:
    """Read-only view of a case folder — no lease, no FSM, no write surface.

    Works in any process without importing a concrete case class. Each property
    reads from disk when accessed (no caching, no refresh API) — with ONE
    deliberate exception: ``case_assets`` and the alias trust book memoize the
    parsed asset-alias mapping (see ``case_assets`` docstring). Multiple reads on
    the same instance may see slightly different values if the case is advancing
    underneath; hold property values or sub-objects if you need a snapshot-consistent
    view.

    ``case_dwell_secs`` and ``case_lease_secs_left`` are ``now()``-relative and
    decay between accesses. Any caching policy belongs outside this class.

    By default ``case_assets`` loads declared data objects generically via
    LazyLoadedFileData — the zero-dependency story (no case class, no model classes).
    Pass ``resolve_asset_types=True`` to opt in to TYPED loading: each alias whose
    persisted loader name resolves through the asset-dataclass registry
    (``asset_dataclass_registry.register(...)`` at startup) loads as that
    FileMappedPydanticMixin subclass; any unresolved name (or the "Callable" sentinel)
    falls back to LazyLoadedFileData for that alias.
    """

    def __init__(self, case_folder: Path, *, resolve_asset_types: bool = False) -> None:
        self._folder = Path(case_folder)
        self._resolve_asset_types = resolve_asset_types
        self._assets: CaseAssets | None = None
        self._asset_book: AliasedAssetSpecs | None = None

    @staticmethod
    def _as_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
        """Read a naive (local) event-log mtime as aware UTC; pass None through."""
        return dt.astimezone(datetime.timezone.utc) if dt is not None else None

    def _peek_record(self) -> CaseRecord:
        return CaseRecord.open(str(self._folder / RECORD_NAME), without_lock=True)

    def _peek_events(self) -> CaseEventLogReader:
        return CaseEventLogReader.for_folder(self._folder)

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
        """A CaseAssets view of the case folder's declared data objects.

        Unlike the other properties, this is memoized: the case record is opened
        (and its near-immutable ``asset_aliases`` parsed into specs) only on the
        FIRST access, and the resulting CaseAssets is reused on every later
        access — so nothing is loaded unless ``case_assets`` is actually used, and
        repeated access does not re-read or re-parse the record. The alias mapping
        is safe to cache because it mirrors the class-level asset_aliases and never
        changes over a case's life. Asset FILES are still read live by CaseAssets
        methods, so asset CONTENT remains a fresh, point-in-time view.

        Note: typed resolution (``resolve_asset_types=True``) is bound when the
        mapping is first cached; register asset dataclasses before first access.
        """
        if self._assets is None:
            self._assets = CaseAssets(
                self._folder,
                asset_specs=self._resolve_asset_book().spec_map(),
                flexible_dataclass_loading=True,
            )
        return self._assets

    def case_load_dataclass(self, alias: str) -> object:
        """Load a declared asset alias after checking persisted state validity.
        Raises AssetNotTrustedInStateError before disk I/O when not trusted."""
        self._resolve_asset_book().assert_trusted(alias, self.case_state)
        return self.case_assets.load_dataclass(alias)

    @property
    def case_events(self) -> CaseEventLogReader:
        return self._peek_events()

    @property
    def case_lease_secs_left(self) -> float | None:
        """Lock-free lease-time read for this case folder.

        Return-value semantics: see `HeartbeatLease.secs_left`."""
        return HeartbeatLease.secs_left(self._folder / LEASE_NAME)
