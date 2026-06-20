# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Gmail email proxy implementations for Google Gmail API integration.

This module enables caching of Gmail emails and attachments using the FileProxy pattern.
Emails are stored as .eml files with attachments/embedded content extracted separately, achieving
significant file size reduction while maintaining full data integrity.

## Architecture

The module uses a shared EmailDataHandler pattern to minimize API calls:
- One Gmail API call per email (regardless of attachment count)
- One MIME parse per email (shared across email + attachment proxies)
- Lazy loading triggers only when data is actually needed

## Quick Start

```python
from datetime import datetime, timedelta
import json

# Load service account credentials
with open('service_account.json') as f:
    service_account_info = json.load(f)

factory = GmailEmailFileProxyFactory(
    service_account_info=service_account_info,
    user_email="user@example.com"
)

# Scan recent emails
for proxy in factory.scan_messages(received_after=datetime.now() - timedelta(days=7)):
    match proxy.email_component():
        case "eml":
            print(f"Email from {proxy.sender_email}: {proxy._subject}")
        case "attach":
            print(f"  Attachment: {proxy.file_name()}")
        case "embed":
            print(f"  Embedded: {proxy.file_name()}")
```

## Authentication

This module uses service account authentication (OAuth2 with domain-wide delegation).
Requires `https://www.googleapis.com/auth/gmail.readonly` scope.
See `GmailEmailFileProxyFactory` class docstring for detailed setup instructions.

## Label System

Gmail uses labels instead of folders. Key differences from Outlook/Microsoft 365:
- Each email can have MULTIPLE labels (e.g., ["INBOX", "IMPORTANT", "Label_123"])
- Labels are flat, not hierarchical
- System labels: INBOX, SENT, DRAFT, TRASH, SPAM, STARRED, IMPORTANT, UNREAD
- Custom labels: User-created labels with IDs like "Label_123"

