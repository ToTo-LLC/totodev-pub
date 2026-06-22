# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Generic Email Folder Synchronizer - Dual-Mode Synchronization Orchestrator

This module provides a reusable orchestrator for synchronizing email folders with
dual-mode synchronization:
- Lightweight checks: frequent polling for new items using upsert_file()
- Full sweeps: periodic change detection using resync_bulk() with mark-and-sweep

The synchronizer works with any email system (Outlook, Gmail, IMAP) by accepting
a generic FileGenCallable that wraps the email proxy factory.

State is automatically persisted using FileMappedPydanticMixin for seamless
resumption after restarts or failures.

Dependencies:
    This module relies heavily on the totodev_pub.cached_file_folders module and related
    classes for its functionality:
    - CachedFileFolders: Main cache interface for file synchronization
    - CacheGrouping: Facet that binds cache + grouping_key for scoped operations
    - FileProxyBase: Abstract interface for remote file access
    - FileMappedPydanticMixin: Automatic file persistence for state models

Production Example:
    For a complete, production-ready example of using EmailFolderSynchronizer with
    Microsoft 365/Outlook, see:
        totodev_pub/cached_file_folders_support/examples/outlook_email_sync.py
    
    This example demonstrates:
    - Complete Azure AD authentication setup
    - Proper error handling and logging
    - Change receiver callbacks for processing emails
    - CLI interface with all configuration options
    - Both single-run and continuous monitoring modes

Basic Usage Pattern:
    ```python
    import asyncio
    from datetime import datetime
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.cached_file_folders_support.email_folder_synchronizer import (
        EmailFolderSynchronizer, EmailSyncTimingInfo
    )
    from your_email_system import YourEmailProxyFactory  # e.g., OutlookEmailFileProxyFactory
    
    async def sync_inbox():
        # 1. Setup cache with grouping pattern
        cache = CachedFileFolders("emails/{folder}/", "/path/to/cache")
        inbox_grouping = cache.grouping(["inbox"])
        
        # 2. Create/load sync timing info (auto-persisted)
        slave_dir = inbox_grouping.get_slave_dir()
        sync_info = EmailSyncTimingInfo.open(
            str(slave_dir / "sync_timing.yaml"),
            without_lock=False
        )
        
        # 3. Setup email fetcher (wraps your email system's API)
        email_factory = YourEmailProxyFactory(credentials=...)
        
        def fetch_emails(cutoff: datetime):
            return email_factory.scan_messages(received_after=cutoff)
        
        # 4. Create synchronizer
        synchronizer = EmailFolderSynchronizer(sync_info, inbox_grouping, fetch_emails)
        
        # 5. Run sync loop
        while True:
            result = await synchronizer.sync()
            print(f"{result['sync_type']}: {result['count']} changes in {result['duration_secs']:.1f}s")
            
            # Wait before next check
            await asyncio.sleep(synchronizer.recommended_wait_secs())
    
    # Run the sync
    asyncio.run(sync_inbox())
    ```

Complete Integration Example:
    See outlook_example() at the bottom of this file for complete integration
    with Microsoft 365/Outlook email synchronization including authentication,
    error handling, and change notifications.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Generator, Optional, Dict, Any, List
from pydantic import BaseModel, Field, model_validator

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from totodev_pub.cached_file_folders import ChangeNotice
from totodev_pub.cached_file_folders_support.sync_types import UpsertFailure

logger = logging.getLogger(__name__)


# =============================================================================
# TYPE ALIASES
# =============================================================================

FileGenCallable = Callable[[datetime], Generator[FileProxyBase, None, None]]
"""
Type alias for email fetcher function.

Takes a datetime cutoff and returns a generator of FileProxyBase objects
representing emails received after that cutoff.
"""


# =============================================================================
# SYNC TIMING MODEL
# =============================================================================

