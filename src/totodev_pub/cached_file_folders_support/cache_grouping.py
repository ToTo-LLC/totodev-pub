# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Cache Grouping - Facet Pattern for CachedFileFolders

This module provides the CacheGrouping class, which implements a facet pattern
for working with specific groupings within a CachedFileFolders cache. It binds
a grouping key to a cache instance, providing curried convenience methods that
eliminate repetitive grouping_key parameters.

Key Benefits:
- Cleaner API for single-grouping operations
- Type safety through early validation
- Better encapsulation and intent clarity

Example:
    cache = CachedFileFolders("projects/{project}/", "/cache/root")
    
    # Traditional approach - repetitive
    cache.upsert_file(file1, ["webapp"])
    cache.upsert_file(file2, ["webapp"])
    for file in cache.files(["webapp"]):
        process(file)
    
    # Facet approach - cleaner
    webapp = cache.grouping(["webapp"])
    webapp.upsert_file(file1)
    webapp.upsert_file(file2)
    for file in webapp.files():
        process(file)


Side Note: The CachedGroupingVersioner class was created to provide capture and "versioning" of data files within a CacheGrouping (using git as the implementation).  This can be an easy way to manage a collection of interrelated files (such as config files).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple, Union

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders import CachedFileFolders, ChangeNotice, GroupingKey
    from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
    from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
    from totodev_pub.cached_file_folders_support.sync_types import ResyncBulkResult


