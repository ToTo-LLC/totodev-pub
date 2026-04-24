#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Outlook Email Sync - Dual-Mode Email Synchronization Example

Production-ready email caching using EmailFolderSynchronizer with Microsoft 365.
Demonstrates intelligent dual-mode sync: frequent lightweight checks + periodic full sweeps.

## Cache Structure

Emails and attachments are organized semantically:
    Email:       {cache_root}/owner@company.com/Inbox/2025-11-03/104949_sender@example_a3f2c9b1.eml
    Attachment:  {cache_root}/owner@company.com/Inbox/2025-11-03/104949_sender@example_a3f2c9b1_files/att01_Report.pdf
    Metadata:    {cache_root}/owner@company.com/Inbox/2025-11-03/104949_sender@example_a3f2c9b1.eml._slave/metadata.yaml
    State:       {cache_root}/owner@company.com/Inbox/_grouping._slave/sync_timing.yaml

## Key Concepts

**Dual-Mode Synchronization:**
- Lightweight mode: Fast polling for new items (uses upsert_file)
- Full sweep mode: Comprehensive change detection with mark-and-sweep (uses resync_bulk)
- First run auto-detects and performs full sweep
- State persistence via EmailSyncTimingInfo enables seamless resumption

**Core Components:**
- EmailFolderSynchronizer: Manages sync timing and mode selection
- CachedFileFolders: Intelligent caching with automatic change detection
- CacheGrouping: Scoped operations within cache (owner + folder)
- FileProxy: Unified abstraction for emails and attachments
- Change receiver callback: Process INSERT/UPDATE/DELETE events

**Per-File Metadata:**
- Each email gets a metadata.yaml file in its slave directory (email.eml._slave/)
- Contains processing timestamp, flag status, and complete attachment inventory
- Lists both extracted attachments (as separate files) and inline attachments (kept in EML)
- Useful for tracking processing state, OCR completion, indexing status, etc.

## Quick Start

```bash
# Required environment setup
export AZURE_CLIENT_ID="your-client-id"
export AZURE_CLIENT_SECRET="your-client-secret"
export AZURE_TENANT_ID="your-tenant-id"
export AZURE_USER_EMAIL="user@company.com"

# Test with 5 emails
python outlook_email_sync.py \\
    --cache-root /tmp/email_cache \\
    --owner-email owner@company.com \\
    --received-after 2025-11-01T00:00:00 \\
    --max-results 5 \\
    --run-minutes 0.5

# Production single-run (cron mode)
python outlook_email_sync.py \\
    --cache-root /var/cache/email \\
    --owner-email owner@company.com \\
    --received-after 2025-11-01T00:00:00 \\
    --run-minutes 0
```

## Adapting for Other Email Systems

To adapt for Gmail, IMAP, or other email systems:
1. Replace the `fetch_emails()` function with your email provider's API
2. Ensure it returns FileProxyBase objects (or create your own FileProxy implementation)
3. Keep the EmailFolderSynchronizer structure - it's provider-agnostic
4. Update authentication mechanism as needed

## Azure Setup

Requires app-only authentication (OAuth2 client credentials). See the
`get_access_token()` function docstring for complete Azure app registration steps.

## Production Notes

