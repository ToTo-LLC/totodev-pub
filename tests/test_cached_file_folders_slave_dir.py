# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for grouping-level and per-file slave directories.

This module tests the get_slave_dir() method on both CachedFileFolders
and CacheGrouping classes, ensuring proper isolation, persistence, and
integration with cache operations.
"""

import pytest
from pathlib import Path
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy


@pytest.fixture
def temp_cache(tmp_path):
    """Create a CachedFileFolders instance with grouped pattern."""
    cache_root = tmp_path / "cache"
    cache = CachedFileFolders(
        grouping_pattern="test/{group}/",
        root_dir=str(cache_root)
    )
    return cache


@pytest.fixture
def flat_cache(tmp_path):
    """Create a CachedFileFolders instance with flat pattern."""
    cache_root = tmp_path / "flat_cache"
    cache = CachedFileFolders(
        grouping_pattern="flat/",
        root_dir=str(cache_root)
    )
    return cache


@pytest.fixture
def sample_file(tmp_path):
    """Create a temporary file for testing."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("test content")
    return test_file


class TestGroupingLevelSlaveDir:
    """Tests for grouping-level slave directories."""
    
    def test_create_grouping_slave_dir(self, temp_cache):
        """Test that grouping-level slave dir is created lazily."""
        slave_dir = temp_cache.get_slave_dir(["group1"], ref_path=None)
        
        assert slave_dir.exists()
        assert slave_dir.is_dir()
        assert slave_dir.name == "_grouping._slave"
        assert "group1" in str(slave_dir)
    
    def test_grouping_slave_dir_persistent(self, temp_cache):
        """Test that grouping slave dir persists across calls."""
        slave_dir1 = temp_cache.get_slave_dir(["group1"], ref_path=None)
        (slave_dir1 / "state.txt").write_text("persistent")
        
        slave_dir2 = temp_cache.get_slave_dir(["group1"], ref_path=None)
        
        assert slave_dir1 == slave_dir2
        assert (slave_dir2 / "state.txt").read_text() == "persistent"
    
    def test_multiple_groupings_isolated(self, temp_cache):
        """Test that different grouping keys have separate slave dirs."""
        slave_dir1 = temp_cache.get_slave_dir(["group1"], ref_path=None)
        slave_dir2 = temp_cache.get_slave_dir(["group2"], ref_path=None)
        
        (slave_dir1 / "data.txt").write_text("group1 data")
        (slave_dir2 / "data.txt").write_text("group2 data")
        
        assert slave_dir1 != slave_dir2
        assert (slave_dir1 / "data.txt").read_text() == "group1 data"
        assert (slave_dir2 / "data.txt").read_text() == "group2 data"
    
    def test_flat_pattern_grouping_slave_dir(self, flat_cache):
        """Test grouping slave dir works with flat patterns."""
        slave_dir = flat_cache.get_slave_dir(None, ref_path=None)
        
        assert slave_dir.exists()
        assert slave_dir.is_dir()
        assert slave_dir.name == "_grouping._slave"
    
    @pytest.mark.asyncio
    async def test_slave_dir_unaffected_by_resync(self, temp_cache, sample_file):
        """Test that grouping slave dir is not touched during resync operations."""
        # Create grouping slave dir with data
        slave_dir = temp_cache.get_slave_dir(["group1"], ref_path=None)
        (slave_dir / "important.txt").write_text("preserve me")
        
        # Perform resync operation
        proxy = LocalFileProxy(str(sample_file))
        result = await temp_cache.resync_bulk(
            file_proxies=[proxy],
            grouping_key=["group1"],
            auto_delete=True
        )
        
        # Verify slave dir and its contents still exist
        assert slave_dir.exists()
        assert (slave_dir / "important.txt").exists()
        assert (slave_dir / "important.txt").read_text() == "preserve me"


class TestPerFileSlaveDir:
    """Tests for per-file slave directories accessed via get_slave_dir()."""
    
    @pytest.mark.asyncio
    async def test_get_slave_dir_for_existing_file(self, temp_cache, sample_file):
        """Test getting slave dir for an existing cached file."""
        # Add file to cache
        proxy = LocalFileProxy(str(sample_file))
        notice = await temp_cache.upsert_file(proxy, ["group1"])
        
        # Get slave dir via get_slave_dir()
        slave_dir = temp_cache.get_slave_dir(["group1"], ref_path=proxy.ref_path())
        
        assert slave_dir.exists()
        assert slave_dir.is_dir()
        assert slave_dir == notice.cur.slave_dir_path
    
    @pytest.mark.asyncio
    async def test_get_slave_dir_nonexistent_file_raises(self, temp_cache):
        """Test that getting slave dir for non-existent file raises ValueError."""
        with pytest.raises(ValueError, match="File not found in cache"):
            temp_cache.get_slave_dir(["group1"], ref_path="nonexistent.txt")
    
    @pytest.mark.asyncio
    async def test_per_file_slave_dir_operations(self, temp_cache, sample_file):
        """Test writing to per-file slave dir."""
        proxy = LocalFileProxy(str(sample_file))
        await temp_cache.upsert_file(proxy, ["group1"])
        
        slave_dir = temp_cache.get_slave_dir(["group1"], ref_path=proxy.ref_path())
        (slave_dir / "metadata.json").write_text('{"processed": true}')
        
        assert (slave_dir / "metadata.json").exists()
        assert '"processed": true' in (slave_dir / "metadata.json").read_text()