This implementation:
- Determines a "primary label" for grouping (SENT > DRAFT > TRASH > SPAM > INBOX)
- Stores ALL labels in a custom header for change detection
- Includes complete label list in YAML metadata
"""

from typing import Optional, Dict, Any, Iterator, List, Literal
from datetime import datetime
from pathlib import Path
import base64
import json
import hashlib
import logging
import time
import traceback
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pydantic import BaseModel, Field
from totodev_pub.optional_dependencies import raise_missing_dependency

try:
    import yaml
except ImportError:
    yaml = None

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    raise_missing_dependency(
        feature="Gmail support",
        packages=["google-auth", "google-api-python-client"],
        extra="connectors",
    )

from .file_proxy_base import FileProxyBase, OriginMetadata

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Retry and rate limiting constants
MAX_RETRY_ATTEMPTS = 3
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30

# Gmail API pagination
DEFAULT_PAGE_SIZE = 100

# Attachment extraction thresholds
# Small images below this size (in KB) are kept embedded in EML to reduce
# filesystem noise from signature/decoration images
DEFAULT_MIN_IMAGE_KB = 20

# Gmail system label mappings (label ID -> friendly name)
GMAIL_SYSTEM_LABELS = {
    'INBOX': 'Inbox',
    'SENT': 'Sent',
    'DRAFT': 'Drafts',
    'TRASH': 'Trash',
    'SPAM': 'Spam',
    'STARRED': 'Starred',
    'IMPORTANT': 'Important',
    'UNREAD': 'Unread',
    'CHAT': 'Chat',
    'CATEGORY_PERSONAL': 'Personal',
    'CATEGORY_SOCIAL': 'Social',
    'CATEGORY_PROMOTIONS': 'Promotions',
    'CATEGORY_UPDATES': 'Updates',
    'CATEGORY_FORUMS': 'Forums',
}

# Primary label precedence for determining grouping folder
# When an email has multiple labels, use the first matching label from this list
# Order: TRASH > SPAM > DRAFT > SENT > INBOX
# Anything not in this list uses alphabetical sorting
LABEL_PRECEDENCE = ['TRASH', 'SPAM', 'DRAFT', 'SENT', 'INBOX']

# Email header filtering for "slim" mode
# When headers="slim" is specified, ONLY these headers are kept in cached .eml files.
SLIM_MODE_HEADER_WHITELIST = {
    # Core email headers - who, what, when
    'from', 'to', 'cc', 'bcc', 'subject', 'date', 'message-id',
    
    # MIME structure - how content is encoded
    'content-type', 'content-transfer-encoding', 'mime-version',
    
    # Threading and replies - conversation context
    'reply-to', 'in-reply-to', 'references',
    
    # Custom metadata - mutable fields we track
    'x-custom-gmaillabels',  # Label tracking for change detection
}

# Image types subject to size-based filtering
SIZE_RESTRICTED_IMAGE_TYPES = {'.png', '.gif', '.jpg', '.jpeg', '.bmp', '.webp'}


# =============================================================================
# ATTACHMENT FILTERING UTILITIES
# =============================================================================

def _should_extract_attachment(attach_info: Dict[str, Any], min_img_kbytes: int) -> bool:
    """
    Determine if attachment should be extracted to separate file.
    
    Small images (< min_img_kbytes) are kept embedded in EML to reduce
    filesystem noise from signature/decoration images.
    
    Args:
        attach_info: Dict with 'filename', 'size_bytes', 'original_content_type'
        min_img_kbytes: Minimum KB for image extraction (0 = extract all)
    
    Returns:
        True if should extract to file, False if should keep inline in EML
    """
    if min_img_kbytes == 0:
        return True  # Extract everything when threshold is 0
    
    file_ext = Path(attach_info['filename']).suffix.lower()
    content_type = attach_info['original_content_type'].lower()
    size_bytes = attach_info['size_bytes']
    threshold_bytes = min_img_kbytes * 1024
    
    # Check if it's an image by extension OR content-type
    is_image = (file_ext in SIZE_RESTRICTED_IMAGE_TYPES or
                content_type.startswith('image/'))
    
    # Extract if: not an image, or large image
    # Keep inline if: small image
    return not (is_image and size_bytes < threshold_bytes)


# =============================================================================
# PATH AND IDENTIFIER FORMATTING
# =============================================================================

def _sanitize_path_component(text: str, max_length: int = 50) -> str:
    """Sanitize string for safe filesystem use (replaces unsafe chars, truncates)."""
    if not text:
        return "unknown"
    
    unsafe_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t']
    result = text
    for char in unsafe_chars:
        result = result.replace(char, '_')
    
    result = result.strip(' .')[:max_length] if len(result) > max_length else result.strip(' .')
    return result or "unknown"


def _extract_counterparty(email_address: str, max_length: int = 50) -> str:
    """
    Extract short counterparty identifier from email address for filenames.
    
    Truncates domain to first part for brevity in filenames while maintaining readability.
    
    Examples: joe.smith@example.com -> joe.smith@example
              admin@localhost -> admin@localhost
    """
    if not email_address or '@' not in email_address:
        return _sanitize_path_component(email_address or "unknown", max_length)
    
    local, domain = email_address.split('@', 1)
    domain_part = domain.split('.')[0] if '.' in domain else domain
    return _sanitize_path_component(f"{local}@{domain_part}", max_length)


def _hash_msg_id(msg_id: str, length: int = 12) -> str:
    """
    Hash message ID to short identifier.
    
    Uses SHA256 (first 12 hex chars = 48 bits = ~281 trillion possibilities).
    Deterministic and collision-resistant for personal mailbox scale.
    """
    return hashlib.sha256(msg_id.encode('utf-8')).hexdigest()[:length]


def _format_ref_path_email(primary_label: str, received_dt: datetime, 
                           counterparty_email: str, msg_id: str) -> str:
    """
    Format ref_path for email.
    
    Pattern: {date}/{time}_{counterparty}_{hash}.eml
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1.eml
    
    Note: The primary_label parameter is kept for compatibility but not included in ref_path.
    The label should be specified in the CachedFileFolders grouping_pattern instead.
    
    Date/time conversion: If received_dt is timezone-aware (typically UTC from Gmail API),
    it will be converted to local timezone for folder organization.
    """
    # Convert to local timezone if datetime is timezone-aware
    local_dt = received_dt
    if received_dt.tzinfo is not None:
        local_dt = received_dt.astimezone()
    
    return (f"{local_dt.strftime('%Y-%m-%d')}/"
            f"{local_dt.strftime('%H%M%S')}_{_extract_counterparty(counterparty_email)}_{_hash_msg_id(msg_id)}.eml")


def _format_ref_path_attachment(parent_email_ref_path: str, sequence_num: int,
                                original_filename: str, is_embedded_content: bool = False,
                                mime_content_type: Optional[str] = None) -> str:
    """
    Format ref_path for attachment or embedded content.
    
    All attachments and embedded content are stored in a single _files folder with prefixes:
    - Attachments: att{seq:02d}_{filename}
    - Embedded:    emb{seq:02d}_{filename}
    
    Examples:
        2025-11-01/143000_joe.smith@example_a3f2c9b1_files/att01_document.pdf
        2025-11-01/143000_joe.smith@example_a3f2c9b1_files/emb01_meeting.ics
    """
    email_base = parent_email_ref_path[:-4] if parent_email_ref_path.endswith('.eml') else parent_email_ref_path
    
    # Use 3-char prefix: att for attachments, emb for embedded content
    prefix = "emb" if is_embedded_content else "att"
    safe_filename = _sanitize_path_component(original_filename, max_length=200)
    full_filename = f"{prefix}{sequence_num:02d}_{safe_filename}"
    
    return f"{email_base}_files/{full_filename}"


def _get_type_indicator(mime_content_type: str) -> str:
    """Map MIME type to short filename indicator (text/calendar -> cal, text/vcard -> vcard)."""
    type_map = {
        'text/calendar': 'cal', 'application/ics': 'cal',
        'text/vcard': 'vcard', 'text/x-vcard': 'vcard', 'text/directory': 'vcard'
    }
    clean_type = mime_content_type.lower().split(';')[0].strip()
    return type_map.get(clean_type, 'embed')


# =============================================================================
# MIME PART ANALYSIS
# =============================================================================

def _should_extract_mime_part(mime_part) -> tuple[bool, bool]:
    """
    Determine if MIME part should be extracted and how to categorize it.
    
    Returns:
        (should_extract, is_embedded_content)
        
        (True, False): Traditional attachment (Content-Disposition: attachment/inline)
        (True, True):  Embedded content (text/calendar without Content-Disposition)
        (False, False): Message body part (keep in .eml)
    """
    content_disposition = mime_part.get('Content-Disposition', '')
    if content_disposition.startswith(('attachment', 'inline')):
        return (True, False)
    
    extractable_types = {'text/calendar', 'application/ics', 'text/vcard', 'text/x-vcard', 'text/directory'}
    return (True, True) if mime_part.get_content_type() in extractable_types else (False, False)


def _api_call_with_retry(api_func, max_retry_attempts: int = MAX_RETRY_ATTEMPTS):
    """
    Execute Gmail API call with retry logic and rate limiting support.
    
    - Exponential backoff on errors: 1s, 2s, 4s
    - Honors HTTP 429 rate limiting
    - Raises HttpError if all retries fail
    
    Args:
        api_func: Callable that executes the API request (e.g., request.execute)
        max_retry_attempts: Maximum number of retry attempts
    
    Returns:
        API response
    """
    for attempt in range(max_retry_attempts):
        try:
            return api_func()
            
        except HttpError as e:
            # Handle rate limiting - wait and retry
            if e.resp.status == 429:
                wait_seconds = int(e.resp.get('retry-after', DEFAULT_RATE_LIMIT_WAIT_SECONDS))
                logger.warning(f"Rate limited by Gmail API, waiting {wait_seconds}s before retry")
                time.sleep(wait_seconds)
                continue
            
            # Other HTTP errors - retry with backoff
            if attempt < max_retry_attempts - 1:
                backoff_time = 2 ** attempt
                logger.warning(f"Gmail API error on attempt {attempt + 1}/{max_retry_attempts}, "
                              f"waiting {backoff_time}s: {e}", exc_info=True)
                time.sleep(backoff_time)
            else:
                logger.error(f"Gmail API error after {max_retry_attempts} attempts: {e}", exc_info=True)
                raise
                
        except Exception as e:
            if attempt < max_retry_attempts - 1:
                backoff_time = 2 ** attempt
                logger.warning(f"Request failed on attempt {attempt + 1}/{max_retry_attempts}, "
                              f"waiting {backoff_time}s: {e}", exc_info=True)
                time.sleep(backoff_time)
            else:
                logger.error(f"Request failed after {max_retry_attempts} attempts: {e}", exc_info=True)
                raise
    
    raise Exception(f"Failed after {max_retry_attempts} attempts")


# =============================================================================
# LABEL UTILITIES
# =============================================================================

def _sort_labels_by_precedence(label_ids: List[str]) -> List[str]:
    """
    Sort labels using precedence + alphabetical order.
    
    Uses hybrid sorting:
    1. Precedence labels first (TRASH > SPAM > DRAFT > SENT > INBOX)
    2. All other labels alphabetically after precedence labels
    
    This ensures consistent ordering for both primary label determination
    and change detection headers.
    
    Args:
        label_ids: List of Gmail label IDs
    
    Returns:
        Sorted list with precedence labels first, then alphabetical
    
    Examples:
        ['INBOX', 'CATEGORY_UPDATES', 'Label_2'] → ['INBOX', 'CATEGORY_UPDATES', 'Label_2']
        ['SENT', 'INBOX', 'IMPORTANT'] → ['SENT', 'INBOX', 'IMPORTANT']
        ['Label_Zebra', 'Label_Alpha', 'INBOX'] → ['INBOX', 'Label_Alpha', 'Label_Zebra']
    """
    if not label_ids:
        return []
    
    # Separate labels into precedence and non-precedence
    precedence_labels = []
    other_labels = []
    
    for label in label_ids:
        if label in LABEL_PRECEDENCE:
            precedence_labels.append(label)
        else:
            other_labels.append(label)
    
    # Sort precedence labels by precedence order
    precedence_labels.sort(key=lambda x: LABEL_PRECEDENCE.index(x))
    
    # Sort other labels alphabetically
    other_labels.sort()
    
    # Combine: precedence first, then alphabetical
    return precedence_labels + other_labels


def _determine_primary_label(label_ids: List[str]) -> str:
    """
    Determine primary label for grouping from list of label IDs.
    
    Uses the first label from precedence-sorted list.
    
    Args:
        label_ids: List of Gmail label IDs
    
    Returns:
        Primary label ID for grouping (first in sorted order)
    
    Examples:
        ['INBOX', 'CATEGORY_UPDATES'] → 'INBOX' (precedence first)
        ['SENT', 'INBOX'] → 'SENT' (precedence order)
        ['IMPORTANT', 'Label_Projects'] → 'IMPORTANT' (alphabetical)
    """
    if not label_ids:
        return "INBOX"
    
    sorted_labels = _sort_labels_by_precedence(label_ids)
    return sorted_labels[0]


def _format_label_header_value(label_ids: List[str]) -> str:
    """
    Format label IDs for custom header storage.
    
    Uses precedence + alphabetical sorting (same as primary label determination)
    for consistent ordering.
    
    Args:
        label_ids: List of Gmail label IDs
    
    Returns:
        Comma-separated sorted label string (e.g., "INBOX,CATEGORY_UPDATES,Label_123")
    """
    if not label_ids:
        return ''
    
    sorted_labels = _sort_labels_by_precedence(label_ids)
    return ','.join(sorted_labels)


# =============================================================================
# API CLIENT (Internal)
# =============================================================================

class _GmailApiClient:
    """
    Internal API client for Gmail operations.
    
    Provides a mockable interface for Gmail API calls. This class is private
    to the module (underscore prefix) but methods are public within the class
    to enable stubbing in tests via monkey patching.
    """
    
    def __init__(self, service_account_info: Dict[str, Any], user_email: str):
        """
        Initialize Gmail API client with service account credentials.
        
        Args:
            service_account_info: Service account credentials dict (from JSON file)
            user_email: Email address to impersonate (domain-wide delegation)
        """
        self.service_account_info = service_account_info
        self.user_email = user_email
        
        # Create credentials with domain-wide delegation
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/gmail.readonly'],
            subject=user_email
        )
        
        # Build Gmail API service
        self.service = build('gmail', 'v1', credentials=credentials)
    
    def fetch_email_mime(self, msg_id: str) -> bytes:
        """
        Fetch raw MIME content for a single email message.
        
        Args:
            msg_id: Gmail message ID
            
        Returns:
            Raw MIME content as bytes
            
        Raises:
            HttpError: If API call fails after retries
        """
        def _fetch():
            message = self.service.users().messages().get(
                userId='me',
                id=msg_id,
                format='raw'
            ).execute()
            
            # Decode base64url to get raw MIME
            return base64.urlsafe_b64decode(message['raw'])
        
        return _api_call_with_retry(_fetch)
    
    def fetch_message_metadata(self, msg_id: str) -> Dict[str, Any]:
        """
        Fetch message metadata (headers, labels, dates) without body content.
        
        Args:
            msg_id: Gmail message ID
            
        Returns:
            Message metadata dict with 'id', 'threadId', 'labelIds', 'internalDate', 'payload'
            
        Raises:
            HttpError: If API call fails after retries
        """
        def _fetch():
            return self.service.users().messages().get(
                userId='me',
                id=msg_id,
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date', 'Cc', 'Bcc', 'Reply-To', 
                                'Message-ID', 'In-Reply-To', 'References']
            ).execute()
        
        return _api_call_with_retry(_fetch)
    
    def fetch_message_list(self, query_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch a page of message IDs from Gmail API.
        
        Args:
            query_params: Query parameters dict (q, labelIds, maxResults, pageToken, etc.)
            
        Returns:
            JSON response dict with 'messages' array and optional 'nextPageToken'
            
        Raises:
            HttpError: If API call fails after retries
        """
        def _fetch():
            return self.service.users().messages().list(
                userId='me',
                **query_params
            ).execute()
        
        return _api_call_with_retry(_fetch)


