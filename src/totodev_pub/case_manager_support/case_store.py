# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Durable storage of cases, addressed by ``case_id`` and organized by status.

A ``CaseStore`` owns two things together — **where a case's folder is** and
**what pool-activity-status it has** — because location is the projection of
status. A case sitting in the ``terminal_2026-08`` bucket *is* terminated; there
is no separate status record that could disagree with it, and no write-ahead log,
because the filesystem's atomic rename already supplies the crash-consistency.

Vocabulary boundary
-------------------
The store speaks ``case_id``, status, and local ``Path``. **No cache type, no
``ref_path``, no ``grouping_key``, no slave directory, and no placeholder concept
crosses this boundary in either direction.** Everything on the far side is
storage mechanics; everything on this side is the manager's domain. That rule is
what makes this a boundary rather than a pass-through, and it is worth checking
against on every change to this file.

Status is not the case's own FSM state
--------------------------------------
``case_record.yaml`` owns the case's own status and stays authoritative for it.
Pool-activity-status is the *manager's* view, and the two are deliberately
different fields. They agree in almost every situation, but disagreement is legal
and is something the manager reports rather than silently repairs.

**Only ``live`` means the pool is actively driving the case.** Every other value
means it is not — *including a value this version does not recognize*. That makes
the field fail-safe by construction rather than by accident.

Locality
--------
Case identity here is a local filesystem path, by decision. A networked or
checkout/checkin arrangement is handled outside this class by whatever
materializes local paths; ``case_id``-plus-server addressing is not this class's
concern. The one concession to a future non-local store is that
``resolve_path()`` is async, so adding remote materialization later does not
churn its callers.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.constants import PLACEHOLDER_HEADER
from totodev_pub.case_manager_support.exceptions import (
    CaseNotInStoreError,
    UnknownCaseStatusError,
)
from totodev_pub.case_manager_support.layout import (
    assert_case_folder_movable,
    read_case_id_from_folder,
)
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME

logger = logging.getLogger(__name__)

# Pool-activity-status. Predefined meanings in an otherwise open vocabulary; see
# the module docstring for the fail-safe rule that governs unrecognized values.
LIVE = "live"
TERMINATED = "terminated"
QUARANTINED = "quarantined"

WRITABLE_STATUSES = (LIVE, TERMINATED, QUARANTINED)


@dataclass(frozen=True)
class CaseEntry:
    """One case's storage identity, **as of the moment it was read**.

    A status change moves the folder, so any entry held across one is stale.
    Re-resolve rather than reusing ``case_folder`` — the store's iterators are
    point-in-time snapshots for the same reason.
    """

    case_id: str
    status: str
    partition: str | None
    case_folder: Path


