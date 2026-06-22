# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Integration tests for CachedFileFolders using MockNetworkFileProxy.

These tests exercise paths the existing suite doesn't reach: real async
latency, failure/retry mechanics, _DIR_MTIME.txt change detection, truncated-
entry sidecar correctness, force=True behaviour, orphaned temp files, concurrent
bulk sync, and LREXCLUDE sweep.

If a test fails and the failure is not a logic error in the test itself, the
test is left failing as a bug report for the CachedFileFolders maintainer.
"""

import os
import time
import pytest
from pathlib import Path

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.sync_types import ChangeType
from totodev_pub.cached_file_folders_support import truncation_support as ts
from totodev_pub.cached_file_folders_support.file_proxy_mock_network import (
    MockNetworkFileProxy,
    MockNetworkFileProxyFactory,
    MockNetworkError,
    _DIR_MTIME_FILENAME,
)
from totodev_pub.pytest_tools import very_lazy_test


_LAZY_DEPS = [
    "totodev_pub.cached_file_folders",
    "totodev_pub.cached_file_folders_support.file_proxy_mock_network",
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_source(dirpath: Path, name: str, content: str = "hello\n") -> str:
    path = dirpath / name
    path.write_text(content)
    return str(path)


def _make_dir_mtime(dirpath: Path, mtime: float) -> None:
    (dirpath / _DIR_MTIME_FILENAME).write_text(str(mtime))


def _proxy(source_path: str, **kwargs) -> MockNetworkFileProxy:
    return MockNetworkFileProxy(source_path, **kwargs)


def _make_cache(root: Path) -> CachedFileFolders:
    return CachedFileFolders("files/", str(root), use_xxhash=False)


# ---------------------------------------------------------------------------
# Scenario 1: Slow retrieval — wall-clock timing test
# ---------------------------------------------------------------------------

class TestSlowRetrieval:
    """
    fixtures/
    └── slow_report_RL50ms.txt      ["hello"]

    t0 = now()
    -> resync_bulk()  returns 1 INSERT notice
    elapsed = now() - t0

    assert elapsed >= 0.050
    assert notice.cur.file_path.read_text() == "hello"
    """

    @pytest.mark.asyncio
    @very_lazy_test(_LAZY_DEPS, reverify_days=21)
    async def test_wall_clock_at_least_latency(self, tmp_path):
        """End-to-end resync_bulk with a 50ms proxy takes at least 50ms wall clock."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "slow_report_RL50ms.txt", "hello")

        t0 = time.monotonic()
        result = await cache.resync_bulk([_proxy(src)])
        elapsed = time.monotonic() - t0

        assert len(result.changes) == 1
        assert result.changes[0].change_type == ChangeType.INSERT
        assert elapsed >= 0.050, f"expected >= 50ms, got {elapsed * 1000:.1f}ms"
        assert result.changes[0].cur.file_path.read_text() == "hello"


# ---------------------------------------------------------------------------
# Pathological latency — optional, run rarely
# ---------------------------------------------------------------------------

