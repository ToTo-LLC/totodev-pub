# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
import shutil
from pathlib import Path
from typing import Optional

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy
from totodev_pub.cached_file_folders_support.file_proxy_cache_grouping import (
    CacheGroupingFileProxyFactory,
)


def _clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _volatile_test_root() -> Path:
    # Place test artifacts under volatile/ as per project rules
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "volatile" / "tmp" / "test_cache_grouping_file_proxy"


def _create_cache(pattern: str, root_dir: Path) -> CachedFileFolders:
    return CachedFileFolders(pattern, str(root_dir))


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _touch_like(src: Path, dst: Path) -> None:
    # Utility for verifying mtime/size in assertions if needed
    shutil.copy2(src, dst)


@pytest.mark.asyncio
async def test_scan_and_upsert_with_per_file_slave_copy():
    root = _volatile_test_root()
    _clean_dir(root)

    # Create two caches with grouping pattern
    source_cache_root = root / "source_cache"
    dest_cache_root = root / "dest_cache"
    source = _create_cache("projects/{project}/", source_cache_root)
    dest = _create_cache("projects/{project}/", dest_cache_root)

    # Groupings
    src_group = source.grouping(["alpha"])
    dst_group = dest.grouping(["beta"])

    # Create two source input files on disk, then upsert into source grouping
    inputs_dir = root / "inputs"
    _clean_dir(inputs_dir)
    f1 = inputs_dir / "docs" / "a.txt"
    f2 = inputs_dir / "docs" / "b.txt"
    _write_text_file(f1, "hello A")
    _write_text_file(f2, "hello B")

    # Upsert into source grouping with distinct ref_paths
    notice1 = await src_group.upsert_file(LocalFileProxy(str(f1), ref_path="docs/a.txt"))
    notice2 = await src_group.upsert_file(LocalFileProxy(str(f2), ref_path="docs/b.txt"))
    assert notice1 is not None and notice2 is not None

    # Add some per-file slave_dir content under source
    src_slave_a = src_group.get_slave_dir("docs/a.txt")
    src_slave_b = src_group.get_slave_dir("docs/b.txt")
    (src_slave_a / "metaA.yaml").write_text("a: 1", encoding="utf-8")
    (src_slave_b / "metaB.yaml").write_text("b: 2", encoding="utf-8")

    # Build factory from source grouping; identity transform into destination
    factory = CacheGroupingFileProxyFactory(src_group)

    def transform(ref_path: str) -> Optional[str]:
        # map source docs/* into same path under destination
        return ref_path

    # Upsert into destination grouping with change_receiver to copy per-file slave_dir
    receiver = factory.make_change_receiver(copy_slave_dir=True)
    for proxy in factory.scan_files(ref_path_glob="docs/*.txt", ref_path_transform=transform):
        notice = await dst_group.upsert_file(proxy, force=False)
        # Simulate change receiver invocation as cached_file_folders does
        if notice:
            receiver(notice, proxy)  # type: ignore

    # Verify files deployed
    assert dst_group.file_exists("docs/a.txt")
    assert dst_group.file_exists("docs/b.txt")

    # Verify per-file slave_dir content copied
    dst_slave_a = dst_group.get_slave_dir("docs/a.txt")
    dst_slave_b = dst_group.get_slave_dir("docs/b.txt")
    assert (dst_slave_a / "metaA.yaml").exists()
    assert (dst_slave_b / "metaB.yaml").exists()


@pytest.mark.asyncio
async def test_grouping_level_slave_dir_not_copied_by_scan_but_copied_by_helper():
    root = _volatile_test_root()
    _clean_dir(root)

    source_cache_root = root / "source_cache2"
    dest_cache_root = root / "dest_cache2"
    source = _create_cache("space/{name}/", source_cache_root)
    dest = _create_cache("space/{name}/", dest_cache_root)

    src_group = source.grouping(["g1"])
    dst_group = dest.grouping(["g2"])

    # Ensure at least one file exists so grouping directories are created
    temp_file = root / "inputs2" / "x.dat"
    _write_text_file(temp_file, "x")
    await src_group.upsert_file(LocalFileProxy(str(temp_file), ref_path="x.dat"))

    # Put content in grouping-level slave_dir in source
    src_group_slave = src_group.get_slave_dir()
    (src_group_slave / "state.json").write_text('{"ok": true}', encoding="utf-8")

    # Copy files using factory without copying grouping-level slave_dir
    factory = CacheGroupingFileProxyFactory(src_group)
    receiver = factory.make_change_receiver(copy_slave_dir=True)
    for proxy in factory.scan_files(ref_path_glob="*.dat", ref_path_transform=lambda r: r):
        notice = await dst_group.upsert_file(proxy)
        if notice:
            receiver(notice, proxy)  # type: ignore

    # Grouping-level slave_dir should NOT be present in destination yet
    dst_group_slave = dst_group.get_slave_dir()
    assert not (dst_group_slave / "state.json").exists()

    # Use convenience helper to copy grouping-level slave_dir
    CacheGroupingFileProxyFactory.copy_grouping_slave_dir(src_group, dst_group)
    assert (dst_group_slave / "state.json").exists()


@pytest.mark.asyncio
async def test_ref_path_transform_can_skip_items():
    root = _volatile_test_root()
    _clean_dir(root)

    source_cache_root = root / "source_cache3"
    dest_cache_root = root / "dest_cache3"
    source = _create_cache("{bucket}/", source_cache_root)
    dest = _create_cache("{bucket}/", dest_cache_root)

    src = source.grouping(["bucketA"])
    dst = dest.grouping(["bucketB"])

    inputs_dir = root / "inputs3"
    _clean_dir(inputs_dir)
    f_keep = inputs_dir / "keep.txt"
    f_drop = inputs_dir / "drop.txt"
    _write_text_file(f_keep, "keep")
    _write_text_file(f_drop, "drop")

    await src.upsert_file(LocalFileProxy(str(f_keep), ref_path="keep.txt"))
    await src.upsert_file(LocalFileProxy(str(f_drop), ref_path="drop.txt"))

    factory = CacheGroupingFileProxyFactory(src)

    def transform(ref_path: str) -> Optional[str]:
        # Skip anything named drop.txt
        if ref_path.endswith("drop.txt"):
            return None
        return ref_path

    receiver = factory.make_change_receiver(copy_slave_dir=True)
    for proxy in factory.scan_files(ref_path_glob="*.txt", ref_path_transform=transform):
        notice = await dst.upsert_file(proxy)
        if notice:
            receiver(notice, proxy)  # type: ignore

    assert dst.file_exists("keep.txt")
    assert not dst.file_exists("drop.txt")