# =============================================================================
# EMAIL DATA HANDLER (Internal)
# =============================================================================

class _GmailDataHandler:
    """
    Internal helper managing email retrieval and MIME parsing with lazy loading.
    
    Shared between email proxy and all its attachment proxies to prevent redundant
    API calls and duplicate parsing. Fetches email content from Gmail API only when
    first accessed, then caches in memory.
    
    Key optimizations:
    - Single API call per email (regardless of attachment count)
    - Single MIME parse (reused by all proxies)
    - Lazy loading (doesn't fetch until data is needed)
    """
    
    def __init__(self, msg_id: str, service_account_info: Dict[str, Any], user_email: str,
                 metadata: Optional[Dict[str, Any]] = None, headers: str = "slim"):
        self.msg_id = msg_id
        self.service_account_info = service_account_info
        self.user_email = user_email
        self._metadata = metadata or {}
        self._headers_mode = headers  # "full" or "slim"
        self._api_client = _GmailApiClient(service_account_info, user_email)
        self._raw_email_bytes: Optional[bytes] = None
        self._parsed_mime = None
        self._modified_email_bytes: Optional[bytes] = None
        self._attachments: Optional[List[Dict[str, Any]]] = None
        self._fetch_attempted = False
    
    def _fetch_email_if_needed(self) -> bool:
        """
        Fetch and parse email from Gmail API if not already fetched.
        
        Returns:
            True if email was successfully fetched/cached, False otherwise
        """
        if self._fetch_attempted:
            return self._raw_email_bytes is not None
        
        self._fetch_attempted = True
        
        try:
            self._raw_email_bytes = self._api_client.fetch_email_mime(self.msg_id)
            self._parsed_mime = message_from_bytes(self._raw_email_bytes)
            self._process_attachments()  # Extract and replace with placeholders
            
            logger.debug(f"Successfully fetched email {self.msg_id} ({len(self._raw_email_bytes)} bytes, "
                        f"{len(self._attachments) if self._attachments else 0} attachments)")
            return True
            
        except HttpError as e:
            # Log detailed API error but return False to indicate failure
            logger.error(
                f"Failed to fetch email from Gmail API: {e}. "
                f"Message ID: {self.msg_id}, User: {self.user_email}",
                exc_info=True
            )
            return False
            
        except Exception as e:
            # Unexpected error during parsing - log and return False
            logger.error(
                f"Unexpected error processing email {self.msg_id}: {e}. "
                f"Content size: {len(self._raw_email_bytes) if self._raw_email_bytes else 0} bytes",
                exc_info=True
            )
            return False
    
    def _process_attachments(self):
        """
        Extract attachments and embedded content from email, replacing them with JSON placeholders.
        
        This achieves significant file size reduction while maintaining data integrity.
        
        Process:
        1. Email is fetched from Gmail API in MIME format
        2. Attachments/embedded content extracted and stored as separate files
        3. Original MIME parts replaced with compact JSON placeholders
        4. Modified .eml file saved (dramatically smaller)
        """
        self._attachments = []
        
        if not self._parsed_mime or not self._parsed_mime.is_multipart():
            # Single-part message - inject custom headers and apply filtering
            from email import message_from_bytes
            msg_copy = message_from_bytes(self._raw_email_bytes)
            
            # Inject label header AT THE TOP for faster parsing
            if 'label_ids' in self._metadata:
                self._prepend_header(msg_copy, 'X-Custom-GmailLabels', 
                                   _format_label_header_value(self._metadata['label_ids']))
            
            # Apply header filtering if in slim mode
            if self._headers_mode == "slim":
                self._filter_headers(msg_copy)
            
            self._modified_email_bytes = msg_copy.as_bytes()
            return
        
        sequence = 0
        for part in self._parsed_mime.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            
            if not (extract_result := _should_extract_mime_part(part))[0]:
                continue
                
            sequence += 1
            _, is_embedded = extract_result
            content_type = part.get_content_type()
            
            # Generate appropriate filename based on content type
            if is_embedded:
                filename = ("meeting-invite.ics" if content_type.startswith('text/calendar')
                           else "contact.vcf" if content_type.startswith('text/vcard')
                           else f"embedded-{_get_type_indicator(content_type)}.dat")
            else:
                filename = part.get_filename() or f"attachment_{sequence}"
            
            disposition = part.get('Content-Disposition', '')
            disposition_type = ('embedded' if is_embedded
                              else 'inline' if disposition.startswith('inline')
                              else 'attachment')
            
            payload = part.get_payload(decode=True) or b''
            
            self._attachments.append({
                'sequence_number': sequence,
                'filename': filename,
                'content': payload,
                'original_content_type': content_type,
                'original_content_disposition': disposition_type,
                'original_content_transfer_encoding': part.get('Content-Transfer-Encoding', 'base64'),
                'size_bytes': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest(),
                'is_embedded': is_embedded
            })
            
            logger.debug(f"Extracted {'embedded' if is_embedded else 'attachment'} "
                        f"{sequence}: {filename} ({len(payload)} bytes)")
        
        # Create modified email if we have attachments, we're in slim mode, OR we need to inject label header
        needs_modification = self._attachments or self._headers_mode == "slim" or 'label_ids' in self._metadata
        self._modified_email_bytes = (self._create_modified_email_with_placeholders()
                                     if needs_modification else self._raw_email_bytes)
        
        if self._attachments:
            logger.info(f"Processed email {self.msg_id}: found {len(self._attachments)} "
                       f"attachments/embedded items")
    
    def _prepend_header(self, msg, header_name: str, header_value: str) -> None:
        """
        Prepend a header to the beginning of the message's header list.
        
        This is used to place X-Custom-GmailLabels at the top for faster parsing
        in the looks_same() method, reducing the number of headers that need to be
        read during change detection.
        """
        if hasattr(msg, '_headers'):
            msg._headers.insert(0, (header_name, header_value))
        else:
            # Fallback: just add normally if _headers doesn't exist
            msg[header_name] = header_value
    
    def _filter_headers(self, msg) -> None:
        """
        Filter headers in-place based on headers mode (full/slim).
        
        In slim mode, uses a whitelist approach (SLIM_MODE_HEADER_WHITELIST constant)
        to keep only essential headers for email content processing, removing all 
        authentication, routing, and diagnostic headers.
        """
        if self._headers_mode != "slim":
            return  # Keep all headers in full mode
        
        # Get all current headers (collect keys first to avoid modifying during iteration)
        all_headers = list(msg.keys())
        
        # Remove any header NOT in the whitelist (case-insensitive comparison)
        for header in all_headers:
            if header.lower() not in SLIM_MODE_HEADER_WHITELIST:
                del msg[header]
    
    def _create_modified_email_with_placeholders(self) -> bytes:
        if not self._parsed_mime.is_multipart():
            # Non-multipart email - apply header filtering if in slim mode
            if self._headers_mode == "slim":
                msg_copy = message_from_bytes(self._raw_email_bytes)
                self._filter_headers(msg_copy)
                return msg_copy.as_bytes()
            return self._raw_email_bytes
        
        new_msg = MIMEMultipart()
        
        # Copy all headers from original first
        for header in self._parsed_mime.keys():
            # Add all values for this header (headers can have multiple values)
            for value in self._parsed_mime.get_all(header):
                new_msg[header] = value
        
        # Apply header filtering based on mode
        self._filter_headers(new_msg)
        
        # Inject label header AT THE TOP for faster parsing (AFTER copying headers)
        if 'label_ids' in self._metadata:
            self._prepend_header(new_msg, 'X-Custom-GmailLabels', 
                               _format_label_header_value(self._metadata['label_ids']))
        
        sequence = 0
        for part in self._parsed_mime.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            
            should_extract, _ = _should_extract_mime_part(part)
            
            if should_extract:
                sequence += 1
                if not (attach := next((a for a in self._attachments 
                                       if a['sequence_number'] == sequence), None)):
                    continue
                
                # Create informative placeholder filename for human readability
                type_prefix = "embed" if attach.get('is_embedded') else "attach"
                file_ext = (attach['filename'].rsplit('.', 1)[-1].lower()[:10] 
                           if '.' in attach['filename'] else "dat")
                placeholder_name = f"{type_prefix}-{attach['sequence_number']:02d}-{file_ext}-placeholder.json"
                
                json_part = MIMEApplication(
                    json.dumps({
                        'sequence_number': attach['sequence_number'],
                        'filename': attach['filename'],
                        'original_content_type': attach['original_content_type'],
                        'original_content_disposition': attach['original_content_disposition'],
                        'original_content_transfer_encoding': attach['original_content_transfer_encoding'],
                        'size_bytes': attach['size_bytes'],
                        'sha256': attach['sha256'],
                        'is_embedded': attach.get('is_embedded', False)
                    }, indent=2).encode('utf-8'),
                    'json',
                    _encoder=lambda x: None  # Already UTF-8, don't re-encode
                )
                json_part.add_header('Content-Disposition', 'attachment', filename=placeholder_name)
                json_part.set_charset('utf-8')
                new_msg.attach(json_part)
            else:
                # Keep message body parts (text/plain, text/html)
                new_msg.attach(part)
        
        return new_msg.as_bytes()
    
    def get_email_body(self) -> Optional[bytes]:
        """
        Get email content as .eml format with attachments replaced by JSON placeholders.
        
        Returns None if email fetch fails. The modified email preserves message body and
        headers while replacing binary attachments/embedded content with compact JSON metadata.
        """
        return self._modified_email_bytes if self._fetch_email_if_needed() else None
    
    def get_attachment(self, sequence_number: int) -> Optional[bytes]:
        """
        Get attachment or embedded content by sequence number.
        
        Args:
            sequence_number: 1-based sequence (matches filename prefix)
            
        Returns:
            Raw bytes of attachment/embedded content, or None if not found
        """
        if not self._fetch_email_if_needed():
            return None
        return next((a['content'] for a in (self._attachments or []) 
                    if a['sequence_number'] == sequence_number), None)
    
    def get_metadata(self) -> Dict[str, Any]:
        """
        Get complete email metadata for YAML serialization.
        
        Includes sender, receiver, dates, labels, and attachment details
        (filenames, sizes, hashes). Suitable for writing to eml_metadata.yaml.
        """
        self._fetch_email_if_needed()
        
        metadata = dict(self._metadata)
        metadata['attachment_count'] = len(self._attachments or [])
        
        if self._attachments:
            metadata['attachments'] = [{
                'filename': a['filename'],
                'size_bytes': a['size_bytes'],
                'sha256': a['sha256'],
                'content_type': a['original_content_type'],
                'is_inline': a['original_content_disposition'] == 'inline',
                'is_embedded': a.get('is_embedded', False)
            } for a in self._attachments]
        
        return metadata
    
    def get_attachment_list(self) -> List[Dict[str, Any]]:
        """
        Get list of ALL attachments/embedded content without raw binary data.
        
        Returns all attachments regardless of size or type. Use get_extractable_attachments()
        if you want filtered results based on image size threshold.
        
        Returns:
            List of attachment metadata dicts (unfiltered)
        """
        self._fetch_email_if_needed()
        return [{
            'sequence_number': a['sequence_number'],
            'filename': a['filename'],
            'size_bytes': a['size_bytes'],
            'original_content_type': a['original_content_type'],
            'is_inline': a['original_content_disposition'] == 'inline',
            'is_embedded': a.get('is_embedded', False)
        } for a in (self._attachments or [])]
    
    def get_extractable_attachments(self, min_img_kbytes: int) -> List[Dict[str, Any]]:
        """
        Get list of attachments that should be extracted to separate files.
        
        Filters out small images (< min_img_kbytes) which remain embedded in the EML
        to reduce filesystem noise from signature/decoration images.
        
        Args:
            min_img_kbytes: Minimum KB for image extraction (0 = extract all)
        
        Returns:
            List of attachment metadata dicts (filtered)
        """
        all_attachments = self.get_attachment_list()
        
        if min_img_kbytes == 0:
            return all_attachments  # No filtering
        
        return [att for att in all_attachments 
                if _should_extract_attachment(att, min_img_kbytes)]


