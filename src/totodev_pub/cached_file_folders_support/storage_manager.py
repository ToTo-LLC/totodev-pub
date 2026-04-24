# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CachedFileStorageManager: Low-level storage operations for CachedFileFolders.

⚠️  IMPORTANT: This class is a subordinate component of CachedFileFolders and is NOT
    intended to be used independently. It provides the underlying storage infrastructure
    for the higher-level CachedFileFolders API.

## Overview

The CachedFileStorageManager implements a sophisticated file caching system with two
primary storage states:

1. **Active Cache**: Files are stored in organized directory structures with:
   - Configurable grouping patterns and directory organization
   - "Slave directories" for metadata (with configurable extensions)
   - SQLite databases for tracking file references and locations
   - Support for different file comparison methods (mtime vs xxhash)

2. **Staging Area for Deletions/Updates**: When files are removed or replaced they are
   briefly moved to a predictable location so higher-level APIs can surface them to
   change handlers.
   - Uses prefixes like `old_file_`, `deleted_file_`, `old_slave_`, `deleted_slave_`
   - Includes timestamps and UUIDs for uniqueness and tracking
   - Optional metadata sidecar files (`.meta.yaml`) for additional context
   - By default `CachedFileFolders` deletes staged artifacts immediately after the
     change_receiver (if any) returns; longer retention is only achieved by calling the
     cleanup APIs directly or overriding the policy in custom flows.

## Key Features

### File Lifecycle Management
- **Insert**: Deploy files to cache, create slave directories, update databases
- **Compare**: Check file equality using configured method (mtime or xxhash)
- **Move to Retained**: Safely move files to temp locations before deletion
- **Cleanup**: Remove expired files from retained locations

### Staging & Recovery
- Short-lived staging of replaced/deleted files so callers can inspect them inside
  change callbacks
- Optional retention knobs for custom workflows that need longer access windows
- Best-effort cleanup operations that gracefully handle failures
- Recovery capability: staged files can be reattached when inconsistencies are detected
- Auditing support: metadata preserved about deletions and timing

