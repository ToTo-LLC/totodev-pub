# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SharePoint file proxy implementations for Microsoft Graph API integration.

This module provides two main classes for working with SharePoint files:

1. **SharepointFileProxy**: A proxy for individual SharePoint files
   - Represents a single file on a SharePoint server
   - Handles lazy downloading via Microsoft Graph API
   - Useful when you know the exact file path and want to work with a specific file

2. **SharepointFileProxyFactory**: A factory for discovering and creating file proxies
   - **This is the more powerful and commonly used class**
   - Scans SharePoint drives to discover files matching your criteria
   - Creates multiple SharepointFileProxy objects automatically
   - Provides extensive filtering and search capabilities

## Credentials and Authentication

To use this library, register an Azure AD app with Microsoft Graph API permissions, grant admin consent, then use your tenant ID, client ID, and client secret to obtain a Microsoft Graph access token via OAuth 2.0 client credentials flow; pass this token as `accesstoken` when initializing classes.

## Quick Start with SharepointFileProxyFactory

The factory is designed for most use cases where you want to work with multiple SharePoint files:

```python
# Initialize factory with your SharePoint credentials
factory = SharepointFileProxyFactory(
    site_id="your-sharepoint-site-id",
    drive_id="your-document-library-id", 
    access_token="your-microsoft-graph-access-token"
)

# Basic usage: scan all document files
for proxy in factory.scan_files():
    print(f"Found: {proxy.file_path}")
    # Proxy is ready for materialize() and deploy()
```

## Advanced Scanning with Filters

The `scan_files()` method is the factory's most powerful feature, offering extensive filtering:

### File Type Filtering
```python
# Only PDF files
pdf_files = factory.scan_files(file_extensions={'.pdf'})

# Multiple document types
docs = factory.scan_files(file_extensions={'.pdf', '.docx', '.xlsx'})

# All files (no extension filter)
all_files = factory.scan_files(file_extensions=None)
```

### Size-Based Filtering
```python
# Large files (> 10MB)
large_files = factory.scan_files(min_size_bytes=10*1024*1024)

# Small files (< 1MB) 
small_files = factory.scan_files(max_size_bytes=1024*1024)

# Files between 1MB and 10MB
medium_files = factory.scan_files(
    min_size_bytes=1024*1024,
    max_size_bytes=10*1024*1024
)
```

### Date-Based Filtering
```python
from datetime import datetime, timedelta

# Files modified in the last 7 days
recent = factory.scan_files(
    modified_after=datetime.now() - timedelta(days=7)
)

# Files modified in a specific date range
date_range = factory.scan_files(
    modified_after=datetime(2024, 1, 1),
    modified_before=datetime(2024, 12, 31)
)
```

### Name Pattern Matching
```python
# Files with "report" in the name
reports = factory.scan_files(name_pattern="*report*")

# Files starting with "Q1"
quarterly = factory.scan_files(name_pattern="Q1*")

# Specific filename pattern
specific = factory.scan_files(name_pattern="*_final_*.pdf")
```

### Folder-Specific Scanning
```python
# Scan only root folder (non-recursive)
root_files = factory.scan_files(
    folder_path="root",
    include_subfolders=False
)

# Scan specific folder recursively
project_files = factory.scan_files(
    folder_path="Documents/Projects/2024",
    include_subfolders=True
)
```

### Performance and Memory Management
```python
# Limit results for testing or performance
limited = factory.scan_files(max_files=100)

# Process in batches for memory efficiency
for batch in factory.scan_files_batched(batch_size=20):
    for proxy in batch:
        # Process each file in the batch
        pass

# Skip metadata for faster scanning (less functionality)
fast_scan = factory.scan_files(include_metadata=False)
```

### Progress Reporting
```python
def progress_callback(processed, total):
    print(f"Progress: {processed}/{total} ({processed/total*100:.1f}%)")

# Scan with progress updates
for proxy in factory.scan_files(progress_callback=progress_callback):
    # Process files with progress feedback
    pass
```

## Counting Files

For cases where you only need a count without creating proxy objects:

```python
# Count all files
count = factory.count_files()

# Count recent files (last 7 days)
recent_count = factory.count_files(modified_after=datetime.now() - timedelta(days=7))

# Count large PDF files
large_pdf_count = factory.count_files(
    file_extensions={'.pdf'},
    min_size_bytes=10*1024*1024
)
```

## Combining Filters

You can combine multiple filters for precise results:

```python
# Recent large PDF files in a specific folder
target_files = factory.scan_files(
    folder_path="Documents/Reports",
    file_extensions={'.pdf'},
    modified_after=datetime.now() - timedelta(days=30),
    min_size_bytes=1024*1024,  # > 1MB
    name_pattern="*quarterly*",
    max_files=50
)
```

## Error Handling and Robustness

The factory includes robust error handling:

```python
# Continue on individual file errors (default behavior)
robust_scan = factory.scan_files(skip_errors=True)

# Stop on first error for debugging
strict_scan = factory.scan_files(skip_errors=False)

# Custom logging
import logging
logger = logging.getLogger(__name__)
detailed_scan = factory.scan_files(logger=logger)
```

## Integration with CachedFileFolders

The factory integrates seamlessly with the CachedFileFolders system:

```python
from totodev_pub.cached_file_folders import CachedFileFolders

# Create cache
cache = CachedFileFolders("sharepoint_cache/", "/tmp/cache")

# Scan and cache SharePoint files
for proxy in factory.scan_files(file_extensions={'.pdf'}):
    change_notice = await cache.upsert_file(proxy, ["sharepoint", "documents"])
    if change_notice:
        print(f"Cached: {change_notice.file_path}")
```