# =============================================================================
# METADATA MODELS (Internal)
# =============================================================================

class _EmailMetadata(BaseModel):
    """
    Internal: Configuration for creating a GmailEmailProxy.
    
    Captures all email metadata from Gmail API in a structured format,
    providing validation and making it easy to extend without changing signatures.
    
    This is an internal implementation detail. Users should use 
    GmailEmailProxy.from_gmail_api() or GmailEmailFileProxyFactory instead.
    """
    
    # Identity
    msg_id: str
    thread_id: str
    label_ids: List[str]
    primary_label: str
    
    # People
    sender_email: str
    sender_name: str
    receiver_email: str
    receiver_name: str
    counterparty_email: str
    counterparty_name: str
    
    # Content
    subject: str
    snippet: str
    
    # Timestamps
    received_datetime: datetime
    sent_datetime: datetime
    
    # Gmail-specific metadata
    is_starred: bool = False
    
    # Allow extra fields for forward compatibility
    model_config = {"extra": "allow"}


class _AttachmentMetadata(BaseModel):
    """
    Internal: Configuration for creating a GmailAttachmentProxy.
    
    Captures attachment/embedded content metadata in a structured format.
    
    This is an internal implementation detail. Users should use 
    GmailEmailFileProxyFactory to create attachment proxies.
    """
    
    # Email context
    email_ref_path: str
    primary_label: str
    msg_id: str
    thread_id: str
    label_ids: List[str]
    
    # Attachment identity
    sequence_number: int
    filename: str
    size_bytes: int
    content_type: str
    is_embedded: bool = False
    
    # People (inherited from email)
    sender_email: str
    receiver_email: str
    counterparty_email: str
    
    # Timestamps
    received_datetime: datetime
    
    # Allow extra fields for forward compatibility
    model_config = {"extra": "allow"}


# =============================================================================
# EMAIL PROXY
# =============================================================================

