#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Upload and download scratch folders with a secret link and a deadline.

A web app often needs a place to receive one upload, or to stage one download,
and a URL that can reach that place for a short time. UploadDownloadFolders
owns a CachedFileFolders cache and uses it as the registry of those places.
Each call to create_upload() or create_download() makes a new folder, returns
a long unguessable token, and starts the clock. The application writes files
into ticket.work_dir. Later it resolves the token from the URL, or moves the
file out to permanent storage.

This is an example of owning CachedFileFolders, not a change to it. The cache
indexes a tiny sentinel file per ticket and deletes that entry when the ticket
ends. The bytes the user cares about live in the sentinel's slave directory,
which the cache leaves alone until the ticket is removed. Session checks,
deadlines, one-shot links, and unzipping are ticket rules. They are not cache
retention. Cache retention decides whether a mirrored document keeps its body.
These folders decide whether a link still works.

When to use this
----------------
- A browser uploads a file to a token you minted for that user and that session.
- A report is written into a folder and the user downloads it from a link.
- The folder must survive a minimum time, then disappear, and the link must
  fail even if a locked file kept the directory on disk.

Create one instance per process, at startup:

    slots = UploadDownloadFolders(Path("/var/app/scratch"))
    await slots.startup()          # sweep expired folders; pass sweep=False to skip

    ticket = await slots.create_download(session_id=session_id, once=True)
    report = ticket.work_dir / "report.pdf"
    report.write_bytes(pdf_bytes)
    url = f"/downloads/{ticket.token}"

    # On the GET:
    got = await slots.resolve(token, kind="download", session_id=session_id)
    return FileResponse(got.work_dir / "report.pdf")
    # once=True: a second resolve raises TicketConsumed.

    # Taking an upload away:
    up = await slots.create_upload(session_id=session_id, once=True)
    # the client writes into up.work_dir, then the app seals and moves:
    await slots.seal(up.token, kind="upload", session_id=session_id)
    dest = await slots.move_out(up.token, permanent_dir / "intake.pdf",
                                kind="upload", session_id=session_id)

Multiple instances may point at the same root (for example several workers in
one deployment). Ticket state lives on disk with file locks, so they share the
same tokens and payloads. That is one trust domain, not multi-tenant isolation:
apps that should not see each other's scratch files need separate roots.

Two clocks, both measured from create
--------------------------------------
`minimum_retention` is how long the sweeper will refuse to delete the folder.
`available_for` is how long resolve, seal, and move_out will honor the token.
They default to 30 minutes and can differ. After available_for, the link is
not available whether or not the directory is still on disk. discard() deletes
immediately and is the way to override the retention floor.

A session id is optional. If you pass one at create, every later call must
pass the same id. The token is the secret. Do not put the session id in the URL.

once=True closes the public link and still lets the application move the file
out until the availability cutoff. For an upload, seal() after a file has
landed closes it. For a download, the first successful resolve closes it.
Resolving or sealing an empty folder raises TicketNotReady and does not use
the link up.

auto_unzip=True expands a single zip during seal(), one level, and refuses
archives that path-traverse, symlink, or exceed the size and entry caps.

move_out() renames the payload to the destination when it can, and copies,
flushes, and deletes the source when it cannot. It removes the payload from
the scratch folder before it unregisters the cache entry.

Failures raise TicketUnavailable, TicketConsumed, TicketNotReady,
AmbiguousPayload, or UnsafeArchive, each with a fixed message.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal, Optional, Union

import portalocker
import yaml
from pydantic import BaseModel

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy

logger = logging.getLogger(__name__)

Kind = Literal["upload", "download"]

MSG_UNAVAILABLE = "This link is no longer available."
MSG_CONSUMED = "This link has already been used."
MSG_NOT_READY = "Nothing has been placed for download yet."
MSG_AMBIGUOUS = "Name the file to move."
MSG_UNSAFE_ARCHIVE = "The archive cannot be expanded."

SENTINEL_NAME = "ticket.sentinel"
SLOT_YAML = "slot.yaml"
SLOT_LOCK = "slot.lock"
PAYLOAD_DIR = "payload"
UNPACKED_DIR = "unpacked"
DEFAULT_RETENTION = timedelta(minutes=30)
DEFAULT_AVAILABLE = timedelta(minutes=30)
DEFAULT_MAX_UNZIP_ENTRIES = 10_000
DEFAULT_MAX_UNZIP_BYTES = 512 * 1024 * 1024
LOCK_TIMEOUT_SECS = 5.0