class LocalCaseStore:
    """A case store backed by a local ``CachedFileFolders`` tree.

    ``local`` is load-bearing in the name: it front-loads the distinction a
    non-local variant would need, and it is the only implementation there is.
    No abstract base class exists, deliberately — one would be justified by a
    second implementation nobody has written. Extracting it later is mechanical.
    """

    def __init__(self, root_dir: str | Path, policy: CaseManagerPolicy) -> None:
        self._root = Path(root_dir).resolve()
        self._policy = policy
        self._cache = CachedFileFolders(policy.grouping_pattern, str(self._root))

    @classmethod
    def provision(cls, root_dir: str | Path, policy: CaseManagerPolicy) -> "LocalCaseStore":
        """Create the storage tree, or attach to a compatible existing one.

        Idempotent with validation: re-provisioning the same layout is a no-op,
        while a different grouping pattern over the same root raises from the
        cache's own manifest check. The layout facts the cache manifest does not
        carry — bucket names and the ref-path template — are validated against
        the persisted policy file by ``CaseManager.open_local_store()``, so they
        are not re-recorded here.
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

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create_location(
        self, case_id: str, *, status: str = LIVE, partition: str | None = None
    ) -> Path:
        """Reserve a folder for ``case_id`` at ``status`` and return it.

        The folder comes back empty unless one already existed, in which case
        that one is returned untouched — this is how a re-driven relocation
        converges instead of failing on its second attempt.
        """
        grouping = self._grouping_for(status, partition)
        existing = self._cache.find_file(self._ref_path(case_id), grouping)
        if existing is not None:
            return existing.slave_dir_path
        return await self._register(case_id, grouping)

    async def set_status(
        self,
        case_id: str,
        new_status: str,
        *,
        partition: str | None = None,
        force_despite_lease: bool = False,
    ) -> Path:
        """Move ``case_id`` to ``new_status`` and return its new folder.

        Refuses — it does not defer — while the case's heartbeat lease is held.
        Waiting out a lease is the manager's timing protocol, not the store's;
        carrying a "status set, move pending" limbo state here would mean a
        reconciliation pass inside a storage object and a second place where
        status and location can disagree.

        Idempotent: a case already at the requested status returns its current
        folder without touching the filesystem.

        Args:
            partition: required for ``terminated``, which is partitioned by
                archive label. Ignored otherwise.
            force_despite_lease: operator escape hatch for the one case waiting
                cannot fix — a process that keeps renewing the lease while
                failing every interaction. Moving a folder out from under a
                process that believes it owns the case is a split-brain, so this
                stays out of every automatic path and its use is logged.
        """
        entry = self.find(case_id)
        if entry is None:
            raise CaseNotInStoreError(case_id)
        dst_grouping = self._grouping_for(new_status, partition)
        if (entry.status, entry.partition) == (new_status, partition):
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

        ref_path = self._ref_path(case_id)
        src_grouping = self._grouping_of(entry)
        # Relocation is filesystem work of unbounded size — a case folder can
        # hold gigabytes of assets, and a synchronous copy here would stall the
        # manager's whole tick.
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._cache.move_file(
                ref_path,
                ref_path,
                grouping_key=src_grouping,
                new_grouping_key=dst_grouping,
                overwrite=True,
            ),
        )
        moved = self._cache.find_file(ref_path, dst_grouping)
        if moved is None:  # pragma: no cover - move_file raises rather than no-op
            raise CaseNotInStoreError(case_id)
        return moved.slave_dir_path

    async def absorb_orphan(
        self, case_id: str, source_folder: Path, *, status: str, partition: str | None = None
    ) -> Path:
        """Take a case folder the store has no entry for and record it at ``status``.

        This is the rescue path for a folder that exists on disk but is unknown
        to the index — the residue of a relocation that died between its two
        halves. Copying rather than moving is deliberate: the source may be the
        destination already (a re-driven rescue), and leaving it in place costs
        nothing that the next pass will not clean up.
        """
        dest = await self.create_location(case_id, status=status, partition=partition)
        if source_folder.exists() and not source_folder.samefile(dest):
            shutil.copytree(source_folder, dest, dirs_exist_ok=True)
        return dest

    async def export(
        self, case_id: str, export_to: Path, *, force_despite_lease: bool = False
    ) -> Path:
        """Remove ``case_id`` from managed storage, leaving its folder at ``export_to``.

        The destination is deliberately a path rather than a status: an exported
        case has left the store, and afterwards ``find()`` reports it as absent.
        """
        entry = self.find(case_id)
        if entry is None:
            raise CaseNotInStoreError(case_id)
        if not force_despite_lease:
            assert_case_folder_movable(entry.case_folder, case_id, "export")

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
        await self._cache.delete_file(self._ref_path(case_id), self._grouping_of(entry))
        return export_to

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find(self, case_id: str) -> CaseEntry | None:
        """Locate a case at any status. Index lookups only — no record reads.

        A live case costs exactly one index lookup: the ref path is derived from
        the ``case_id``, so the case's own bucket answers directly and the fleet
        is never scanned. This is the hot path — every addressed ``fire()`` goes
        through it.

        A non-live case costs one directory glob to enumerate the archive
        partitions plus one index lookup each. That is the deliberate asymmetry:
        live cases are bounded by the concurrency ceiling, while terminal buckets
        grow without bound, so only the live side is worth optimizing for.
        """
        ref_path = self._ref_path(case_id)
        live_ref = self._cache.find_file(ref_path, (self._policy.live_bucket,))
        if live_ref is not None:
            return CaseEntry(case_id, LIVE, None, live_ref.slave_dir_path)
        for grouping in self._existing_groupings(skip_live=True):
            ref = self._cache.find_file(ref_path, grouping)
            if ref is not None:
                status, partition = self._status_for_bucket(grouping[0])
                return CaseEntry(case_id, status, partition, ref.slave_dir_path)
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
        """True when ``path`` lies inside a bucket this store manages.

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
        status, _partition = self._status_for_bucket(relative.parts[0])
        return status in WRITABLE_STATUSES

    def status_of(self, case_id: str) -> str | None:
        """This case's pool-activity-status, or None if the store has no entry.

        Remember the fail-safe rule: only ``live`` means the pool is driving it.
        Anything else — including a value this version does not recognize —
        means it is not."""
        entry = self.find(case_id)
        return None if entry is None else entry.status

    def iter_by_status(self, status: str) -> Iterator[CaseEntry]:
        """Snapshot of every case at ``status``, spanning all its partitions."""
        for grouping in self._existing_groupings():
            bucket_status, partition = self._status_for_bucket(grouping[0])
            if bucket_status != status:
                continue
            yield from self._entries_in(grouping, bucket_status, partition)

    def iter_all(self) -> Iterator[CaseEntry]:
        """Snapshot of every case the store holds, at every status."""
        for grouping in self._existing_groupings():
            status, partition = self._status_for_bucket(grouping[0])
            yield from self._entries_in(grouping, status, partition)

    # ------------------------------------------------------------------
    # Storage mechanics — nothing below this line is part of the boundary
    # ------------------------------------------------------------------

    def _ref_path(self, case_id: str) -> str:
        return self._policy.case_ref_path_template.format(case_id=case_id)

    def _case_id_from_ref(self, ref_path: str) -> str | None:
        """Invert the ref-path template. None when the entry is not one of ours."""
        prefix, sep, suffix = self._policy.case_ref_path_template.partition("{case_id}")
        if not sep:
            return None
        if not (ref_path.startswith(prefix) and ref_path.endswith(suffix)):
            return None
        end = len(ref_path) - len(suffix)
        if end <= len(prefix):
            return None
        return ref_path[len(prefix) : end]

    def _live_bucket_dir(self) -> Path:
        return self._root / self._policy.live_bucket

    def _grouping_for(self, status: str, partition: str | None) -> tuple[str, ...]:
        if status == LIVE:
            return (self._policy.live_bucket,)
        if status == QUARANTINED:
            return (self._policy.aberrant_bucket,)
        if status == TERMINATED:
            if not partition:
                raise ValueError(
                    "status 'terminated' is partitioned by archive label; "
                    "pass partition=<label>."
                )
            return (f"{self._policy.terminal_prefix}_{partition}",)
        raise UnknownCaseStatusError(status, known=WRITABLE_STATUSES)

    def _status_for_bucket(self, bucket: str) -> tuple[str, str | None]:
        if bucket == self._policy.live_bucket:
            return LIVE, None
        if bucket == self._policy.aberrant_bucket:
            return QUARANTINED, None
        prefix = f"{self._policy.terminal_prefix}_"
        if bucket.startswith(prefix):
            return TERMINATED, bucket[len(prefix) :]
        # A bucket this version does not recognize. Fail safe: report it as-is,
        # which is not `live`, and so means "not driven".
        return bucket, None

    def _grouping_of(self, entry: CaseEntry) -> tuple[str, ...]:
        """The grouping an existing entry currently sits in.

        Unlike ``_grouping_for`` this never raises: an entry read out of an
        unrecognized bucket reports that bucket name as its status, so the round
        trip still addresses the case that is actually there.
        """
        try:
            return self._grouping_for(entry.status, entry.partition)
        except UnknownCaseStatusError:
            return (entry.status,)

    def _existing_groupings(self, *, skip_live: bool = False) -> list[tuple[str, ...]]:
        """Managed groupings that actually exist.

        Probing a grouping that does not exist would create its directory as a
        side effect, so candidates always come from the cache's own index.
        """
        globs = [self._policy.aberrant_bucket, f"{self._policy.terminal_prefix}_*"]
        if not skip_live:
            globs.insert(0, self._policy.live_bucket)
        found: list[tuple[str, ...]] = []
        for glob in globs:
            for grouping in self._cache.groupings(filters=[glob]):
                key = tuple(grouping.grouping_key or ())
                if key and key not in found:
                    found.append(key)
        return found

    def _entries_in(
        self, grouping: tuple[str, ...], status: str, partition: str | None
    ) -> Iterator[CaseEntry]:
        for ref in self._cache.files(grouping):
            folder = ref.slave_dir_path
            if not folder.exists():
                continue
            case_id = self._case_id_from_ref(ref.ref_path) or read_case_id_from_folder(folder)
            if case_id is None:
                continue
            yield CaseEntry(case_id, status, partition, folder)

    def _case_folder_containing(self, path: Path) -> Path | None:
        resolved = path.resolve()
        for candidate in (resolved, *resolved.parents):
            if (candidate / RECORD_NAME).exists():
                return candidate
            if candidate == self._root:
                break
        return None

    async def _register(self, case_id: str, grouping: tuple[str, ...]) -> Path:
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
                LocalFileProxy(tmp_path, ref_path=self._ref_path(case_id), delete_after_deploy=True),
                grouping,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return self._cache.get_slave_dir(grouping, self._ref_path(case_id))
