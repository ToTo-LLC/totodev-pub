#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Local File Tree Sync - Cache Design Patterns Example

This example demonstrates core design patterns for building intelligent caches using 
local files as the data source. It complements the SharePoint example by showing how
the same CachedFileFolders patterns work for any data source behind the FileProxy interface.

This simplified example is ideal for:
- Learning cache synchronization patterns without external dependencies
- Testing and experimentation without authentication setup
- Understanding the FileProxy abstraction's power
- Quick prototyping of cache-based workflows

## REQUIREMENTS

### Python Path Setup
This script requires that the `totodev_pub` package be available in the Python path.
This is typically accomplished by mounting the totodev_pub repo in your project at
`src/totodev_pub` as a submodule and including `src/` in your PYTHONPATH.
Ensure that the `src/` directory is in your PYTHONPATH or run the script from within the `src/` directory.

### No External Dependencies
Unlike the SharePoint example, this script requires NO environment variables or external services.
It works immediately with any local directory.

## Learning Objectives

This script demonstrates the same core design patterns as the SharePoint example:

### FileProxy Abstraction Power
- **Same Interface, Different Backend**: Shows how LocalFileProxy and SharepointFileProxy work identically
- **Unified Cache API**: The same resync_bulk() call works for local and remote sources
- **Portable Patterns**: Change handlers work the same regardless of data source

### Intelligent Cache Architecture
- **Automatic Change Detection**: CachedFileFolders handles INSERT/UPDATE/DELETE without manual diff logic
- **File Organization**: Flexible grouping patterns for organizing cached data
- **Concurrent Processing**: Async patterns for efficient processing
- **Retention Policies**: Predictable file retention with safety windows

### Production-Ready Sync Patterns
- **Bulk Synchronization**: Simple one-call synchronization with automatic change detection
- **Change Handlers**: Process files differently based on type (documents, images, etc.)
- **Progress Tracking**: Comprehensive result reporting

## Usage

```bash
python local_file_tree_sync.py \\
    --cache-root volatile/local_sync/ \\
    --dir-key my-project \\
    --source-dir ~/Documents/Projects \\
    --glob-pattern "**/*.pdf"
```

## Command Line Arguments

- `--cache-root`: Root directory for the CachedFileFolders cache (required)
- `--dir-key`: Arbitrary key for organizing files in the cache (required)
- `--source-dir`: Local directory to scan for files (required)
- `--glob-pattern`: Glob pattern for file selection (default: "**/*" for all files)
- `--max-files`: Maximum number of files to process before exiting (optional, useful for testing)
- `--follow-symlinks`: Follow symbolic links when scanning (flag, default: False)

## File Organization

Files are organized in the cache using the pattern: `key-{dir_key}/`
The `--dir-key` parameter controls which subdirectory is used within this pattern.
For example, with `--dir-key my-docs`, files will be cached under `key-my-docs/`

Note: The grouping_key passed to CachedFileFolders is `[dir_key]` to match this pattern.

## Examples

### Sync all PDF files from a directory
```bash
python local_file_tree_sync.py \\
    --cache-root volatile/local_sync/ \\
    --dir-key documents \\
    --source-dir ~/Documents \\
    --glob-pattern "**/*.pdf"
```

### Sync with file limit for testing
```bash
python local_file_tree_sync.py \\
    --cache-root volatile/local_sync/ \\
    --dir-key test-run \\
    --source-dir ~/Projects \\
    --glob-pattern "**/*.{txt,md}" \\
    --max-files 5
```

### Sync including symlinks
```bash
python local_file_tree_sync.py \\
    --cache-root volatile/local_sync/ \\
    --dir-key linked-docs \\
    --source-dir ~/LinkedFolder \\
    --follow-symlinks
```

## Integration Notes

This tool demonstrates a clean architecture built on:
- `SimpleLocalFileSync`: Encapsulates sync logic and provides clean API
- `LocalFileProxyFactory`: For discovering and accessing local files via glob patterns
- `CachedFileFolders`: For intelligent caching and change detection

The `SimpleLocalFileSync` class can be extracted and reused in other projects.
Compare this with the SharePoint example to see how the same patterns apply
to different data sources through the FileProxy abstraction.

## Comparison to SharePoint Example

| Aspect | SharePoint | Local Files |
|--------|-----------|-------------|
| Setup | Requires OAuth credentials | Works immediately |
| Dependencies | msal, requests | None (beyond project) |
| File Discovery | Microsoft Graph API | Glob patterns |
| Use Case | Production sync tool | Learning/testing |
| Complexity | ~475 lines | ~300 lines |
| Core Pattern | **Identical** | **Identical** |