class TestPathologicalLatency:
    """
    fixtures/
    └── big_job_RL5000ms.txt        ["data"]

    t0 = now()
    -> resync_bulk()  returns 1 INSERT notice
    elapsed = now() - t0

    assert elapsed >= 5.000
    assert result.changes[0].cur.file_path.read_text() == "data"

    Verifies that the system does not impose hidden timeouts or exhaust resources
    under sustained multi-second retrieval. Skipped after first pass for 90 days.
    """

    @pytest.mark.asyncio
    @very_lazy_test(_LAZY_DEPS, reverify_days=90)
    async def test_five_second_retrieval_completes(self, tmp_path):
        """5-second proxy completes successfully with a correct INSERT notice."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "big_job_RL5000ms.txt", "data")

        t0 = time.monotonic()
        result = await cache.resync_bulk([_proxy(src)])
        elapsed = time.monotonic() - t0

        assert len(result.failures) == 0
        assert len(result.changes) == 1
        assert result.changes[0].change_type == ChangeType.INSERT
        assert elapsed >= 5.0, f"expected >= 5s, got {elapsed:.2f}s"
        assert result.changes[0].cur.file_path.read_text() == "data"


# ---------------------------------------------------------------------------
# Scenarios 2 & 3: Retrieval failure — _FF counter
# ---------------------------------------------------------------------------

class TestRetrievalFailure:
    """
    Scenario 2 (_FF2 eventually succeeds):
    fixtures/
    └── data_FF2.txt                ["content"]

    -> resync_bulk(retry_count=2)  returns 1 INSERT, 0 failures
       (3 total attempts: fail, fail, succeed)

    Scenario 3 (_FF5 exhausts retry budget):
    fixtures/
    └── data_FF5.txt                ["content"]

    -> resync_bulk(retry_count=2)  returns 0 changes, 1 failure

    Note: retry_count=N gives N+1 total attempts. The same proxy instance is
    reused on each retry, so the per-instance _FF counter decrements correctly.
    """

    @pytest.mark.asyncio
    async def test_ff_eventually_succeeds(self, tmp_path):
        """_FF2 with retry_count=2 (3 attempts) fails twice then inserts successfully."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "data_FF2.txt", "content")

        result = await cache.resync_bulk([_proxy(src)], retry_count=2)

        assert len(result.failures) == 0, f"unexpected failures: {result.failures}"
        assert len(result.changes) == 1
        assert result.changes[0].change_type == ChangeType.INSERT

    @pytest.mark.asyncio
    async def test_ff_exhausts_budget(self, tmp_path):
        """_FF5 with retry_count=2 (3 attempts) always fails; failure is recorded."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "data_FF5.txt", "content")

        result = await cache.resync_bulk([_proxy(src)], retry_count=2)

        assert len(result.changes) == 0
        assert len(result.failures) == 1
        # CachedFileFolders wraps proxy exceptions in RuntimeError (with context);
        # the original MockNetworkError is preserved as __cause__
        exc = result.failures[0].exception
        assert isinstance(exc, RuntimeError)
        assert isinstance(exc.__cause__, MockNetworkError)
        assert cache.find_file(result.failures[0].ref_path) is None


# ---------------------------------------------------------------------------
# Scenario 4: Content-driven failure toggle (_FAILS)
# ---------------------------------------------------------------------------

class TestFailsContentToggle:
    """
    -- State 1 --------------------------------------------------
    fixtures/
    └── status_FAILS.txt            ["FAIL - server down"]

        -> resync_bulk(retry_count=0)  returns 0 changes, 1 failure (MockNetworkError)

    -- State 2 (flip content) -----------------------------------
    fixtures/
    └── status_FAILS.txt            ["OK - service restored"]  <- edited

        -> resync_bulk(retry_count=0)  returns 1 INSERT
    """

    @pytest.mark.asyncio
    async def test_fails_content_toggle(self, tmp_path):
        """Editing file content flips failure behavior without renaming the file."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        source = fx / "status_FAILS.txt"
        source.write_text("FAIL - server down")

        # State 1: file starts in FAIL state
        result1 = await cache.resync_bulk(
            [MockNetworkFileProxy(str(source))], retry_count=0
        )
        assert len(result1.changes) == 0
        assert len(result1.failures) == 1
        exc1 = result1.failures[0].exception
        assert isinstance(exc1, RuntimeError)
        assert isinstance(exc1.__cause__, MockNetworkError)

        # State 2: flip content — create a new proxy (single-use objects)
        source.write_text("OK - service restored")
        result2 = await cache.resync_bulk(
            [MockNetworkFileProxy(str(source))], retry_count=0
        )
        assert len(result2.failures) == 0
        assert len(result2.changes) == 1
        assert result2.changes[0].change_type == ChangeType.INSERT
        assert result2.changes[0].cur.file_path.read_text() == "OK - service restored"


# ---------------------------------------------------------------------------
# Scenarios 5, 6, 7: _DIR_MTIME.txt sentinel
# ---------------------------------------------------------------------------