- Consider cron/systemd over long-running processes
- Implement monitoring for consecutive failures (max_consecutive_errors parameter)
- Use certificate-based auth instead of client secrets for security
- Add structured logging with rotation
- The --run-minutes parameter is primarily for testing dual-mode behavior
"""

import sys
import os
import asyncio
import logging
from typing import Callable, Generator, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass

import click
from msal import ConfidentialClientApplication
import requests

from totodev_pub.cached_file_folders_support.file_proxy_outlook_email import (
    OutlookEmailFileProxyFactory,
    DEFAULT_MIN_IMAGE_KB
)
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from totodev_pub.cached_file_folders_support import ChangeNotice, ChangeType
from totodev_pub.cached_file_folders_support.sync_types import UpsertFailure
from totodev_pub.cached_file_folders_support.email_folder_synchronizer import (
    EmailFolderSynchronizer, EmailSyncTimingInfo
)
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping


# =============================================================================
# MODULE CONSTANTS
# =============================================================================

# Grouping pattern for organizing emails by owner email and server folder
GROUPING_PATTERN = "{owner_email}/{server_folder}/"

# Required Azure/Microsoft 365 environment variables for app-only authentication
REQUIRED_ENVIRONMENT_STRINGS = [
    "AZURE_CLIENT_ID",       # Application (client) ID from Azure app registration
    "AZURE_CLIENT_SECRET",   # Client secret value (expires periodically, rotate regularly)
    "AZURE_TENANT_ID",       # Directory (tenant) ID for your organization
    "AZURE_USER_EMAIL"       # Email address of the mailbox to access (e.g., user@company.com)
]

# Note: DEFAULT_MIN_IMAGE_KB is imported from file_proxy_outlook_email module
EMAIL_FILE_EXTENSION = '.eml'
UPSERT_FAIL_POLICY = "RETAIN_OLD"


# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

def configure_logging(debug_enabled: bool = False) -> None:
    """Configure logging levels for external libraries."""
    for logger_name in ['msal', 'urllib3', 'requests', 'asyncio']:
        logging.getLogger(logger_name).setLevel(logging.DEBUG if debug_enabled else logging.WARNING)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class EmailSyncConfig:
    """Configuration for email synchronization.
    
    Groups related parameters to reduce function signature complexity.
    """
    cache_root: str
    owner_email: str
    folder_path: str
    received_after: datetime
    received_before: datetime | None
    max_results: int | None
    retain_days: int
    new_check_interval: int
    full_check_interval: int
    run_minutes: float
    max_consecutive_errors: int
    azure_config: dict


# =============================================================================
# SYNC STATISTICS TRACKING
# =============================================================================

class SyncStats:
    """Track sync statistics during change processing."""
    def __init__(self):
        self.insert_count = 0
        self.update_count = 0
        self.delete_count = 0
        self.emails_processed = 0
        self.attachments_processed = 0


# =============================================================================
# CHANGE RECEIVER: Classification & Dispatch Pattern
# =============================================================================
# Extracted from mega-function to demonstrate proper modular design.
# This pattern is reusable, testable, and follows single responsibility principle.

@dataclass
class ChangeClassification:
    """Classification of a change event for dispatch.
    
    Extracts all the classification logic into a single, testable unit.
    For deletes, includes pre-computed display message since all info is available.
    """
    operation: str      # 'upsert' or 'delete'
    file_type: str      # 'email' or 'attachment'
    filename: str       # Name of the file being changed
    change_type_for_stats: str  # 'insert', 'update', or 'delete' - for aggregate tracking
    delete_msg: str | None = None  # Pre-computed message for delete operations
    
    @classmethod
    def from_change(cls, change: ChangeNotice) -> 'ChangeClassification':
        """Classify a change event for dispatch.
        
        Factory method that creates a classification from a ChangeNotice.
        Extracts: operation type, file type, filename, and pre-computes delete messages.
        """
        # Determine operation type
        change_type = change.change_type.value.lower()
        operation = 'upsert' if change_type in ('insert', 'update') else 'delete'
        
        # Determine file type and filename
        if operation == 'delete':
            file_path = change.old.file_path if change.old else None
            filename = file_path.name if file_path else 'unknown'
            file_extension = (file_path.suffix if file_path else "").lower()
        else:
            file_path = change.cur.file_path
            filename = file_path.name
            file_extension = file_path.suffix.lower()
        
        file_type = 'email' if file_extension == EMAIL_FILE_EXTENSION else 'attachment'
        
        # Pre-compute delete message (all info available upfront)
        delete_msg = None
        if operation == 'delete':
            delete_msg = f"🗑️  Email deleted: {filename}" if file_type == 'email' else f"  🗑️  Attachment deleted: {filename}"
        
        return cls(
            operation=operation,
            file_type=file_type,
            filename=filename,
            change_type_for_stats=change_type,
            delete_msg=delete_msg
        )
    
    def update_stats(self, stats: SyncStats) -> None:
        """Update aggregate statistics based on this change type.
        
        Encapsulates stats tracking logic with the classification that determines it.
        This eliminates redundant inspection of change_type in the main receiver.
        """
        if self.change_type_for_stats == 'insert':
            stats.insert_count += 1
        elif self.change_type_for_stats == 'update':
            stats.update_count += 1
        elif self.change_type_for_stats == 'delete':
            stats.delete_count += 1


def _build_attachment_metadata(proxy, email_ref_path: str, min_img_kb: int = DEFAULT_MIN_IMAGE_KB) -> tuple[dict, int, int]:
    """Extract attachment metadata from an email proxy.
    
    Returns:
        tuple of (attachment_refs_dict, total_attachments, extracted_count)
        
    Builds a complete mapping of all attachments, indicating which were extracted
    to separate files vs kept inline in the EML.
        
    Note: Accesses proxy._handler for attachment details. While this accesses a private
    member, it's acceptable in example code where we're demonstrating advanced usage.
    In production, consider requesting a public API from the proxy.
    """
    from totodev_pub.cached_file_folders_support.file_proxy_outlook_email import (
        _format_ref_path_attachment, _should_extract_attachment
    )
    
    attachment_refs = {}
    
    try:
        # Access attachment list via handler (see note in docstring about encapsulation)
        all_attachments = proxy._handler.get_attachment_list()
    except AttributeError:
        # Proxy doesn't have _handler (not an OutlookEmailProxy) - no attachments
        return {}, 0, 0
    except Exception as e:
        # Unexpected error accessing attachments - log and return empty
        import logging
        logging.warning(f"Failed to get attachment list: {e}")
        return {}, 0, 0
    
    total_attachments = len(all_attachments)
    extracted_count = 0
    
    for attach_info in all_attachments:
        try:
            # Validate required fields
            if not all(key in attach_info for key in ['filename', 'size_bytes', 'sequence_number', 'original_content_type']):
                import logging
                logging.warning(f"Skipping malformed attachment: {attach_info}")
                continue
            
            # Use shared utility to determine if this was extracted
            should_extract = _should_extract_attachment(attach_info, min_img_kb)
            
            if not should_extract:
                # Small image kept in EML - no ref_path
                attachment_refs[attach_info['filename']] = {
                    'kept_inline': True,
                    'size_bytes': attach_info['size_bytes'],
                    'content_type': attach_info['original_content_type'],
                    'reason': f'Small image < {min_img_kb}KB threshold'
                }
            else:
                # Extracted attachment - compute ref_path
                extracted_count += 1
                attach_ref_path = _format_ref_path_attachment(
                    parent_email_ref_path=email_ref_path,
                    sequence_num=attach_info['sequence_number'],
                    original_filename=attach_info['filename'],
                    is_embedded_content=attach_info.get('is_embedded', False),
                    mime_content_type=attach_info['original_content_type']
                )
                
                # Compute relative path from email base
                email_base = email_ref_path[:-4] if email_ref_path.endswith(EMAIL_FILE_EXTENSION) else email_ref_path
                relative_path = attach_ref_path.replace(email_base + '/', '')
                
                attachment_refs[attach_info['filename']] = {
                    'kept_inline': False,
                    'ref_path': attach_ref_path,
                    'relative_path': relative_path,
                    'size_bytes': attach_info['size_bytes'],
                    'content_type': attach_info['original_content_type'],
                    'is_embedded': attach_info.get('is_embedded', False),
                    'sequence': attach_info['sequence_number']
                }
        except Exception as e:
            # Skip this attachment on error, but continue processing others
            import logging
            logging.warning(f"Error processing attachment {attach_info.get('filename', 'unknown')}: {e}")
            continue
    
    return attachment_refs, total_attachments, extracted_count


def _format_email_display(filename: str, flag_status: str, 
                         total_attachments: int, extracted_count: int) -> str:
    """Format a user-friendly display string for an email."""
    flag_emoji = "🚩" if flag_status == "flagged" else ""
    
    if total_attachments > 0:
        if extracted_count == total_attachments:
            attach_info = f" ({total_attachments} attachments)"
        else:
            kept_inline = total_attachments - extracted_count
            attach_info = f" ({total_attachments} attachments: {extracted_count} extracted, {kept_inline} kept inline)"
    else:
        attach_info = ""
    
    return f"📧 Email: {filename} {flag_emoji}{attach_info}"


def _handle_email_upsert(change: ChangeNotice, proxy: FileProxyBase | None, stats: SyncStats) -> str:
    """Handle email insert/update: extract rich metadata, write YAML.
    
    Returns: display message to show after success
    """
    from totodev_pub.cached_file_folders_support.file_proxy_outlook_email import OutlookEmailProxy
    
    stats.emails_processed += 1
    
    # Extract email-specific metadata
    flag_status = "unknown"
    attachment_refs = {}
    total_attachments = 0
    extracted_count = 0
    
    if proxy and isinstance(proxy, OutlookEmailProxy):
        flag_status = proxy.follow_up_flag_status
        email_ref_path = proxy.ref_path()
        attachment_refs, total_attachments, extracted_count = _build_attachment_metadata(
                proxy, email_ref_path, min_img_kb=DEFAULT_MIN_IMAGE_KB
            )
    
    # Write custom metadata.yaml with attachment dictionary
    # NOTE: Must explicitly call overwrite_source_file() to save metadata to disk
    # The metadata() method returns a LazyLoadedFileData accessor, not the file itself
    meta = change.metadata()
    if meta is not None:
        meta.overwrite_source_file({
            'file': change.cur.file_path.name,
            'processed': datetime.now().isoformat(),
            'type': 'Email',
            'ref_path': change.ref_path,
            'flag_status': flag_status,
            'total_attachments': total_attachments,
            'extracted_attachments': extracted_count,
            'attachments': attachment_refs
        })
    
    # Generate display message (needs data from metadata extraction)
    return _format_email_display(
        change.cur.file_path.name, flag_status, total_attachments, extracted_count
    )


def _handle_attachment_upsert(change: ChangeNotice, proxy: FileProxyBase | None, stats: SyncStats) -> str:
    """Handle attachment insert/update: write simple metadata.
    
    Returns: display message to show after success
    """
    stats.attachments_processed += 1
    
    # Write attachment metadata
    # NOTE: Must explicitly call overwrite_source_file() to save metadata to disk
    meta = change.metadata()
    if meta is not None:
        meta.overwrite_source_file({
            'file': change.cur.file_path.name,
            'processed': datetime.now().isoformat(),
            'type': 'Attachment',
            'ref_path': change.ref_path
        })
    
    return f"  📎 Attachment: {change.cur.file_path.name}"


class _ChangeReceiver:
    """Change receiver with explicit state for better testability.
    
    Demonstrates clean separation:
    - Classification logic is isolated and testable
    - Handlers are module-level functions (independently testable)
    - Stats tracking is explicit instance state (not hidden in closure)
    - Private method can be directly tested without factory wrapper
    """
    
    def __init__(self, stats: SyncStats):
        """Initialize change receiver with stats tracking.
        
        Args:
            stats: SyncStats instance for tracking processed items and changes
        """
        self.stats = stats
    
    def _change_receiver(self, change: ChangeNotice, proxy: FileProxyBase | None) -> None:
        """Process INSERT/UPDATE/DELETE events for emails and attachments.
        
        Deletes just display a message; upserts extract metadata and write YAML.
        """
        classification = ChangeClassification.from_change(change)
        classification.update_stats(self.stats)
        
        # Early return for deletes - no further processing needed
        if classification.operation == 'delete':
            click.echo(classification.delete_msg)
            return
        
        # Handle upserts (insert/update)
        if classification.file_type == 'email':
            display_msg = _handle_email_upsert(change, proxy, self.stats)
        elif classification.file_type == 'attachment':
            display_msg = _handle_attachment_upsert(change, proxy, self.stats)
        else:
            click.echo(f"⚠️  Unknown file type: {classification.file_type}")
            return
        
        click.echo(display_msg)


def create_change_receiver(stats: SyncStats) -> Callable[[ChangeNotice, FileProxyBase | None], None]:
    """Create a change receiver callback with proper modular design.
    
    Thin wrapper that returns bound method from _ChangeReceiver class.
    This approach improves testability by making the receiver logic accessible
    as a class method rather than hidden in a closure.
    
    Returns:
        Callable that processes INSERT/UPDATE/DELETE events for emails and attachments
    """
    return _ChangeReceiver(stats)._change_receiver


# =============================================================================
# ENVIRONMENT VALIDATION
# =============================================================================

def validate_environment() -> dict:
    """Validate required Azure environment variables are set.
    
    Returns:
        Dict mapping lowercase variable names to their values
        
    Raises:
        SystemExit: If any required variables are missing
    """
    missing = [var for var in REQUIRED_ENVIRONMENT_STRINGS if not os.getenv(var)]
    
    if missing:
        click.echo(f"❌ Missing {len(missing)} required environment variable(s): {', '.join(missing)}", err=True)
        click.echo("\nRequired environment variables:", err=True)
        for var in REQUIRED_ENVIRONMENT_STRINGS:
            status = "✓" if os.getenv(var) else "✗"
            click.echo(f"  {status} export {var}='<your-value-here>'", err=True)
        click.echo("\nRun with --help for setup instructions and variable descriptions.", err=True)
        sys.exit(1)
    
    return {var.lower(): os.getenv(var) for var in REQUIRED_ENVIRONMENT_STRINGS}


# =============================================================================
# AZURE AD AUTHENTICATION
# =============================================================================

def get_access_token(config: dict) -> str:
    """Get access token using Azure AD app-only authentication (OAuth2 client credentials).
    
    Uses MSAL ConfidentialClientApplication for unattended access to mailboxes.
    Perfect for automated scripts and cron jobs - no user sign-in required.
    
    ## Azure App Registration Steps
    
    1. Register app at https://entra.microsoft.com/ → App registrations → New registration
    2. Add **Application permissions** (NOT Delegated): Mail.Read + Mail.ReadBasic
    3. Grant admin consent (required for application permissions)
    4. Create client secret under "Certificates & secrets"
    5. Record these values for environment variables:
       - Application (client) ID → AZURE_CLIENT_ID
       - Directory (tenant) ID → AZURE_TENANT_ID
       - Client secret value → AZURE_CLIENT_SECRET
       - Mailbox email → AZURE_USER_EMAIL
    
    **Note:** Client secrets expire periodically. Consider certificate-based auth for production.
    
    Args:
        config: Dict with azure_client_id, azure_tenant_id, azure_client_secret
        
    Returns:
        Access token string for Graph API
        
    Raises:
        RuntimeError: If authentication fails
    """
    app = ConfidentialClientApplication(
        config['azure_client_id'], 
        authority=f"https://login.microsoftonline.com/{config['azure_tenant_id']}", 
        client_credential=config['azure_client_secret']
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    
    if "access_token" not in result:
        error_msg = result.get('error_description', 'Unknown authentication error')
        raise RuntimeError(f"Failed to get access token: {error_msg}")
    
    return result["access_token"]


# =============================================================================
# EMAIL FETCHER
# =============================================================================

def create_email_fetcher(
    config: dict,
    folder_path: str,
    received_before: datetime | None,
    max_results: int | None
) -> Callable[[datetime], Generator[FileProxyBase, None, None]]:
    """Create an email fetcher function for the synchronizer.
    
    This factory function demonstrates provider-specific email fetching logic.
    The returned function takes a cutoff datetime and yields FileProxyBase objects.
    
    Args:
        config: Azure config dict with keys: azure_user_email, azure_client_id, etc.
        folder_path: Email folder to sync (e.g., "Inbox")
        received_before: Optional upper bound on email received datetime
        max_results: Optional limit on number of emails to process
    
    Returns:
        Callable that fetches emails from Outlook using Graph API
    """
    def fetch_emails(cutoff: datetime) -> Generator[FileProxyBase, None, None]:
        """Fetch emails from Outlook using Graph API (provider-specific implementation)."""
        # Get fresh access token for this fetch operation
        access_token = get_access_token(config)
        
        # Create factory to generate file proxies
        factory = OutlookEmailFileProxyFactory(
            user_email=config['azure_user_email'],
            access_token=access_token,
            min_img_kbytes=DEFAULT_MIN_IMAGE_KB
        )
        
        # Generate email proxies
        # scan_messages() now yields only emails; resync_bulk() will handle nested_proxies() intelligently
        for email_proxy in factory.scan_messages(
            received_after=cutoff,
            folder_path=folder_path,
            received_before=received_before,
            max_results=max_results,
            newest_first=True,  # Process newer emails first for efficiency
            headers="slim"      # Cleaner .eml files
        ):
            yield email_proxy
    
    return fetch_emails


# =============================================================================
# MAIN SYNC FUNCTION
# =============================================================================

async def sync_outlook_emails(config: EmailSyncConfig) -> None:
    """Main sync function demonstrating EmailFolderSynchronizer usage.
    
    Shows the complete pattern:
    1. Create CachedFileFolders and CacheGrouping facet
    2. Load/create EmailSyncTimingInfo (state persists between runs)
    3. Define email fetcher function (provider-specific logic)
    4. Define change receiver callback (process INSERT/UPDATE/DELETE events)
    5. Create EmailFolderSynchronizer
    6. Run sync loop (single-run or continuous monitoring)
    
    The synchronizer automatically handles dual-mode selection based on timing state.
    
    Args:
        config: EmailSyncConfig containing all sync parameters
    """
    
    # Step 1: Create cache instance and grouping facet
    cache = CachedFileFolders(
        grouping_pattern=GROUPING_PATTERN,
        root_dir=os.path.abspath(config.cache_root),
        use_xxhash=False
    )
    grouping = cache.grouping([config.owner_email, config.folder_path])
    
    # Step 2: Create/load EmailSyncTimingInfo from grouping slave directory
    # State persists between runs for seamless resumption
    slave_dir = grouping.get_slave_dir()
    sync_info = EmailSyncTimingInfo.open(
        str(slave_dir / "sync_timing.yaml"),
        without_lock=False
    )
    
    # Configure timing parameters
    sync_info.retain_days = config.retain_days
    sync_info.new_check_interval_secs = config.new_check_interval
    sync_info.full_check_interval_secs = config.full_check_interval
    
    # Step 3: Create email fetcher using factory function
    # This pattern makes the synchronizer generic - works with any email system
    fetch_emails = create_email_fetcher(
        config.azure_config, config.folder_path, config.received_before, config.max_results
    )
    
    # Step 4: Create change receiver using factory function
    # The change receiver logic is extracted to module level for proper modular design.
    # This demonstrates reusable, testable code organization.
    stats = SyncStats()
    change_receiver = create_change_receiver(stats)
    
    # Step 5: Create synchronizer
    synchronizer = EmailFolderSynchronizer(
        sync_info=sync_info,
        cache=grouping,
        email_fetcher=fetch_emails,
        upsert_fail_policy=UPSERT_FAIL_POLICY,
        max_consecutive_errors=config.max_consecutive_errors,
        min_cutoff_date=config.received_after  # Don't fetch emails earlier than this
    )
    
    # Step 6: Run sync loop
    click.echo(f"📬 Starting email sync from folder: {config.folder_path}")
    click.echo(f"   Owner: {config.owner_email}")
    click.echo(f"   Retain: {config.retain_days} days")
    click.echo(f"   Mode: {'Single run' if config.run_minutes == 0 else f'Continuous for {config.run_minutes} min'}")
    if config.max_results:
        click.echo(f"   Max results: {config.max_results} (testing mode)")
    click.echo(f"   State file: {slave_dir / 'sync_timing.yaml'}")
    click.echo()
    
    try:
        if config.run_minutes == 0:
            # Single sync run
            click.echo("⏱️  Running single sync...")
            result = await synchronizer.sync(change_receiver=change_receiver)
            _print_sync_result(result, stats, config.cache_root, config.owner_email, config.folder_path)
        else:
            # Continuous monitoring loop
            end_time = datetime.now() + timedelta(minutes=config.run_minutes)
            iteration = 0
            
            while datetime.now() < end_time:
                iteration += 1
                click.echo(f"⏱️  Sync iteration {iteration} ({result.get('sync_type', 'unknown') if iteration > 1 else 'starting'})")
                
                result = await synchronizer.sync(change_receiver=change_receiver)
                _print_sync_result(result, stats, config.cache_root, config.owner_email, config.folder_path)
                
                # Wait before next iteration
                wait = synchronizer.recommended_wait_secs()
                remaining = (end_time - datetime.now()).total_seconds()
                
                if remaining <= 0:
                    break
                
                actual_wait = min(wait, remaining)
                click.echo(f"⏸️  Waiting {actual_wait:.0f}s until next check...")
                click.echo()
                await asyncio.sleep(actual_wait)
            
            click.echo(f"✅ Monitoring complete after {iteration} iteration(s)")
    finally:
        # Clean up: release file lock on state file
        if hasattr(sync_info, 'release_lock'):
            sync_info.release_lock()


def _print_sync_result(result: Dict[str, Any], stats: SyncStats, cache_root: str, owner_email: str, folder_path: str) -> None:
    """Print formatted sync result summary."""
    click.echo()
    click.echo("─" * 60)
    click.echo(f"📊 Sync Result ({result['sync_type'].upper()})")
    click.echo(f"   Processed: {stats.emails_processed} emails, {stats.attachments_processed} attachments")
    click.echo(f"   Changes: {stats.insert_count} inserted, {stats.update_count} updated, {stats.delete_count} deleted")
    click.echo(f"   Duration: {result['duration_secs']:.2f}s")
    if result.get('failures'):
        click.echo(f"   ⚠️  Failures: {len(result['failures'])}")
        for i, failure in enumerate(result['failures'][:3]):
            ref_path = failure.file_proxy.ref_path() if hasattr(failure.file_proxy, 'ref_path') else 'unknown'
            click.echo(f"      {i+1}. {ref_path}: {type(failure.exception).__name__}")
        if len(result['failures']) > 3:
            click.echo(f"      ... and {len(result['failures']) - 3} more")
    click.echo(f"   📁 Cache: {cache_root}/{owner_email}/{folder_path}/")
    click.echo("─" * 60)


# =============================================================================
# CLI INTERFACE
# =============================================================================

@click.command()
@click.option('--cache-root', required=True, help='Root directory for the CachedFileFolders cache')
@click.option('--owner-email', required=True, help='Email address of the mailbox owner (e.g., owner@company.com)')
@click.option('--folder-path', default='Inbox', help='Mail folder to sync from (default: Inbox)')
@click.option('--received-after', required=True, help='REQUIRED: Filter emails received after this datetime (ISO format: YYYY-MM-DDTHH:MM:SS)')
@click.option('--received-before', help='Optional: Filter emails received before this datetime (ISO format)')
@click.option('--max-results', type=int, help='Maximum number of emails to process (useful for testing)')
@click.option('--retain-days', type=int, default=2, help='Days of emails to retain in cache (default: 2)')
@click.option('--new-check-interval', type=int, default=15, help='Seconds between lightweight new item checks (default: 15)')
@click.option('--full-check-interval', type=int, default=3600, help='Seconds between full change detection sweeps (default: 3600)')
@click.option('--run-minutes', type=float, default=1.0, help='Minutes to run sync loop, 0 for single run (default: 1.0)')
@click.option('--max-consecutive-errors', type=int, default=3, help='Stop if same error repeats this many times (default: 3)')
@click.option('--debug', is_flag=True, help='Enable debug logging from external libraries')
def main(
    cache_root: str,
    owner_email: str,
    folder_path: str,
    received_after: str,
    received_before: str | None,
    max_results: int | None,
    retain_days: int,
    new_check_interval: int,
    full_check_interval: int,
    run_minutes: float,
    max_consecutive_errors: int,
    debug: bool
):
    """
    Outlook Email Sync - Dual-Mode Email Synchronization
    
    Demonstrates production-ready email caching with intelligent dual-mode sync:
    - Lightweight checks (--new-check-interval): Fast polling for new items
    - Full sweeps (--full-check-interval): Comprehensive change detection
    - First run auto-detects and performs full baseline sync
    - State persists between runs for seamless resumption
    
    \b
    PREREQUISITES:
    - Azure app registration with Mail.Read + Mail.ReadBasic permissions
    - Environment variables: AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID, AZURE_USER_EMAIL
    - See https://entra.microsoft.com/ for setup (details in get_access_token() docstring)
    
    \b
    RUN MODES:
    - --run-minutes 0: Single sync run (recommended for cron jobs)
    - --run-minutes N: Continuous monitoring for N minutes (demonstrates dual-mode)
    
    \b
    TESTING RECOMMENDATIONS:
    1. Start with --max-results 5 and --run-minutes 0.5
    2. Use --debug to see detailed logging
    3. Use recent --received-after date (e.g., yesterday)
    4. Check state file: {cache_root}/{owner_email}/{folder}/_grouping._slave/sync_timing.yaml
    5. Run again to see incremental sync
    """
    
    configure_logging(debug)
    azure_config = validate_environment()
    
    # Parse datetime strings
    try:
        received_after_dt = datetime.fromisoformat(received_after)
    except ValueError:
        click.echo(f"❌ Invalid datetime format for --received-after: {received_after}", err=True)
        click.echo("   Use ISO format: YYYY-MM-DDTHH:MM:SS", err=True)
        sys.exit(1)
    
    received_before_dt = None
    if received_before:
        try:
            received_before_dt = datetime.fromisoformat(received_before)
        except ValueError:
            click.echo(f"❌ Invalid datetime format for --received-before: {received_before}", err=True)
            click.echo("   Use ISO format: YYYY-MM-DDTHH:MM:SS", err=True)
            sys.exit(1)
    
    # Validate cache root path
    cache_root_path = Path(cache_root)
    if not cache_root_path.parent.exists():
        click.echo(f"❌ Parent directory does not exist: {cache_root_path.parent}", err=True)
        sys.exit(1)
    
    sync_config = EmailSyncConfig(
        cache_root=cache_root,
        owner_email=owner_email,
        folder_path=folder_path,
        received_after=received_after_dt,
        received_before=received_before_dt,
        max_results=max_results,
        retain_days=retain_days,
        new_check_interval=new_check_interval,
        full_check_interval=full_check_interval,
        run_minutes=run_minutes,
        max_consecutive_errors=max_consecutive_errors,
        azure_config=azure_config
    )
    
    asyncio.run(sync_outlook_emails(sync_config))


if __name__ == "__main__":
    main()