## When to Use Each Class

- **Use SharepointFileProxyFactory** when you want to:
  - Discover files on SharePoint
  - Work with multiple files
  - Filter files by various criteria
  - Batch process files
  - Build file discovery workflows

- **Use SharepointFileProxy** when you:
  - Know the exact file path
  - Want to work with a single specific file
  - Need direct control over individual file operations
  - Are building custom file handling logic

The factory's `scan_files()` method is the recommended entry point for most SharePoint file operations, as it provides extensive filtering capabilities and flexibility.

## File Discovery and Directory Scanning

For common project needs, the factory also provides two specialized methods:

1. **`find_file_by_name()`**: Locate a specific file by filename across the entire SharePoint site
   - Useful when you know a filename but not its location
   - Returns the full path or None if not found
   - Handles case-insensitive matching and searches all subdirectories

2. **`scan_directory()`**: Recursively scan a specific directory and yield all files
   - Convenience method that wraps `scan_files()` with directory-specific parameters
   - Identical return structure to `scan_files()` for consistency
   - Useful for processing all files within a known directory structure

## Testability and Mocking

Both classes are designed for easy testing with mocked API calls:

- **SharepointFileProxyFactory**: API calls are isolated in `_api_get_folder_contents()`
- **SharepointFileProxy**: API calls are isolated in `_api_download_file_content()`