class TestDirMtime:
    """
    Scenario 5 — Stable sentinel suppresses re-downloads:
    fixtures/
    ├── _DIR_MTIME.txt              {T=1700000000}
    ├── doc_a.txt                   ["content A"]
    └── doc_b.txt                   ["content B"]

    -- Pass 1: both INSERT
    -- Pass 2 (same proxies): no change (looks_same() uses sentinel mtime)

    Scenario 6 — Advancing sentinel triggers re-sync:
    -- State 1: T=1700000000 -> INSERT
    -- State 2: T=1700000001 (advance) -> UPDATE

    Scenario 7 — Two directories with isolated sentinels:
    dir_a/ {T=1700000000}    dir_b/ {T=1700000100}
    Advance dir_a only -> only dir_a/file.txt triggers UPDATE
    """

    @pytest.mark.asyncio
    async def test_stable_sentinel_suppresses_re_download(self, tmp_path):
        """Sentinel-pinned proxies do not re-download when nothing has changed."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        _make_dir_mtime(fx, 1_700_000_000.0)
        src_a = _make_source(fx, "doc_a.txt", "content A")
        src_b = _make_source(fx, "doc_b.txt", "content B")

        # Pass 1: populate cache
        n_a1 = await cache.upsert_file(_proxy(src_a))
        n_b1 = await cache.upsert_file(_proxy(src_b))
        assert n_a1.change_type == ChangeType.INSERT
        assert n_b1.change_type == ChangeType.INSERT

        # Pass 2: fresh proxy instances — sentinel still pinned to T
        n_a2 = await cache.upsert_file(_proxy(src_a))
        n_b2 = await cache.upsert_file(_proxy(src_b))
        assert n_a2 is None, "sentinel mtime stable — looks_same() should return True"
        assert n_b2 is None

    @pytest.mark.asyncio
    async def test_advancing_sentinel_triggers_update(self, tmp_path):
        """Bumping _DIR_MTIME.txt causes the next upsert to re-download."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        _make_dir_mtime(fx, 1_700_000_000.0)
        src = _make_source(fx, "doc_a.txt", "content A")

        n1 = await cache.upsert_file(_proxy(src))
        assert n1.change_type == ChangeType.INSERT

        # Advance the sentinel
        _make_dir_mtime(fx, 1_700_000_001.0)

        n2 = await cache.upsert_file(_proxy(src))
        assert n2 is not None, "sentinel advanced — looks_same() should return False"
        assert n2.change_type == ChangeType.UPDATE

    @pytest.mark.asyncio
    async def test_two_directories_isolated_sentinels(self, tmp_path):
        """A sentinel in dir_a does not affect proxies sourced from dir_b."""
        cache = _make_cache(tmp_path)
        dir_a = tmp_path / "fx_a"
        dir_b = tmp_path / "fx_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _make_dir_mtime(dir_a, 1_700_000_000.0)
        _make_dir_mtime(dir_b, 1_700_000_100.0)
        src_a = _make_source(dir_a, "file.txt", "A content")
        src_b = _make_source(dir_b, "file.txt", "B content")

        # Initial population — explicit ref_paths to avoid collision (same filename)
        await cache.upsert_file(_proxy(src_a, ref_path="dir_a/file.txt"))
        await cache.upsert_file(_proxy(src_b, ref_path="dir_b/file.txt"))

        # Advance only dir_a sentinel
        _make_dir_mtime(dir_a, 1_700_000_001.0)

        n_a = await cache.upsert_file(_proxy(src_a, ref_path="dir_a/file.txt"))
        n_b = await cache.upsert_file(_proxy(src_b, ref_path="dir_b/file.txt"))

        assert n_a is not None, "dir_a sentinel advanced — should trigger UPDATE"
        assert n_a.change_type == ChangeType.UPDATE
        assert n_b is None, "dir_b sentinel unchanged — should suppress re-download"


# ---------------------------------------------------------------------------
# Scenarios 8 & 9: _FS override + LRTRUNCATE
# ---------------------------------------------------------------------------

