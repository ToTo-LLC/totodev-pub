# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for ResyncOrchestrator - External orchestration with optimistic concurrency
"""

import asyncio
import pytest
import tempfile
import time
from pathlib import Path

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support import ChangeType, ResyncOrchestrator, FileSnapshot
from totodev_pub.cached_file_folders_support.file_proxy_dummy import FileProxyDummy
from totodev_pub.pytest_tools import very_lazy_test

ORCHESTRATOR_LAZY = very_lazy_test(
    [
        "totodev_pub.cached_file_folders_support.resync_orchestrator",
        "totodev_pub.cached_file_folders_support.storage_manager",
        "totodev_pub.cached_file_folders",
    ],
    stability_delay=600,
    reverify_days=14,
)


@pytest.fixture
def temp_cache_dir():
    """Create a temporary directory for cache testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cache(temp_cache_dir):
    """Create a CachedFileFolders instance for testing."""
    cache_root = temp_cache_dir / "cache"
    cache = CachedFileFolders(
        grouping_pattern="{group}/files/",
        root_dir=str(cache_root)
    )
    return cache


class TestFileSnapshot:
    """Test FileSnapshot dataclass."""
    
    def test_file_snapshot_creation(self, temp_cache_dir):
        """Test creating a FileSnapshot."""
        test_file = temp_cache_dir / "test.txt"
        test_file.write_text("test content")
        
        stat = test_file.stat()
        snapshot = FileSnapshot(
            ref_path="test/file.txt",
            file_path=test_file,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            grouping_key=("test_group",)
        )
        
        assert snapshot.ref_path == "test/file.txt"
        assert snapshot.file_path == test_file
        assert snapshot.mtime_ns == stat.st_mtime_ns
        assert snapshot.size == stat.st_size


class TestResyncOrchestratorBasics:
    """Test basic ResyncOrchestrator functionality."""
    
    @pytest.mark.asyncio
    async def test_orchestrator_creation(self, cache):
        """Test creating ResyncOrchestrator."""
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",)
        ) as session:
            assert session is not None
            assert len(session._snapshots) == 0  # No files yet
            assert len(session._touched) == 0
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_orchestrator_snapshot_capture(self, cache):
        """Test that orchestrator captures snapshots on enter."""
        # Add some files first
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200)
        await cache.upsert_file(proxy1, ("test_group",))
        await cache.upsert_file(proxy2, ("test_group",))
        
        # Create orchestrator - should capture snapshot
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",)
        ) as session:
            assert len(session._snapshots) == 2
            ref_paths = {s.ref_path for s in session._snapshots}
            assert ref_paths == {"test/file1.txt", "test/file2.txt"}
            
            # Verify mtime and size are captured
            for snapshot in session._snapshots:
                assert snapshot.mtime_ns > 0
                assert snapshot.size > 0


class TestResyncOrchestratorMtimeVerification:
    """Test optimistic concurrency via mtime verification."""
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_mtime_changed_file_preserved(self, cache):
        """Test that files modified during sweep are preserved."""
        # Add initial file
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        await cache.upsert_file(proxy1, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            auto_delete=True
        ) as session:
            # Touch file1 externally (modify its mtime)
            file_ref = cache.find_file("test/file1.txt", ("test_group",))
            assert file_ref is not None
            
            # Simulate file modification by touching it
            await asyncio.sleep(0.01)  # Ensure time difference
            file_ref.file_path.touch()
            
            # Don't touch file1 in the session - it should be preserved due to mtime change
        
        # File should still exist (preserved due to mtime change)
        remaining = list(cache.files(("test_group",)))
        assert len(remaining) == 1
        assert remaining[0].ref_path == "test/file1.txt"
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_unchanged_file_deleted(self, cache):
        """Test that unchanged untouched files are deleted."""
        # Add initial files
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200)
        await cache.upsert_file(proxy1, ("test_group",))
        await cache.upsert_file(proxy2, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            auto_delete=True
        ) as session:
            # Only touch file1
            session.upsert_file(proxy1)
            await session.wait_for_completion()
        
        # Only file1 should remain
        remaining = list(cache.files(("test_group",)))
        assert len(remaining) == 1
        assert remaining[0].ref_path == "test/file1.txt"
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_size_changed_file_preserved(self, cache):
        """Test that files with changed size during sweep are preserved."""
        # Add initial file
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        await cache.upsert_file(proxy1, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            auto_delete=True
        ) as session:
            # Modify file size
            file_ref = cache.find_file("test/file1.txt", ("test_group",))
            assert file_ref is not None
            
            # Change content (changes size and mtime)
            file_ref.file_path.write_text("modified content with different size")
            
            # Don't touch file1 in session
        
        # File should still exist (preserved due to size change)
        remaining = list(cache.files(("test_group",)))
        assert len(remaining) == 1


