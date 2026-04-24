# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import asyncio
import os
from pathlib import Path

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders


@pytest.mark.asyncio
async def test_move_file_intra_group_preserves_filename(tmp_path: Path):
    root = tmp_path / "cache_root"
    root.mkdir()
    cache = CachedFileFolders("projects/{project}/", str(root))

    # Prepare a source file to upsert
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    original_file = src_dir / "original.pdf"
    original_file.write_text("ORIGINAL_CONTENT")

    grouping = ("webapp",)
    await cache.upsert_file(str(original_file), grouping)
    # Find the actual ref_path that was stored (LocalFileProxy uses the source path by default)
    old_ref = None
    for fr in cache.files(grouping):
        if fr.file_path.name == original_file.name:
            old_ref = fr.ref_path
            break
    assert old_ref is not None
    # Place a marker in the slave dir to ensure it moves
    slave_dir = cache.get_slave_dir(grouping, old_ref)  # old_ref exists due to upsert
    (slave_dir / "marker.txt").write_text("marker")

    # Move within same grouping; ref tail looks like a filename, but filename must be preserved
    new_ref = "a/c/looks-like-name.txt"
    moved = cache.move_file(old_ref, new_ref, grouping_key=grouping)
    assert moved.ref_path == new_ref
    assert moved.grouping_key == grouping
    # Filename preserved
    assert moved.file_path.name == "original.pdf"
    assert moved.file_path.exists()
    # Slave dir moved and marker preserved
    assert moved.slave_dir_path.exists()
    assert (moved.slave_dir_path / "marker.txt").read_text() == "marker"
    # Old location cleaned
    assert not (root / "projects/webapp/a/b").exists() or len(list((root / "projects/webapp/a/b").glob("**/*"))) == 0


@pytest.mark.asyncio
async def test_move_file_cross_group(tmp_path: Path):
    root = tmp_path / "cache_root"
    root.mkdir()
    cache = CachedFileFolders("projects/{project}/", str(root))

    # Prepare a source file to upsert
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    original_file = src_dir / "report.csv"
    original_file.write_text("alpha,beta,gamma")

    src_group = ("g1",)
    dst_group = ("g2",)
    await cache.upsert_file(str(original_file), src_group)
    # Determine actual ref
    old_ref = None
    for fr in cache.files(src_group):
        if fr.file_path.name == original_file.name:
            old_ref = fr.ref_path
            break
    assert old_ref is not None

    moved = cache.move_file(old_ref, "reports/2025/pretend.csv", grouping_key=src_group, new_grouping_key=dst_group)
    # Check destination grouping and filename preserved
    assert moved.grouping_key == dst_group
    assert moved.file_path.name == "report.csv"
    assert moved.file_path.exists()
    # Ensure source DB no longer has old ref
    assert cache.find_file(old_ref, src_group) is None


@pytest.mark.asyncio
async def test_move_file_destination_exists_behavior(tmp_path: Path):
    root = tmp_path / "cache_root"
    root.mkdir()
    cache = CachedFileFolders("projects/{project}/", str(root))

    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    f1 = src_dir / "one.txt"
    f2 = src_dir / "two.txt"
    f1.write_text("ONE")
    f2.write_text("TWO")

    grouping = ("g",)
    await cache.upsert_file(str(f1), grouping)
    await cache.upsert_file(str(f2), grouping)
    # Determine refs
    ref_src = None
    ref_two = None
    for fr in cache.files(grouping):
        if fr.file_path.name == "one.txt":
            ref_src = fr.ref_path
        if fr.file_path.name == "two.txt":
            ref_two = fr.ref_path
    assert ref_src is not None and ref_two is not None
    # Create a destination ref by moving the second file there
    ref_dst = "path/dst.txt"
    cache.move_file(ref_two, ref_dst, grouping_key=grouping)
    # Now try moving f1 into ref_dst without overwrite: should raise
    with pytest.raises(ValueError):
        cache.move_file(ref_src, ref_dst, grouping_key=grouping, overwrite=False)
    # With overwrite=True: succeeds and content is from f1
    moved = cache.move_file(ref_src, ref_dst, grouping_key=grouping, overwrite=True)
    assert moved.file_path.read_text() == "ONE"


@pytest.mark.asyncio
async def test_move_file_filenamey_tail_does_not_rename(tmp_path: Path):
    root = tmp_path / "cache_root"
    root.mkdir()
    cache = CachedFileFolders("flat/", str(root))

    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    source = src_dir / "data.bin"
    source.write_bytes(b"\x00\x01")

    await cache.upsert_file(str(source), grouping_key=None)
    # Obtain ref that points to our file
    current_ref = None
    for fr in cache.files():
        if fr.file_path.name == "data.bin":
            current_ref = fr.ref_path
            break
    assert current_ref is not None

    new_ref = "some/path/looks-like-name.json"
    moved = cache.move_file(current_ref, new_ref, grouping_key=None)
    assert moved.ref_path == new_ref
    assert moved.file_path.name == "data.bin"
    assert moved.file_path.exists()


