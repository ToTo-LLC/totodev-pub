# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Resync Orchestrator - External coordination for cache synchronization

This module provides external orchestration for cache resynchronization using
optimistic concurrency control (mtime-based) instead of file locking.

Key features:
- Lock-free operation using mtime verification
- Concurrent file operations with throttle control
- Mark-and-sweep cleanup with safety checks
- Change tracking and failure handling
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Sequence, Union, Dict, Callable
import os

from .cache_operations_protocol import CacheOperations
from .sync_types import ChangeType, UpsertFailure
from .change_notice import ChangeNotice
from .file_proxy_base import FileProxyBase, LocalRetentionRecommendation
from .cached_file_ref import CachedFileRef

# Type aliases for better readability
GroupingKey = Sequence[str]

# Module logger
logger = logging.getLogger(__name__)


@dataclass
class FileSnapshot:
    """
    Immutable snapshot of a file's state at a point in time.
    Used for optimistic concurrency control during mark-and-sweep.
    """
    ref_path: str
    file_path: Path
    mtime_ns: int
    size: int
    grouping_key: Optional[tuple]


class ResyncOrchestrator:
    """
    External orchestrator for cache resynchronization using optimistic concurrency.
    
    Unlike traditional lock-based approaches, this orchestrator:
    - Captures file snapshots (with mtime) at start
    - Performs concurrent upserts/deletes without holding locks
    - Only deletes untouched files if their mtime hasn't changed
    - Prevents data loss from concurrent updates during sweep
    
    This enables maximum concurrency while maintaining safety through
    optimistic concurrency control.
    """
    
    def __init__(self,
                 cache: CacheOperations,
                 grouping_key: Optional[GroupingKey] = None,
                 auto_delete: bool = True,
                 upsert_fail_policy: str = "RETAIN_OLD",
                 max_concurrency: int = 5,
                 throttle_queue_limits: Optional[Dict[str, int]] = None,
                 change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None,
                 record_sweep_timestamp: bool = True,
                 retry_count: int = 0,
                 expand_nested: bool = True):
        """
        Initialize orchestrator.
        
        Args:
            cache: CachedFileFolders instance to orchestrate
            grouping_key: Optional grouping key to limit scope
            auto_delete: Whether to delete untouched files on exit
            upsert_fail_policy: "RETAIN_OLD", "DELETE_OLD", or "FAIL_FAST"
            max_concurrency: Default concurrency limit
            throttle_queue_limits: Per-queue concurrency limits (e.g., {"gmail": 3})
            change_receiver: Callback(notice, proxy) for change notifications. May be sync or
                async; see `ChangeNotice` ("Synchronous vs. async receivers") for guidance.
            record_sweep_timestamp: Whether to record sweep start time
            retry_count: Number of retry attempts for bulk_sync() (default: 0, no retries)
            expand_nested: Whether to auto-expand nested proxies in bulk_sync() (default: True)
        """
        self.cache = cache
        self.grouping_key = grouping_key
        self.auto_delete = auto_delete
        self.upsert_fail_policy = upsert_fail_policy
        self.max_concurrency = max_concurrency
        self.record_sweep_timestamp = record_sweep_timestamp
        self.retry_count = retry_count
        self.expand_nested = expand_nested
        
        # Validate and store change_receiver
        self._validate_change_receiver(change_receiver)
        self.change_receiver = change_receiver
        
        # Session state (managed internally, not via external session_state)
        self._snapshots: List[FileSnapshot] = []
        self._touched: set[str] = set()
        self._pending_operations: List[asyncio.Task] = []
        self._changes: List[ChangeNotice] = []
        
        # Buffers for consume-and-clear protocol
        self._upsert_failures_buffer: List[UpsertFailure] = []
        self._upsert_changes_buffer: List[ChangeNotice] = []
        self._deletions_cached: Optional[List[ChangeNotice]] = None
        
        # Throttle queue configuration
        self._throttle_queue_limits = throttle_queue_limits or {}
        self._throttle_semaphores: Dict[str, asyncio.Semaphore] = {}
        
        # Validate throttle_queue_limits
        self._validate_throttle_queue_limits()
        
        # Timing
        self._start_time: float = 0
    
    def _validate_change_receiver(self, change_receiver: Optional[Callable]) -> None:
        """Validate that change_receiver accepts 2 arguments (notice, proxy)."""
        if change_receiver is None:
            return
        
        try:
            sig = inspect.signature(change_receiver)
            params = [p for p in sig.parameters.values() 
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            param_count = len(params)
            
            if param_count < 2:
                raise TypeError(
                    f"change_receiver must accept 2 arguments (notice: ChangeNotice, proxy: Optional[FileProxyBase]), "
                    f"but only accepts {param_count} argument(s)."
                )
        except TypeError:
            raise
        except Exception as e:
            logger.error(f"Error inspecting change_receiver signature: {e}")
            raise
    
    def _validate_throttle_queue_limits(self):
        """Validate that throttle_queue_limits only contains non-numeric string keys."""
        for queue_name in self._throttle_queue_limits.keys():
            if not isinstance(queue_name, str):
                raise TypeError(f"Throttle queue names must be strings, got {type(queue_name)}")
            
            try:
                float(queue_name)
                raise ValueError(f"Throttle queue '{queue_name}' cannot be numeric. "
                               f"Numeric queues (like throttle_queue=5) are created automatically.")
            except ValueError as e:
                if "cannot be numeric" in str(e):
                    raise
                else:
                    pass  # Not a number - this is good
    
    def _get_throttle_config(self, throttle_queue: Union[int, str, None]):
        """Get throttle configuration for a given throttle_queue parameter."""
        if throttle_queue is None:
            return None, None
        
        if isinstance(throttle_queue, int):
            queue_name = str(throttle_queue)
            semaphore_limit = throttle_queue
            
            if queue_name not in self._throttle_semaphores:
                self._throttle_semaphores[queue_name] = asyncio.Semaphore(semaphore_limit)
            return queue_name, self._throttle_semaphores[queue_name]
        
        elif isinstance(throttle_queue, str):
            queue_name = throttle_queue
            limit = self._throttle_queue_limits.get(queue_name)
            
            if limit is None:
                raise ValueError(f"Throttle queue '{queue_name}' not found in throttle_queue_limits. "
                               f"Available queues: {list(self._throttle_queue_limits.keys())}")
            
            if queue_name not in self._throttle_semaphores:
                self._throttle_semaphores[queue_name] = asyncio.Semaphore(limit)
            return queue_name, self._throttle_semaphores[queue_name]
        
        else:
            raise TypeError(f"throttle_queue must be int, str, or None, got {type(throttle_queue)}")
    
    async def __aenter__(self) -> 'ResyncOrchestrator':
        """Initialize sweep: capture file snapshots with mtime/size."""
        self._start_time = time.time()
        
        # Record sweep timestamp if requested
        if self.record_sweep_timestamp:
            self.cache._write_last_sweep_timestamp(self._start_time, self.grouping_key)
        
        # Capture snapshot with mtime for each file
        for file_ref in self.cache.files(self.grouping_key):
            try:
                stat = file_ref.file_path.stat()
                self._snapshots.append(FileSnapshot(
                    ref_path=file_ref.ref_path,
                    file_path=file_ref.file_path,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    grouping_key=self.grouping_key
                ))
            except FileNotFoundError:
                # File disappeared between listing and stat - skip it
                logger.debug(f"File {file_ref.ref_path} disappeared during snapshot")
                continue
        
        logger.info(f"Snapshot captured {len(self._snapshots)} files in grouping {self.grouping_key}")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Complete sweep: wait for operations, delete untouched files with mtime verification."""
        try:
            # Wait for all pending operations
            await self.wait_for_completion()
            
            if not self.auto_delete:
                logger.info("auto_delete=False, skipping untouched file deletion")
                return
            
            # Mark-and-sweep: delete untouched files with mtime safety check
            deleted_count = 0
            skipped_count = 0
            
            for snapshot in self._snapshots:
                if snapshot.ref_path in self._touched:
                    continue  # We touched it during sweep, keep it
                
                # Optimistic concurrency check: verify file hasn't changed
                try:
                    current_stat = snapshot.file_path.stat()
                    
                    # Check if file was modified during sweep
                    if current_stat.st_mtime_ns != snapshot.mtime_ns:
                        logger.info(
                            f"Preserving {snapshot.ref_path} - modified during sweep "
                            f"(mtime changed from {snapshot.mtime_ns} to {current_stat.st_mtime_ns})"
                        )
                        skipped_count += 1
                        continue
                    
                    # Extra safety: check size too
                    if current_stat.st_size != snapshot.size:
                        logger.info(
                            f"Preserving {snapshot.ref_path} - size changed during sweep "
                            f"(from {snapshot.size} to {current_stat.st_size})"
                        )
                        skipped_count += 1
                        continue
                    
                    # Safe to delete - file unchanged since snapshot
                    notice = await self.cache.delete_file(snapshot.ref_path, self.grouping_key)
                    if notice:
                        self._changes.append(notice)
                        if self.change_receiver:
                            if asyncio.iscoroutinefunction(self.change_receiver):
                                await self.change_receiver(notice, None)
                            else:
                                self.change_receiver(notice, None)
                        deleted_count += 1
                    
                except FileNotFoundError:
                    # Already deleted - that's fine
                    logger.debug(f"File {snapshot.ref_path} already deleted")
                    continue
            
            duration = time.time() - self._start_time
            logger.info(
                f"Sweep completed in {duration:.2f}s: "
                f"{len(self._changes)} total changes, {deleted_count} deletions, "
                f"{skipped_count} skipped (modified during sweep), "
                f"{len(self._upsert_failures_buffer)} failures"
            )
            
        except Exception as e:
            logger.error(f"Error during sweep cleanup: {e}", exc_info=True)
            raise
    
    def upsert_file(self, source_file: Union[FileProxyBase, os.PathLike, str], 
                    grouping_key: Optional[GroupingKey] = None,
                    throttle_queue: Union[int, str, None] = None,
                    force: bool = False) -> None:
        """
        Start an upsert operation and return immediately.
        
        Args:
            source_file: File to upsert (FileProxyBase, path-like, or string)
            grouping_key: Grouping key (uses session's if None)
            throttle_queue: Concurrency control (int, str, or None)
            force: Skip change detection, always treat as update
        """
        # Use session's grouping_key as default if not provided
        if grouping_key is None:
            grouping_key = self.grouping_key
        
        # Normalize grouping_key
        grouping_key = self.cache._storage.normalize_grouping_key(grouping_key)
        
        # Convert to FileProxy if needed
        if hasattr(source_file, "ref_path") and hasattr(source_file, "file_name"):
            file_proxy = source_file
        else:
            from .file_proxy_local_file import LocalFileProxy
            file_proxy = LocalFileProxy(str(source_file))
        
        ref_path = file_proxy.ref_path()
        file_name = file_proxy.file_name()
        
        target_file_path = self.cache._storage.ref_path_to_filesystem_path(ref_path, file_name, grouping_key)
        target_slave_dir_path = self.cache._storage.get_slave_dir_path(target_file_path)
        existing_file_ref = self.cache.find_file(ref_path, grouping_key)
        
        from .async_operation_handlers import AsyncUpsertOperation
        
        async_operation = AsyncUpsertOperation(
            self.cache, file_proxy, ref_path, grouping_key, 
            target_file_path, target_slave_dir_path, existing_file_ref, self, force
        )
        
        # Determine throttling configuration
        queue_name, semaphore = self._get_throttle_config(throttle_queue)
        
        if semaphore is not None:
            async def throttled_operation():
                async with semaphore:
                    return await async_operation.execute()
            operation_task = asyncio.create_task(throttled_operation())
        else:
            operation_task = asyncio.create_task(async_operation.execute())
        
        self._pending_operations.append(operation_task)
        self._touched.add(ref_path)
    
    def delete_file(self, ref_path: str, grouping_key: Optional[GroupingKey] = None) -> None:
        """
        Start a delete operation and return immediately.
        
        Args:
            ref_path: Reference path of file to delete
            grouping_key: Grouping key (uses session's if None)
        """
        # Use session's grouping_key as default
        if grouping_key is None:
            grouping_key = self.grouping_key
        
        grouping_key = self.cache._storage.normalize_grouping_key(grouping_key)
        
        existing_file_ref = self.cache.find_file(ref_path, grouping_key)
        if existing_file_ref is None:
            return
        
        from .async_operation_handlers import AsyncDeleteOperation
        
        async_operation = AsyncDeleteOperation(
            self.cache, ref_path, grouping_key, existing_file_ref, self
        )
        
        operation_task = asyncio.create_task(async_operation.execute())
        self._pending_operations.append(operation_task)
        self._touched.add(ref_path)
    
    async def wait_for_completion(self) -> None:
        """Wait for all pending operations to complete."""
        if self._pending_operations:
            results = await asyncio.gather(*self._pending_operations, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    if self.upsert_fail_policy == "FAIL_FAST":
                        raise result
                    else:
                        # Log with full traceback for debugging
                        logger.error(
                            "Exception in async operation (policy=%s, will continue): %s",
                            self.upsert_fail_policy,
                            type(result).__name__,
                            exc_info=result
                        )
                        # Exception swallowed per RETAIN_OLD policy, but fully logged for debugging
                elif result is not None:
                    self._changes.append(result)
            self._pending_operations.clear()
    
    async def upserted_list(self) -> List[ChangeNotice]:
        """
        Get list of all upsert changes (INSERT/UPDATE).
        Waits for completion first. Clears buffer after returning.
        """
        await self.wait_for_completion()
        
        changes = [notice for notice in self._changes if notice.change_type != ChangeType.DELETE]
        self._upsert_changes_buffer.clear()
        
        return changes
    
    async def deleted_list(self) -> List[ChangeNotice]:
        """
        Get list of all deletion changes.
        Waits for completion first. Returns preview of what will be deleted.
        """
        await self.wait_for_completion()
        
        if self._deletions_cached is None:
            # Preview deletions based on current snapshot
            self._deletions_cached = []
            for snapshot in self._snapshots:
                if snapshot.ref_path not in self._touched:
                    # Would be deleted
                    slave_dir_path = self.cache._storage.get_slave_dir_path(snapshot.file_path)
                    self._deletions_cached.append(ChangeNotice(
                        file_name=snapshot.file_path.name,
                        old=CachedFileRef(
                            ref_path=snapshot.ref_path,
                            grouping_key=self.grouping_key,
                            file_path=snapshot.file_path,
                            slave_dir_path=slave_dir_path
                        )
                    ))
        
        return self._deletions_cached
    
    async def failed_upserts(self) -> List[UpsertFailure]:
        """
        Get list of all upsert failures.
        Waits for completion first. Clears buffer after returning.
        """
        await self.wait_for_completion()
        
        failures = self._upsert_failures_buffer.copy()
        self._upsert_failures_buffer.clear()
        
        return failures
    
    def get_changes(self) -> List[ChangeNotice]:
        """Get all changes recorded so far (does not wait)."""
        return self._changes.copy()
    
    def get_failures(self) -> List[UpsertFailure]:
        """Get all failures recorded so far (does not wait)."""
        return self._upsert_failures_buffer.copy()
    
    def get_touched_files(self) -> set[str]:
        """Get set of ref_paths touched during sweep."""
        return self._touched.copy()
    
    async def bulk_sync(self, file_proxies: Union[list, tuple]) -> 'ResyncBulkResult':
        """
        Perform bulk synchronization with retry and nested expansion.
        
        Uses self.retry_count for retry attempts and self.expand_nested for nested proxy handling.
        
        Process:
        1. First pass: Upsert all parent proxies
        2. Track which parents changed
        3. Expand nested proxies only for changed parents
        4. Retry failed operations up to retry_count times
        5. Return ResyncBulkResult with all changes and failures
        """
        from .sync_types import ResyncBulkResult
        
        all_changes = []
        all_failures = []
        
        # Convert to list if needed for retries
        if not isinstance(file_proxies, (list, tuple)):
            file_proxies = list(file_proxies)
        
        remaining_files = list(file_proxies)
        
        for attempt in range(self.retry_count + 1):
            if attempt > 0 and not remaining_files:
                break  # No more files to retry
            
            # Track which parent proxies changed for nested expansion
            changed_parent_refs = set()
            proxy_map = {}
            
            # Phase 1: Upsert all parent proxies (skip EXCLUDE-recommended ones)
            for file_proxy in remaining_files:
                if file_proxy.local_retention_recommendation() == LocalRetentionRecommendation.EXCLUDE:
                    continue  # not touched; prior cached entry falls to sweep deletion
                self.upsert_file(file_proxy)
                proxy_map[file_proxy.ref_path()] = file_proxy
            
            # Phase 2: Wait for parents to complete and collect results
            await self.wait_for_completion()
            
            # Get all changes so far (parent changes)
            parent_changes = [notice for notice in self._changes if notice.change_type != ChangeType.DELETE]
            
            # Track which parents changed for nested expansion
            for notice in parent_changes:
                if notice.cur and hasattr(notice.cur, 'ref_path'):
                    changed_parent_refs.add(notice.cur.ref_path)
            
            # Phase 3: Expand nested proxies only for changed parents
            if self.expand_nested:
                proxies_to_expand = [
                    proxy_map[ref] for ref in changed_parent_refs 
                    if ref in proxy_map and hasattr(proxy_map[ref], 'nested_proxies')
                ]
                for proxy in proxies_to_expand:
                    try:
                        for nested_proxy in proxy.nested_proxies():
                            if nested_proxy.local_retention_recommendation() == LocalRetentionRecommendation.EXCLUDE:
                                continue
                            self.upsert_file(nested_proxy)
                    except Exception as e:
                        logger.warning(
                            "Failed to expand nested proxies for %s (will skip and continue): %s",
                            proxy.ref_path(),
                            type(e).__name__,
                            exc_info=True
                        )
                        # Intentionally continuing - one proxy's failure shouldn't stop others
                
                # Wait for nested proxies to complete
                await self.wait_for_completion()
            
            # Phase 4: Collect all results (includes both parent and nested changes)
            attempt_changes = [notice for notice in self._changes if notice.change_type != ChangeType.DELETE]
            attempt_failures = self._upsert_failures_buffer.copy()
            
            # Clear buffers for next attempt
            self._changes.clear()
            self._upsert_failures_buffer.clear()
            
            # Clear old ref for convenience (user can't access it after method returns)
            for notice in attempt_changes:
                notice.old = None
                all_changes.append(notice)
            
            # If this is the last attempt, add all failures
            if attempt == self.retry_count:
                all_failures.extend(attempt_failures)
            else:
                # Prepare for retry
                remaining_files = [failure.file_proxy for failure in attempt_failures]
        
        return ResyncBulkResult(all_changes, all_failures)


# Backward compatibility alias (deprecated)
BulkResyncOrchestrator = ResyncOrchestrator