class TestFSOverrideWithTruncation:
    """
    Scenario 8 — _FS override size recorded in sidecar:
    fixtures/
    └── archive_FS512KB_LRTRUNCATE.txt    ["small source text"]

    -> upsert_file()  INSERT, truncated
    -> sidecar.size == 512 * 1024  (override size, not actual source size)

    Scenario 9 — retrieval_hint survives sidecar round-trip:
    fixtures/
    └── report_LRTRUNCATE.txt       ["data"]

    -> upsert_file()  INSERT, truncated
    -> ts.read_truncation_info(slave_dir).retrieval_hint == proxy.retrieval_hint()
    """

    @pytest.mark.asyncio
    async def test_fs_override_recorded_in_sidecar(self, tmp_path):
        """_FS512KB_LRTRUNCATE: sidecar records overridden size, not actual source size."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "archive_FS512KB_LRTRUNCATE.txt", "small source text")

        notice = await cache.upsert_file(_proxy(src))

        assert notice is not None
        assert notice.change_type == ChangeType.INSERT
        assert notice.cur.is_truncated() is True
        assert notice.cur.file_path.stat().st_size == 0
        sidecar = ts.read_truncation_info(notice.cur.slave_dir_path)
        assert sidecar is not None
        assert sidecar.size == 512 * 1024, (
            f"sidecar should record FS-override size 524288, got {sidecar.size}"
        )

    @pytest.mark.asyncio
    async def test_retrieval_hint_round_trip(self, tmp_path):
        """retrieval_hint() survives YAML sidecar serialization unchanged."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "report_LRTRUNCATE.txt", "data")
        proxy = _proxy(src)
        expected_hint = proxy.retrieval_hint()

        notice = await cache.upsert_file(proxy)

        assert notice is not None
        assert notice.cur.is_truncated() is True
        sidecar = ts.read_truncation_info(notice.cur.slave_dir_path)
        assert sidecar is not None
        assert sidecar.retrieval_hint == expected_hint, (
            f"retrieval_hint did not survive sidecar round-trip\n"
            f"  expected: {expected_hint}\n"
            f"  got:      {sidecar.retrieval_hint}"
        )


# ---------------------------------------------------------------------------
# Scenarios 10 & 11: force=True
# ---------------------------------------------------------------------------

class TestForceParameter:
    """
    Scenario 10 — force=True bypasses looks_same():
    fixtures/
    ├── _DIR_MTIME.txt              {T=1700000000}
    └── doc.txt                     ["content"]

    -- Pass 1: INSERT
    -- Pass 2 (no force): no change  (looks_same() = True via sentinel)
    -- Pass 3 (force=True): UPDATE

    Scenario 11 — force=True on truncated entry restores full body:
    -- State 1: doc_LRTRUNCATE.txt (ref_path="doc.txt") -> INSERT, truncated
    -- State 2: doc.txt (ref_path="doc.txt", force=True) -> UPDATE, not truncated
    """

    @pytest.mark.asyncio
    async def test_force_bypasses_looks_same(self, tmp_path):
        """force=True causes an UPDATE even when looks_same() would return True."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        _make_dir_mtime(fx, 1_700_000_000.0)
        src = _make_source(fx, "doc.txt", "content")

        n1 = await cache.upsert_file(_proxy(src))
        assert n1.change_type == ChangeType.INSERT

        # Pass 2: looks_same() returns True — sentinel pins mtime
        n2 = await cache.upsert_file(_proxy(src))
        assert n2 is None, "looks_same() should return True with stable sentinel"

        # Pass 3: force=True overrides looks_same()
        n3 = await cache.upsert_file(_proxy(src), force=True)
        assert n3 is not None, "force=True should bypass looks_same()"
        assert n3.change_type == ChangeType.UPDATE

    @pytest.mark.asyncio
    async def test_force_restores_truncated_entry(self, tmp_path):
        """force=True on a truncated entry restores the full body."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        _make_dir_mtime(fx, 1_700_000_000.0)

        # State 1: insert as truncated using a LRTRUNCATE-encoded source filename
        src_trunc = _make_source(fx, "doc_LRTRUNCATE.txt", "body content")
        n1 = await cache.upsert_file(_proxy(src_trunc, ref_path="doc.txt"))
        assert n1 is not None
        assert n1.cur.is_truncated() is True

        # State 2: force restore using a KEEP proxy for the same ref_path
        src_keep = _make_source(fx, "doc.txt", "body content")
        n2 = await cache.upsert_file(_proxy(src_keep, ref_path="doc.txt"), force=True)

        assert n2 is not None, "force=True should trigger re-materialization"
        assert n2.change_type == ChangeType.UPDATE
        assert n2.cur.is_truncated() is False
        assert n2.cur.file_path.read_text() == "body content"


