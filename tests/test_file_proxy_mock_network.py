# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for MockNetworkFileProxy and MockNetworkFileProxyFactory.

Covers:
- Filename pattern parsing (_RL, _FS, _FF, _LR, _FAILS)
- Materialization: latency, size override, content copy
- Failure simulation: _FF countdown and _FAILS content toggle
- deploy(): move vs orphan_tempfile copy
- looks_same() with and without truncated-entry override_byte_count
- peek_metadata() with and without FS override
- local_retention_recommendation() for all three LR values
- touch() mtime override
- MockNetworkFileProxyFactory glob scanning
"""

import asyncio
import gc
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import pytest

from totodev_pub.pytest_tools import very_lazy_test
from totodev_pub.cached_file_folders_support.file_proxy_mock_network import (
    MockNetworkError,
    MockNetworkFileProxy,
    MockNetworkFileProxyFactory,
    _DIR_MTIME_FILENAME,
)
from totodev_pub.cached_file_folders_support.file_proxy_base import (
    LocalRetentionRecommendation,
)


_LAZY_DEPS = ['totodev_pub.cached_file_folders_support.file_proxy_mock_network']

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(tmpdir: Path, name: str, content: str = "hello test\n") -> str:
    """Create a source fixture file and return its path."""
    path = tmpdir / name
    path.write_text(content)
    return str(path)


def _make_dir_mtime(dirpath: Path, mtime: float) -> None:
    """Write a _DIR_MTIME.txt sentinel into dirpath."""
    (dirpath / _DIR_MTIME_FILENAME).write_text(str(mtime))


def _proxy(source_path: str, **kwargs) -> MockNetworkFileProxy:
    return MockNetworkFileProxy(source_path, **kwargs)


# ---------------------------------------------------------------------------
# Pattern parsing
# ---------------------------------------------------------------------------

class TestPatternParsing:
    """Verify that filename-encoded parameters are parsed correctly at construction."""

    def test_rl_seconds(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_RL5s.txt"))
        assert p._latency_secs == pytest.approx(5.0)

    def test_rl_deciseconds(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_RL3ds.txt"))
        assert p._latency_secs == pytest.approx(0.3)

    def test_rl_centiseconds(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_RL50cs.txt"))
        assert p._latency_secs == pytest.approx(0.5)

    def test_rl_milliseconds(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_RL100ms.txt"))
        assert p._latency_secs == pytest.approx(0.1)

    def test_rl_case_insensitive(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_rl200MS.txt"))
        assert p._latency_secs == pytest.approx(0.2)

    def test_rl_absent(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "plain.txt"))
        assert p._latency_secs == 0.0

    def test_fs_bytes(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_FS500B.txt"))
        assert p._override_size == 500

    def test_fs_kibibytes(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_FS4KB.txt"))
        assert p._override_size == 4 * 1024

    def test_fs_mebibytes(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_FS2MB.txt"))
        assert p._override_size == 2 * 1024 * 1024

    def test_fs_case_insensitive(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_fs8kb.txt"))
        assert p._override_size == 8 * 1024

    def test_fs_absent(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "plain.txt"))
        assert p._override_size is None

    def test_ff_count(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_FF3.txt"))
        assert p._failures_remaining == 3

    def test_ff_zero(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_FF0.txt"))
        assert p._failures_remaining == 0

    def test_ff_absent(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "plain.txt"))
        assert p._failures_remaining == 0

    def test_lr_truncate(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_LRTRUNCATE.txt"))
        assert p._local_retention == LocalRetentionRecommendation.TRUNCATE

    def test_lr_keep(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_LRKEEP.txt"))
        assert p._local_retention == LocalRetentionRecommendation.KEEP

    def test_lr_exclude(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_LREXCLUDE.txt"))
        assert p._local_retention == LocalRetentionRecommendation.EXCLUDE

    def test_lr_case_insensitive(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_lrtruncate.txt"))
        assert p._local_retention == LocalRetentionRecommendation.TRUNCATE

    def test_lr_absent_defaults_to_keep(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "plain.txt"))
        assert p._local_retention == LocalRetentionRecommendation.KEEP

    def test_fails_flag_detected(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "report_FAILS.txt"))
        assert p._has_fails is True

    def test_fails_flag_absent(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "plain.txt"))
        assert p._has_fails is False

    def test_fails_not_matched_inside_word(self, tmp_path):
        # _FAILSAFE should NOT trigger the _FAILS flag.
        p = _proxy(_make_source(tmp_path, "report_FAILSAFE.txt"))
        assert p._has_fails is False

    def test_multiple_patterns_combined(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "invoice_RL100ms_FS50KB_FF2_LRTRUNCATE.txt"))
        assert p._latency_secs == pytest.approx(0.1)
        assert p._override_size == 50 * 1024
        assert p._failures_remaining == 2
        assert p._local_retention == LocalRetentionRecommendation.TRUNCATE


# ---------------------------------------------------------------------------
# Materialization — basic
# ---------------------------------------------------------------------------

class TestMaterializationBasic:

    @pytest.mark.asyncio
    async def test_materialize_creates_temp_file(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "source content")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            result = await p.materialize(5.0, Path(td))
        assert result is True

    @pytest.mark.asyncio
    async def test_materialize_copies_source_content(self, tmp_path):
        content = "the quick brown fox"
        src = _make_source(tmp_path, "file.txt", content)
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            assert p._temp_path is not None
            assert Path(p._temp_path).read_text() == content

    @pytest.mark.asyncio
    async def test_materialize_fs_override_writes_x_content(self, tmp_path):
        src = _make_source(tmp_path, "file_FS1KB.txt", "tiny source")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            assert p._temp_path is not None
            data = Path(p._temp_path).read_bytes()
        assert len(data) == 1024
        assert data == b'X' * 1024

    @pytest.mark.asyncio
    @very_lazy_test(_LAZY_DEPS, reverify_days=21)
    async def test_materialize_fs_override_correct_size(self, tmp_path):
        src = _make_source(tmp_path, "file_FS2MB.txt", "small")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            size = os.path.getsize(p._temp_path)
        assert size == 2 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_materialize_preserves_source_mtime(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        source_mtime = os.stat(src).st_mtime
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            temp_mtime = os.stat(p._temp_path).st_mtime
        assert temp_mtime == pytest.approx(source_mtime, abs=1.0)

    @pytest.mark.asyncio
    async def test_materialize_idempotent(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            r1 = await p.materialize(5.0, Path(td))
            first_path = p._temp_path
            r2 = await p.materialize(5.0, Path(td))
        assert r1 is True
        assert r2 is True
        assert p._temp_path == first_path

    @pytest.mark.asyncio
    async def test_materialize_requires_temp_dir(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        with pytest.raises(ValueError, match="temp_dir"):
            await p.materialize(5.0, None)

    @pytest.mark.asyncio
    async def test_materialize_ext_preserved(self, tmp_path):
        src = _make_source(tmp_path, "document.pdf", "pdf-like")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            assert p._temp_path.endswith(".pdf")


# ---------------------------------------------------------------------------
# Materialization — latency
# ---------------------------------------------------------------------------

class TestMaterializationLatency:

    @pytest.mark.asyncio
    @very_lazy_test(_LAZY_DEPS, reverify_days=21)
    async def test_latency_honoured(self, tmp_path):
        src = _make_source(tmp_path, "file_RL50ms.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            t0 = time.monotonic()
            await p.materialize(5.0, Path(td))
            elapsed = time.monotonic() - t0
        assert elapsed >= 0.04  # 50 ms with some tolerance

    @pytest.mark.asyncio
    async def test_no_latency_still_async(self, tmp_path):
        # asyncio.sleep(0) should still yield without error.
        src = _make_source(tmp_path, "plain.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            result = await p.materialize(5.0, Path(td))
        assert result is True


# ---------------------------------------------------------------------------
# Forced failures (_FF)
# ---------------------------------------------------------------------------

class TestForcedFailures:

    @pytest.mark.asyncio
    async def test_ff_fails_then_succeeds(self, tmp_path):
        src = _make_source(tmp_path, "file_FF2.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            with pytest.raises(MockNetworkError):
                await p.materialize(5.0, Path(td))
            with pytest.raises(MockNetworkError):
                await p.materialize(5.0, Path(td))
            result = await p.materialize(5.0, Path(td))
        assert result is True

    @pytest.mark.asyncio
    async def test_ff_counter_decrements(self, tmp_path):
        src = _make_source(tmp_path, "file_FF3.txt")
        p = _proxy(src)
        assert p._failures_remaining == 3
        with tempfile.TemporaryDirectory() as td:
            with pytest.raises(MockNetworkError):
                await p.materialize(5.0, Path(td))
        assert p._failures_remaining == 2

    @pytest.mark.asyncio
    async def test_ff_zero_succeeds_immediately(self, tmp_path):
        src = _make_source(tmp_path, "file_FF0.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            result = await p.materialize(5.0, Path(td))
        assert result is True


# ---------------------------------------------------------------------------
# Content-driven failures (_FAILS)
# ---------------------------------------------------------------------------

class TestFailsContentToggle:

    @pytest.mark.asyncio
    async def test_fails_raises_when_content_starts_with_fail(self, tmp_path):
        src = _make_source(tmp_path, "report_FAILS.txt", "FAIL - server down\n")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            with pytest.raises(MockNetworkError, match="FAIL"):
                await p.materialize(5.0, Path(td))

    @pytest.mark.asyncio
    async def test_fails_succeeds_when_content_ok(self, tmp_path):
        src = _make_source(tmp_path, "report_FAILS.txt", "OK - all good\n")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            result = await p.materialize(5.0, Path(td))
        assert result is True

    @pytest.mark.asyncio
    async def test_fails_toggle_by_editing_content(self, tmp_path):
        src_path = tmp_path / "report_FAILS.txt"
        src_path.write_text("FAIL - initial state\n")
        p = _proxy(str(src_path))

        with tempfile.TemporaryDirectory() as td:
            # First attempt: fails
            with pytest.raises(MockNetworkError):
                await p.materialize(5.0, Path(td))

            # "Fix" the file by changing content
            src_path.write_text("OK - recovered\n")

            # Second attempt: succeeds
            result = await p.materialize(5.0, Path(td))
        assert result is True

    @pytest.mark.asyncio
    async def test_no_fails_flag_ignores_content(self, tmp_path):
        # File has FAIL content but no _FAILS in name → should succeed.
        src = _make_source(tmp_path, "report.txt", "FAIL - server down\n")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            result = await p.materialize(5.0, Path(td))
        assert result is True

    @pytest.mark.asyncio
    async def test_fails_flag_fail_check_before_content(self, tmp_path):
        # Both _FAILS (content=FAIL) and _FF3 present: _FAILS fires first.
        src = _make_source(tmp_path, "file_FAILS_FF3.txt", "FAIL - down\n")
        p = _proxy(src)
        assert p._failures_remaining == 3
        with tempfile.TemporaryDirectory() as td:
            with pytest.raises(MockNetworkError):
                await p.materialize(5.0, Path(td))
        # _FF counter should NOT have been decremented (FAILS fired first).
        assert p._failures_remaining == 3


# ---------------------------------------------------------------------------
# deploy()
# ---------------------------------------------------------------------------

class TestDeploy:

    @pytest.mark.asyncio
    async def test_deploy_moves_temp_file(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            temp_path = p._temp_path
            p.deploy(str(target_dir))
        # Default (move): temp file should be gone.
        assert not os.path.exists(temp_path)
        assert (target_dir / "file.txt").exists()

    @pytest.mark.asyncio
    async def test_deploy_orphan_copies_leaves_temp(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src, orphan_tempfile=True)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        # Use a persistent subdir (under tmp_path) so the orphan survives past
        # the deploy call.  pytest owns tmp_path and cleans up the whole tree
        # at test teardown — including the orphan — so no manual os.remove needed.
        td = tmp_path / "tempwork"
        td.mkdir()
        await p.materialize(5.0, td)
        temp_path = p._temp_path
        p.deploy(str(target_dir))
        assert os.path.exists(temp_path)
        assert (target_dir / "file.txt").exists()

    @pytest.mark.asyncio
    async def test_deploy_applies_mtime(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        fixed_mtime = 1_700_000_000.0
        p = _proxy(src, init_mtime=fixed_mtime)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))
        deployed_mtime = (target_dir / "file.txt").stat().st_mtime
        assert deployed_mtime == pytest.approx(fixed_mtime, abs=1.0)

    @pytest.mark.asyncio
    async def test_deploy_dev_null_discards(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            temp_path = p._temp_path
            p.deploy("/dev/null")
        assert not os.path.exists(temp_path)
        assert p._was_deployed is True

    def test_deploy_without_materialize_raises(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with pytest.raises(RuntimeError, match="materialized"):
            p.deploy(str(target_dir))

    @pytest.mark.asyncio
    async def test_deploy_twice_raises(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))
            with pytest.raises(RuntimeError, match="already been deployed"):
                p.deploy(str(target_dir))


# ---------------------------------------------------------------------------
# looks_same()
# ---------------------------------------------------------------------------

class TestLooksSame:

    @pytest.mark.asyncio
    async def test_looks_same_true_for_identical_deployed_file(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "same content")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))
        cached_path = str(target_dir / "file.txt")
        assert p.looks_same(cached_path) is True

    @pytest.mark.asyncio
    async def test_looks_same_false_after_source_touch(self, tmp_path):
        src_path = tmp_path / "file.txt"
        src_path.write_text("original content")
        p = _proxy(str(src_path))
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))

        # Advance source mtime by touching it.
        future_mtime = src_path.stat().st_mtime + 10.0
        os.utime(str(src_path), (future_mtime, future_mtime))

        cached_path = str(target_dir / "file.txt")
        assert p.looks_same(cached_path) is False

    def test_looks_same_nonexistent_other_returns_none(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p.looks_same("/nonexistent/path/file.txt") is None

    @pytest.mark.asyncio
    async def test_looks_same_with_fs_override_and_override_byte_count(self, tmp_path):
        src = _make_source(tmp_path, "file_FS1KB.txt", "tiny")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))

        cached_path = str(target_dir / "file_FS1KB.txt")
        # Simulate a truncated entry: the on-disk file is zero bytes but we pass
        # the original size as override_byte_count.
        cached_path_path = Path(cached_path)
        original_mtime = cached_path_path.stat().st_mtime
        cached_path_path.write_bytes(b'')  # zero out (simulate truncation)
        os.utime(cached_path, (original_mtime, original_mtime))

        assert p.looks_same(cached_path, override_byte_count=1024) is True

    @pytest.mark.asyncio
    async def test_looks_same_fs_override_size_mismatch(self, tmp_path):
        src = _make_source(tmp_path, "file_FS1KB.txt", "tiny")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))

        cached_path = str(target_dir / "file_FS1KB.txt")
        # Pass wrong size → should report different.
        assert p.looks_same(cached_path, override_byte_count=512) is False


# ---------------------------------------------------------------------------
# peek_metadata()
# ---------------------------------------------------------------------------

class TestPeekMetadata:

    @pytest.mark.asyncio
    async def test_peek_returns_source_size_without_override(self, tmp_path):
        content = "hello"
        src = _make_source(tmp_path, "file.txt", content)
        p = _proxy(src)
        meta = await p.peek_metadata()
        assert meta is not None
        assert meta.size == len(content)

    @pytest.mark.asyncio
    async def test_peek_returns_override_size_with_fs(self, tmp_path):
        src = _make_source(tmp_path, "file_FS4KB.txt", "tiny")
        p = _proxy(src)
        meta = await p.peek_metadata()
        assert meta is not None
        assert meta.size == 4 * 1024

    @pytest.mark.asyncio
    async def test_peek_returns_source_mtime(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        source_mtime = os.stat(src).st_mtime
        p = _proxy(src)
        meta = await p.peek_metadata()
        assert meta is not None
        assert meta.mtime == pytest.approx(source_mtime, abs=1.0)

    @pytest.mark.asyncio
    async def test_peek_returns_init_mtime_override(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        fixed_mtime = 1_600_000_000.0
        p = _proxy(src, init_mtime=fixed_mtime)
        meta = await p.peek_metadata()
        assert meta is not None
        assert meta.mtime == pytest.approx(fixed_mtime, abs=1.0)


# ---------------------------------------------------------------------------
# local_retention_recommendation()
# ---------------------------------------------------------------------------

class TestLocalRetentionRecommendation:

    def test_default_is_keep(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "plain.txt"))
        assert p.local_retention_recommendation() == LocalRetentionRecommendation.KEEP

    def test_truncate(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_LRTRUNCATE.txt"))
        assert p.local_retention_recommendation() == LocalRetentionRecommendation.TRUNCATE

    def test_keep_explicit(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_LRKEEP.txt"))
        assert p.local_retention_recommendation() == LocalRetentionRecommendation.KEEP

    def test_exclude(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file_LREXCLUDE.txt"))
        assert p.local_retention_recommendation() == LocalRetentionRecommendation.EXCLUDE


# ---------------------------------------------------------------------------
# touch()
# ---------------------------------------------------------------------------

class TestTouch:

    def test_touch_sets_mtime_override(self, tmp_path):
        p = _proxy(_make_source(tmp_path, "file.txt"))
        p.touch(1_500_000_000.0)
        assert p._mtime_override == pytest.approx(1_500_000_000.0)

    @pytest.mark.asyncio
    async def test_touch_after_materialize_updates_temp_file(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            new_mtime = 1_500_000_000.0
            p.touch(new_mtime)
            assert p._temp_path is not None
            actual = os.stat(p._temp_path).st_mtime
        assert actual == pytest.approx(new_mtime, abs=1.0)

    @pytest.mark.asyncio
    async def test_touch_reflected_in_peek_metadata(self, tmp_path):
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src)
        new_mtime = 1_500_000_000.0
        p.touch(new_mtime)
        meta = await p.peek_metadata()
        assert meta is not None
        assert meta.mtime == pytest.approx(new_mtime, abs=1.0)


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------

class TestCleanup:

    @pytest.mark.asyncio
    async def test_cleanup_removes_temp_file(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            temp_path = p._temp_path
            assert os.path.exists(temp_path)
            p.cleanup()
        assert not os.path.exists(temp_path)

    @pytest.mark.asyncio
    async def test_cleanup_idempotent(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.cleanup()
            p.cleanup()  # should not raise


# ---------------------------------------------------------------------------
# ref_path and get_context_info
# ---------------------------------------------------------------------------

class TestRefPathAndContextInfo:

    def test_ref_path_defaults_to_source_path(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p.ref_path() == src

    def test_ref_path_custom(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src, ref_path="custom/logical/file.txt")
        assert p.ref_path() == "custom/logical/file.txt"

    def test_get_context_info_keys(self, tmp_path):
        src = _make_source(tmp_path, "file_RL100ms_FS1KB.txt")
        p = _proxy(src)
        info = p.get_context_info()
        assert info["proxy_type"] == "MockNetworkFileProxy"
        assert "source_path" in info
        assert "latency_secs" in info
        assert "override_size" in info

    def test_retrieval_hint_contains_source_path(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        hint = p.retrieval_hint()
        assert hint["source_path"] == src


# ---------------------------------------------------------------------------
# _DIR_MTIME.txt sentinel
# ---------------------------------------------------------------------------

class TestDirMtime:

    SENTINEL_MTIME = 1_700_000_000.0

    @pytest.mark.asyncio
    async def test_dir_mtime_applied_to_peek_metadata(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src)
        meta = await p.peek_metadata()
        assert meta is not None
        assert meta.mtime == pytest.approx(self.SENTINEL_MTIME)

    @pytest.mark.asyncio
    async def test_dir_mtime_stamped_on_materialized_file(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src)
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            temp_mtime = os.stat(p._temp_path).st_mtime
        assert temp_mtime == pytest.approx(self.SENTINEL_MTIME, abs=1.0)

    @pytest.mark.asyncio
    async def test_dir_mtime_used_in_looks_same(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt", "content")
        p = _proxy(src)
        target_dir = tmp_path / "cache"
        target_dir.mkdir()
        with tempfile.TemporaryDirectory() as td:
            await p.materialize(5.0, Path(td))
            p.deploy(str(target_dir))
        # Deployed file has the sentinel mtime — looks_same should agree.
        assert p.looks_same(str(target_dir / "file.txt")) is True

    def test_dir_mtime_parsed_at_construction(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p._dir_mtime == pytest.approx(self.SENTINEL_MTIME)

    def test_no_sentinel_dir_mtime_is_none(self, tmp_path):
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p._dir_mtime is None

    def test_init_mtime_overrides_dir_mtime(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt")
        explicit_mtime = 1_600_000_000.0
        p = _proxy(src, init_mtime=explicit_mtime)
        assert p._effective_mtime() == pytest.approx(explicit_mtime)

    def test_touch_overrides_dir_mtime(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        p.touch(1_600_000_000.0)
        assert p._effective_mtime() == pytest.approx(1_600_000_000.0)

    def test_dir_mtime_not_applied_to_files_in_other_directory(self, tmp_path):
        subdir_a = tmp_path / "a"
        subdir_a.mkdir()
        subdir_b = tmp_path / "b"
        subdir_b.mkdir()
        _make_dir_mtime(subdir_a, self.SENTINEL_MTIME)
        src_b = _make_source(subdir_b, "file.txt")
        p = _proxy(src_b)
        assert p._dir_mtime is None

    def test_dir_mtime_malformed_content_ignored(self, tmp_path):
        (tmp_path / _DIR_MTIME_FILENAME).write_text("not-a-number\n")
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p._dir_mtime is None

    def test_dir_mtime_empty_file_ignored(self, tmp_path):
        (tmp_path / _DIR_MTIME_FILENAME).write_text("")
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p._dir_mtime is None

    def test_dir_mtime_in_context_info(self, tmp_path):
        _make_dir_mtime(tmp_path, self.SENTINEL_MTIME)
        src = _make_source(tmp_path, "file.txt")
        p = _proxy(src)
        assert p.get_context_info()["dir_mtime"] == pytest.approx(self.SENTINEL_MTIME)


# ---------------------------------------------------------------------------
# MockNetworkFileProxyFactory
# ---------------------------------------------------------------------------

class TestMockNetworkFileProxyFactory:

    def test_scan_files_yields_proxies(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        factory = MockNetworkFileProxyFactory()
        proxies = list(factory.scan_files(str(tmp_path / "*.txt")))
        assert len(proxies) == 2
        assert all(isinstance(p, MockNetworkFileProxy) for p in proxies)

    def test_scan_files_skips_directories(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "file.txt").write_text("content")
        factory = MockNetworkFileProxyFactory()
        proxies = list(factory.scan_files(str(tmp_path / "*")))
        assert len(proxies) == 1

    def test_scan_files_picks_up_filename_params(self, tmp_path):
        (tmp_path / "doc_RL200ms_LRTRUNCATE.txt").write_text("data")
        factory = MockNetworkFileProxyFactory()
        proxies = list(factory.scan_files(str(tmp_path / "*.txt")))
        assert len(proxies) == 1
        p = proxies[0]
        assert p._latency_secs == pytest.approx(0.2)
        assert p._local_retention == LocalRetentionRecommendation.TRUNCATE

    def test_factory_orphan_tempfile_propagated(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")
        factory = MockNetworkFileProxyFactory(orphan_tempfile=True)
        proxies = list(factory.scan_files(str(tmp_path / "*.txt")))
        assert len(proxies) == 1
        assert proxies[0]._orphan_tempfile is True

    def test_scan_files_batched(self, tmp_path):
        for i in range(5):
            (tmp_path / f"file{i}.txt").write_text("x")
        factory = MockNetworkFileProxyFactory()
        batches = list(factory.scan_files_batched(str(tmp_path / "*.txt"), batch_size=2))
        total = sum(len(b) for b in batches)
        assert total == 5
        assert len(batches) == 3  # 2 + 2 + 1

    def test_scan_files_empty_pattern_raises(self, tmp_path):
        factory = MockNetworkFileProxyFactory()
        with pytest.raises(ValueError):
            list(factory.scan_files(""))

    def test_scan_files_skips_dir_mtime_sentinel(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")
        _make_dir_mtime(tmp_path, 1_700_000_000.0)
        factory = MockNetworkFileProxyFactory()
        proxies = list(factory.scan_files(str(tmp_path / "*")))
        names = [os.path.basename(p.ref_path()) for p in proxies]
        assert _DIR_MTIME_FILENAME not in names
        assert len(proxies) == 1

    def test_scan_files_applies_dir_mtime_to_yielded_proxies(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        sentinel_mtime = 1_700_000_000.0
        _make_dir_mtime(tmp_path, sentinel_mtime)
        factory = MockNetworkFileProxyFactory()
        proxies = list(factory.scan_files(str(tmp_path / "*.txt")))
        assert len(proxies) == 2
        for p in proxies:
            assert p._dir_mtime == pytest.approx(sentinel_mtime)
