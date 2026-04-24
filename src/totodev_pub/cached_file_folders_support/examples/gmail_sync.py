#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Gmail Sync - Dual-Mode Email Synchronization Example

Production-ready email caching using EmailFolderSynchronizer with Gmail.
Demonstrates intelligent dual-mode sync: frequent lightweight checks + periodic full sweeps.

## Cache Structure

Emails and attachments are organized semantically:
    Email:       {cache_root}/user@gmail.com/Inbox/2025-11-03/104949_sender@example_a3f2c9b1.eml
    Attachment:  {cache_root}/user@gmail.com/Inbox/2025-11-03/104949_sender@example_a3f2c9b1_files/att01_Report.pdf
    Metadata:    {cache_root}/user@gmail.com/Inbox/2025-11-03/104949_sender@example_a3f2c9b1.eml._slave/metadata.yaml
    State:       {cache_root}/user@gmail.com/Inbox/_grouping._slave/sync_timing.yaml

## Key Concepts

**Dual-Mode Synchronization:**
- Lightweight mode: Fast polling for new items (uses upsert_file)
- Full sweep mode: Comprehensive change detection with mark-and-sweep (uses resync_bulk)
- First run auto-detects and performs full sweep
- State persistence via EmailSyncTimingInfo enables seamless resumption

**Core Components:**
- EmailFolderSynchronizer: Manages sync timing and mode selection
- CachedFileFolders: Intelligent caching with automatic change detection
- CacheGrouping: Scoped operations within cache (owner + label)
- FileProxy: Unified abstraction for emails and attachments
- Change receiver callback: Process INSERT/UPDATE/DELETE events

**Gmail Labels vs Outlook Folders:**
- Gmail uses labels (emails can have multiple labels)
- Primary label used for grouping (SENT > DRAFT > TRASH > SPAM > INBOX)
- All labels tracked in metadata for complete context

## Quick Start

```bash
# Required environment setup
source volatile/credentials/gmail_access_creds.sh

# Test with 5 emails
python gmail_sync.py \\
    --cache-root /tmp/gmail_cache \\
    --received-after 2025-11-01T00:00:00 \\
    --labels INBOX \\
    --max-results 5 \\
    --run-minutes 0.5

# Production single-run (cron mode)
python gmail_sync.py \\
    --cache-root /var/cache/gmail \\
    --received-after 2025-11-01T00:00:00 \\
    --run-minutes 0
```

## Gmail API Setup

Requires service account authentication with domain-wide delegation.
Set these environment variables (see gmail_access_creds.sh):

- `GMAIL_SERVICE_ACCOUNT_EMAIL` - Service account email address
- `GMAIL_PROJECT_ID` - Google Cloud project ID
- `GMAIL_PRIVATE_KEY_ID` - Private key ID from service account
- `GMAIL_PRIVATE_KEY` - Private key content (multi-line)
- `GMAIL_CLIENT_ID` - Client ID from service account
- `GMAIL_USER_EMAIL` - Mailbox email address to access
- `GMAIL_TOKEN_URI` - OAuth token URI
- `GMAIL_AUTH_URI` - OAuth auth URI

## Production Notes