# ---------------------------------------------------------------------------
# Scenario 12: Orphaned temp file recovery
# ---------------------------------------------------------------------------

class TestOrphanCleanup:
    """
    fixtures/
    └── doc.txt                     ["content"]

    -> upsert_file(orphan_tempfile=True)
       INSERT returned; original mkstemp temp file remains in CFF temp root

    -> os.utime(orphan, (time.time() - 130, time.time() - 130))   # age past grace
    -> cache._storage.cleanup_temp_files(120)
    -> orphan no longer exists

    Note: orphan_tempfile=True causes deploy() to COPY (not move) the temp file
    to the staging location; the original mkstemp file stays in the temp root.
    cleanup_temp_files() iterates files (not subdirs) in the temp root.
    """

    @pytest.mark.asyncio
    async def test_orphaned_temp_file_cleaned_up(self, tmp_path):
        """Aged orphan in CFF temp root is deleted by cleanup_temp_files(120)."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "doc.txt", "content")

        proxy = MockNetworkFileProxy(src, orphan_tempfile=True)
        temp_root = cache.get_temp_directory_root()

        files_before = {f for f in temp_root.iterdir() if f.is_file()}
        await cache.upsert_file(proxy)
        files_after = {f for f in temp_root.iterdir() if f.is_file()}

        new_orphans = files_after - files_before
        assert len(new_orphans) == 1, (
            f"orphan_tempfile=True should leave exactly 1 orphan file, found: {new_orphans}"
        )
        orphan = next(iter(new_orphans))
        assert orphan.exists()

        # Age orphan past the 120-second grace period
        old_time = time.time() - 130
        os.utime(orphan, (old_time, old_time))

        cache._storage.cleanup_temp_files(120)

        assert not orphan.exists(), (
            "cleanup_temp_files(120) should have deleted the 130-second-old orphan"
        )


# ---------------------------------------------------------------------------
# Scenario 13: Concurrent materialization wall-clock test
# ---------------------------------------------------------------------------

class TestConcurrentMaterialization:
    """
    fixtures/
    ├── file0_RL50ms.txt
    ├── file1_RL50ms.txt
    ├── file2_RL50ms.txt
    ├── file3_RL50ms.txt
    └── file4_RL50ms.txt

    t0 = now()
    -> resync_bulk()  returns 5 INSERT notices
    elapsed = now() - t0

    assert elapsed < 0.350
    (serial would be 5 × 50ms = 250ms minimum before any overhead)
    """

    @pytest.mark.asyncio
    @very_lazy_test(_LAZY_DEPS, reverify_days=21)
    async def test_five_slow_proxies_run_concurrently(self, tmp_path):
        """resync_bulk materializes proxies concurrently, not sequentially."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()

        proxies = [
            _proxy(_make_source(fx, f"file{i}_RL50ms.txt", f"content {i}"))
            for i in range(5)
        ]

        t0 = time.monotonic()
        result = await cache.resync_bulk(proxies)
        elapsed = time.monotonic() - t0

        assert len(result.failures) == 0
        assert len(result.changes) == 5
        assert elapsed < 0.350, (
            f"5 concurrent 50ms proxies should finish in ~50ms+overhead, "
            f"not {elapsed * 1000:.0f}ms (serial floor is 250ms)"
        )


# ---------------------------------------------------------------------------
# Scenario 14: blocking_secs < latency — semantic clarification
# ---------------------------------------------------------------------------

