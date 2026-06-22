# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Offline tests for the Zoho WorkDrive TRUNCATE / summarize / index demo.

The Zoho-specific code (HTTP/OAuth) is intentionally NOT exercised here. Instead the
source-agnostic core -- `RetentionPolicy`, `StubVectorIndex`, and the async
`SummaryIndexer` change-receiver -- is driven through a real `CachedFileFolders` cache
using the library's `MockNetworkFileProxy` (filename-encoded `_FS<n>KB/MB` sizes). An
instrumented proxy subclass (a) drives the cache's truncation decision from OUR
`RetentionPolicy` and (b) counts `materialize()` calls so we can prove the
"never materialize" backstops for media and over-ceiling files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_base import LocalRetentionRecommendation
from totodev_pub.cached_file_folders_support.file_proxy_mock_network import MockNetworkFileProxy
from totodev_pub.cached_file_folders_support.sync_types import ChangeType
from totodev_pub.cached_file_folders_support.examples.retention_policy import RetentionPolicy
from totodev_pub.cached_file_folders_support.examples.stub_vector_index import (
    StubEmbedder,
    StubVectorIndex,
)
from totodev_pub.cached_file_folders_support.examples.summary_indexer import SummaryIndexer

# A tuple grouping key (not a list) is required so CachedFileFolders.files() takes the
# exact-match path; a list is interpreted as a pattern filter and breaks sweep deletes.
GROUP = ("demo",)
GROUPING_PATTERN = "key-{dir_key}/"
MIB = 1024 * 1024


def _pandoc_available() -> bool:
    try:
        import pypandoc
        pypandoc.get_pandoc_version()
        return True
    except Exception:
        return False


class _PolicyMockProxy(MockNetworkFileProxy):
    """MockNetworkFileProxy whose retention is decided by OUR policy and that counts fetches."""

    def __init__(self, source_path: str, policy: RetentionPolicy, ref_path: Optional[str] = None,
                 init_mtime: Optional[float] = None) -> None:
        super().__init__(source_path, ref_path=ref_path, init_mtime=init_mtime)
        self._policy = policy
        self.materialize_calls = 0

    def _size(self) -> int:
        # Deliberately reads MockNetworkFileProxy's internal `_override_size` (the size
        # encoded in the `_FS<n>KB/MB` filename) so OUR policy sees the same simulated size
        # the cache does. Coupled to the base mock on purpose; this is test-only code.
        if self._override_size is not None:
            return self._override_size
        return os.stat(self._source_path).st_size

    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        return self._policy.recommend(os.path.basename(self._source_path), self._size())

    async def materialize(self, blocking_secs, temp_dir=None):
        self.materialize_calls += 1
        return await super().materialize(blocking_secs, temp_dir)


def _make_source(tmp_path: Path, name: str, content: str = "hello demo content\n") -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def _new_cache(tmp_path: Path) -> CachedFileFolders:
    return CachedFileFolders(
        grouping_pattern=GROUPING_PATTERN,
        root_dir=str(tmp_path / "cache"),
        use_xxhash=False,
    )


def _new_indexer(cache: CachedFileFolders, vec_path: Path) -> SummaryIndexer:
    index = StubVectorIndex(vec_path, embedder=StubEmbedder())
    return SummaryIndexer(
        cache=cache,
        grouping_key=GROUP,
        policy=RetentionPolicy(),
        vector_index=index,
        source="test",
    )


async def _sync(cache: CachedFileFolders, indexer: SummaryIndexer, proxies: List) -> dict:
    grouping = cache.grouping(GROUP)
    result = await grouping.resync_bulk(
        file_proxies=list(proxies),
        change_receiver=indexer.on_change,
        max_concurrent_requests=1,
    )
    by_ref = {}
    for change in result.changes:
        by_ref[change.ref_path] = change.change_type
    return by_ref


def _read_index_json(cache: CachedFileFolders, ref_path: str) -> dict:
    import json
    slave = cache.get_slave_dir(GROUP, ref_path)
    return json.loads((Path(slave) / "index.json").read_text())


