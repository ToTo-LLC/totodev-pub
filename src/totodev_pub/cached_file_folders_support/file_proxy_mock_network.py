# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Mock network file proxy for testing CachedFileFolders with simulated remote behavior.

Contains:
- MockNetworkError
- MockNetworkFileProxy
- MockNetworkFileProxyFactory
"""

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, Generator, Optional

from .file_proxy_base import FileProxyBase, LocalRetentionRecommendation, OriginMetadata


# Sentinel filename for the directory-level mtime override.
_DIR_MTIME_FILENAME = "_DIR_MTIME.txt"

# Compiled regexes for filename-encoded test parameters (all case-insensitive).
_RE_RL = re.compile(r'_RL(\d+)(d|m|c|)s', re.IGNORECASE)
_RE_FS = re.compile(r'_FS(\d+)(K|M|)B', re.IGNORECASE)
_RE_FF = re.compile(r'_FF(\d+)', re.IGNORECASE)
_RE_LR = re.compile(r'_LR(TRUNCATE|KEEP|EXCLUDE)', re.IGNORECASE)
_RE_FAILS = re.compile(r'_FAILS(?=[^A-Za-z]|$)', re.IGNORECASE)

# Unit multipliers for _RL: '' = seconds, 'd' = deciseconds, 'c' = centiseconds, 'm' = milliseconds
_RL_MULTIPLIERS: Dict[str, float] = {'': 1.0, 'd': 0.1, 'c': 0.01, 'm': 0.001}

# Unit multipliers for _FS: '' = bytes, 'k' = kibibytes, 'm' = mebibytes
_FS_MULTIPLIERS: Dict[str, int] = {'': 1, 'k': 1024, 'm': 1024 * 1024}

_LR_MAP = {
    'keep': LocalRetentionRecommendation.KEEP,
    'truncate': LocalRetentionRecommendation.TRUNCATE,
    'exclude': LocalRetentionRecommendation.EXCLUDE,
}


def _parse_latency(basename: str) -> float:
    m = _RE_RL.search(basename)
    if not m:
        return 0.0
    return int(m.group(1)) * _RL_MULTIPLIERS[m.group(2).lower()]


def _parse_override_size(basename: str) -> Optional[int]:
    m = _RE_FS.search(basename)
    if not m:
        return None
    return int(m.group(1)) * _FS_MULTIPLIERS[m.group(2).lower()]


def _parse_forced_failures(basename: str) -> int:
    m = _RE_FF.search(basename)
    return int(m.group(1)) if m else 0


def _parse_local_retention(basename: str) -> Optional[LocalRetentionRecommendation]:
    m = _RE_LR.search(basename)
    return _LR_MAP[m.group(1).lower()] if m else None


def _has_fails_flag(basename: str) -> bool:
    return bool(_RE_FAILS.search(basename))


def _read_dir_mtime(source_path: str) -> Optional[float]:
    """Read the _DIR_MTIME.txt sentinel from the parent directory of source_path.

    Returns the parsed float timestamp, or None if the sentinel is absent or unreadable.
    Malformed content is silently ignored so a bad sentinel never breaks proxy construction.
    """
    sentinel = os.path.join(os.path.dirname(os.path.abspath(source_path)), _DIR_MTIME_FILENAME)
    try:
        with open(sentinel, 'r') as f:
            return float(f.readline().strip())
    except (OSError, IOError, ValueError):
        return None


class MockNetworkError(RuntimeError):
    """
    Raised by MockNetworkFileProxy to simulate retrieval failures.

    Triggered when the _FF forced-failure counter fires, or when the _FAILS flag
    is set and the source file's first 30 bytes contain "FAIL".
    """


class MockNetworkFileProxy(FileProxyBase):
    """
    A file proxy backed by a local fixture file that behaves like a remote network
    resource, designed for testing ``CachedFileFolders`` sync flows without a real
    network connection.

    -----------------------------------------------------------------------
    THE CORE IDEA
    -----------------------------------------------------------------------

    Real-world proxies (SharePoint, Gmail, SFTP) are slow, stateful, and hard to
    script for regression tests.  ``MockNetworkFileProxy`` replaces them with
    ordinary files sitting in a local *fixture directory*.  The filename of each
    fixture encodes the mock behaviour you need — latency, file size, failure
    mode, retention hint — so a single directory tree can describe a rich test
    scenario without any per-file Python setup code.

    ``LocalFileProxy`` already reads local files, but it materialises instantly
    and produces no temp file, making it impossible to test timing, orphaned-file
    recovery, or truncation paths.  ``MockNetworkFileProxy`` adds:

    * An ``asyncio.sleep()`` during ``materialize()`` to simulate download latency,
      making concurrent-materialisation tests realistic.
    * A real temporary file that lives between ``materialize()`` and ``deploy()``,
      enabling tests of the orphaned-temp-file recovery path in
      ``CachedFileFolders``.
    * A content-driven failure toggle (``_FAILS``) so a single test can drive a
      failure → edit-file → recovery workflow without reinitialising any proxies.
    * A directory-level mtime sentinel (``_DIR_MTIME.txt``) that pins a stable
      mtime across git checkouts and CI runs, preventing flaky change-detection
      tests.

    -----------------------------------------------------------------------
    TYPICAL TEST WORKFLOW
    -----------------------------------------------------------------------

    1.  **Create a fixture directory** containing ordinary text or binary files.
        The directory lives under your project's ``tests/fixtures/`` tree so it
        is committed to version control along with the tests that use it.

    2.  **Name each file** using the encoding conventions below.  The name
        controls latency, size, failure behaviour, and retention hints; the
        file content is used as-is (or replaced with synthetic ``'X'`` bytes when
        ``_FS`` is encoded).

    3.  **Optionally add ``_DIR_MTIME.txt``** to the directory containing a single
        Unix timestamp.  All proxies in that directory will report and stamp that
        mtime instead of each file's actual filesystem mtime.

    4.  **Scan with** ``MockNetworkFileProxyFactory`` to get a list of proxies and
        pass them to ``CachedFileFolders.resync_bulk()`` or ``upsert_file()``.

    5.  **Edit a fixture file** to change its behaviour mid-test.  Rename it (to
        change encoded parameters) or edit its first 30 bytes (to toggle the
        ``_FAILS`` switch) without touching any proxy or cache objects.

    -----------------------------------------------------------------------
    EXAMPLE FIXTURE DIRECTORY
    -----------------------------------------------------------------------

    ::

        tests/fixtures/email_inbox/
        │
        │   # Plain file — no special behaviour, fast materialisation.
        ├── welcome.eml
        │
        │   # 200 ms simulated download latency.
        ├── newsletter_RL200ms.eml
        │
        │   # 50 ms latency + fixed 100 KB size (content replaced with 'X' bytes).
        │   # Useful for testing truncation without committing a real 100 KB file.
        ├── report_RL50ms_FS100KB.eml
        │
        │   # Proxy recommends local truncation; 100 ms latency.
        ├── confidential_LRTRUNCATE_RL100ms.eml
        │
        │   # Fails the first 2 materialise attempts, then succeeds.
        │   # Tests CachedFileFolders retry / error-recovery paths.
        ├── flaky_attachment_FF2_RL100ms.pdf
        │
        │   # Content-driven failure toggle (see _FAILS section below).
        │   # Currently starts with "FAIL ..." so materialise raises MockNetworkError.
        │   # Edit the file to start with "OK ..." to make it succeed.
        ├── broken_server_FAILS.eml
        │
        │   # Excluded from the cache entirely (resync_bulk filters it out).
        ├── draft_LREXCLUDE.eml
        │
        │   # Pins a stable mtime for ALL files in this directory.
        │   # Content: a single Unix timestamp, e.g.  1700000000
        └── _DIR_MTIME.txt

    -----------------------------------------------------------------------
    FILENAME ENCODING CONVENTIONS  (all patterns are case-insensitive)
    -----------------------------------------------------------------------

    Every encoded parameter is parsed once at construction from
    ``os.path.basename(source_path)``.  Multiple parameters may be combined
    freely in a single filename.

    ``_RL<n>[d|c|m]s``  — **Retrieval Latency**
        ``asyncio.sleep()`` is called for this duration inside ``materialize()``.
        Unit suffixes: ``ds`` = deciseconds (×0.1 s), ``cs`` = centiseconds
        (×0.01 s), ``ms`` = milliseconds (×0.001 s), bare ``s`` = seconds.
        Examples: ``_RL5s`` (5 s), ``_RL200ms`` (0.2 s), ``_RL50cs`` (0.5 s).
        Default: 0 (``asyncio.sleep(0)`` is still called to keep materialisation
        consistently async even without an encoded delay).

    ``_FS<n>[K|M]B``  — **File Size Override**
        When present, ``materialize()`` produces a file of exactly this many
        bytes filled with the character ``'X'``, regardless of the actual size of
        the source file on disk.  ``peek_metadata()`` also reports this size.
        Unit suffixes: ``KB`` = kibibytes (×1024), ``MB`` = mebibytes (×1024²),
        bare ``B`` = bytes.
        Examples: ``_FS500B``, ``_FS4KB``, ``_FS2MB``.
        Default: absent — source file content is copied as-is.

    ``_FF<n>``  — **Forced Failures**
        ``materialize()`` raises ``MockNetworkError`` for the first *n* calls,
        then succeeds.  The counter is per-instance and decrements on each
        failure, so a single proxy object enforces exactly *n* failures before
        becoming permanently successful.
        Example: ``_FF3`` — fails on calls 1, 2, and 3; succeeds on call 4.
        Default: absent (0 forced failures).

    ``_LR(TRUNCATE|KEEP|EXCLUDE)``  — **Local Retention Recommendation**
        Sets the value returned by ``local_retention_recommendation()``.
        ``CachedFileFolders`` uses this to decide whether to keep the full file
        body, truncate it to zero bytes with a sidecar, or exclude it from the
        cache altogether.
        Examples: ``_LRTRUNCATE``, ``_LRKEEP``, ``_LREXCLUDE``.
        Default: absent → ``LocalRetentionRecommendation.KEEP``.

    ``_FAILS``  — **Content-Driven Failure Toggle** (see section below)
        Marks the file as a failure-capable fixture.  The actual failure is
        controlled by the *content* of the source file, not the filename.
        Default: absent (content is never inspected).

    -----------------------------------------------------------------------
    _DIR_MTIME.txt — STABLE DIRECTORY-LEVEL MTIME
    -----------------------------------------------------------------------

    File mtimes are not preserved by ``git clone`` or ``git checkout``, so
    fixture files arrive with the checkout timestamp rather than their original
    mtime.  Any test that exercises ``looks_same()`` or mtime-based change
    detection will produce different results on every machine or CI run unless
    mtimes are explicitly stabilised.

    Drop a file named exactly ``_DIR_MTIME.txt`` into a fixture directory.  Its
    first line must be a Unix timestamp (integer or float)::

        1700000000

    Every ``MockNetworkFileProxy`` whose ``source_path`` lives in that directory
    will use this value instead of the actual file mtime — in
    ``peek_metadata()``, in the mtime stamped on the materialised/deployed file,
    and in ``looks_same()`` comparisons.

    Scope: **immediate parent directory only** (not recursive).
    The sentinel file itself is skipped by ``MockNetworkFileProxyFactory`` and
    never yielded as a proxy.

    Mtime precedence (highest → lowest):
      1. Explicit ``init_mtime`` constructor argument or ``touch()`` call
      2. ``_DIR_MTIME.txt`` sentinel in the parent directory
      3. Actual ``os.stat(source_path).st_mtime``

    -----------------------------------------------------------------------
    _FAILS — CONTENT-DRIVEN FAILURE TOGGLE
    -----------------------------------------------------------------------

    When ``_FAILS`` appears anywhere in the filename, ``materialize()``
    reads the **first 30 bytes** of the source file *at call time* (after
    the ``_RL`` sleep, before writing the temp file).  If those bytes contain
    the string ``"FAIL"`` (exact case, all caps), ``MockNetworkError`` is raised.
    Otherwise materialisation proceeds normally.

    This design lets you toggle the failure state by editing the source file
    content — no proxy re-creation, no parameter change::

        # tests/fixtures/broken_server_FAILS.eml — currently failing:
        FAIL - smtp.example.com unreachable since 09:14
        ... rest of the email content ...

        # Simulate the server recovering — just overwrite the first line:
        OK - smtp.example.com restored at 09:47
        ... rest of the email content ...

    A single test can therefore drive a full "failure → recovery" lifecycle by
    writing to the fixture file between two calls to ``resync_bulk()``, without
    touching any proxy or cache object.

    The ``_FAILS`` check fires **after** the ``_RL`` sleep, so failure scenarios
    also exercise the latency path.  If both ``_FAILS`` (content = ``"FAIL"``)
    and ``_FF<n>`` are encoded in the same filename, ``_FAILS`` fires first and
    the ``_FF`` counter is not decremented.

    Designed for future extensibility: additional keywords in the first 30 bytes
    (e.g. ``"TIMEOUT"``, ``"NOAUTH"``) can be mapped to distinct exception
    subtypes in later versions without changing the filename convention.

    -----------------------------------------------------------------------
    FULL WORKING EXAMPLE
    -----------------------------------------------------------------------

    ::

        import asyncio, tempfile
        from pathlib import Path
        from totodev_pub.cached_file_folders import CachedFileFolders
        from totodev_pub.cached_file_folders_support.file_proxy_mock_network import (
            MockNetworkFileProxyFactory, MockNetworkError,
        )

        async def test_sync_failure_then_recovery(tmp_path):
            cache = CachedFileFolders("inbox/{account}/", str(tmp_path / "cache"))
            factory = MockNetworkFileProxyFactory()
            fixture_dir = Path("tests/fixtures/email_inbox")

            # ── Round 1: broken_server_FAILS.eml starts with "FAIL ..." ──────────
            proxies = list(factory.scan_files(str(fixture_dir / "*.eml")))
            result = await cache.resync_bulk(
                proxies, ["inbox", "work"],
                error_policy=ResyncBulkErrorPolicy.RETAIN_OLD,
            )
            assert result.error_count == 1   # broken_server_FAILS raised

            # ── "Fix" the server by editing the fixture file ──────────────────────
            (fixture_dir / "broken_server_FAILS.eml").write_text(
                "OK - smtp.example.com restored\\n... email body ..."
            )

            # ── Round 2: all files succeed ────────────────────────────────────────
            proxies = list(factory.scan_files(str(fixture_dir / "*.eml")))
            result = await cache.resync_bulk(proxies, ["inbox", "work"])
            assert result.error_count == 0
    """

    def __init__(
        self,
        source_path: str,
        ref_path: Optional[str] = None,
        orphan_tempfile: bool = False,
        init_mtime: Optional[float] = None,
    ):
        """
        Args:
            source_path: Path to the backing fixture file on disk.
            ref_path: Logical identifier used as the cache key (defaults to source_path).
            orphan_tempfile: If True, deploy() copies instead of moves the temp file,
                leaving an orphan behind to test recovery paths in CachedFileFolders.
            init_mtime: Mtime (POSIX timestamp) stamped onto the deployed file.
                None means inherit the source file's actual mtime.
        """
        self._source_path = source_path
        self._ref_path = ref_path if ref_path is not None else source_path
        self._orphan_tempfile = orphan_tempfile
        self._mtime_override: Optional[float] = init_mtime
        self._dir_mtime: Optional[float] = _read_dir_mtime(source_path)

        basename = os.path.basename(source_path)
        self._latency_secs: float = _parse_latency(basename)
        self._override_size: Optional[int] = _parse_override_size(basename)
        self._failures_remaining: int = _parse_forced_failures(basename)
        self._local_retention: LocalRetentionRecommendation = (
            _parse_local_retention(basename) or LocalRetentionRecommendation.KEEP
        )
        self._has_fails: bool = _has_fails_flag(basename)

        self._temp_path: Optional[str] = None
        self._was_deployed = False
        self._materialization_started = False
        self._materialization_completed = False

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # FileProxyBase interface                                              #
    # ------------------------------------------------------------------ #

    def ref_path(self) -> str:
        return self._ref_path

    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """
        Simulate async retrieval: sleep for the encoded latency, check failure conditions,
        then create a temp file.

        Args:
            blocking_secs: Timeout hint (respected for in-progress polls; the initial
                materialization always runs to completion).
            temp_dir: Required — directory for the temp file.
        """
        if temp_dir is None or str(temp_dir).strip() in ('', '.'):
            raise ValueError("temp_dir must be provided and non-blank for MockNetworkFileProxy")

        if self._materialization_completed:
            return True

        if self._materialization_started:
            if blocking_secs > 0:
                await asyncio.sleep(min(0.1, blocking_secs))
            return self._materialization_completed

        self._materialization_started = True
        try:
            await self._do_materialize(temp_dir)
            self._materialization_completed = True
            return True
        except Exception:
            self._materialization_started = False
            raise

    def deploy(self, target_dir: str) -> None:
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
        if not self._materialization_completed or self._temp_path is None:
            raise RuntimeError("File must be materialized before deployment")

        if target_dir == "/dev/null":
            if os.path.exists(self._temp_path):
                os.remove(self._temp_path)
            self._was_deployed = True
            return

        if not os.path.isdir(target_dir):
            raise RuntimeError(f"Target directory does not exist: {target_dir}")

        filename = os.path.basename(self._ref_path)
        target_path = os.path.join(target_dir, filename)
        try:
            if self._orphan_tempfile:
                shutil.copy2(self._temp_path, target_path)
            else:
                shutil.move(self._temp_path, target_path)

            effective_mtime = self._effective_mtime()
            if effective_mtime is not None:
                os.utime(target_path, (effective_mtime, effective_mtime))

            self._was_deployed = True
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to deploy file to {target_dir}: {e}")

    def looks_same(self, other_fpath: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
        try:
            source_stat = os.stat(self._source_path)
            self_size = self._override_size if self._override_size is not None else source_stat.st_size
            self_mtime = self._effective_mtime()
            if self_mtime is None:
                return None

            other_stat = os.stat(other_fpath)
            other_size = override_byte_count if override_byte_count is not None else other_stat.st_size

            return self_size == other_size and self_mtime == other_stat.st_mtime
        except (OSError, IOError):
            return None

    async def peek_metadata(self) -> Optional[OriginMetadata]:
        try:
            st = os.stat(self._source_path)
            size = self._override_size if self._override_size is not None else st.st_size
            return OriginMetadata(size=size, mtime=self._effective_mtime())
        except (OSError, IOError):
            return None

    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        return self._local_retention

    def retrieval_hint(self) -> Dict[str, Any]:
        return {
            "source_path": self._source_path,
            "override_size": self._override_size,
            "latency_secs": self._latency_secs,
        }

    def get_context_info(self) -> Dict[str, Any]:
        return {
            "proxy_type": "MockNetworkFileProxy",
            "source_path": self._source_path,
            "ref_path": self._ref_path,
            "orphan_tempfile": self._orphan_tempfile,
            "latency_secs": self._latency_secs,
            "override_size": self._override_size,
            "failures_remaining": self._failures_remaining,
            "local_retention": self._local_retention.value,
            "has_fails_flag": self._has_fails,
            "mtime_override": self._mtime_override,
            "dir_mtime": self._dir_mtime,
            "temp_path": self._temp_path,
            "was_deployed": self._was_deployed,
            "materialization_started": self._materialization_started,
            "materialization_completed": self._materialization_completed,
        }

    # ------------------------------------------------------------------ #
    # Convenience methods                                                  #
    # ------------------------------------------------------------------ #

    def touch(self, mtime: float) -> None:
        """Override the mtime applied to the materialized/deployed file."""
        self._mtime_override = mtime
        if self._temp_path and os.path.exists(self._temp_path):
            os.utime(self._temp_path, (mtime, mtime))

    def cleanup(self) -> None:
        """Remove the temp file if it still exists (not yet deployed or orphaned)."""
        if self._temp_path and os.path.exists(self._temp_path):
            try:
                os.remove(self._temp_path)
            except (OSError, IOError):
                pass
            finally:
                self._temp_path = None

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _effective_mtime(self) -> Optional[float]:
        """Return the mtime to stamp on the materialized file.

        Precedence: explicit init_mtime/touch() > _DIR_MTIME.txt sentinel > actual file stat.
        """
        if self._mtime_override is not None:
            return self._mtime_override
        if self._dir_mtime is not None:
            return self._dir_mtime
        try:
            return os.stat(self._source_path).st_mtime
        except OSError:
            return None

    async def _do_materialize(self, temp_dir: Path) -> None:
        """Sleep → check failures → create temp file."""
        # asyncio.sleep(0) still yields even when there is no encoded latency,
        # keeping materialization consistently async.
        await asyncio.sleep(self._latency_secs)

        if self._has_fails:
            self._check_fails_content()

        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise MockNetworkError(
                f"Forced failure for testing "
                f"(failures remaining after this: {self._failures_remaining})"
            )

        ext = os.path.splitext(self._source_path)[1]
        temp_fd, temp_path = tempfile.mkstemp(suffix=ext, dir=str(temp_dir))
        try:
            # Close the fd immediately so we can use higher-level file APIs without
            # ownership ambiguity.
            os.close(temp_fd)
            temp_fd = -1

            if self._override_size is not None:
                Path(temp_path).write_bytes(b'X' * self._override_size)
            else:
                shutil.copy2(self._source_path, temp_path)

            effective_mtime = self._effective_mtime()
            if effective_mtime is not None:
                os.utime(temp_path, (effective_mtime, effective_mtime))

            self._temp_path = temp_path
        except Exception:
            if temp_fd >= 0:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _check_fails_content(self) -> None:
        """Read the first 30 bytes of the source file; raise MockNetworkError if they contain 'FAIL'."""
        try:
            with open(self._source_path, 'rb') as f:
                header = f.read(30)
        except (OSError, IOError) as e:
            raise MockNetworkError(f"Cannot read source file for _FAILS check: {e}")

        header_text = header.decode('utf-8', errors='replace')
        if 'FAIL' in header_text:
            raise MockNetworkError(
                f"Content-driven failure: source file begins with 'FAIL' "
                f"(first 30 chars: {header_text!r})"
            )


class MockNetworkFileProxyFactory:
    """
    Factory that scans a local directory tree using glob patterns and yields
    MockNetworkFileProxy objects for each matching file.

    Mirrors LocalFileProxyFactory — point it at a fixture directory and get
    properly configured mock proxies, with all test parameters living in the
    filenames themselves.

    Constructor-level ``orphan_tempfile`` applies to all yielded proxies; per-file
    filename-encoded parameters (_RL, _FS, _FF, _LR, _FAILS) are picked up
    automatically.

    Example usage::

        factory = MockNetworkFileProxyFactory()
        for proxy in factory.scan_files("tests/fixtures/email_sync/**/*"):
            await cache.upsert_file(proxy, ["email", "inbox"])
    """

    def __init__(self, orphan_tempfile: bool = False) -> None:
        self._orphan_tempfile = orphan_tempfile

    def scan_files(
        self,
        pattern: str,
        follow_symlinks: bool = False,
    ) -> Generator[MockNetworkFileProxy, None, None]:
        """
        Yield MockNetworkFileProxy objects for files matching the glob pattern.

        Args:
            pattern: Glob pattern (relative or absolute), e.g. ``"fixtures/**/*.pdf"``.
            follow_symlinks: Whether to follow symbolic links (default False).
        """
        if not pattern or not pattern.strip():
            raise ValueError("Pattern must be non-empty")

        pattern = pattern.strip()
        try:
            if os.path.isabs(pattern):
                pattern_path = Path(pattern)
                search_dir = None
                for parent in pattern_path.parents:
                    if parent.exists() and parent.is_dir():
                        search_dir = parent
                        search_pattern = str(pattern_path.relative_to(parent))
                        break
                if search_dir is None:
                    raise OSError(f"Cannot find valid base directory for pattern: {pattern}")
                matches = list(search_dir.glob(search_pattern))
            else:
                matches = list(Path(".").glob(pattern))

            for match in matches:
                if match.is_dir():
                    continue
                if not follow_symlinks and match.is_symlink():
                    continue
                if match.name.lower() == _DIR_MTIME_FILENAME.lower():
                    continue
                yield MockNetworkFileProxy(
                    source_path=str(match.resolve()),
                    orphan_tempfile=self._orphan_tempfile,
                )
        except OSError:
            raise
        except Exception as e:
            raise OSError(f"Error scanning files with pattern '{pattern}': {e}")

    def scan_files_batched(
        self,
        pattern: str,
        batch_size: int = 100,
        follow_symlinks: bool = False,
    ) -> Generator[list, None, None]:
        """
        Yield batches of MockNetworkFileProxy objects.

        Args:
            pattern: Glob pattern for file matching.
            batch_size: Number of proxies per batch (default 100).
            follow_symlinks: Whether to follow symbolic links.
        """
        batch: list = []
        for proxy in self.scan_files(pattern, follow_symlinks=follow_symlinks):
            batch.append(proxy)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