The key insight: Despite different backends, both use the same CachedFileFolders
API and change handler patterns, demonstrating the power of abstraction.
"""

import sys
import os
import asyncio
from typing import List, Optional, Callable, Union, Sequence
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass

import click

DUMMY_GROUPING_PATTERN = "key-{dir_key}/"

from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxyFactory
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support import ChangeNotice, ChangeType
from totodev_pub.cached_file_folders_support.sync_types import UpsertFailure, ResyncBulkResult


@dataclass
class SyncResult:
    """Simple result object containing sync statistics and changes."""
    insert_count: int
    update_count: int
    delete_count: int
    changes: List[ChangeNotice]
    failures: List[UpsertFailure]
    total_files_scanned: int

class SimpleLocalFileSync:
    """Simplified local file synchronization class for tutorial purposes.
    
    This class encapsulates local file scanning and caching logic,
    allowing users to focus on business logic through change handlers.
    
    This mirrors the SimpleSharepointSync class but is much simpler since
    it doesn't require authentication or API calls - demonstrating how
    the same patterns work across different data sources.
    """

    ChangeEventHandler = Callable[[ChangeNotice, Optional[FileProxyBase]], None]
    
    def __init__(self, cache: CachedFileFolders, grouping_key: str, 
                 source_dir: str, glob_pattern: str = "**/*", 
                 follow_symlinks: bool = False):
        """Initialize the sync instance with a CachedFileFolders object and configuration.
        
        Args:
            cache: CachedFileFolders instance to use for synchronization
            grouping_key: Key to use when storing files in the cache
            source_dir: Local directory to scan for files
            glob_pattern: Glob pattern for file selection (e.g., "**/*.pdf", "**/*")
            follow_symlinks: Whether to follow symbolic links when scanning
        """
        self.cache = cache
        self.grouping_key = grouping_key
        self.source_dir = Path(source_dir).expanduser().resolve()
        self.glob_pattern = glob_pattern
        self.follow_symlinks = follow_symlinks
        self._handlers: dict[str, Callable[[ChangeNotice], None]] = {}
    
    def set_change_handler(self, file_extension: Union[str, Sequence[str]], handler: ChangeEventHandler):
        """Set a callback function to handle file changes for a specific file type.

        Can do things with the file and the "slave directory" (a directory created alongside the cached file)
        Such as:
          - Convert to markdown into the slave directory
          - Create metadata file into the slave directory
          - OCR scan into the slave directory
          - Chunk and insert into a vector database
          - Remove from databases (if change type is DELETE)

        Note that the old slave directory and file is automatically deleted after the handler returns.
        This means that the handler can access the old file and slave directory briefly if needed.
        
        Args:
            file_extension: File extension (e.g., '.md', '.docx', '*') - use lowercase
            handler: Function that receives a ChangeNotice object for file changes
        """
        extensions = [file_extension.lower()] if isinstance(file_extension, str) else file_extension
        for ext in extensions:
            self._handlers[ext] = handler

    
    async def sync(self, max_files: Optional[int] = None) -> SyncResult:
        """Execute synchronization of local files to cache.
        
        Args:
            max_files: Optional limit on number of files to process (useful for testing)
            
        Returns:
            SyncResult with statistics and change information
        """
        if not self.source_dir.exists():
            raise FileNotFoundError(f"Source directory does not exist: {self.source_dir}")
        if not self.source_dir.is_dir():
            raise NotADirectoryError(f"Source path is not a directory: {self.source_dir}")
        
        factory = LocalFileProxyFactory()
        original_cwd = os.getcwd()
        
        try:
            os.chdir(str(self.source_dir))
            
            def file_proxy_iterator():
                files_processed = 0
                for proxy in factory.scan_files(self.glob_pattern, self.follow_symlinks):
                    if max_files is not None and files_processed >= max_files:
                        break
                    files_processed += 1
                    yield proxy
            
            resync_result: ResyncBulkResult = await self.cache.resync_bulk(
                file_proxies=file_proxy_iterator(),
                grouping_key=[self.grouping_key],
                upsert_fail_policy="RETAIN_OLD",
                max_concurrent_requests=5
            )
        finally:
            os.chdir(original_cwd)
        
        stats = {'insert': 0, 'update': 0, 'delete': 0}
        for change in resync_result.changes:
            change_type = change.change_type.value.lower()
            stats[change_type] += 1
            
            if handler := self._get_handler_for_change(change):
                handler(change)
        
        return SyncResult(
            insert_count=stats['insert'],
            update_count=stats['update'], 
            delete_count=stats['delete'],
            changes=resync_result.changes,
            failures=resync_result.failures,
            total_files_scanned=len(resync_result.changes) + len(resync_result.failures)
        )
    
    def _get_handler_for_change(self, change: ChangeNotice) -> Optional[ChangeEventHandler]:
        """Get the appropriate handler for a file change based on extension."""
        file_path = change.old.file_path if change.change_type == ChangeType.DELETE else change.cur.file_path
        ext = (file_path.suffix or "").lower()
        return self._handlers.get(ext) or self._handlers.get('*')


def _make_change_handler(file_type: str) -> Callable[[ChangeNotice, Optional[FileProxyBase]], None]:
    """Factory function to create change handlers for different file types."""
    def handler(change: ChangeNotice, proxy: Optional[FileProxyBase]) -> None:
        """Handle file changes. Proxy argument available but not used in this example."""
        change_type = change.change_type.value.lower()
        
        if change_type in ['insert', 'update']:
            (change.cur.slave_dir_path / "processing_info.txt").write_text(
                f"File: {change.cur.file_path.name}\n"
                f"Processed: {datetime.now().isoformat()}\n"
                f"Type: {file_type}"
            )
            click.echo(f"  {file_type} {change_type}: {change.cur.file_path.name}")
        elif change_type == 'delete':
            filename = change.old.file_path.name if change.old else 'unknown'
            click.echo(f"  {file_type} deleted: {filename}")
    
    return handler


async def sync_local_folder(
    cache_root: str,
    dir_key: str, 
    source_dir: str,
    glob_pattern: str,
    max_files: Optional[int],
    follow_symlinks: bool
) -> None:
    """Demonstrate the complete local file sync workflow using SimpleLocalFileSync.
    
    This function shows how the SimpleLocalFileSync class provides a clean interface
    for file synchronization, while allowing business logic to be handled through
    callback functions.
    
    Key learning concepts:
    1. Class-based encapsulation: Sync infrastructure hidden behind simple interface
    2. Callback pattern: Business logic separated from infrastructure concerns  
    3. Clean separation: Sync logic vs. file processing logic are distinct
    4. Reusability: The SimpleLocalFileSync class can be used in other contexts
    5. Per-file-type handlers: Different processing for different file types
    
    Compare this with sync_sharepoint_folder() in the SharePoint example to see
    how the same patterns apply despite different data sources.
    """
    
    cache = CachedFileFolders(
        grouping_pattern=DUMMY_GROUPING_PATTERN,
        root_dir=os.path.abspath(cache_root),
        use_xxhash=False
    )
    
    sync = SimpleLocalFileSync(
        cache=cache,
        grouping_key=dir_key,
        source_dir=source_dir,
        glob_pattern=glob_pattern,
        follow_symlinks=follow_symlinks
    )
    
    sync.set_change_handler(['.docx', '.doc', '.pdf', '.txt', '.rtf', '.md'], _make_change_handler("Document"))
    sync.set_change_handler(['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'], _make_change_handler("Image"))
    sync.set_change_handler('*', _make_change_handler("File"))
    
    click.echo(f"Scanning: {source_dir}")
    click.echo(f"Pattern: {glob_pattern}")
    click.echo(f"Target cache: {cache_root}/key-{dir_key}/")
    click.echo("")
    
    result = await sync.sync(max_files=max_files)
    
    click.echo("")
    click.echo(f"Sync complete!")
    click.echo(f"  Inserted: {result.insert_count}")
    click.echo(f"  Updated: {result.update_count}")
    click.echo(f"  Deleted: {result.delete_count}")
    if result.failures:
        click.echo(f"  Failed: {len(result.failures)}")
    click.echo(f"  Total files scanned: {result.total_files_scanned}")
    click.echo("")
    click.echo(f"Cached to: {cache_root}/key-{dir_key}/")


@click.command()
@click.option('--cache-root', required=True, help='Root directory for the CachedFileFolders cache')
@click.option('--dir-key', required=True, help='Arbitrary key for organizing files in the cache')
@click.option('--source-dir', required=True, help='Local directory to scan for files')
@click.option('--glob-pattern', default='**/*', help='Glob pattern for file selection (default: **/*)')
@click.option('--max-files', type=int, help='Maximum number of files to process before exiting')
@click.option('--follow-symlinks', is_flag=True, help='Follow symbolic links when scanning')
def main(cache_root: str, dir_key: str, source_dir: str, glob_pattern: str, max_files: Optional[int], follow_symlinks: bool):
    """
    Local File Tree Sync - Cache Design Patterns Example
    
    This tool demonstrates core patterns for building intelligent caches using local files:
    
    Design Patterns Demonstrated:
    - FileProxy abstraction for unified data source access
    - CachedFileFolders for automatic change detection and file organization
    - Async processing for efficient file handling
    - Bulk synchronization with simple one-call processing
    - Type-specific change handlers for business logic
    
    This is a simplified version of the SharePoint example, showing how the same
    patterns work for any data source through the FileProxy interface.
    
    Use --max-files with a small number for testing before running on large directories.
    """
    
    source_path = Path(source_dir).expanduser()
    if not source_path.exists():
        click.echo(f"Error: Source directory does not exist: {source_dir}", err=True)
        sys.exit(1)
    if not source_path.is_dir():
        click.echo(f"Error: Source path is not a directory: {source_dir}", err=True)
        sys.exit(1)
    
    asyncio.run(sync_local_folder(
        cache_root=cache_root,
        dir_key=dir_key,
        source_dir=str(source_path),
        glob_pattern=glob_pattern,
        max_files=max_files,
        follow_symlinks=follow_symlinks
    ))

if __name__ == "__main__":
    main()