# ---------------------------------------------------------------------------
# RetentionPolicy unit coverage
# ---------------------------------------------------------------------------

class TestRetentionPolicy:
    def test_small_text_keeps(self):
        p = RetentionPolicy()
        assert p.recommend("a.txt", 10 * 1024) == LocalRetentionRecommendation.KEEP
        assert p.may_materialize("a.txt", 10 * 1024) is True

    def test_large_text_truncates_but_fetchable(self):
        p = RetentionPolicy()
        assert p.recommend("a.txt", 60 * 1024) == LocalRetentionRecommendation.TRUNCATE
        assert p.may_materialize("a.txt", 60 * 1024) is True

    def test_media_truncates_never_materialize(self):
        p = RetentionPolicy()
        assert p.recommend("clip.mp4", 1024) == LocalRetentionRecommendation.TRUNCATE
        assert p.may_materialize("clip.mp4", 1024) is False
        assert p.media_kind("clip.mp4") == "video"
        assert p.media_kind("song.mp3") == "audio"

    def test_over_ceiling_never_materialize(self):
        p = RetentionPolicy()
        assert p.recommend("huge.txt", 150 * MIB) == LocalRetentionRecommendation.TRUNCATE
        assert p.may_materialize("huge.txt", 150 * MIB) is False

    def test_opaque_binary_never_materialized(self):
        p = RetentionPolicy()
        # Large archive: truncates by size AND must never be fetched to summarize.
        assert p.recommend("bundle.zip", 34 * MIB) == LocalRetentionRecommendation.TRUNCATE
        assert p.may_materialize("bundle.zip", 34 * MIB) is False
        assert p.is_opaque_binary("bundle.zip") is True
        assert p.summarize_by_filename_only("bundle.zip") is True
        # Small archive: KEPT as a normal mirror file, but still filename-only to summarize.
        assert p.recommend("tiny.zip", 4 * 1024) == LocalRetentionRecommendation.KEEP
        assert p.may_materialize("tiny.zip", 4 * 1024) is False
        # A few more opaque types; container docs are NOT opaque.
        assert p.is_opaque_binary("disk.iso") is True
        assert p.is_opaque_binary("setup.exe") is True
        assert p.is_opaque_binary("report.docx") is False

    def test_media_size_classes(self):
        p = RetentionPolicy()
        assert p.classify_media_size(10 * MIB) == "small"
        assert p.classify_media_size(100 * MIB) == "medium"
        assert p.classify_media_size(300 * MIB) == "large"


# ---------------------------------------------------------------------------
# extract_root_folder_id URL parsing
# ---------------------------------------------------------------------------

class TestExtractRootFolderId:
    """The CLI tolerates a pasted WorkDrive URL in place of a bare folder id."""

    def _fn(self):
        from totodev_pub.cached_file_folders_support.examples.zoho_workdrive_sync import (
            extract_root_folder_id,
        )
        return extract_root_folder_id

    def test_bare_id_passes_through(self):
        f = self._fn()
        assert f("q8hgxe014e7b400254200a61e328f5552e149") == "q8hgxe014e7b400254200a61e328f5552e149"

    def test_bare_id_is_stripped(self):
        assert self._fn()("  abc123  ") == "abc123"

    def test_folder_url_returns_folder_id(self):
        f = self._fn()
        url = "https://workdrive.zoho.com/home/teams/T/ws/WS123/folders/FOLDER456"
        assert f(url) == "FOLDER456"

    def test_folder_url_with_query_and_fragment(self):
        f = self._fn()
        url = "https://workdrive.zoho.com/ws/WS123/folders/FOLDER456?x=1#frag"
        assert f(url) == "FOLDER456"

    def test_files_landing_falls_back_to_workspace(self):
        f = self._fn()
        url = ("https://workdrive.zoho.com/q8team/teams/q8team/ws/"
               "q8hgxe014e7b400254200a61e328f5552e149/folders/files")
        assert f(url) == "q8hgxe014e7b400254200a61e328f5552e149"

    def test_singular_folder_path(self):
        assert self._fn()("https://workdrive.zoho.com/folder/ID789?a=b") == "ID789"


