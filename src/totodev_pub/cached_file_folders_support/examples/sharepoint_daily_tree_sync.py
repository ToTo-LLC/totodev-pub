#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SharePoint Daily Tree Sync - Cache Design Patterns Example

This example demonstrates core design patterns for building intelligent caches against 
external data sources using the CachedFileFolders library. It shows how to:

1. Abstract external data sources behind a unified FileProxy interface
2. Build production-ready sync tools with automatic change detection
3. Handle authentication, error recovery, and concurrent processing
4. Organize cached data with flexible grouping patterns

## REQUIREMENTS

### Python Path Setup
This script requires that the `totodev_pub` package be available in the Python path.
This is typically accomplished by mounting the totodev_pub repo in your project at
`src/totodev_pub` as a submodule and including `src/` in your PYTHONPATH.
Ensure that the `src/` directory is in your PYTHONPATH or run the script from within the `src/` directory.

### Environment Variables
Before running this script, you MUST set Sharepoint environment variables:

Example:
    export SHAREPOINT_CLIENT_ID="your-client-id"
    export SHAREPOINT_CLIENT_SECRET="your-client-secret"
    export SHAREPOINT_TENANT_ID="your-tenant-id"
    export SHAREPOINT_SITE_NAME="your-site-name"
    export SHAREPOINT_DOMAIN="yourcompany.sharepoint.com"
    export SHAREPOINT_DRIVE_ID="your-drive-id"

## Learning Objectives

This script demonstrates core design patterns for building intelligent caches against external data sources:

### External Data Source Integration
- **FileProxy Pattern**: Abstracting different data sources (SharePoint, HTTP, local files) behind a unified interface
- **Authentication Management**: Handling OAuth2 flows and token refresh for external APIs
- **Data Source Discovery**: Enumerating remote file hierarchies and metadata

### Intelligent Cache Architecture
- **Automatic Change Detection**: CachedFileFolders handles INSERT/UPDATE/DELETE without manual diff logic
- **Concurrent Processing**: Async patterns for 3-20x performance gains over sequential downloads
- **File Organization**: Flexible grouping patterns for organizing cached data by project, environment, or category
- **Retention Policies**: Predictable file retention with safety windows for accessing old versions

### Production-Ready Sync Patterns
- **Bulk Synchronization**: Simple one-call synchronization with automatic change detection
- **Error Handling**: Graceful degradation and retry logic for network failures
- **Progress Tracking**: Batch processing with comprehensive result reporting

## Usage

```bash
python sharepoint_daily_tree_sync.py \\
    --cache-root volatile/sp_sync/ \\
    --dir-key my-project \\
    --target-folder "Documents/Projects/2024"
```

Note: The script uses the configured drive ID directly rather than auto-discovering it.

## Command Line Arguments

- `--cache-root`: Root directory for the CachedFileFolders cache (required)
- `--dir-key`: Arbitrary key for organizing files in the cache (required)
- `--target-folder`: SharePoint folder path to sync from (required). Use "" or "/" for root directory
- `--max-files`: Maximum number of files to process before exiting (optional)
- `--debug`: Enable debug logging from external libraries

## File Organization

Files are organized in the cache using the pattern: `primary_group-{dir_key}/`
The `--dir-key` parameter controls which subdirectory is used within this pattern.
For example, with `--dir-key my-project`, files will be cached under `primary_group-my-project/`

Note: The grouping_key passed to CachedFileFolders is `[dir_key]` to match this pattern.

## Examples

Sync example (with limits on file count for testing)
```bash
python sharepoint_daily_tree_sync.py \\
    --cache-root volatile/sp_sync/ \\
    --dir-key verification \\
    --target-folder "Documents/Reports" \\
    --max-files 5
```


## Integration Notes

This tool demonstrates a clean architecture built on:
- `SimpleSharepointSync`: Encapsulates SharePoint complexity and provides clean API
- `SharepointFileProxyFactory`: For discovering and accessing SharePoint files
- `CachedFileFolders`: For intelligent caching and change detection
- Microsoft Graph API: For SharePoint integration
- MSAL: For Azure AD authentication