- Consider cron/systemd over long-running processes
- Implement monitoring for consecutive failures (max_consecutive_errors parameter)
- Rotate service account credentials regularly
- Add structured logging with rotation
- The --run-minutes parameter is primarily for testing dual-mode behavior
"""

import sys
import os
import asyncio
import logging
import json
from typing import Callable, Generator, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass

import click

from totodev_pub.cached_file_folders_support.file_proxy_gmail import (
    GmailEmailFileProxyFactory,
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

# Grouping pattern for organizing emails by owner email and primary label
GROUPING_PATTERN = "{owner_email}/{server_folder}/"

# Required Gmail environment variables for service account authentication
REQUIRED_ENVIRONMENT_STRINGS = [
    "GMAIL_SERVICE_ACCOUNT_EMAIL",  # Service account email address
    "GMAIL_PROJECT_ID",              # Google Cloud project ID
    "GMAIL_PRIVATE_KEY_ID",          # Private key ID from service account JSON
    "GMAIL_PRIVATE_KEY",             # Private key content (multi-line)
    "GMAIL_CLIENT_ID",               # Client ID from service account JSON
    "GMAIL_USER_EMAIL",              # Email address of the mailbox to access
    "GMAIL_TOKEN_URI",               # OAuth token URI
    "GMAIL_AUTH_URI",                # OAuth auth URI
]

EMAIL_FILE_EXTENSION = '.eml'
UPSERT_FAIL_POLICY = "RETAIN_OLD"


# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

def configure_logging(debug_enabled: bool = False) -> None:
    """Configure logging levels for external libraries."""
    for logger_name in ['googleapiclient', 'urllib3', 'requests', 'asyncio', 'google.auth']:
        logging.getLogger(logger_name).setLevel(logging.DEBUG if debug_enabled else logging.WARNING)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class EmailSyncConfig:
    """Configuration for email synchronization."""
    cache_root: str
    owner_email: str
    label_filter: list[str] | None
    received_after: datetime
    received_before: datetime | None
    max_results: int | None
    retain_days: int
    new_check_interval: int
    full_check_interval: int
    run_minutes: float
    max_consecutive_errors: int
    service_account_info: dict
    min_img_kbytes: int


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
# CHANGE RECEIVER PATTERN
# =============================================================================

@dataclass
class ChangeClassification:
    """Classification of a change event for dispatch."""
    operation: str      # 'upsert' or 'delete'
    file_type: str      # 'email' or 'attachment'
    filename: str
    change_type_for_stats: str  # 'insert', 'update', or 'delete'
    delete_msg: str | None = None
    
    @classmethod
    def from_change(cls, change: ChangeNotice) -> 'ChangeClassification':
        """Classify a change event for dispatch."""
        change_type = change.change_type.value.lower()
        operation = 'upsert' if change_type in ('insert', 'update') else 'delete'
        
        if operation == 'delete':
            file_path = change.old.file_path if change.old else None
            filename = file_path.name if file_path else 'unknown'
            file_extension = (file_path.suffix if file_path else "").lower()
        else:
            file_path = change.cur.file_path
            filename = file_path.name
            file_extension = file_path.suffix.lower()
        
        file_type = 'email' if file_extension == EMAIL_FILE_EXTENSION else 'attachment'
        
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
        """Update aggregate statistics based on this change type."""
        if self.change_type_for_stats == 'insert':
            stats.insert_count += 1
        elif self.change_type_for_stats == 'update':
            stats.update_count += 1
        elif self.change_type_for_stats == 'delete':
            stats.delete_count += 1


def _build_attachment_metadata(proxy, email_ref_path: str, min_img_kb: int = DEFAULT_MIN_IMAGE_KB) -> tuple[dict, int, int]:
    """Extract attachment metadata from an email proxy."""
    from totodev_pub.cached_file_folders_support.file_proxy_gmail import (
        _format_ref_path_attachment, _should_extract_attachment
    )
    
    attachment_refs = {}
    
    try:
        all_attachments = proxy._handler.get_attachment_list()
    except (AttributeError, Exception) as e:
        logging.warning(f"Failed to get attachment list: {e}")
        return {}, 0, 0
    
    total_attachments = len(all_attachments)
    extracted_count = 0
    
    for attach_info in all_attachments:
        try:
            if not all(key in attach_info for key in ['filename', 'size_bytes', 'sequence_number', 'original_content_type']):
                logging.warning(f"Skipping malformed attachment: {attach_info}")
                continue
            
            should_extract = _should_extract_attachment(attach_info, min_img_kb)
            
            if not should_extract:
                attachment_refs[attach_info['filename']] = {
                    'kept_inline': True,
                    'size_bytes': attach_info['size_bytes'],
                    'content_type': attach_info['original_content_type'],
                    'reason': f'Small image < {min_img_kb}KB threshold'
                }
            else:
                extracted_count += 1
                attach_ref_path = _format_ref_path_attachment(
                    parent_email_ref_path=email_ref_path,
                    sequence_num=attach_info['sequence_number'],
                    original_filename=attach_info['filename'],
                    is_embedded_content=attach_info.get('is_embedded', False),
                    mime_content_type=attach_info['original_content_type']
                )
                
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
            logging.warning(f"Error processing attachment {attach_info.get('filename', 'unknown')}: {e}")
            continue
    
    return attachment_refs, total_attachments, extracted_count


def _format_email_display(filename: str, label_ids: list[str], 
                         total_attachments: int, extracted_count: int) -> str:
    """Format a user-friendly display string for an email."""
    # Check for STARRED label
    star_emoji = "⭐" if "STARRED" in label_ids else ""
    
    if total_attachments > 0:
        if extracted_count == total_attachments:
            attach_info = f" ({total_attachments} attachments)"
        else:
            kept_inline = total_attachments - extracted_count
            attach_info = f" ({total_attachments} attachments: {extracted_count} extracted, {kept_inline} kept inline)"
    else:
        attach_info = ""
    
    return f"📧 Email: {filename} {star_emoji}{attach_info}"


class _ChangeReceiver:
    """Change receiver with explicit state for better testability.
    
    Class-based approach makes testing easier by exposing state as instance
    variables rather than hiding it in closure scope. The private method
    can be tested directly without going through the factory function.
    """
    
    def __init__(self, stats: SyncStats, min_img_kb: int = DEFAULT_MIN_IMAGE_KB):
        """Initialize change receiver with stats tracking and configuration.
        
        Args:
            stats: SyncStats instance for tracking processed items and changes
            min_img_kb: Minimum KB threshold for image extraction
        """
        self.stats = stats
        self.min_img_kb = min_img_kb
    
    def _change_receiver(self, change: ChangeNotice, proxy: FileProxyBase | None) -> None:
        """Process INSERT/UPDATE/DELETE events for emails and attachments."""
        from totodev_pub.cached_file_folders_support.file_proxy_gmail import GmailEmailProxy
        
        classification = ChangeClassification.from_change(change)
        classification.update_stats(self.stats)
        
        if classification.operation == 'delete':
            print(classification.delete_msg)
            if classification.file_type == 'email':
                self.stats.emails_processed += 1
            else:
                self.stats.attachments_processed += 1
            return
        
        # Upsert operations - proxy is provided as parameter
        if classification.file_type == 'email':
            self.stats.emails_processed += 1
            
            # Get label information
            label_ids = getattr(proxy, 'label_ids', [])
            is_starred = 'STARRED' in label_ids if label_ids else False
            
            # Build attachment metadata
            attachment_refs = {}
            total_attachments = 0
            extracted_count = 0
            
            if proxy and isinstance(proxy, GmailEmailProxy):
                email_ref_path = proxy.ref_path()
                attachment_refs, total_attachments, extracted_count = _build_attachment_metadata(
                    proxy, email_ref_path, self.min_img_kb
                )
            
            display_msg = _format_email_display(
                classification.filename, label_ids, 
                total_attachments, extracted_count
            )
            print(display_msg)
            
            # Write proxy's built-in eml_metadata.yaml
            if proxy and isinstance(proxy, GmailEmailProxy) and change.cur and change.cur.slave_dir_path:
                try:
                    proxy.write_metadata_to_slave_dir(change.cur.slave_dir_path)
                except Exception as e:
                    logging.error(f"Failed to write eml_metadata for {classification.filename}: {e}")
            
            # Write custom metadata.yaml with attachment dictionary
            # NOTE: Must explicitly call overwrite_source_file() to save metadata to disk
            # The metadata() method returns a LazyLoadedFileData accessor, not the file itself
            if change.cur is None or change.cur.slave_dir_path is None:
                logging.warning(f"slave_dir_path is None for {classification.filename} - cannot write metadata")
                return
            
            meta = change.metadata()
            if meta is not None:
                try:
                    meta.overwrite_source_file({
                        'file': change.cur.file_path.name,
                        'processed': datetime.now().isoformat(),
                        'type': 'Email',
                        'ref_path': change.ref_path,
                        'is_starred': is_starred,
                        'labels': label_ids,
                        'total_attachments': total_attachments,
                        'extracted_attachments': extracted_count,
                        'attachments': attachment_refs
                    })
                except Exception as e:
                    logging.error(f"Failed to write metadata for {classification.filename}: {e}")
                
        else:  # attachment
            self.stats.attachments_processed += 1
            print(f"  📎 Attachment: {classification.filename}")
            
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


def create_change_receiver(stats: SyncStats, min_img_kb: int = DEFAULT_MIN_IMAGE_KB) -> Callable:
    """Create a change receiver function for processing sync events.
    
    Thin wrapper that returns bound method from _ChangeReceiver class.
    This approach improves testability by making the receiver logic accessible
    as a class method rather than hidden in a closure.
    
    Args:
        stats: SyncStats instance for tracking processed items
        min_img_kb: Minimum KB threshold for image extraction
    
    Returns:
        Callable that processes INSERT/UPDATE/DELETE events
    """
    return _ChangeReceiver(stats, min_img_kb)._change_receiver


# =============================================================================
# EMAIL FETCHING
# =============================================================================

def fetch_emails(service_account_info: Dict[str, Any],
                owner_email: str,
                received_after: datetime,
                label_filter: list[str] | None = None,
                max_results: int | None = None,
                create_error_placeholders: bool = False,
                min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB) -> Generator:
    """
    Fetch emails from Gmail using FileProxy pattern.
    
    Args:
        service_account_info: Service account credentials dict
        owner_email: Gmail account to sync
        received_after: Filter emails after this date
        label_filter: Filter by label IDs (e.g., ["INBOX", "IMPORTANT"])
        max_results: Limit number of emails (for testing)
        create_error_placeholders: Create .error.json files for failed fetches
        min_img_kbytes: Minimum size in KB for image extraction
    
    Yields:
        FileProxyBase: Email proxies (attachments via nested_proxies())
    """
    factory = GmailEmailFileProxyFactory(
        service_account_info=service_account_info,
        user_email=owner_email,
        create_error_placeholders=create_error_placeholders,
        min_img_kbytes=min_img_kbytes
    )
    
    # scan_messages() now yields only emails; resync_bulk() handles nested_proxies() intelligently
    return factory.scan_messages(
        received_after=received_after,
        label_ids=label_filter,
        max_results=max_results,
        newest_first=True
    )


# =============================================================================
# SYNCHRONIZATION
# =============================================================================

async def sync_gmail_emails(config: EmailSyncConfig) -> None:
    """Main sync function demonstrating EmailFolderSynchronizer usage with Gmail.
    
    Follows the same pattern as outlook_email_sync.py:
    1. Create CachedFileFolders and CacheGrouping facet
    2. Load/create EmailSyncTimingInfo (state persists between runs)
    3. Define email fetcher function
    4. Define change receiver callback
    5. Create EmailFolderSynchronizer
    6. Run sync loop (single-run or continuous monitoring)
    """
    
    # Step 1: Create cache instance and grouping facet
    cache = CachedFileFolders(
        grouping_pattern=GROUPING_PATTERN,
        root_dir=os.path.abspath(config.cache_root),
        use_xxhash=False
    )
    
    primary_label = config.label_filter[0] if config.label_filter else "INBOX"
    grouping = cache.grouping([config.owner_email, primary_label])
    
    # Step 2: Create/load EmailSyncTimingInfo from grouping slave directory
    slave_dir = grouping.get_slave_dir()
    sync_info = EmailSyncTimingInfo.open(
        str(slave_dir / "sync_timing.yaml"),
        without_lock=False
    )
    
    # Configure timing parameters
    sync_info.retain_days = config.retain_days
    sync_info.new_check_interval_secs = config.new_check_interval
    sync_info.full_check_interval_secs = config.full_check_interval
    
    # Step 3: Create email fetcher function
    def fetch_emails_wrapper(cutoff_datetime: datetime) -> Generator:
        """Wrapper function that adapts factory.scan_messages() to synchronizer interface."""
        return fetch_emails(
            service_account_info=config.service_account_info,
            owner_email=config.owner_email,
            received_after=cutoff_datetime,
            label_filter=config.label_filter,
            max_results=config.max_results,
            create_error_placeholders=True,
            min_img_kbytes=config.min_img_kbytes
        )
    
    # Step 4: Create change receiver
    stats = SyncStats()
    change_receiver = create_change_receiver(stats, config.min_img_kbytes)
    
    # Step 5: Create synchronizer
    synchronizer = EmailFolderSynchronizer(
        sync_info=sync_info,
        cache=grouping,
        email_fetcher=fetch_emails_wrapper,
        upsert_fail_policy=UPSERT_FAIL_POLICY,
        max_consecutive_errors=config.max_consecutive_errors,
        min_cutoff_date=config.received_after
    )
    
    # Step 6: Run sync loop
    print(f"📬 Starting Gmail sync for: {primary_label}")
    print(f"   Owner: {config.owner_email}")
    print(f"   Labels: {config.label_filter or 'ALL'}")
    print(f"   Retain: {config.retain_days} days")
    print(f"   Mode: {'Single run' if config.run_minutes == 0 else f'Continuous for {config.run_minutes} min'}")
    if config.max_results:
        print(f"   Max results: {config.max_results} (testing mode)")
    print(f"   State file: {slave_dir / 'sync_timing.yaml'}")
    print()
    
    try:
        if config.run_minutes == 0:
            # Single sync run
            print("⏱️  Running single sync...")
            result = await synchronizer.sync(change_receiver=change_receiver)
            _print_sync_result(result, stats)
        else:
            # Continuous monitoring loop
            end_time = datetime.now() + timedelta(minutes=config.run_minutes)
            iteration = 0
            
            while datetime.now() < end_time:
                iteration += 1
                print(f"⏱️  Sync iteration {iteration}")
                
                result = await synchronizer.sync(change_receiver=change_receiver)
                _print_sync_result(result, stats)
                
                # Wait before next iteration
                wait = synchronizer.recommended_wait_secs()
                remaining = (end_time - datetime.now()).total_seconds()
                
                if remaining <= 0:
                    break
                
                actual_wait = min(wait, remaining)
                print(f"⏸️  Waiting {actual_wait:.0f}s until next check...")
                print()
                await asyncio.sleep(actual_wait)
            
            print(f"✅ Monitoring complete after {iteration} iteration(s)")
    finally:
        # Clean up: release file lock on state file
        if hasattr(sync_info, 'release_lock'):
            sync_info.release_lock()


def _print_sync_result(result: Dict[str, Any], stats: SyncStats) -> None:
    """Print formatted sync result summary."""
    print()
    print("─" * 60)
    print(f"📊 Sync Result ({result['sync_type'].upper()})")
    print(f"   Processed: {stats.emails_processed} emails, {stats.attachments_processed} attachments")
    print(f"   Changes: {stats.insert_count} inserted, {stats.update_count} updated, {stats.delete_count} deleted")
    print(f"   Duration: {result['duration_secs']:.2f}s")
    if result.get('failures'):
        print(f"   ⚠️  Failures: {len(result['failures'])}")
    print("─" * 60)
    print()


# =============================================================================
# ENVIRONMENT VALIDATION
# =============================================================================

def validate_environment() -> dict:
    """
    Validate required Gmail environment variables are set.
    
    Returns:
        Dict mapping lowercase variable names to their values
        
    Raises:
        SystemExit: If any required variables are missing
    """
    missing = [var for var in REQUIRED_ENVIRONMENT_STRINGS if not os.getenv(var)]
    
    if missing:
        print(f"❌ Missing {len(missing)} required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        print("\nRequired environment variables:", file=sys.stderr)
        for var in REQUIRED_ENVIRONMENT_STRINGS:
            status = "✓" if os.getenv(var) else "✗"
            print(f"  {status} export {var}='<your-value-here>'", file=sys.stderr)
        print("\nSource the credentials file: source volatile/credentials/gmail_access_creds.sh", file=sys.stderr)
        sys.exit(1)
    
    return {var.lower(): os.getenv(var) for var in REQUIRED_ENVIRONMENT_STRINGS}


def build_service_account_info(env_vars: dict) -> dict:
    """
    Build service account JSON structure from environment variables.
    
    Args:
        env_vars: Dict from validate_environment() with lowercase keys
    
    Returns:
        Service account info dict compatible with Google auth library
    """
    # Handle private key - replace literal \n with actual newlines if needed
    private_key = env_vars['gmail_private_key']
    if '\\n' in private_key:
        private_key = private_key.replace('\\n', '\n')
    
    return {
        "type": "service_account",
        "project_id": env_vars['gmail_project_id'],
        "private_key_id": env_vars['gmail_private_key_id'],
        "private_key": private_key,
        "client_email": env_vars['gmail_service_account_email'],
        "client_id": env_vars['gmail_client_id'],
        "auth_uri": env_vars['gmail_auth_uri'],
        "token_uri": env_vars['gmail_token_uri'],
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "universe_domain": "googleapis.com"
    }


# =============================================================================
# CLI
# =============================================================================

@click.command()
@click.option('--cache-root', required=True, type=click.Path(),
              help='Root directory for email cache')
@click.option('--received-after', required=True,
              help='Only sync emails after this date (YYYY-MM-DDTHH:MM:SS)')
@click.option('--labels', default=None,
              help='Comma-separated label IDs to filter (e.g., INBOX,IMPORTANT)')
@click.option('--max-results', type=int, default=None,
              help='Maximum number of emails to fetch (for testing)')
@click.option('--retain-days', type=int, default=90,
              help='Days to retain emails in cache (default: 90)')
@click.option('--new-check-interval', type=int, default=300,
              help='Seconds between lightweight checks (default: 300)')
@click.option('--full-check-interval', type=int, default=3600,
              help='Seconds between full sweeps (default: 3600)')
@click.option('--run-minutes', type=float, default=0,
              help='Minutes to run (0 = single run, default)')
@click.option('--max-consecutive-errors', type=int, default=3,
              help='Max consecutive errors before exit (default: 3)')
@click.option('--min-img-kbytes', type=int, default=DEFAULT_MIN_IMAGE_KB,
              help=f'Min KB for image extraction (default: {DEFAULT_MIN_IMAGE_KB})')
@click.option('--debug', is_flag=True,
              help='Enable debug logging')
def main(cache_root: str, received_after: str, labels: str | None, max_results: int | None,
         retain_days: int, new_check_interval: int, full_check_interval: int,
         run_minutes: float, max_consecutive_errors: int,
         min_img_kbytes: int, debug: bool):
    """Gmail email synchronization with dual-mode caching.
    
    Requires environment variables to be set (source volatile/credentials/gmail_access_creds.sh):
        GMAIL_SERVICE_ACCOUNT_EMAIL, GMAIL_PROJECT_ID, GMAIL_PRIVATE_KEY_ID,
        GMAIL_PRIVATE_KEY, GMAIL_CLIENT_ID, GMAIL_USER_EMAIL,
        GMAIL_TOKEN_URI, GMAIL_AUTH_URI
    """
    
    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    configure_logging(debug)
    
    # Validate environment variables and build service account info
    env_vars = validate_environment()
    service_account_info = build_service_account_info(env_vars)
    owner_email = env_vars['gmail_user_email']
    
    # Parse parameters
    try:
        received_after_dt = datetime.fromisoformat(received_after)
    except ValueError:
        print(f"❌ Invalid date format for --received-after: {received_after}")
        print("   Expected format: YYYY-MM-DDTHH:MM:SS (e.g., 2025-11-01T00:00:00)")
        sys.exit(1)
    
    label_filter = labels.split(',') if labels else None
    
    # Build config
    config = EmailSyncConfig(
        cache_root=cache_root,
        owner_email=owner_email,
        label_filter=label_filter,
        received_after=received_after_dt,
        received_before=None,
        max_results=max_results,
        retain_days=retain_days,
        new_check_interval=new_check_interval,
        full_check_interval=full_check_interval,
        run_minutes=run_minutes,
        max_consecutive_errors=max_consecutive_errors,
        service_account_info=service_account_info,
        min_img_kbytes=min_img_kbytes
    )
    
    # Run sync loop
    asyncio.run(sync_gmail_emails(config))


if __name__ == "__main__":
    main()