class TestResyncOrchestratorConcurrency:
    """Test concurrent operations and throttling."""
    
    @pytest.mark.asyncio
    async def test_concurrent_upserts(self, cache):
        """Test that multiple upserts run concurrently."""
        proxies = [
            FileProxyDummy(f"test/file{i}.txt", version_num=i*100, materialize_secs=0.1)
            for i in range(5)
        ]
        
        start_time = time.time()
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            max_concurrency=5
        ) as session:
            for proxy in proxies:
                session.upsert_file(proxy)
            await session.wait_for_completion()
        
        duration = time.time() - start_time
        
        # Should complete in ~0.1s (concurrent), not 0.5s (sequential)
        assert duration < 0.3, f"Took {duration}s, expected < 0.3s for concurrent execution"
        
        # Verify all files were added
        files = list(cache.files(("test_group",)))
        assert len(files) == 5
    
    @pytest.mark.asyncio
    async def test_throttle_queue_numeric(self, cache):
        """Test numeric throttle queues."""
        proxies = [
            FileProxyDummy(f"test/file{i}.txt", version_num=i*100, materialize_secs=0.1)
            for i in range(5)
        ]
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",)
        ) as session:
            for proxy in proxies:
                session.upsert_file(proxy, throttle_queue=2)  # Limit to 2 concurrent
            await session.wait_for_completion()
        
        # All files should be processed
        files = list(cache.files(("test_group",)))
        assert len(files) == 5
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_throttle_queue_named(self, cache):
        """Test named throttle queues."""
        proxies = [FileProxyDummy(f"test/file{i}.txt", version_num=i*100) for i in range(3)]
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            throttle_queue_limits={"slow": 1}
        ) as session:
            for proxy in proxies:
                session.upsert_file(proxy, throttle_queue="slow")
            await session.wait_for_completion()
        
        files = list(cache.files(("test_group",)))
        assert len(files) == 3


class TestResyncOrchestratorAutoDelete:
    """Test auto_delete behavior."""
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_auto_delete_true(self, cache):
        """Test that untouched files are deleted when auto_delete=True."""
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200)
        await cache.upsert_file(proxy1, ("test_group",))
        await cache.upsert_file(proxy2, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            auto_delete=True
        ) as session:
            # Only touch file1
            session.upsert_file(proxy1)
        
        # Only file1 should remain
        files = list(cache.files(("test_group",)))
        assert len(files) == 1
        assert files[0].ref_path == "test/file1.txt"
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_auto_delete_false(self, cache):
        """Test that untouched files are NOT deleted when auto_delete=False."""
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200)
        await cache.upsert_file(proxy1, ("test_group",))
        await cache.upsert_file(proxy2, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            auto_delete=False
        ) as session:
            # Only touch file1
            session.upsert_file(proxy1)
        
        # Both files should remain
        files = list(cache.files(("test_group",)))
        assert len(files) == 2


class TestResyncOrchestratorResults:
    """Test result collection methods."""
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_upserted_list(self, cache):
        """Test upserted_list returns changes."""
        proxies = [FileProxyDummy(f"test/file{i}.txt", version_num=i*100) for i in range(3)]
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",)
        ) as session:
            for proxy in proxies:
                session.upsert_file(proxy)
            
            changes = await session.upserted_list()
            assert len(changes) == 3
            assert all(c.change_type == ChangeType.INSERT for c in changes)
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_deleted_list_preview(self, cache):
        """Test deleted_list shows preview of deletions."""
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200)
        await cache.upsert_file(proxy1, ("test_group",))
        await cache.upsert_file(proxy2, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            auto_delete=False  # Don't actually delete
        ) as session:
            # Only touch file1
            session.upsert_file(proxy1)
            
            # Check deletion preview
            deletions = await session.deleted_list()
            assert len(deletions) == 1
            assert deletions[0].ref_path == "test/file2.txt"
            assert deletions[0].change_type == ChangeType.DELETE
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_failed_upserts(self, cache):
        """Test failed_upserts tracks failures."""
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200, forced_to_fail_counter=1)
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            upsert_fail_policy="RETAIN_OLD"
        ) as session:
            session.upsert_file(proxy1)
            session.upsert_file(proxy2)
            
            failures = await session.failed_upserts()
            assert len(failures) == 1
            assert failures[0].file_proxy.ref_path() == "test/file2.txt"