class TicketUnavailable(Exception):
    """Unknown, expired, moved, or otherwise unusable ticket."""

    def __init__(self, message: str = MSG_UNAVAILABLE) -> None:
        super().__init__(message)


class TicketSessionMismatch(TicketUnavailable):
    """Session id does not match the ticket's bound session."""

    def __init__(self, message: str = MSG_UNAVAILABLE) -> None:
        super().__init__(message)


class TicketConsumed(Exception):
    """One-shot ticket has already been used."""

    def __init__(self, message: str = MSG_CONSUMED) -> None:
        super().__init__(message)


class TicketNotReady(Exception):
    """Payload is empty when content is required."""

    def __init__(self, message: str = MSG_NOT_READY) -> None:
        super().__init__(message)


class AmbiguousPayload(Exception):
    """More than one top-level payload entry and no name was given."""

    def __init__(self, message: str = MSG_AMBIGUOUS) -> None:
        super().__init__(message)


class UnsafeArchive(Exception):
    """Zip failed safety checks and was not expanded."""

    def __init__(self, message: str = MSG_UNSAFE_ARCHIVE) -> None:
        super().__init__(message)


class SlotRecord(BaseModel):
    """On-disk ticket metadata stored as slot.yaml in the slave directory."""

    token: str
    kind: Kind
    session_id: Optional[str] = None
    once: bool = False
    auto_unzip: bool = False
    created_at: datetime
    available_until: datetime
    retain_until: datetime
    consumed_at: Optional[datetime] = None
    moved_at: Optional[datetime] = None
    delete_requested: bool = False


@dataclass(frozen=True)
class SlotTicket:
    """Public handle returned by create / resolve / seal."""

    token: str
    kind: Kind
    work_dir: Path
    created_at: datetime
    available_until: datetime
    retain_until: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _token_prefix(token: str) -> str:
    return token[:6] if token else ""


def _ref_path_for(token: str) -> str:
    return f"{token}/{SENTINEL_NAME}"


def _grouping_key(kind: Kind) -> tuple[str, ...]:
    return (kind,)


def _payload_entries(work_dir: Path) -> list[Path]:
    if not work_dir.exists():
        return []
    return sorted(p for p in work_dir.iterdir() if p.name != ".DS_Store")


def _empty_payload(work_dir: Path) -> bool:
    return len(_payload_entries(work_dir)) == 0


