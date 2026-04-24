# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for EmailFolderSynchronizer and EmailSyncTimingInfo.

This module tests the dual-mode email synchronization pattern, ensuring
proper state persistence, timing logic, and integration with CacheGrouping.
"""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.email_folder_synchronizer import (
    EmailSyncTimingInfo,
    EmailFolderSynchronizer
)
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase



# =============================================================================
# MOCK FILE PROXY
# =============================================================================

class MockEmailProxy(FileProxyBase):
    """Mock email proxy for testing without real email servers."""
    
    def __init__(self, ref_path: str, content: str, received_dt: datetime):
        self._ref_path = ref_path
        self._content = content
        self._received_dt = received_dt
    
    def ref_path(self) -> str:
        return self._ref_path
    
    def file_name(self) -> str:
        return self._ref_path.split('/')[-1]
    
    def deploy(self, target_dir: str) -> None:
        (Path(target_dir) / self.file_name()).write_text(self._content)
    
    def looks_same(self, cached_file_path: str) -> bool:
        cached_path = Path(cached_file_path)
        if not cached_path.exists():
            return False
        return cached_path.read_text() == self._content
    
    async def materialize(self, blocking_secs: float, temp_dir=None) -> bool:
        return True
    
    def get_context_info(self) -> dict:
        return {'ref_path': self._ref_path, 'received': self._received_dt}
    
    # Property to match real email proxies
    @property
    def received_datetime(self) -> datetime:
        return self._received_dt


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def temp_cache(tmp_path):
    """Create a CachedFileFolders instance for testing."""
    cache_root = tmp_path / "cache"
    cache = CachedFileFolders(
        grouping_pattern="emails/{folder}/",
        root_dir=str(cache_root)
    )
    return cache


@pytest.fixture
def timing_file(tmp_path):
    """Create a path for timing info file."""
    return tmp_path / "sync_timing.yaml"


# =============================================================================
# EMAILSYNCTIMINGINFO TESTS
# =============================================================================

class TestEmailSyncTimingInfo:
    """Tests for EmailSyncTimingInfo model with FileMappedPydanticMixin."""
    
    def test_create_with_defaults(self):
        """Test creating EmailSyncTimingInfo with default values."""
        info = EmailSyncTimingInfo()
        
        assert info.retain_days == 2
        assert info.new_check_interval_secs == 15
        assert info.full_check_interval_secs == 3600
        # last_full_check and last_check are auto-initialized to retain_days+1 ago
        expected = datetime.now() - timedelta(days=3)  # retain_days=2, so 2+1=3
        assert abs((info.last_full_check - expected).total_seconds()) < 1
        assert abs((info.last_check - expected).total_seconds()) < 1
    
    def test_create_with_custom_values(self):
        """Test creating EmailSyncTimingInfo with custom values."""
        info = EmailSyncTimingInfo(
            retain_days=7,
            new_check_interval_secs=30,
            full_check_interval_secs=7200
        )
        
        assert info.retain_days == 7
        assert info.new_check_interval_secs == 30
        assert info.full_check_interval_secs == 7200
    
    def test_default_last_full_check_fallback(self):
        """Test last_full_check is auto-initialized to retain_days+1 ago if None."""
        info = EmailSyncTimingInfo(retain_days=2)
        
        expected = datetime.now() - timedelta(days=3)  # retain_days=2, so 2+1=3
        
        # Allow 1 second tolerance for test execution time
        assert abs((info.last_full_check - expected).total_seconds()) < 1
    
    def test_default_last_full_check_uses_value(self):
        """Test last_full_check uses provided value when set."""
        specific_time = datetime(2025, 1, 1, 12, 0, 0)
        info = EmailSyncTimingInfo(last_full_check=specific_time)
        
        assert info.last_full_check == specific_time
    
    def test_default_last_check_fallback(self):
        """Test last_check is auto-initialized to retain_days+1 ago if None."""
        info = EmailSyncTimingInfo(retain_days=3)
        
        expected = datetime.now() - timedelta(days=4)  # retain_days=3, so 3+1=4
        
        assert abs((info.last_check - expected).total_seconds()) < 1
    
    def test_default_last_check_uses_value(self):
        """Test last_check uses provided value when set."""
        specific_time = datetime(2025, 1, 15, 10, 30, 0)
        info = EmailSyncTimingInfo(last_check=specific_time)
        
        assert info.last_check == specific_time
    
    def test_persistence_with_file_mapped_mixin(self, timing_file):
        """Test that EmailSyncTimingInfo persists via FileMappedPydanticMixin."""
        # Create with fallback and save
        info = EmailSyncTimingInfo.open(
            str(timing_file),
            fallback_value=lambda: EmailSyncTimingInfo(
                retain_days=5,
                new_check_interval_secs=20
            ),
            without_lock=False
        )
        info.last_check = datetime(2025, 2, 1, 15, 0, 0)
        info.save()
        info.release_lock()
        
        # Load and verify
        loaded = EmailSyncTimingInfo.open(str(timing_file), without_lock=True)
        
        assert loaded.retain_days == 5
        assert loaded.new_check_interval_secs == 20
        assert loaded.last_check == datetime(2025, 2, 1, 15, 0, 0)


# =============================================================================
# EMAILFOLDERSYNCHRONIZER TESTS
# =============================================================================

class TestEmailFolderSynchronizer:
    """Tests for EmailFolderSynchronizer class."""
    
    def test_recommended_wait_secs_immediate(self):
        """Test recommended_wait_secs() when check just happened."""
        info = EmailSyncTimingInfo(
            new_check_interval_secs=30,
            last_check=datetime.now()
        )
        cache_grouping = None  # Not needed for this test
        sync = EmailFolderSynchronizer(info, cache_grouping, lambda x: [])
        
        wait = sync.recommended_wait_secs()
        
        # Should recommend waiting close to the full interval
        assert 28 <= wait <= 30
    
    def test_recommended_wait_secs_overdue(self):
        """Test recommended_wait_secs() when check is overdue."""
        info = EmailSyncTimingInfo(
            new_check_interval_secs=30,
            last_check=datetime.now() - timedelta(seconds=60)
        )
        cache_grouping = None
        sync = EmailFolderSynchronizer(info, cache_grouping, lambda x: [])
        
        wait = sync.recommended_wait_secs()
        
        # Should recommend immediate check
        assert wait == 0
    
    def test_recommended_wait_secs_halfway(self):
        """Test recommended_wait_secs() when halfway through interval."""
        info = EmailSyncTimingInfo(
            new_check_interval_secs=60,
            last_check=datetime.now() - timedelta(seconds=30)
        )
        cache_grouping = None
        sync = EmailFolderSynchronizer(info, cache_grouping, lambda x: [])
        
        wait = sync.recommended_wait_secs()
        
        # Should recommend waiting about half the interval
        assert 28 <= wait <= 32
    
    @pytest.mark.asyncio

    async def test_sync_new_items_mode(self, temp_cache):
        """Test that sync() uses new items mode when full check not needed."""
        # Setup: last full check was recent, so should do new items only
        info = EmailSyncTimingInfo(
            new_check_interval_secs=15,
            full_check_interval_secs=3600,
            last_full_check=datetime.now() - timedelta(seconds=60),
            last_check=datetime.now() - timedelta(seconds=20)
        )
        
        grouping = temp_cache.grouping(["inbox"])
        
        # Mock fetcher that returns a single email
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Perform sync (save_sync_timing=False since info not bound to file)
        result = await sync.sync(save_sync_timing=False)
        
        # Verify it was a new items sync
        assert result['sync_type'] == 'new_items'
        assert result['count'] == 1
        assert len(result['changes']) == 1
        assert len(result['failures']) == 0
        # Verify new fields are present
        assert 'items_scanned' in result
        assert 'duration_secs' in result
        assert 'errors_encountered' in result
    
    @pytest.mark.asyncio

    async def test_sync_full_mode_by_time(self, temp_cache):
        """Test that sync() uses full mode when full check interval elapsed."""
        # Setup: last full check was long ago
        info = EmailSyncTimingInfo(
            new_check_interval_secs=15,
            full_check_interval_secs=3600,
            last_full_check=datetime.now() - timedelta(seconds=7200),  # 2 hours ago
            last_check=datetime.now() - timedelta(seconds=20)
        )
        
        grouping = temp_cache.grouping(["inbox"])
        
        # Mock fetcher
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [
                MockEmailProxy("email1.eml", "content1", datetime.now()),
                MockEmailProxy("email2.eml", "content2", datetime.now())
            ]
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Perform sync (save_sync_timing=False since info not bound to file)
        result = await sync.sync(save_sync_timing=False)
        
        # Verify it was a full sync
        assert result['sync_type'] == 'full'
        assert result['count'] == 2
    
    @pytest.mark.asyncio

    async def test_sync_full_mode_forced(self, temp_cache):
        """Test that force_full=True triggers full mode."""
        # Setup: full check not needed by time
        info = EmailSyncTimingInfo(
            full_check_interval_secs=3600,
            last_full_check=datetime.now() - timedelta(seconds=60)
        )
        
        grouping = temp_cache.grouping(["inbox"])
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Force full sync (save_sync_timing=False since info not bound to file)
        result = await sync.sync(force_full=True, save_sync_timing=False)
        
        # Verify it was a full sync despite timing
        assert result['sync_type'] == 'full'
    
    @pytest.mark.asyncio

    async def test_sync_updates_timestamps(self, temp_cache, timing_file):
        """Test that sync() updates and persists timestamps."""
        # Create timing info with file persistence
        info = EmailSyncTimingInfo.open(
            str(timing_file),
            fallback_value=lambda: EmailSyncTimingInfo(
                last_check=datetime.now() - timedelta(seconds=30)
            ),
            without_lock=False
        )
        
        grouping = temp_cache.grouping(["inbox"])
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return []
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        before_sync = datetime.now()
        await sync.sync()
        after_sync = datetime.now()
        
        # Verify timestamps were updated
        assert info.last_check is not None
        assert before_sync <= info.last_check <= after_sync
        
        # Verify persistence (reload from file)
        info.release_lock()
        reloaded = EmailSyncTimingInfo.open(str(timing_file), without_lock=True)
        assert reloaded.last_check == info.last_check
    
    @pytest.mark.asyncio

    async def test_sync_with_change_receiver(self, temp_cache):
        """Test that sync() calls change_receiver for each change."""
        info = EmailSyncTimingInfo()
        grouping = temp_cache.grouping(["inbox"])
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [
                MockEmailProxy("email1.eml", "content1", datetime.now()),
                MockEmailProxy("email2.eml", "content2", datetime.now())
            ]
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Track change_receiver calls
        received_changes = []
        
        def change_receiver(notice, proxy):
            received_changes.append((notice, proxy))
        
        # Perform sync with change receiver (save_sync_timing=False since info not bound to file)
        result = await sync.sync(change_receiver=change_receiver, save_sync_timing=False)
        
        # Verify change_receiver was called for each change
        assert len(received_changes) == 2
        assert all(notice is not None for notice, _ in received_changes)
        assert all(proxy is not None for _, proxy in received_changes)
    
    @pytest.mark.asyncio

    async def test_min_cutoff_date_used_when_more_recent(self, temp_cache):
        """Test that min_cutoff_date overrides retain_days when min_cutoff_date is more recent."""
        # Setup: retain_days would go back 3 days, but min_cutoff_date is only 1 day ago
        info = EmailSyncTimingInfo(
            retain_days=3,
            full_check_interval_secs=0  # Force full sync
        )
        grouping = temp_cache.grouping(["inbox"])
        
        # Track what cutoff was passed to fetcher
        received_cutoffs = []
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            received_cutoffs.append(cutoff)
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        # min_cutoff_date is 1 day ago (more recent than 3 days)
        min_cutoff = datetime.now() - timedelta(days=1)
        
        sync = EmailFolderSynchronizer(
            info, grouping, mock_fetcher,
            min_cutoff_date=min_cutoff
        )
        
        # Perform full sync
        result = await sync.sync(force_full=True, save_sync_timing=False)
        
        # Verify fetcher was called with min_cutoff_date, not retain_days cutoff
        assert len(received_cutoffs) == 1
        # Should be close to min_cutoff (within a few seconds for test execution time)
        time_diff = abs((received_cutoffs[0] - min_cutoff).total_seconds())
        assert time_diff < 5, f"Cutoff should match min_cutoff_date, but diff was {time_diff}s"
    
    @pytest.mark.asyncio

    async def test_min_cutoff_date_ignored_when_older(self, temp_cache):
        """Test that retain_days is used when min_cutoff_date is older than retain_days cutoff."""
        # Setup: retain_days is 1 day, but min_cutoff_date is 3 days ago
        info = EmailSyncTimingInfo(
            retain_days=1,
            full_check_interval_secs=0  # Force full sync
        )
        grouping = temp_cache.grouping(["inbox"])
        
        # Track what cutoff was passed to fetcher
        received_cutoffs = []
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            received_cutoffs.append(cutoff)
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        # min_cutoff_date is 3 days ago (older than 1 day retain_days)
        min_cutoff = datetime.now() - timedelta(days=3)
        retain_cutoff = datetime.now() - timedelta(days=1)
        
        sync = EmailFolderSynchronizer(
            info, grouping, mock_fetcher,
            min_cutoff_date=min_cutoff
        )
        
        # Perform full sync
        result = await sync.sync(force_full=True, save_sync_timing=False)
        
        # Verify fetcher was called with retain_days cutoff (more recent)
        assert len(received_cutoffs) == 1
        # Should be close to retain_cutoff (within a few seconds)
        time_diff = abs((received_cutoffs[0] - retain_cutoff).total_seconds())
        assert time_diff < 5, f"Cutoff should match retain_days, but diff was {time_diff}s"
    
    @pytest.mark.asyncio

    async def test_min_cutoff_date_none_uses_retain_days(self, temp_cache):
        """Test that retain_days cutoff is used when min_cutoff_date is None."""
        # Setup: min_cutoff_date not provided
        info = EmailSyncTimingInfo(
            retain_days=2,
            full_check_interval_secs=0  # Force full sync
        )
        grouping = temp_cache.grouping(["inbox"])
        
        # Track what cutoff was passed to fetcher
        received_cutoffs = []
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            received_cutoffs.append(cutoff)
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        # No min_cutoff_date provided (defaults to None)
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Calculate expected cutoff
        expected_cutoff = datetime.now() - timedelta(days=2)
        
        # Perform full sync
        result = await sync.sync(force_full=True, save_sync_timing=False)
        
        # Verify fetcher was called with retain_days cutoff
        assert len(received_cutoffs) == 1
        time_diff = abs((received_cutoffs[0] - expected_cutoff).total_seconds())
        assert time_diff < 5, f"Cutoff should match retain_days, but diff was {time_diff}s"
    
    @pytest.mark.asyncio

    async def test_min_cutoff_date_prevents_fetching_old_emails(self, temp_cache):
        """Test that min_cutoff_date prevents wasting bandwidth on emails that won't be retained."""
        # Real-world scenario: CLI specifies --received-after 1 day, but --retain-days 7
        # Should only fetch 1 day of emails, not 7 days
        info = EmailSyncTimingInfo(
            retain_days=7,  # Would normally fetch 7 days
            full_check_interval_secs=0
        )
        grouping = temp_cache.grouping(["inbox"])
        
        # Create emails spanning 3 days
        now = datetime.now()
        three_days_ago = now - timedelta(days=3)
        two_days_ago = now - timedelta(days=2)
        one_day_ago = now - timedelta(days=1)
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            # Simulate fetching emails based on cutoff
            emails = []
            if cutoff <= three_days_ago:
                emails.append(MockEmailProxy("old3.eml", "3 days old", three_days_ago))
            if cutoff <= two_days_ago:
                emails.append(MockEmailProxy("old2.eml", "2 days old", two_days_ago))
            if cutoff <= one_day_ago:
                emails.append(MockEmailProxy("old1.eml", "1 day old", one_day_ago))
            return emails
        
        # Set min_cutoff_date to 1 day ago
        sync = EmailFolderSynchronizer(
            info, grouping, mock_fetcher,
            min_cutoff_date=one_day_ago
        )
        
        # Perform full sync
        result = await sync.sync(force_full=True, save_sync_timing=False)
        
        # Should only have fetched 1 email (1 day old), not all 3
        assert result['count'] == 1
        assert result['items_scanned'] == 1


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegration:
    """Integration tests for complete workflow."""
    
    @pytest.mark.asyncio

    async def test_complete_workflow(self, temp_cache, timing_file):
        """Test complete workflow: create, sync, reload, sync again."""
        # Step 1: Create fresh timing info with recent timestamps to trigger new_items mode
        info = EmailSyncTimingInfo.open(
            str(timing_file),
            fallback_value=lambda: EmailSyncTimingInfo(
                retain_days=1,
                new_check_interval_secs=10,
                full_check_interval_secs=3600,  # 1 hour - so first check won't be full
                last_full_check=datetime.now() - timedelta(seconds=30),  # Recent full check
                last_check=datetime.now() - timedelta(seconds=15)  # Recent check
            ),
            without_lock=False
        )
        
        grouping = temp_cache.grouping(["inbox"])
        
        call_count = [0]
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            call_count[0] += 1
            return [MockEmailProxy(f"email{call_count[0]}.eml", f"content{call_count[0]}", datetime.now())]
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Step 2: First sync (new items mode because last_full_check is recent)
        result1 = await sync.sync()
        assert result1['sync_type'] == 'new_items'
        assert result1['count'] == 1
        
        # Step 3: Immediate second sync (should still be new items, might return 0)
        result2 = await sync.sync()  # save_sync_timing=True OK, file-bound
        assert result2['sync_type'] == 'new_items'
        
        # Step 4: Force full sync
        result3 = await sync.sync(force_full=True)  # save_sync_timing=True OK, file-bound
        assert result3['sync_type'] == 'full'
        
        # Verify all syncs updated timestamps
        assert info.last_check is not None
        assert info.last_full_check is not None
        
        # Step 5: Reload from file and verify state persists
        info.release_lock()
        reloaded = EmailSyncTimingInfo.open(str(timing_file), without_lock=True)
        assert reloaded.last_check == info.last_check
        assert reloaded.last_full_check == info.last_full_check
    
    @pytest.mark.asyncio

    async def test_grouping_slave_dir_usage(self, temp_cache):
        """Test that timing info naturally lives in grouping slave dir."""
        # This pattern from the plan
        grouping = temp_cache.grouping(["inbox"])
        slave_dir = grouping.get_slave_dir()
        timing_file = slave_dir / "sync_timing.yaml"
        
        # Create timing info in slave dir
        info = EmailSyncTimingInfo.open(
            str(timing_file),
            fallback_value=lambda: EmailSyncTimingInfo(retain_days=3),
            without_lock=False
        )
        info.save()
        
        # Verify file exists in slave dir
        assert timing_file.exists()
        assert "_grouping._slave" in str(timing_file)
        
        # Verify we can reload it
        info.release_lock()
        reloaded = EmailSyncTimingInfo.open(str(timing_file), without_lock=True)
        assert reloaded.retain_days == 3