### Path Resolution & Organization
- Converts reference paths to filesystem paths with safe character replacement
- Supports protocol stripping (http://, file://, etc.)
- Handles grouping patterns and directory structure creation
- Manages slave directory creation and cleanup

### Database Management
- SQLite databases for tracking file references
- Per-group database isolation
- Automatic database cleanup and resource management

## Usage

This class should only be instantiated and used by CachedFileFolders instances.
Direct usage is not recommended as it bypasses the higher-level API and error handling.

## Configuration

The manager uses a YAML configuration file (`.cached_storage.yaml`) for:
- Staging retention policies and timeouts
- Temporary file naming prefixes
- Grouping patterns and settings
- Per-group retention overrides

## Concurrency Considerations

**Thread Safety**: This class provides **improved concurrency support** through SQLite with Write-Ahead Logging (WAL) mode. While not fully thread-safe for all scenarios, it handles concurrent access much better than the previous DBM/shelve implementation.

**Concurrent Reads (Safe)**:
- Multiple threads can safely perform read-only operations simultaneously
- File comparison operations (`compare_files()`) can run concurrently
- Path resolution and query operations (`existing_cached_files()`, `existing_grouping_keys()`) are safe for concurrent access
- Reading from SQLite databases is safe even during write operations (thanks to WAL mode)

**Concurrent Writes (Improved but use with caution)**:
- SQLite with WAL mode supports multiple concurrent readers and a single writer
- Database write operations include retry logic (5 attempts with 0.1s delay) to handle transient locking conflicts
- File system write operations (create, move, delete) are still not atomic and could result in race conditions
- Concurrent writes to the same grouping key are possible but may experience brief delays due to retries

**Best Practices**:
- Use the higher-level `CachedFileFolders` API with its `resync_sweep()` context manager for concurrent operations
- The `resync_sweep()` provides file-based locking for multi-process safety
- For high-concurrency scenarios, consider using separate grouping keys to distribute load
- Database operations automatically retry on transient locking conflicts

**Migration Note**: Legacy DBM/shelve caches are no longer migrated automatically. We accelerated this removal ahead of the original schedule—sorry for the disruption. Please export via the portage mechanism before upgrading.

## Future Recoverability Enhancement

The SQLite databases serve as a performance optimization for tracking file references, but the underlying file system structure contains sufficient information to reconstruct them if corruption occurs. Future improvements could implement automatic database reconstruction by: (1) scanning the directory tree to identify all cached files and their slave directories, (2) extracting reference paths from file metadata or naming conventions, (3) rebuilding the SQLite database mappings from this discovered information, and (4) optionally validating file integrity during reconstruction. This would make the system more resilient to database corruption while maintaining the performance benefits of the SQLite optimization.
"""

from pathlib import Path
from typing import Dict, Optional, Sequence, Any, Union, List, Iterator
import time
import uuid
import yaml
import hashlib
import shutil
import tempfile
import os
import fnmatch

from sqlitedict import SqliteDict

from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from ._internal_category_folders import InternalCategoryFolders


class CachedFileStorageManager:
    def __init__(self, grouping_pattern: str, root_dir: str,
                 use_xxhash: bool,
                 slave_dir_extension: str,
                 char_replacement_map: Dict[str, str],
                 metadata_filename: str = "metadata.yaml"):
        # Internalized CategoryFolders implementation (private to this library)
        self.category_folders = InternalCategoryFolders(grouping_pattern, root_dir)
        self.use_xxhash = use_xxhash
        self.slave_dir_extension = slave_dir_extension
        self.char_replacement_map = char_replacement_map
        self.metadata_filename = metadata_filename
        self._sqlite_databases = {}
        self.CONFIG_FILENAME = ".cached_storage.yaml"
        self._config: Dict[str, Any] = {}
        self._load_config_if_present()

    # -----------------------------
    # YAML config management
    # -----------------------------
    @property
    def config_path(self) -> Path:
        return self.category_folders.root_dir / self.CONFIG_FILENAME

    def _default_config(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "grouping_pattern": getattr(self.category_folders, "pattern", None),
            "temp_prefixes": {
                "old_file": "old_file_",
                "old_slave": "old_slave_",
                "deleted_file": "deleted_file_",
                "deleted_slave": "deleted_slave_"
            }
        }

    def _load_config_if_present(self) -> None:
        try:
            path = self.config_path
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    self._config = data
                else:
                    self._config = self._default_config()
            else:
                # Don't write by default; keep in-memory defaults until user updates
                self._config = self._default_config()
        except Exception:
            # Fall back to defaults on any error
            self._config = self._default_config()

    def save_config(self) -> None:
        try:
            self.category_folders.root_dir.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._config, f, sort_keys=False)
        except Exception:
            # Best-effort; caller can decide to handle errors
            pass

    # -----------------------------
    # Retained temp object helpers
    # -----------------------------
    def _temp_name(self, kind: str, suffix: str = "") -> str:
        prefixes = self._config.get("temp_prefixes", {})
        prefix = prefixes.get(kind, f"{kind}_")
        return f"{prefix}{int(time.time())}_{uuid.uuid4().hex}{suffix}"

    def move_to_retained(self, path: Path, kind: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[Path]:
        """
        Move a file or directory to a temp retained location with a predictable name.
        Returns new Path or None if original didn't exist.
        """
        if path is None:
            return None
        if not path.exists():
            return None
        tmp_dir = Path(Path.cwd()).resolve().anchor  # fallback to system temp via tempfile later
        try:
            tmp_dir = Path(tempfile.gettempdir())
        except Exception:
            pass
        suffix = path.suffix if path.is_file() else (self.slave_dir_extension if path.is_dir() else "")
        dest = tmp_dir / self._temp_name(kind, suffix)
        try:
            shutil.move(str(path), str(dest))
            # Optional sidecar metadata
            if metadata:
                sidecar = dest.parent / f"{dest.name}.meta.yaml"
                try:
                    with open(sidecar, "w", encoding="utf-8") as f:
                        yaml.safe_dump(metadata, f, sort_keys=False)
                except Exception:
                    pass
            return dest
        except Exception:
            return None

    def delete_path_safely(self, path: Optional[Path]) -> bool:
        if not path:
            return False
        try:
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            return True
        except Exception:
            return False

    def delete_pair_safely(self, file_path: Optional[Path], slave_dir_path: Optional[Path]) -> tuple[bool, bool]:
        return self.delete_path_safely(file_path), self.delete_path_safely(slave_dir_path)

    def get_version_control_patterns(self) -> Dict[str, List[str]]:
        """Get file patterns for version control integration.
        
        Returns patterns for files to ignore vs version in git or other VCS.
        
        Returns:
            Dictionary with 'ignore' and 'version' keys containing lists of patterns.
            - 'ignore': Patterns for files that should not be version controlled
              (binary databases, lock files, temporary files)
            - 'version': Patterns for files that should be version controlled
              (portage files, configuration, slave directories)
        
        Example:
            >>> storage = CachedFileStorageManager(...)
            >>> patterns = storage.get_version_control_patterns()
            >>> print(patterns['ignore'])
            ['*.sqlite', '*.sqlite-shm', '*.sqlite-wal', '*.lock', ...]
            >>> print(patterns['version'])
            ['*.portage.jsonl', '.cached_storage.yaml', ...]
        """
        return {
            'ignore': [
                '*.sqlite',           # SQLite databases (regenerated from portage)
                '*.sqlite-shm',       # SQLite shared memory files
                '*.sqlite-wal',       # SQLite write-ahead log files
                '*.lock',             # File locks
                '.update.lock',       # Resync sweep lock file
                '*.tmp',              # Temporary files
                '.temp_materialization/',  # Temporary materialization directory
            ],
            'version': [
                '*.portage.jsonl',             # Portage files (source of truth)
                '.cached_storage.yaml',        # Storage configuration
                '.cached_file_folders.json',   # Cache manifest
                f'*{self.slave_dir_extension}/',  # Slave directories (metadata)
            ]
        }

    def cleanup_empty_directories(self, file_path: Path, grouping_key: Optional[Sequence[str]]) -> None:
        """
        Clean up empty directories left behind after file deletion.
        Walks up the directory tree from the deleted file's location to the grouping key root,
        removing any empty directories encountered.
        
        Args:
            file_path: Path to the deleted file (used to determine cleanup path)
            grouping_key: Grouping key to determine the root directory for cleanup
        """
        if not file_path or not file_path.exists():
            return
            
        # Get the grouping key root directory
        grouping_root = self.category_folders.get_grouping_path(grouping_key)
        if not grouping_root:
            return
            
        # Start from the file's parent directory and walk up
        current_path = file_path.parent
        
        # Collect all parent directories up to the grouping root
        parent_dirs = []
        while (current_path != grouping_root and 
               current_path != current_path.parent and  # Stop at filesystem root
               current_path.is_relative_to(grouping_root)):  # Ensure we're within the grouping root
            parent_dirs.append(current_path)
            current_path = current_path.parent
        
        # Clean up empty directories, starting from the deepest level
        # Sort by path depth (deepest first) to ensure proper cleanup order
        parent_dirs.sort(key=lambda p: len(p.parts), reverse=True)
        
        for parent_dir in parent_dirs:
            try:
                # Only delete if the directory exists and is completely empty
                if (parent_dir.exists() and 
                    parent_dir.is_dir() and 
                    parent_dir != grouping_root and
                    not any(parent_dir.iterdir())):
                    
                    parent_dir.rmdir()
                    
            except (OSError, PermissionError) as e:
                # Log the error but continue with other directories
                import logging
                logger = logging.getLogger(__name__)
                logger.warning("Failed to delete empty directory %s: %s", parent_dir, e)

    def cleanup_retained_artifacts(self, max_age_seconds: int = 120) -> int:
        """
        Remove staged artifacts that were moved via move_to_retained().
        Intended to run after each change notification to prevent orphaned
        temporary files.
        """
        try:
            tmp = Path(tempfile.gettempdir())
            prefixes = self._config.get("temp_prefixes", {})
            cutoff = time.time() - max_age_seconds
            removed = 0
            for item in tmp.iterdir():
                name = item.name
                if any(name.startswith(pfx) for pfx in prefixes.values()):
                    try:
                        if item.stat().st_mtime < cutoff:
                            if self.delete_path_safely(item):
                                removed += 1
                    except Exception:
                        continue
            return removed
        except Exception:
            return 0

    def insert_file_to_storage(self, source_file: FileProxyBase, target_file_path: Path, 
                              target_slave_dir_path: Path, grouping_key: Optional[Sequence[str]], 
                              ref_path: str) -> None:
        """
        Handle all storage operations for file insertion.
        
        Args:
            source_file: The file proxy to deploy
            target_file_path: Where to place the file
            target_slave_dir_path: Where to create the slave directory
            grouping_key: Grouping key for database operations
            ref_path: Reference path for database storage
        """
        # Create target directory
        target_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Deploy the file
        source_file.deploy(str(target_file_path.parent))
        
        # Create slave directory
        self.create_slave_directory(target_file_path)
        
        # Update SQLite database with retry logic
        db = self.get_database(grouping_key)
        stored_path = self._serialize_path_for_storage(target_file_path, grouping_key)
        self._db_operation_with_retry(lambda: db.__setitem__(ref_path, stored_path))

    def get_slave_dir_path(self, file_path: Path) -> Path:
        return file_path.parent / (file_path.name + self.slave_dir_extension)

    def _get_grouping_base_path(self, grouping_key: Optional[Sequence[str]]) -> Path:
        """Internal: Get base filesystem path for a grouping key."""
        return self._get_grouping_root_path(grouping_key, create=False)

    def _get_grouping_root_path(self, grouping_key: Optional[Sequence[str]], create: bool = False) -> Path:
        """
        Return the filesystem root folder for a grouping key.

        Args:
            grouping_key: Grouping key sequence or None for flat patterns.
            create: When True, ensure the directory exists on disk.

        Returns:
            Path to the grouping root directory.
        """
        if self._pattern_has_variables():
            grouping_key_list = list(grouping_key) if grouping_key else []
            return self.category_folders.folder(grouping_key_list, create=create)

        base = self.category_folders.root_dir / self.category_folders.pattern.strip('/')
        if create:
            base.mkdir(parents=True, exist_ok=True)
        return base

    def _serialize_path_for_storage(self, absolute_path: Path, grouping_key: Optional[Sequence[str]]) -> str:
        """
        Represent a cached file path for persistence.

        The preferred format is relative to the grouping root so repositories remain
        relocatable. If the path cannot be expressed relative to that root (for example,
        legacy entries written with absolute paths or files moved outside the grouping
        directory), the absolute POSIX path is stored instead. Callers must always feed
        the result back through `_deserialize_stored_path` before filesystem access so
        mixed absolute/relative data continues to work.
        """
        try:
            grouping_root = self._get_grouping_root_path(grouping_key, create=True)
        except Exception:
            grouping_root = None

        try:
            resolved_path = absolute_path if absolute_path.is_absolute() else absolute_path.resolve(strict=False)
        except OSError:
            resolved_path = absolute_path

        if grouping_root is not None:
            try:
                relative_path = resolved_path.relative_to(grouping_root)
                # Use POSIX separators for portability
                return relative_path.as_posix()
            except ValueError:
                pass

        return resolved_path.as_posix()

    def _deserialize_stored_path(self, stored_path: str, grouping_key: Optional[Sequence[str]]) -> Path:
        """
        Convert a persisted path string back into an absolute filesystem location.

        Stored strings produced by `_serialize_path_for_storage` may be absolute or
        grouping-root-relative. Absolute paths are returned as-is. Relative paths are
        resolved against the grouping root (creating the directory tree on demand) so
        relocated caches remain accessible.
        """
        path_obj = Path(stored_path)
        if path_obj.is_absolute():
            return path_obj

        grouping_root = self._get_grouping_root_path(grouping_key, create=True)
        return grouping_root / path_obj

    def get_temp_directory_root(self, selector: Optional[str] = None) -> Path:
        """Get or create temporary directory root for various temporary operations.
        
        Args:
            selector: Optional string to specify subdirectory within temp area.
                     Currently unused but reserved for future subdivision of temp areas
                     (e.g., 'volatile' vs 'persistent' temp storage).
                     
        Returns:
            Path: Path to the temporary directory (or subdirectory if selector provided)
        """
        temp_root = self.category_folders.root_dir / ".temp_materialization"
        temp_root.mkdir(exist_ok=True)
        
        # Future: Add selector-based subdirectory logic here
        # if selector:
        #     temp_dir = temp_root / selector
        #     temp_dir.mkdir(exist_ok=True)
        #     return temp_dir
        
        return temp_root

    def cleanup_temp_files(self, max_age_seconds: int = 3600) -> int:
        """Clean up old temporary files in the materialization temp directory.
        
        Args:
            max_age_seconds: Maximum age of temp files before cleanup (default: 1 hour)
            
        Returns:
            int: Number of files cleaned up
        """
        temp_dir = self.get_temp_directory_root()
        if not temp_dir.exists():
            return 0
            
        current_time = time.time()
        cleaned = 0
        
        for temp_path in temp_dir.iterdir():
            try:
                if not temp_path.is_file():
                    continue
                    
                file_age = current_time - temp_path.stat().st_mtime
                if file_age >= max_age_seconds:
                    if self.delete_path_safely(temp_path):
                        cleaned += 1
            except (OSError, PermissionError) as e:
                # Log warning but continue with other files
                import logging
                logger = logging.getLogger(__name__)
                logger.warning("Failed to cleanup temp file %s: %s", temp_path, e)
            except Exception as e:
                # Log unexpected errors but continue
                import logging
                logger = logging.getLogger(__name__)
                logger.warning("Unexpected error cleaning up temp file %s: %s", temp_path, e)
        
        return cleaned

    def create_slave_directory(self, file_path: Path) -> Path:
        slave_dir_path = self.get_slave_dir_path(file_path)
        slave_dir_path.mkdir(parents=True, exist_ok=True)
        return slave_dir_path

    def delete_slave_directory(self, file_path: Path) -> None:
        slave_dir_path = self.get_slave_dir_path(file_path)
        try:
            if slave_dir_path.exists():
                if slave_dir_path.is_dir():
                    shutil.rmtree(slave_dir_path)
                elif slave_dir_path.is_file():
                    slave_dir_path.unlink()
        except OSError:
            pass

    def ref_path_to_filesystem_path(self, ref_path: str, file_name: str,
                                    grouping_key: Optional[Sequence[str]] = None) -> Path:
        """
        Map a logical ref_path to the concrete filesystem location for the cached file.

        Notes:
            - Any protocol prefix (e.g. https://) is stripped while preserving the path
              component so URLs remain inspectable on disk.
            - Only the directory segments derived from ref_path are created. If the final
              segment of ref_path contains a dot (looks like a filename), it is omitted
              from the directory structure because the actual file name comes from the
              materialized payload's `file_name`. The ref_path value still participates in
              cache lookups unchanged.
        """
        parsed_path = ref_path
        # Check for protocol separator (://) in first 15 characters
        # This detects URLs and strips the protocol+domain while preserving path structure
        protocol_sep_pos = parsed_path.find('://')
        if protocol_sep_pos != -1 and protocol_sep_pos < 15:
            # Found a protocol - strip everything up to and including first slash after ://
            after_protocol = parsed_path[protocol_sep_pos + 3:]  # Skip past ://
            first_slash_pos = after_protocol.find('/')
            if first_slash_pos != -1:
                # Strip domain, keep path after it
                parsed_path = after_protocol[first_slash_pos + 1:]
            # If no slash after protocol, parsed_path stays as-is (e.g., "https://example.com")
        parsed_path = parsed_path.split('?')[0]
        parsed_path = parsed_path.split('#')[0]
        path_components = [comp for comp in parsed_path.replace('\\', '/').split('/') if comp]
        if path_components and '.' in path_components[-1]:
            path_components.pop()

        safe_components = []
        for component in path_components:
            safe_component = component
            for illegal_char, replacement in self.char_replacement_map.items():
                safe_component = safe_component.replace(illegal_char, replacement)
            safe_components.append(safe_component)

        if self._pattern_has_variables():
            grouping_key_list = list(grouping_key) if grouping_key else []
            grouping_folder = self.category_folders.folder(grouping_key_list)
        else:
            grouping_folder = self.category_folders.root_dir / self.category_folders.pattern.strip('/')

        if safe_components:
            return grouping_folder / Path(*safe_components) / file_name
        return grouping_folder / file_name

    def compare_files(self, file_proxy_or_path: Union[FileProxyBase, Path], existing_file_path: Path) -> Optional[bool]:
        """Compare files using the configured comparison method (mtime or xxhash)."""
        if not existing_file_path.exists():
            return None
        try:
            return (self._compare_files_with_xxhash if self.use_xxhash else self._compare_files_with_mtime)(
                file_proxy_or_path, existing_file_path
            )
        except Exception:
            return None

    def _compare_files_with_mtime(self, file_proxy_or_path: Union[FileProxyBase, Path], existing_file_path: Path) -> Optional[bool]:
        """Compare files using mtime and size. Can handle both proxies and materialized files."""
        try:
            existing_stat = existing_file_path.stat()
            
            # Handle materialized file (Path object)
            if isinstance(file_proxy_or_path, Path):
                source_stat = file_proxy_or_path.stat()
                return (source_stat.st_size == existing_stat.st_size and 
                       source_stat.st_mtime == existing_stat.st_mtime)
            
            # Handle file proxy
            file_proxy = file_proxy_or_path
            if hasattr(file_proxy, 'looks_same'):
                result = file_proxy.looks_same(str(existing_file_path))
                if result is not None:
                    return result
            
            return None
        except OSError:
            return None

    def _compare_files_with_xxhash(self, file_proxy_or_path: Union[FileProxyBase, Path], existing_file_path: Path) -> Optional[bool]:
        """Compare files using xxhash. Can handle both proxies and materialized files."""
        try:
            # Handle materialized file (Path object)
            if isinstance(file_proxy_or_path, Path):
                return self._compute_and_compare_xxhash(file_proxy_or_path, existing_file_path)
            
            # Handle file proxy - need to materialize for hash comparison
            file_proxy = file_proxy_or_path
            if hasattr(file_proxy, 'looks_same'):
                result = file_proxy.looks_same(str(existing_file_path))
                if result is not None:
                    return result
            
            return None
        except OSError:
            return None

    def _compute_and_compare_xxhash(self, source_file_path: Path, existing_file_path: Path) -> Optional[bool]:
        """Compute and compare xxhash of two files."""
        try:
            # Try to use xxhash if available, fall back to hashlib.sha256
            try:
                import xxhash
                source_hash = xxhash.xxh64(source_file_path.read_bytes()).hexdigest()
                existing_hash = xxhash.xxh64(existing_file_path.read_bytes()).hexdigest()
            except ImportError:
                # Fallback to SHA256 if xxhash is not available
                source_hash = hashlib.sha256(source_file_path.read_bytes()).hexdigest()
                existing_hash = hashlib.sha256(existing_file_path.read_bytes()).hexdigest()
            
            return source_hash == existing_hash
        except (OSError, IOError):
            return None

    def _grouping_exists(self, grouping_key: Optional[Sequence[str]] = None) -> bool:
        """Check if a grouping exists without creating any filesystem artifacts."""
        key = tuple(grouping_key) if grouping_key is not None else ()

        if key in self._sqlite_databases:
            return True

        if key:
            safe_key = "_".join(str(k) for k in key)
            safe_key = "".join(c if c.isalnum() or c in '-_' else '_' for c in safe_key)
            db_filename = f".cached_files_{safe_key}"
            grouping_dir = self.category_folders.folder(grouping_key, create=False)
            db_path = grouping_dir / db_filename
            sqlite_path = db_path.with_suffix('.sqlite')
            portage_path = sqlite_path.with_suffix('.portage.jsonl')

            if sqlite_path.exists():
                return True

            if portage_path.exists():
                return True

            if grouping_dir.exists():
                return True
        else:
            db_filename = ".cached_files_global"
            db_path = self.category_folders.root_dir / db_filename
            sqlite_path = db_path.with_suffix('.sqlite')
            portage_path = sqlite_path.with_suffix('.portage.jsonl')

            if sqlite_path.exists():
                return True

            if portage_path.exists():
                return True

        return False

    def get_database(self, grouping_key: Optional[Sequence[str]] = None):
        """Get or create a SQLite database for the given grouping key.

        Legacy DBM migration has been removed; caches must already be in SQLite form
        (or restored from a portage export) before this method is called.

        Args:
            grouping_key: Optional grouping key for the database
            
        Returns:
            SqliteDict: The database instance
        """
        key = tuple(grouping_key) if grouping_key is not None else ()
        if key not in self._sqlite_databases:
            if key:
                safe_key = "_".join(str(k) for k in key)
                safe_key = "".join(c if c.isalnum() or c in '-_' else '_' for c in safe_key)
                db_filename = f".cached_files_{safe_key}"
                # Place database in the grouping directory instead of cache root
                grouping_dir = self.category_folders.folder(grouping_key, create=True)
                db_path = grouping_dir / db_filename
            else:
                db_filename = ".cached_files_global"
                # Global database stays at cache root
                db_path = self.category_folders.root_dir / db_filename
                self.category_folders.root_dir.mkdir(parents=True, exist_ok=True)
            
            # Use .sqlite extension for SQLite databases
            sqlite_path = db_path.with_suffix('.sqlite')
            
            if not sqlite_path.exists():
                # DBM migration support was removed in 2025-11. We accelerated this change—sorry for the disruption.
                # Existing caches must be exported via the JSONL portage flow prior to upgrade.
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)

                import logging

                portage_path = sqlite_path.with_suffix('.portage.jsonl')
                if portage_path.exists():
                    logger = logging.getLogger(__name__)
                    logger.info(f"SQLite database missing, rebuilding from portage: {portage_path.name}")
                    try:
                        stats = self._restore_from_portage(portage_path, grouping_key, validate_files=True)
                        logger.info(f"Restored {stats['entries_restored']} entries from portage")
                    except Exception as e:
                        logger.error(f"Failed to restore from portage, creating new database: {e}")
            
            # Open SQLite database with autocommit for simplicity and WAL mode for concurrency
            self._sqlite_databases[key] = SqliteDict(
                str(sqlite_path),
                autocommit=True,
                journal_mode='WAL'
            )
        return self._sqlite_databases[key]

    def close_databases(self):
        """Close all open SQLite databases and clear the cache."""
        for db in self._sqlite_databases.values():
            try:
                db.close()
            except Exception:
                pass
        self._sqlite_databases.clear()
    
    def _db_operation_with_retry(self, operation_func, max_retries=5, delay=0.1):
        """Execute database operation with retry logic for concurrent access.
        
        This method wraps database operations to handle transient locking conflicts
        that can occur during concurrent access. It retries the operation with a
        small delay between attempts.
        
        Args:
            operation_func: Callable that performs the database operation
            max_retries: Maximum number of retry attempts (default: 5)
            delay: Delay in seconds between retries (default: 0.1)
            
        Returns:
            The return value from operation_func
            
        Raises:
            The last exception if all retries are exhausted
            
        Example:
            >>> def set_value():
            ...     db[key] = value
            >>> self._db_operation_with_retry(set_value)
        """
        import time
        for attempt in range(max_retries):
            try:
                return operation_func()
            except Exception as e:
                if attempt == max_retries - 1:
                    # Last attempt failed, re-raise the exception
                    raise
                # Log retry attempt for debugging
                import logging
                logger = logging.getLogger(__name__)
                logger.debug(f"Database operation retry {attempt + 1}/{max_retries}: {e}")
                time.sleep(delay)

    def existing_grouping_keys(self, 
                              filters: Optional[Union[Dict[str, str], List[str]]] = None,
                              reverse: bool = False) -> Iterator[Sequence[str]]:
        """
        Find existing grouping keys that match glob-style filters.
        
        This leverages CategoryFolders.existing_folders() to find matching folders,
        then extracts the grouping keys from those folder paths.
        
        Args:
            filters: Glob patterns for grouping key components.
                Can be a dict mapping field names to patterns, or a list of patterns
                in pattern field order. Use "*" or "" for wildcards.
            reverse: If True, return keys in reverse order.
            
        Yields:
            Sequence[str]: Grouping key tuples matching the specified filters.
            
        Example:
            # Find all grouping keys for "project*" groups
            keys = list(storage.existing_grouping_keys(filters={"project": "project*", "category": "*"}))
            # Returns: [("webapp-v1", "api"), ("webapp-v2", "ui"), ...]
            
            # Find all grouping keys for "api" category
            keys = list(storage.existing_grouping_keys(filters={"project": "*", "category": "api"}))
            # Returns: [("webapp", "api"), ("mobile", "api"), ...]
        """
        # Use CategoryFolders existing_folders method for filtering
        matching_folders = self.category_folders.existing_folders(filters=filters, reverse=reverse)
        
        # Extract grouping keys from folder paths
        for folder_path in matching_folders:
            # Skip hidden directories (starting with .) to avoid versioning system directories like .git/
            if any(part.startswith('.') for part in folder_path.parts):
                continue
            try:
                # Convert absolute path to relative path before inferring key
                # This prevents infer_key() from matching unintended parts of the absolute path
                relative_path = folder_path.relative_to(self.category_folders.root_dir)
                key_data = self.category_folders.infer_key(relative_path)
                # Convert to tuple of field values in order
                grouping_key = tuple(getattr(key_data, field) for field in self.category_folders.key_names())
                yield grouping_key
            except Exception:
                # Skip folders that don't match the pattern
                continue

    def existing_cached_files(self, 
                             grouping_key_filters: Optional[Union[Dict[str, str], List[str]]] = None,
                             ref_path_glob: Optional[str] = None,
                             reverse: bool = False) -> Iterator['CachedFileRef']:
        """
        Find existing cached files with optional glob-style filters.
        
        This method combines folder-level filtering (using existing_grouping_folders) with
        file-level filtering on reference paths using a single glob pattern.
        
        Args:
            grouping_key_filters: Glob patterns for grouping key components.
                Can be a dict mapping field names to patterns, or a list of patterns
                in pattern field order. Use "*" or "" for wildcards.
            ref_path_glob: A single glob pattern to match against reference paths.
                The pattern can match any part of the full reference path.
                Path separators are automatically normalized for cross-platform compatibility.
            reverse: If True, return files in reverse order.
            
        Yields:
            CachedFileRef: Files matching the specified filters.
            
        Example:
            # Find all files in "project*" groups
            files = list(storage.existing_cached_files(grouping_key_filters={"project": "project*", "category": "*"}))
            
            # Find all PDF files (matches anywhere in the path)
            files = list(storage.existing_cached_files(ref_path_glob="*.pdf"))
            
            # Find all files with "config" anywhere in the path
            files = list(storage.existing_cached_files(ref_path_glob="*config*"))
            
            # Find all files in "api" category with "config" in the name
            files = list(storage.existing_cached_files(
                grouping_key_filters={"project": "*", "category": "api"},
                ref_path_glob="*config*"
            ))
            
            # Find files in specific path patterns
            files = list(storage.existing_cached_files(ref_path_glob="*/api/*.json"))
        """
        
        # Get all grouping keys that match the filters
        matching_grouping_keys = self.existing_grouping_keys(grouping_key_filters, reverse)
        
        # Iterate through matching grouping keys and their files
        for grouping_key in matching_grouping_keys:
            
            # Get the SQLite database for this grouping key
            db = self.get_database(grouping_key)
            ref_paths = list(db.keys())
            
            # Sort for deterministic iteration
            ref_paths.sort()
            if reverse:
                ref_paths.reverse()
            
            # Filter and yield files
            for ref_path in ref_paths:
                # Apply reference path glob filter if specified
                if ref_path_glob:
                    # Normalize path separators in both the ref_path and glob pattern
                    # to handle cross-platform compatibility
                    normalized_ref_path = self._normalize_path_separators(ref_path)
                    normalized_glob = self._normalize_path_separators(ref_path_glob)
                    
                    if not fnmatch.fnmatch(normalized_ref_path, normalized_glob):
                        continue
                
                stored_path = db[ref_path]
                file_path = self._deserialize_stored_path(stored_path, grouping_key)
                
                # Verify the file still exists on disk
                if file_path.exists():
                    slave_dir_path = self.get_slave_dir_path(file_path)
                    
                    file_ref = CachedFileRef(
                        ref_path=ref_path,
                        grouping_key=grouping_key,
                        file_path=file_path,
                        slave_dir_path=slave_dir_path
                    )
                    file_ref._metadata_filename = self.metadata_filename
                    yield file_ref
                else:
                    # File no longer exists on disk, remove from database with retry logic
                    self._db_operation_with_retry(lambda: db.__delitem__(ref_path))

    def _normalize_path_separators(self, path: str) -> str:
        """
        Normalize path separators to use the current operating system's separator.
        
        This allows users to write glob patterns with either forward slashes or
        backslashes, and they will work correctly on any operating system.
        
        Args:
            path: Path string that may contain mixed or incorrect separators
            
        Returns:
            str: Path with normalized separators for the current OS
        """
        # Get the current OS separator
        current_sep = os.sep
        
        # Replace both forward slashes and backslashes with the current OS separator
        # This handles cases where users might hard-code separators in their patterns
        normalized = path.replace('/', current_sep).replace('\\', current_sep)
        
        return normalized

    # -----------------------------
    # CategoryFolders delegation methods
    # -----------------------------
    
    def _pattern_has_variables(self) -> bool:
        """Check if the pattern contains variables that require grouping keys."""
        import re
        return bool(re.search(r'\{[^}]+\}', self.category_folders.pattern))
    
    def normalize_grouping_key(self, grouping_key: Optional[Sequence[str]]) -> Sequence[str]:
        """Normalize and validate a grouping key."""
        if self._pattern_has_variables():
            if grouping_key is None:
                raise ValueError(f"grouping_key is required when using grouped patterns. "
                               f"Pattern '{self.category_folders.pattern}' contains variables and requires grouping keys.")
            return grouping_key
        else:
            # For flat patterns, grouping_key must be None or empty tuple
            # Any non-empty grouping key is nonsensical for flat patterns
            if grouping_key is not None and len(grouping_key) > 0:
                raise ValueError(f"grouping_key is not allowed for flat patterns. "
                               f"Pattern '{self.category_folders.pattern}' has no variables and represents a single repository.")
            return ()
    
    def key_names(self) -> List[str]:
        """Get the key field names in pattern order."""
        return self.category_folders.key_names()
    
    # -----------------------------
    # Portage file methods
    # -----------------------------
    
    def _get_sqlite_path(self, grouping_key: Optional[Sequence[str]] = None) -> Path:
        """Get path to SQLite database file for a grouping key."""
        if grouping_key:
            safe_key = "_".join(str(k) for k in grouping_key)
            safe_key = "".join(c if c.isalnum() or c in '-_' else '_' for c in safe_key)
            db_filename = f".cached_files_{safe_key}"
            grouping_dir = self.category_folders.folder(grouping_key, create=False)
            db_path = grouping_dir / db_filename
        else:
            db_filename = ".cached_files_global"
            db_path = self.category_folders.root_dir / db_filename
        return db_path.with_suffix('.sqlite')

    def _get_portage_path(self, grouping_key: Optional[Sequence[str]] = None) -> Path:
        """Get path to portage file for a grouping key."""
        sqlite_path = self._get_sqlite_path(grouping_key)
        return sqlite_path.with_suffix('.portage.jsonl')
    
    def _portage_database(self, grouping_key: Optional[Sequence[str]], include_metadata: bool) -> Path:
        """Generate portage JSONL file for a specific database.
        
        Args:
            grouping_key: Grouping key for the database
            include_metadata: If True, include mtime and size for each entry
            
        Returns:
            Path to the created portage file
        """
        import json
        import logging
        
        logger = logging.getLogger(__name__)
        
        portage_path = self._get_portage_path(grouping_key)
        sqlite_path = self._get_sqlite_path(grouping_key)
        
        # Get database (will create if doesn't exist)
        db = self.get_database(grouping_key)
        
        # Create portage file with atomic write (temp + rename)
        temp_portage = portage_path.parent / f"{portage_path.name}.tmp"
        portage_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(temp_portage, 'w', encoding='utf-8') as f:
                # Write header comments
                f.write(f"# CachedFileFolders Portage File v1.0\n")
                f.write(f"# Grouping Pattern: {self.category_folders.pattern}\n")
                if grouping_key:
                    f.write(f"# Grouping Key: {list(grouping_key)}\n")
                f.write(f"# SQLite Database: {sqlite_path.name}\n")
                f.write(f"#\n")
                if include_metadata:
                    f.write(f'# Format: {{"ref_path": str, "file_path": str, "mtime": float, "size": int}}\n')
                else:
                    f.write(f'# Format: {{"ref_path": str, "file_path": str}}\n')
                f.write(f"# Note: file_path values are stored relative to the grouping root when possible.\n")
                
                # Collect and sort entries by filesystem path structure
                entries_data = []
                for ref_path in db.keys():
                    stored_path = db[ref_path]
                    absolute_path = self._deserialize_stored_path(stored_path, grouping_key)
                    entries_data.append((ref_path, stored_path, absolute_path))
                
                # Sort by filesystem path components
                def get_sort_key(entry_tuple):
                    _, _, path_obj = entry_tuple
                    
                    # Get the grouping folder to calculate relative path
                    grouping_folder = self._get_grouping_root_path(grouping_key, create=False)
                    
                    try:
                        # Calculate relative path from grouping folder, excluding grouping key components
                        rel_path = path_obj.relative_to(grouping_folder)
                        # Convert to list of components for sorting
                        return list(rel_path.parts)
                    except ValueError:
                        # Fallback to original ref_path sorting if relative path calculation fails
                        return [ref_path]
                
                sorted_entries = sorted(entries_data, key=get_sort_key)
                
                # Write entries with directory change comments
                entry_count = 0
                last_directory = None
                
                for ref_path, stored_path, path_obj in sorted_entries:
                    
                    # Calculate current directory relative to grouping folder
                    grouping_folder = self._get_grouping_root_path(grouping_key, create=False)
                    
                    try:
                        rel_path = path_obj.relative_to(grouping_folder)
                        current_directory = str(rel_path.parent) if rel_path.parent != Path('.') else ""
                    except ValueError:
                        current_directory = ""
                    
                    # Add directory comment if directory changed
                    if current_directory != last_directory:
                        if current_directory:
                            f.write(f"# Directory: {current_directory}\n")
                        last_directory = current_directory
                    
                    # Create entry data
                    entry = {
                        "ref_path": ref_path,
                        "file_path": stored_path
                    }
                    
                    # Add optional metadata
                    if include_metadata:
                        try:
                            file_stat = path_obj.stat()
                            entry["mtime"] = file_stat.st_mtime
                            entry["size"] = file_stat.st_size
                        except (OSError, FileNotFoundError):
                            # File missing - skip metadata but include entry
                            pass
                    
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                    entry_count += 1
            
            # Atomic rename
            temp_portage.replace(portage_path)
            logger.info(f"Generated portage file with {entry_count} entries: {portage_path}")
            return portage_path
            
        except Exception as e:
            # Clean up temp file on error
            if temp_portage.exists():
                temp_portage.unlink()
            raise RuntimeError(f"Failed to generate portage file: {e}") from e
    
    def _restore_from_portage(self, portage_path: Path, grouping_key: Optional[Sequence[str]], 
                             validate_files: bool) -> Dict[str, int]:
        """Restore SQLite database from portage file.
        
        Args:
            portage_path: Path to portage JSONL file
            grouping_key: Grouping key for the database
            validate_files: If True, skip entries where file doesn't exist on disk
            
        Returns:
            Statistics dictionary with restoration results
        """
        import json
        import logging
        
        logger = logging.getLogger(__name__)
        
        if not portage_path.exists():
            raise FileNotFoundError(f"Portage file not found: {portage_path}")
        
        sqlite_path = self._get_sqlite_path(grouping_key)
        stats = {
            'entries_total': 0,
            'entries_restored': 0,
            'entries_skipped': 0,
            'entries_invalid': 0
        }
        
        logger.info(f"Restoring SQLite database from portage file: {portage_path}")
        
        try:
            # Open SQLite database (creates new one)
            from sqlitedict import SqliteDict
            with SqliteDict(str(sqlite_path), autocommit=False, journal_mode='WAL') as db:
                with open(portage_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        # Skip comment lines
                        if line.startswith('#') or not line.strip():
                            continue
                        
                        stats['entries_total'] += 1
                        
                        try:
                            entry = json.loads(line)
                            ref_path = entry['ref_path']
                            stored_path = entry['file_path']
                            if not isinstance(stored_path, str):
                                stats['entries_invalid'] += 1
                                logger.warning(f"Invalid portage entry at line {line_num}: file_path must be a string")
                                continue
                            absolute_path = self._deserialize_stored_path(stored_path, grouping_key)
                            
                            # Validate file exists if requested
                            if validate_files and not absolute_path.exists():
                                stats['entries_skipped'] += 1
                                logger.debug(f"Skipping missing file: {ref_path}")
                                continue
                            
                            # Restore to database
                            normalized_stored_path = self._serialize_path_for_storage(absolute_path, grouping_key)
                            db[ref_path] = normalized_stored_path
                            stats['entries_restored'] += 1
                            
                        except (json.JSONDecodeError, KeyError) as e:
                            stats['entries_invalid'] += 1
                            logger.warning(f"Invalid portage entry at line {line_num}: {e}")
                            continue
                
                # Commit all changes
                db.commit()
            
            logger.info(f"Restored {stats['entries_restored']} entries from portage "
                       f"(skipped: {stats['entries_skipped']}, invalid: {stats['entries_invalid']})")
            return stats
            
        except Exception as e:
            raise RuntimeError(f"Failed to restore from portage: {e}") from e
    
    def purge_folders(self, filters=None, dry_run=False):
        """Purge folders using CategoryFolders functionality."""
        return self.category_folders.purge_folders(filters, dry_run)
    
    def purge(self, grouping_key: Optional[Sequence[str]] = None, dry_run: bool = False) -> List[Path]:
        """Purge cache files.
        
        WARNING: This operation is unrecoverable and should be used carefully.
        All cached files, slave directories, and databases will be permanently deleted.
        
        Args:
            grouping_key: Specific grouping to purge. If None, purges entire cache.
            dry_run: If True, return what would be deleted without deleting
            
        Returns:
            List of deleted (or would-be-deleted) paths
        """
        if grouping_key is None:
            # Purge everything
            if self._pattern_has_variables():
                return self.purge_folders(dry_run=dry_run)
            
            pattern_dir = self.category_folders.root_dir / self.category_folders.pattern.strip('/')
            if pattern_dir.exists():
                if not dry_run:
                    shutil.rmtree(pattern_dir)
                return [pattern_dir]
            return []
        else:
            # Purge specific grouping
            normalized_key = self.normalize_grouping_key(grouping_key)
            expected_len = len(self.key_names())
            if len(normalized_key) != expected_len:
                raise ValueError(
                    f"grouping_key must have {expected_len} elements matching pattern fields {self.key_names()}. "
                    f"Received {len(normalized_key)}: {list(normalized_key)}"
                )
            return self.purge_folders(filters=list(normalized_key), dry_run=dry_run)

    def recover_orphaned_file_to_database(self, ref_path: str, grouping_key: Optional[Sequence[str]], 
                                         target_file_path: Path) -> 'CachedFileRef':
        """
        Recover an orphaned file by adding it to the database.
        
        This method is used when a file exists on the filesystem but is not tracked
        in the database, typically due to a previous database update failure.
        
        Args:
            ref_path: Reference path for the file
            grouping_key: Grouping key for the file
            target_file_path: Path to the orphaned file on disk
            
        Returns:
            CachedFileRef: The recovered file reference
            
        Raises:
            RuntimeError: If the file doesn't exist on disk or database update fails
        """
        # Verify the file actually exists on disk
        if not target_file_path.exists():
            raise RuntimeError(f"Cannot recover orphaned file: {target_file_path} does not exist")
        
        # Create slave directory if it doesn't exist
        slave_dir_path = self.get_slave_dir_path(target_file_path)
        if not slave_dir_path.exists():
            try:
                self.create_slave_directory(target_file_path)
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning("Failed to create slave directory for orphaned file %s: %s", target_file_path, e)
        
        # Update SQLite database with retry logic
        try:
            db = self.get_database(grouping_key)
            stored_path = self._serialize_path_for_storage(target_file_path, grouping_key)
            self._db_operation_with_retry(lambda: db.__setitem__(ref_path, stored_path))
        except Exception as e:
            raise RuntimeError(f"Failed to update database for orphaned file {ref_path}: {e}") from e
        
        file_ref = CachedFileRef(
            ref_path=ref_path,
            grouping_key=grouping_key,
            file_path=target_file_path,
            slave_dir_path=slave_dir_path
        )
        file_ref._metadata_filename = self.metadata_filename
        return file_ref


