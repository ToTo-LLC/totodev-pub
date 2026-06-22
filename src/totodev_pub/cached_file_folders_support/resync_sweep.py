# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
[DEPRECATED] This module is deprecated and will be removed in a future version.

The synchronization logic has been moved to resync_orchestrator.py.
Use ResyncOrchestrator instead of directly using AsyncSyncSession.

This file is kept temporarily for backward compatibility only.

---

Resync Sweep - Concurrent File Synchronization with Mark-and-Sweep Cleanup

This module provides the AsyncSyncSession class which serves as the context manager
for the resync_sweep() method, enabling concurrent file operations with automatic
cleanup of untouched files through mark-and-sweep semantics.

The resync_sweep provides significant performance improvements for batch operations
and brings the cache into alignment regardless of its state. Multiple files can be 
processed simultaneously, dramatically improving performance for batch operations 
with multiple remote files.

Performance Benefits:
- **Concurrent Downloads**: Multiple remote files (SharePoint, HTTP, etc.) are downloaded
  simultaneously instead of sequentially
- **3-20x Faster**: Batch operations with multiple files complete much faster
- **Better Resource Utilization**: Network I/O happens concurrently while waiting

File Retention Policy:
- Old files from upsert operations are staged in a temporary location only while
  the associated change_receiver callback (if any) is executing
- If you need to inspect an old file (from an UPDATE/DELETE change notice), copy
  what you need inside the callback; once the callback returns, the staged copy
  is deleted immediately
- When no change_receiver is provided, the staged artifacts are still removed as
  soon as the upsert/delete operation completes
- All remaining extraneous files (old files, deleted files, slave folders) are
  removed when the context exits (if auto_delete=True)

Thread Safety:
    Only one resync sweep can be active at a time across the entire cache.
    Uses file-based locking for multi-process safety.

RESYNC SWEEP USAGE PATTERNS
===========================

# PATTERN 1: "Start then collect" - Start multiple upserts, collect results at the end
# This pattern is ideal when you want to start all operations and then process results together
async with cache.resync_sweep(["sharepoint", "documents"]) as session:
    # Start all operations (no awaiting needed - they run concurrently)
    session.upsert_file(sp_proxy1)
    session.upsert_file(sp_proxy2)
    session.upsert_file(sp_proxy3)
    session.upsert_file(sp_proxy4)
    session.upsert_file(sp_proxy5)
    session.delete_file("obsolete.pdf")
    
    # Collect all results in one call at the end
    upserted = await session.upserted_list()  # List[ChangeNotice] - only changes
    deleted = await session.deleted_list()    # List[ChangeNotice] - only deletions
    
    # Process all results together
    for notice in upserted:
        if notice.change_type == ChangeType.INSERT:
            print(f"Downloaded: {notice.file_path}")
        elif notice.change_type == ChangeType.UPDATE:
            print(f"Updated: {notice.file_path}")
    
    for notice in deleted:
        print(f"Deleted: {notice.ref_path}")

# PATTERN 2: "Process as completed" - Start upserts and process each as it completes
# This pattern is ideal when you want to process results immediately as they become available
async with cache.resync_sweep(["project", "data"]) as session:
    # Start operations and await each one individually to process immediately
    notice1 = await session.upsert_file(file1)
    if notice1 and notice1.change_type == ChangeType.INSERT:
        print(f"Immediately processed: {notice1.file_path}")
    
staged_info = {}

def capture_old(notice, _proxy):
    if notice.change_type == ChangeType.UPDATE and notice.old is not None:
        staged_info["size"] = notice.old.file_path.stat().st_size

notice2 = await session.upsert_file(file2, change_receiver=capture_old)
if notice2 and notice2.change_type == ChangeType.UPDATE:
    print(f"Updated file: {notice2.file_path}")
    if "size" in staged_info:
        print(f"Old file size during callback: {staged_info['size']} bytes")
    
    notice3 = await session.upsert_file(file3)
    if notice3 and notice3.change_type == ChangeType.INSERT:
        print(f"Another file processed: {notice3.file_path}")
    
    # You can also use asyncio.gather() for multiple immediate results
    notices = await asyncio.gather(
        session.upsert_file(file4),
        session.upsert_file(file5),
        session.upsert_file(file6)
    )
    for notice in notices:
        if notice and notice.change_type == ChangeType.INSERT:
            print(f"Batch processed: {notice.file_path}")