The `SimpleSharepointSync` class can be extracted and reused in other projects,
demonstrating how production sync tools might be structured. The tool is designed 
to be run regularly (e.g., via cron) for automated synchronization workflows.
"""

import sys
import os
import asyncio
import logging
from typing import List, Optional, Callable, Union, Sequence
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass

import click
from msal import ConfidentialClientApplication
import requests

# The below pattern is just an example, could be anything
# see CachedFileFolders documentation for more details
DUMMY_GROUPING_PATTERN = "key-{dir_key}/"

# Configure logging levels for external libraries
def configure_logging(debug_enabled: bool = False): # for external libraries
    for logger_name in ['msal', 'urllib3', 'requests', 'asyncio', 'totodev_pub.cached_file_folders_support.file_proxy_sharepoint']:
        logging.getLogger(logger_name).setLevel(logging.DEBUG if debug_enabled else logging.WARNING)


from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import SharepointFileProxyFactory
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from totodev_pub.cached_file_folders_support import ChangeNotice, ChangeType
from totodev_pub.cached_file_folders_support.sync_types import UpsertFailure, ResyncBulkResult


# =============================================================================
# SIMPLE SHAREPOINT SYNC CLASS
# =============================================================================

@dataclass
class SyncResult:
    """Simple result object containing sync statistics and changes."""
    insert_count: int
    update_count: int
    delete_count: int
    changes: List[ChangeNotice]
    failures: List[UpsertFailure]
    total_files_scanned: int

class SimpleSharepointSync:
    """Simplified SharePoint synchronization class for tutorial purposes.
    
    This class encapsulates the SharePoint authentication and caching logic,
    allowing users to focus on business logic through change handlers.
    
    This is a simplified version of what might be created in a production
    environment - it demonstrates the core pattern without complexity.
    """

    ChangeEventHandler = Callable[[ChangeNotice, Optional[FileProxyBase]], None]
    
    def __init__(self, cache: CachedFileFolders, grouping_key: str, config: dict, target_folder: str = "root"):
        """Initialize the sync instance with a CachedFileFolders object and grouping key.
        
        Args:
            cache: CachedFileFolders instance to use for synchronization
            grouping_key: Key to use when storing files in the cache
            config: SharePoint configuration dictionary
            target_folder: SharePoint folder path to sync from
        """
        self.cache = cache
        self.grouping_key = grouping_key
        self.config = config
        self.target_folder = target_folder

        # Unified change handlers - maps file extension to callable action
        # Use '*' as wildcard for fallback handler
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
            handler: Function that receives a ChangeNotice object and the originating FileProxy (if available)
        """
        if isinstance(file_extension, str):
            file_extension = [file_extension.lower()]
        for ext in file_extension:
            self._handlers[ext] = handler

    
    async def sync(self, max_files: Optional[int] = None) -> SyncResult:
        # Get SharePoint credentials and site information
        access_token = get_access_token(self.config)
        site_id, drive_id, drive_name = get_site_info(access_token, self.config)
        
        # Create factory to generate file proxies for SharePoint files
        factory = SharepointFileProxyFactory(
            site_id=site_id,
            drive_id=drive_id,
            access_token=access_token,
            site_name=self.config['sharepoint_site_name'],
            drive_name=drive_name
        )
        
        # Use "root" to scan entire site, or specific folder path
        scan_path = "root" if self.target_folder in ["", "/", "root"] else self.target_folder
        
        # Create an iterator of SharePoint file proxies for bulk synchronization
        def file_proxy_iterator():
            files_processed = 0
            for proxy in factory.scan_files(
                folder_path=scan_path,
                file_extensions=None,  # Include all file types
                include_subfolders=True
            ):
                if max_files is not None and files_processed >= max_files:
                    break
                
                # Skip folder proxies (end with /) - we only want actual files
                if proxy.ref_path().endswith('/'):
                    continue
                
                files_processed += 1
                yield proxy
        
        # Use the simpler resync_bulk method for synchronization
        def _change_receiver(change: ChangeNotice, proxy: Optional[FileProxyBase]) -> None:
            suffix = ""
            if change.change_type == ChangeType.DELETE and change.old is not None:
                suffix = change.old.file_path.suffix
            elif change.cur is not None:
                suffix = change.cur.file_path.suffix

            suffix = (suffix or "").lower()
            handler = self._handlers.get(suffix) or self._handlers.get('*')

            if handler:
                handler(change, proxy)

        resync_result: ResyncBulkResult = await self.cache.resync_bulk(
            file_proxies=file_proxy_iterator(),
            grouping_key=[self.grouping_key],
            upsert_fail_policy="RETAIN_OLD",
            max_concurrent_requests=5,
            change_receiver=_change_receiver
        )
        
        # Count changes and call user handlers if provided
        stats = {'insert': 0, 'update': 0, 'delete': 0}
        for change in resync_result.changes:
            change_type = change.change_type.value.lower()
            stats[change_type] += 1
        
        return SyncResult(
            insert_count=stats['insert'],
            update_count=stats['update'], 
            delete_count=stats['delete'],
            changes=resync_result.changes,
            failures=resync_result.failures,
            total_files_scanned=len(resync_result.changes) + len(resync_result.failures)
        )


# =============================================================================
# ENVIRONMENT VALIDATION
# =============================================================================

