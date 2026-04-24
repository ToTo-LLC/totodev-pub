#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CachedFileFolders Tutorial: SharePoint File Synchronization

This tutorial shows how easy it is to use CachedFileFolders with SharePoint.
CachedFileFolders automatically handles:
- Retrieval
    - Intelligent caching (only downloads changed files based on file size and mtime)
    - Parallel retrieval
- Concurrent downloads
- File organization with slave directories
- Retry logic

The tutorial demonstrates:
1. Downloading files from SharePoint
2. Working with a CacheGrouping facet for simplified access to cache
3. Processing files immediately after caching (OCR, metadata)
4. Using standardized metadata tracking and primitive event logs for processing state
5. Handling all change types (INSERT, UPDATE, DELETE)
6. Using the built in metadata() for storing per-file data
7. Using the built in event_log() for tracking processing state

The core usage is just a few lines of code!

Configuration: Update the constants below with your SharePoint details.
"""

import sys
import os
import asyncio
from typing import List, Tuple
from datetime import datetime
from msal import ConfidentialClientApplication
import requests

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))

from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import SharepointFileProxyFactory
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from totodev_pub.cached_file_folders_support.sync_types import ResyncBulkResult

# =============================================================================
# CONFIGURATION - Update these with your SharePoint details
# =============================================================================
CLIENT_ID = "INSERT_YOUR_CLIENT_ID_HERE"
CLIENT_SECRET = "INSERT_YOUR_CLIENT_SECRET_HERE" 
TENANT_ID = "INSERT_YOUR_TENANT_ID_HERE"
SITE_NAME = "INSERT_YOUR_SITE_NAME_HERE"
SHAREPOINT_DOMAIN = "yourcompany.sharepoint.com"
MAX_FILES = 10  # Number of files to download

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_access_token() -> str:
    """Get Microsoft Graph access token."""
    app = ConfidentialClientApplication(
        CLIENT_ID, 
        authority=f"https://login.microsoftonline.com/{TENANT_ID}", 
        client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    return result["access_token"]

def get_site_info(access_token: str) -> Tuple[str, str]:
    """Get SharePoint site and drive IDs."""
    headers = {'Authorization': f'Bearer {access_token}'}
    
    # Get site
    site_url = f"https://graph.microsoft.com/v1.0/sites/{SHAREPOINT_DOMAIN}:/sites/{SITE_NAME}"
    site_data = requests.get(site_url, headers=headers).json()
    site_id = site_data['id']
    
    # Get drive
    drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
    drives_data = requests.get(drives_url, headers=headers).json()
    drive_id = drives_data['value'][0]['id']
    
    return site_id, drive_id

# =============================================================================
# MAIN TUTORIAL - This is the heart of CachedFileFolders usage!
# =============================================================================

async def main():
    """The core CachedFileFolders tutorial - just a few lines!"""
    
    # Step 1: Get SharePoint access
    access_token = get_access_token()
    site_id, drive_id = get_site_info(access_token)
    
    # Step 2: Create file proxy factory - lazy retrieval of sharepoint files
    factory: SharepointFileProxyFactory = SharepointFileProxyFactory(
                                                                     site_id=site_id,
                                                                     drive_id=drive_id,
                                                                     access_token=access_token,
                                                                     site_name=SITE_NAME
                                                                    )
    
    # File proxies are objects that can be converted to files (e.g. by retrieval)
    # Note: scan_files() returns a generator, not a list
    file_proxies = factory.scan_files(max_files=MAX_FILES)
    
    # IMPORTANT NOTE: While this example retrieves its files from a single Sharepoint site
    #                 it can be getting data from anywhere, essentially anything that
    #                 can be written as a file including:
    #                 - an actual tree of local files
    #                 - RESTFUL data retrieval endpoints
    #                 - Retrieved URLs
    #                 - Data records
    #                 - Other file APIs (Dropbox, S3, etc.)
    # 
    # It's also possible to comingle this data in the same cache using different groupings
    # or simply by ensuring you name them differently with their ref_path.
    
    # Step 3: Create the cache and grab a CacheGrouping facet for this site
    #         CacheGrouping follows the facet pattern—once you have the facet you
    #         no longer pass grouping_key into every call; the grouping is baked in.
    cache: CachedFileFolders = CachedFileFolders(
                                                 grouping_pattern="sites/{site_name}/",  # Organize files by site
                                                 root_dir=".",  # Cache in current directory
                                                 use_xxhash=False,  # Use file size/date for change detection
                                                 metadata_filename="metadata.yaml"  # Customize metadata filename (default: metadata.yaml)
                                                )
    site_cache: CacheGrouping = cache.grouping([SITE_NAME])
    
    # Step 4: Download files using the CacheGrouping facet to simplify access to cache.
    print(f"Scanning and downloading {MAX_FILES} files...")
    resync_result: ResyncBulkResult = await site_cache.resync_bulk(
                                                file_proxies=file_proxies,
                                                max_concurrent_requests=5,
                                                upsert_fail_policy="RETAIN_OLD"  # Keep existing files if download fails
                                               )
    # IMPORTANT NOTE: While we use resync_bulk() above to show the simplest approach
    #                we could also use resync_sweep() instead which would allow us to
    #                - access the deleted items and slave_dirs before they are destroyed.
    #                  can be done with change_receiver parameter of resync_bulk()
    #                  (change_receiver gets both the change notice and the FileProxyBase)
    #                - control throttling and concurrence granularly
    #                - begin processing files without waiting for all downloads to complete
    #
    

    # Step 5: Process cached files and track progress using standardized metadata
    print(f"🔧 Processing {len(resync_result.changes)} cached files...")
    processed_count = 0
    for change in resync_result.changes:
        # Each change exposes the new CachedFileRef via change.cur (or old for deletes).
        # CachedFileRef.event_log() is a convenience wrapper that creates a PrimitiveEventLog
        # rooted in the file's slave directory, letting you track downstream processing steps.
        event_log = None
        if change.cur is not None:
            event_log = change.cur.event_log()
        elif change.old is not None:
            event_log = change.old.event_log()

        # Handle different change types
        if change.change_type.value == "INSERT":
            # New file - create OCR and metadata
            # The given ref_path and grouping_key were not in the repository
            # so a new file was created.
            
            # Use the convenient metadata() method to access standardized metadata
            meta = change.metadata()
            if meta:
                meta.overwrite_source_file({
                    'downloaded_at': datetime.now().isoformat(),
                    'source': 'sharepoint',
                    'site_name': SITE_NAME,
                    'processing_state': 'pending',
                    'ocr_completed': False
                })
            
            # Create fake OCR file in slave directory
            slave_dir = change.cur.slave_dir_path
            slave_dir.mkdir(parents=True, exist_ok=True)
            (slave_dir / "ocr.txt").write_text(f"Fake OCR text for {change.cur.file_path.name}")

            if event_log is not None:
                event_log.create_event(
                    "DOCUMENT-STATUS",
                    "DOWNLOADED",
                    {
                        "downloaded_at": datetime.now().isoformat(),
                        "grouping_key": list(change.cur.grouping_key or []),
                        "ref_path": change.cur.ref_path
                    }
                )
            processed_count += 1
            
        elif change.change_type.value == "UPDATE":
            # Updated file - regenerate OCR and update metadata
            # Update type changes indicate the ref_path and grouping_key 
            # was already in the repository but that the file looks changed
            
            previous_state = {}
            meta = change.metadata()
            if meta:
                # Preserve some old metadata, update processing state
                current_meta = meta.as_dict()
                current_meta.update({
                    'updated_at': datetime.now().isoformat(),
                    'processing_state': 'pending',
                    'ocr_completed': False
                })
                meta.overwrite_source_file(current_meta)
                previous_state = current_meta
            
            # Regenerate OCR
            slave_dir = change.cur.slave_dir_path
            slave_dir.mkdir(parents=True, exist_ok=True)
            (slave_dir / "ocr.txt").write_text(f"Updated OCR text for {change.cur.file_path.name}")

            if event_log is not None:
                event_log.create_event(
                    "DOCUMENT-STATUS",
                    "REFRESHED",
                    {
                        "updated_at": datetime.now().isoformat(),
                        "ref_path": change.cur.ref_path,
                        "previous_state": previous_state
                    }
                )
            processed_count += 1
            
        elif change.change_type.value == "DELETE":
            # Deleted file - could remove from vector database, etc.
            # A file with ref_path and grouping_key was already in the repository
            # but was not sent in the resync_bulk() call
            
            # Optional: Access old metadata before cleanup
            old_meta = change.old.metadata()
            if old_meta and old_meta.as_dict():
                print(f"  Cleaning up file that was: {old_meta.as_dict().get('processing_state', 'unknown')}")

            if event_log is not None:
                event_log.create_event(
                    "DOCUMENT-STATUS",
                    "REMOVED",
                    {
                        "removed_at": datetime.now().isoformat(),
                        "ref_path": change.old.ref_path if change.old else change.ref_path
                    }
                )
    
    # Done! CachedFileFolders handled everything automatically
    print(f"✅ Downloaded {len(resync_result.changes)} files")
    print(f"🔧 Processed {processed_count} files for analysis")
    print(f"📁 Files cached to: {site_cache.folder_path}")
    if resync_result.failures:
        print(f"❌ {len(resync_result.failures)} downloads failed")

if __name__ == "__main__":
    if CLIENT_ID == "INSERT_YOUR_CLIENT_ID_HERE":
        print("❌ Please update the configuration constants with your SharePoint details")
        sys.exit(1)
    
    asyncio.run(main())