class TestCacheGroupingIntegration:
    """Tests for CacheGrouping.get_slave_dir() integration."""
    
    def test_cache_grouping_get_slave_dir_no_ref_path(self, temp_cache):
        """Test CacheGrouping.get_slave_dir() for grouping-level access."""
        grouping = temp_cache.grouping(["group1"])
        slave_dir = grouping.get_slave_dir()
        
        assert slave_dir.exists()
        assert slave_dir.is_dir()
        assert "_grouping._slave" in str(slave_dir)
    
    @pytest.mark.asyncio
    async def test_cache_grouping_get_slave_dir_with_ref_path(self, temp_cache, sample_file):
        """Test CacheGrouping.get_slave_dir() for per-file access."""
        grouping = temp_cache.grouping(["group1"])
        
        proxy = LocalFileProxy(str(sample_file))
        await grouping.upsert_file(proxy)
        
        slave_dir = grouping.get_slave_dir(ref_path=proxy.ref_path())
        
        assert slave_dir.exists()
        assert slave_dir.is_dir()
    
    def test_cache_grouping_cleaner_api(self, temp_cache):
        """Test that CacheGrouping provides cleaner API for slave dirs."""
        grouping = temp_cache.grouping(["group1"])
        
        # Cleaner than: temp_cache.get_slave_dir(["group1"], ref_path=None)
        slave_dir = grouping.get_slave_dir()
        (slave_dir / "config.yaml").write_text("cleaner: true")
        
        assert (slave_dir / "config.yaml").exists()


class TestSlaveDirectoryIsolation:
    """Tests ensuring proper isolation between different slave directory types."""
    
    @pytest.mark.asyncio
    async def test_grouping_and_file_slave_dirs_separate(self, temp_cache, sample_file):
        """Test that grouping-level and per-file slave dirs are separate."""
        # Create grouping-level slave dir
        grouping_slave = temp_cache.get_slave_dir(["group1"], ref_path=None)
        (grouping_slave / "global_state.txt").write_text("global")
        
        # Add file and get its slave dir
        proxy = LocalFileProxy(str(sample_file))
        await temp_cache.upsert_file(proxy, ["group1"])
        file_slave = temp_cache.get_slave_dir(["group1"], ref_path=proxy.ref_path())
        
        # Verify they're different directories
        assert grouping_slave != file_slave
        assert (grouping_slave / "global_state.txt").exists()
        assert not (file_slave / "global_state.txt").exists()
    
    @pytest.mark.asyncio
    async def test_multiple_files_have_separate_slave_dirs(self, temp_cache, tmp_path):
        """Test that multiple files in same grouping have separate slave dirs."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("content1")
        file2.write_text("content2")
        
        proxy1 = LocalFileProxy(str(file1))
        proxy2 = LocalFileProxy(str(file2))
        
        await temp_cache.upsert_file(proxy1, ["group1"])
        await temp_cache.upsert_file(proxy2, ["group1"])
        
        slave1 = temp_cache.get_slave_dir(["group1"], ref_path=proxy1.ref_path())
        slave2 = temp_cache.get_slave_dir(["group1"], ref_path=proxy2.ref_path())
        
        assert slave1 != slave2
        (slave1 / "data.txt").write_text("file1 data")
        (slave2 / "data.txt").write_text("file2 data")
        
        assert (slave1 / "data.txt").read_text() == "file1 data"
        assert (slave2 / "data.txt").read_text() == "file2 data"


class TestRealWorldUsagePatterns:
    """Tests for real-world usage patterns with slave directories."""
    
    @pytest.mark.asyncio
    async def test_sync_state_tracking(self, temp_cache, tmp_path):
        """Test using grouping slave dir for sync state tracking."""
        import json
        from datetime import datetime
        
        grouping = temp_cache.grouping(["inbox"])
        state_dir = grouping.get_slave_dir()
        state_file = state_dir / "sync_state.json"
        
        # Write sync state
        state = {
            "last_sync": datetime.now().isoformat(),
            "emails_processed": 42
        }
        state_file.write_text(json.dumps(state, indent=2))
        
        # Verify state persists
        loaded_state = json.loads(state_file.read_text())
        assert loaded_state["emails_processed"] == 42
    
    @pytest.mark.asyncio
    async def test_per_file_processing_logs(self, temp_cache, sample_file):
        """Test using per-file slave dir for processing logs."""
        proxy = LocalFileProxy(str(sample_file))
        notice = await temp_cache.upsert_file(proxy, ["documents"])
        
        # Write processing log
        log_file = notice.cur.slave_dir_path / "processing.log"
        log_file.write_text("Step 1: OCR complete\nStep 2: Indexing complete\n")
        
        # Verify log accessible via get_slave_dir()
        slave_dir = temp_cache.get_slave_dir(["documents"], ref_path=proxy.ref_path())
        assert (slave_dir / "processing.log").read_text() == log_file.read_text()