# Performance comparison example
# Sequential (slow): 20 files × 3 seconds = 60 seconds
# Concurrent (fast): max(3 seconds) = 3 seconds (20x improvement!)

# PATTERN 3: "Handle failures gracefully" - Check for failures and retry if needed
async with cache.resync_sweep(["project", "data"]) as session:
    # Start operations
    session.upsert_file(file1)
    session.upsert_file(file2)
    session.upsert_file(file3)
    
    # Check for failures
    failures = await session.failed_upserts()
    if failures:
        print(f"Failed to upsert {len(failures)} files:")
        for failure in failures:
            print(f"  - {failure.ref_path} in {failure.grouping_key}: {failure.exception}")
    
    # Get successful upserts
    upserted = await session.upserted_list()
    print(f"Successfully upserted {len(upserted)} files")

# PATTERN 4: "Fail fast on errors" - Stop on first failure
async with cache.resync_sweep(["project", "data"], upsert_fail_policy="FAIL_FAST") as session:
    try:
        session.upsert_file(file1)
        session.upsert_file(file2)
        # If any upsert fails, an exception will be raised
    except Exception as e:
        print(f"Upsert failed: {e}")
        # Handle the failure appropriately

WORKING WITH SHAREPOINT FILE PROXY FACTORY
==========================================

# Using SharePoint File Proxy Factory for multiple files
from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import SharepointFileProxyFactory

# Create a factory for a specific SharePoint site
sp_factory = SharepointFileProxyFactory(
    site_id="your-site-id",
    drive_id="your-drive-id", 
    access_token="your-access-token"
)

# Create multiple file proxies from the same site
file_paths = ["Documents/doc1.pdf", "Documents/doc2.pdf", "Reports/report.xlsx"]
sp_proxies = [sp_factory.create(path) for path in file_paths]

# Process multiple files (use resync_sweep for better performance)
async with cache.resync_sweep(["sharepoint", "documents"]) as session:
    for proxy in sp_proxies:
        session.upsert_file(proxy)  # Uses session's grouping_key automatically
    
    # Collect results
    upserted = await session.upserted_list()
    for notice in upserted:
        if notice.change_type == ChangeType.INSERT:
            print(f"Downloaded: {notice.file_path}")

# Force updates (skip change detection)
async with cache.resync_sweep(["project", "data"]) as session:
    # Force update specific files regardless of change detection
    session.upsert_file(file1, force=True)  # Always treated as UPDATE
    session.upsert_file(file2, force=True)  # Always treated as UPDATE
    
    # Regular upserts with change detection
    session.upsert_file(file3)  # Only updated if different
    
    upserted = await session.upserted_list()
    print(f"Processed {len(upserted)} files (some forced, some with change detection)")

WORKING WITH EXPECTED DELETES
=============================

# Working with expected_deletes() in resync sweeps
async with cache.resync_sweep(["project", "cleanup"]) as session:
    # Add some files
    session.upsert_file(file1)  # Uses session's grouping_key automatically
    session.upsert_file(file2)  # Uses session's grouping_key automatically
    
    # Check what would be deleted (files not touched during sweep)
    deletions = await session.deleted_list()
    if deletions:
        print(f"Will delete {len(deletions)} untouched files:")
        for notice in deletions:
            print(f"  - {notice.ref_path} (was at {notice.old_file_path})")
    
    # Files are actually deleted when sweep ends (if auto_delete=True)

WORKING WITH FLAT PATTERNS
==========================

# For flat patterns (no interpolation variables), no grouping_key is needed
flat_cache = CachedFileFolders("flat_storage/", "/cache/root")

async with flat_cache.resync_sweep() as session:  # No grouping_key needed
    # All operations use the single flat storage location
    session.upsert_file(file1)  # grouping_key automatically None for flat patterns
    session.upsert_file(file2)  # grouping_key automatically None for flat patterns
    session.delete_file("obsolete.pdf")  # grouping_key automatically None for flat patterns
    
    # Collect results
    upserted = await session.upserted_list()
    print(f"Processed {len(upserted)} files in flat storage")