class GmailEmailProxy(FileProxyBase):
    """
    Proxy for Gmail email message stored as .eml file.
    
    Represents a single email with metadata stored in YAML format in the slave_dir.
    Shares a _GmailDataHandler with attachment proxies to avoid redundant API calls.
    
    ## File Organization & ref_path Structure
    
    **Email ref_path pattern:**
    ```
    {date}/{time}_{counterparty}_{hash}.eml
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1.eml
    ```
    
    **Full path with CachedFileFolders grouping:**
    ```
    {owner_email}/{primary_label}/{date}/{time}_{counterparty}_{hash}.eml
    Example: user@gmail.com/Inbox/2025-11-01/143000_joe.smith@example_a3f2c9b1.eml
    ```
    
    **Design rationale:**
    - Owner email and primary label specified in CachedFileFolders grouping_pattern
    - Groups emails by day for efficient browsing
    - Chronological ordering within each day (HHMMSS prefix)
    - Counterparty uses truncated domain for brevity
    - Message ID hash ensures uniqueness (48-bit collision resistance)
    
    **Counterparty Logic:**
    - For "SENT" label: counterparty = receiver email (person you sent to)
    - For other labels: counterparty = sender email (person who sent to you)
    
    ## Public Interface
    
    **Properties:**
        sender_email, receiver_email, counterparty_email, received_datetime,
        attachment_count, label_ids, primary_label, sequence_number (None for emails)
    
    **Gmail-specific methods:**
        email_component() -> Literal["eml", "embed", "attach"]: Returns "eml"
        ref_path_of_email() -> str: Returns this email's ref_path
        email_msg_id() -> str: Returns Gmail message ID
        write_metadata_to_slave_dir(slave_dir): Writes eml_metadata.yaml
    """
    
    def __init__(self, handler: _GmailDataHandler, metadata: _EmailMetadata, min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB):
        """
        Initialize GmailEmailProxy with handler and metadata.
        
        Args:
            handler: Shared data handler for email + attachments
            metadata: Email metadata from Gmail API (internal use only)
            min_img_kbytes: Minimum KB for image extraction (stored for nested_proxies())
        """
        self._handler = handler
        self._primary_label = metadata.primary_label
        self._msg_id = metadata.msg_id
        self._thread_id = metadata.thread_id
        self._label_ids = metadata.label_ids
        self._sender_email = metadata.sender_email
        self._sender_name = metadata.sender_name
        self._receiver_email = metadata.receiver_email
        self._receiver_name = metadata.receiver_name
        self._counterparty_email = metadata.counterparty_email
        self._counterparty_name = metadata.counterparty_name
        self._subject = metadata.subject
        self._snippet = metadata.snippet
        self._received_datetime = metadata.received_datetime
        self._sent_datetime = metadata.sent_datetime
        self._is_starred = metadata.is_starred
        self._extra_metadata = metadata.model_extra or {}
        self._min_img_kbytes = min_img_kbytes
        self._ref_path = _format_ref_path_email(
            metadata.primary_label, metadata.received_datetime, 
            metadata.counterparty_email, metadata.msg_id
        )
    
    @classmethod
    def from_gmail_api(cls, gmail_api_message: Dict[str, Any], service_account_info: Dict[str, Any],
                       user_email: str, headers: str = "slim", min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB) -> 'GmailEmailProxy':
        """
        Create GmailEmailProxy from Gmail API message dict.
        
        Parses the Gmail API response structure and extracts all necessary metadata
        to create a properly configured email proxy with shared data handler.
        
        Args:
            gmail_api_message: Raw dict from Gmail API messages.list endpoint
                             (must include 'id', 'threadId', 'labelIds', 'internalDate', 'payload')
            service_account_info: Service account credentials dict
            user_email: User's email address for API calls
            headers: Header mode - "full" or "slim"
            
        Returns:
            Configured GmailEmailProxy instance
            
        Raises:
            KeyError: If required fields are missing from Gmail API response
            ValueError: If datetime fields have invalid format
        """
        try:
            # Extract basic fields
            msg_id = gmail_api_message['id']
            thread_id = gmail_api_message['threadId']
            label_ids = gmail_api_message.get('labelIds', [])
            
            # Parse headers from payload
            headers_dict = {}
            if 'payload' in gmail_api_message and 'headers' in gmail_api_message['payload']:
                headers_dict = {h['name'].lower(): h['value'] 
                               for h in gmail_api_message['payload']['headers']}
            
            subject = headers_dict.get('subject', '(No Subject)')
            snippet = gmail_api_message.get('snippet', '')
            
            # Parse sender from headers
            from_header = headers_dict.get('from', 'unknown@unknown.com')
            sender_email, sender_name = _parse_email_header(from_header)
            
            # Parse primary receiver from headers
            to_header = headers_dict.get('to', 'unknown@unknown.com')
            receiver_email, receiver_name = _parse_email_header(to_header)
            
            # Determine primary label and counterparty
            primary_label = _determine_primary_label(label_ids)
            is_sent = primary_label == 'SENT'
            counterparty_email = receiver_email if is_sent else sender_email
            counterparty_name = receiver_name if is_sent else sender_name
            
            # Parse timestamp (Gmail uses epoch milliseconds)
            internal_date_ms = int(gmail_api_message.get('internalDate', '0'))
            received_dt = datetime.fromtimestamp(internal_date_ms / 1000.0, tz=None)
            
            # Sent date from headers (may not exist)
            date_header = headers_dict.get('date', '')
            try:
                from email.utils import parsedate_to_datetime
                sent_dt = parsedate_to_datetime(date_header) if date_header else received_dt
            except Exception:
                sent_dt = received_dt
            
            # Check if starred
            is_starred = 'STARRED' in label_ids
            
            logger.debug(f"Parsing email from Gmail API: {msg_id}, subject='{subject[:50]}'")
            
        except KeyError as e:
            logger.error(
                f"Missing required field {e} in Gmail API message. "
                f"Available keys: {list(gmail_api_message.keys())}. "
                f"User: {user_email}",
                exc_info=True
            )
            raise
            
        except (ValueError, TypeError) as e:
            logger.error(
                f"Failed to parse Gmail API message data: {e}. "
                f"internalDate={gmail_api_message.get('internalDate')}, "
                f"msg_id={gmail_api_message.get('id', 'unknown')}",
                exc_info=True
            )
            raise
        
        # Create metadata object
        metadata = _EmailMetadata(
            msg_id=msg_id,
            thread_id=thread_id,
            label_ids=label_ids,
            primary_label=primary_label,
            sender_email=sender_email,
            sender_name=sender_name,
            receiver_email=receiver_email,
            receiver_name=receiver_name,
            counterparty_email=counterparty_email,
            counterparty_name=counterparty_name,
            subject=subject,
            snippet=snippet,
            received_datetime=received_dt,
            sent_datetime=sent_dt,
            is_starred=is_starred
        )
        
        # Create shared handler for email + attachments
        handler = _GmailDataHandler(
            msg_id=msg_id, service_account_info=service_account_info, user_email=user_email,
            metadata={'subject': subject, 'sender_email': sender_email, 'sender_name': sender_name,
                     'receiver_email': receiver_email, 'receiver_name': receiver_name,
                     'label_ids': label_ids},
            headers=headers
        )
        
        return cls(handler=handler, metadata=metadata, min_img_kbytes=min_img_kbytes)
    
    # Properties (simple getters)
    sender_email = property(lambda self: self._sender_email)
    receiver_email = property(lambda self: self._receiver_email)
    counterparty_email = property(lambda self: self._counterparty_email)
    received_datetime = property(lambda self: self._received_datetime)
    attachment_count = property(lambda self: len(self._handler.get_attachment_list()))
    label_ids = property(lambda self: self._label_ids)
    primary_label = property(lambda self: self._primary_label)
    sequence_number = property(lambda self: None)
    
    # FileProxyBase abstract methods
    def ref_path(self) -> str:
        """Return the ref_path (includes .eml extension)."""
        return self._ref_path
    
    def file_name(self) -> str:
        """Return just the filename portion of ref_path."""
        return self._ref_path.split('/')[-1]
    
    def deploy(self, target_dir: str) -> None:
        """
        Write .eml file to target directory.
        
        The email content has attachments/embedded content replaced with JSON placeholders.
        Actual metadata (YAML) should be written separately via write_metadata_to_slave_dir().
        
        Raises:
            RuntimeError: If email content cannot be fetched from handler
            IOError/OSError: If file cannot be written to disk
        """
        email_content = self._handler.get_email_body()
        
        if not email_content:
            logger.error(
                f"Handler returned None for email body. "
                f"Message ID: {self._msg_id}, Primary Label: {self._primary_label}, "
                f"Sender: {self._sender_email}, Subject: {self._subject[:100]}"
            )
            raise RuntimeError(
                f"Failed to fetch email content for message {self._msg_id} in {self._primary_label}. "
                f"Handler returned None - check logs for underlying cause."
            )
        
        try:
            target_path = Path(target_dir) / self.file_name()
            target_path.write_bytes(email_content)
            logger.debug(f"Deployed email to {target_path} ({len(email_content)} bytes)")
        except (IOError, OSError) as e:
            logger.error(
                f"Failed to write email file to {target_path}: {e}. "
                f"Target dir exists: {Path(target_dir).exists()}, "
                f"Content size: {len(email_content)} bytes",
                exc_info=True
            )
            raise
    
    def looks_same(self, cached_file_path: str) -> Optional[bool]:
        """
        Quick comparison: file exists + label list matches.
        
        Gmail emails are immutable in content, but labels can be changed by users.
        Since cached_file_path is derived from ref_path() which includes the msg_id hash,
        if the file exists at this path, the message ID already matches by definition.
        We only need to check the label list.
        
        ## Label Tracking
        
        Gmail's multi-label system means labels can be added/removed over time.
        To detect these changes efficiently:
        
        **X-Custom-GmailLabels Header:**
        - Injected into every cached .eml file as the **first header** for fast parsing
        - Contains comma-separated sorted label IDs: "IMPORTANT,INBOX,Label_123"
        - Preserved in both "full" and "slim" header modes
        - Enables detection of label changes during resync
        
        **Optimization:**
        - File existence check confirms message ID matches (via ref_path)
        - Parses only the first header (X-Custom-GmailLabels) for label changes
        - Returns False if label list differs, triggering re-cache
        - Fast validation for unchanged emails
        
        Returns:
            True: File exists AND labels match
            False: File doesn't exist OR label list differs
            None: Can't determine (parse error, etc.)
        """
        cached_path = Path(cached_file_path)
        if not cached_path.exists():
            return False
        
        # File exists at this path means message ID matches (ref_path includes hash)
        # Only need to check label list
        try:
            cached_msg = message_from_bytes(cached_path.read_bytes())
            
            # Check label list
            cached_labels = cached_msg.get('X-Custom-GmailLabels', '')
            current_labels = _format_label_header_value(self._label_ids)
            
            if cached_labels != current_labels:
                logger.debug(
                    f"Labels changed for {self._msg_id}: "
                    f"{cached_labels} -> {current_labels}"
                )
                return False
            
            # File exists and labels match
            return True
            
        except Exception as e:
            # Couldn't parse the cached file - treat as unknown
            logger.warning(
                f"Failed to parse cached EML headers at {cached_file_path}: {e}. "
                f"Treating as unknown (will likely re-cache)."
            )
            return None  # Can't determine
    
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """
        Fetch email from Gmail API.
        
        Returns True if successful, False otherwise. Uses lazy loading from shared handler.
        """
        try:
            return self._handler.get_email_body() is not None
        except Exception as e:
            logger.error(f"Failed to materialize email {self._msg_id}: {e}")
            return False
    
    def get_context_info(self) -> Dict[str, Any]:
        """Return safe logging context (no sensitive data like passwords)."""
        return {
            'type': 'email', 'msg_id': self._msg_id, 'thread_id': self._thread_id,
            'primary_label': self._primary_label, 'label_ids': self._label_ids,
            'subject': self._subject, 'sender': self._sender_email,
            'receiver': self._receiver_email, 'received': self._received_datetime.isoformat(),
            'ref_path': self._ref_path
        }

    def retrieval_hint(self) -> Dict[str, Any]:
        """Record the Gmail message coordinates needed to re-fetch this email later.

        The serialized .eml size is not known until the body is fetched and the
        placeholder-rewriting runs, so peek_metadata() is left at its default
        (None) for emails; this hint still records how to retrieve the original.
        """
        return {"source": "gmail", "kind": "email", "msg_id": self._msg_id, "thread_id": self._thread_id}
    
    def nested_proxies(self) -> Iterator[FileProxyBase]:
        """
        Yield attachment proxies for this email.
        
        This generator lazily fetches the email body and extracts attachment metadata
        only when called. This enables efficient caching - if the email is already cached
        and looks_same() returns True, this method is never called and the body is not fetched.
        
        The generator is NOT replayable - calling it multiple times will trigger repeated
        email body fetches. If callers need to iterate over attachments multiple times,
        they should materialize the results into a list.
        
        Yields:
            GmailAttachmentProxy: Attachment proxies for this email's attachments
        """
        # Fetch email body and extract attachment list (lazy loading)
        attachment_list = self._handler.get_extractable_attachments(
            min_img_kbytes=self._min_img_kbytes
        )
        
        for attach_info in attachment_list:
            metadata = _AttachmentMetadata(
                email_ref_path=self._ref_path,
                primary_label=self._primary_label,
                msg_id=self._msg_id,
                thread_id=self._thread_id,
                label_ids=self._label_ids,
                sequence_number=attach_info['sequence_number'],
                filename=attach_info['filename'],
                size_bytes=attach_info['size_bytes'],
                content_type=attach_info['original_content_type'],
                is_embedded=attach_info.get('is_embedded', False),
                sender_email=self._sender_email,
                receiver_email=self._receiver_email,
                counterparty_email=self._counterparty_email,
                received_datetime=self._received_datetime
            )
            yield GmailAttachmentProxy(
                handler=self._handler,
                metadata=metadata
            )
    
    # Gmail-specific interface
    def email_component(self) -> Literal["eml", "embed", "attach"]:
        """Return the type of email component this proxy represents."""
        return "eml"
    
    def ref_path_of_email(self) -> str:
        """Return this email's ref_path."""
        return self._ref_path
    
    def email_msg_id(self) -> str:
        """Return Gmail message ID."""
        return self._msg_id
    
    def write_metadata_to_slave_dir(self, slave_dir: Path):
        """
        Write eml_metadata.yaml to slave directory.
        
        Creates comprehensive YAML metadata file with sender/receiver info, dates,
        labels, and attachment details. This is the primary metadata store for
        cached emails.
        
        Raises:
            ImportError: If PyYAML is not installed
            IOError/OSError: If metadata file cannot be written
        """
        if yaml is None:
            raise_missing_dependency(
                feature="YAML metadata export",
                packages=["pyyaml"],
            )
        
        # Get friendly label names
        label_names = [GMAIL_SYSTEM_LABELS.get(lid, lid) for lid in self._label_ids]
        
        metadata = {
            'msg_id': self._msg_id, 'thread_id': self._thread_id,
            'subject': self._subject, 'snippet': self._snippet,
            'sender_email': self._sender_email, 'sender_name': self._sender_name,
            'receiver_email': self._receiver_email, 'receiver_name': self._receiver_name,
            'counterparty_email': self._counterparty_email, 'counterparty_name': self._counterparty_name,
            'to_recipients': [{'email': self._receiver_email, 'name': self._receiver_name}],
            'cc_recipients': [], 'bcc_recipients': [],
            'received_datetime': self._received_datetime.isoformat(),
            'sent_datetime': self._sent_datetime.isoformat(),
            'primary_label': self._primary_label,
            'label_ids': self._label_ids,
            'label_names': label_names,
            'is_starred': self._is_starred,
            'attachment_count': self.attachment_count,
            'attachments': self._handler.get_attachment_list(),
            **self._extra_metadata
        }
        
        yaml_path = slave_dir / "eml_metadata.yaml"
        
        try:
            with open(yaml_path, 'w', encoding='utf-8') as f:
                yaml.dump(metadata, f, default_flow_style=False, sort_keys=False)
            logger.debug(f"Wrote metadata to {yaml_path} ({len(metadata)} fields)")
        except (IOError, OSError) as e:
            logger.error(
                f"Failed to write metadata file {yaml_path}: {e}. "
                f"Slave dir exists: {slave_dir.exists()}, "
                f"Metadata keys: {list(metadata.keys())}",
                exc_info=True
            )
            raise