# ---------------------------------------------------------------------------
# StubVectorIndex unit coverage
# ---------------------------------------------------------------------------

class TestStubVectorIndex:
    def test_upsert_remove_query(self, tmp_path):
        idx = StubVectorIndex(tmp_path / "vec.json", embedder=StubEmbedder())
        idx.upsert("a", "alpha beta gamma")
        idx.upsert("b", "delta epsilon")
        assert "a" in idx and len(idx) == 2
        results = idx.query("alpha beta gamma", k=2)
        assert results[0][0] == "a"  # deterministic: exact text ranks itself first
        assert idx.remove("a") is True
        assert "a" not in idx and len(idx) == 1

    def test_persistence_reload(self, tmp_path):
        path = tmp_path / "vec.json"
        StubVectorIndex(path, embedder=StubEmbedder()).upsert("x", "hello world")
        reloaded = StubVectorIndex(path, embedder=StubEmbedder())
        assert "x" in reloaded


# ---------------------------------------------------------------------------
# SummaryIndexer integration via the cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_text_truncated_and_summarized_via_transient_body(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/report.txt"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "report_FS60KB.txt"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    changes = await _sync(cache, indexer, [proxy])
    assert changes[ref] == ChangeType.INSERT

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and cached.is_truncated()

    record = _read_index_json(cache, ref)
    assert record["retention"] == "truncate"
    assert record["body_inspected"] is True
    assert record["summary_method"] in ("text_first_2k", "pandoc_first_2k")
    assert (Path(cached.slave_dir_path) / "summary.md").exists()
    assert ref in indexer.vector_index
    # exactly one transient fetch by the indexer (cache only peeked for a truncate)
    assert proxy.materialize_calls == 1


