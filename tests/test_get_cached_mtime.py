# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import asyncio
import time
from pathlib import Path

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders, ChangeType
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy


@pytest.mark.asyncio
async def test_get_cached_mtime_target_only(tmp_path: Path):
    # Arrange: create cache and a source file
    root_dir = tmp_path / "cache_root"
    root_dir.mkdir(parents=True, exist_ok=True)
    cache = CachedFileFolders("cache/", str(root_dir))

    src = tmp_path / "source.txt"
    src.write_text("hello")
    proxy = LocalFileProxy(str(src), delete_after_deploy=False)

    # Act: upsert and query mtimes
    notice = await cache.upsert_file(proxy)
    assert notice is not None
    assert notice.change_type in (ChangeType.INSERT, ChangeType.UPDATE)

    target_mtime = cache.get_cached_mtime(proxy.ref_path(), None, includes="target_only")

    # Assert
    assert isinstance(target_mtime, float)
    assert target_mtime > 0


@pytest.mark.asyncio
async def test_get_cached_mtime_slave_only_and_both(tmp_path: Path):
    # Arrange
    root_dir = tmp_path / "cache_root"
    root_dir.mkdir(parents=True, exist_ok=True)
    cache = CachedFileFolders("cache/", str(root_dir))

    src = tmp_path / "doc.txt"
    src.write_text("v1")
    proxy = LocalFileProxy(str(src), delete_after_deploy=False)
    notice = await cache.upsert_file(proxy)
    assert notice is not None

    # Initially, slave dir is empty -> slave_files_only should be None
    slave_only_before = cache.get_cached_mtime(proxy.ref_path(), None, includes="slave_files_only")
    assert slave_only_before is None

    # Add files into the slave directory and ensure distinct mtimes
    slave_dir = notice.cur.slave_dir_path
    f1 = slave_dir / "a.json"
    f1.write_text("1")
    time.sleep(0.01)
    f2 = slave_dir / "b.log"
    f2.write_text("2")

    slave_only_after = cache.get_cached_mtime(proxy.ref_path(), None, includes="slave_files_only")
    both_after = cache.get_cached_mtime(proxy.ref_path(), None, includes="both")
    target_only = cache.get_cached_mtime(proxy.ref_path(), None, includes="target_only")

    # Assert: slave-only reflects the newest file in slave dir
    assert isinstance(slave_only_after, float)
    assert slave_only_after >= f2.stat().st_mtime

    # Assert: both is at least max(target, newest slave)
    assert isinstance(both_after, float)
    assert both_after >= max(target_only or 0.0, slave_only_after or 0.0)


@pytest.mark.asyncio
async def test_get_cached_mtime_missing_raises(tmp_path: Path):
    root_dir = tmp_path / "cache_root"
    root_dir.mkdir(parents=True, exist_ok=True)
    cache = CachedFileFolders("cache/", str(root_dir))

    with pytest.raises(ValueError):
        cache.get_cached_mtime("nonexistent.txt", None, includes="both")