def _parse_email_header(header_value: str) -> tuple[str, str]:
    """
    Parse email header (From/To) into email and name.
    
    Examples:
        "John Doe <john@example.com>" -> ("john@example.com", "John Doe")
        "john@example.com" -> ("john@example.com", "john@example.com")
    """
    from email.utils import parseaddr
    name, email = parseaddr(header_value)
    return email or "unknown@unknown.com", name or email or "Unknown"


# =============================================================================
# ATTACHMENT PROXY
# =============================================================================

class GmailAttachmentProxy(FileProxyBase):
    """
    Proxy for email attachment or embedded content.
    
    Represents either:
    - Traditional attachment (Content-Disposition: attachment/inline)
    - Embedded content (text/calendar meeting invite, text/vcard contact, etc.)
    
    Shares _GmailDataHandler with parent email to avoid redundant API calls.
    
    ## File Organization
    
    **Attachment ref_path pattern:**
    ```
    {email_base}_files/att{seq:02d}_{filename}
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1_files/att01_document.pdf
    ```
    
    **Embedded content ref_path pattern:**
    ```
    {email_base}_files/emb{seq:02d}_{filename}
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1_files/emb01_meeting.ics
    ```
    
    ## Public Interface
    
    **Properties:**
        sender_email, receiver_email, counterparty_email, received_datetime,
        attachment_count, label_ids, primary_label, sequence_number (1-based)
    
    **Gmail-specific methods:**
        email_component() -> Literal["eml", "embed", "attach"]: Returns "attach" or "embed"
        ref_path_of_email() -> str: Returns parent email's ref_path
        email_msg_id() -> str: Returns Gmail message ID
    """
    
    def __init__(self, handler: _GmailDataHandler, metadata: _AttachmentMetadata):
        """
        Initialize GmailAttachmentProxy with handler and metadata.
        
        Args:
            handler: Shared data handler for email + attachments
            metadata: Attachment metadata (internal use only)
        """
        self._handler = handler
        self._email_ref_path = metadata.email_ref_path
        self._sequence_number = metadata.sequence_number
        self._filename = metadata.filename
        self._size_bytes = metadata.size_bytes
        self._content_type = metadata.content_type
        self._is_embedded = metadata.is_embedded
        self._primary_label = metadata.primary_label
        self._msg_id = metadata.msg_id
        self._thread_id = metadata.thread_id
        self._label_ids = metadata.label_ids
        self._sender_email = metadata.sender_email
        self._receiver_email = metadata.receiver_email
        self._counterparty_email = metadata.counterparty_email
        self._received_datetime = metadata.received_datetime
        self._extra_metadata = metadata.model_extra or {}
        self._ref_path = _format_ref_path_attachment(
            metadata.email_ref_path, metadata.sequence_number, metadata.filename,
            metadata.is_embedded, metadata.content_type
        )
    
    # Properties (simple getters)
    sender_email = property(lambda self: self._sender_email)
    receiver_email = property(lambda self: self._receiver_email)
    counterparty_email = property(lambda self: self._counterparty_email)
    received_datetime = property(lambda self: self._received_datetime)
    attachment_count = property(lambda self: len(self._handler.get_attachment_list()))
    label_ids = property(lambda self: self._label_ids)
    primary_label = property(lambda self: self._primary_label)
    sequence_number = property(lambda self: self._sequence_number)
    
    # FileProxyBase abstract methods
    def ref_path(self) -> str:
        """Return the ref_path for this attachment."""
        return self._ref_path
    
    def file_name(self) -> str:
        """Return just the filename portion."""
        return self._ref_path.split('/')[-1]
    
    def deploy(self, target_dir: str) -> None:
        """Write attachment/embedded content file to target directory."""
        if not (content := self._handler.get_attachment(self._sequence_number)):
            raise RuntimeError(f"Failed to fetch attachment {self._sequence_number} for {self._msg_id}")
        
        (Path(target_dir) / self.file_name()).write_bytes(content)
        logger.debug(f"Deployed attachment to {Path(target_dir) / self.file_name()}")
    
    def looks_same(self, cached_file_path: str) -> Optional[bool]:
        """
        Quick comparison using file size.
        
        Attachments are immutable when parent email is immutable, so size match is reliable.
        """
        cached_path = Path(cached_file_path)
        return cached_path.stat().st_size == self._size_bytes if cached_path.exists() else False
    
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """
        Fetch attachment from Gmail API.
        
        Returns True if successful, False otherwise. Uses lazy loading from shared handler.
        """
        try:
            return self._handler.get_attachment(self._sequence_number) is not None
        except Exception as e:
            logger.error(f"Failed to materialize attachment {self._sequence_number} for {self._msg_id}: {e}")
            return False
    
    async def peek_metadata(self) -> Optional[OriginMetadata]:
        """Report the attachment size cheaply (known from email metadata).

        Attachment size is provided by the Gmail metadata without downloading the
        bytes. mtime is left None (attachments don't carry an independent
        modification time; the parent email's received time is not the file's).
        """
        return OriginMetadata(size=self._size_bytes)

    def retrieval_hint(self) -> Dict[str, Any]:
        return {
            "source": "gmail",
            "kind": "attachment",
            "msg_id": self._msg_id,
            "sequence_number": self._sequence_number,
        }

    def get_context_info(self) -> Dict[str, Any]:
        """Return safe logging context (no sensitive data)."""
        return {
            'type': 'attachment', 'msg_id': self._msg_id, 'sequence': self._sequence_number,
            'filename': self._filename, 'size_bytes': self._size_bytes,
            'email_ref_path': self._email_ref_path, 'ref_path': self._ref_path
        }
    
    # Gmail-specific interface
    def email_component(self) -> Literal["eml", "embed", "attach"]:
        """Return the type of email component this proxy represents."""
        return "embed" if self._is_embedded else "attach"
    
    def ref_path_of_email(self) -> str:
        """Return parent email's ref_path."""
        return self._email_ref_path
    
    def email_msg_id(self) -> str:
        """Return Gmail message ID."""
        return self._msg_id


