# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Cached File Folders - Synchronized File Caching with Automatic Cleanup
========================================================================

A database-like structure in the local filesystem for storing and synchronizing
files with automatic change detection and cleanup. Originally built for mirroring
remote document stores (SharePoint, Gmail, etc.), but has broad applications for
any medium-scale file caching needs (thousands of files).  This file is the main
entry point for a family of classes.


WHEN TO USE
-----------
- Mirror SharePoint/Dropbox document libraries for faster processing (see file_proxy_sharepoint.py and examples/sharepoint_tutorial.py)
- Cache API responses and remote files for offline analysis
- Sync email attachments or website content for local processing (see file_proxy_outlook_email.py and examples/outlook_email_sync.py)
- Maintain local copies of database exports for ETL pipelines
- Manage work-in-progress files for long-running tasks
- Best for: thousands (or fewer) files per logical grouping


QUICK EXAMPLE
-------------
    from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import SharepointFileProxyFactory
    
    # Create cache with a grouping pattern
    cache = CachedFileFolders("projects/{project_id}/", "/cache/root")
    
    # Set up file proxies for remote files
    sp_factory = SharepointFileProxyFactory(site_id="...", drive_id="...", access_token="...")
    file_proxies = [
        sp_factory.create("Documents/config.json"),
        sp_factory.create("Documents/schema.sql")
    ]
    
    # Bulk synchronization with automatic change detection
    changes, failures = await cache.resync_bulk(file_proxies, ["webapp", "documents"])
    print(f"Downloaded {len(changes)} files, {len(failures)} failed")


CORE CONCEPTS
-------------

File Identity:
    Each cached file is identified by a unique key consisting of two parts:
    - grouping_key: Tuple[str] - Groups files into logical categories
    - ref_path: str - Arbitrary identifier (typically looks like a URL or file path)

Physical Locations:
    Each cached file has two associated filesystem locations:
    - file_path: Path - The actual cached file location
    - slave_dir_path: Path - Directory for auxiliary files (metadata, attachments, etc.)

Cache Groupings:
    For simpler APIs, use CacheGrouping objects instead of CachedFileFolders directly.
    Access via grouping() or groupings() methods. Each grouping has its own slave_dir
    for collective application state. A cache grouping is a group of cached files
    that share a common grouping_key.

Change Detection:
    Built-in change detection during "resync" operations signals whether an upsert represents:
    - New file added
    - Existing file modified (with access to both old and new versions)
    - No-op (file unchanged)

File Proxies:
    Load files via proxy objects (FileProxyBase subclasses) for:
    - Remote sources (SharePoint, Gmail, etc.)
    - Data-to-file conversion (emails, in-memory structures)
    - Lazy-loading and async processing
    - Bulk operations


HOW IT WORKS
------------

Storage Layout:
    Files are stored in a semantic directory structure under a root directory.
    The location is determined by grouping_pattern, grouping_key, and ref_path.
    While not intended for direct traversal, developers can inspect the physical
    storage during development.

Working with Files:
    Files can be added directly or via proxy objects. The cache supports:
    - Individual upserts with change detection
    - Bulk operations for efficiency
    - Mark-and-sweep "resync" operations to align with remote sources
      (see resync_sweep() and resync_bulk() methods)

Metadata Support:
    Convenience methods access a "metadata" file in each slave directory,
    assuming YAML or JSON format with known filename patterns.

Database Portability:
    Uses SQLite with optional "portage files" - human-readable JSONL snapshots
    for version control and portability.


USAGE PATTERNS
--------------

Common usage pattern is to have one section of your code responsible for
loading the cache and a second which uses the uses the cache but doesn't
have elaborte loading responsibilities.  Loaders will need to understand
details about cache structure, ref_path structure, and proxies.  Non-loaders
need to know much less about cache details and will typically either iterate over files in the
cache or do rapid lookups.


ADVANCED FEATURES
-----------------
- CachedGroupingVersioner: Git-based versioning for snapshot/restore operations
  (see cached_file_folders_support.cached_folders_versioner)
- Async operation handlers for parallel file processing
- Custom file proxy implementations for any data source
- examples are provided in the cached_file_folders_support/examples directory

