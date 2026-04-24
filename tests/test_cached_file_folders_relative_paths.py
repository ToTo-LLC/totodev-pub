# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for relative-path persistence in CachedFileFolders."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy


@pytest.mark.asyncio
async def test_new_entries_use_grouping_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()

    cache = CachedFileFolders("groups/{team}/", str(root))
    grouping = ("alpha",)

    source = tmp_path / "source.txt"
    source.write_text("hello world")

    proxy = LocalFileProxy(str(source), ref_path="docs/config.txt", delete_after_deploy=False)
    await cache.upsert_file(proxy, grouping)

    db = cache._storage.get_database(grouping)
    stored_path = db["docs/config.txt"]

    assert stored_path == "docs/config.txt"
    absolute_path = cache._storage._deserialize_stored_path(stored_path, grouping)
    expected_path = root / "groups" / "alpha" / "docs" / "config.txt"
    assert absolute_path == expected_path
    assert absolute_path.exists()

    db.close()
    cache.close()


@pytest.mark.asyncio
async def test_legacy_absolute_entry_still_resolves(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()

    cache = CachedFileFolders("groups/{team}/", str(root))
    grouping = ("legacy",)

    group_root = cache._storage._get_grouping_root_path(grouping, create=True)
    target_file = group_root / "legacy.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("legacy data")

    db = cache._storage.get_database(grouping)
    db["legacy.txt"] = str(target_file)

    file_ref = cache.find_file("legacy.txt", grouping)
    assert file_ref is not None
    assert file_ref.file_path == target_file

    all_files = list(cache.files(grouping))
    assert any(ref.file_path == target_file for ref in all_files)

    db.close()
    cache.close()


@pytest.mark.asyncio
async def test_portage_roundtrip_rewrites_absolute_entries(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()

    cache = CachedFileFolders("groups/{team}/", str(root))
    grouping = ("team",)

    source = tmp_path / "fresh.txt"
    source.write_text("fresh data")
    fresh_proxy = LocalFileProxy(str(source), ref_path="docs/fresh.txt", delete_after_deploy=False)
    await cache.upsert_file(fresh_proxy, grouping)

    group_root = cache._storage._get_grouping_root_path(grouping, create=True)
    legacy_file = group_root / "legacy.txt"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text("legacy data")

    db = cache._storage.get_database(grouping)
    db["legacy.txt"] = str(legacy_file)

    portage_path = cache.portage(grouping, include_metadata=False, force_rebuild=True)
    assert portage_path.exists()

    # Database is rebuilt lazily from portage file on next access
    db_rebuilt = cache._storage.get_database(grouping)
    stored_fresh = db_rebuilt["docs/fresh.txt"]
    stored_legacy = db_rebuilt["legacy.txt"]

    assert stored_fresh == "docs/fresh.txt"
    assert not Path(stored_legacy).is_absolute()
    assert cache._storage._deserialize_stored_path(stored_legacy, grouping) == legacy_file

    db_rebuilt.close()
    cache.close()


@pytest.mark.asyncio
async def test_manifest_relaxation_allows_root_move(tmp_path: Path) -> None:
    original_root = tmp_path / "original_cache"
    original_root.mkdir()

    cache = CachedFileFolders("groups/{team}/", str(original_root))
    grouping = ("migrated",)

    source = tmp_path / "migrated.txt"
    source.write_text("migrated data")
    proxy = LocalFileProxy(str(source), ref_path="migrated.txt", delete_after_deploy=False)
    await cache.upsert_file(proxy, grouping)

    cache.close()

    relocated_root = tmp_path / "relocated_cache"
    shutil.move(str(original_root), str(relocated_root))

    relocated_cache = CachedFileFolders("groups/{team}/", str(relocated_root))
    file_ref = relocated_cache.find_file("migrated.txt", grouping)
    assert file_ref is not None
    assert file_ref.file_path.exists()
    relocated_cache.close()

    # Ensure from_root also accepts the relocated manifest
    reopened_cache = CachedFileFolders.from_root(str(relocated_root))
    reopened_ref = reopened_cache.find_file("migrated.txt", grouping)
    assert reopened_ref is not None
    assert reopened_ref.file_path.exists()
    reopened_cache.close()

