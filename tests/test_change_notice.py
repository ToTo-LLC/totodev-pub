# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the ChangeNotice lifecycle with ephemeral old artifacts."""

import tempfile
from pathlib import Path
from typing import Callable, Optional

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders, CachedFileRef, ChangeNotice
from totodev_pub.cached_file_folders_support.file_proxy_dummy import FileProxyDummy
from totodev_pub.cached_file_folders_support.sync_types import ChangeType

TEST_MATERIALIZE_SECS = 0.01  # Accelerate dummy proxy work for tests


def fast_proxy(ref_path: str, **kwargs) -> FileProxyDummy:
    """Create a FileProxyDummy with predictable fast behaviour."""
    if "materialize_secs" not in kwargs:
        kwargs["materialize_secs"] = TEST_MATERIALIZE_SECS
    return FileProxyDummy(ref_path, **kwargs)


class TestChangeNotice:
    """Exercise observable ChangeNotice behaviour with ephemeral old artifacts."""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def cache(self, temp_dir):
        return CachedFileFolders("test/{group}/", str(temp_dir))

    async def _create_insert_notice(self, cache: CachedFileFolders) -> ChangeNotice:
        proxy = fast_proxy("test/file.txt", version_num=100)
        notice = await cache.upsert_file(proxy, ["test_group"])
        assert notice.change_type == ChangeType.INSERT
        return notice

    async def _create_update_notice(
        self,
        cache: CachedFileFolders,
        change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyDummy]], None]] = None,
    ) -> ChangeNotice:
        await cache.upsert_file(fast_proxy("test/file.txt", version_num=100), ["test_group"])
        notice = await cache.upsert_file(
            fast_proxy("test/file.txt", version_num=200),
            ["test_group"],
            change_receiver=change_receiver,
        )
        assert notice.change_type == ChangeType.UPDATE
        return notice

    async def _create_delete_notice(
        self,
        cache: CachedFileFolders,
        change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyDummy]], None]] = None,
    ) -> ChangeNotice:
        await cache.upsert_file(fast_proxy("test/file.txt", version_num=100), ["test_group"])
        notice = await cache.delete_file(
            "test/file.txt",
            ["test_group"],
            change_receiver=change_receiver,
        )
        assert notice.change_type == ChangeType.DELETE
        return notice

    @pytest.mark.asyncio
    async def test_insert_notice_has_only_current_artifacts(self, cache: CachedFileFolders):
        notice = await self._create_insert_notice(cache)

        assert notice.change_type == ChangeType.INSERT
        assert notice.cur is not None
        assert notice.old is None
        assert notice.cur.file_path.exists()
        assert notice.cur.slave_dir_path.exists()

    @pytest.mark.asyncio
    async def test_update_old_artifacts_exist_only_during_callback(self, cache: CachedFileFolders):
        staged_paths = []

        def receiver(notice: ChangeNotice, proxy: Optional[FileProxyDummy]) -> None:
            assert notice.change_type == ChangeType.UPDATE
            assert notice.old is not None
            staged_paths.append((notice.old.file_path, notice.old.slave_dir_path))
            assert notice.old.file_path.exists()
            assert notice.old.slave_dir_path.exists()

        notice = await self._create_update_notice(cache, change_receiver=receiver)

        assert notice.old is not None
        for file_path, slave_dir_path in staged_paths:
            assert not file_path.exists()
            assert not slave_dir_path.exists()

    @pytest.mark.asyncio
    async def test_delete_old_artifacts_exist_only_during_callback(self, cache: CachedFileFolders):
        staged_paths = []

        def receiver(notice: ChangeNotice, proxy: Optional[FileProxyDummy]) -> None:
            assert notice.change_type == ChangeType.DELETE
            assert notice.old is not None
            staged_paths.append((notice.old.file_path, notice.old.slave_dir_path))
            assert notice.old.file_path.exists()
            assert notice.old.slave_dir_path.exists()

        notice = await self._create_delete_notice(cache, change_receiver=receiver)

        assert notice.old is not None
        for file_path, slave_dir_path in staged_paths:
            assert not file_path.exists()
            assert not slave_dir_path.exists()

    @pytest.mark.asyncio
    async def test_update_without_receiver_cleans_up_old_artifacts(self, cache: CachedFileFolders):
        notice = await self._create_update_notice(cache, change_receiver=None)
        assert notice.old is not None
        assert not notice.old.file_path.exists()
        assert not notice.old.slave_dir_path.exists()

    def test_change_notice_path_field_requirements_table(self):
        """Ensure the docstring table remains accurate."""

        insert_notice = ChangeNotice(
            change_type=ChangeType.INSERT,
            file_name="test.txt",
            cur=CachedFileRef(
                ref_path="test.txt",
                grouping_key=None,
                file_path=Path("/tmp/test.txt"),
                slave_dir_path=Path("/tmp/test.txt._slave"),
            ),
        )
        assert insert_notice.cur is not None
        assert insert_notice.old is None

        update_notice = ChangeNotice(
            change_type=ChangeType.UPDATE,
            file_name="test.txt",
            cur=CachedFileRef(
                ref_path="test.txt",
                grouping_key=None,
                file_path=Path("/tmp/test.txt"),
                slave_dir_path=Path("/tmp/test.txt._slave"),
            ),
            old=CachedFileRef(
                ref_path="test.txt",
                grouping_key=None,
                file_path=Path("/tmp/test_old.txt"),
                slave_dir_path=Path("/tmp/test_old.txt._slave"),
            ),
        )
        assert update_notice.cur is not None
        assert update_notice.old is not None

        delete_notice = ChangeNotice(
            change_type=ChangeType.DELETE,
            file_name="test.txt",
            old=CachedFileRef(
                ref_path="test.txt",
                grouping_key=None,
                file_path=Path("/tmp/test_old.txt"),
                slave_dir_path=Path("/tmp/test_old.txt._slave"),
            ),
        )
        assert delete_notice.cur is None
        assert delete_notice.old is not None