class TestResyncOrchestratorChangeReceiver:
    """Test change_receiver callback integration."""
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_change_receiver_called(self, cache):
        """Test that change_receiver is called for changes."""
        received_changes = []
        
        def change_receiver(notice, proxy):
            received_changes.append((notice, proxy))
        
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            change_receiver=change_receiver
        ) as session:
            session.upsert_file(proxy1)
            await session.wait_for_completion()
        
        assert len(received_changes) == 1
        notice, proxy = received_changes[0]
        assert notice.change_type == ChangeType.INSERT
        assert proxy is proxy1
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_change_receiver_for_deletions(self, cache):
        """Test that change_receiver is called for deletions."""
        received_changes = []
        
        def change_receiver(notice, proxy):
            received_changes.append((notice, proxy))
        
        # Add file
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100)
        await cache.upsert_file(proxy1, ("test_group",))
        
        async with ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            change_receiver=change_receiver,
            auto_delete=True
        ) as session:
            # Don't touch file1 - it will be deleted
            pass
        
        # Should have one deletion notice
        delete_notices = [n for n, p in received_changes if n.change_type == ChangeType.DELETE]
        assert len(delete_notices) == 1
        assert delete_notices[0].ref_path == "test/file1.txt"


class TestBulkResyncOrchestrator:
    """Test ResyncOrchestrator.bulk_sync() with retry logic."""
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_bulk_sync_basic(self, cache):
        """Test basic bulk_sync operation."""
        proxies = [FileProxyDummy(f"test/file{i}.txt", version_num=i*100) for i in range(3)]
        
        orchestrator = ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            retry_count=0
        )
        
        async with orchestrator as session:
            result = await session.bulk_sync(proxies)
        
        assert len(result.changes) == 3
        assert len(result.failures) == 0
        assert all(c.change_type == ChangeType.INSERT for c in result.changes)
    
    @pytest.mark.asyncio
    @ORCHESTRATOR_LAZY
    async def test_bulk_sync_with_retries(self, cache):
        """Test bulk_sync retry logic."""
        # Proxy that fails once then succeeds
        proxies = [
            FileProxyDummy("test/file1.txt", version_num=100),
            FileProxyDummy("test/file2.txt", version_num=200, forced_to_fail_counter=1),
        ]
        
        orchestrator = ResyncOrchestrator(
            cache=cache,
            grouping_key=("test_group",),
            retry_count=2,
            upsert_fail_policy="RETAIN_OLD"
        )
        
        async with orchestrator as session:
            result = await session.bulk_sync(proxies)
        
        # Both should succeed (file2 after retry)
        assert len(result.changes) == 2
        assert len(result.failures) == 0


class TestResyncOrchestratorNoConcurrentSweepLock:
    """Test that sweeps can run concurrently (no locking)."""
    
    @pytest.mark.asyncio
    async def test_concurrent_sweeps_different_groupings(self, cache):
        """Test that sweeps on different groupings can run concurrently."""
        proxy1 = FileProxyDummy("test/file1.txt", version_num=100, materialize_secs=0.2)
        proxy2 = FileProxyDummy("test/file2.txt", version_num=200, materialize_secs=0.2)
        
        # Start two sweeps concurrently on different groupings
        async def sweep1():
            async with ResyncOrchestrator(cache=cache, grouping_key=("group1",)) as session:
                session.upsert_file(proxy1)
                await session.wait_for_completion()
        
        async def sweep2():
            async with ResyncOrchestrator(cache=cache, grouping_key=("group2",)) as session:
                session.upsert_file(proxy2)
                await session.wait_for_completion()
        
        start = time.time()
        await asyncio.gather(sweep1(), sweep2())
        duration = time.time() - start
        
        # Should complete in ~0.2s (concurrent), not 0.4s (sequential)
        assert duration < 0.35, f"Took {duration}s, expected < 0.35s for concurrent sweeps"
        
        # Both files should exist in their respective groupings
        assert len(list(cache.files(("group1",)))) == 1
        assert len(list(cache.files(("group2",)))) == 1

