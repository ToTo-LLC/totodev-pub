# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Durable storage of cases, addressed by ``case_id`` and organized by status.

A ``CaseStore`` owns two things together — **where a case's folder is** and
**what pool-activity-status it has** — because location is the projection of
status. There is no separate status ledger that could disagree with the tree,
and no write-ahead log, because the filesystem's atomic rename already supplies
the crash-consistency.

Vocabulary boundary
-------------------
The store speaks ``case_id``, status, and local ``Path``. **No cache type, no
``ref_path``, no ``grouping_key``, no slave directory, and no placeholder concept
crosses this boundary in either direction.** Everything on the far side is
storage mechanics; everything on this side is the manager's domain.

Status is not the case's own FSM state
--------------------------------------
``case_record.yaml`` owns the case's own status and stays authoritative for it.
Pool-activity-status is the *manager's* view, and the two are deliberately
different fields. They agree in almost every situation, but disagreement is legal
and is something the manager reports rather than silently repairs.

**Only the configured live status (default ``live``) means the pool is actively
driving the case.** Every other status name is frozen storage and shares the same
datetime-encoded layout. That makes the field fail-safe by construction: an
unrecognized value is not live, so it is not driven.

Layout
------
Groupings are status names (``{status}/``). Live cases use a stable
``{case_id}.yaml`` ref. Non-live cases use
``YYYY-MM/YYYY-MM-DD/HHMM_{case_id}.yaml`` (naive local), frozen from
``last_activity_at`` at first arrival into that status.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.constants import PLACEHOLDER_HEADER
from totodev_pub.case_manager_support.exceptions import (
    CaseNotInStoreError,
)
from totodev_pub.case_manager_support.layout import (
    assert_case_folder_movable,
    read_case_id_from_folder,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME

logger = logging.getLogger(__name__)

# Pool-activity-status vocabulary the manager uses. The store treats only the
# configured live status as special; every other string shares non-live layout.
LIVE = "live"
TERMINATED = "terminated"
QUARANTINED = "quarantined"

_TIMED_REF_RE = re.compile(
    r"^(?P<ym>\d{4}-\d{2})/(?P<ymd>\d{4}-\d{2}-\d{2})/(?P<hhmm>\d{4})_(?P<case_id>.+)\.yaml$"
)


@dataclass(frozen=True)
class CaseEntry:
    """One case's storage identity, **as of the moment it was read**.

    A status change moves the folder, so any entry held across one is stale.
    Re-resolve rather than reusing ``case_folder`` — the store's iterators are
    point-in-time snapshots for the same reason.
    """

    case_id: str
    status: str
    case_folder: Path
    activity_at: datetime | None = None


class LocalCaseStore:
    """``CaseManager``'s durable filespace: case folders addressed by ``case_id``.

    Owns the on-disk layout the manager drives — create, locate, relocate by
    pool-activity-status, and iterate. Location *is* status (see module
    docstring); the manager never keeps a separate status ledger. Bound to a
    local root and a ``CaseManagerPolicy`` that defines the live status name and
    grouping pattern; not a general filesystem API.
    """

    def __init__(self, root_dir: str | Path, policy: CaseManagerPolicy) -> None:
        self._root = Path(root_dir).resolve()
        self._policy = policy
        self._live_status = policy.live_bucket
        self._cache = CachedFileFolders(policy.grouping_pattern, str(self._root))

    @classmethod
    def provision(cls, root_dir: str | Path, policy: CaseManagerPolicy) -> "LocalCaseStore":
        """Create the storage tree, or attach to a compatible existing one.

        Idempotent with validation: re-provisioning the same layout is a no-op,
        while a different grouping pattern over the same root raises from the
        cache's own manifest check. Layout facts the cache manifest does not
        carry are validated against the persisted policy file by
        ``CaseManager.open_local_store()``.
        """
        root = Path(root_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        store = cls(root, policy)
        # The live bucket must exist before the first scan, or startup recovery
        # reads a missing directory rather than an empty fleet.
        store._live_bucket_dir().mkdir(parents=True, exist_ok=True)
        return store

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def root_dir(self) -> Path:
        return self._root

    @property
    def policy(self) -> CaseManagerPolicy:
        """The policy this store's layout was built from.

        Carrying it here is what lets a store stand alone as "the filespace,
        resolved": root plus policy is enough to derive every other path a
        manager needs.
        """
        return self._policy

    @property
    def live_status(self) -> str:
        return self._live_status

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create_location(self, case_id: str, *, status: str = LIVE) -> Path:
        """Reserve a folder for ``case_id`` at ``status`` and return it.

        The folder comes back empty unless one already existed at that status, in
        which case that one is returned untouched — this is how a re-driven
        relocation converges instead of failing on its second attempt.
        """
        status = self._require_status(status)
        existing = self._cached_ref(case_id, status)
        if existing is not None:
            return existing.slave_dir_path
        ref_path = self._ref_for_create(case_id, status, folder=None)
        return await self._register(case_id, self._grouping_for(status), ref_path)

    async def set_status(
        self,
        case_id: str,
        new_status: str,
        *,
        force_despite_lease: bool = False,
    ) -> Path:
        """Move ``case_id`` to ``new_status`` and return its new folder.

        Refuses — it does not defer — while the case's heartbeat lease is held.
        Waiting out a lease is the manager's timing protocol, not the store's.

        Idempotent: a case already at the requested status returns its current
        folder without rewriting a timed path when activity has drifted.

        Args:
            force_despite_lease: operator escape hatch for the one case waiting
                cannot fix — a process that keeps renewing the lease while
                failing every interaction. Moving a folder out from under a
                process that believes it owns the case is a split-brain, so this
                stays out of every automatic path and its use is logged.
        """
        new_status = self._require_status(new_status)
        entry = self.find(case_id)
        if entry is None:
            raise CaseNotInStoreError(case_id)
        if entry.status == new_status:
            return entry.case_folder

        if force_despite_lease:
            logger.warning(
                "Relocating case %s to %s while its lease may still be held "
                "(force_despite_lease); open handles in a live owner will follow "
                "the old inode.",
                case_id,
                new_status,
            )
        else:
            assert_case_folder_movable(
                entry.case_folder, case_id, f"set status to {new_status}"
            )

        old_ref = self._ref_path_of(entry)
        new_ref = self._ref_for_create(case_id, new_status, folder=entry.case_folder)
        src_grouping = self._grouping_for(entry.status)
        dst_grouping = self._grouping_for(new_status)
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._cache.move_file(
                old_ref,
                new_ref,
                grouping_key=src_grouping,
                new_grouping_key=dst_grouping,
                overwrite=True,
            ),
        )
        moved = self._cached_ref(case_id, new_status)
        if moved is None:  # pragma: no cover - move_file raises rather than no-op
            raise CaseNotInStoreError(case_id)
        return moved.slave_dir_path

    async def absorb_orphan(
        self, case_id: str, source_folder: Path, *, status: str
    ) -> Path:
        """Take a case folder the store has no entry for and record it at ``status``.

        This is the rescue path for a folder that exists on disk but is unknown
        to the index — the residue of a relocation that died between its two
        halves. Copying rather than moving is deliberate: the source may be the
        destination already (a re-driven rescue), and leaving it in place costs
        nothing that the next pass will not clean up.
        """
        status = self._require_status(status)
        # Prefer activity from the source folder before registering an empty dest.
        if source_folder.exists() and (Path(source_folder) / RECORD_NAME).exists():
            ref_path = self._ref_for_create(case_id, status, folder=Path(source_folder))
            existing = self._cached_ref(case_id, status)
            if existing is None:
                await self._register(case_id, self._grouping_for(status), ref_path)
            dest = self._cached_ref(case_id, status)
            assert dest is not None
            dest_folder = dest.slave_dir_path
        else:
            dest_folder = await self.create_location(case_id, status=status)
        if source_folder.exists() and not source_folder.samefile(dest_folder):
            shutil.copytree(source_folder, dest_folder, dirs_exist_ok=True)
        return dest_folder

    async def export(
        self, case_id: str, export_to: Path | None, *, force_despite_lease: bool = False
    ) -> Path | None:
        """Remove ``case_id`` from managed storage.

        ``export_to`` is a destination path, not a status: afterwards ``find()``
        reports the case as absent. Pass ``None`` to destroy the folder instead
        of keeping it.
        """
        entry = self.find(case_id)
        if entry is None:
            raise CaseNotInStoreError(case_id)
        if not force_despite_lease:
            assert_case_folder_movable(entry.case_folder, case_id, "export")

        ref_path = self._ref_path_of(entry)
        grouping = self._grouping_for(entry.status)
        if export_to is None:
            await self._cache.delete_file(ref_path, grouping)
            return None

        export_to = Path(export_to)
        export_to.parent.mkdir(parents=True, exist_ok=True)
        if export_to.exists():
            shutil.rmtree(export_to)
        source = entry.case_folder
        try:
            shutil.move(str(source), str(export_to))
            # Renaming the folder away leaves the cache holding an entry whose
            # storage is gone, and its deletion path cannot describe that state.
            # Put an empty directory back so the entry can be retired normally;
            # the alternative is copying the whole case rather than renaming it.
            source.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Cross-device move; copy and let the entry deletion remove the source.
            shutil.copytree(source, export_to)
        await self._cache.delete_file(ref_path, grouping)
        return export_to

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find(self, case_id: str) -> CaseEntry | None:
        """Locate a case at any status. Index lookups only — no record reads for live.

        A live case costs exactly one index lookup: the ref path is derived from
        the ``case_id``. A non-live case is found by timed-ref glob under each
        non-live status grouping.
        """
        live = self._cached_ref(case_id, self._live_status)
        if live is not None:
            return CaseEntry(case_id, self._live_status, live.slave_dir_path, None)
        for status in self.populated_statuses():
            if status == self._live_status:
                continue
            ref = self._cached_ref(case_id, status)
            if ref is not None:
                return CaseEntry(
                    case_id,
                    status,
                    ref.slave_dir_path,
                    self._parse_activity_from_ref(ref.ref_path),
                )
        return None

    async def resolve_path(self, case_id: str) -> Path | None:
        """The case's folder, materialized.

        Async because materializing a non-live case is where a future non-local
        store would fetch it from wherever it was tiered off to. For this store
        it is exactly ``find().case_folder`` and costs nothing; the point is that
        callers on the write paths already await it.
        """
        entry = self.find(case_id)
        return None if entry is None else entry.case_folder

    def contains(self, case_id: str) -> bool:
        """Whether the store holds this case at *any* status.

        The duplicate check adopt needs: a case_id already archived is still
        taken, and admitting a second one would give two cases one identity."""
        return self.find(case_id) is not None

    def case_id_at(self, path: Path) -> str | None:
        """Reverse addressing: the case whose folder contains ``path``, if any.

        Reads one ``case_record.yaml`` and confirms the answer against the index,
        so a path pointing at an unmanaged folder that happens to hold a case
        record reports nothing.
        """
        folder = self._case_folder_containing(Path(path))
        if folder is None:
            return None
        case_id = read_case_id_from_folder(folder)
        if case_id is None:
            return None
        entry = self.find(case_id)
        if entry is None or entry.case_folder.resolve() != folder.resolve():
            return None
        return case_id

    def owns_path(self, path: Path) -> bool:
        """True when ``path`` lies inside a status bucket this store manages.

        Callers that take a folder from outside — adopt, above all — need to know
        whether they were handed something the store is already responsible for,
        without learning what a bucket is.
        """
        try:
            relative = Path(path).resolve().relative_to(self._root)
        except ValueError:
            return False
        if not relative.parts:
            return False
        top = relative.parts[0]
        if top == self._policy.manager_namespace or top.startswith("."):
            return False
        return True

    def status_of(self, case_id: str) -> str | None:
        """This case's pool-activity-status, or None if the store has no entry.

        Remember the fail-safe rule: only the configured live status means the
        pool is driving it. Anything else means it is not."""
        entry = self.find(case_id)
        return None if entry is None else entry.status

    def populated_statuses(self) -> list[str]:
        """Status groupings that exist on disk (including live when present)."""
        found: list[str] = []
        for grouping in self._cache.groupings():
            key = tuple(grouping.grouping_key or ())
            if key and key[0] not in found:
                found.append(key[0])
        return found

    def iter_by_status(
        self,
        status: str,
        *,
        reverse: bool = False,
        after: datetime | date | None = None,
        before: datetime | date | None = None,
    ) -> Iterator[CaseEntry]:
        """Snapshot of every case at ``status``.

        Default order is oldest-first. Live sorts by ``case_id``; non-live by
        timed ref path. ``after`` / ``before`` filter on activity time (peeked
        for live; path-encoded for non-live, with peek fallback).
        """
        status = self._require_status(status)
        after_dt, before_dt = self._normalize_bounds(after, before)
        grouping = self._grouping_for(status)
        if not self._cache_grouping_exists(grouping):
            return
        if self._is_live(status):
            yield from self._iter_live(grouping, reverse=reverse, after=after_dt, before=before_dt)
        else:
            yield from self._iter_non_live(
                status, grouping, reverse=reverse, after=after_dt, before=before_dt
            )

    def iter_all(
        self,
        *,
        reverse: bool = False,
        after: datetime | date | None = None,
        before: datetime | date | None = None,
    ) -> Iterator[CaseEntry]:
        """Snapshot of every case the store holds, at every status."""
        for status in self.populated_statuses():
            yield from self.iter_by_status(
                status, reverse=reverse, after=after, before=before
            )

    # ------------------------------------------------------------------
    # Storage mechanics — nothing below this line is part of the boundary
    # ------------------------------------------------------------------

    def _is_live(self, status: str) -> bool:
        return status == self._live_status

    def _require_status(self, status: str) -> str:
        if not status or not str(status).strip():
            raise ValueError("status must be a non-empty string")
        return str(status)

    def _live_ref(self, case_id: str) -> str:
        return f"{case_id}.yaml"

    def _timed_ref(self, when: datetime, case_id: str) -> str:
        local = self._to_naive_local(when)
        return (
            f"{local:%Y-%m}/{local:%Y-%m-%d}/"
            f"{local:%H%M}_{case_id}.yaml"
        )

    def _ref_for_create(
        self, case_id: str, status: str, *, folder: Path | None
    ) -> str:
        if self._is_live(status):
            return self._live_ref(case_id)
        if folder is not None and folder.exists():
            when = self._activity_at(folder)
        else:
            when = datetime.now()
        return self._timed_ref(when, case_id)

    def _ref_path_of(self, entry: CaseEntry) -> str:
        ref = self._cached_ref(entry.case_id, entry.status)
        if ref is None:  # pragma: no cover
            raise CaseNotInStoreError(entry.case_id)
        return ref.ref_path

    def _grouping_for(self, status: str) -> tuple[str, ...]:
        return (status,)

    def _live_bucket_dir(self) -> Path:
        return self._root / self._live_status

    def _cache_grouping_exists(self, grouping: tuple[str, ...]) -> bool:
        return (self._root / grouping[0]).is_dir()

    def _cached_ref(self, case_id: str, status: str):
        grouping = self._grouping_for(status)
        if not self._cache_grouping_exists(grouping):
            return None
        if self._is_live(status):
            return self._cache.find_file(self._live_ref(case_id), grouping)
        matches = list(
            self._cache.files(
                grouping, ref_path_glob=f"*/*/*_{case_id}.yaml"
            )
        )
        return matches[0] if matches else None

    def _activity_at(self, folder: Path) -> datetime:
        """Resolve freeze/filter time: last_activity_at → created → local now."""
        try:
            journal = FolderBackedCase.peek_case_event_journal(folder)
            if journal.last_activity_at is not None:
                return self._to_naive_local(journal.last_activity_at)
        except Exception:
            logger.debug("peek_case_event_journal failed for %s", folder, exc_info=True)
        try:
            record = FolderBackedCase.peek_case_record(folder)
            if record.created is not None:
                return self._to_naive_local(record.created)
        except Exception:
            logger.debug("peek_case_record failed for %s", folder, exc_info=True)
        return datetime.now()

    @staticmethod
    def _to_naive_local(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(microsecond=0)
        return value.astimezone().replace(tzinfo=None, microsecond=0)

    def _parse_activity_from_ref(self, ref_path: str) -> datetime | None:
        match = _TIMED_REF_RE.match(ref_path.replace("\\", "/"))
        if match is None:
            return None
        ymd = match.group("ymd")
        hhmm = match.group("hhmm")
        return datetime(
            int(ymd[0:4]),
            int(ymd[5:7]),
            int(ymd[8:10]),
            int(hhmm[0:2]),
            int(hhmm[2:4]),
        )

    def _normalize_bounds(
        self,
        after: datetime | date | None,
        before: datetime | date | None,
    ) -> tuple[datetime | None, datetime | None]:
        after_dt = self._bound_to_datetime(after, end_of_day=False)
        before_dt = self._bound_to_datetime(before, end_of_day=True)
        if after_dt is not None and before_dt is not None and after_dt > before_dt:
            raise ValueError(
                f"after ({after_dt!r}) must be <= before ({before_dt!r})"
            )
        return after_dt, before_dt

    def _bound_to_datetime(
        self, value: datetime | date | None, *, end_of_day: bool
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return self._to_naive_local(value)
        if end_of_day:
            return datetime(value.year, value.month, value.day, 23, 59, 59)
        return datetime(value.year, value.month, value.day, 0, 0, 0)

    def _activity_in_bounds(
        self,
        activity: datetime | None,
        after: datetime | None,
        before: datetime | None,
    ) -> bool:
        if after is None and before is None:
            return True
        if activity is None:
            return False
        t = self._to_naive_local(activity)
        if after is not None and t < after:
            return False
        if before is not None and t > before:
            return False
        return True

    def _non_live_glob(
        self, after: datetime | None, before: datetime | None
    ) -> str:
        """Tighten ref_path_glob when both bounds share year / month / day."""
        if after is not None and before is not None:
            if (
                after.year == before.year
                and after.month == before.month
                and after.day == before.day
            ):
                return f"{after:%Y-%m}/{after:%Y-%m-%d}/*.yaml"
            if after.year == before.year and after.month == before.month:
                return f"{after:%Y-%m}/*/*.yaml"
            if after.year == before.year:
                return f"{after:%Y}-*/*/*.yaml"
        return "*/*/*.yaml"

    def _iter_live(
        self,
        grouping: tuple[str, ...],
        *,
        reverse: bool,
        after: datetime | None,
        before: datetime | None,
    ) -> Iterator[CaseEntry]:
        for ref in self._cache.files(grouping, reverse=reverse):
            folder = ref.slave_dir_path
            if not folder.exists():
                continue
            case_id = self._case_id_from_live_ref(ref.ref_path) or read_case_id_from_folder(
                folder
            )
            if case_id is None:
                continue
            activity = None
            if after is not None or before is not None:
                activity = self._activity_at(folder)
                if not self._activity_in_bounds(activity, after, before):
                    continue
            yield CaseEntry(case_id, self._live_status, folder, activity)

    def _iter_non_live(
        self,
        status: str,
        grouping: tuple[str, ...],
        *,
        reverse: bool,
        after: datetime | None,
        before: datetime | None,
    ) -> Iterator[CaseEntry]:
        glob = self._non_live_glob(after, before)
        for ref in self._cache.files(grouping, reverse=reverse, ref_path_glob=glob):
            folder = ref.slave_dir_path
            if not folder.exists():
                continue
            match = _TIMED_REF_RE.match(ref.ref_path.replace("\\", "/"))
            case_id = (
                match.group("case_id")
                if match
                else read_case_id_from_folder(folder)
            )
            if case_id is None:
                continue
            activity = self._parse_activity_from_ref(ref.ref_path)
            if activity is None:
                activity = self._activity_at(folder)
            if not self._activity_in_bounds(activity, after, before):
                continue
            yield CaseEntry(case_id, status, folder, activity)

    def _case_id_from_live_ref(self, ref_path: str) -> str | None:
        name = Path(ref_path).name
        if name.endswith(".yaml"):
            return name[: -len(".yaml")]
        return None

    def _case_folder_containing(self, path: Path) -> Path | None:
        resolved = path.resolve()
        for candidate in (resolved, *resolved.parents):
            if (candidate / RECORD_NAME).exists():
                return candidate
            if candidate == self._root:
                break
        return None

    async def _register(
        self, case_id: str, grouping: tuple[str, ...], ref_path: str
    ) -> Path:
        """Create the cache entry for a case and return its folder.

        ``upsert_file`` is the cache's only entry-creating call, which is what
        makes every write path here a coroutine. Writing the placeholder by path
        arithmetic instead would leave no index row, and the folder would be
        invisible to every lookup that follows.
        """
        from totodev_pub.cached_file_folders_support.file_proxy_local_file import (
            LocalFileProxy,
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(PLACEHOLDER_HEADER)
            tmp_path = tmp.name
        try:
            await self._cache.upsert_file(
                LocalFileProxy(tmp_path, ref_path=ref_path, delete_after_deploy=True),
                grouping,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return self._cache.get_slave_dir(grouping, ref_path)