# =============================================================================
# ERROR PROXY
# =============================================================================

class GmailEmailErrorProxy(FileProxyBase):
    """
    Proxy representing a failed email fetch with error details preserved as a file.
    
    When email fetching fails (network errors, API errors, parsing errors), this proxy
    creates an .error.json file containing full error details, traceback, and metadata.
    This makes failures visible in the filesystem and enables intelligent retry logic.
    
    File structure (ref_path only, prepend with grouping pattern):
        2025-11-01/143000_joe.smith@example_error_a3f2c9b1.json
    
    The error file contains:
        - Full exception type and message
        - Complete stack trace
        - Email metadata (subject, sender, etc.)
        - Timestamp for retry logic
        - Can-retry flag for intelligent handling
    """
    
    def __init__(self, error_info: Dict[str, Any], email_metadata: Dict[str, Any], 
                 base_ref_path: str):
        """
        Initialize error proxy.
        
        Args:
            error_info: Dict with error_type, error_message, traceback, timestamp, can_retry
            email_metadata: Dict with msg_id, subject, sender_email, primary_label, etc.
            base_ref_path: Base ref_path that would have been used for successful email
        """
        self._error_info = error_info
        self._email_metadata = email_metadata
        # Replace .eml with .error.json
        self._ref_path = base_ref_path.replace('.eml', '.error.json')
    
    def ref_path(self) -> str:
        """Return the ref_path (.error.json file)."""
        return self._ref_path
    
    def file_name(self) -> str:
        """Return just the filename portion."""
        return self._ref_path.split('/')[-1]
    
    def deploy(self, target_dir: str) -> None:
        """Write error details as JSON file."""
        error_doc = {
            **self._error_info,
            'email_metadata': self._email_metadata,
            'note': 'This file represents a failed email fetch. See error_type and traceback for details.'
        }
        
        target_path = Path(target_dir) / self.file_name()
        target_path.write_text(json.dumps(error_doc, indent=2, default=str), encoding='utf-8')
        logger.debug(f"Deployed error placeholder to {target_path}")
    
    def looks_same(self, cached_file_path: str) -> Optional[bool]:
        """Error files should be re-attempted on next sync."""
        return False  # Always consider different to trigger retry
    
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """Error placeholders are already 'materialized' - just return True."""
        return True
    
    def get_context_info(self) -> Dict[str, Any]:
        """Return safe logging context."""
        return {
            'type': 'error',
            'msg_id': self._email_metadata.get('msg_id', 'unknown'),
            'error_type': self._error_info.get('error_type', 'unknown'),
            'ref_path': self._ref_path
        }
    
    # Gmail-specific interface
    def email_component(self) -> Literal["eml", "embed", "attach", "error"]:
        """Return the type of email component this proxy represents."""
        return "error"
    
    def ref_path_of_email(self) -> str:
        """Return this error's ref_path."""
        return self._ref_path
    
    def email_msg_id(self) -> str:
        """Return Gmail message ID if available."""
        return self._email_metadata.get('msg_id', 'unknown')


# =============================================================================
# FACTORY
# =============================================================================