# =============================================================================
# ERROR HANDLING TESTS
# =============================================================================

class TestErrorHandling:
    """Tests for error handling and retry logic."""
    
    @pytest.mark.asyncio

    async def test_fetcher_retry_with_exponential_backoff(self, temp_cache):
        """Test that fetcher failures trigger retry with exponential backoff."""
        import time
        
        info = EmailSyncTimingInfo()
        grouping = temp_cache.grouping(["inbox"])
        
        call_times = []
        attempt_count = [0]
        
        def failing_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            call_times.append(time.time())
            attempt_count[0] += 1
            if attempt_count[0] < 3:  # Fail twice, succeed third time
                raise ConnectionError("Temporary network failure")
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        sync = EmailFolderSynchronizer(info, grouping, failing_fetcher)
        
        start = time.time()
        result = await sync.sync(save_sync_timing=False)
        
        # Should have retried and eventually succeeded
        assert attempt_count[0] == 3
        assert result['count'] == 1
        
        # Verify exponential backoff delays (0.2s, 0.4s)
        if len(call_times) >= 3:
            delay1 = call_times[1] - call_times[0]
            delay2 = call_times[2] - call_times[1]
            assert 0.15 <= delay1 <= 0.3  # ~0.2s with tolerance
            assert 0.35 <= delay2 <= 0.5  # ~0.4s with tolerance
    
    @pytest.mark.asyncio

    async def test_consecutive_identical_errors_stop_sync(self, temp_cache):
        """Test that 3 consecutive identical errors trigger systematic failure."""
        info = EmailSyncTimingInfo()
        grouping = temp_cache.grouping(["inbox"])
        
        attempt_count = [0]
        
        def always_failing_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            attempt_count[0] += 1
            raise ValueError("Same error every time")
        
        sync = EmailFolderSynchronizer(
            info, grouping, always_failing_fetcher,
            max_consecutive_errors=3
        )
        
        # Should raise after 3 identical errors
        with pytest.raises(RuntimeError, match="consecutive identical errors"):
            await sync.sync(save_sync_timing=False)
        
        # Should have tried exactly 3 times
        assert attempt_count[0] == 3
    
    @pytest.mark.asyncio

    async def test_different_errors_reset_counter(self, temp_cache):
        """Test that different errors reset the consecutive error counter."""
        info = EmailSyncTimingInfo()
        grouping = temp_cache.grouping(["inbox"])
        
        attempt_count = [0]
        # Test with 2 errors then success: Error 1, Error 2, success
        # Different error should reset counter
        errors = [ValueError("Error 1"), ConnectionError("Error 2")]
        
        def varying_error_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            if attempt_count[0] < len(errors):
                e = errors[attempt_count[0]]
                attempt_count[0] += 1
                raise e
            attempt_count[0] += 1
            return [MockEmailProxy("email1.eml", "content1", datetime.now())]
        
        sync = EmailFolderSynchronizer(
            info, grouping, varying_error_fetcher,
            max_consecutive_errors=3
        )
        
        # Different error in middle resets counter, so should succeed on 3rd attempt
        result = await sync.sync(save_sync_timing=False)
        assert result['count'] == 1
        assert attempt_count[0] == 3  # 2 failures + 1 success
    
    @pytest.mark.asyncio

    async def test_upsert_failure_with_retain_old_policy(self, temp_cache):
        """Test that upsert failures are tracked but don't stop sync with RETAIN_OLD."""
        # Set recent last_full_check to trigger new_items mode
        info = EmailSyncTimingInfo(
            last_full_check=datetime.now() - timedelta(seconds=60),
            last_check=datetime.now() - timedelta(seconds=20)
        )
        grouping = temp_cache.grouping(["inbox"])
        
        # Create a proxy that will fail on deploy
        class FailingProxy(MockEmailProxy):
            def deploy(self, target_dir: str):
                raise PermissionError("Cannot write to directory")
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [
                MockEmailProxy("good.eml", "good content", datetime.now()),
                FailingProxy("/invalid/../path.eml", "bad", datetime.now()),  # Will fail
                MockEmailProxy("also_good.eml", "more good", datetime.now()),
            ]
        
        sync = EmailFolderSynchronizer(
            info, grouping, mock_fetcher,
            upsert_fail_policy="RETAIN_OLD"
        )
        
        result = await sync.sync(save_sync_timing=False)
        
        # Should have processed all items despite one failure
        assert result['items_scanned'] == 3
        assert result['errors_encountered'] >= 1
        assert len(result['failures']) >= 1
    
    @pytest.mark.asyncio

    async def test_upsert_failure_with_fail_fast_policy(self, temp_cache):
        """Test that upsert failures stop sync immediately with FAIL_FAST."""
        # Set recent last_full_check to trigger new_items mode
        info = EmailSyncTimingInfo(
            last_full_check=datetime.now() - timedelta(seconds=60),
            last_check=datetime.now() - timedelta(seconds=20)
        )
        grouping = temp_cache.grouping(["inbox"])
        
        # Create a mock proxy that will fail on deploy
        class FailingProxy(MockEmailProxy):
            def deploy(self, target_dir: str):
                raise PermissionError("Cannot write to directory")
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [
                MockEmailProxy("good.eml", "content", datetime.now()),
                FailingProxy("bad.eml", "content", datetime.now()),
                MockEmailProxy("never_reached.eml", "content", datetime.now()),
            ]
        
        sync = EmailFolderSynchronizer(
            info, grouping, mock_fetcher,
            upsert_fail_policy="FAIL_FAST"
        )
        
        # Should raise on first failure
        with pytest.raises(RuntimeError, match="FAIL_FAST"):
            await sync.sync(save_sync_timing=False)
    
    @pytest.mark.asyncio

    async def test_save_sync_timing_false(self, temp_cache):
        """Test that save_sync_timing=False works without file binding."""
        info = EmailSyncTimingInfo()  # No file binding
        grouping = temp_cache.grouping(["inbox"])
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return [MockEmailProxy("email.eml", "content", datetime.now())]
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Should work fine without file binding when save_sync_timing=False
        result = await sync.sync(save_sync_timing=False)
        assert result['count'] == 1
    
    @pytest.mark.asyncio

    async def test_save_sync_timing_true_requires_file(self, temp_cache):
        """Test that save_sync_timing=True raises error without file binding."""
        info = EmailSyncTimingInfo()  # No file binding
        grouping = temp_cache.grouping(["inbox"])
        
        def mock_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            return []
        
        sync = EmailFolderSynchronizer(info, grouping, mock_fetcher)
        
        # Should raise error about not being bound to file
        with pytest.raises(RuntimeError, match="not bound to a file"):
            await sync.sync(save_sync_timing=True)
    
    @pytest.mark.asyncio

    async def test_error_tracking_resets_on_success(self, temp_cache):
        """Test that error tracking resets after a successful fetch."""
        # Use two separate cache groupings to avoid "already cached" issues
        info1 = EmailSyncTimingInfo()
        grouping1 = temp_cache.grouping(["inbox1"])
        
        attempt_count = [0]
        
        def intermittent_fetcher(cutoff: datetime) -> List[FileProxyBase]:
            attempt_count[0] += 1
            # Fail first 2 times with same error
            if attempt_count[0] <= 2:
                raise ValueError("Same error")
            # Then succeed with file matching the attempt
            return [MockEmailProxy(f"email{attempt_count[0]}.eml", f"content{attempt_count[0]}", datetime.now())]
        
        sync = EmailFolderSynchronizer(info1, grouping1, intermittent_fetcher)
        
        # First sync: should succeed after retries (error count: 2)
        result1 = await sync.sync(save_sync_timing=False)
        assert result1['count'] == 1
        
        # Verify error tracking was reset (internal state check)
        assert sync._consecutive_error_count == 0
        
        # Reset attempt counter and use a different grouping for second sync
        attempt_count[0] = 0
        info2 = EmailSyncTimingInfo()
        grouping2 = temp_cache.grouping(["inbox2"])
        sync2 = EmailFolderSynchronizer(info2, grouping2, intermittent_fetcher)
        
        # Second sync: error tracking should have been reset
        # So even if we get 2 more same errors, won't hit the limit of 3
        result2 = await sync2.sync(save_sync_timing=False)
        assert result2['count'] == 1
        assert attempt_count[0] == 3  # 2 failures + 1 success