ADDITIONAL SWEEP BEHAVIOR EXAMPLES
==================================

# Additional examples showing the sweep behavior
async with cache.resync_sweep(["documents"]) as session:
    # Start all operations
    for proxy in document_proxies:
        session.upsert_file(proxy)  # Uses session's grouping_key automatically
    
    # The sweep will automatically clean up any files not touched during this operation
    # Files that existed before but weren't upserted will be deleted when the context exits
    
    # Collect results
    upserted = await session.upserted_list()  # All complete in ~3 seconds total
    print(f"Processed {len(upserted)} files concurrently")
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import warnings
from typing import Optional, List, Sequence, Union, Dict, Any, Callable
import os

from .cache_operations_protocol import CacheOperations
from .sync_types import ChangeType, UpsertFailure
from .change_notice import ChangeNotice
from .file_proxy_base import FileProxyBase

# Type aliases for better readability
GroupingKey = Sequence[str]

# Module logger
logger = logging.getLogger(__name__)


class AsyncSyncSession:
    """Async session object for managing file resynchronization operations with concurrent processing.

    A `change_receiver` may be sync or async; see `ChangeNotice` ("Synchronous vs. async
    receivers") for guidance on choosing.
    """

    def __init__(self, cache: CacheOperations, session_state, auto_delete: bool = True, upsert_fail_policy: str = "RETAIN_OLD", throttle_queue_limits: Optional[Dict[str, int]] = None, change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None):
        self.cache = cache
        self.session_state = session_state
        self.auto_delete = auto_delete
        self.upsert_fail_policy = upsert_fail_policy
        
        # Validate and store change_receiver
        self._validate_change_receiver(change_receiver)
        self.change_receiver = change_receiver
        
        self._deletions_cached = None
        self._pending_materializations = []
        self._pending_operations = []
        
        # Store the session's grouping key for use as default in operations
        self._session_grouping_key = session_state.grouping_key
        
        # Buffers for consume-and-clear protocol
        self._upsert_failures_buffer: List[UpsertFailure] = []
        self._upsert_changes_buffer: List[ChangeNotice] = []
        
        # Throttle queue configuration
        self._throttle_queue_limits = throttle_queue_limits or {}
        self._throttle_semaphores: Dict[str, asyncio.Semaphore] = {}
        
        # Validate throttle_queue_limits
        self._validate_throttle_queue_limits()

    def _validate_change_receiver(self, change_receiver: Optional[Callable]) -> None:
        """Validate that change_receiver accepts 2 arguments (notice, proxy).
        
        Args:
            change_receiver: User-provided change receiver callback
            
        Raises:
            TypeError: If change_receiver doesn't accept exactly 2 positional arguments
            
        Note:
            This validation was added in November 2024 as a breaking change to enforce
            consistent signatures. The validation can be relaxed or removed after 2025-07-01
            once users have had time to migrate.
        """
        if change_receiver is None:
            return
        
        try:
            sig = inspect.signature(change_receiver)
            # Count positional parameters (exclude *args, **kwargs for this count)
            params = [p for p in sig.parameters.values() 
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            param_count = len(params)
            
            if param_count < 2:
                raise TypeError(
                    f"change_receiver must accept 2 arguments (notice: ChangeNotice, proxy: Optional[FileProxyBase]), "
                    f"but only accepts {param_count} argument(s). "
                    f"The proxy argument provides access to file-specific metadata during change events."
                )
        except TypeError:
            # Re-raise our TypeError
            raise
        except Exception as e:
            logger.error(f"Error inspecting change_receiver signature: {e}")
            raise

    def _validate_throttle_queue_limits(self):
        """Validate that throttle_queue_limits only contains non-numeric string keys."""
        for queue_name in self._throttle_queue_limits.keys():
            if not isinstance(queue_name, str):
                raise TypeError(f"Throttle queue names must be strings, got {type(queue_name)}")
            
            # Check if it's a pure numeric string (including floats like "3.14")
            try:
                float(queue_name)
                raise ValueError(f"Throttle queue '{queue_name}' cannot be numeric. "
                               f"Numeric queues (like throttle_queue=5) are created automatically.")
            except ValueError as e:
                if "cannot be numeric" in str(e):
                    # This is our validation error, re-raise it
                    raise
                else:
                    # Not a number - this is good
                    pass

    def _get_throttle_config(self, throttle_queue: Union[int, str, None]):
        """Get throttle configuration for a given throttle_queue parameter."""
        if throttle_queue is None:
            return None, None
        
        if isinstance(throttle_queue, int):
            # Numeric queue - create implicitly with limit = queue value
            queue_name = str(throttle_queue)
            semaphore_limit = throttle_queue
            
            # Get or create semaphore for this numeric queue
            if queue_name not in self._throttle_semaphores:
                self._throttle_semaphores[queue_name] = asyncio.Semaphore(semaphore_limit)
            return queue_name, self._throttle_semaphores[queue_name]
        
        elif isinstance(throttle_queue, str):
            # String queue - must be pre-configured in throttle_queue_limits
            queue_name = throttle_queue
            limit = self._throttle_queue_limits.get(queue_name)
            
            if limit is None:
                raise ValueError(f"Throttle queue '{queue_name}' not found in throttle_queue_limits. "
                               f"Available queues: {list(self._throttle_queue_limits.keys())}")
            
            # Get or create semaphore for this named queue
            if queue_name not in self._throttle_semaphores:
                self._throttle_semaphores[queue_name] = asyncio.Semaphore(limit)
            return queue_name, self._throttle_semaphores[queue_name]
        
        else:
            raise TypeError(f"throttle_queue must be int, str, or None, got {type(throttle_queue)}")

    def upsert_file(self, source_file: Union[FileProxyBase, os.PathLike, str], grouping_key: Optional[GroupingKey] = None, throttle_queue: Union[int, str, None] = None, force: bool = False) -> None:
        """Start an upsert operation and return immediately.
        
        This method starts the upsert operation and returns immediately.
        The operation runs concurrently with other operations.
        Use upserted_list() to collect all results later.
        
        Args:
            source_file: The file to upsert (FileProxyBase instance, path-like object, or string path)
            grouping_key: Sequence of strings that match the grouping_pattern variables.
                         For grouped patterns, this is required and must match the pattern variables.
                         For flat patterns, this must be None.
                         If None, uses the session's grouping_key from the resync_sweep context.
            throttle_queue: Optional throttle queue for controlling concurrency:
                          - int: Creates implicit queue named after the number with limit equal to the number
                          - str: Uses named queue from throttle_queue_limits (must be pre-configured)
                          - None: No throttling, runs immediately
            force: If True, skip all change detection and always treat existing files as updates.
                   When False (default), files are compared to determine if an update is needed.
        """
        # Use session's grouping_key as default if not provided
        if grouping_key is None:
            grouping_key = self._session_grouping_key
            
        # Use the cache's normalization logic to handle flat vs grouped patterns correctly
        grouping_key = self.cache._storage.normalize_grouping_key(grouping_key)

        if hasattr(source_file, "ref_path") and hasattr(source_file, "file_name"):
            file_proxy = source_file
        else:
            from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy  # type: ignore

            file_proxy = LocalFileProxy(str(source_file))

        ref_path = file_proxy.ref_path()
        file_name = file_proxy.file_name()

        target_file_path = self.cache._storage.ref_path_to_filesystem_path(ref_path, file_name, grouping_key)
        target_slave_dir_path = self.cache._storage.get_slave_dir_path(target_file_path)

        existing_file_ref = self.cache.find_file(ref_path, grouping_key)

        from .async_operation_handlers import AsyncUpsertOperation

        async_operation = AsyncUpsertOperation(
            self.cache, file_proxy, ref_path, grouping_key, target_file_path, target_slave_dir_path, existing_file_ref, self, force
        )

        # Determine throttling configuration
        queue_name, semaphore = self._get_throttle_config(throttle_queue)
        
        if semaphore is not None:
            # Create throttled operation
            async def throttled_operation():
                async with semaphore:
                    return await async_operation.execute()
            
            operation_task = asyncio.create_task(throttled_operation())
        else:
            # No throttling - create operation directly
            operation_task = asyncio.create_task(async_operation.execute())
        
        self._pending_operations.append(operation_task)

        self.session_state.touched_files.add(ref_path)

        # Return immediately without awaiting

    def delete_file(self, ref_path: str, grouping_key: Optional[GroupingKey] = None) -> None:
        """Start a delete operation and return immediately.
        
        This method starts the delete operation and returns immediately.
        The operation runs concurrently with other operations.
        Use deleted_list() to collect all results later.
        
        Args:
            ref_path: The reference path of the file to delete
            grouping_key: Sequence of strings that match the grouping_pattern variables.
                         For grouped patterns, this is required and must match the pattern variables.
                         For flat patterns, this must be None.
                         If None, uses the session's grouping_key from the resync_sweep context.
        """
        # Use session's grouping_key as default if not provided
        if grouping_key is None:
            grouping_key = self._session_grouping_key
            
        # Use the cache's normalization logic to handle flat vs grouped patterns correctly
        grouping_key = self.cache._storage.normalize_grouping_key(grouping_key)

        existing_file_ref = self.cache.find_file(ref_path, grouping_key)
        if existing_file_ref is None:
            return

        from .async_operation_handlers import AsyncDeleteOperation

        async_operation = AsyncDeleteOperation(self.cache, ref_path, grouping_key, existing_file_ref, self)

        operation_task = asyncio.create_task(async_operation.execute())
        self._pending_operations.append(operation_task)

        self.session_state.touched_files.add(ref_path)

        # Return immediately without awaiting

    async def deleted_list(self) -> list[ChangeNotice]:
        """Get a list of all files that would be deleted (untouched files).
        
        This method awaits completion of all pending operations and returns
        a list of ChangeNotice objects for files that would be deleted.
        """
        # Ensure all pending operations are completed first
        await self.wait_for_completion()
        
        if self._deletions_cached is None:
            all_changes = await self.cache.end_updates(self.session_state, delete_notices_only=True, change_receiver=self.change_receiver)
            self._deletions_cached = [notice for notice in all_changes if notice.change_type == ChangeType.DELETE]
        return self._deletions_cached

    async def upserted_list(self) -> list[ChangeNotice]:
        """Get a list of all upsert operations that resulted in changes.
        
        This method awaits completion of all pending operations and returns
        a list of ChangeNotice objects for operations that actually triggered changes.
        After returning, the buffer is cleared so subsequent calls return only new changes.
        """
        # Ensure all pending operations are completed first
        await self.wait_for_completion()
        
        # Get all session changes that are not deletions
        changes = [notice for notice in self.session_state.session_changes if notice.change_type != ChangeType.DELETE]
        
        # Clear the buffer after returning (consume-and-clear protocol)
        self._upsert_changes_buffer.clear()
        
        return changes

    async def failed_upserts(self) -> list[UpsertFailure]:
        """Get a list of all upsert operations that failed.
        
        This method awaits completion of all pending operations and returns
        a list of UpsertFailure objects containing failure details.
        After returning, the buffer is cleared so subsequent calls return only new failures.
        
        Returns:
            List of UpsertFailure objects containing all failure details.
            Empty list if no failures occurred.
            
        Example:
            failures = await session.failed_upserts()
            if failures:
                for failure in failures:
                    print(f"Failed: {failure.ref_path} in {failure.grouping_key} - {failure.exception}")
        """
        # Ensure all pending operations are completed first
        await self.wait_for_completion()
        
        # Return a copy and clear the buffer (consume-and-clear protocol)
        failures = self._upsert_failures_buffer.copy()
        self._upsert_failures_buffer.clear()
        
        return failures

    async def wait_for_completion(self) -> None:
        """Wait for all pending operations to complete.
        
        This method is called automatically by the context manager, but can be
        called manually if needed.
        """
        if self._pending_operations:
            results = await asyncio.gather(*self._pending_operations, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    # This is a failure - it should have been handled by the operation
                    # and added to the failures buffer. If we get here, it means
                    # the operation didn't handle it properly according to the policy.
                    if self.upsert_fail_policy == "FAIL_FAST":
                        raise result
                    else:
                        # This shouldn't happen if operations are implemented correctly
                        # Log it as an unexpected error
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.error("Unexpected exception in wait_for_completion: %s", result)
                elif result is not None:
                    self.session_state.session_changes.append(result)
            self._pending_operations.clear()