class GmailEmailFileProxyFactory:
    """
    Factory for discovering and creating email/attachment proxies from Gmail.
    
    Main entry point for working with Gmail emails. Scans mailboxes using Gmail API
    and creates FileProxy objects for emails, attachments, and embedded content.
    
    ## Authentication & Permissions
    
    This factory uses **service account authentication** (OAuth2 with domain-wide delegation):
    - No user sign-in required - perfect for automated workflows and cron jobs
    - Requires service account credentials JSON file
    - Must have domain-wide delegation enabled
    - Requires `https://www.googleapis.com/auth/gmail.readonly` scope
    
    **Setup Steps:**
    1. Create service account in Google Cloud Console
    2. Download service account JSON credentials
    3. Enable domain-wide delegation for the service account
    4. Grant `https://www.googleapis.com/auth/gmail.readonly` scope
    5. Use the user's email address when creating the factory
    
    ## Usage Pattern
    
    1. Create factory with service_account_info and user_email
    2. Call scan_messages() with filters
    3. Iterate through returned proxies (emails first, then their attachments)
    4. Use with CachedFileFolders.resync_bulk() for automatic caching
    
    Example:
        with open('service_account.json') as f:
            service_account_info = json.load(f)
        
        factory = GmailEmailFileProxyFactory(
            service_account_info=service_account_info,
            user_email="user@example.com"
        )
        
        for proxy in factory.scan_messages(received_after=datetime(2025, 1, 1)):
            print(proxy.file_name())
    """
    
    def __init__(self, service_account_info: Dict[str, Any], user_email: str,
                 create_error_placeholders: bool = False,
                 min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB):
        """
        Initialize factory.
        
        Args:
            service_account_info: Service account credentials dict (from JSON file)
            user_email: User's email address for mailbox access
            create_error_placeholders: If True, create .error.json files when email
                fetching fails instead of silently skipping. Useful for debugging
                and enabling retry logic.
            min_img_kbytes: Minimum size in kilobytes for images to be extracted as
                separate attachment files. Images smaller than this (typically signature
                or decoration images) remain embedded in the EML file. Only applies to
                image types in SIZE_RESTRICTED_IMAGE_TYPES. Set to 0 to extract all images.
                Defaults to DEFAULT_MIN_IMAGE_KB constant.
        
        Raises:
            ValueError: If user_email or service_account_info are invalid
        """
        # Validate inputs
        if not user_email or '@' not in user_email:
            raise ValueError(f"Invalid user_email: {user_email!r}. Must be a valid email address.")
        if not service_account_info or 'private_key' not in service_account_info:
            raise ValueError("Invalid service_account_info. Must include 'private_key' field.")
        
        self.service_account_info = service_account_info
        self.user_email = user_email
        self.create_error_placeholders = create_error_placeholders
        self.min_img_kbytes = min_img_kbytes
        self._api_client = _GmailApiClient(service_account_info, user_email)
        
        logger.debug(f"Initialized GmailEmailFileProxyFactory for {user_email} "
                    f"(error_placeholders={'enabled' if create_error_placeholders else 'disabled'}, "
                    f"min_img_kbytes={min_img_kbytes})")
    
    def scan_messages(self, received_after: datetime,
                     label_ids: Optional[List[str]] = None,
                     received_before: Optional[datetime] = None,
                     from_address: Optional[str] = None,
                     subject_contains: Optional[str] = None,
                     has_attachment: Optional[bool] = None,
                     max_results: Optional[int] = None,
                     newest_first: bool = True,
                     headers: str = "slim"
                     ) -> Iterator[FileProxyBase]:
        """
        Scan mailbox and yield email proxies with filtering.
        
        Returns emails in chronological order (newest first by default). This supports
        incremental caching: iterate until encountering previously cached emails, then stop.
        
        Yields only GmailEmailProxy objects (email messages). To get attachments for each
        email, call the email_proxy.nested_proxies() method, which returns a generator of
        GmailAttachmentProxy objects. This lazy loading pattern enables efficient caching -
        attachments are only fetched when needed.
        
        Args:
            received_after: REQUIRED - Only emails received after this datetime (prevents
                           scanning entire mailbox)
            label_ids: Filter by label IDs (e.g., ["INBOX", "IMPORTANT"]). If None, searches all labels.
            received_before: Optional upper datetime bound
            from_address: Filter by exact sender email
            subject_contains: Filter by substring in subject (case-insensitive)
            has_attachment: Filter by attachment presence (True/False/None for both)
            max_results: Limit number of emails returned (useful for testing)
            newest_first: True=newest first (default), False=oldest first
            headers: Header mode for .eml files - "slim" (default) removes authentication, routing,
                    and diagnostic headers for cleaner files, "full" keeps all headers
            
        Yields:
            GmailEmailProxy: Email message proxies (attachments via nested_proxies())
            
        Examples:
            # Simple scan - emails only
            for email_proxy in factory.scan_messages(
                received_after=datetime(2025, 1, 1),
                label_ids=["INBOX"]
            ):
                print(f"Email: {email_proxy._subject}")
            
            # With attachments using nested_proxies()
            for email_proxy in factory.scan_messages(
                received_after=datetime(2025, 1, 1),
                label_ids=["INBOX", "IMPORTANT"],
                from_address="boss@company.com",
                has_attachment=True,
                max_results=10
            ):
                print(f"Important email from boss: {email_proxy._subject}")
                for attach_proxy in email_proxy.nested_proxies():
                    print(f"  Attachment: {attach_proxy.file_name()}")
        """
        # Build Gmail query string
        query_parts = []
        
        # Date filtering (required)
        after_str = received_after.strftime('%Y/%m/%d')
        query_parts.append(f"after:{after_str}")
        
        if received_before:
            before_str = received_before.strftime('%Y/%m/%d')
            query_parts.append(f"before:{before_str}")
        
        # Sender filtering
        if from_address:
            query_parts.append(f"from:{from_address}")
        
        # Subject filtering
        if subject_contains:
            # Escape quotes in subject
            escaped_subject = subject_contains.replace('"', '\\"')
            query_parts.append(f'subject:"{escaped_subject}"')
        
        # Attachment filtering
        if has_attachment is True:
            query_parts.append("has:attachment")
        elif has_attachment is False:
            query_parts.append("-has:attachment")
        
        query_string = ' '.join(query_parts) if query_parts else None
        
        # Build API query parameters
        query_params = {
            'maxResults': min(DEFAULT_PAGE_SIZE, max_results) if max_results else DEFAULT_PAGE_SIZE,
        }
        
        if query_string:
            query_params['q'] = query_string
        
        if label_ids:
            query_params['labelIds'] = label_ids
        
        emails_yielded = 0
        errors_encountered = 0
        
        logger.info(f"Starting scan of mailbox for {self.user_email} "
                   f"(received_after={received_after}, label_ids={label_ids}, max_results={max_results})")
        
        # Pagination loop (follows nextPageToken)
        next_page_token = None
        while True:
            if max_results and emails_yielded >= max_results:
                break
            
            if next_page_token:
                query_params['pageToken'] = next_page_token
            
            try:
                response_data = self._api_client.fetch_message_list(query_params)
                messages = response_data.get('messages', [])
                
                if not messages:
                    logger.debug("No more messages found")
                    break
                
                logger.debug(f"Fetched {len(messages)} message IDs from page")
                
                # Sort by date if needed (API returns chronological by default)
                if not newest_first:
                    messages = list(reversed(messages))
                
                for msg_basic in messages:
                    if max_results and emails_yielded >= max_results:
                        break
                    
                    msg_id = msg_basic['id']
                    
                    try:
                        # Fetch full message metadata
                        msg_data = self._api_client.fetch_message_metadata(msg_id)
                        
                        # Create and yield email proxy
                        # Attachments are accessed via email_proxy.nested_proxies()
                        email_proxy = GmailEmailProxy.from_gmail_api(
                            gmail_api_message=msg_data,
                            service_account_info=self.service_account_info,
                            user_email=self.user_email,
                            headers=headers,
                            min_img_kbytes=self.min_img_kbytes
                        )
                        yield email_proxy
                        emails_yielded += 1
                                    
                    except Exception as e:
                        # Single email proxy creation failed
                        errors_encountered += 1
                        logger.error(
                            f"Failed to process message {msg_id}: {e}",
                            exc_info=True
                        )
                        
                        if self.create_error_placeholders:
                            # Create error placeholder proxy
                            try:
                                # Try to construct a base ref_path for the error file
                                try:
                                    # Get basic message info for error proxy
                                    msg_data_basic = msg_basic
                                    received_dt = datetime.now()  # Fallback
                                    
                                    # Try to get metadata if available
                                    if 'internalDate' in msg_data_basic:
                                        internal_date_ms = int(msg_data_basic['internalDate'])
                                        received_dt = datetime.fromtimestamp(internal_date_ms / 1000.0)
                                except Exception:
                                    received_dt = datetime.now()
                                
                                counterparty = "unknown"
                                primary_label = _determine_primary_label(label_ids or ["INBOX"])
                                
                                base_ref_path = _format_ref_path_email(
                                    primary_label, received_dt, counterparty, msg_id
                                )
                                
                                error_proxy = GmailEmailErrorProxy(
                                    error_info={
                                        'error': True,
                                        'error_type': type(e).__name__,
                                        'error_message': str(e),
                                        'timestamp': datetime.now().isoformat(),
                                        'traceback': traceback.format_exc(),
                                        'can_retry': isinstance(e, (HttpError, ConnectionError))
                                    },
                                    email_metadata={
                                        'msg_id': msg_id,
                                        'subject': '(Error fetching email)',
                                        'sender_email': 'unknown',
                                        'primary_label': primary_label
                                    },
                                    base_ref_path=base_ref_path
                                )
                                yield error_proxy
                                logger.debug(f"Created error placeholder for {msg_id}")
                            except Exception as error_proxy_error:
                                logger.error(f"Failed to create error placeholder: {error_proxy_error}", exc_info=True)
                        
                        continue  # Move on to next message
                
                # Check for next page
                next_page_token = response_data.get('nextPageToken')
                if not next_page_token:
                    logger.debug("No more pages available")
                    break
                
                logger.debug(f"Following pagination ({emails_yielded} emails processed so far)")
                
            except HttpError as e:
                # Network/API errors - log detailed info and stop
                logger.error(
                    f"Gmail API error while scanning mailbox for {self.user_email}: {e}. "
                    f"Successfully processed {emails_yielded} emails before failure.",
                    exc_info=True
                )
                break  # Stop pagination on network errors
                
            except Exception as e:
                # Unexpected error - log with full details and stop
                logger.error(
                    f"Unexpected error during message scanning: {e}. "
                    f"Processed {emails_yielded} emails before failure.",
                    exc_info=True
                )
                break
        
        logger.info(f"Scan complete: yielded {emails_yielded} emails ({errors_encountered} errors)")