def validate_environment() -> dict:
    # Check for required SharePoint environment variables
    required = ['SHAREPOINT_CLIENT_ID', 'SHAREPOINT_CLIENT_SECRET', 'SHAREPOINT_TENANT_ID', 
                'SHAREPOINT_SITE_NAME', 'SHAREPOINT_DOMAIN', 'SHAREPOINT_DRIVE_ID']
    
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        click.echo(f"❌ Missing environment variables: {', '.join(missing)}", err=True)
        sys.exit(1)
    
    return {var.lower(): os.getenv(var) for var in required}

# =============================================================================
# SHAREPOINT AUTHENTICATION
# =============================================================================

def get_access_token(config: dict) -> str:
    """Demonstrate Azure AD client credentials flow for SharePoint access.
    
    This shows how to authenticate as an application (not a user) to access SharePoint.
    The client credentials flow is ideal for automated scripts like this sync tool.
    
    Key concepts:
    - MSAL ConfidentialClientApplication for app-only authentication
    - The scope "https://graph.microsoft.com/.default" grants broad Graph API access
    - No user interaction required - perfect for automated workflows
    """
    app = ConfidentialClientApplication(
        config['sharepoint_client_id'], 
        authority=f"https://login.microsoftonline.com/{config['sharepoint_tenant_id']}", 
        client_credential=config['sharepoint_client_secret']
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    
    if "access_token" not in result:
        error_msg = result.get('error_description', 'Unknown authentication error')
        raise RuntimeError(f"Failed to get access token: {error_msg}")
    
    return result["access_token"]

def get_site_info(access_token: str, config: dict) -> tuple[str, str, Optional[str]]:
    """Show how to locate SharePoint sites and document libraries.
    
    This demonstrates the Microsoft Graph API pattern for finding SharePoint resources:
    1. Sites are identified by domain and site name: sites/{domain}:/sites/{site_name}
    2. Document libraries (drives) have their own IDs that don't change
    3. We use the configured drive ID directly rather than auto-discovery
    
    Interesting aspects:
    - SharePoint sites vs. document libraries (drives) are separate concepts
    - The URL pattern reveals SharePoint's internal organization
    - Drive IDs are stable identifiers that don't change like site URLs might
    """
    headers = {'Authorization': f'Bearer {access_token}'}
    
    # Get site
    site_url = f"https://graph.microsoft.com/v1.0/sites/{config['sharepoint_domain']}:/sites/{config['sharepoint_site_name']}"
    response = requests.get(site_url, headers=headers)
    response.raise_for_status()
    site_data = response.json()
    site_id = site_data['id']

    drive_id = config['sharepoint_drive_id']

    # Attempt to fetch drive metadata to enrich downstream processing
    drive_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
    drive_name: Optional[str]
    try:
        drive_response = requests.get(drive_url, headers=headers, timeout=10)
        drive_response.raise_for_status()
        drive_data = drive_response.json()
        drive_name = drive_data.get('name')
    except requests.HTTPError as exc:
        logging.getLogger(__name__).warning(
            "Unable to fetch SharePoint drive metadata (HTTP %s): %s",
            getattr(exc.response, "status_code", "unknown"),
            exc,
        )
        drive_name = "metadata-unavailable:insufficient-permissions"
    except requests.RequestException as exc:
        logging.getLogger(__name__).warning(
            "Unable to fetch SharePoint drive metadata: %s", exc
        )
        drive_name = "metadata-unavailable:error"
    
    return site_id, drive_id, drive_name




# =============================================================================
# MAIN SYNC FUNCTION
# =============================================================================

async def sync_sharepoint_folder(
    cache_root: str,
    dir_key: str, 
    target_folder: str,
    max_files: Optional[int],
    config: dict
) -> None:
    """Demonstrate the complete SharePoint sync workflow using SimpleSharepointSync.
    
    This function shows how the SimpleSharepointSync class encapsulates all the complexity
    of SharePoint authentication, file discovery, and caching, while allowing business
    logic to be handled through clean callback functions.
    
    Key learning concepts:
    1. Class-based encapsulation: Complex infrastructure hidden behind simple interface
    2. Callback pattern: Business logic separated from infrastructure concerns  
    3. Clean separation: Sync logic vs. document processing logic are distinct
    4. Reusability: The SimpleSharepointSync class can be used in other contexts
    5. Per-file-type handlers: Different processing for different file types
    
    This demonstrates how production code might be structured - infrastructure classes
    that handle the hard parts, with business logic injected via callbacks.
    """
    
    # Create the cache instance - this handles file organization and change detection
    cache = CachedFileFolders(
        grouping_pattern=DUMMY_GROUPING_PATTERN,
        root_dir=os.path.abspath(cache_root),
        use_xxhash=False
    )
    
    # Create the sync instance - this encapsulates all the SharePoint complexity
    sync = SimpleSharepointSync(
        cache=cache,
        grouping_key=dir_key,
        config=config,
        target_folder=target_folder
    )
    
    # Define business logic handlers for different file types
    def handle_document_change(change: ChangeNotice, proxy: Optional[FileProxyBase]) -> None:
        """Handle document file changes (Word, PDF, etc.) - simple logging and slave directory usage."""
        change_type = change.change_type.value.lower()
        
        if change_type in ['insert', 'update']:
            metadata = change.metadata()
            if metadata is not None:
                sharepoint_info = {
                    'site_id': proxy.site_id if proxy is not None else None,
                    'site_name': proxy.site_name if proxy is not None else config['sharepoint_site_name'],
                    'site_domain': config['sharepoint_domain'],
                    'drive_id': proxy.drive_id if proxy is not None else None,
                    'drive_name': proxy.drive_name if proxy is not None else None,
                    'file_path': proxy.file_path if proxy is not None else None,
                    'ref_path': change.ref_path,
                    'target_folder': target_folder
                }
                metadata.overwrite_source_file({
                    'file_name': change.cur.file_path.name,
                    'processed_at': datetime.now().isoformat(),
                    'change_type': change.change_type.value.lower(),
                    'category': 'Document',
                    'sharepoint': sharepoint_info
                })
            
            click.echo(f"📄 Document processed: {change.cur.file_path.name}")
        elif change_type == 'delete':
            click.echo(f"🗑️  Document deleted: {change.old.file_path.name if change.old else 'unknown'}")
   
    def handle_other_change(change: ChangeNotice, proxy: Optional[FileProxyBase]) -> None:
        """Handle all other file types - simple logging and slave directory usage."""
        change_type = change.change_type.value.lower()
        
        if change_type in ['insert', 'update']:
            metadata = change.metadata()
            if metadata is not None:
                sharepoint_info = {
                    'site_id': proxy.site_id if proxy is not None else None,
                    'site_name': proxy.site_name if proxy is not None else config['sharepoint_site_name'],
                    'site_domain': config['sharepoint_domain'],
                    'drive_id': proxy.drive_id if proxy is not None else None,
                    'drive_name': proxy.drive_name if proxy is not None else None,
                    'file_path': proxy.file_path if proxy is not None else None,
                    'ref_path': change.ref_path,
                    'target_folder': target_folder
                }
                metadata.overwrite_source_file({
                    'file_name': change.cur.file_path.name,
                    'processed_at': datetime.now().isoformat(),
                    'change_type': change.change_type.value.lower(),
                    'category': 'Other',
                    'sharepoint': sharepoint_info
                })
            
            click.echo(f"📄 File processed: {change.cur.file_path.name}")
        elif change_type == 'delete':
            click.echo(f"🗑️  File deleted: {change.old.file_path.name if change.old else 'unknown'}")
    
    # Register handlers for specific file types
    sync.set_change_handler(['.docx', '.doc', '.pdf', '.rtf'], handle_document_change)
    sync.set_change_handler('*', handle_other_change) # handle all other file types
    
    # Execute the sync - all the SharePoint complexity is hidden
    result = await sync.sync(max_files=max_files)
    
    # Display results
    click.echo(f"Sync complete: {result.insert_count} inserted, {result.update_count} updated, {result.delete_count} deleted")
    if result.failures:
        click.echo(f"⚠️  {len(result.failures)} downloads failed")
    click.echo(f"📁 Cached to: {cache_root}/key-{dir_key}/")

# =============================================================================
# CLI INTERFACE
# =============================================================================

@click.command()
@click.option('--cache-root', help='Root directory for the CachedFileFolders cache')
@click.option('--dir-key', help='Arbitrary key for organizing files in the cache')
@click.option('--target-folder', required=True, help='SharePoint folder path to sync from')
@click.option('--max-files', type=int, help='Maximum number of files to process before exiting')
@click.option('--debug', is_flag=True, help='Enable debug logging from external libraries')
def main(cache_root: Optional[str], dir_key: Optional[str], target_folder: str, max_files: Optional[int], debug: bool):
    """
    SharePoint Daily Tree Sync - Cache Design Patterns Example
    
    This tool demonstrates core patterns for building intelligent caches against external data sources:
    
    Design Patterns Demonstrated:
    - FileProxy abstraction for unified data source access
    - CachedFileFolders for automatic change detection and file organization
    - Async processing for concurrent downloads and 3-20x performance gains
    - Authentication management with OAuth2 token refresh
    - Bulk synchronization with simple one-call processing
    
    Use --max-files with a small number for testing before running on large folders.
    """
    
    configure_logging(debug)
    config = validate_environment()
    
    cache_root_path = Path(cache_root)
    # No explicit cache_root_path existence or type checks; let exceptions fall through.
    
    asyncio.run(sync_sharepoint_folder(
        cache_root=cache_root,
        dir_key=dir_key,
        target_folder=target_folder,
        max_files=max_files,
        config=config
    ))

if __name__ == "__main__":
    main()