class EmailSyncTimingInfo(BaseModel, FileMappedPydanticMixin):
    """
    Email sync configuration and state with automatic file persistence.
    
    This model tracks both configuration (how often to sync) and state
    (when we last synced) using FileMappedPydanticMixin for automatic
    persistence to YAML/JSON file.
    
    Typically stored in the grouping-level slave directory for per-folder
    state tracking.
    
    Attributes:
        retain_days: Number of days of emails to retain in cache
        new_check_interval_secs: Seconds between lightweight new item checks
        full_check_interval_secs: Seconds between full change detection sweeps
        last_full_check: Timestamp of last full check with mark-and-sweep
        last_check: Timestamp of last check of any type
    
    Example:
        # Create/load sync timing info (uses default field values if file doesn't exist)
        slave_dir = grouping.get_slave_dir() # see totodev_pub.cached_file_folders_support.cache_grouping
        sync_info = EmailSyncTimingInfo.open(
            slave_dir / "sync_timing.yaml",
            without_lock=False
        )
    """
    
    # Configuration (set at initialization, rarely changed)
    retain_days: int = Field(
        default=2,
        description="Days of emails to retain in cache"
    )
    new_check_interval_secs: int = Field(
        default=15,
        description="Interval for new item checks (lightweight)"
    )
    full_check_interval_secs: int = Field(
        default=3600,
        description="Interval for full sweeps (with mark-and-sweep)"
    )
    
    # State (automatically updated by sync operations)
    last_full_check: Optional[datetime] = Field(
        default=None,
        description="Timestamp of last full check with mark-and-sweep"
    )
    last_check: Optional[datetime] = Field(
        default=None,
        description="Timestamp of last check (any type)"
    )
    last_received_email_timestamp: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the most recently received email (from email metadata)"
    )
    
    @model_validator(mode='after')
    def _set_default_timestamps(self):
        """Set last_full_check and last_check to retain_days+1 ago if None.
        This is to force a full recheck on first run."""
        if self.last_full_check is None:
            self.last_full_check = datetime.now() - timedelta(days=self.retain_days+1)
        if self.last_check is None:
            self.last_check = datetime.now() - timedelta(days=self.retain_days+1)
        return self


# =============================================================================
# EMAIL FOLDER SYNCHRONIZER
# =============================================================================