class TestBlockingSecsSemantics:
    """
    fixtures/
    └── big_RL200ms.txt

    -> proxy.materialize(blocking_secs=0.001, temp_dir=...)
       returns True after ~200ms (full latency always awaited, blocking_secs ignored)

    This test encodes the current design decision: MockNetworkFileProxy always
    awaits the full _RL latency regardless of blocking_secs. If this is changed
    intentionally, update both this test and the module docstring.
    """

    @pytest.mark.asyncio
    @very_lazy_test(_LAZY_DEPS, reverify_days=21)
    async def test_blocking_secs_ignored_full_latency_awaited(self, tmp_path):
        """MockNetworkFileProxy always awaits full RL latency; blocking_secs is ignored."""
        fx = tmp_path / "fx"
        fx.mkdir()
        src = _make_source(fx, "big_RL200ms.txt")

        proxy = _proxy(src)
        work_dir = tmp_path / "tmp"
        work_dir.mkdir()

        t0 = time.monotonic()
        result = await proxy.materialize(blocking_secs=0.001, temp_dir=work_dir)
        elapsed = time.monotonic() - t0

        proxy.cleanup()

        assert result is True, "materialize() should return True after awaiting full latency"
        assert elapsed >= 0.200, (
            f"blocking_secs=0.001 should be ignored; "
            f"expected >= 200ms elapsed, got {elapsed * 1000:.1f}ms"
        )


# ---------------------------------------------------------------------------
# Scenario 15: Factory scan → resync_bulk end-to-end
# ---------------------------------------------------------------------------

class TestFactoryPipeline:
    """
    fixtures/
    ├── _DIR_MTIME.txt              {T=1700000000}
    ├── plain.txt                   ["plain file"]
    ├── big_FS10KB.txt              ["small source"]
    ├── trunc_LRTRUNCATE.txt        ["truncated"]
    └── skip_LREXCLUDE.txt          ["excluded"]

    factory.scan_files("fixtures/*.txt") yields 4 proxies (sentinel excluded)

    -> resync_bulk()  returns 3 changes, 0 failures
       (LREXCLUDE proxy filtered before upsert)

    -> trunc_LRTRUNCATE.txt  is truncated in cache
    -> big_FS10KB.txt  has on-disk size == 10 * 1024
    -> skip_LREXCLUDE.txt  not present in cache
    """

    @pytest.mark.asyncio
    async def test_factory_scan_then_resync_bulk(self, tmp_path):
        """Full factory-scan → resync_bulk pipeline with diverse encoded files."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()

        _make_dir_mtime(fx, 1_700_000_000.0)
        _make_source(fx, "plain.txt", "plain file")
        _make_source(fx, "big_FS10KB.txt", "small source")
        _make_source(fx, "trunc_LRTRUNCATE.txt", "truncated")
        _make_source(fx, "skip_LREXCLUDE.txt", "excluded")

        factory = MockNetworkFileProxyFactory()
        proxies = list(factory.scan_files(str(fx / "*.txt")))

        # Sentinel file must not be yielded as a proxy
        assert len(proxies) == 4, (
            f"expected 4 proxies (sentinel skipped), got {len(proxies)}: "
            f"{[p.ref_path() for p in proxies]}"
        )
        assert not any(_DIR_MTIME_FILENAME in p.ref_path() for p in proxies)

        result = await cache.resync_bulk(proxies)

        assert len(result.failures) == 0, f"unexpected failures: {result.failures}"
        assert len(result.changes) == 3, (
            f"expected 3 changes (LREXCLUDE filtered), got {len(result.changes)}: "
            f"{[c.ref_path for c in result.changes]}"
        )

        # Truncated entry: zero bytes on disk, valid sidecar
        trunc_notice = next(
            (c for c in result.changes if "trunc_LRTRUNCATE" in c.ref_path), None
        )
        assert trunc_notice is not None, "trunc_LRTRUNCATE.txt should have been inserted"
        assert trunc_notice.cur.is_truncated() is True

        # FS-overridden entry: on-disk size matches override
        big_notice = next(
            (c for c in result.changes if "big_FS10KB" in c.ref_path), None
        )
        assert big_notice is not None, "big_FS10KB.txt should have been inserted"
        assert big_notice.cur.file_path.stat().st_size == 10 * 1024

        # LREXCLUDE entry: not present in cache
        skip_proxies = [p for p in proxies if "skip" in p.ref_path()]
        assert len(skip_proxies) == 1
        assert cache.find_file(skip_proxies[0].ref_path()) is None


# ---------------------------------------------------------------------------
# Scenario 16: No-spurious-update stability
# ---------------------------------------------------------------------------

class TestNoSpuriousUpdate:
    """
    -- Pass 1 ---------------------------------------------------
    fixtures/
    ├── _DIR_MTIME.txt              {T=1700000000}
    └── report.txt                  ["original content"]   (16 bytes)

        -> upsert_file()  returns INSERT

    -- Pass 2 (identical proxy) ---------------------------------
        -> upsert_file()  returns no change

    -- Pass 3 (same size/mtime, different bytes) ----------------
    fixtures/
    └── report.txt                  ["different conten"]   <- edited (same 16 bytes)

        -> upsert_file()  returns no change
           (size+mtime comparison cannot detect same-size byte changes — known limitation)
    """

    @pytest.mark.asyncio
    async def test_no_spurious_update_on_re_sync(self, tmp_path):
        """Identical proxies produce no change notice; same-size content change is undetected."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()
        _make_dir_mtime(fx, 1_700_000_000.0)
        source = fx / "report.txt"

        content_a = "original content"   # 16 bytes
        content_b = "different conten"   # 16 bytes — same length, different content
        assert len(content_a.encode()) == len(content_b.encode())

        source.write_text(content_a)

        # Pass 1: initial insert
        n1 = await cache.upsert_file(_proxy(str(source)))
        assert n1 is not None
        assert n1.change_type == ChangeType.INSERT

        # Pass 2: identical proxy — no change expected
        n2 = await cache.upsert_file(_proxy(str(source)))
        assert n2 is None, "identical proxy should produce no change notice"

        # Pass 3: same byte count + sentinel mtime, different content — still no change
        source.write_text(content_b)
        n3 = await cache.upsert_file(_proxy(str(source)))
        assert n3 is None, (
            "size+mtime-only comparison cannot detect same-size content change "
            "(known limitation documented here as a regression guard)"
        )