class CacheGrouping:
    """Facet representing a specific grouping within a CachedFileFolders cache.
    
    This class provides a convenience wrapper that binds a specific grouping_key
    to a parent cache, eliminating the need to repeatedly pass the grouping_key
    parameter to cache operations.
    
    Instances are typically created via `CachedFileFolders.grouping()` or returned
    by `CachedFileFolders.groupings()`.
    
    Note: Some operations like portage() are intentionally excluded - use the
    parent cache for those operations.
    
    Example:
        cache = CachedFileFolders("projects/{project}/docs/", "/cache/root")
        
        # Create facet for a specific project
        webapp = cache.grouping(["webapp", "api"])
        
        # Work with just that grouping
        await webapp.upsert_file(document)
        print(f"Files stored at: {webapp.folder_path}")
        
        for file in webapp.files():
            print(f"  {file.ref_path}")
        
        # Access parent cache if needed
        webapp.parent_cache.portage(webapp.grouping_key)
    """
    
    def __init__(self, parent_cache: CachedFileFolders, grouping_key: Optional[GroupingKey]):
        """Create a facet for a specific grouping.
        
        Note: Typically created via CachedFileFolders.grouping() rather than directly.
        
        Args:
            parent_cache: The parent CachedFileFolders instance
            grouping_key: The grouping key this facet represents
            
        Raises:
            ValueError: If grouping_key doesn't match parent cache's pattern requirements
        """
        self._parent = parent_cache
        self._grouping_key = parent_cache._storage.normalize_grouping_key(grouping_key)
        
        # Validate that this grouping key is valid for the parent pattern
        if parent_cache._requires_grouping and grouping_key is None:
            raise ValueError("Grouping key required for grouped patterns")
        if not parent_cache._requires_grouping and grouping_key is not None:
            raise ValueError("Grouping key not allowed for flat patterns")
    
    # Properties
    
    @property
    def grouping_key(self) -> Optional[GroupingKey]:
        """Returns None for flat patterns, tuple of strings for grouped patterns."""
        return self._grouping_key
    
    @property
    def parent_cache(self) -> CachedFileFolders:
        """Access to cache-level operations like portage() and purge()."""
        return self._parent
    
    @property
    def cache_root_dir(self) -> str:
        """Absolute path to the cache root directory."""
        return self._parent.root_dir
    
    @property
    def root_dir(self) -> str:
        """Deprecated alias retained temporarily to aid migration."""
        raise AttributeError(
            "CacheGrouping.root_dir was removed; use CacheGrouping.cache_root_dir instead. "
            "This compatibility stub will be removed after July 2026."
        )
    
    @property
    def pattern(self) -> str:
        return self._parent.pattern
    
    @property
    def folder_path(self) -> Path:
        """Absolute path to this grouping's root directory (property alias for grouping_root_dir())."""
        return self.grouping_root_dir()
    
    def grouping_root_dir(self) -> Path:
        """Absolute path to this grouping's root directory."""
        return self._parent._storage.category_folders.folder(
            self._grouping_key, create=False
        )
    
    # File operations - curried versions
    
    async def upsert_file(self, source_file: Union[FileProxyBase, os.PathLike, str], 
                         force: bool = False) -> Optional[ChangeNotice]:
        """Add or update a file in this grouping.
        
        See CachedFileFolders.upsert_file() for details.
        """
        return await self._parent.upsert_file(source_file, self._grouping_key, force)
    
    async def delete_file(self, ref_path: str) -> Optional[ChangeNotice]:
        """Delete a file from this grouping.
        
        See CachedFileFolders.delete_file() for details.
        """
        return await self._parent.delete_file(ref_path, self._grouping_key)
    
    def find_file(self, ref_path: str) -> Optional[CachedFileRef]:
        """Find a file in this grouping by its reference path."""
        return self._parent.find_file(ref_path, self._grouping_key)
    
    def file_exists(self, ref_path: str) -> bool:
        """Check if a file exists in this grouping."""
        return self._parent.file_exists(ref_path, self._grouping_key)
    
    def files(self, reverse: bool = False, ref_path_glob: Optional[str] = None) -> Iterator[CachedFileRef]:
        """Iterate files in this grouping.
        
        See CachedFileFolders.files() for details.
        """
        return self._parent.files(self._grouping_key, reverse, ref_path_glob)
    
    def files_count(self) -> int:
        """Count files in this grouping."""
        return self._parent.files_count(self._grouping_key)
    
    def is_empty(self) -> bool:
        """Check if this grouping has no files."""
        return self.files_count() == 0
    
    def get_cached_mtime(self, ref_path: str, 
                        includes: Literal["target_only", "slave_files_only", "both"] = "both") -> Optional[float]:
        """Get newest mtime for a cached entry in this grouping.
        
        See CachedFileFolders.get_cached_mtime() for details on the includes parameter.
        """
        return self._parent.get_cached_mtime(ref_path, self._grouping_key, includes)
    
    def filter_map(self, 
                   glob: str = '*',
                   predicate: Optional[Callable[[CachedFileRef], Any]] = None,
                   mapper: Optional[Callable[[Any], Any]] = None) -> Iterator[Any]:
        """Filter and map files in this grouping.
        
        Provides itertools-style filtering and mapping over cached files.
        
        Args:
            glob: Glob pattern for file filtering
            predicate: Optional filter function. If result is falsy, skip file.
                      If True (literal), pass CachedFileRef to mapper.
                      If truthy (not True), pass result to mapper.
            mapper: Optional transformation function applied to predicate results
            
        Yields:
            Mapped results from files that pass the predicate
            
        Examples:
            # Get all .txt file sizes
            sizes = grouping.filter_map(
                glob='*.txt',
                mapper=lambda f: f.target_file_path.stat().st_size
            )
            
            # Filter by size, then get paths
            large_files = grouping.filter_map(
                predicate=lambda f: f.target_file_path.stat().st_size > 1000,
                mapper=lambda f: f.ref_path
            )
        """
        for file_ref in self.files(ref_path_glob=glob):
            if predicate is not None:
                pred_result = predicate(file_ref)
                if not pred_result:
                    continue
                value = file_ref if pred_result is True else pred_result
            else:
                value = file_ref
                
            if mapper is not None:
                yield mapper(value)
            else:
                yield value
    
    def get_slave_dir(self, ref_path: Optional[str] = None) -> Path:
        """Get slave directory for storing auxiliary files.
        
        Returns per-grouping slave dir if ref_path is None, otherwise per-file slave dir.
        See CachedFileFolders.get_slave_dir() for details.
        
        Examples:
            # Grouping-level slave directory
            app_dir = grouping.get_slave_dir()
            (app_dir / "sync_state.yaml").touch()
            
            # Per-file slave directory
            file_dir = grouping.get_slave_dir("emails/msg.eml")
            (file_dir / "metadata.yaml").touch()
        """
        return self._parent.get_slave_dir(self._grouping_key, ref_path)
    
    # Bulk operations
    
    @asynccontextmanager
    async def resync_sweep(self, auto_delete: bool = True,
                          upsert_fail_policy: str = "RETAIN_OLD",
                          throttle_queue_limits: Optional[Dict[str, int]] = None,
                          change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None):
        """Start a resync sweep session with concurrent file processing and mark-and-sweep cleanup.
        
        See CachedFileFolders.resync_sweep() for details on parameters and usage.
        `change_receiver` may be sync or async; see `ChangeNotice` ("Synchronous vs. async
        receivers") for guidance on choosing.
        """
        async with self._parent.resync_sweep(
            self._grouping_key, auto_delete, 
            upsert_fail_policy, throttle_queue_limits, change_receiver
        ) as session:
            yield session
    
    async def resync_bulk(self, 
                         file_proxies: Union[Iterator[FileProxyBase], List[FileProxyBase], Tuple[FileProxyBase, ...]],
                         auto_delete: bool = True,
                         upsert_fail_policy: str = "RETAIN_OLD",
                         retry_count: int = 1,
                         max_concurrent_requests: int = 5,
                         change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None) -> ResyncBulkResult:
        """Bulk synchronization with concurrent processing and automatic retries.
        
        See CachedFileFolders.resync_bulk() for details on parameters and usage.
        `change_receiver` may be sync or async; see `ChangeNotice` ("Synchronous vs. async
        receivers") for guidance on choosing.
        """
        return await self._parent.resync_bulk(
            file_proxies, self._grouping_key, auto_delete,
            upsert_fail_policy, retry_count, max_concurrent_requests, change_receiver
        )
    
    # Grouping-specific operations
    
    def get_last_resync_sweep_timestamp(self) -> Optional[float]:
        """Unix timestamp of last resync sweep start, or None if no sweep occurred."""
        return self._parent.get_last_resync_sweep_timestamp(self._grouping_key)
    
    def purge(self, dry_run: bool = False) -> List[Path]:
        """Delete all files in this grouping.
        
        WARNING: This operation is unrecoverable and should be used carefully.
        See CachedFileFolders.purge() for details.
        """
        return self._parent.purge(self._grouping_key, dry_run)
    
    # Low-level operations
    
    def move_file(self, old_ref_path: str, new_ref_path: str, new_grouping_key: Optional[GroupingKey] = None, overwrite: bool = False) -> "CachedFileRef":
        """Move a file within or out of this grouping by changing its ref_path and/or grouping.
        
        This is a thin wrapper around parent_cache.move_file(), binding the source grouping
        to this facet. See parent method for full semantics and caveats.
        
        Args:
            old_ref_path: Existing ref_path in this grouping.
            new_ref_path: New ref_path to assign.
            new_grouping_key: Optional destination grouping key (defaults to this grouping).
            overwrite: Replace destination if already exists.
        
        Returns:
            CachedFileRef at the new location.
        """
        return self._parent.move_file(
            old_ref_path=old_ref_path,
            new_ref_path=new_ref_path,
            grouping_key=self._grouping_key,
            new_grouping_key=new_grouping_key,
            overwrite=overwrite,
        )
    
    # Comparison and hashing
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CacheGrouping):
            return NotImplemented
        return self.grouping_root_dir() == other.grouping_root_dir()
    
    def __hash__(self) -> int:
        return hash(self.grouping_root_dir())
    
    def __lt__(self, other: object) -> bool:
        """Lexicographic ordering by grouping_key."""
        if not isinstance(other, CacheGrouping):
            return NotImplemented
        if self._parent is not other._parent:
            raise ValueError("Cannot compare groupings from different caches")
        return self._grouping_key < other._grouping_key
    
    def __le__(self, other: object) -> bool:
        if not isinstance(other, CacheGrouping):
            return NotImplemented
        if self._parent is not other._parent:
            raise ValueError("Cannot compare groupings from different caches")
        return self._grouping_key <= other._grouping_key
    
    def __gt__(self, other: object) -> bool:
        if not isinstance(other, CacheGrouping):
            return NotImplemented
        if self._parent is not other._parent:
            raise ValueError("Cannot compare groupings from different caches")
        return self._grouping_key > other._grouping_key
    
    def __ge__(self, other: object) -> bool:
        if not isinstance(other, CacheGrouping):
            return NotImplemented
        if self._parent is not other._parent:
            raise ValueError("Cannot compare groupings from different caches")
        return self._grouping_key >= other._grouping_key
    
    def __repr__(self) -> str:
        return f"CacheGrouping(grouping_key={self._grouping_key!r}, root={self.cache_root_dir})"
    
    def __str__(self) -> str:
        if self._grouping_key:
            key_str = "/".join(str(k) for k in self._grouping_key)
        else:
            key_str = "(root)"
        return f"<CacheGrouping: {key_str}>"