For detailed usage patterns, file retention semantics, and advanced features,
see the CachedFileFolders class documentation below.
"""

# Migration roadmap: once all persisted entries have been rewritten with grouping-root-relative
# paths, remove the absolute-path fallback logic in storage and manifest validation to make cache
# relocation fully transparent.

# Standard library imports
import asyncio
import fnmatch
import glob
import logging
import os
import re
import shutil
import time
import uuid
import warnings
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import ( Any, Callable, Dict, Iterator, List, Literal, Optional, Sequence, Tuple, Union,)
from dataclasses import dataclass

# Type aliases for better readability
GroupingKey = Sequence[str]
RefPath = str

# Third-party imports
from pydantic import BaseModel, PrivateAttr, ValidationError, model_validator

# Local application imports
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping
from totodev_pub.cached_file_folders_support.cache_manifest import (
    CachedFileFoldersManifest,
)
from totodev_pub.cached_file_folders_support.cached_file_ref import (
    CachedFileRef,  # Re-exported for backward compatibility
)
from totodev_pub.cached_file_folders_support.change_notice import (
    ChangeNotice,  # Re-exported for backward compatibility
)
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from totodev_pub.cached_file_folders_support.resync_sweep import (
    AsyncSyncSession as SharedAsyncSyncSession,
)
from totodev_pub.cached_file_folders_support.storage_manager import CachedFileStorageManager
from totodev_pub.cached_file_folders_support.sync_types import (
    ChangeType,
    ResyncBulkResult,
)
# CategoryFolders is now handled internally by CachedFileStorageManager
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData 

# Module-level logger
logger = logging.getLogger(__name__)

# Constants
SLAVE_DIR_EXTENSION = "._slave"  # appended to filename to make slave directory
GROUPING_KEY_FOR_UNIGROUP = ()  # empty tuple representing the default grouping key for flat patterns
TEMP_RETENTION_GRACE_SECONDS = 120  # seconds before temp artifacts are force-cleaned

# Cleanup policy constants (moved to storage manager)


# ChangeType imported from shared types
# ChangeNotice imported from change_notice module (re-exported for backward compatibility)


class CachedFileFolders:
    """
    Main class for managing synchronized file caches with automatic cleanup and change detection.
    
    This is the primary entry point for creating and managing a cache. For simpler operations
    within a single grouping, consider using CacheGrouping objects via grouping() or groupings().
    
    
    INITIALIZATION PARAMETERS
    -------------------------
    
    grouping_pattern: str
        Controls how files are organized and subdivided within the cache for efficiency and 
        namespace segmentation. Two patterns are supported:
        
        - **Flat pattern** (no grouping): "cache/" or "storage/"
          All files in one directory. Must pass grouping_key=None in all operations.
        
        - **Grouped pattern** (with categorization): "projects/{project}/" or "{org}/{team}/"
          Files subdivided by grouping_key values. Variables in braces become grouping dimensions.
          Example: "projects/{project}/" with grouping_key=("webapp",) → "projects/webapp/"
    
    root_dir: str
        **MUST be an absolute path**. The cache root directory on the local filesystem.
        If the directory doesn't exist, it will be created automatically (parent must exist).
        Relative paths will raise ValueError.
    
    use_xxhash: bool = False
        If True, uses xxhash+size for change detection (faster, non-portable).
        If False, uses mtime+size (portable across systems, slightly slower).
        Choose xxhash when cache never moves between systems.
    
    char_replacement_map: Optional[Dict[str, str]] = None
        Custom character replacements for sanitizing ref_path values into filesystem paths.
        If None, uses default replacements (e.g., "/" → "∕", ":" → "꞉").
        Rarely needs customization.
    
    metadata_filename: str = "metadata.yaml"
        Default filename for optional per-file metadata in slave directories.
        Change only if "metadata.yaml" conflicts with your file naming.
    
    
    PRIMARY API METHODS
    -------------------
    
    Construction & Inspection:
        grouping()           → Get CacheGrouping facet for a specific grouping_key
        groupings()          → Iterate over all CacheGrouping facets in the cache
        get_slave_dir()      → Get slave directory path for a file or grouping
    
    Adding/Updating Files:
        upsert_file()        → Add or update a single file (returns ChangeNotice)
        resync_bulk()        → Bulk upsert of multiple files with change tracking
        resync_sweep()       → Context manager for bulk operations with automatic cleanup
    
    Reading/Finding Files:
        files()              → Iterate over cached files with optional filtering
        get_cached_file()    → Look up a single file by grouping_key + ref_path
    
    Deletion & Cleanup:
        delete_file()        → Remove a single file from the cache
        delete_grouping_key() → Remove all files for a specific grouping_key
        (Passive cleanup happens automatically during upsert/delete operations)
    
    
    KEY BEHAVIORAL GUARANTEES
    -------------------------
    
    Change Receiver Window:
        Old file versions from UPDATE/DELETE operations are retained only for the duration of
        any registered change_receiver callback. Once the callback returns, the staged copy is
        deleted immediately.
    
    Multi-Process Safety:
        Uses file-based locking to ensure safe concurrent access from multiple processes.
        Multiple readers are allowed; writers acquire exclusive locks.
    
    Flat Pattern Restrictions:
        When using flat patterns (no {} variables), grouping_key MUST be None in all operations.
        Passing any other value will raise ValueError.
    
    
    CACHE GROUPING vs. CACHED FILE FOLDERS
    ---------------------------------------
    
    Use CacheGrouping when:
        - Working within a single grouping_key
        - Want simpler API without repeatedly passing grouping_key
        - Need grouping-specific operations (files_count, metadata access, etc.)
        - Example: grouping = cache.grouping(("project", "docs"))
    
    Use CachedFileFolders directly when:
        - Creating/configuring the cache
        - Operating across multiple grouping_key values
        - Need cache-wide operations (all files, cross-group queries)
    
    
    RETURN TYPES & KEY CLASSES
    ---------------------------
    
    CachedFileRef:
        Returned by files() and get_cached_file(). Represents a cached file with properties:
        file_path, slave_dir_path, ref_path, grouping_key, and metadata() accessor.
    
    ChangeNotice:
        Returned by upsert operations. Indicates change_type (INSERT, UPDATE, NOOP) and
        provides access to old and current file references.
    
    CacheGrouping:
        Returned by groupings() and grouping(). A facet providing simplified API for
        operations within a specific grouping_key.
    
    
    COMMON PATTERNS
    ---------------
    
    Pattern 1: Standalone upsert with change detection
        notice = cache.upsert_file(file_proxy, ("project", "docs"))
        if notice.change_type == ChangeType.INSERT:
            print(f"New file: {notice.cur.ref_path}")
    
    Pattern 2: Bulk sync with automatic cleanup
        async with cache.resync_sweep(("project", "docs")) as session:
            session.upsert_file(proxy1)
            session.upsert_file(proxy2)
            # Old files automatically removed at context exit
    
    Pattern 3: Finding and filtering files
        for file_ref in cache.files({"project": "webapp*"}, ref_path_glob="*.pdf"):
            process(file_ref.file_path)
    
    
    SEE ALSO
    --------
    - CacheGrouping: Simpler API for single-grouping operations
    - Module docstring: Conceptual overview and longer examples
    - cached_file_folders_support/examples/: Complete working examples
    - FileProxyBase subclasses: For loading from specific sources (SharePoint, Gmail, etc.)
    """
    
    # Debug/Dev feature: Automatically generate portage files after each resync_bulk operation.
    # This provides human-readable JSONL snapshots of the cache state for easier debugging and inspection.
    # Should typically only be enabled in DEV and TEST environments due to performance impact.
    _portageAfterAllResync: bool = False

    def __init__(self, grouping_pattern: str,  # e.g. "groups/{project}/{category}/" or "flat/" for no grouping
                 root_dir: Union[str, os.PathLike],  # absolute path; creates if missing (parent must exist)
                 use_xxhash: bool = False,  # use xxhash+size instead of mtime+size for comparison
                 slave_dir_extension: str = SLAVE_DIR_EXTENSION,
                 char_replacement_map: Optional[Dict[str, str]] = None,
                 metadata_filename: str = "metadata.yaml"  # name for optional metadata file in slave dirs
                ):
        """Initialize CachedFileFolders. See class docstring for detailed usage."""
        # Normalize root_dir to Path (accepts str or Path-like, must be absolute)
        root_path = Path(root_dir)
        if not root_path.is_absolute():
            raise ValueError("root_dir must be an absolute path")
        root_path = root_path.resolve()  # Resolve symlinks/..
        
        # Create the final directory if it doesn't exist, but only if parent exists
        if not root_path.exists():
            parent_dir = root_path.parent
            if not parent_dir.exists():
                raise ValueError(f"Cannot create root_dir '{root_path}' because parent directory '{parent_dir}' does not exist")
            try:
                root_path.mkdir()
                logger.info("Created root directory: %s", root_path)
            except (OSError, PermissionError) as e:
                raise ValueError(f"Cannot create root_dir '{root_path}': {e}") from e
        
        resolved_root = str(root_path)

        self.use_xxhash = use_xxhash
        self.slave_dir_extension = slave_dir_extension
        self.metadata_filename = metadata_filename
        _char_replacement_fallback_map = self._default_char_replacements.as_dict()

        manifest_path = CachedFileFoldersManifest.manifest_path_for_root(resolved_root)
        manifest: Optional[CachedFileFoldersManifest] = None
        manifest_loaded_ok = False
        if manifest_path.exists():
            try:
                manifest = CachedFileFoldersManifest.load(str(manifest_path), acquire_lock=False, format_override="json")
                manifest_loaded_ok = True
            except Exception as e:
                logger.warning("Manifest at %s unreadable or invalid: %s", manifest_path, e)
                # If we have explicit parameters, we will rebuild below

        # Compute effective parameters possibly influenced by manifest
        if manifest_loaded_ok and manifest is not None:
            # If caller did not provide a map, adopt persisted manifest map
            effective_char_map = char_replacement_map if char_replacement_map is not None else manifest.char_replacement_map
        else:
            # No manifest or corrupt; use provided or fallback
            effective_char_map = char_replacement_map if char_replacement_map is not None else _char_replacement_fallback_map
        self.char_replacement_map = effective_char_map
        
        # Create storage manager with CategoryFolders internally
        self._storage = CachedFileStorageManager(
            grouping_pattern=grouping_pattern,
            root_dir=resolved_root,
            use_xxhash=use_xxhash,
            slave_dir_extension=slave_dir_extension,
            char_replacement_map=effective_char_map,
            metadata_filename=metadata_filename
        )
        
        # Extract needed properties from storage manager
        self.pattern = self._storage.category_folders.pattern
        self.root_dir = self._storage.category_folders.root_dir
        self._requires_grouping = self._storage._pattern_has_variables()
        
        # Grouping key normalization is now handled by the storage manager

        # After storage is ready, validate or create manifest
        try:
            self._initialize_or_validate_manifest(
                resolved_root,
                grouping_pattern,
                self.use_xxhash,
                self.slave_dir_extension,
                self.char_replacement_map,
                manifest,
                manifest_loaded_ok,
            )
        except Exception:
            # Re-raise to caller; construction should fail on hard errors
            raise
        
        # Clean up any orphaned temporary files from previous sweeps
        try:
            self._storage.cleanup_temp_files()  # Also cleanup materialization temp files
            self._storage.cleanup_retained_artifacts(TEMP_RETENTION_GRACE_SECONDS)
        except Exception as e:
            logger.warning("Failed to cleanup expired files during initialization: %s", e)
        
        

    def close(self):
        if hasattr(self, '_storage'):
            self._storage.close_databases()
    
    # Class-level character replacements loaded from YAML file
    _default_char_replacements = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'cached_file_folders_support', 'character_replacements.yaml'), change_detection_secs=0)

    # Manifest helpers
    def _initialize_or_validate_manifest(
        self,
        resolved_root: str,
        grouping_pattern: str,
        use_xxhash: bool,
        slave_dir_extension: str,
        char_replacement_map: Dict[str, str],
        manifest: Optional[CachedFileFoldersManifest],
        manifest_loaded_ok: bool,
    ) -> None:
        """Create or validate the manifest at the root.

        Behavior:
        - If manifest exists and is readable: validate values; raise on mismatch.
        - If manifest exists but is corrupt/unreadable: rebuild with explicit params (log warning).
        - If manifest is missing: write one; if data exists under root, log adoption warning.
        """
        manifest_path = CachedFileFoldersManifest.manifest_path_for_root(resolved_root)

        if manifest_loaded_ok and manifest is not None:
            # Validate against effective parameters using manifest's method
            mismatches = manifest.validate_against_parameters(
                root_dir=resolved_root,
                grouping_pattern=grouping_pattern,
                use_xxhash=use_xxhash,
                slave_dir_extension=slave_dir_extension,
                char_replacement_map=char_replacement_map,
            )
            
            if mismatches:
                allowable_relaxed_mismatch = set(mismatches.keys()) == {"root_dir"}
                if allowable_relaxed_mismatch:
                    manifest_parent = manifest_path.parent.resolve()
                    current_root = Path(resolved_root).resolve()
                    if manifest_parent == current_root:
                        old_root = mismatches["root_dir"][0]
                        logger.warning(
                            "Adopting cache manifest from previous root '%s'; updating to new root '%s'.",
                            old_root,
                            resolved_root,
                        )
                        manifest.root_dir = resolved_root
                        try:
                            manifest.save(
                                file_path=str(manifest_path),
                                format_override="json",
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to persist updated manifest root at %s: %s",
                                manifest_path,
                                e,
                            )
                        return

                details = "; ".join([f"{k}: existing={v[0]!r} new={v[1]!r}" for k, v in mismatches.items()])
                raise ValueError(
                    "Cache manifest parameter mismatch. "
                    f"{details}. Delete the manifest at '{manifest_path}' or use a different root."
                )
            return

        # If manifest exists but is corrupt/unreadable: rebuild with explicit parameters
        if manifest_path.exists() and not manifest_loaded_ok:
            logger.warning("Rebuilding corrupt/unreadable cache manifest at %s using provided parameters", manifest_path)
        
        # Create new manifest (handles both corrupt and missing cases)
        CachedFileFoldersManifest.create_new(
            root_dir=resolved_root,
            grouping_pattern=grouping_pattern,
            use_xxhash=use_xxhash,
            slave_dir_extension=slave_dir_extension,
            char_replacement_map=char_replacement_map,
            warn_if_data_exists=not manifest_path.exists(),  # Only warn if manifest is truly missing
        )

    @staticmethod
    def from_root(root_dir: Union[str, os.PathLike]) -> "CachedFileFolders":
        """Attach to existing cache using its manifest. Raises ValueError if not absolute or manifest missing/corrupt."""
        root_path = Path(root_dir)
        if not root_path.is_absolute():
            raise ValueError("root_dir must be an absolute path")
        resolved_root = str(root_path.resolve())
        manifest_path = CachedFileFoldersManifest.manifest_path_for_root(resolved_root)
        if not manifest_path.exists():
            raise ValueError(
                f"Manifest not found at '{manifest_path}'. Cannot attach using from_root()."
            )
        try:
            manifest = CachedFileFoldersManifest.load(str(manifest_path), acquire_lock=False, format_override="json")
        except Exception as e:
            raise ValueError(f"Manifest at '{manifest_path}' is unreadable or invalid: {e}")
        if manifest.root_dir != resolved_root:
            manifest_parent = manifest_path.parent.resolve()
            current_root = Path(resolved_root).resolve()
            if manifest_parent == current_root:
                logger.warning(
                    "Adopting cache manifest from previous root '%s'; updating to '%s'.",
                    manifest.root_dir,
                    resolved_root,
                )
                manifest.root_dir = resolved_root
                try:
                    manifest.save(file_path=str(manifest_path), format_override="json")
                except Exception as e:
                    logger.warning(
                        "Failed to persist updated manifest root at %s: %s",
                        manifest_path,
                        e,
                    )
            else:
                raise ValueError(
                    f"Manifest root_dir '{manifest.root_dir}' does not match provided root '{resolved_root}'"
                )
        return CachedFileFolders(
            manifest.grouping_pattern,
            resolved_root,
            use_xxhash=manifest.use_xxhash,
            slave_dir_extension=manifest.slave_dir_extension,
            char_replacement_map=manifest.char_replacement_map,
        )

    def _get_timestamp_file_path(self, grouping_key: Optional[GroupingKey]) -> Path:
        if grouping_key is None:
            return Path(self.root_dir) / ".last_resync_sweep_timestamp"
        
        grouping_dir = self._storage.category_folders.folder(grouping_key, create=False)
        return grouping_dir / ".last_resync_sweep_timestamp"

    def _write_last_sweep_timestamp(self, timestamp: float, grouping_key: Optional[GroupingKey]) -> None:
        try:
            timestamp_file = self._get_timestamp_file_path(grouping_key)
            timestamp_file.parent.mkdir(parents=True, exist_ok=True)
            timestamp_file.write_text(str(timestamp))
        except Exception as e:
            logger.warning("Failed to write sweep timestamp file %s: %s", timestamp_file, e)

    def _read_last_sweep_timestamp(self, grouping_key: Optional[GroupingKey]) -> Optional[float]:
        try:
            timestamp_file = self._get_timestamp_file_path(grouping_key)
            if timestamp_file.exists():
                return float(timestamp_file.read_text().strip())
        except Exception as e:
            logger.warning("Failed to read sweep timestamp file %s: %s", timestamp_file, e)
        return None

    def _get_oldest_sweep_timestamp(self) -> Optional[float]:
        timestamps = []
        
        # Check global timestamp file (for flat patterns)
        global_timestamp = self._read_last_sweep_timestamp(None)
        if global_timestamp is not None:
            timestamps.append(global_timestamp)
        
        # Check all existing grouping directories
        try:
            for grouping_key in self._storage.existing_grouping_keys():
                timestamp = self._read_last_sweep_timestamp(grouping_key)
                if timestamp is not None:
                    timestamps.append(timestamp)
        except Exception as e:
            logger.warning("Error scanning grouping keys for timestamps: %s", e)
        
        return min(timestamps) if timestamps else None

    def get_last_resync_sweep_timestamp(self, grouping_key: Optional[GroupingKey] = None) -> Optional[float]:
        """Get last resync sweep start timestamp. None=oldest across all keys, specific key=that key's timestamp."""
        if grouping_key is None:
            return self._get_oldest_sweep_timestamp()
        else:
            return self._read_last_sweep_timestamp(grouping_key)
    
    def _get_proxy_context_info(self, source_file: FileProxyBase) -> str:
        context_info = source_file.get_context_info()
        proxy_type = type(source_file).__name__
        context_parts = [f"proxy_type={proxy_type}"]
        context_parts.extend(f"{key}={value}" for key, value in context_info.items() if key != "proxy_type")
        return ", ".join(context_parts)

    def _handle_materialization_error(self, source_file: FileProxyBase, ref_path: str, error: Exception) -> None:
        context_info = self._get_proxy_context_info(source_file)
        raise RuntimeError(
            f"Failed to materialize file proxy for {ref_path} "
            f"(context: {context_info}): {error}"
        ) from error

    def _create_temp_file_path(self, target_file_path: Path) -> Path:
        return self._storage.get_temp_directory_root() / f"temp_{uuid.uuid4()}_{target_file_path.name}"

    def _deploy_to_temp_location(self, source_file: FileProxyBase, temp_dir: Path) -> Path:
        """Deploy file to temp directory and return the actual file path created by the proxy.
        
        Args:
            source_file: The file proxy to deploy
            temp_dir: Directory to deploy the file into
            
        Returns:
            Path: The actual file path that was created by the proxy
            
        Raises:
            RuntimeError: If deployment fails or expected file is not found
        """
        # Let the proxy choose the filename and deploy to the temp directory
        source_file.deploy(str(temp_dir))
        
        # Find out what filename the proxy actually used
        expected_filename = source_file.file_name()
        deployed_file = temp_dir / expected_filename
        
        if not deployed_file.exists():
            raise RuntimeError(f"Proxy deployed file but expected {deployed_file} not found")
        
        return deployed_file

    
    def _recover_orphaned_file(self, ref_path: str, grouping_key: Optional[GroupingKey], 
                              file_name: str, target_file_path: Path) -> Optional[CachedFileRef]:
        """
        Check for orphaned files on the filesystem and recover them to the database.
        
        This method implements the fallback mechanism for database consistency recovery.
        When a database lookup fails, this method checks if the file actually exists
        on the filesystem and recovers it by updating the database.
        
        Args:
            ref_path: Reference path for the file
            grouping_key: Grouping key for the file
            file_name: Expected filename
            target_file_path: Expected path to the file on filesystem
            
        Returns:
            CachedFileRef: The recovered file reference if file exists, None otherwise
        """
        # Check if the expected file exists on the filesystem
        if not target_file_path.exists():
            return None
        
        try:
            # Log the recovery operation
            logger.info("Recovered orphaned file from filesystem: %s", ref_path)
            logger.warning("Database inconsistency detected and corrected: %s", ref_path)
            
            # Use storage manager to recover the file to database
            return self._storage.recover_orphaned_file_to_database(ref_path, grouping_key, target_file_path)
            
        except Exception as e:
            logger.error("Failed to recover orphaned file: %s - %s", ref_path, e)
            # Don't re-raise the exception - let the calling code handle the absence gracefully
            # This ensures that recovery failures don't break normal operations
            return None
    

    


    async def upsert_file(
        self,
        source_file: Union[FileProxyBase, os.PathLike, str],
        grouping_key: Optional[GroupingKey] = None,
        force: bool = False,
        change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None,
    ) -> Optional[ChangeNotice]:
        """
        Add or update a file in the cache. Returns ChangeNotice (INSERT/UPDATE) or None if no change.

        The ref_path determines the directory layout beneath the grouping root. If the final segment
        of ref_path resembles a filename (contains a dot), that segment is *not* turned into another
        directory – the actual file name comes from `source_file.file_name()`. The ref_path string is
        still stored verbatim for lookup purposes.

        Old artifacts from UPDATEs are retained only for the duration of a `change_receiver` callback.

        Tip: if the callback needs to reinsert the staged artifact later, see the
        `SavedUpsert` class in `totodev_pub.cached_file_folders_support.saved_upsert`.
        """
        # PASSIVE CLEANUP: Delegate to storage manager global policy
        try:
            self._storage.cleanup_temp_files(TEMP_RETENTION_GRACE_SECONDS)
            self._storage.cleanup_retained_artifacts(TEMP_RETENTION_GRACE_SECONDS)
        except Exception as e:
            logger.warning("Failed to cleanup temporary files during upsert_file: %s", e)
        
        
        grouping_key = self._storage.normalize_grouping_key(grouping_key)
        
        if not isinstance(source_file, FileProxyBase):
            from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy
            source_file = LocalFileProxy(str(source_file), delete_after_deploy=False)
        
        ref_path, file_name = source_file.ref_path(), source_file.file_name()
        target_file_path = self._storage.ref_path_to_filesystem_path(ref_path, file_name, grouping_key)
        target_slave_dir_path = self._storage.get_slave_dir_path(target_file_path)
        existing_file_ref = self.find_file(ref_path, grouping_key)
        
        # Fallback: Check filesystem for orphaned file if database lookup failed
        if existing_file_ref is None:
            existing_file_ref = self._recover_orphaned_file(ref_path, grouping_key, file_name, target_file_path)
        
        notice = await self._upsert_file_core(
            source_file,
            ref_path,
            grouping_key,
            target_file_path,
            target_slave_dir_path,
            existing_file_ref,
            force,
        )
        return self._finalize_change_notice(notice, change_receiver, source_file)
    
    async def _upsert_file_core(self, source_file: FileProxyBase, ref_path: str, grouping_key: Optional[GroupingKey], 
                         target_file_path: Path, target_slave_dir_path: Path, existing_file_ref: Optional[CachedFileRef], 
                         force: bool = False) -> Optional[ChangeNotice]:
        return (await self._insert_file(source_file, ref_path, grouping_key, target_file_path, target_slave_dir_path) 
                if existing_file_ref is None 
                else await self._update_file_optimized(source_file, ref_path, grouping_key, target_file_path, target_slave_dir_path, existing_file_ref, force))
    
    async def _insert_file(self, source_file: FileProxyBase, ref_path: str, grouping_key: Optional[GroupingKey], 
                    target_file_path: Path, target_slave_dir_path: Path) -> ChangeNotice:
        try:
            if not await source_file.materialize(blocking_secs=30.0, temp_dir=self._storage.get_temp_directory_root()):
                raise RuntimeError(f"Failed to materialize file proxy for {ref_path}")
        except Exception as e:
            self._handle_materialization_error(source_file, ref_path, e)
        
        self._storage.insert_file_to_storage(source_file, target_file_path, target_slave_dir_path, grouping_key, ref_path)
        
        notice = ChangeNotice(
            file_name=target_file_path.name,
            cur=CachedFileRef(
                ref_path=ref_path,
                grouping_key=grouping_key,
                file_path=target_file_path,
                slave_dir_path=target_slave_dir_path
            )
        )
        notice._metadata_filename = self.metadata_filename
        return notice
    
    async def _update_file_optimized(self, source_file: FileProxyBase, ref_path: str, grouping_key: Optional[GroupingKey], 
                              target_file_path: Path, target_slave_dir_path: Path, existing_file_ref: CachedFileRef, 
                              force: bool = False) -> Optional[ChangeNotice]:
        # If force=True, skip all change detection and proceed directly to update
        if force:
            return await self._update_file_with_temporary_materialization(
                source_file, ref_path, grouping_key, target_file_path, target_slave_dir_path,
                existing_file_ref, force
            )
        
        if not self._storage.use_xxhash and hasattr(source_file, 'looks_same'):
            # For SharePoint proxies, ensure metadata is available before comparison
            if hasattr(source_file, 'ensure_metadata_available'):
                await source_file.ensure_metadata_available()
            
            if (comparison_result := source_file.looks_same(str(existing_file_ref.file_path))) is True:
                return None
            elif comparison_result is False:
                return await self._update_file_with_temporary_materialization(
                    source_file, ref_path, grouping_key, target_file_path, target_slave_dir_path,
                    existing_file_ref, force
                )
        
        return await self._update_file_with_temporary_materialization(
            source_file, ref_path, grouping_key, target_file_path, target_slave_dir_path,
            existing_file_ref, force
        )
    
    async def _update_file_with_temporary_materialization(self, source_file: FileProxyBase, ref_path: str, 
                                                   grouping_key: Optional[GroupingKey], target_file_path: Path, 
                                                   target_slave_dir_path: Path, existing_file_ref: CachedFileRef, 
                                                   force: bool = False) -> Optional[ChangeNotice]:
        temp_file_path = self._create_temp_file_path(target_file_path)
        
        try:
            if not await source_file.materialize(blocking_secs=30.0, temp_dir=self._storage.get_temp_directory_root()):
                raise RuntimeError(f"Failed to materialize file proxy for {ref_path}")
            
            # Deploy to temp directory and get the actual file path
            temp_dir = temp_file_path.parent
            actual_temp_file = self._deploy_to_temp_location(source_file, temp_dir)
            
            # Skip comparison if force=True, otherwise check if files are identical
            if not force and self._storage.compare_files(actual_temp_file, existing_file_ref.file_path) is True:
                self._storage.delete_path_safely(actual_temp_file)
                return None
            
            return self._complete_update_with_materialized_file(
                actual_temp_file, ref_path, grouping_key, target_file_path, target_slave_dir_path,
                existing_file_ref
            )
            
        except Exception as e:
            # Clean up any temp files that might have been created
            if 'actual_temp_file' in locals():
                self._storage.delete_path_safely(actual_temp_file)
            else:
                self._storage.delete_path_safely(temp_file_path)
            self._handle_materialization_error(source_file, ref_path, e)
    
    def _complete_update_with_materialized_file(self, materialized_file_path: Path, ref_path: str, 
                                               grouping_key: Optional[GroupingKey], target_file_path: Path, 
                                               target_slave_dir_path: Path, existing_file_ref: CachedFileRef) -> ChangeNotice:
        old_file_path, old_slave_dir_path = existing_file_ref.file_path, existing_file_ref.slave_dir_path
        metadata = {"change": "UPDATE", "ref_path": ref_path, "grouping_key": list(grouping_key) if grouping_key else None}

        temp_old_file = self._storage.move_to_retained(old_file_path, "old_file", metadata=metadata)
        temp_old_slave_dir = self._storage.move_to_retained(old_slave_dir_path, "old_slave", metadata=metadata)
        
        target_file_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(materialized_file_path), str(target_file_path))
        self._storage.create_slave_directory(target_file_path)
        
        db = self._storage.get_database(grouping_key)
        stored_path = self._storage._serialize_path_for_storage(target_file_path, grouping_key)
        self._storage._db_operation_with_retry(lambda: db.__setitem__(ref_path, stored_path))
        
        notice = ChangeNotice(
            file_name=target_file_path.name,
            cur=CachedFileRef(
                ref_path=ref_path,
                grouping_key=grouping_key,
                file_path=target_file_path,
                slave_dir_path=target_slave_dir_path
            ),
            old=CachedFileRef(
                ref_path=ref_path,
                grouping_key=grouping_key,
                file_path=temp_old_file,
                slave_dir_path=temp_old_slave_dir
            )
        )
        notice._metadata_filename = self.metadata_filename
        return notice
    

    async def delete_file(
        self,
        ref_path: str,
        grouping_key: Optional[GroupingKey] = None,
        change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None,
    ) -> Optional[ChangeNotice]:
        """Delete file and slave directory. Returns ChangeNotice or None if not found.
        
        Old files are retained only while any change_receiver callback executes."""
        # TIP: use SavedUpsert.from_change_notice inside change receivers when you need to
        # keep the staged file for a later upsert rather than allowing automatic cleanup.
        # PASSIVE CLEANUP: Delegate to storage manager global policy
        try:
            self._storage.cleanup_temp_files(TEMP_RETENTION_GRACE_SECONDS)
            self._storage.cleanup_retained_artifacts(TEMP_RETENTION_GRACE_SECONDS)
        except Exception as e:
            logger.warning("Failed to cleanup temporary files during delete_file: %s", e)
        
        grouping_key = self._storage.normalize_grouping_key(grouping_key)
        
        if (existing_file_ref := self.find_file(ref_path, grouping_key)) is None:
            return None
        
        notice = await self._delete_file_core(ref_path, grouping_key, existing_file_ref)
        return self._finalize_change_notice(notice, change_receiver, None)
    
    async def _delete_file_core(self, ref_path: str, grouping_key: Optional[GroupingKey], 
                         existing_file_ref: CachedFileRef) -> ChangeNotice:
        old_file_path, old_slave_dir_path = existing_file_ref.file_path, existing_file_ref.slave_dir_path
        metadata = {"change": "DELETE", "ref_path": ref_path, "grouping_key": list(grouping_key) if grouping_key else None}
        
        temp_old_file = self._storage.move_to_retained(old_file_path, "deleted_file", metadata=metadata)
        temp_old_slave_dir = self._storage.move_to_retained(old_slave_dir_path, "deleted_slave", metadata=metadata)
        
        # Clean up empty directories left behind after file deletion
        self._storage.cleanup_empty_directories(old_file_path, grouping_key)
        
        db = self._storage.get_database(grouping_key)
        if ref_path in db:
            self._storage._db_operation_with_retry(lambda: db.__delitem__(ref_path))
        
        notice = ChangeNotice(
            file_name=existing_file_ref.file_path.name,
            old=CachedFileRef(
                ref_path=ref_path,
                grouping_key=grouping_key,
                file_path=temp_old_file,
                slave_dir_path=temp_old_slave_dir
            )
        )
        notice._metadata_filename = self.metadata_filename
        return notice

    def _finalize_change_notice(
        self,
        notice: Optional[ChangeNotice],
        change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]],
        source_file: Optional[FileProxyBase],
    ) -> Optional[ChangeNotice]:
        """
        Deliver the change notice to any registered receiver and immediately
        dispose of staged artifacts associated with the change.
        """
        if notice is None:
            return None

        staged_paths: List[Path] = []
        if notice.old is not None:
            if notice.old.file_path is not None:
                staged_paths.append(notice.old.file_path)
            if notice.old.slave_dir_path is not None:
                staged_paths.append(notice.old.slave_dir_path)

        try:
            if change_receiver is not None:
                change_receiver(notice, source_file)
            return notice
        finally:
            for path in staged_paths:
                self._storage.delete_path_safely(path)
            try:
                self._storage.cleanup_retained_artifacts(TEMP_RETENTION_GRACE_SECONDS)
            except Exception as e:
                logger.warning("Failed to cleanup retained artifacts: %s", e)
            try:
                self._storage.cleanup_temp_files(TEMP_RETENTION_GRACE_SECONDS)
            except Exception as e:
                logger.warning("Failed to cleanup temp materialization files: %s", e)


    def move_file(
        self,
        old_ref_path: str,
        new_ref_path: str,
        grouping_key: Optional[GroupingKey] = None,
        new_grouping_key: Optional[GroupingKey] = None,
        overwrite: bool = False,
    ) -> "CachedFileRef":
        """
        Move a cached file (and its slave directory) to a new logical ref_path and/or grouping.
        
        This updates both the filesystem location and the cache database entry. It does NOT
        emit any ChangeNotice and is intended as a low-level operation.
        
        Note on ref_path vs filename:
            The new_ref_path may end with a segment that looks like a filename (contains a dot).
            This segment is used only to determine directory layout. The actual filename on disk
            remains the cached file's existing filename; ref_path does not rename files.
        
        Example:
            Current file stored as ".../dir/a/b/file.pdf"
            move_file("x/a/b/file.pdf", "x/a/c/fileX.pdf")
            Result: file ends up at ".../dir/a/c/file.pdf" (filename unchanged)
                    and the database key is "x/a/c/fileX.pdf".
        
        Args:
            old_ref_path: Existing ref_path for the file to move.
            new_ref_path: New ref_path to assign.
            grouping_key: Current grouping key (None for flat patterns).
            new_grouping_key: Destination grouping key (defaults to grouping_key).
            overwrite: If False and destination exists, raise ValueError. If True, replace destination.
        
        Returns:
            CachedFileRef for the new location.
        
        Raises:
            ValueError if the source does not exist, destination exists and overwrite=False,
            or if grouping_key usage violates the pattern (flat vs grouped).
        """
        # Normalize grouping keys and validate existence
        src_grouping = self._storage.normalize_grouping_key(grouping_key)
        dst_grouping = self._storage.normalize_grouping_key(
            src_grouping if new_grouping_key is None else new_grouping_key
        )
        
        existing = self.find_file(old_ref_path, src_grouping)
        if existing is None:
            raise ValueError(
                f"Source not found in cache: ref_path={old_ref_path!r}, grouping_key={src_grouping!r}"
            )
        
        # Compute destination paths; preserve actual filename
        file_name = existing.file_path.name
        dst_file_path = self._storage.ref_path_to_filesystem_path(new_ref_path, file_name, dst_grouping)
        dst_slave_dir = self._storage.get_slave_dir_path(dst_file_path)
        
        # Handle destination existence: check DB and filesystem
        dst_db = self._storage.get_database(dst_grouping)
        dst_exists_in_db = new_ref_path in dst_db
        dst_physical_exists = dst_file_path.exists() or dst_slave_dir.exists()
        if (dst_exists_in_db or dst_physical_exists) and not overwrite:
            raise ValueError(
                f"Destination already exists for ref_path={new_ref_path!r}, grouping_key={dst_grouping!r}"
            )
        
        # If overwriting, remove existing destination entry and files safely
        if (dst_exists_in_db or dst_physical_exists) and overwrite:
            try:
                if dst_exists_in_db:
                    self._storage._db_operation_with_retry(lambda: dst_db.__delitem__(new_ref_path))
            except KeyError:
                pass
            # Remove physical destination paths if present
            if dst_file_path.exists():
                self._storage.delete_path_safely(dst_file_path)
            if dst_slave_dir.exists():
                self._storage.delete_path_safely(dst_slave_dir)
        
        # Ensure destination parent directories exist
        dst_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Move file
        shutil.move(str(existing.file_path), str(dst_file_path))
        
        # Move slave directory (rename if exists)
        if existing.slave_dir_path.exists():
            dst_slave_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(existing.slave_dir_path), str(dst_slave_dir))
        else:
            # Ensure a slave dir exists at destination
            self._storage.create_slave_directory(dst_file_path)
        
        # Update databases: remove old entry, add new entry
        src_db = self._storage.get_database(src_grouping)
        if old_ref_path in src_db:
            self._storage._db_operation_with_retry(lambda: src_db.__delitem__(old_ref_path))
        
        stored_path = self._storage._serialize_path_for_storage(dst_file_path, dst_grouping)
        self._storage._db_operation_with_retry(lambda: dst_db.__setitem__(new_ref_path, stored_path))
        
        # Cleanup any empty directories left behind at source
        self._storage.cleanup_empty_directories(existing.file_path, src_grouping)
        
        # Return new reference
        new_ref = CachedFileRef(
            ref_path=new_ref_path,
            grouping_key=dst_grouping,
            file_path=dst_file_path,
            slave_dir_path=dst_slave_dir,
        )
        new_ref._metadata_filename = self.metadata_filename
        return new_ref

    def grouping(self, grouping_key: Optional[GroupingKey] = None) -> CacheGrouping:
        """Create CacheGrouping facet for working with specific grouping without repeatedly passing grouping_key."""
        return CacheGrouping(self, grouping_key)

    def groupings(self, reverse: bool = False, 
                 filters: Optional[Union[Dict[str, str], List[str]]] = None) -> Iterator[CacheGrouping]:
        """Yield CacheGrouping facets for all grouping keys present in the cache.
        
        Returns CacheGrouping objects (not raw tuples). Access .grouping_key property for raw tuple.
        Raises ValueError for flat patterns."""
        if not self._requires_grouping:
            raise ValueError(f"groupings() is not applicable for flat patterns. "
                           f"Pattern '{self.pattern}' has no variables and represents a single repository. "
                           f"Use grouping() to get a facet for the single repository, or files() to iterate files.")
        
        # Use storage manager's existing_grouping_keys method for filtering
        for grouping_key in self._storage.existing_grouping_keys(filters=filters, reverse=reverse):
            yield CacheGrouping(self, grouping_key)


    def files(self, grouping_key: Optional[Union[Sequence[str], Dict[str, str], List[str]]] = None, 
              reverse: bool = False,
              ref_path_glob: Optional[str] = None) -> Iterator[CachedFileRef]:
        """Yield all files with optional filtering by grouping_key (exact/pattern) and ref_path_glob.
        
        grouping_key can be:
        - None: Only allowed for flat patterns (no variables in pattern). For grouped patterns,
                use a specific grouping_key, dict/list filters, or call files() on a CacheGrouping instance.
        - Specific tuple: Exact grouping key match
        - Dict/List: Pattern filters for grouping key components
        
        See CachedFileRef for returned fields."""
        # Validate that None grouping_key is only allowed for flat patterns
        if grouping_key is None and self._requires_grouping:
            raise ValueError(f"grouping_key is required for grouped patterns. "
                           f"Pattern '{self.pattern}' contains variables and requires grouping keys. "
                           f"Use a specific grouping_key, dict/list filters, or call files() on a CacheGrouping instance.")
        
        grouping_key_filters = None
        specific_grouping_key = None
        
        if grouping_key is not None:
            if isinstance(grouping_key, (dict, list)):
                grouping_key_filters = grouping_key
                if not self._requires_grouping:
                    raise ValueError(f"grouping_key filters are not applicable for flat patterns. "
                                   f"Pattern '{self.pattern}' has no variables and represents a single repository. "
                                   f"Use ref_path_glob for filtering files in flat patterns.")
            else:
                specific_grouping_key = grouping_key
        
        # If ref_path_glob is provided but we have a specific grouping_key, convert it to a filter
        # to ensure we only search within that specific grouping
        if ref_path_glob is not None and specific_grouping_key is not None:
            # Convert specific grouping_key to exact-match filters
            if self._requires_grouping:
                pattern_parts = self.pattern.split('/')
                grouping_key_filters = {}
                grouping_key_tuple = tuple(specific_grouping_key) if not isinstance(specific_grouping_key, tuple) else specific_grouping_key
                grouping_key_idx = 0  # Track position in grouping_key_tuple separately
                for part in pattern_parts:
                    if part.startswith('{') and part.endswith('}'):
                        var_name = part[1:-1]
                        if grouping_key_idx < len(grouping_key_tuple):
                            grouping_key_filters[var_name] = grouping_key_tuple[grouping_key_idx]
                            grouping_key_idx += 1
                specific_grouping_key = None  # Clear so we use the filter path
        
        if grouping_key_filters is not None or (ref_path_glob is not None and specific_grouping_key is None):
            yield from self._storage.existing_cached_files(
                grouping_key_filters=grouping_key_filters,
                ref_path_glob=ref_path_glob,
                reverse=reverse
            )
            return
        
        specific_grouping_key = self._storage.normalize_grouping_key(specific_grouping_key)

        if not self._storage._grouping_exists(specific_grouping_key):
            return

        db = self._storage.get_database(specific_grouping_key)
        
        ref_paths = sorted(db.keys(), reverse=reverse)
        
        for ref_path in ref_paths:
            # Apply reference path glob filter if specified
            if ref_path_glob:
                # Normalize path separators in both the ref_path and glob pattern
                # to handle cross-platform compatibility
                normalized_ref_path = self._storage._normalize_path_separators(ref_path)
                normalized_glob = self._storage._normalize_path_separators(ref_path_glob)
                
                if not fnmatch.fnmatch(normalized_ref_path, normalized_glob):
                    continue
            
            stored_path = db[ref_path]
            file_path = self._storage._deserialize_stored_path(stored_path, specific_grouping_key)
            
            if file_path.exists():
                file_ref = CachedFileRef(
                    ref_path=ref_path,
                    grouping_key=specific_grouping_key,
                    file_path=file_path,
                    slave_dir_path=self._storage.get_slave_dir_path(file_path)
                )
                file_ref._metadata_filename = self.metadata_filename
                yield file_ref
            else:
                self._storage._db_operation_with_retry(lambda rp=ref_path: db.__delitem__(rp))

    def files_count(self, grouping_key: Optional[Union[Sequence[str], Dict[str, str], List[str]]] = None) -> int:
        """Count files matching grouping_key filter."""
        if grouping_key is None:
            # Count across all grouping keys
            total = 0
            for grouping in self.groupings():
                db = self._storage.get_database(grouping.grouping_key)
                total += len(db)
            return total
        else:
            # Specific grouping key
            normalized_key = self._storage.normalize_grouping_key(grouping_key)
            if not self._storage._grouping_exists(normalized_key):
                return 0
            db = self._storage.get_database(normalized_key)
            return len(db)

    def file_exists(self, ref_path: str, grouping_key: Optional[GroupingKey] = None) -> bool:
        return self.find_file(ref_path, grouping_key) is not None

    def find_file(self, ref_path: str, grouping_key: Optional[GroupingKey] = None) -> Optional[CachedFileRef]:
        """Find file by ref_path and grouping_key. Returns CachedFileRef or None."""
        grouping_key = self._storage.normalize_grouping_key(grouping_key)
        db = self._storage.get_database(grouping_key)
        
        if ref_path not in db:
            return None
            
        stored_path = db[ref_path]
        file_path = self._storage._deserialize_stored_path(stored_path, grouping_key)
        
        if not file_path.exists():
            self._storage._db_operation_with_retry(lambda: db.__delitem__(ref_path))
            return None
            
        file_ref = CachedFileRef(
            ref_path=ref_path,
            grouping_key=grouping_key,
            file_path=file_path,
            slave_dir_path=self._storage.get_slave_dir_path(file_path)
        )
        file_ref._metadata_filename = self.metadata_filename
        return file_ref

    def get_slave_dir(self, grouping_key: Optional[GroupingKey], 
                      ref_path: Optional[str] = None) -> Path:
        """
        Get slave directory for a file or grouping key.
        
        Slave directories are user-controlled spaces where applications can store
        arbitrary data. The cache system guarantees these directories will not be 
        modified by cache operations.  Each file gets a slave directory.  Each
        grouping gets a slave directory.
        
        Args:
            grouping_key: The grouping key (required for grouped patterns, None for flat)
            ref_path: The ref_path of a specific file, or None for grouping-level slave dir
            
        Returns:
            Path to slave directory (created lazily):
            - If ref_path is None: per-grouping-key slave directory (_grouping._slave)
            - If ref_path is provided: per-file slave directory (file must exist)
            
        Raises:
            ValueError: If ref_path is provided but file doesn't exist in cache
            
        Lifecycle:
            - Created empty when the file is added to the cache.
            - Per-file slave dirs: deleted when file is deleted
            - Per-grouping slave dirs: persistent until grouping is explicitly removed
            
        Use Cases:
            - Per-file: metadata, processing logs, derived data, thumbnails
            - Per-grouping: sync state, processing queues, statistics, application databases
            
        Examples:
            # Get grouping-level slave directory for application state
            app_dir = cache.get_slave_dir(["inbox"], ref_path=None)
            (app_dir / "sync_state.yaml").write_text('last_sync: 2024-01-01')
            
            # Get per-file slave directory for processing logs
            file_dir = cache.get_slave_dir(["inbox"], "emails/msg.eml")
            (file_dir / "metadata.yaml").write_text('processed: true')
            
            # With CacheGrouping facet for cleaner API
            grouping = cache.grouping(["inbox"])
            app_dir = grouping.get_slave_dir()  # Grouping-level
            file_dir = grouping.get_slave_dir("emails/msg.eml")  # Per-file
        """
        if ref_path is None:
            # Grouping-level slave directory
            base_path = self._storage._get_grouping_base_path(grouping_key)
            slave_dir = base_path / f"_grouping{self.slave_dir_extension}"
            slave_dir.mkdir(parents=True, exist_ok=True)
            return slave_dir
        else:
            # Per-file slave directory (existing logic)
            file_ref = self.find_file(ref_path, grouping_key)
            if file_ref is None:
                raise ValueError(
                    f"File not found in cache: ref_path={ref_path!r}, "
                    f"grouping_key={grouping_key!r}. Cannot get slave directory for non-existent file."
                )
            return file_ref.slave_dir_path

    def get_temp_directory_root(self, selector: Optional[str] = None) -> Path:
        """
        Expose the cache-managed temporary directory used for materialization.

        Callers may use this directory for short-lived artifacts (for example when
        capturing staged files inside a change receiver). The cache performs regular
        cleanup, so any files placed here should be considered transient. Optional
        `selector` is reserved for future subdivision and currently ignored.
        """
        return self._storage.get_temp_directory_root(selector)

    def get_cached_mtime(
        self,
        ref_path: str,
        grouping_key: Optional[GroupingKey] = None,
        includes: Literal["target_only", "slave_files_only", "both"] = "both",
    ) -> Optional[float]:
        """Return newest mtime (Unix timestamp) for cached file/slave files. Raises ValueError if not found."""
        # Validate and locate cached file
        file_ref = self.find_file(ref_path, grouping_key)
        if file_ref is None:
            raise ValueError(
                f"Cached entry not found for ref_path='{ref_path}' with grouping_key={list(grouping_key) if grouping_key else None}"
            )

        newest_times: List[float] = []

        # Consider target file mtime
        if includes in ("target_only", "both"):
            try:
                if file_ref.file_path.exists():
                    newest_times.append(file_ref.file_path.stat().st_mtime)
            except (FileNotFoundError, PermissionError):
                pass

        # Consider slave directory files mtime (regular files only)
        if includes in ("slave_files_only", "both"):
            try:
                slave_dir = file_ref.slave_dir_path
                if slave_dir.exists():
                    for p in slave_dir.rglob("*"):
                        try:
                            if p.is_file():
                                newest_times.append(p.stat().st_mtime)
                        except (FileNotFoundError, PermissionError):
                            continue
            except (FileNotFoundError, PermissionError):
                pass

        if not newest_times:
            return None
        return max(newest_times)

    def purge(self, grouping_key: Optional[GroupingKey] = None, dry_run: bool = False) -> List[Path]:
        """Purge cached files.
        
        WARNING: This operation is unrecoverable and should be used carefully.
        All cached files, slave directories, and databases will be permanently deleted.
        
        Args:
            grouping_key: Specific grouping to purge. If None, purges entire cache.
                         For flat patterns, must be None (raises ValueError otherwise).
            dry_run: If True, return what would be deleted without deleting
            
        Returns:
            List of deleted (or would-be-deleted) paths
            
        Raises:
            ValueError: If grouping_key provided for flat pattern
        """
        if grouping_key is not None and not self._requires_grouping:
            raise ValueError(
                f"grouping_key cannot be specified for flat patterns. "
                f"Pattern '{self.pattern}' has no variables."
            )
        
        return self._storage.purge(grouping_key, dry_run)

    def portage(self, 
               grouping_key: Optional[GroupingKey] = None,
               include_metadata: bool = False,
               force_rebuild: bool = False) -> Union[Path, List[Path]]:
        """Create JSONL portage file(s) mirroring SQLite database(s).
        
        force_rebuild=True deletes SQLite after export (auto-rebuilds from portage on next access).
        Returns single Path or List[Path] depending on grouping_key (None=all groups)."""
        # Collect grouping keys to process
        grouping_keys_to_process = []
        
        if grouping_key is not None:
            grouping_key = self._storage.normalize_grouping_key(grouping_key)
            grouping_keys_to_process.append(grouping_key)
        else:
            # Process all grouping keys
            if self._requires_grouping:
                # Grouped pattern - iterate all existing groups
                grouping_keys_to_process.extend(self._storage.existing_grouping_keys())
            else:
                # Flat pattern - single global database
                grouping_keys_to_process.append(None)
        
        # Generate portage files
        portage_paths = []
        for gk in grouping_keys_to_process:
            portage_path = self._storage._portage_database(gk, include_metadata)
            portage_paths.append(portage_path)
        
        # Delete SQLite databases if force_rebuild is True
        if force_rebuild:
            for gk in grouping_keys_to_process:
                try:
                    # Close the database connection first
                    db_key = tuple(gk) if gk else ()
                    if db_key in self._storage._sqlite_databases:
                        self._storage._sqlite_databases[db_key].close()
                        del self._storage._sqlite_databases[db_key]
                    
                    # Delete the SQLite database file
                    sqlite_path = self._storage._get_sqlite_path(gk)
                    if sqlite_path.exists():
                        sqlite_path.unlink()
                        logger.info("Deleted SQLite database for force_rebuild: %s", sqlite_path)
                    
                    # Also delete WAL and SHM files if they exist (SQLite Write-Ahead Log)
                    wal_path = sqlite_path.with_suffix('.sqlite-wal')
                    if wal_path.exists():
                        wal_path.unlink()
                    shm_path = sqlite_path.with_suffix('.sqlite-shm')
                    if shm_path.exists():
                        shm_path.unlink()
                        
                except Exception as e:
                    logger.warning("Failed to delete SQLite database during force_rebuild for %s: %s", gk, e)
        
        # Return single path or list based on input
        if grouping_key is not None:
            return portage_paths[0]
        return portage_paths

    def get_version_control_patterns(self) -> Dict[str, List[str]]:
        """Return dict with 'ignore' and 'version' patterns for VCS integration (e.g., .gitignore)."""
        return self._storage.get_version_control_patterns()

    @asynccontextmanager
    async def resync_sweep(self, grouping_key: Optional[GroupingKey] = None, 
                                auto_delete: bool = True,
                                upsert_fail_policy: str = "RETAIN_OLD",
                                throttle_queue_limits: Optional[Dict[str, int]] = None,
                                change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None):
        """Async context manager for batch resynchronization with concurrent processing and mark-and-sweep.
        
        Yields ResyncOrchestrator session for concurrent file operations. Performs mark-and-sweep cleanup 
        on exit if auto_delete=True. Uses optimistic concurrency (mtime verification) instead of file locking.
        
        upsert_fail_policy: "RETAIN_OLD" (default), "DELETE_OLD", or "FAIL_FAST"
        throttle_queue_limits: Optional dict mapping queue names to concurrency limits
        change_receiver: Optional callback(notice, proxy) for change notifications
        """
        from .cached_file_folders_support.resync_orchestrator import ResyncOrchestrator
        
        orchestrator = ResyncOrchestrator(
            cache=self,
            grouping_key=grouping_key,
            auto_delete=auto_delete,
            upsert_fail_policy=upsert_fail_policy,
            max_concurrency=5,
            throttle_queue_limits=throttle_queue_limits,
            change_receiver=change_receiver,
            record_sweep_timestamp=True
        )
        
        async with orchestrator as session:
            yield session
    async def resync_bulk(self, 
                         file_proxies: Union[Iterator[FileProxyBase], List[FileProxyBase], Tuple[FileProxyBase, ...]], 
                         grouping_key: Optional[GroupingKey] = None,
                         auto_delete: bool = True,
                         upsert_fail_policy: str = "RETAIN_OLD",
                         retry_count: int = 1,
                         max_concurrent_requests: int = 5,
                         change_receiver: Optional[Callable[[ChangeNotice, Optional[FileProxyBase]], None]] = None) -> ResyncBulkResult:
        """Convenience method for bulk synchronization with retry support and intelligent nested proxy handling.
        
        Automatically expands nested proxies (via nested_proxies() method) for files that are
        new or changed, while skipping nested proxy fetching for already-cached files.
        For example, in the case where the proxy represents an email and the nested proxies are attachments,
        if the email is not detected as "changed", the attachments will never be iterated.
        
        **LIMITATION**: Clears the `old` CachedFileRef from returned notices. Use resync_sweep() if you need old file access.
        Retries failed files up to retry_count times. Total attempts = retry_count + 1.
        """
        from .cached_file_folders_support.resync_orchestrator import ResyncOrchestrator
        
        # Validate grouping_key requirement
        if self._requires_grouping and grouping_key is None:
            raise ValueError(f"grouping_key is required for grouped patterns. "
                           f"Pattern '{self.pattern}' requires grouping keys.")
        elif not self._requires_grouping and grouping_key is not None:
            raise ValueError(f"grouping_key is not allowed for flat patterns. "
                           f"Pattern '{self.pattern}' represents a single repository.")
        
        orchestrator = ResyncOrchestrator(
            cache=self,
            grouping_key=grouping_key,
            auto_delete=auto_delete,
            upsert_fail_policy=upsert_fail_policy,
            max_concurrency=max_concurrent_requests,
            change_receiver=change_receiver,
            record_sweep_timestamp=True,
            retry_count=retry_count,
            expand_nested=True
        )
        
        async with orchestrator as session:
            result = await session.bulk_sync(file_proxies)
        
        # Maintain portage behavior
        if CachedFileFolders._portageAfterAllResync:
            try:
                self.portage(grouping_key, include_metadata=True)
            except Exception as e:
                logger.warning(f"Failed to generate portage file after resync_bulk: {e}")
        
        return result


