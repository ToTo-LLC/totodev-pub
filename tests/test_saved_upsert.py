# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
from typing import List
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy
from totodev_pub.cached_file_folders_support.saved_upsert import SavedUpsert


@pytest.mark.asyncio
async def test_saved_upsert_capture_and_upsert(tmp_path):
    cache_root = tmp_path / "cache_root"
    cache = CachedFileFolders("groups/{team}/", str(cache_root))

    original = tmp_path / "original.txt"
    original.write_text("hello world", encoding="utf-8")

    proxy = LocalFileProxy(str(original), ref_path="files/original.txt")
    initial_notice = await cache.upsert_file(proxy, grouping_key=("alpha",))
    assert initial_notice is not None and initial_notice.cur is not None

    slave_note = initial_notice.cur.slave_dir_path / "note.txt"
    slave_note.write_text("metadata", encoding="utf-8")

    saved: List[SavedUpsert] = []

    def receiver(notice, _):
        saved.append(
            SavedUpsert.from_change_notice(
                cache,
                notice,
                new_ref_path="archive/original.txt",
                preserve_slave_dir=True,
            )
        )

    await cache.delete_file("files/original.txt", ("alpha",), change_receiver=receiver)
    assert len(saved) == 1
    assert not cache.file_exists("files/original.txt", ("alpha",))

    saved_upsert = saved[0]
    change = await saved_upsert.upsert(cache, grouping_key=("alpha",), force=True)
    assert change is not None and change.cur is not None

    restored = cache.find_file("archive/original.txt", ("alpha",))
    assert restored is not None
    assert restored.file_path.read_text(encoding="utf-8") == "hello world"
    assert (restored.slave_dir_path / "note.txt").read_text(encoding="utf-8") == "metadata"


@pytest.mark.asyncio
async def test_saved_upsert_discard(tmp_path):
    cache_root = tmp_path / "cache_root"
    cache = CachedFileFolders("groups/{team}/", str(cache_root))

    original = tmp_path / "to_discard.txt"
    original.write_text("bye", encoding="utf-8")

    proxy = LocalFileProxy(str(original), ref_path="files/to_discard.txt")
    await cache.upsert_file(proxy, grouping_key=("beta",))

    saved: List[SavedUpsert] = []

    def receiver(notice, _):
        saved.append(SavedUpsert.from_change_notice(cache, notice, preserve_slave_dir=False))

    await cache.delete_file("files/to_discard.txt", ("beta",), change_receiver=receiver)
    saved_upsert = saved[0]

    temp_file = saved_upsert._file_path
    assert temp_file.exists()

    saved_upsert.discard()
    assert not temp_file.exists()
    assert not cache.file_exists("files/to_discard.txt", ("beta",))