@pytest.mark.asyncio
async def test_small_text_kept_and_summarized_from_local_body(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/small.txt"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "small_FS10KB.txt"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    changes = await _sync(cache, indexer, [proxy])
    assert changes[ref] == ChangeType.INSERT

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and not cached.is_truncated()

    record = _read_index_json(cache, ref)
    assert record["retention"] == "keep"
    assert record["body_inspected"] is True
    assert ref in indexer.vector_index


@pytest.mark.asyncio
async def test_media_is_metadata_only_and_never_materialized(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/clip.mp4"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "clip_FS300MB.mp4"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and cached.is_truncated()

    record = _read_index_json(cache, ref)
    assert record["body_inspected"] is False
    assert record["summary_method"] == "metadata_only"
    assert record["media_kind"] == "video"
    assert record["size_class"] == "large"
    # the crux: a 300 MB media body was never downloaded
    assert proxy.materialize_calls == 0


@pytest.mark.asyncio
async def test_over_ceiling_text_never_materialized(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/huge.txt"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "huge_FS150MB.txt"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and cached.is_truncated()

    record = _read_index_json(cache, ref)
    assert record["body_inspected"] is False
    assert record["summary_method"] == "metadata_only"
    # ceiling overrides the otherwise-fetchable text band
    assert proxy.materialize_calls == 0


@pytest.mark.asyncio
async def test_large_archive_truncated_metadata_only_never_materialized(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/bundle.zip"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "bundle_FS34MB.zip"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and cached.is_truncated()

    record = _read_index_json(cache, ref)
    assert record["retention"] == "truncate"
    assert record["body_inspected"] is False
    assert record["summary_method"] == "metadata_only"
    assert "zip" in record["summary"].lower()
    # the crux: a 34 MB archive was never downloaded just to (fail to) summarize it
    assert proxy.materialize_calls == 0
    assert ref in indexer.vector_index


@pytest.mark.asyncio
async def test_small_archive_kept_but_summarized_by_filename_only(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/tiny.zip"
    # Small enough to be KEPT as a normal mirror file (no _FS override).
    proxy = _PolicyMockProxy(_make_source(tmp_path, "tiny.zip", content="PK\x03\x04 tiny"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and not cached.is_truncated()  # body kept on disk

    record = _read_index_json(cache, ref)
    assert record["retention"] == "keep"
    # ...but we did NOT inspect the body to summarize it -- filename only.
    assert record["body_inspected"] is False
    assert record["summary_method"] == "metadata_only"
    # The single materialize is the cache storing the mirror body (KEEP); the summarizer
    # adds NO extra fetch on top -- it summarized from the filename, not the bytes.
    assert proxy.materialize_calls == 1


@pytest.mark.asyncio
async def test_update_re_derives_and_upserts(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/report.txt"
    src = _make_source(tmp_path, "report_FS60KB.txt")

    await _sync(cache, indexer, [_PolicyMockProxy(src, policy, ref_path=ref, init_mtime=1700000000.0)])
    first = _read_index_json(cache, ref)

    # Same ref, newer mtime -> UPDATE.
    changes = await _sync(cache, indexer, [_PolicyMockProxy(src, policy, ref_path=ref, init_mtime=1700009999.0)])
    assert changes[ref] == ChangeType.UPDATE

    second = _read_index_json(cache, ref)
    assert second["indexed_at"] >= first["indexed_at"]
    assert ref in indexer.vector_index


@pytest.mark.asyncio
async def test_delete_removes_entry_slave_dir_and_index_row(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/small.txt"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "small_FS10KB.txt"),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])
    cached = cache.find_file(ref, GROUP)
    slave_dir = Path(cached.slave_dir_path)
    assert slave_dir.exists()
    assert ref in indexer.vector_index

    # Empty sweep -> the previously cached entry is deleted. (resync_bulk's returned
    # `changes` intentionally omits DELETEs, but the DELETE change_receiver still fires;
    # the vector-index removal below proves it ran.)
    await _sync(cache, indexer, [])

    assert cache.find_file(ref, GROUP) is None
    assert not slave_dir.exists()
    assert ref not in indexer.vector_index


@pytest.mark.asyncio
@pytest.mark.skipif(not _pandoc_available(), reason="pandoc/pypandoc not installed")
async def test_non_text_uses_pandoc(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/note.rtf"
    # Small, real RTF body (no _FS override) so it is KEPT and read from local disk.
    rtf = r"{\rtf1\ansi\deff0 Hello from a 2023 RTF document.}"
    proxy = _PolicyMockProxy(_make_source(tmp_path, "note.rtf", content=rtf),
                             policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])
    record = _read_index_json(cache, ref)
    assert record["summary_method"] == "pandoc_first_2k"
    assert record["body_inspected"] is True


class _NativeDocMockProxy(_PolicyMockProxy):
    """A proxy that advertises itself as a Zoho-native doc via the duck-typed flag.

    SummaryIndexer._is_native_doc() reads `is_native_doc` off the proxy (getattr), so a
    proxy only has to expose the attribute -- this exercises that source-agnostic seam.
    """

    is_native_doc = True


@pytest.mark.asyncio
async def test_native_doc_summarized_by_filename_only_never_materialized(tmp_path):
    cache = _new_cache(tmp_path)
    indexer = _new_indexer(cache, tmp_path / "vec.json")
    policy = RetentionPolicy()
    ref = "zohowd://demo/proposal.zwriter"
    # Sized over the truncate threshold so the cache truncates (peek only, no fetch).
    proxy = _NativeDocMockProxy(_make_source(tmp_path, "proposal_FS60KB.zwriter"),
                                policy, ref_path=ref, init_mtime=1700000000.0)

    await _sync(cache, indexer, [proxy])

    cached = cache.find_file(ref, GROUP)
    assert cached is not None and cached.is_truncated()

    record = _read_index_json(cache, ref)
    assert record["is_native_doc"] is True
    assert record["body_inspected"] is False
    assert record["summary_method"] == "metadata_only"
    assert "native" in record["summary"].lower()
    # the crux: a native doc is never downloaded -- not even to measure it
    assert proxy.materialize_calls == 0
    assert ref in indexer.vector_index