class EmailFolderSynchronizer:
    """
    Generic dual-mode email synchronization orchestrator.
    
    This class provides a reusable pattern for synchronizing email folders
    with automatic mode selection based on timing. It works with any email
    system (Outlook, Gmail, IMAP) by accepting a generic FileGenCallable.
    
    Sync Modes:
    -----------
    **Lightweight (frequent)**: Polls for new emails since last check using
    upsert_file(). No mark-and-sweep, just adds new items. Fast and minimal
    API load. Suitable for running every 15-30 seconds.
    
    **Full (periodic)**: Scans entire retention window using resync_bulk()
    with mark-and-sweep. Detects new items, changes (via looks_same()), and
    removes emails beyond retention period. More expensive but comprehensive.
    Suitable for running every 1-24 hours.
    
    State Persistence:
    ------------------
    State is automatically persisted via EmailSyncTimingInfo using
    FileMappedPydanticMixin. After each sync, timestamps are updated and
    saved to disk, enabling seamless resumption after restarts or failures.
    
    Error Handling:
    ---------------
    - Individual upsert failures are tracked and reported in results
    - Email fetcher failures use exponential backoff with retry
    - Consecutive identical errors trigger systematic failure detection
    - All errors are logged with context before retry or propagation
    
    Usage Pattern:
    --------------
    1. Create EmailSyncTimingInfo (loads from file if exists)
    2. Create CacheGrouping facet for your email folder
    3. Create FileGenCallable wrapper around your email factory
    4. Create EmailFolderSynchronizer with these three components
    5. Call sync() in a loop, sleeping for recommended_wait_secs() between calls
    
    Example:
        See outlook_example() at the bottom of this file for complete integration.
    """
    
    def __init__(self, 
                 sync_info: EmailSyncTimingInfo,
                 cache: CacheGrouping,
                 email_fetcher: FileGenCallable,
                 upsert_fail_policy: str = "RETAIN_OLD",
                 max_consecutive_errors: int = 3,
                 min_cutoff_date: Optional[datetime] = None):
        """
        Initialize email folder synchronizer.
        
        Args:
            sync_info: Timing configuration and state (auto-persisted to file)
            cache: CacheGrouping facet binding cache + grouping_key together
            email_fetcher: Callable taking datetime cutoff, returning email proxies
            upsert_fail_policy: How to handle upsert failures - 
                              "RETAIN_OLD" (continue, log) or "FAIL_FAST" (abort)
            max_consecutive_errors: Stop if same fetcher error occurs this many 
                                  times consecutively
            min_cutoff_date: Optional minimum cutoff date (don't fetch earlier than this).
                           When provided, the effective cutoff will be max(retain_days_cutoff, min_cutoff_date)
        
        Example:
            # Setup components
            slave_dir = cache.grouping(["inbox"]).get_slave_dir()
            sync_info = EmailSyncTimingInfo.open(str(slave_dir / "timing.yaml"))
            cache_grouping = cache.grouping(["inbox"])
            fetcher = lambda cutoff: factory.scan_messages(received_after=cutoff)
            
            # Create synchronizer
            sync = EmailFolderSynchronizer(sync_info, cache_grouping, fetcher)
        """
        self.sync_info = sync_info
        self.cache = cache
        self.email_fetcher = email_fetcher
        self.upsert_fail_policy = upsert_fail_policy
        self.max_consecutive_errors = max_consecutive_errors
        self.min_cutoff_date = min_cutoff_date
        self._last_error_message = None
        self._consecutive_error_count = 0
    
    def recommended_wait_secs(self) -> int:
        """
        Calculate recommended seconds to wait before next sync() call.
        
        Based on when the last check occurred and the new_check_interval_secs
        configuration. This enables simple scheduling:
        
            while True:
                await sync.sync()
                await asyncio.sleep(sync.recommended_wait_secs())
        
        Returns:
            Recommended wait time in seconds (minimum 0)
        """
        now = datetime.now()
        last_check = self.sync_info.last_check
        elapsed = (now - last_check).total_seconds()
        wait_time = max(0, self.sync_info.new_check_interval_secs - elapsed)
        return int(wait_time)
    
    async def sync(self, 
                   change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None,
                   force_full: bool = False,
                   save_sync_timing: bool = True) -> Dict[str, Any]:
        """
        Perform synchronization - automatically chooses lightweight vs full.
        
        Decision Logic:
        - If force_full=True: do full sweep
        - If full_check_interval elapsed since last_full_check: do full sweep
        - Otherwise: do lightweight new item check
        
        After sync completes, timestamps are updated and automatically persisted
        via FileMappedPydanticMixin (if save_sync_timing=True).
        
        Args:
            change_receiver: Optional callback for change notifications.
                           Called with (ChangeNotice, Optional[FileProxyBase])
                           for each change detected. May be sync or async; see
                           `ChangeNotice` ("Synchronous vs. async receivers") for
                           guidance on choosing.
            force_full: Force full sweep regardless of timing
            save_sync_timing: If True, save timing info to filesystem after sync.
                            Raises error if sync_info is not bound to a file.
                            Set False for testing or when managing persistence manually.
            
        Returns:
            Dict with sync results:
            {
                'sync_type': 'full' or 'new_items',
                'changes': List[ChangeNotice],
                'failures': List[UpsertFailure],
                'count': int (number of changes),
                'items_scanned': int (emails processed),
                'duration_secs': float (total time),
                'errors_encountered': int (upsert failures)
            }
        
        Raises:
            RuntimeError: If max_consecutive_errors reached for email fetcher
            Various: Propagated from email_fetcher or upsert operations if FAIL_FAST
        
        Example:
            result = await sync.sync()
            print(f"{result['sync_type']}: {result['count']} changes")
            
            for change in result['changes']:
                print(f"  {change.change_type.value}: {change.ref_path}")
        """
        now = datetime.now()
        last_full = self.sync_info.last_full_check
        elapsed_since_full = (now - last_full).total_seconds()
        
        # Decide which type of sync to run
        should_do_full = (
            force_full or 
            elapsed_since_full >= self.sync_info.full_check_interval_secs
        )
        
        sync_type = 'full' if should_do_full else 'new_items'
        logger.info(f"Starting {sync_type} sync (force_full={force_full}, "
                   f"elapsed_since_full={elapsed_since_full:.0f}s)")
        
        if should_do_full:
            result = await self._sync_full(change_receiver)
            self.sync_info.last_full_check = now
        else:
            result = await self._sync_new_items(change_receiver)
        
        # Update last check time
        self.sync_info.last_check = now
        
        # Persist to disk if requested and bound to file
        if save_sync_timing:
            if not hasattr(self.sync_info, '_file_path') or not self.sync_info._file_path:
                raise RuntimeError(
                    "Cannot save sync timing: EmailSyncTimingInfo is not bound to a file. "
                    "Use EmailSyncTimingInfo.open() to bind to a file, or set save_sync_timing=False."
                )
            logger.debug(f"Persisting sync timing to {self.sync_info._file_path}")
            self.sync_info.save()
        
        logger.info(f"Sync complete: {result['sync_type']}, "
                   f"{result['count']} changes, {result['errors_encountered']} errors, "
                   f"{result['duration_secs']:.2f}s")
        
        return result
    
    async def _sync_new_items(self, change_receiver) -> Dict[str, Any]:
        """
        Lightweight sync: upsert_file() for new emails only.
        
        Fetches emails using last received email's timestamp minus 1 minute as cutoff.
        This ensures overlap with the last known email, guaranteeing no gaps.
        Falls back to retain_days ago on first run.
        
        No mark-and-sweep, no change detection for existing emails.
        Fast and minimal API load.
        
        Implements retry logic with exponential backoff for fetcher errors.
        Individual upsert failures are tracked but don't stop the sync
        (unless upsert_fail_policy is FAIL_FAST).
        """
        start_time = time.time()
        
        # Calculate cutoff based on last received email timestamp (with 1-minute overlap)
        # or fall back to retain_days ago if no prior emails
        if self.sync_info.last_received_email_timestamp:
            cutoff = self.sync_info.last_received_email_timestamp - timedelta(minutes=1)
            logger.debug(f"Using last_received_email_timestamp strategy: cutoff={cutoff}")
        else:
            cutoff = datetime.now() - timedelta(days=self.sync_info.retain_days)
            logger.debug(f"First run: using retain_days strategy: cutoff={cutoff}")
        
        changes = []
        failures = []
        items_scanned = 0
        errors_encountered = 0
        max_received_timestamp = None
        
        logger.debug(f"Fetching emails newer than {cutoff}")
        
        # Fetch emails with retry and error tracking
        try:
            proxies = await self._fetch_with_retry(cutoff)
        except Exception as e:
            # Fetcher failed after retries - add context and re-raise
            duration = time.time() - start_time
            logger.error(f"Email fetcher failed after retries: {e}")
            raise RuntimeError(
                f"Email fetcher failed after {self.max_consecutive_errors} consecutive "
                f"identical errors. Last error: {e}"
            ) from e
        
        # Process each email proxy
        for proxy in proxies:
            items_scanned += 1
            try:
                notice = await self.cache.upsert_file(proxy)
                if notice:
                    changes.append(notice)
                    logger.debug(f"{notice.change_type.value}: {notice.ref_path}")
                    if change_receiver:
                        change_receiver(notice, proxy)
                
                # Track the maximum received timestamp from processed emails
                if hasattr(proxy, 'received_datetime'):
                    email_timestamp = proxy.received_datetime
                    if max_received_timestamp is None or email_timestamp > max_received_timestamp:
                        max_received_timestamp = email_timestamp
                
                # Success - reset error tracking
                self._reset_error_tracking()
                
            except Exception as e:
                errors_encountered += 1
                ref_path = proxy.ref_path() if hasattr(proxy, 'ref_path') else str(proxy)
                failure = UpsertFailure(
                    grouping_key=self.cache._grouping_key,
                    file_proxy=proxy,
                    exception=e
                )
                failures.append(failure)
                
                logger.warning(f"Failed to upsert {ref_path}: {type(e).__name__}: {e}")
                
                if self.upsert_fail_policy == "FAIL_FAST":
                    # Add context and re-raise original exception
                    raise RuntimeError(
                        f"Upsert failed in FAIL_FAST mode for {ref_path}. "
                        f"Original error: {e}"
                    ) from e
        
        # Update last_received_email_timestamp if we processed any emails
        if max_received_timestamp is not None:
            self.sync_info.last_received_email_timestamp = max_received_timestamp
            logger.debug(f"Updated last_received_email_timestamp to {max_received_timestamp}")
        
        duration = time.time() - start_time
        return {
            'sync_type': 'new_items',
            'changes': changes,
            'failures': failures,
            'count': len(changes),
            'items_scanned': items_scanned,
            'duration_secs': duration,
            'errors_encountered': errors_encountered
        }
    
    async def _sync_full(self, change_receiver) -> Dict[str, Any]:
        """
        Full sync: resync_bulk() with mark-and-sweep.
        
        Fetches all emails in retention window and uses resync_bulk() with
        auto_delete=True to:
        - Add new emails (INSERT)
        - Detect changes via looks_same() (UPDATE)
        - Remove emails beyond retention or deleted from server (DELETE)
        
        More expensive but comprehensive. Uses resync_bulk's built-in
        error handling and failure tracking.
        """
        start_time = time.time()
        
        # Calculate cutoff based on retain_days
        retain_cutoff = datetime.now() - timedelta(days=self.sync_info.retain_days)
        
        # Use the more recent of retain_cutoff and min_cutoff_date (if provided)
        # This ensures we don't fetch emails we won't retain anyway
        if self.min_cutoff_date is not None:
            cutoff = max(retain_cutoff, self.min_cutoff_date)
            logger.debug(f"Fetching all emails since {cutoff} (retain_days={self.sync_info.retain_days}, min_cutoff_date={self.min_cutoff_date}, effective_cutoff={cutoff})")
        else:
            cutoff = retain_cutoff
            logger.debug(f"Fetching all emails since {cutoff} (retain_days={self.sync_info.retain_days})")
        
        # Fetch emails with retry and error tracking
        try:
            proxies_list = await self._fetch_with_retry(cutoff)
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Email fetcher failed after retries: {e}")
            raise RuntimeError(
                f"Email fetcher failed after {self.max_consecutive_errors} consecutive "
                f"identical errors. Last error: {e}"
            ) from e
        
        # Reset error tracking on successful fetch
        self._reset_error_tracking()
        
        # Track maximum received timestamp from all fetched emails (emails only, not attachments)
        max_received_timestamp = None
        for proxy in proxies_list:
            if hasattr(proxy, 'received_datetime'):
                email_timestamp = proxy.received_datetime
                if max_received_timestamp is None or email_timestamp > max_received_timestamp:
                    max_received_timestamp = email_timestamp
        
        result = await self.cache.resync_bulk(
            file_proxies=proxies_list,
            auto_delete=True,  # Mark-and-sweep cleanup
            change_receiver=change_receiver,
            upsert_fail_policy=self.upsert_fail_policy
        )
        
        # Update last_received_email_timestamp if we processed any emails
        if max_received_timestamp is not None:
            self.sync_info.last_received_email_timestamp = max_received_timestamp
            logger.debug(f"Updated last_received_email_timestamp to {max_received_timestamp}")
        
        duration = time.time() - start_time
        return {
            'sync_type': 'full',
            'changes': result.changes,
            'failures': result.failures,
            'count': len(result.changes),
            'items_scanned': len(list(result.changes)),  # Approximate
            'duration_secs': duration,
            'errors_encountered': len(result.failures)
        }
    
    async def _fetch_with_retry(self, cutoff: datetime) -> List[FileProxyBase]:
        """
        Fetch emails with exponential backoff retry logic.
        
        Retries on errors with delays: 0.2s, 0.4s, 1.6s
        Stops if the same error message occurs max_consecutive_errors times.
        
        Args:
            cutoff: Datetime cutoff for email fetching
            
        Returns:
            List of FileProxyBase objects
            
        Raises:
            Exception: Re-raises the last exception after max retries with added context
        """
        delays = [0.2, 0.4, 1.6]
        attempt = 0
        
        while attempt <= len(delays):
            try:
                # Call the fetcher and materialize generator to list
                proxies = list(self.email_fetcher(cutoff))
                logger.debug(f"Fetcher returned {len(proxies)} emails")
                return proxies
                
            except Exception as e:
                error_message = f"{type(e).__name__}: {str(e)}"
                
                # Track consecutive identical errors
                if error_message == self._last_error_message:
                    self._consecutive_error_count += 1
                else:
                    self._last_error_message = error_message
                    self._consecutive_error_count = 1
                
                # Check if we've hit max consecutive errors
                if self._consecutive_error_count >= self.max_consecutive_errors:
                    logger.error(
                        f"🛑 Systematic failure detected: Same error occurred "
                        f"{self._consecutive_error_count} times consecutively: {error_message}"
                    )
                    raise
                
                # Check if we have retries left
                if attempt >= len(delays):
                    logger.error(f"Email fetcher failed after {attempt} retries: {error_message}")
                    raise
                
                # Log and retry
                delay = delays[attempt]
                logger.warning(
                    f"Email fetcher attempt {attempt + 1} failed ({error_message}), "
                    f"retrying in {delay}s... "
                    f"(consecutive identical errors: {self._consecutive_error_count}/{self.max_consecutive_errors})"
                )
                await asyncio.sleep(delay)
                attempt += 1
        
        # Should never reach here
        raise RuntimeError("Unexpected state in _fetch_with_retry")
    
    def _reset_error_tracking(self):
        """Reset consecutive error tracking after successful operation."""
        if self._consecutive_error_count > 0:
            logger.debug(f"Resetting error tracking after success "
                        f"(was at {self._consecutive_error_count} consecutive errors)")
        self._last_error_message = None
        self._consecutive_error_count = 0
