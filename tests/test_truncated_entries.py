# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Integration tests for the truncated-entries feature in CachedFileFolders.

Uses FileProxyDummy (reports 1024 bytes and a deterministic mtime) and
a temporary CachedFileFolders instance backed by a tmp directory.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
from totodev_pub.cached_file_folders_support.change_notice import ChangeNotice
from totodev_pub.cached_file_folders_support.file_proxy_base import (
    LocalRetentionRecommendation,
)
from totodev_pub.cached_file_folders_support.file_proxy_dummy import FileProxyDummy
from totodev_pub.cached_file_folders_support import truncation_support as ts
from totodev_pub.cached_file_folders_support.sync_types import ChangeType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TruncatingDummy(FileProxyDummy):
    """FileProxyDummy variant that always recommends TRUNCATE.

    Uses content-based looks_same (inherits from FileProxyDummy). A zero-byte
    cached file has no parseable content, so looks_same always returns False —
    the correct behaviour for content-parsing proxies (re-materialize if uncertain).
    """

    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        return LocalRetentionRecommendation.TRUNCATE


class SizeMtimeTruncatingDummy(FileProxyDummy):
    """TRUNCATE dummy with size+mtime looks_same, like LocalFileProxy.

    Use this when testing the cheap no-op path: looks_same uses override_byte_count
    (the recorded pre-truncation size), so a zero-byte cached file is correctly
    identified as unchanged when its recorded size matches and mtime matches.
    """

    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        return LocalRetentionRecommendation.TRUNCATE

    def looks_same(self, other_fpath: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
        try:
            st = os.stat(other_fpath)
            other_size = override_byte_count if override_byte_count is not None else st.st_size
            if other_size != 1024:  # FileProxyDummy always reports 1024 bytes
                return False
            if self._file_mtime is not None and abs(st.st_mtime - self._file_mtime) > 1.0:
                return False
            return True
        except OSError:
            return None


class ExcludingDummy(FileProxyDummy):
    """FileProxyDummy variant that always recommends EXCLUDE."""

    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        return LocalRetentionRecommendation.EXCLUDE


def _make_cache(tmp_path: Path) -> CachedFileFolders:
    return CachedFileFolders("files/", str(tmp_path), use_xxhash=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_truncated_entry(tmp_path):
    """Inserting a TRUNCATE proxy creates a zero-byte file with a valid sidecar."""
    cache = _make_cache(tmp_path)
    proxy = TruncatingDummy(ref_path="docs/report.pdf", version_num=1)

    notice = await cache.upsert_file(proxy)

    assert notice is not None
    assert notice.change_type == ChangeType.INSERT
    ref = notice.cur
    assert ref.file_path.exists()
    assert ref.file_path.stat().st_size == 0
    assert ts.is_truncated(ref.file_path, ref.slave_dir_path)
    sidecar = ts.read_truncation_info(ref.slave_dir_path)
    assert sidecar is not None
    assert sidecar.size == 1024  # FileProxyDummy reports 1024 bytes
    # is_truncated() memo should be pre-seeded
    assert ref._is_truncated_memo is True
    assert ref.is_truncated() is True


@pytest.mark.asyncio
async def test_truncated_cheap_noop(tmp_path):
    """Upserting the same TRUNCATE proxy twice with no change returns None on the second call.

    Requires a size+mtime proxy (SizeMtimeTruncatingDummy) so that looks_same can use
    override_byte_count to correctly identify the unchanged zero-byte cached file.
    Content-parsing proxies (like plain TruncatingDummy) cannot do this cheap check.
    """
    cache = _make_cache(tmp_path)
    proxy = SizeMtimeTruncatingDummy(ref_path="docs/report.pdf", version_num=1)

    await cache.upsert_file(proxy)
    notice2 = await cache.upsert_file(proxy)

    assert notice2 is None


@pytest.mark.asyncio
async def test_demote_full_to_truncated_source_unchanged(tmp_path):
    """Demoting a KEEP entry to TRUNCATE with no source change emits no notice."""
    cache = _make_cache(tmp_path)
    # First upsert: full KEEP entry
    keep_proxy = FileProxyDummy(ref_path="docs/report.pdf", version_num=1)
    notice1 = await cache.upsert_file(keep_proxy)
    assert notice1 is not None
    ref = notice1.cur
    assert ref.file_path.stat().st_size > 0  # full file

    # Second upsert: same source, now TRUNCATE
    trunc_proxy = TruncatingDummy(ref_path="docs/report.pdf", version_num=1)
    notice2 = await cache.upsert_file(trunc_proxy)
    assert notice2 is None  # origin-unchanged demotion: no notice
    assert ref.file_path.stat().st_size == 0  # zeroed in-place
    assert ts.is_truncated(ref.file_path, ref.slave_dir_path)
    sidecar = ts.read_truncation_info(cache.find_file("docs/report.pdf").slave_dir_path)
    assert sidecar is not None
    assert sidecar.size is not None and sidecar.size > 0


@pytest.mark.asyncio
async def test_demote_full_to_truncated_source_changed(tmp_path):
    """Demoting a KEEP entry when source changed emits an UPDATE notice."""
    cache = _make_cache(tmp_path)
    keep_proxy = FileProxyDummy(ref_path="docs/report.pdf", version_num=1)
    await cache.upsert_file(keep_proxy)

    # New version of the source, recommend TRUNCATE
    trunc_proxy = TruncatingDummy(ref_path="docs/report.pdf", version_num=2)
    notice = await cache.upsert_file(trunc_proxy)
    assert notice is not None
    assert notice.change_type == ChangeType.UPDATE
    ref = notice.cur
    assert ref.file_path.stat().st_size == 0
    assert ts.is_truncated(ref.file_path, ref.slave_dir_path)
    assert ref._is_truncated_memo is True


@pytest.mark.asyncio
async def test_restore_truncated_to_full(tmp_path):
    """Switching from TRUNCATE back to KEEP restores the full body."""
    cache = _make_cache(tmp_path)
    # Insert as truncated
    await cache.upsert_file(TruncatingDummy(ref_path="docs/a.txt", version_num=1))
    file_ref = cache.find_file("docs/a.txt")
    assert ts.is_truncated(file_ref.file_path, file_ref.slave_dir_path)

    # Re-upsert with KEEP recommendation
    notice = await cache.upsert_file(FileProxyDummy(ref_path="docs/a.txt", version_num=1))

    assert notice is not None
    assert notice.change_type == ChangeType.UPDATE
    restored = cache.find_file("docs/a.txt")
    assert restored.file_path.stat().st_size > 0
    assert not ts.is_truncated(restored.file_path, restored.slave_dir_path)
    # The old ref should record that it was truncated
    assert notice.old is not None
    assert notice.old._is_truncated_memo is True


@pytest.mark.asyncio
async def test_existing_cached_files_include_truncated_filter(tmp_path):
    """include_truncated=False excludes truncated entries from iteration."""
    cache = _make_cache(tmp_path)
    await cache.upsert_file(TruncatingDummy(ref_path="a.pdf", version_num=1))
    await cache.upsert_file(FileProxyDummy(ref_path="b.pdf", version_num=1))

    all_files = list(cache.files())
    assert len(all_files) == 2

    full_only = list(cache._storage.existing_cached_files(include_truncated=False))
    assert len(full_only) == 1
    assert full_only[0].ref_path == "b.pdf"


@pytest.mark.asyncio
async def test_exclude_recommendation_not_upserted_in_bulk(tmp_path):
    """EXCLUDE proxies in resync_bulk are skipped; a prior cached entry is swept."""
    cache = _make_cache(tmp_path)
    # Pre-cache the file with KEEP
    await cache.upsert_file(FileProxyDummy(ref_path="docs/x.txt", version_num=1))
    assert cache.file_exists("docs/x.txt")

    # Now bulk sync with EXCLUDE recommendation
    exclude_proxy = ExcludingDummy(ref_path="docs/x.txt", version_num=1)
    result = await cache.resync_bulk([exclude_proxy], auto_delete=True)

    # The file should have been deleted (not touched → swept)
    assert not cache.file_exists("docs/x.txt")


@pytest.mark.asyncio
async def test_force_truncation_on_full_file(tmp_path):
    """force_truncation(enabled=True) on a full file zeroes it and writes a FORCE_TRUNCATE sidecar."""
    cache = _make_cache(tmp_path)
    await cache.upsert_file(FileProxyDummy(ref_path="docs/big.mp4", version_num=1))
    ref = cache.find_file("docs/big.mp4")
    assert ref.file_path.stat().st_size > 0

    cache.force_truncation("docs/big.mp4")

    ref2 = cache.find_file("docs/big.mp4")
    assert ref2.file_path.stat().st_size == 0
    sidecar = ts.read_truncation_info(ref2.slave_dir_path)
    assert sidecar is not None
    assert sidecar.manual_override == "FORCE_TRUNCATE"
    assert sidecar.size is not None and sidecar.size > 0


@pytest.mark.asyncio
async def test_force_truncation_clear_override(tmp_path):
    """force_truncation(enabled=False) clears the FORCE_TRUNCATE override."""
    cache = _make_cache(tmp_path)
    await cache.upsert_file(FileProxyDummy(ref_path="docs/big.mp4", version_num=1))
    cache.force_truncation("docs/big.mp4")

    cache.force_truncation("docs/big.mp4", enabled=False)

    ref = cache.find_file("docs/big.mp4")
    sidecar = ts.read_truncation_info(ref.slave_dir_path)
    assert sidecar is not None
    assert sidecar.manual_override is None


@pytest.mark.asyncio
async def test_force_truncation_preserved_across_sync(tmp_path):
    """FORCE_TRUNCATE override keeps entry truncated even when proxy recommends KEEP.

    FileProxyDummy is a content-parsing proxy: its looks_same reads version numbers
    from file content, so a zero-byte cached file always returns False (differs).
    The correct outcome is an UPDATE notice with a refreshed sidecar — the entry
    remains truncated with the override intact. A size-based proxy would produce a
    no-op instead (see test_truncated_cheap_noop).
    """
    cache = _make_cache(tmp_path)
    await cache.upsert_file(FileProxyDummy(ref_path="docs/keep.txt", version_num=1))
    cache.force_truncation("docs/keep.txt")

    # Re-sync with KEEP proxy: effective retention is TRUNCATE due to FORCE_TRUNCATE override.
    # FileProxyDummy.looks_same returns False on a zero-byte file → sidecar is refreshed.
    notice = await cache.upsert_file(FileProxyDummy(ref_path="docs/keep.txt", version_num=1))
    assert notice is not None
    assert notice.change_type == ChangeType.UPDATE
    ref = cache.find_file("docs/keep.txt")
    assert ref.file_path.stat().st_size == 0  # still truncated
    sidecar = ts.read_truncation_info(ref.slave_dir_path)
    assert sidecar.manual_override == "FORCE_TRUNCATE"  # override preserved


@pytest.mark.asyncio
async def test_force_truncation_preserved_cheap_noop(tmp_path):
    """FORCE_TRUNCATE override + size-based proxy → cheap no-op on repeated sync."""
    cache = _make_cache(tmp_path)
    # Insert as full, then force-truncate
    await cache.upsert_file(FileProxyDummy(ref_path="docs/keep.txt", version_num=1))
    cache.force_truncation("docs/keep.txt")

    # Re-sync with a size-based KEEP proxy: looks_same uses override_byte_count → True
    class SizeMtimeKeepDummy(SizeMtimeTruncatingDummy):
        def local_retention_recommendation(self):
            return LocalRetentionRecommendation.KEEP

    notice = await cache.upsert_file(SizeMtimeKeepDummy(ref_path="docs/keep.txt", version_num=1))
    assert notice is None  # source unchanged; entry stays truncated
    ref = cache.find_file("docs/keep.txt")
    assert ref.file_path.stat().st_size == 0
    sidecar = ts.read_truncation_info(ref.slave_dir_path)
    assert sidecar.manual_override == "FORCE_TRUNCATE"


@pytest.mark.asyncio
async def test_async_change_receiver(tmp_path):
    """An async change_receiver is awaited; it receives a pre-seeded is_truncated() result."""
    cache = _make_cache(tmp_path)
    received = []

    async def my_receiver(notice: ChangeNotice, proxy):
        received.append((notice.change_type, notice.cur.is_truncated()))

    await cache.upsert_file(TruncatingDummy(ref_path="a.pdf", version_num=1),
                            change_receiver=my_receiver)

    assert len(received) == 1
    change_type, is_trunc = received[0]
    assert change_type == ChangeType.INSERT
    assert is_trunc is True


@pytest.mark.asyncio
async def test_spurious_sidecar_on_nonempty_file(tmp_path):
    """A non-zero file with a sidecar is treated as full (spurious sidecar → not truncated)."""
    cache = _make_cache(tmp_path)
    notice = await cache.upsert_file(FileProxyDummy(ref_path="doc.txt", version_num=1))
    ref = notice.cur
    # Manually plant a sidecar next to a non-zero file
    ts.write_truncation_info(ref.slave_dir_path,
        ts.TruncationInfo(size=99, mtime=1234567890.0))

    assert not ts.is_truncated(ref.file_path, ref.slave_dir_path)
    assert ref.is_truncated() is False  # memo is None; re-checks disk


@pytest.mark.asyncio
async def test_is_truncated_memo_on_delete_notice(tmp_path):
    """Deleting a truncated entry seeds _is_truncated_memo=True on the old ref."""
    cache = _make_cache(tmp_path)
    await cache.upsert_file(TruncatingDummy(ref_path="doc.txt", version_num=1))

    received_notices = []

    def receiver(notice, proxy):
        received_notices.append(notice)

    await cache.delete_file("doc.txt", change_receiver=receiver)

    assert len(received_notices) == 1
    assert received_notices[0].old._is_truncated_memo is True


@pytest.mark.asyncio
async def test_truncation_info_roundtrip():
    """TruncationInfo serialises/deserialises correctly, including all optional fields."""
    original = ts.TruncationInfo(
        size=123456,
        mtime=1700000000.5,
        hash="abc123",
        retrieval_hint={"source": "sharepoint", "path": "/docs/a.pdf"},
        manual_override="FORCE_TRUNCATE",
    )
    text = ts._to_yaml_text(original)
    assert text.startswith(ts.MARKER_LINE + "\n")
    parsed = ts._from_yaml_text(text)
    assert parsed == original


@pytest.mark.asyncio
async def test_truncation_info_marker_version():
    """MARKER_LINE contains a version suffix for future format evolution."""
    assert "/v" in ts.MARKER_LINE, "MARKER_LINE must contain a version suffix (e.g. /v1)"


def test_is_truncated_invalid_sidecar_returns_false(tmp_path):
    """A sidecar with a wrong first line is not recognised as a valid truncation marker."""
    file_path = tmp_path / "fake.txt"
    slave_dir = tmp_path / "fake.txt._slave"
    slave_dir.mkdir()
    file_path.touch()  # zero bytes
    sidecar = slave_dir / ts.SIDECAR_FILENAME
    sidecar.write_text("kind: something-else\nsize: 42\n")

    assert not ts.is_truncated(file_path, slave_dir)


def test_is_truncated_missing_sidecar_returns_false(tmp_path):
    """A zero-byte file without a sidecar is NOT truncated (could be a genuinely empty file)."""
    file_path = tmp_path / "empty.txt"
    slave_dir = tmp_path / "empty.txt._slave"
    slave_dir.mkdir()
    file_path.touch()

    assert not ts.is_truncated(file_path, slave_dir)


# ---------------------------------------------------------------------------
# force_truncation() invoked during a bulk rescan (resync_sweep) event handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_truncation_during_sweep_on_touched_file(tmp_path):
    """force_truncation() called from a change_receiver mid-sweep is handled gracefully.

    The file being upserted is "touched" by the sweep, so it is exempt from
    mark-and-sweep deletion. Forcing truncation on it from inside its own change
    handler must complete without error and leave a valid truncated entry behind.
    """
    cache = _make_cache(tmp_path)
    handler_errors = []

    def receiver(notice: ChangeNotice, proxy):
        # Errors raised here would be swallowed by the sweep's RETAIN_OLD policy,
        # so capture them explicitly to assert the call was graceful.
        try:
            if notice.cur is not None and notice.cur.ref_path == "docs/a.txt":
                cache.force_truncation("docs/a.txt")
        except Exception as e:  # noqa: BLE001 - test wants to surface any failure
            handler_errors.append(e)

    async with cache.resync_sweep(change_receiver=receiver) as session:
        session.upsert_file(
            FileProxyDummy(ref_path="docs/a.txt", version_num=1, materialize_secs=0.0))

    assert handler_errors == []
    ref = cache.find_file("docs/a.txt")
    assert ref is not None  # touched file survives the sweep
    assert ref.file_path.stat().st_size == 0
    assert ts.is_truncated(ref.file_path, ref.slave_dir_path)
    sidecar = ts.read_truncation_info(ref.slave_dir_path)
    assert sidecar is not None and sidecar.manual_override == "FORCE_TRUNCATE"


@pytest.mark.asyncio
async def test_force_truncation_during_sweep_preserves_untouched_file(tmp_path):
    """force_truncation() on an *untouched* sweep-deletion candidate is graceful.

    "docs/b.txt" is not re-upserted in the sweep, so it would normally be deleted by
    mark-and-sweep. Forcing its truncation from inside another file's change handler
    mutates it mid-sweep; the orchestrator's optimistic concurrency check (mtime/size
    verification) then detects the change and PRESERVES the file instead of deleting
    it. The end result is a valid truncated entry — no crash, no data loss.
    """
    cache = _make_cache(tmp_path)
    await cache.upsert_file(
        FileProxyDummy(ref_path="docs/a.txt", version_num=1, materialize_secs=0.0))
    await cache.upsert_file(
        FileProxyDummy(ref_path="docs/b.txt", version_num=1, materialize_secs=0.0))

    handler_errors = []

    def receiver(notice: ChangeNotice, proxy):
        try:
            if notice.cur is not None and notice.cur.ref_path == "docs/a.txt":
                # b.txt is intentionally NOT re-upserted: absent this truncation it
                # would be swept away on context exit.
                cache.force_truncation("docs/b.txt")
        except Exception as e:  # noqa: BLE001 - test wants to surface any failure
            handler_errors.append(e)

    async with cache.resync_sweep(change_receiver=receiver) as session:
        # Re-upsert only a.txt (version bump → real change → handler fires).
        session.upsert_file(
            FileProxyDummy(ref_path="docs/a.txt", version_num=2, materialize_secs=0.0))

    assert handler_errors == []
    b = cache.find_file("docs/b.txt")
    assert b is not None, "force-truncated untouched file must be preserved, not swept"
    assert b.file_path.stat().st_size == 0
    assert ts.is_truncated(b.file_path, b.slave_dir_path)
    sidecar = ts.read_truncation_info(b.slave_dir_path)
    assert sidecar is not None and sidecar.manual_override == "FORCE_TRUNCATE"
