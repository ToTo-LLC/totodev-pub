# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Outlook/Microsoft 365 email proxy implementations for Microsoft Graph API integration.

This module enables caching of Microsoft 365 emails and attachments using the FileProxy pattern.
Emails are stored as .eml files with attachments/embedded content extracted separately, achieving
significant file size reduction while maintaining full data integrity.

## Architecture

The module uses a shared EmailDataHandler pattern to minimize API calls:
- One Graph API call per email (regardless of attachment count)
- One MIME parse per email (shared across email + attachment proxies)
- Lazy loading triggers only when data is actually needed

## Quick Start

```python
from datetime import datetime, timedelta

factory = OutlookEmailFileProxyFactory(
    user_email="user@company.com",
    access_token="your-graph-api-token"
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

This module uses app-only authentication (OAuth2 client credentials flow). Requires 
`Mail.Read` and `Mail.ReadBasic` application permissions with admin consent. 
See `OutlookEmailFileProxyFactory` class docstring for detailed setup instructions.
"""

from typing import Optional, Dict, Any, Iterator, List, Literal
from datetime import datetime
from pathlib import Path
import asyncio
import requests
import json
import hashlib
import logging
import threading
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

from .file_proxy_base import FileProxyBase, OriginMetadata

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Retry and rate limiting constants
MAX_RETRY_ATTEMPTS = 3
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30

# Graph API pagination
DEFAULT_PAGE_SIZE = 50

# Attachment extraction thresholds
# Small images below this size (in KB) are kept embedded in EML to reduce
# filesystem noise from signature/decoration images
DEFAULT_MIN_IMAGE_KB = 20

# Email header filtering for "slim" mode
# When headers="slim" is specified, ONLY these headers are kept in cached .eml files.
# All other headers (authentication, routing, diagnostics, etc.) are removed to create
# cleaner files focused on email content rather than delivery infrastructure.
#
# This whitelist approach is future-proof: any new authentication or routing header
# standards will be automatically filtered without code changes.
#
# Modify this set to customize which headers are preserved in slim mode.
SLIM_MODE_HEADER_WHITELIST = {
    # Core email headers - who, what, when
    'from',           # Sender address
    'to',             # Primary recipient(s)
    'cc',             # Carbon copy recipient(s)
    'bcc',            # Blind carbon copy recipient(s)
    'subject',        # Email subject line
    'date',           # Send date/time
    'message-id',     # Unique message identifier
    
    # MIME structure - how content is encoded
    'content-type',              # MIME type and charset
    'content-transfer-encoding', # Encoding method (base64, quoted-printable, etc.)
    'mime-version',              # MIME protocol version
    
    # Threading and replies - conversation context
    'reply-to',       # Address for replies (if different from From)
    'in-reply-to',    # Message ID being replied to
    'references',     # Thread of related message IDs
    
    # Custom metadata - mutable fields we track
    'x-custom-followupflag',  # Follow-up flag status (notFlagged/flagged/complete)
}

# Image types subject to size-based filtering
# Small images of these types (typically signature/decoration images) can be kept
# embedded in the EML file rather than extracted to separate attachment files.
# This reduces filesystem noise while preserving complete email rendering.
SIZE_RESTRICTED_IMAGE_TYPES = {'.png', '.gif', '.jpg', '.jpeg', '.bmp', '.webp'}


# =============================================================================
# ATTACHMENT FILTERING UTILITIES
# =============================================================================

def _should_extract_attachment(attach_info: Dict[str, Any], min_img_kbytes: int) -> bool:
    """
    Determine if attachment should be extracted to separate file.
    
    Small images (< min_img_kbytes) are kept embedded in EML to reduce
    filesystem noise from signature/decoration images. This utility function
    provides shared filtering logic for Outlook, Gmail, IMAP, and other email systems.
    
    Args:
        attach_info: Dict with 'filename', 'size_bytes', 'original_content_type'
        min_img_kbytes: Minimum KB for image extraction (0 = extract all)
    
    Returns:
        True if should extract to file, False if should keep inline in EML
    
    Example:
        for attach in all_attachments:
            if _should_extract_attachment(attach, min_img_kbytes=DEFAULT_MIN_IMAGE_KB):
                yield create_proxy(attach)  # Extract this one
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
# MAIL FOLDER NAMES
# =============================================================================

class MailFolders:
    """
    Well-known Microsoft 365 mail folder names (case-insensitive).
    
    These can be used directly with OutlookEmailFileProxyFactory.scan_messages().
    For custom folders, use the folder display name (e.g., "Projects/2025").
    
    Examples:
        factory.scan_messages(folder_path=MailFolders.INBOX, ...)
        factory.scan_messages(folder_path=MailFolders.SENT_ITEMS, ...)
        factory.scan_messages(folder_path="Projects/2025", ...)
    """
    INBOX = "Inbox"
    SENT_ITEMS = "Sent Items"
    DRAFTS = "Drafts"
    DELETED_ITEMS = "Deleted Items"
    JUNK_EMAIL = "Junk Email"
    OUTBOX = "Outbox"
    ARCHIVE = "Archive"


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


def _format_ref_path_email(mail_folder: str, received_dt: datetime, 
                           counterparty_email: str, msg_id: str) -> str:
    """
    Format ref_path for email.
    
    Pattern: {date}/{time}_{counterparty}_{hash}.eml
    Example: 2025-11-01/143000_joe.smith@example.com_a3f2c9b1.eml
    
    Note: The mail_folder parameter is kept for compatibility but not included in ref_path.
    The folder should be specified in the CachedFileFolders grouping_pattern instead.
    
    The .eml extension signals to CachedFileFolders this is a file (not directory),
    placing emails directly in date folder without extra nesting.
    
    Date/time conversion: If received_dt is timezone-aware (typically UTC from Graph API),
    it will be converted to local timezone for folder organization. This ensures emails
    are organized by local date, not UTC date.
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
    
    The 3-character prefixes (att/emb) allow easy identification while maintaining equal length
    for programmatic filename manipulation. Original filename can be recovered by stripping
    the first 6 characters (prefix + sequence number + underscore).
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
    
    Rationale:
        Meeting invitations (text/calendar) often contain 80+ lines of base64 iCalendar data
        that isn't displayed by email clients. Extracting these:
        - Reduces .eml size by 20-90%
        - Makes emails easier to read/search
        - Preserves data in standard format (.ics) for calendar tools
    """
    content_disposition = mime_part.get('Content-Disposition', '')
    if content_disposition.startswith(('attachment', 'inline')):
        return (True, False)
    
    extractable_types = {'text/calendar', 'application/ics', 'text/vcard', 'text/x-vcard', 'text/directory'}
    return (True, True) if mime_part.get_content_type() in extractable_types else (False, False)


def _api_call_with_retry(api_url: str, bearer_token: str, 
                         max_retry_attempts: int = MAX_RETRY_ATTEMPTS) -> requests.Response:
    """
    Make Graph API call with retry logic and rate limiting support.
    
    - Exponential backoff on timeout: 1s, 2s, 4s
    - Honors HTTP 429 Retry-After header
    - Raises RequestException if all retries fail
    """
    headers = {'Authorization': f'Bearer {bearer_token}'}
    
    for attempt in range(max_retry_attempts):
        try:
            response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            
            # Handle rate limiting - honor server's Retry-After directive
            if response.status_code == 429:
                wait_seconds = int(response.headers.get('Retry-After', DEFAULT_RATE_LIMIT_WAIT_SECONDS))
                logger.warning(f"Rate limited by API, waiting {wait_seconds}s before retry")
                time.sleep(wait_seconds)
                continue
            
            response.raise_for_status()
            return response
            
        except requests.Timeout:
            if attempt < max_retry_attempts - 1:
                backoff_time = 2 ** attempt  # Exponential: 1s, 2s, 4s
                logger.warning(f"Request timeout on attempt {attempt + 1}/{max_retry_attempts}, "
                              f"waiting {backoff_time}s. URL: {api_url[:100]}...")
                time.sleep(backoff_time)
            else:
                logger.error(f"Request timeout after {max_retry_attempts} attempts. URL: {api_url}")
                raise
                
        except requests.RequestException as e:
            if attempt < max_retry_attempts - 1:
                backoff_time = 2 ** attempt
                logger.warning(f"Request failed on attempt {attempt + 1}/{max_retry_attempts}, "
                              f"waiting {backoff_time}s: {e}", exc_info=True)
                time.sleep(backoff_time)
            else:
                logger.error(f"Request failed after {max_retry_attempts} attempts: {e}", exc_info=True)
                raise
    
    raise requests.RequestException(f"Failed after {max_retry_attempts} attempts")


# =============================================================================
# API CLIENT (Internal)
# =============================================================================

class _GraphApiClient:
    """
    Internal API client for Microsoft Graph operations.
    
    Provides a mockable interface for Graph API calls. This class is private
    to the module (underscore prefix) but methods are public within the class
    to enable stubbing in tests via monkey patching.
    """
    
    def __init__(self, base_url: str, access_token: str):
        """
        Initialize Graph API client.
        
        Args:
            base_url: Graph API base URL (e.g., "https://graph.microsoft.com/v1.0")
            access_token: Bearer token for authentication
        """
        self.base_url = base_url
        self.access_token = access_token
    
    def fetch_email_mime(self, user_email: str, msg_id: str) -> bytes:
        """
        Fetch raw MIME content for a single email message.
        
        Args:
            user_email: User's email address (UPN)
            msg_id: Microsoft Graph message ID
            
        Returns:
            Raw MIME content as bytes
            
        Raises:
            requests.RequestException: If API call fails after retries
        """
        url = f"{self.base_url}/users/{user_email}/messages/{msg_id}/$value"
        response = _api_call_with_retry(url, self.access_token)
        return response.content
    
    def fetch_message_list(self, url: str) -> Dict[str, Any]:
        """
        Fetch a page of message metadata from Graph API.
        
        Args:
            url: Complete Graph API URL (may include pagination link)
            
        Returns:
            JSON response dict with 'value' array and optional '@odata.nextLink'
            
        Raises:
            requests.RequestException: If API call fails after retries
            json.JSONDecodeError: If response is not valid JSON
        """
        response = _api_call_with_retry(url, self.access_token)
        return response.json()


# =============================================================================
# METADATA MODELS (Internal)
# =============================================================================

class _EmailMetadata(BaseModel):
    """
    Internal: Configuration for creating an OutlookEmailProxy.
    
    Captures all email metadata from Microsoft Graph API in a structured format,
    providing validation and making it easy to extend without changing signatures.
    
    This is an internal implementation detail. Users should use 
    OutlookEmailProxy.from_graph_api() or OutlookEmailFileProxyFactory instead.
    """
    
    # Identity
    msg_id: str
    folder_path: str
    
    # People
    sender_email: str
    sender_name: str
    receiver_email: str
    receiver_name: str
    counterparty_email: str
    counterparty_name: str
    
    # Content
    subject: str
    
    # Timestamps
    received_datetime: datetime
    sent_datetime: datetime
    
    # Outlook metadata
    follow_up_flag_status: str = "notFlagged"
    importance: str = "normal"
    is_read: bool = False
    categories: List[str] = Field(default_factory=list)
    conversation_id: Optional[str] = None
    
    # Allow extra fields for forward compatibility
    model_config = {"extra": "allow"}


class _AttachmentMetadata(BaseModel):
    """
    Internal: Configuration for creating an OutlookAttachmentProxy.
    
    Captures attachment/embedded content metadata in a structured format.
    
    This is an internal implementation detail. Users should use 
    OutlookEmailFileProxyFactory to create attachment proxies.
    """
    
    # Email context
    email_ref_path: str
    folder_path: str
    msg_id: str
    
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
    
    # Outlook metadata
    follow_up_flag_status: str = "notFlagged"
    
    # Allow extra fields for forward compatibility
    model_config = {"extra": "allow"}


# =============================================================================
# EMAIL DATA HANDLER (Internal)
# =============================================================================

class _EmailDataHandler:
    """
    Internal helper managing email retrieval and MIME parsing with lazy loading.
    
    Shared between email proxy and all its attachment proxies to prevent redundant
    API calls and duplicate parsing. Fetches email content from Graph API only when
    first accessed, then caches in memory.
    
    Key optimizations:
    - Single API call per email (regardless of attachment count)
    - Single MIME parse (reused by all proxies)
    - Lazy loading (doesn't fetch until data is needed)
    """
    
    def __init__(self, msg_id: str, user_email: str, access_token: str,
                 base_url: str = "https://graph.microsoft.com/v1.0",
                 metadata: Optional[Dict[str, Any]] = None,
                 headers: str = "full"):
        self.msg_id = msg_id
        self.user_email = user_email
        self.access_token = access_token
        self.base_url = base_url
        self._metadata = metadata or {}
        self._headers_mode = headers  # "full" or "slim"
        self._api_client = _GraphApiClient(base_url, access_token)
        self._raw_email_bytes: Optional[bytes] = None
        self._parsed_mime = None
        self._modified_email_bytes: Optional[bytes] = None
        self._attachments: Optional[List[Dict[str, Any]]] = None
        self._fetch_attempted = False
        # Guards the lazy fetch so the memoization stays atomic when materialize()
        # runs the handler on worker threads (see the proxies' materialize()).
        self._fetch_lock = threading.Lock()
    
    def _fetch_email_if_needed(self) -> bool:
        """
        Fetch and parse email from Graph API if not already fetched.
        
        Returns:
            True if email was successfully fetched/cached, False otherwise
        """
        if self._fetch_attempted:
            return self._raw_email_bytes is not None
        
        with self._fetch_lock:
            # Double-checked: another thread may have completed the fetch while we waited.
            if self._fetch_attempted:
                return self._raw_email_bytes is not None
            
            self._fetch_attempted = True
            
            try:
                self._raw_email_bytes = self._api_client.fetch_email_mime(self.user_email, self.msg_id)
                self._parsed_mime = message_from_bytes(self._raw_email_bytes)
                self._process_attachments()  # Extract and replace with placeholders
                
                logger.debug(f"Successfully fetched email {self.msg_id} ({len(self._raw_email_bytes)} bytes, "
                            f"{len(self._attachments) if self._attachments else 0} attachments)")
                return True
                
            except requests.RequestException as e:
                # Log detailed network/API error but return False to indicate failure
                logger.error(
                    f"Failed to fetch email from Graph API: {e}. "
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
        
        This achieves significant file size reduction while maintaining data integrity:
        
        **Process:**
        1. Email is fetched from Microsoft Graph API in MIME format
        2. Attachments/embedded content extracted and stored as separate files
        3. Original MIME parts replaced with compact JSON placeholders
        4. Modified .eml file saved (dramatically smaller)
        
        **JSON Placeholder Structure:**
        ```json
        {
          "sequence_number": 1,
          "filename": "document.pdf",
          "original_content_type": "application/pdf",
          "original_content_disposition": "attachment",
          "size_bytes": 852124,
          "sha256": "79b1c098f2a1...",
          "is_embedded": false
        }
        ```
        
        **Benefits:**
        - Dramatic file size reduction (10MB email → 50KB .eml file)
        - Preserves complete metadata for auditability
        - SHA256 hashes ensure data integrity
        - Process is reversible if needed
        - Enables individual attachment access without parsing full email
        """
        self._attachments = []
        
        if not self._parsed_mime or not self._parsed_mime.is_multipart():
            # Single-part message - inject custom headers and apply filtering
            from email import message_from_bytes
            msg_copy = message_from_bytes(self._raw_email_bytes)
            
            # Inject follow-up flag as custom header AT THE TOP for faster parsing
            # This optimizes looks_same() method by reducing header parsing time
            if 'follow_up_flag_status' in self._metadata:
                self._prepend_header(msg_copy, 'X-Custom-FollowUpFlag', self._metadata['follow_up_flag_status'])
            
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
        
        # Create modified email if:
        # - We have attachments to replace with placeholders, OR
        # - We're in slim mode and need to filter headers
        needs_modification = self._attachments or self._headers_mode == "slim"
        self._modified_email_bytes = (self._create_modified_email_with_placeholders()
                                     if needs_modification else self._raw_email_bytes)
        
        if self._attachments:
            logger.info(f"Processed email {self.msg_id}: found {len(self._attachments)} "
                       f"attachments/embedded items")
    
    def _prepend_header(self, msg, header_name: str, header_value: str) -> None:
        """
        Prepend a header to the beginning of the message's header list.
        
        This is used to place X-Custom-FollowUpFlag at the top for faster parsing
        in the looks_same() method, reducing the number of headers that need to be
        read during change detection.
        
        Args:
            msg: email.message.Message object
            header_name: Name of the header to prepend
            header_value: Value of the header
        """
        # The email.message.Message class stores headers in _headers list
        # We insert at position 0 to make it the first header
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
        
        Args:
            msg: email.message.Message object to filter
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
        
        # Inject follow-up flag as custom header AT THE TOP for faster parsing
        # This optimizes looks_same() method by reducing header parsing time
        if 'follow_up_flag_status' in self._metadata:
            self._prepend_header(new_msg, 'X-Custom-FollowUpFlag', self._metadata['follow_up_flag_status'])
        
        # Copy all headers from original, then filter if in slim mode
        for header in self._parsed_mime.keys():
            # Add all values for this header (headers can have multiple values)
            for value in self._parsed_mime.get_all(header):
                new_msg[header] = value
        
        # Apply header filtering based on mode
        self._filter_headers(new_msg)
        
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
                # Format: {type}-{seq}-{ext}-placeholder.json
                # Example: embed-01-ics-placeholder.json, attach-02-pdf-placeholder.json
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
        
        Includes sender, receiver, dates, categories, and attachment details
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
        
        Example:
            handler.get_extractable_attachments(75)  # Only images >= 75KB
        """
        all_attachments = self.get_attachment_list()
        
        if min_img_kbytes == 0:
            return all_attachments  # No filtering
        
        return [att for att in all_attachments 
                if _should_extract_attachment(att, min_img_kbytes)]


# =============================================================================
# EMAIL PROXY
# =============================================================================

class OutlookEmailProxy(FileProxyBase):
    """
    Proxy for Microsoft 365 email message stored as .eml file.
    
    Represents a single email with metadata stored in YAML format in the slave_dir.
    Shares an _EmailDataHandler with attachment proxies to avoid redundant API calls.
    
    ## File Organization & ref_path Structure
    
    **Email ref_path pattern:**
    ```
    {date}/{time}_{counterparty}_{hash}.eml
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1.eml
    ```
    
    **Full path with CachedFileFolders grouping:**
    ```
    {owner_email}/{server_folder}/{date}/{time}_{counterparty}_{hash}.eml
    Example: owner@company.com/Inbox/2025-11-01/143000_joe.smith@example_a3f2c9b1.eml
    ```
    
    **Design rationale:**
    - Owner email and server folder are specified in CachedFileFolders grouping_pattern
    - Groups emails by day for efficient browsing
    - Chronological ordering within each day (HHMMSS prefix)
    - Counterparty field uses truncated domain for brevity (e.g., joe@example vs joe@example.com)
    - Message ID hash ensures uniqueness (48-bit collision resistance)
    - Scales well up to several thousand emails per day
    
    **Counterparty Logic:**
    The counterparty provides a consistent "other party" reference:
    - For "Sent" folders: counterparty = receiver email (person you sent to)
    - For other folders: counterparty = sender email (person who sent to you)
    This makes it easy to find all correspondence with a specific person.
    
    ## Public Interface
    
    **Properties:**
        sender_email, receiver_email, counterparty_email, received_datetime,
        attachment_count, follow_up_flag_status, sequence_number (None for emails)
    
    **Outlook-specific methods:**
        email_component() -> Literal["eml", "embed", "attach"]: Returns "eml"
        ref_path_of_email() -> str: Returns this email's ref_path
        email_msg_id() -> str: Returns Microsoft 365 message ID
        write_metadata_to_slave_dir(slave_dir): Writes eml_metadata.yaml
    """
    
    def __init__(self, handler: _EmailDataHandler, metadata: _EmailMetadata, min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB):
        """
        Initialize OutlookEmailProxy with handler and metadata.
        
        Args:
            handler: Shared data handler for email + attachments
            metadata: Email metadata from Graph API (internal use only)
            min_img_kbytes: Minimum KB for image extraction (stored for nested_proxies())
        """
        self._handler = handler
        self._folder_path = metadata.folder_path
        self._msg_id = metadata.msg_id
        self._sender_email = metadata.sender_email
        self._sender_name = metadata.sender_name
        self._receiver_email = metadata.receiver_email
        self._receiver_name = metadata.receiver_name
        self._counterparty_email = metadata.counterparty_email
        self._counterparty_name = metadata.counterparty_name
        self._subject = metadata.subject
        self._received_datetime = metadata.received_datetime
        self._sent_datetime = metadata.sent_datetime
        self._follow_up_flag_status = metadata.follow_up_flag_status
        self._importance = metadata.importance
        self._is_read = metadata.is_read
        self._categories = metadata.categories
        self._conversation_id = metadata.conversation_id
        self._extra_metadata = metadata.model_extra or {}
        self._min_img_kbytes = min_img_kbytes
        self._ref_path = _format_ref_path_email(
            metadata.folder_path, metadata.received_datetime, 
            metadata.counterparty_email, metadata.msg_id
        )
    
    @classmethod
    def from_graph_api(cls, graph_api_message: Dict[str, Any], folder_path: str,
                       user_email: str, access_token: str, 
                       base_url: str = "https://graph.microsoft.com/v1.0",
                       headers: str = "full",
                       min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB) -> 'OutlookEmailProxy':
        """
        Create OutlookEmailProxy from Microsoft Graph API message dict.
        
        Parses the Graph API response structure and extracts all necessary metadata
        to create a properly configured email proxy with shared data handler.
        
        Args:
            graph_api_message: Raw dict from Graph API /messages endpoint
            folder_path: Mail folder path (e.g., "Inbox", "Sent Items")
            user_email: User's email address for API calls
            access_token: Bearer token for Graph API authentication
            base_url: Graph API base URL (default: v1.0 endpoint)
            
        Returns:
            Configured OutlookEmailProxy instance
            
        Raises:
            KeyError: If required fields are missing from Graph API response
            ValueError: If datetime fields have invalid format
        """
        try:
            # Extract basic fields
            msg_id = graph_api_message['id']
            subject = graph_api_message.get('subject', '(No Subject)')
            
            # Parse sender from Graph API structure
            from_addr = graph_api_message.get('from', {}).get('emailAddress', {})
            sender_email = from_addr.get('address', 'unknown@unknown.com')
            sender_name = from_addr.get('name', sender_email)
            
            # Parse primary receiver from toRecipients array
            if to_recip := graph_api_message.get('toRecipients', []):
                recip_addr = to_recip[0].get('emailAddress', {})
                receiver_email = recip_addr.get('address', 'unknown@unknown.com')
                receiver_name = recip_addr.get('name', receiver_email)
            else:
                receiver_email, receiver_name = 'unknown@unknown.com', 'Unknown'
            
            # Counterparty logic: for sent folders use receiver, otherwise sender
            is_sent = folder_path.lower().startswith('sent')
            counterparty_email = receiver_email if is_sent else sender_email
            counterparty_name = receiver_name if is_sent else sender_name
            
            # Parse ISO datetimes
            received_dt = datetime.fromisoformat(graph_api_message['receivedDateTime'].replace('Z', '+00:00'))
            sent_dt = datetime.fromisoformat(graph_api_message['sentDateTime'].replace('Z', '+00:00'))
            
            logger.debug(f"Parsing email from Graph API: {msg_id}, subject='{subject[:50]}'")
            
        except KeyError as e:
            # Log full context but preserve original exception
            logger.error(
                f"Missing required field {e} in Graph API message. "
                f"Available keys: {list(graph_api_message.keys())}. "
                f"Folder: {folder_path}, User: {user_email}",
                exc_info=True
            )
            raise
            
        except (ValueError, TypeError) as e:
            # Add context about what we were trying to parse
            logger.error(
                f"Failed to parse Graph API message data: {e}. "
                f"receivedDateTime={graph_api_message.get('receivedDateTime')}, "
                f"sentDateTime={graph_api_message.get('sentDateTime')}, "
                f"msg_id={graph_api_message.get('id', 'unknown')}",
                exc_info=True
            )
            raise
        
        # Create metadata object
        metadata = _EmailMetadata(
            msg_id=msg_id,
            folder_path=folder_path,
            sender_email=sender_email,
            sender_name=sender_name,
            receiver_email=receiver_email,
            receiver_name=receiver_name,
            counterparty_email=counterparty_email,
            counterparty_name=counterparty_name,
            subject=subject,
            received_datetime=received_dt,
            sent_datetime=sent_dt,
            follow_up_flag_status=graph_api_message.get('flag', {}).get('flagStatus', 'notFlagged'),
            importance=graph_api_message.get('importance', 'normal'),
            is_read=graph_api_message.get('isRead', False),
            categories=graph_api_message.get('categories', []),
            conversation_id=graph_api_message.get('conversationId')
        )
        
        # Create shared handler for email + attachments
        handler = _EmailDataHandler(
            msg_id=msg_id, user_email=user_email, access_token=access_token,
            base_url=base_url,
            metadata={'subject': subject, 'sender_email': sender_email, 'sender_name': sender_name,
                     'receiver_email': receiver_email, 'receiver_name': receiver_name,
                     'follow_up_flag_status': metadata.follow_up_flag_status},
            headers=headers
        )
        
        return cls(handler=handler, metadata=metadata, min_img_kbytes=min_img_kbytes)
    
    # Properties (simple getters)
    sender_email = property(lambda self: self._sender_email)
    receiver_email = property(lambda self: self._receiver_email)
    counterparty_email = property(lambda self: self._counterparty_email)
    received_datetime = property(lambda self: self._received_datetime)
    attachment_count = property(lambda self: len(self._handler.get_attachment_list()))
    follow_up_flag_status = property(lambda self: self._follow_up_flag_status)
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
            # Log context but let downstream code see the real problem
            logger.error(
                f"Handler returned None for email body. "
                f"Message ID: {self._msg_id}, Folder: {self._folder_path}, "
                f"Sender: {self._sender_email}, Subject: {self._subject[:100]}"
            )
            raise RuntimeError(
                f"Failed to fetch email content for message {self._msg_id} in {self._folder_path}. "
                f"Handler returned None - check logs for underlying cause."
            )
        
        try:
            target_path = Path(target_dir) / self.file_name()
            target_path.write_bytes(email_content)
            logger.debug(f"Deployed email to {target_path} ({len(email_content)} bytes)")
        except (IOError, OSError) as e:
            # File system error - add context but preserve original
            logger.error(
                f"Failed to write email file to {target_path}: {e}. "
                f"Target dir exists: {Path(target_dir).exists()}, "
                f"Content size: {len(email_content)} bytes",
                exc_info=True
            )
            raise
    
    def looks_same(self, cached_file_path: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
        """
        Quick comparison: file exists + follow-up flag matches.

        Note: this proxy compares by parsing an injected header rather than by size,
        so `override_byte_count` does not apply. A truncated (zero-byte) cached file
        has no header to parse, so it will report a difference and trigger
        re-materialization -- which is the correct outcome for flag-change detection.
        
        Microsoft 365 emails are immutable in content, but follow-up flags are mutable.
        Since cached_file_path is derived from ref_path() which includes the msg_id hash,
        if the file exists at this path, the message ID already matches by definition.
        We only need to check the follow-up flag status.
        
        ## Follow-Up Flag Tracking
        
        While Microsoft 365 email content is immutable, follow-up flags can be changed by users.
        To detect these changes efficiently:
        
        **X-Custom-FollowUpFlag Header:**
        - Injected into every cached .eml file as the **first header** for fast parsing
        - Contains flag status: "notFlagged", "flagged", or "complete"
        - Preserved in both "full" and "slim" header modes
        - Enables detection of flag changes during resync
        
        **Optimization:**
        - File existence check confirms message ID matches (via ref_path)
        - Parses only the first header (X-Custom-FollowUpFlag) for flag changes
        - Returns False if flag status differs, triggering re-cache
        - Fast validation for unchanged emails
        
        This is particularly valuable for workflows that depend on follow-up flag status
        to trigger actions or filter emails.
        
        Returns:
            True: File exists AND follow-up flag matches
            False: File doesn't exist OR flag status differs
            None: Can't determine (parse error, etc.)
        """
        cached_path = Path(cached_file_path)
        if not cached_path.exists():
            return False
        
        # File exists at this path means message ID matches (ref_path includes hash)
        # Only need to check follow-up flag status
        try:
            cached_msg = message_from_bytes(cached_path.read_bytes())
            
            # Check follow-up flag status
            cached_flag = cached_msg.get('X-Custom-FollowUpFlag', 'notFlagged')
            if cached_flag != self._follow_up_flag_status:
                logger.debug(
                    f"Follow-up flag changed for {self._msg_id}: "
                    f"{cached_flag} -> {self._follow_up_flag_status}"
                )
                return False
            
            # File exists and flag matches
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
        Fetch email from Microsoft Graph API.
        
        Returns True if successful, False otherwise. Uses lazy loading from shared handler.
        """
        # The handler does blocking, synchronous network I/O (and rate-limit sleeps).
        # Pro: running it on a worker thread frees the event loop, so other downloads
        # progress in parallel. Con: costs a thread-pool thread per call and requires
        # the shared handler's fetch to be thread-safe (see _fetch_lock).
        try:
            return await asyncio.to_thread(self._handler.get_email_body) is not None
        except Exception as e:
            logger.error(f"Failed to materialize email {self._msg_id}: {e}")
            return False
    
    def get_context_info(self) -> Dict[str, Any]:
        """Return safe logging context (no sensitive data like passwords)."""
        return {
            'type': 'email', 'msg_id': self._msg_id, 'folder': self._folder_path,
            'subject': self._subject, 'sender': self._sender_email,
            'receiver': self._receiver_email, 'received': self._received_datetime.isoformat(),
            'ref_path': self._ref_path
        }

    def retrieval_hint(self) -> Dict[str, Any]:
        """Record the Outlook message coordinates needed to re-fetch this email later.

        The serialized .eml size is not known until the body is fetched and the
        placeholder-rewriting runs, so peek_metadata() is left at its default
        (None) for emails; this hint still records how to retrieve the original.
        """
        return {"source": "outlook", "kind": "email", "msg_id": self._msg_id, "folder_path": self._folder_path}
    
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
            OutlookAttachmentProxy: Attachment proxies for this email's attachments
        """
        # Fetch email body and extract attachment list (lazy loading)
        attachment_list = self._handler.get_extractable_attachments(
            min_img_kbytes=self._min_img_kbytes
        )
        
        for attach_info in attachment_list:
            metadata = _AttachmentMetadata(
                email_ref_path=self._ref_path,
                folder_path=self._folder_path,
                msg_id=self._msg_id,
                sequence_number=attach_info['sequence_number'],
                filename=attach_info['filename'],
                size_bytes=attach_info['size_bytes'],
                content_type=attach_info['original_content_type'],
                is_embedded=attach_info.get('is_embedded', False),
                sender_email=self._sender_email,
                receiver_email=self._receiver_email,
                counterparty_email=self._counterparty_email,
                received_datetime=self._received_datetime,
                follow_up_flag_status=self._follow_up_flag_status
            )
            yield OutlookAttachmentProxy(
                handler=self._handler,
                metadata=metadata
            )
    
    # Outlook-specific interface
    def email_component(self) -> Literal["eml", "embed", "attach"]:
        """Return the type of email component this proxy represents."""
        return "eml"
    
    def ref_path_of_email(self) -> str:
        """Return this email's ref_path."""
        return self._ref_path
    
    def email_msg_id(self) -> str:
        """Return Microsoft 365 message ID."""
        return self._msg_id
    
    def write_metadata_to_slave_dir(self, slave_dir: Path):
        """
        Write eml_metadata.yaml to slave directory.
        
        Creates comprehensive YAML metadata file with sender/receiver info, dates,
        categories, and attachment details. This is the primary metadata store for
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
        
        metadata = {
            'msg_id': self._msg_id, 'subject': self._subject,
            'sender_email': self._sender_email, 'sender_name': self._sender_name,
            'receiver_email': self._receiver_email, 'receiver_name': self._receiver_name,
            'counterparty_email': self._counterparty_email, 'counterparty_name': self._counterparty_name,
            'to_recipients': [{'email': self._receiver_email, 'name': self._receiver_name}],
            'cc_recipients': [], 'bcc_recipients': [],
            'received_datetime': self._received_datetime.isoformat(),
            'sent_datetime': self._sent_datetime.isoformat(),
            'folder_path': self._folder_path, 'importance': self._importance,
            'is_read': self._is_read, 'follow_up_flag_status': self._follow_up_flag_status,
            'categories': self._categories, 'attachment_count': self.attachment_count,
            'conversation_id': self._conversation_id,
            'attachments': self._handler.get_attachment_list(),
            **self._extra_metadata
        }
        
        yaml_path = slave_dir / "eml_metadata.yaml"
        
        try:
            with open(yaml_path, 'w', encoding='utf-8') as f:
                yaml.dump(metadata, f, default_flow_style=False, sort_keys=False)
            logger.debug(f"Wrote metadata to {yaml_path} ({len(metadata)} fields)")
        except (IOError, OSError) as e:
            # Add context about what we were trying to write
            logger.error(
                f"Failed to write metadata file {yaml_path}: {e}. "
                f"Slave dir exists: {slave_dir.exists()}, "
                f"Metadata keys: {list(metadata.keys())}",
                exc_info=True
            )
            raise


# =============================================================================
# ATTACHMENT PROXY
# =============================================================================

class OutlookAttachmentProxy(FileProxyBase):
    """
    Proxy for email attachment or embedded content.
    
    Represents either:
    - Traditional attachment (Content-Disposition: attachment/inline)
    - Embedded content (text/calendar meeting invite, text/vcard contact, etc.)
    
    Shares _EmailDataHandler with parent email to avoid redundant API calls.
    
    ## File Organization
    
    **Attachment ref_path pattern:**
    ```
    {email_base}/attach/{seq}_{filename}
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1/attach/01_document.pdf
    ```
    
    **Embedded content ref_path pattern:**
    ```
    {email_base}/embed/{seq}_{type}_{filename}
    Example: 2025-11-01/143000_joe.smith@example_a3f2c9b1/embed/01_cal_meeting.ics
    ```
    
    Attachments are extracted from the parent email and stored in separate subdirectories
    (attach/ or embed/) under the email's base path.
    
    ## Public Interface
    
    **Properties:**
        sender_email, receiver_email, counterparty_email, received_datetime,
        attachment_count, follow_up_flag_status, sequence_number (1-based)
    
    **Outlook-specific methods:**
        email_component() -> Literal["eml", "embed", "attach"]: Returns "attach" or "embed"
        ref_path_of_email() -> str: Returns parent email's ref_path
        email_msg_id() -> str: Returns Microsoft 365 message ID
    """
    
    def __init__(self, handler: _EmailDataHandler, metadata: _AttachmentMetadata):
        """
        Initialize OutlookAttachmentProxy with handler and metadata.
        
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
        self._folder_path = metadata.folder_path
        self._msg_id = metadata.msg_id
        self._sender_email = metadata.sender_email
        self._receiver_email = metadata.receiver_email
        self._counterparty_email = metadata.counterparty_email
        self._received_datetime = metadata.received_datetime
        self._follow_up_flag_status = metadata.follow_up_flag_status
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
    follow_up_flag_status = property(lambda self: self._follow_up_flag_status)
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
    
    def looks_same(self, cached_file_path: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
        """
        Quick comparison using file size.

        Attachments are immutable when parent email is immutable, so size match is reliable.
        """
        cached_path = Path(cached_file_path)
        if not cached_path.exists():
            return False
        # For a truncated entry the on-disk size is zero; use the recorded size when supplied.
        cached_size = cached_path.stat().st_size if override_byte_count is None else override_byte_count
        return cached_size == self._size_bytes
    
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """
        Fetch attachment from Microsoft Graph API.
        
        Returns True if successful, False otherwise. Uses lazy loading from shared handler.
        """
        # On a cache miss the handler does blocking, synchronous network I/O (and
        # rate-limit sleeps). Pro: running it on a worker thread frees the event loop
        # for parallel downloads. Con: costs a thread-pool thread per call and requires
        # the shared handler's fetch to be thread-safe (see _fetch_lock).
        try:
            return await asyncio.to_thread(self._handler.get_attachment, self._sequence_number) is not None
        except Exception as e:
            logger.error(f"Failed to materialize attachment {self._sequence_number} for {self._msg_id}: {e}")
            return False
    
    async def peek_metadata(self) -> Optional[OriginMetadata]:
        """Report the attachment size cheaply (known from email metadata).

        Attachment size is provided by the Graph metadata without downloading the
        bytes. mtime is left None (attachments don't carry an independent
        modification time; the parent email's received time is not the file's).
        """
        return OriginMetadata(size=self._size_bytes)

    def retrieval_hint(self) -> Dict[str, Any]:
        return {
            "source": "outlook",
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
    
    # Outlook-specific interface
    def email_component(self) -> Literal["eml", "embed", "attach"]:
        """Return the type of email component this proxy represents."""
        return "embed" if self._is_embedded else "attach"
    
    def ref_path_of_email(self) -> str:
        """Return parent email's ref_path."""
        return self._email_ref_path
    
    def email_msg_id(self) -> str:
        """Return Microsoft 365 message ID."""
        return self._msg_id


# =============================================================================
# ERROR PROXY
# =============================================================================

class OutlookEmailErrorProxy(FileProxyBase):
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
            email_metadata: Dict with msg_id, subject, sender_email, folder_path, etc.
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
    
    def looks_same(self, cached_file_path: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
        """Error files should be re-attempted on next sync (override_byte_count is ignored)."""
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
    
    # Outlook-specific interface
    def email_component(self) -> Literal["eml", "embed", "attach", "error"]:
        """Return the type of email component this proxy represents."""
        return "error"
    
    def ref_path_of_email(self) -> str:
        """Return this error's ref_path."""
        return self._ref_path
    
    def email_msg_id(self) -> str:
        """Return Microsoft 365 message ID if available."""
        return self._email_metadata.get('msg_id', 'unknown')


# =============================================================================
# FACTORY
# =============================================================================

class OutlookEmailFileProxyFactory:
    """
    Factory for discovering and creating email/attachment proxies from Microsoft 365.
    
    Main entry point for working with Microsoft 365 emails. Scans mailboxes using Graph API
    and creates FileProxy objects for emails, attachments, and embedded content.
    
    ## Authentication & Permissions
    
    This factory uses **app-only authentication** (OAuth2 client credentials flow):
    - No user sign-in required - perfect for automated workflows and cron jobs
    - Requires Application permissions (not Delegated): `Mail.Read` and `Mail.ReadBasic`
    - Admin consent must be granted in Microsoft Entra Admin Center
    - API endpoints use `/users/{email}/` format (not `/me/`)
    
    **Required Environment Variables:**
    - `AZURE_CLIENT_ID` - Application (client) ID from Azure app registration
    - `AZURE_CLIENT_SECRET` - Client secret (expires, needs rotation)
    - `AZURE_TENANT_ID` - Directory (tenant) ID
    - `AZURE_USER_EMAIL` - User's email address for mailbox access
    
    **Setup Steps:**
    1. Register your application at https://entra.microsoft.com/
    2. Navigate to Identity > Applications > App registrations
    3. Create new registration
    4. Add Application permissions: `Mail.Read`, `Mail.ReadBasic`
    5. Grant admin consent
    6. Create client secret under Certificates & secrets
    
    ## Usage Pattern
    
    1. Create factory with user_email and access_token
    2. Call scan_messages() with filters
    3. Iterate through returned proxies (emails first, then their attachments)
    4. Use with CachedFileFolders.resync_bulk() for automatic caching
    
    Example:
        factory = OutlookEmailFileProxyFactory("user@company.com", access_token)
        for proxy in factory.scan_messages(received_after=datetime(2025, 1, 1)):
            print(proxy.file_name())
    """
    
    def __init__(self, user_email: str, access_token: str,
                 base_url: str = "https://graph.microsoft.com/v1.0",
                 create_error_placeholders: bool = False,
                 min_img_kbytes: int = DEFAULT_MIN_IMAGE_KB):
        """
        Initialize factory.
        
        Args:
            user_email: User's email (UPN) for mailbox access
            access_token: Bearer token from OAuth2 client credentials flow
            base_url: Graph API base URL (default: v1.0 endpoint)
            create_error_placeholders: If True, create .error.json files when email
                fetching fails instead of silently skipping. Useful for debugging
                and enabling retry logic.
            min_img_kbytes: Minimum size in kilobytes for images to be extracted as
                separate attachment files. Images smaller than this (typically signature
                or decoration images) remain embedded in the EML file. Only applies to
                image types in SIZE_RESTRICTED_IMAGE_TYPES. Set to 0 to extract all images.
                Defaults to DEFAULT_MIN_IMAGE_KB constant.
        
        Raises:
            ValueError: If user_email, access_token, or base_url are invalid
        """
        # Validate inputs
        if not user_email or '@' not in user_email:
            raise ValueError(f"Invalid user_email: {user_email!r}. Must be a valid email address.")
        if not access_token or len(access_token) < 10:
            raise ValueError("Invalid or missing access_token. Token must be at least 10 characters.")
        if not base_url.startswith('https://'):
            raise ValueError(f"base_url must use HTTPS: {base_url!r}")
        
        self.user_email = user_email
        self.access_token = access_token
        self.base_url = base_url
        self.create_error_placeholders = create_error_placeholders
        self.min_img_kbytes = min_img_kbytes
        self._api_client = _GraphApiClient(base_url, access_token)
        
        logger.debug(f"Initialized OutlookEmailFileProxyFactory for {user_email} "
                    f"(error_placeholders={'enabled' if create_error_placeholders else 'disabled'}, "
                    f"min_img_kbytes={min_img_kbytes})")
    
    def scan_messages(self, received_after: datetime, folder_path: str = "Inbox",
                     received_before: Optional[datetime] = None, from_address: Optional[str] = None,
                     subject_contains: Optional[str] = None, importance: Optional[str] = None,
                     is_read: Optional[bool] = None,
                     max_results: Optional[int] = None, newest_first: bool = True,
                     headers: str = "full"
                     ) -> Iterator[FileProxyBase]:
        """
        Scan mailbox and yield email proxies with filtering.
        
        Returns emails in chronological order (newest first by default). This supports
        incremental caching: iterate until encountering previously cached emails, then stop.
        
        Yields only OutlookEmailProxy objects (email messages). To get attachments for each
        email, call the email_proxy.nested_proxies() method, which returns a generator of
        OutlookAttachmentProxy objects. This lazy loading pattern enables efficient caching -
        attachments are only fetched when needed.
        
        Args:
            received_after: REQUIRED - Only emails received after this datetime (prevents
                           scanning entire mailbox)
            folder_path: Mailbox folder to scan (default: "Inbox", also available as 
                        MailFolders.INBOX). Use MailFolders constants for well-known folders:
                        MailFolders.INBOX, MailFolders.SENT_ITEMS, MailFolders.DRAFTS,
                        MailFolders.DELETED_ITEMS, MailFolders.JUNK_EMAIL, MailFolders.ARCHIVE.
                        For custom folders, use the display name (e.g., "Projects/2025").
            received_before: Optional upper datetime bound
            from_address: Filter by exact sender email
            subject_contains: Filter by substring in subject (case-insensitive)
            importance: Filter by "low", "normal", or "high"
            is_read: Filter by read status (True/False/None for both)
            max_results: Limit number of emails returned (useful for testing)
            newest_first: True=newest first (default), False=oldest first
            headers: Header mode for .eml files - "full" (default) keeps all headers,
                    "slim" removes authentication, routing, and diagnostic headers for cleaner files
            
        Yields:
            OutlookEmailProxy: Email message proxies (attachments via nested_proxies())
            
        Examples:
            # Simple scan - emails only
            for email_proxy in factory.scan_messages(
                folder_path=MailFolders.SENT_ITEMS,
                received_after=datetime(2025, 1, 1)
            ):
                print(f"Email: {email_proxy._subject}")
            
            # With attachments using nested_proxies()
            for email_proxy in factory.scan_messages(
                received_after=datetime(2025, 1, 1),
                importance="high",
                is_read=False,
                max_results=10
            ):
                print(f"Unread important email: {email_proxy._subject}")
                for attach_proxy in email_proxy.nested_proxies():
                    print(f"  Attachment: {attach_proxy.file_name()}")
        """
        # Build OData $filter expression
        filters = [f"receivedDateTime ge {received_after.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
        
        if received_before:
            filters.append(f"receivedDateTime le {received_before.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        if from_address:
            filters.append(f"from/emailAddress/address eq '{from_address}'")
        if importance:
            filters.append(f"importance eq '{importance}'")
        if is_read is not None:
            filters.append(f"isRead eq {str(is_read).lower()}")
        
        # Build Graph API URL (well-known folder names: inbox, sentitems, drafts, deleteditems)
        api_folder_name = folder_path.lower().replace(' ', '')
        url = (f"{self.base_url}/users/{self.user_email}/mailFolders/{api_folder_name}/messages"
               f"?$filter={' and '.join(filters)}"
               f"&$orderby=receivedDateTime {'DESC' if newest_first else 'ASC'}"
               f"&$top={DEFAULT_PAGE_SIZE}"
               f"&$select=id,subject,sender,from,toRecipients,receivedDateTime,sentDateTime,"
               f"hasAttachments,importance,isRead,flag,categories,conversationId")
        
        emails_yielded = 0
        errors_encountered = 0
        
        logger.info(f"Starting scan of {folder_path} for {self.user_email} "
                   f"(received_after={received_after}, max_results={max_results})")
        
        # Pagination loop (follows @odata.nextLink)
        while url and (not max_results or emails_yielded < max_results):
            try:
                response_data = self._api_client.fetch_message_list(url)
                messages = response_data.get('value', [])
                
                logger.debug(f"Fetched {len(messages)} messages from page")
                
                for msg_data in messages:
                    if max_results and emails_yielded >= max_results:
                        break
                    
                    # Graph API doesn't support 'contains' in $filter, so filter locally
                    if subject_contains and subject_contains.lower() not in msg_data.get('subject', '').lower():
                        continue
                    
                    try:
                        # Create and yield email proxy
                        # Attachments are accessed via email_proxy.nested_proxies()
                        email_proxy = self._create_email_proxy(msg_data, folder_path, headers=headers)
                        yield email_proxy
                        emails_yielded += 1
                                    
                    except Exception as e:
                        # Single email proxy creation failed
                        errors_encountered += 1
                        logger.error(
                            f"Failed to process message {msg_data.get('id', 'unknown')} "
                            f"in folder {folder_path}: {e}. "
                            f"Subject: {msg_data.get('subject', 'N/A')}",
                            exc_info=True
                        )
                        
                        if self.create_error_placeholders:
                            # Create error placeholder proxy
                            try:
                                # Try to construct a base ref_path for the error file
                                try:
                                    received_dt = datetime.fromisoformat(
                                        msg_data.get('receivedDateTime', datetime.now().isoformat()).replace('Z', '+00:00')
                                    )
                                except (ValueError, TypeError):
                                    received_dt = datetime.now()
                                
                                counterparty = msg_data.get('from', {}).get('emailAddress', {}).get('address', 'unknown')
                                base_ref_path = _format_ref_path_email(
                                    folder_path, received_dt, counterparty, msg_data.get('id', 'error')
                                )
                                
                                error_proxy = OutlookEmailErrorProxy(
                                    error_info={
                                        'error': True,
                                        'error_type': type(e).__name__,
                                        'error_message': str(e),
                                        'timestamp': datetime.now().isoformat(),
                                        'traceback': traceback.format_exc(),
                                        'can_retry': isinstance(e, (requests.Timeout, requests.ConnectionError))
                                    },
                                    email_metadata={
                                        'msg_id': msg_data.get('id'),
                                        'subject': msg_data.get('subject', '(No Subject)'),
                                        'sender_email': counterparty,
                                        'folder_path': folder_path
                                    },
                                    base_ref_path=base_ref_path
                                )
                                yield error_proxy
                                logger.debug(f"Created error placeholder for {msg_data.get('id', 'unknown')}")
                            except Exception as error_proxy_error:
                                logger.error(f"Failed to create error placeholder: {error_proxy_error}", exc_info=True)
                        
                        continue  # Move on to next message
                
                url = response_data.get('@odata.nextLink')
                if url:
                    logger.debug(f"Following pagination link ({emails_yielded} emails processed so far)")
                
            except requests.RequestException as e:
                # Network/API errors - log detailed info and stop
                logger.error(
                    f"API request failed while scanning {folder_path} for {self.user_email}: {e}. "
                    f"Successfully processed {emails_yielded} emails before failure.",
                    exc_info=True
                )
                break  # Stop pagination on network errors
                
            except (json.JSONDecodeError, KeyError) as e:
                # Unexpected API response format - log and stop
                logger.error(
                    f"Unexpected API response format from Graph API: {e}. "
                    f"Processed {emails_yielded} emails before failure.",
                    exc_info=True
                )
                break
                
            except Exception as e:
                # Unexpected error - log with full details and stop
                logger.error(
                    f"Unexpected error during message scanning: {e}. "
                    f"Processed {emails_yielded} emails before failure.",
                    exc_info=True
                )
                break
        
        logger.info(f"Scan complete: yielded {emails_yielded} emails ({errors_encountered} errors)")
    
    def _create_email_proxy(self, graph_api_message: Dict[str, Any], folder_path: str, 
                           headers: str = "full") -> OutlookEmailProxy:
        """Parse Graph API message data and create OutlookEmailProxy."""
        return OutlookEmailProxy.from_graph_api(
            graph_api_message=graph_api_message,
            folder_path=folder_path,
            user_email=self.user_email,
            access_token=self.access_token,
            base_url=self.base_url,
            headers=headers,
            min_img_kbytes=self.min_img_kbytes
        )