# ---------------------------------------------------------------------------
# Scenario 17: LREXCLUDE proxy — previously cached entry swept after bulk sync
# ---------------------------------------------------------------------------

class TestLRExcludeSweep:
    """
    -- State 1 --------------------------------------------------
    fixtures/
    └── doc.txt                     ["data"]

        -> upsert_file(KEEP proxy, ref_path="doc.txt")  returns INSERT

    -- State 2 --------------------------------------------------
    fixtures/
    └── doc_LREXCLUDE.txt           ["data"]   <- new (ref_path="doc.txt")

        -> resync_bulk([EXCLUDE proxy], auto_delete=True)
           EXCLUDE proxy filtered — doc.txt not "touched" — swept on orchestrator exit

        -> cache.find_file("doc.txt")  returns None

    Note: auto_delete=True is the default. resync_bulk sweeps untouched files
    automatically on exit via the internal ResyncOrchestrator. No separate sweep
    call is needed. DELETE notices are filtered from result.changes.
    """

    @pytest.mark.asyncio
    async def test_exclude_proxy_sweeps_existing_entry(self, tmp_path):
        """LREXCLUDE proxy in resync_bulk causes the previously-cached entry to be swept."""
        cache = _make_cache(tmp_path)
        fx = tmp_path / "fx"
        fx.mkdir()

        # State 1: insert as KEEP — explicit ref_path so both proxies match
        src_keep = _make_source(fx, "doc.txt", "data")
        n1 = await cache.upsert_file(MockNetworkFileProxy(src_keep, ref_path="doc.txt"))
        assert n1 is not None
        assert n1.change_type == ChangeType.INSERT
        assert cache.find_file("doc.txt") is not None

        # State 2: resync with EXCLUDE proxy for the same ref_path
        src_excl = _make_source(fx, "doc_LREXCLUDE.txt", "data")
        exclude_proxy = MockNetworkFileProxy(src_excl, ref_path="doc.txt")
        result = await cache.resync_bulk([exclude_proxy], auto_delete=True)

        assert len(result.failures) == 0
        # DELETE notices are filtered from result.changes
        assert cache.find_file("doc.txt") is None, (
            "doc.txt should be swept: LREXCLUDE proxy filtered it from upsert, "
            "so it was never 'touched', and auto_delete=True swept it on orchestrator exit"
        )