These private methods can be easily mocked or patched in unit tests to avoid external dependencies while testing business logic.
"""

from typing import Optional, Dict, Any, Generator, List, Set, Callable, Union
from datetime import datetime
from pathlib import Path
import shutil
import os
import tempfile
import asyncio
import aiohttp
import aiofiles
import requests
import fnmatch
import logging

from .file_proxy_base import FileProxyBase


class _SharePointGraphApiClient:
    """
    Internal API client for Microsoft Graph SharePoint operations.
    
    This class encapsulates all HTTP communication with SharePoint via Microsoft Graph API.
    Methods are public (no underscore) to enable easy mocking in tests.
    
    Note: This class is private to the module (underscore prefix) and should not be
    used directly by external code.
    """
    
    def __init__(
        self,
        site_id: str,
        drive_id: str,
        access_token: str,
        base_url: str = "https://graph.microsoft.com/v1.0"
    ):
        """
        Initialize the SharePoint Graph API client.
        
        Args:
            site_id: SharePoint site ID
            drive_id: SharePoint drive/document library ID
            access_token: Bearer token for authentication
            base_url: Graph API base URL
        """
        self.site_id = site_id
        self.drive_id = drive_id
        self.access_token = access_token
        self.base_url = base_url.rstrip('/')
    
    def get_folder_contents(self, folder_path: str) -> Dict[str, Any]:
        """
        Get contents of a SharePoint folder (sync version).
        
        Args:
            folder_path: Folder path or ID (e.g., "root", "Documents/Projects", or folder ID)
            
        Returns:
            Dict with structure: {"value": [list of items]}
            Each item contains:
                - id: Item identifier
                - name: Item name
                - folder: Present for folders
                - file: Present for files
                - size: File size in bytes (files only)
                - lastModifiedDateTime: ISO 8601 timestamp
                - webUrl: Web URL to the item
                - @microsoft.graph.downloadUrl: Direct download URL (files only)
                - mimeType: MIME type (files only)
            
        Raises:
            RuntimeError: For HTTP errors (403, 404, etc.)
            requests.exceptions.RequestException: For network errors
        """
        headers = {'Authorization': f'Bearer {self.access_token}'}
        
        # Use intelligent detection to choose the right endpoint first
        # 1) Root
        if folder_path == "root":
            url = f"{self.base_url}/drives/{self.drive_id}/root/children"
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP error {response.status_code}: {response.text}")
            return response.json()
        
        # 2) If it looks like a folder ID, try items/{id}/children first
        if self._is_likely_folder_id(folder_path):
            items_url = f"{self.base_url}/drives/{self.drive_id}/items/{folder_path}/children"
            response = requests.get(items_url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
            # If ID-based fails, fall back to path-based
            if response.status_code not in (400, 404):
                raise RuntimeError(f"HTTP error {response.status_code}: {response.text}")
        
        # 3) Try path-based addressing (for human names or when ID-based failed)
        safe_path = folder_path.lstrip('/')
        if safe_path:
            path_url = f"{self.base_url}/drives/{self.drive_id}/root:/{safe_path}:/children"
            response = requests.get(path_url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
            # Only fall back for expected path errors; other statuses still raise
            if response.status_code not in (400, 404):
                raise RuntimeError(f"HTTP error {response.status_code}: {response.text}")
        
        # 4) Final fallback to items/{id}/children (if path-based failed)
        items_url = f"{self.base_url}/drives/{self.drive_id}/items/{folder_path}/children"
        response = requests.get(items_url, headers=headers, timeout=30)
        if response.status_code == 403:
            raise RuntimeError(f"Access denied to folder: {folder_path}")
        elif response.status_code == 404:
            raise RuntimeError(f"Folder not found: {folder_path}")
        elif response.status_code != 200:
            raise RuntimeError(f"HTTP error {response.status_code}: {response.text}")
        return response.json()
    
    def _is_likely_folder_id(self, folder_path: str) -> bool:
        """Check if folder_path looks like a SharePoint folder ID rather than a human name."""
        if not folder_path or len(folder_path) < 20:
            return False
        
        # SharePoint IDs are typically 22-32 chars, alphanumeric, no spaces
        if len(folder_path) > 35 or ' ' in folder_path:
            return False
        
        # Check if it's mostly alphanumeric with minimal special chars
        alphanumeric_count = sum(1 for c in folder_path if c.isalnum())
        if alphanumeric_count < len(folder_path) * 0.8:  # 80% alphanumeric
            return False
        
        return True
    
    async def download_file_content(self, url: str, output_path: str) -> None:
        """
        Download file content from SharePoint (async version).
        
        Args:
            url: Direct download URL or Graph API content URL
            output_path: Local file path where content should be written
            
        Raises:
            RuntimeError: For HTTP errors (404, 401, 403, etc.)
            aiohttp.ClientError: For network errors
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/octet-stream"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    raise RuntimeError("File not found")
                elif response.status == 401:
                    raise RuntimeError("Authentication failed - invalid or expired access token")
                elif response.status == 403:
                    raise RuntimeError("Access denied - insufficient permissions to access this file")
                elif response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"HTTP error {response.status}: {error_text}")
                
                async with aiofiles.open(output_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        await f.write(chunk)
    
    async def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        """
        Get file metadata without downloading content (async version).
        
        Args:
            file_path: Path to file within drive (e.g., "Documents/file.pdf")
            
        Returns:
            Dict containing file metadata:
                - size: File size in bytes
                - lastModifiedDateTime: ISO 8601 timestamp
                - name: File name
                - id: File identifier
                - (other SharePoint file properties)
            
        Raises:
            RuntimeError: If file not found or API error
        """
        safe_path = file_path.lstrip('/')
        url = f"{self.base_url}/drives/{self.drive_id}/root:/{safe_path}"
        headers = {'Authorization': f'Bearer {self.access_token}'}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise RuntimeError(f"HTTP error {response.status}: {error_text}")


class SharepointFileProxyFactory:
    """
    A factory class for creating SharepointFileProxy objects for a specific SharePoint site.
    
    This class encapsulates the site configuration and provides methods for creating file proxies
    and scanning for files within that site.
    """
    
    def __init__(
        self,
        site_id: str,
        drive_id: str,
        access_token: str,
        base_url: str = "https://graph.microsoft.com/v1.0",
        site_name: Optional[str] = None,
        drive_name: Optional[str] = None,
    ):
        """
        Initialize the factory with SharePoint site configuration.
        
        Args:
            site_id: The SharePoint site ID (e.g., from sites/{site_id})
            drive_id: The SharePoint drive ID (document library ID)
            access_token: Bearer token for Microsoft Graph API authentication
            base_url: Base URL for Microsoft Graph API (defaults to v1.0)
            site_name: Optional human-readable site name for semantic ref_paths (e.g., "MCPolicies")
        """
        self.site_id = site_id
        self.drive_id = drive_id
        self.access_token = access_token
        self.base_url = base_url
        self.site_name = site_name
        self.drive_name = drive_name
        
        # Create API client for all Graph API operations
        self._api_client = _SharePointGraphApiClient(
            site_id=site_id,
            drive_id=drive_id,
            access_token=access_token,
            base_url=base_url
        )
    
    def create(
        self, 
        file_path: str, 
        file_size: Optional[int] = None, 
        last_modified: Optional[datetime] = None,
        download_url: str = ""
    ) -> "SharepointFileProxy":
        """
        Create a SharepointFileProxy for a specific file.
        
        Args:
            file_path: The path to the file within the SharePoint drive (relative to root)
            file_size: Optional file size in bytes (for optimization and validation)
            last_modified: Optional last modified timestamp (for comparison)
            download_url: Optional direct download URL from SharePoint API
            
        Returns:
            A SharepointFileProxy object configured for the specified file.
        """
        return SharepointFileProxy(
            site_id=self.site_id,
            drive_id=self.drive_id,
            file_path=file_path,
            access_token=self.access_token,
            file_size=file_size,
            last_modified=last_modified,
            base_url=self.base_url,
            site_name=self.site_name,
            drive_name=self.drive_name,
            download_url=download_url
        )
    
    def scan_files(
        self,
        folder_path: str = "root",
        file_extensions: Optional[Set[str]] = None,
        min_size_bytes: Optional[int] = None,
        max_size_bytes: Optional[int] = None,
        modified_after: Optional[datetime] = None,
        modified_before: Optional[datetime] = None,
        name_pattern: Optional[str] = None,
        include_subfolders: bool = True,
        **kwargs
    ) -> Generator["SharepointFileProxy", None, None]:
        """
        Generator that scans through SharePoint files and yields SharepointFileProxy objects.
        
        Args:
            folder_path: Starting folder path to scan (default: "root")
            file_extensions: Set of allowed file extensions (e.g., {'.pdf', '.docx'})
            min_size_bytes: Minimum file size in bytes
            max_size_bytes: Maximum file size in bytes
            modified_after: Only include files modified after this datetime
            modified_before: Only include files modified before this datetime
            name_pattern: Glob-style pattern for filename matching
            include_subfolders: Whether to scan subfolders recursively
            **kwargs: Additional options (include_metadata, skip_errors, progress_callback, logger)
            
        Yields:
            SharepointFileProxy: Configured proxy objects for matching files
        """
        # Extract kwargs with defaults
        include_metadata = kwargs.get('include_metadata', True)
        skip_errors = kwargs.get('skip_errors', True)
        progress_callback = kwargs.get('progress_callback')
        logger = kwargs.get('logger', logging.getLogger(__name__))
        prefetch_folders: int = max(0, int(kwargs.get('prefetch_folders', 1)))
            
        # Default file extensions for documents (from sync_sharepoint.py)
        if file_extensions is None:
            file_extensions = {'.doc', '.docx', '.pdf', '.txt', '.md'}
        
        # Track progress
        files_processed = 0
        files_yielded = 0
        
        try:
            # Scan files lazily - yield as we find them
            for file_info in self._scan_files_lazy(
                folder_path, include_subfolders, logger, "",
                file_extensions, min_size_bytes, max_size_bytes,
                modified_after, modified_before, name_pattern, include_metadata,
                skip_errors, progress_callback, prefetch_folders
            ):
                files_processed += 1
                
                try:
                    # Files are already filtered by _scan_files_lazy
                    
                    # Parse modification date if metadata is included
                    last_modified = None
                    if include_metadata and file_info.get('modified'):
                        try:
                            last_modified = datetime.fromisoformat(
                                file_info['modified'].replace('Z', '+00:00')
                            )
                        except (ValueError, TypeError):
                            logger.warning(f"Could not parse date for {file_info['name']}: {file_info.get('modified')}")
                    
                    # Create and yield the proxy
                    proxy = SharepointFileProxy(
                        site_id=self.site_id,
                        drive_id=self.drive_id,
                        file_path=file_info['path'],
                        access_token=self.access_token,
                        file_size=file_info.get('size') if include_metadata else None,
                        last_modified=last_modified,
                        base_url=self.base_url,
                        site_name=self.site_name,
                        drive_name=self.drive_name,
                        download_url=file_info.get('downloadUrl', '')
                    )
                    
                    yield proxy
                    files_yielded += 1
                        
                except Exception as e:
                    if skip_errors:
                        logger.warning(f"Error processing file {file_info.get('name', 'unknown')}: {e}")
                        continue
                    else:
                        raise
                
                # Update progress (we don't know total with lazy scanning)
                if progress_callback:
                    progress_callback(files_processed, 0)  # 0 means unknown total
            
            logger.info(f"Scan completed: {files_processed} files processed, {files_yielded} files yielded")
            
        except Exception as e:
            logger.error(f"Error during SharePoint scan: {e}")
            raise RuntimeError(f"SharePoint scan failed: {e}")
    
    def scan_files_batched(
        self,
        batch_size: int = 100,
        **scan_kwargs
    ) -> Generator[List["SharepointFileProxy"], None, None]:
        """
        Generator that yields batches of SharepointFileProxy objects.
        
        Args:
            batch_size: Number of files to include in each batch
            **scan_kwargs: All arguments supported by scan_files()
            
        Yields:
            List[SharepointFileProxy]: Batches of proxy objects
        """
        batch = []
        for proxy in self.scan_files(**scan_kwargs):
            batch.append(proxy)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        
        if batch:
            yield batch
    
    
    def _scan_files_lazy(
        self,
        folder_path: str,
        include_subfolders: bool,
        logger: logging.Logger,
        current_path: str,
        file_extensions: Set[str],
        min_size_bytes: Optional[int],
        max_size_bytes: Optional[int],
        modified_after: Optional[datetime],
        modified_before: Optional[datetime],
        name_pattern: Optional[str],
        include_metadata: bool,
        skip_errors: bool,
        progress_callback: Optional[Callable[[int, int], None]],
        prefetch_folders: int
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Breadth-first, files-first lazy scanner.
        - Yields file items in the current folder before descending into subfolders
        - Traverses folders using a FIFO queue to reduce time-to-first-yield
        """
        # Track consecutive errors for systematic failure detection
        if not hasattr(self, '_consecutive_errors'):
            self._consecutive_errors = {}

        # Queue contains tuples of (folder_id_or_path, display_path, future_or_none)
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor, Future
        queue = deque()
        queue.append((folder_path, current_path, None))

        executor: Optional[ThreadPoolExecutor] = None
        in_flight: int = 0
        if prefetch_folders > 0:
            executor = ThreadPoolExecutor(max_workers=max(1, prefetch_folders))

        try:
            while queue:
                next_folder, display_path, fut = queue.popleft()
                try:
                    # Obtain items for this folder (use prefetched future if available)
                    if fut is not None:
                        try:
                            items_data = fut.result()
                        finally:
                            # Decrement in-flight regardless of success/failure
                            in_flight = max(0, in_flight - 1)
                    else:
                        if executor is not None and in_flight < prefetch_folders:
                            # Submit now to keep a unified path (optional)
                            fut_now = executor.submit(self._api_client.get_folder_contents, next_folder)
                            try:
                                items_data = fut_now.result()
                            finally:
                                in_flight = max(0, in_flight - 0)  # No pre-increment for immediate call
                        else:
                            # Synchronous fetch
                            items_data = self._api_client.get_folder_contents(next_folder)

                    items = items_data.get('value', [])
                    logger.debug(f"📁 Found {len(items)} items in folder")

                    # Reset error tracking on successful operation
                    if hasattr(self, '_consecutive_errors'):
                        self._consecutive_errors.clear()

                    # Partition into files vs folders
                    folders: List[Dict[str, Any]] = []
                    files: List[Dict[str, Any]] = []
                    for item in items:
                        if 'folder' in item:
                            folders.append(item)
                        else:
                            files.append(item)

                    # Yield files first
                    for item in files:
                        item_name = item['name']
                        item_path = f"{display_path}/{item_name}" if display_path else item_name
                        file_info = self._process_file_item(item, item_name, item_path)
                        if self._file_matches_criteria(
                            file_info, file_extensions, min_size_bytes, max_size_bytes,
                            modified_after, modified_before, name_pattern, include_metadata
                        ):
                            yield file_info

                    # Then enqueue folders (BFS) if requested
                    if include_subfolders:
                        for folder in folders:
                            folder_name = folder['name']
                            folder_display_path = f"{display_path}/{folder_name}" if display_path else folder_name
                            logger.debug(f"📂 Queueing subfolder: {folder_display_path}")
                            future_to_attach: Optional[Future] = None
                            if executor is not None and in_flight < prefetch_folders:
                                future_to_attach = executor.submit(self._api_client.get_folder_contents, folder['id'])
                                in_flight += 1
                            queue.append((folder['id'], folder_display_path, future_to_attach))
                    else:
                        for folder in folders:
                            folder_name = folder['name']
                            folder_display_path = f"{display_path}/{folder_name}" if display_path else folder_name
                            logger.debug(f"📂 Skipping folder (include_subfolders=False): {folder_display_path}")

                except requests.exceptions.Timeout:
                    error_msg = f"Timeout fetching folder contents: {next_folder}"
                    logger.error(f"⏰ {error_msg}")
                    self._track_consecutive_error(error_msg, logger)
                except Exception as e:
                    error_msg = f"Error processing folder {next_folder}: {str(e)}"
                    logger.error(f"❌ {error_msg}")
                    self._track_consecutive_error(error_msg, logger)
        finally:
            if executor is not None:
                # Do not wait; cancel pending
                executor.shutdown(wait=False, cancel_futures=True)
    
    def _track_consecutive_error(self, error_msg: str, logger: logging.Logger) -> None:
        """
        Track consecutive errors and stop if the same error repeats too many times.
        
        Args:
            error_msg: The error message to track
            logger: Logger for error reporting
        """
        # Initialize error tracking if not exists
        if not hasattr(self, '_consecutive_errors'):
            self._consecutive_errors = {}
        
        # Track this error
        if error_msg not in self._consecutive_errors:
            self._consecutive_errors[error_msg] = 0
        
        self._consecutive_errors[error_msg] += 1
        
        # Check if we've hit the threshold for systematic failure
        if self._consecutive_errors[error_msg] >= 5:
            logger.error(f"🛑 SYSTEMATIC FAILURE DETECTED: Same error repeated {self._consecutive_errors[error_msg]} times")
            logger.error(f"🛑 Error: {error_msg}")
            logger.error("🛑 Stopping scan to prevent infinite error loops")
            raise RuntimeError(f"Systematic failure detected: {error_msg} (repeated {self._consecutive_errors[error_msg]} times)")
        
        # Reset other error counts (only track the most recent error type)
        for other_error in list(self._consecutive_errors.keys()):
            if other_error != error_msg:
                del self._consecutive_errors[other_error]
    
    def _process_file_item(self, item: Dict[str, Any], item_name: str, item_path: str) -> Dict[str, Any]:
        # Extract and normalize file metadata from SharePoint API response
        return {
            'id': item['id'],
            'name': item_name,
            'path': item_path,
            'size': item.get('size', 0),
            'modified': item.get('lastModifiedDateTime', ''),
            'webUrl': item.get('webUrl', ''),
            'downloadUrl': item.get('@microsoft.graph.downloadUrl', ''),
            'mimeType': item.get('file', {}).get('mimeType', '')
        }
    
    def _file_matches_criteria(
        self,
        file_info: Dict[str, Any],
        file_extensions: Set[str],
        min_size_bytes: Optional[int],
        max_size_bytes: Optional[int],
        modified_after: Optional[datetime],
        modified_before: Optional[datetime],
        name_pattern: Optional[str],
        include_metadata: bool
    ) -> bool:
        # Apply all filtering criteria in order of performance (fastest first)
        filename = file_info['name']
        
        if not self._matches_extension(filename, file_extensions):
            return False
        
        if not self._matches_name_pattern(filename, name_pattern):
            return False
        
        # Size and date checks require metadata, so only run if available
        if include_metadata:
            if not self._matches_size(file_info, min_size_bytes, max_size_bytes):
                return False
            
            if not self._matches_date(file_info, modified_after, modified_before):
                return False
        
        return True
    
    def _matches_extension(self, filename: str, file_extensions: Optional[Set[str]]) -> bool:
        if not file_extensions:
            return True
        file_ext = Path(filename).suffix.lower()
        return file_ext in file_extensions
    
    def _matches_name_pattern(self, filename: str, name_pattern: Optional[str]) -> bool:
        if not name_pattern:
            return True
        return fnmatch.fnmatch(filename.lower(), name_pattern.lower())
    
    def _matches_size(self, file_info: Dict[str, Any], min_size_bytes: Optional[int], max_size_bytes: Optional[int]) -> bool:
        file_size = file_info.get('size', 0)
        
        if min_size_bytes is not None and file_size < min_size_bytes:
            return False
        
        if max_size_bytes is not None and file_size > max_size_bytes:
            return False
        
        return True
    
    def _matches_date(self, file_info: Dict[str, Any], modified_after: Optional[datetime], modified_before: Optional[datetime]) -> bool:
        modified_str = file_info.get('modified', '')
        if not modified_str or (not modified_after and not modified_before):
            return True
        
        try:
            modified_date = datetime.fromisoformat(modified_str.replace('Z', '+00:00'))
            
            if modified_after and modified_date < modified_after:
                return False
            
            if modified_before and modified_date > modified_before:
                return False
                
        except (ValueError, TypeError):
            # If we can't parse the date, skip date filtering for this file
            pass
        
        return True
    
    def find_file_by_name(
        self,
        filename: str,
        case_sensitive: bool = False,
        folder_path: str = "root",
        include_subfolders: bool = True,
        **kwargs
    ) -> Optional[str]:
        """
        Find a file by its filename within the SharePoint site.
        
        Searches for a file with the specified name across the SharePoint site,
        returning the full path to the file if found. This is useful when you
        know a filename but not its location within the site structure.
        
        Args:
            filename: The name of the file to search for (e.g., "report.pdf", "data.xlsx")
            case_sensitive: Whether the filename comparison should be case-sensitive (default: False)
            folder_path: Starting folder path to search (default: "root")
            include_subfolders: Whether to search subdirectories recursively (default: True)
            **kwargs: Additional options (include_metadata, skip_errors, logger)
            
        Returns:
            str: The full path to the file if found (e.g., "Documents/Reports/2024/report.pdf")
            None: If the file is not found
            
        Example:
            >>> factory = SharepointFileProxyFactory(...)
            >>> path = factory.find_file_by_name("quarterly_report.pdf")
            >>> if path:
            ...     proxy = factory.create(path)
            ...     # Process the found file
        """
        if not filename:
            return None
            
        skip_errors = kwargs.get('skip_errors', True)
        logger = kwargs.get('logger', logging.getLogger(__name__))
        
        try:
            return self._find_file_recursive(
                folder_path, filename, case_sensitive,
                include_subfolders, logger, ""
            )
        except Exception as e:
            logger.error(f"Error during file search for '{filename}': {e}")
            if skip_errors:
                return None
            else:
                raise RuntimeError(f"File search failed: {e}")
    
    def _find_file_recursive(
        self,
        folder_path: str,
        target_filename: str,
        case_sensitive: bool,
        include_subfolders: bool,
        logger: logging.Logger,
        current_path: str
    ) -> Optional[str]:
        """
        Recursively search for a file by name with early termination.
        
        Returns the full path to the first matching file found, or None if not found.
        """
        try:
            logger.debug(f"🔍 Searching in folder: {current_path or 'root'}")
            items_data = self._api_client.get_folder_contents(folder_path)
            items = items_data.get('value', [])
            
            for item in items:
                item_name = item['name']
                item_path = f"{current_path}/{item_name}" if current_path else item_name
                
                if 'folder' in item:
                    if include_subfolders:
                        logger.debug(f"📂 Searching subfolder: {item_path}")
                        result = self._find_file_recursive(
                            item['id'], target_filename, case_sensitive,
                            include_subfolders, logger, item_path
                        )
                        if result is not None:
                            return result
                    else:
                        logger.debug(f"📂 Skipping folder (include_subfolders=False): {item_path}")
                else:
                    item_filename = item_name if case_sensitive else item_name.lower()
                    target = target_filename if case_sensitive else target_filename.lower()
                    if item_filename == target:
                        logger.info(f"✅ Found file: {item_path}")
                        return item_path
                        
        except requests.exceptions.Timeout:
            logger.warning(f"⏰ Timeout searching folder: {current_path or 'root'}")
        except Exception as e:
            logger.warning(f"❌ Error searching folder {current_path or 'root'}: {str(e)}")
        
        return None
    
    def scan_directory(
        self,
        directory_path: str,
        file_extensions: Optional[Set[str]] = None,
        min_size_bytes: Optional[int] = None,
        max_size_bytes: Optional[int] = None,
        modified_after: Optional[datetime] = None,
        modified_before: Optional[datetime] = None,
        name_pattern: Optional[str] = None,
        include_subfolders: bool = True,
        **kwargs
    ) -> Generator["SharepointFileProxy", None, None]:
        """
        Scan a specific directory and yield SharepointFileProxy objects for all files.
        
        This is a convenience method that wraps scan_files() with directory-specific
        parameters. It provides the same interface and functionality as scan_files()
        but is optimized for scanning a known directory structure.
        
        Args:
            directory_path: The directory path to scan (e.g., "Documents/Projects/2024")
            file_extensions: Set of allowed file extensions (e.g., {'.pdf', '.docx'})
            min_size_bytes: Minimum file size in bytes
            max_size_bytes: Maximum file size in bytes
            modified_after: Only include files modified after this datetime
            modified_before: Only include files modified before this datetime
            name_pattern: Glob-style pattern for filename matching
            include_subfolders: Whether to scan subfolders recursively (default: True)
            **kwargs: Additional options (include_metadata, skip_errors, progress_callback, logger)
            
        Yields:
            SharepointFileProxy: Configured proxy objects for matching files in the directory
            
        Example:
            >>> factory = SharepointFileProxyFactory(...)
            >>> for proxy in factory.scan_directory("Documents/Reports/2024"):
            ...     print(f"Found: {proxy.file_path}")
            ...     # Process each file in the directory
        """
        yield from self.scan_files(
            folder_path=directory_path,
            file_extensions=file_extensions,
            min_size_bytes=min_size_bytes,
            max_size_bytes=max_size_bytes,
            modified_after=modified_after,
            modified_before=modified_before,
            name_pattern=name_pattern,
            include_subfolders=include_subfolders,
            **kwargs
        )


class SharepointFileProxy(FileProxyBase):
    """
    A class that represents a file on a SharePoint server.
    
    This proxy handles lazy retrieval of SharePoint files using Microsoft Graph API.
    The file is downloaded to a temporary location when materialize() is called,
    and then moved to the target location when deploy() is called.
    
    ## Readable Attributes
    
    The following SharePoint-related attributes are available for users to access:
    
    - `site_id`: The SharePoint site ID (e.g., "mccompanies.sharepoint.com,f18cd5cd-e94a-4ef2-bc2b-8744f6f101b4,3a2b3765-3790-4aec-88a3-dd525c9d5fc3")
    - `site_name`: The human-readable site name (e.g., "MCPolicies") if provided during construction
    - `drive_id`: The SharePoint drive ID (document library ID) (e.g., "b!zdWM8Urp8k68K4dE9vEBtGU3KzqQN-xKiKPdUlydX8OojUfUj_9rR7Pm3GuC9d2i")
    - `file_path`: The file path within the SharePoint drive (relative to root)
    - `base_url`: The Microsoft Graph API base URL (defaults to "https://graph.microsoft.com/v1.0")
    - `file_size`: The file size in bytes (if available from metadata)
    - `last_modified`: The last modified timestamp (if available from metadata)
    
    ## Internal State Attributes (for debugging)
    
    The following attributes track the internal state of the proxy:
    
    - `_local_file_path`: Path to the locally downloaded file (if materialized)
    - `_was_deployed`: Whether the file has been deployed to its final location
    - `_materialization_started`: Whether the download process has started
    - `_materialization_completed`: Whether the download process has completed
    
    These attributes are useful for debugging, logging, and understanding the SharePoint context
    of the file without needing to parse the ref_path.
    
    ## Example Usage
    
    ```python
    # Access SharePoint metadata
    proxy = SharepointFileProxy(...)
    print(f"Site: {proxy.site_name} (ID: {proxy.site_id})")
    print(f"Drive: {proxy.drive_id}")
    print(f"File: {proxy.file_path}")
    print(f"Size: {proxy.file_size} bytes")
    print(f"Modified: {proxy.last_modified}")
    
    # Check internal state (useful for debugging)
    print(f"Materialized: {proxy._materialization_completed}")
    print(f"Deployed: {proxy._was_deployed}")
    if proxy._local_file_path:
        print(f"Local file: {proxy._local_file_path}")
    ```
    """


    def __init__(
        self,
        site_id: str,
        drive_id: str,
        file_path: str,
        access_token: str,
        file_size: Optional[int] = None,
        last_modified: Optional[datetime] = None,
        base_url: str = "https://graph.microsoft.com/v1.0",
        site_name: Optional[str] = None,
        drive_name: Optional[str] = None,
        download_url: str = ""
    ):
        """
        Initialize a SharePoint file proxy.
        
        Args:
            site_id: The SharePoint site ID (e.g., from sites/{site_id})
            drive_id: The SharePoint drive ID (document library ID)
            file_path: The path to the file within the SharePoint drive (relative to root)
            access_token: Bearer token for Microsoft Graph API authentication
            file_size: Optional file size in bytes (for optimization and validation)
            last_modified: Optional last modified timestamp (for comparison)
            base_url: Base URL for Microsoft Graph API (defaults to v1.0)
            site_name: Optional human-readable site name for semantic ref_paths (e.g., "MCPolicies")
            
        Note:
            The file_path should be relative to the drive root, e.g., "Documents/file.pdf"
            or "folder/subfolder/document.docx". Leading slashes are automatically removed.
        """
        self.site_id = site_id
        self.drive_id = drive_id
        self.file_path = file_path.lstrip('/')  # Remove leading slash if present
        self.access_token = access_token
        self.file_size = file_size
        self.last_modified = last_modified
        self.base_url = base_url.rstrip('/')
        self.site_name = site_name
        self.drive_name = drive_name
        self.download_url = download_url
        
        # Create API client for all Graph API operations
        self._api_client = _SharePointGraphApiClient(
            site_id=site_id,
            drive_id=drive_id,
            access_token=access_token,
            base_url=base_url
        )
        
        # Internal state
        self._local_file_path: Optional[str] = None
        self._was_deployed = False
        self._materialization_started = False
        self._materialization_completed = False
        
    def ref_path(self) -> str:
        """
        Get the reference path for the file in SharePoint.
        Returns a semantic path that serves as both the original location identifier and a unique key within the cache.
        
        If site_name is provided, uses a human-readable format: "sharepoint://{site_name}/{file_path}"
        Otherwise, falls back to the technical format: "sharepoint://{site_id}/{drive_id}/{file_path}"
        """
        if self.site_name:
            return f"sharepoint://{self.site_name}/{self.file_path}"
        else:
            return f"sharepoint://{self.site_id}/{self.drive_id}/{self.file_path}"
    
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """
        Download the SharePoint file to a temporary location using Microsoft Graph API.
        
        Args:
            blocking_secs: Maximum time to block waiting for download completion
            temp_dir: Directory for temporary files during materialization. Must be provided
                     and non-blank to ensure proper cleanup of orphaned temp files.
            
        Returns:
            True if the file has been successfully downloaded and is ready for deployment.
            False if the download is still in progress or failed.
            
        Raises:
            RuntimeError: If an error occurs during the download process.
            ValueError: If temp_dir is None or blank.
        """
        if temp_dir is None or not temp_dir:
            raise ValueError("temp_dir must be provided and non-blank to ensure proper cleanup of orphaned temp files")
        if self._materialization_completed:
            return True
            
        if not self._materialization_started:
            self._materialization_started = True
            try:
                await self._download_file(temp_dir)
                self._materialization_completed = True
                return True
            except Exception as e:
                raise RuntimeError(f"Failed to download SharePoint file: {e}")
        
        # If materialization was started but not completed, wait for it
        if blocking_secs > 0:
            await asyncio.sleep(min(0.1, blocking_secs))
            return self._materialization_completed
        
        return False
    
    def deploy(self, target_dir: str) -> None:
        """
        Move the downloaded file to the target directory.
        
        Args:
            target_dir: Directory where the file should be deployed
            
        Raises:
            RuntimeError: If the file hasn't been materialized or has already been deployed.
        """
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
            
        if not self._materialization_completed or self._local_file_path is None:
            raise RuntimeError("File must be materialized before deployment")
            
        if target_dir == "/dev/null":
            # Just clean up the temporary file
            if os.path.exists(self._local_file_path):
                os.remove(self._local_file_path)
            self._was_deployed = True
            return
            
        # Fail if target directory does not exist
        if not os.path.isdir(target_dir):
            raise RuntimeError(f"Target directory does not exist: {target_dir}")
        
        # Get the filename from the SharePoint path
        filename = os.path.basename(self.file_path)
        target_path = os.path.join(target_dir, filename)
        
        try:
            # Move the file to the target location
            shutil.move(self._local_file_path, target_path)
            
            # Set the modification time if we have that information
            if self.last_modified:
                timestamp = self.last_modified.timestamp()
                os.utime(target_path, (timestamp, timestamp))
                
            self._was_deployed = True
            
        except Exception as e:
            raise RuntimeError(f"Failed to deploy file to {target_dir}: {e}")

    async def _download_file(self, temp_dir: Path) -> None:
        """
        Download the file from SharePoint using Microsoft Graph API.
        
        Uses the download URL provided by SharePoint API if available, otherwise
        constructs URL using the same pattern as folder discovery:
        /drives/{drive_id}/root:/{file_path}:/content
        
        Args:
            temp_dir: Directory where temporary files should be created
        """
        # If we don't have metadata, try to fetch it first (much faster than downloading)
        if self.file_size is None or self.last_modified is None:
            await self._fetch_file_metadata_async()
        
        # Use the download URL provided by SharePoint API if available
        if self.download_url:
            url = self.download_url
        else:
            # Fallback to constructing the URL
            url = f"{self.base_url}/drives/{self.drive_id}/root:/{self.file_path}:/content"
        
        temp_fd, temp_path = tempfile.mkstemp(dir=str(temp_dir))
        os.close(temp_fd)
        
        try:
            await self._api_client.download_file_content(url, temp_path)
            
            # Verify file size if provided (for validation)
            if self.file_size is not None:
                actual_size = os.path.getsize(temp_path)
                if actual_size != self.file_size:
                    raise RuntimeError(f"File size mismatch: expected {self.file_size} bytes, got {actual_size} bytes")
            
            self._local_file_path = temp_path
            
        except Exception as e:
            # Clean up the temporary file on error
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e
    
    async def ensure_metadata_available(self) -> bool:
        """
        Ensure that file metadata is available for fast comparison.
        This method should be called before looks_same() to optimize performance.
        
        Returns:
            bool: True if metadata is now available, False if unable to fetch
        """
        # If we already have metadata, we're good
        if self.file_size is not None and self.last_modified is not None:
            return True
        
        # Try to fetch metadata asynchronously
        return await self._fetch_file_metadata_async()

    def looks_same(self, other_fpath: str) -> Optional[bool]:
        """
        Check if this SharePoint file is the same as the other file using file size and modify time.
        Returns:
            Optional[bool]: True if the files are the same, False if the files are different, or None if unable to determine.
        """
        try:
            # If we have cached file info, use that for comparison
            if self.file_size is not None and self.last_modified is not None:
                other_stat = os.stat(other_fpath)
                return (self.file_size == other_stat.st_size and 
                        self.last_modified.timestamp() == other_stat.st_mtime)
            
            # If we don't have metadata, return None to let the system handle it
            return None
                
        except (OSError, IOError):
            return None

    async def _fetch_file_metadata_async(self) -> bool:
        """
        Fetch file metadata from SharePoint API without downloading the file.
        This is much faster than downloading the entire file for comparison.
        
        Returns:
            bool: True if metadata was successfully fetched, False otherwise
        """
        try:
            # Construct the API URL to get file metadata
            # Use the same pattern as the factory for consistency
            if self.file_path == "root" or self.file_path == "":
                # This shouldn't happen for individual files, but handle it gracefully
                return False
            
            # Use API client to fetch metadata
            file_data = await self._api_client.get_file_metadata(self.file_path)
            
            # Extract metadata
            self.file_size = file_data.get('size')
            
            # Parse last modified date
            if file_data.get('lastModifiedDateTime'):
                try:
                    self.last_modified = datetime.fromisoformat(
                        file_data['lastModifiedDateTime'].replace('Z', '+00:00')
                    )
                except (ValueError, TypeError):
                    self.last_modified = None
            
            return True
                
        except Exception:
            # If anything goes wrong, return False to fall back to file download
            return False

    def get_context_info(self) -> Dict[str, Any]:
        return {
            "proxy_type": "SharepointFileProxy",
            "site_id": self.site_id,
            "site_name": self.site_name,
            "drive_id": self.drive_id,
            "drive_name": self.drive_name,
            "file_path": self.file_path,
            "base_url": self.base_url,
            "file_size": self.file_size,
            "last_modified": self.last_modified,
            "local_file_path": self._local_file_path,
            "was_deployed": self._was_deployed,
            "materialization_started": self._materialization_started,
            "materialization_completed": self._materialization_completed
        }