class UploadDownloadFolders:
    """Own a CachedFileFolders cache of short-lived upload/download scratch folders."""

    def __init__(
        self,
        root_dir: Union[str, os.PathLike],
        *,
        minimum_retention: timedelta = DEFAULT_RETENTION,
        available_for: timedelta = DEFAULT_AVAILABLE,
        auto_unzip: bool = False,
        max_unzip_entries: int = DEFAULT_MAX_UNZIP_ENTRIES,
        max_unzip_bytes: int = DEFAULT_MAX_UNZIP_BYTES,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if minimum_retention < timedelta(0):
            raise ValueError("minimum_retention must be >= 0")
        if available_for < timedelta(0):
            raise ValueError("available_for must be >= 0")
        if max_unzip_entries < 1:
            raise ValueError("max_unzip_entries must be >= 1")
        if max_unzip_bytes < 1:
            raise ValueError("max_unzip_bytes must be >= 1")

        self._root = Path(root_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        cache_root = self._root / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)

        self._minimum_retention = minimum_retention
        self._available_for = available_for
        self._auto_unzip = auto_unzip
        self._max_unzip_entries = max_unzip_entries
        self._max_unzip_bytes = max_unzip_bytes
        self._now = now or _utc_now

        self._cache = CachedFileFolders(
            grouping_pattern="slots/{kind}/",
            root_dir=str(cache_root),
        )

    def close(self) -> None:
        self._cache.close()

    async def startup(self, sweep: bool = True) -> int:
        """Optionally sweep expired or discard-requested tickets. Returns count removed."""
        if not sweep:
            logger.info(
                "UploadDownloadFolders startup at %s (sweep skipped)",
                self._root,
            )
            return 0
        removed = await self._sweep()
        logger.info(
            "UploadDownloadFolders startup at %s: swept %d expired/discarded ticket(s)",
            self._root,
            removed,
        )
        return removed

    async def create_upload(
        self,
        *,
        session_id: Optional[str] = None,
        once: bool = False,
        auto_unzip: Optional[bool] = None,
        available_for: Optional[timedelta] = None,
        minimum_retention: Optional[timedelta] = None,
    ) -> SlotTicket:
        return await self._create(
            kind="upload",
            session_id=session_id,
            once=once,
            auto_unzip=auto_unzip,
            available_for=available_for,
            minimum_retention=minimum_retention,
        )

    async def create_download(
        self,
        *,
        session_id: Optional[str] = None,
        once: bool = False,
        auto_unzip: Optional[bool] = None,
        available_for: Optional[timedelta] = None,
        minimum_retention: Optional[timedelta] = None,
    ) -> SlotTicket:
        return await self._create(
            kind="download",
            session_id=session_id,
            once=once,
            auto_unzip=auto_unzip,
            available_for=available_for,
            minimum_retention=minimum_retention,
        )

    async def resolve(
        self,
        token: str,
        *,
        kind: Kind,
        session_id: Optional[str] = None,
    ) -> SlotTicket:
        with self._locked_record(token, kind) as (record, slave_dir, work_dir):
            self._require_usable(record, session_id=session_id)
            if record.once and record.consumed_at is not None:
                raise TicketConsumed()
            if _empty_payload(work_dir):
                raise TicketNotReady()
            if record.kind == "download" and record.once:
                record.consumed_at = self._now()
                self._write_record(slave_dir, record)
            return self._ticket_from(record, work_dir)

    async def seal(
        self,
        token: str,
        *,
        kind: Kind,
        session_id: Optional[str] = None,
    ) -> SlotTicket:
        with self._locked_record(token, kind) as (record, slave_dir, work_dir):
            self._require_usable(record, session_id=session_id)
            if record.once and record.consumed_at is not None:
                raise TicketConsumed()
            if _empty_payload(work_dir):
                raise TicketNotReady()

            if record.auto_unzip:
                self._maybe_unzip(work_dir)

            if record.kind == "upload" and record.once:
                record.consumed_at = self._now()
                self._write_record(slave_dir, record)

            return self._ticket_from(record, work_dir)

    async def move_out(
        self,
        token: str,
        destination: Union[str, os.PathLike],
        *,
        kind: Kind,
        session_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Path:
        dest = Path(destination)
        with self._locked_record(token, kind) as (record, slave_dir, work_dir):
            # move_out ignores consumed_at; only expiry, session, and moved_at apply.
            self._require_usable(record, session_id=session_id)
            source = self._select_payload_entry(work_dir, name=name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._durable_relocate(source, dest)
            record.moved_at = self._now()
            self._write_record(slave_dir, record)

        await self._unregister(token, kind, work_dir)
        return dest

    async def discard(
        self,
        token: str,
        *,
        kind: Kind,
        session_id: Optional[str] = None,
    ) -> None:
        with self._locked_record(token, kind) as (record, slave_dir, work_dir):
            self._require_usable(record, session_id=session_id)
            record.delete_requested = True
            self._write_record(slave_dir, record)
        await self._unregister(token, kind, work_dir)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _create(
        self,
        *,
        kind: Kind,
        session_id: Optional[str],
        once: bool,
        auto_unzip: Optional[bool],
        available_for: Optional[timedelta],
        minimum_retention: Optional[timedelta],
    ) -> SlotTicket:
        avail = self._available_for if available_for is None else available_for
        retain = self._minimum_retention if minimum_retention is None else minimum_retention
        if avail < timedelta(0):
            raise ValueError("available_for must be >= 0")
        if retain < timedelta(0):
            raise ValueError("minimum_retention must be >= 0")

        unzip = self._auto_unzip if auto_unzip is None else auto_unzip
        created = self._now()
        token = secrets.token_urlsafe(32)
        ref_path = _ref_path_for(token)
        grouping = _grouping_key(kind)

        with tempfile.NamedTemporaryFile(prefix="udf_sentinel_", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(b"")
        try:
            proxy = LocalFileProxy(tmp_path, ref_path=ref_path, delete_after_deploy=True)
            await self._cache.upsert_file(proxy, grouping)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        slave_dir = self._cache.get_slave_dir(grouping, ref_path)
        work_dir = slave_dir / PAYLOAD_DIR
        work_dir.mkdir(parents=True, exist_ok=True)

        record = SlotRecord(
            token=token,
            kind=kind,
            session_id=session_id,
            once=once,
            auto_unzip=unzip,
            created_at=created,
            available_until=created + avail,
            retain_until=created + retain,
        )
        self._write_record(slave_dir, record)
        logger.debug(
            "Created %s ticket %s (available_until=%s retain_until=%s)",
            kind,
            _token_prefix(token),
            record.available_until.isoformat(),
            record.retain_until.isoformat(),
        )
        return self._ticket_from(record, work_dir)

    def _ticket_from(self, record: SlotRecord, work_dir: Path) -> SlotTicket:
        return SlotTicket(
            token=record.token,
            kind=record.kind,
            work_dir=work_dir,
            created_at=record.created_at,
            available_until=record.available_until,
            retain_until=record.retain_until,
        )

    def _slave_and_work(self, token: str, kind: Kind) -> tuple[Path, Path]:
        ref_path = _ref_path_for(token)
        grouping = _grouping_key(kind)
        file_ref = self._cache.find_file(ref_path, grouping)
        if file_ref is None:
            logger.debug(
                "Ticket %s kind=%s not found in cache",
                _token_prefix(token),
                kind,
            )
            raise TicketUnavailable()
        slave_dir = file_ref.slave_dir_path
        work_dir = slave_dir / PAYLOAD_DIR
        return slave_dir, work_dir

    @contextmanager
    def _locked_record(
        self, token: str, kind: Kind
    ) -> Iterator[tuple[SlotRecord, Path, Path]]:
        slave_dir, work_dir = self._slave_and_work(token, kind)
        lock_path = slave_dir / SLOT_LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with portalocker.Lock(
                str(lock_path),
                mode="a+",
                timeout=LOCK_TIMEOUT_SECS,
                flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            ):
                record = self._read_record(slave_dir)
                if record.token != token or record.kind != kind:
                    logger.warning(
                        "Slot record mismatch for %s kind=%s",
                        _token_prefix(token),
                        kind,
                    )
                    raise TicketUnavailable()
                yield record, slave_dir, work_dir
        except portalocker.LockException as e:
            logger.warning(
                "Could not lock ticket %s kind=%s: %s",
                _token_prefix(token),
                kind,
                e,
            )
            raise TicketUnavailable() from e

    def _read_record(self, slave_dir: Path) -> SlotRecord:
        path = slave_dir / SLOT_YAML
        if not path.exists():
            raise TicketUnavailable()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return SlotRecord.model_validate(data)

    def _write_record(self, slave_dir: Path, record: SlotRecord) -> None:
        path = slave_dir / SLOT_YAML
        payload = record.model_dump(mode="json")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False)

    def _require_usable(
        self,
        record: SlotRecord,
        *,
        session_id: Optional[str],
    ) -> None:
        now = self._now()
        if record.moved_at is not None:
            logger.debug(
                "Ticket %s already moved",
                _token_prefix(record.token),
            )
            raise TicketUnavailable()
        if now >= record.available_until:
            logger.debug(
                "Ticket %s past available_until=%s (disk may still exist)",
                _token_prefix(record.token),
                record.available_until.isoformat(),
            )
            raise TicketUnavailable()
        if record.session_id is not None:
            if session_id is None or session_id != record.session_id:
                logger.debug(
                    "Ticket %s session mismatch",
                    _token_prefix(record.token),
                )
                raise TicketSessionMismatch()

    def _select_payload_entry(self, work_dir: Path, *, name: Optional[str]) -> Path:
        entries = _payload_entries(work_dir)
        if not entries:
            raise TicketNotReady()
        if name is not None:
            chosen = work_dir / name
            if not chosen.exists() or chosen.parent != work_dir:
                raise TicketUnavailable()
            return chosen
        if len(entries) > 1:
            raise AmbiguousPayload()
        return entries[0]

    def _durable_relocate(self, source: Path, dest: Path) -> None:
        """Rename when possible; otherwise copy, fsync, then delete source."""
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        try:
            os.rename(source, dest)
            return
        except OSError:
            pass

        if source.is_dir():
            shutil.copytree(source, dest)
            self._fsync_tree(dest)
            shutil.rmtree(source)
        else:
            shutil.copy2(source, dest)
            with open(dest, "rb") as f:
                os.fsync(f.fileno())
            try:
                dir_fd = os.open(str(dest.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            source.unlink()

    def _fsync_tree(self, root: Path) -> None:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    with open(path, "rb") as f:
                        os.fsync(f.fileno())
                except OSError:
                    pass
        try:
            dir_fd = os.open(str(root), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _clear_payload(self, work_dir: Path) -> None:
        """Remove all payload entries. Raises PermissionError if blocked."""
        if not work_dir.exists():
            return
        for entry in list(work_dir.iterdir()):
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    async def _unregister(self, token: str, kind: Kind, work_dir: Path) -> bool:
        """Empty payload then delete the cache sentinel. Returns True if removed."""
        try:
            self._clear_payload(work_dir)
        except PermissionError as e:
            logger.warning(
                "Cleanup deferred for ticket %s kind=%s: payload locked (%s); "
                "link remains unavailable after cutoff",
                _token_prefix(token),
                kind,
                e,
            )
            return False

        ref_path = _ref_path_for(token)
        grouping = _grouping_key(kind)
        try:
            await self._cache.delete_file(ref_path, grouping)
        except Exception as e:
            logger.warning(
                "Failed to delete cache entry for ticket %s kind=%s: %s",
                _token_prefix(token),
                kind,
                e,
            )
            return False
        return True

    async def _sweep(self) -> int:
        now = self._now()
        removed = 0
        for kind in ("upload", "download"):
            grouping = _grouping_key(kind)  # type: ignore[arg-type]
            for file_ref in list(self._cache.files(grouping)):
                token = file_ref.ref_path.split("/", 1)[0]
                slave_dir = file_ref.slave_dir_path
                work_dir = slave_dir / PAYLOAD_DIR
                try:
                    record = self._read_record(slave_dir)
                except TicketUnavailable:
                    # Orphan sentinel without slot.yaml — remove it.
                    if await self._unregister(token, kind, work_dir):  # type: ignore[arg-type]
                        removed += 1
                    continue
                if record.delete_requested or now >= record.retain_until:
                    if await self._unregister(token, kind, work_dir):  # type: ignore[arg-type]
                        removed += 1
                    else:
                        logger.warning(
                            "Sweep deferred for ticket %s kind=%s",
                            _token_prefix(token),
                            kind,
                        )
        return removed

    def _maybe_unzip(self, work_dir: Path) -> None:
        zips = [p for p in _payload_entries(work_dir) if p.is_file() and p.suffix.lower() == ".zip"]
        if not zips:
            return
        if len(zips) != 1 or len(_payload_entries(work_dir)) != 1:
            # Only expand when the sole payload entry is a zip.
            return
        zip_path = zips[0]
        unpacked = work_dir / UNPACKED_DIR
        if unpacked.exists():
            shutil.rmtree(unpacked)
        unpacked.mkdir(parents=True, exist_ok=True)
        try:
            self._safe_extract_zip(zip_path, unpacked)
        except UnsafeArchive:
            if unpacked.exists():
                shutil.rmtree(unpacked, ignore_errors=True)
            raise
        zip_path.unlink()

    def _safe_extract_zip(self, zip_path: Path, dest: Path) -> None:
        dest_resolved = dest.resolve()
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                infos = zf.infolist()
                if len(infos) > self._max_unzip_entries:
                    raise UnsafeArchive()
                total_size = 0
                for info in infos:
                    name = info.filename
                    if name.startswith("/") or name.startswith("\\"):
                        raise UnsafeArchive()
                    # Normalize separators and reject traversal / absolute / symlink.
                    parts = name.replace("\\", "/").split("/")
                    if any(p == ".." for p in parts):
                        raise UnsafeArchive()
                    if info.is_dir():
                        continue
                    # ZipInfo.external_attr high bits: symlink on Unix is 0o120000
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise UnsafeArchive()
                    total_size += info.file_size
                    if total_size > self._max_unzip_bytes:
                        raise UnsafeArchive()
                    target = (dest / name).resolve()
                    try:
                        target.relative_to(dest_resolved)
                    except ValueError as e:
                        raise UnsafeArchive() from e
                for info in infos:
                    name = info.filename.replace("\\", "/")
                    if any(p == ".." for p in name.split("/")):
                        raise UnsafeArchive()
                    target = dest / name
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        except UnsafeArchive:
            raise
        except zipfile.BadZipFile as e:
            raise UnsafeArchive() from e
        except OSError as e:
            raise UnsafeArchive() from e
