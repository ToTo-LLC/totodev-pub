# External Source Sync Examples - Implementation Specification

## ⚠️ DRAFT SPECIFICATION - APPROVAL REQUIRED

**IMPORTANT:** This is a draft specification for demonstration projects. Each individual example program should have its specification and budget approved by project maintainers before development work begins. Do not start implementation without explicit approval.

---

## Overview

This document specifies a series of example implementations demonstrating how to integrate various external data sources with the `CachedFileFolders` library. These examples follow the architectural pattern established in the SharePoint synchronization example and are intended to showcase the versatility of the FileProxy pattern across diverse data sources.

The goal is to create production-quality example code that developers can study and adapt for their own projects, demonstrating best practices for:
- Abstracting external data sources behind a unified interface
- Building intelligent caches with automatic change detection
- Handling authentication and error recovery
- Processing files concurrently for optimal performance

---

## Background: CachedFileFolders and FileProxy Pattern

### Core Concepts

The `CachedFileFolders` class is the foundation of this architecture. It provides:
- **Automatic change detection**: Detects INSERT/UPDATE/DELETE without manual diff logic
- **Intelligent caching**: Maintains a local mirror of external files with metadata
- **Change notifications**: Fires callbacks when files change, enabling downstream processing
- **Concurrent processing**: Async patterns for 3-20x performance gains

### The FileProxy Pattern

External data sources are abstracted behind the `FileProxyBase` interface. Each proxy represents a single file-like unit from an external source and implements:
- `ref_path()`: Returns a unique identifier that cross-references the source with the local cache
- `fetch_data()`: Downloads/retrieves the actual file content
- `get_metadata()`: Returns metadata dictionary (timestamps, size, checksums, etc.)

### The Factory Pattern for FileProxies

The Factory pattern is a **design pattern** (not a base class to inherit from) that makes target selection and file enumeration consistent and straightforward across different data sources. Your `{Service}FileProxyFactory` class should encapsulate the logic for:

- **Authentication**: Handling credentials and tokens
- **Target selection**: Accepting intuitive parameters for what to sync (folders, date ranges, senders, etc.)
- **Enumeration**: Discovering and iterating over files that match the criteria
- **Proxy creation**: Instantiating FileProxy objects for each discovered item

**Key insight**: The factory's job is to hide the complexity of API calls, pagination, filtering, and authentication behind a simple `scan_files()` method that yields FileProxy instances.

**Examples of target selection parameters**:
- **Email systems**: Folder path + date range + sender filter
- **File-folder systems**: Root directory + file extension patterns + size limits
- **Message systems**: Channel ID + timestamp range + user filter
- **Cloud storage**: Bucket/folder ID + modification date + file type filters

By following this pattern consistently, all your example implementations will have similar structure and be easier to understand, maintain, and adapt.

### The ref_path Concept

The `ref_path` is critical—it acts as the primary key linking the external resource to the cached file. This path must be:
- **Unique**: No two different files should have the same ref_path
- **Stable**: The same file should always have the same ref_path across sync runs
- **Meaningful**: Should be human-readable when possible for debugging

For file-based systems (SharePoint, S3, Google Drive), the ref_path is often the file's path. For message-based systems (email, Slack), the ref_path might be constructed from IDs like `"message_id:12345"` or `"channel_abc/timestamp_123456789.json"`.

### Note on grouping_key

While `CachedFileFolders` supports an optional `grouping_key` parameter for additional cache organization, **your example implementations should either not use it or hardcode a simple static value** to avoid adding complexity. The SharePoint example uses a simple pattern like `"key-{dir_key}/"`, and you should follow a similar simple approach.

### Configuration via Environment Variables

**IMPORTANT**: All endpoint connection information and credentials MUST be passed via environment variables, not command-line arguments. This follows security best practices and keeps sensitive data out of command history and process listings.

**Command-line arguments should only be used for**:
- Cache configuration (`--cache-root`, `--dir-key`)
- Operational parameters (`--max-files`, `--debug`)
- Non-sensitive filters that vary per run (optional date overrides, etc.)

**Environment variables should be used for**:
- API keys, tokens, and secrets
- Client IDs and tenant IDs
- Endpoint URLs and domain names
- Email addresses and user identifiers
- Any credential or connection information

Your `validate_environment()` function should check for all required environment variables at startup and provide helpful error messages listing what's missing.

---

## Required Reading and Reference Files

Before beginning implementation, study these files and classes:

### Primary Reference Implementation
- **`src/totodev_pub/cached_file_folders_support/examples/sharepoint_daily_tree_sync.py`**
  - Complete reference implementation showing the full pattern
  - Study the `SimpleSharepointSync` class structure
  - Note the authentication flow, error handling, and CLI design
  - Observe how change handlers are registered and called

### Core Library Classes
- **`src/totodev_pub/cached_file_folders.py`**: The `CachedFileFolders` class
  - Method: `resync_bulk()` - Main synchronization method
  - Understand the async processing model
  - Review the `ResyncBulkResult` return type

- **`src/totodev_pub/cached_file_folders_support/file_proxy_base.py`**: The `FileProxyBase` abstract class
  - Methods to implement: `ref_path()`, `fetch_data()`, `get_metadata()`
  - Understand the contract your FileProxy must fulfill

- **`src/totodev_pub/cached_file_folders_support/file_proxy_sharepoint.py`**: `SharepointFileProxyFactory`
  - Example of a FileProxy implementation
  - Note how authentication tokens are passed
  - Study the `scan_files()` generator pattern

### Supporting Types
- **`src/totodev_pub/cached_file_folders_support/__init__.py`**:
  - `ChangeNotice` dataclass - What your handlers receive
  - `ChangeType` enum - INSERT, UPDATE, DELETE
  
- **`src/totodev_pub/cached_file_folders_support/types.py`**:
  - `ResyncBulkResult` - Return type from sync operations
  - `UpsertFailure` - How failures are reported

### Testing
- **`src/totodev_pub/tests/`**: Review existing tests to understand expected behavior
  - Look for test patterns you can adapt for your implementation

---

## Implementation Projects

Each project below is a standalone example demonstrating integration with a specific external data source.

---

### 1. Google Drive Sync

**Script Name:** `gdrive_daily_sync.py`

**File-like Unit:** Individual Drive files
- Native files (PDFs, images, Office docs): Cached in original format
- Google Docs/Sheets/Slides: Exported to specified format (docx, xlsx, pdf, etc.)
- Each file cached with metadata including file_id, modified_time, md5Checksum

**Suggested ref_path Construction:**
- Use Google Drive's stable file paths: `"path/to/folder/filename.ext"`
- Alternative: Use file ID format: `"gdrive_id:{file_id}"`
- **Recommendation**: Use hierarchical paths for readability, but include file_id in metadata for verification

**Useful Factory Filters:**
```python
class GoogleDriveFileProxyFactory:
    def scan_files(
        self,
        folder_id: Optional[str] = None,  # Starting folder ID (None = entire Drive)
        file_extensions: Optional[List[str]] = None,  # Filter by extensions
        mime_types: Optional[List[str]] = None,  # Filter by MIME types
        modified_after: Optional[datetime] = None,  # Date range filter
        modified_before: Optional[datetime] = None,  # Date range filter
        owned_by_me: bool = True,  # Only owned files
        shared_with_me: bool = False,  # Include shared files
        export_format: str = "pdf",  # For Google Docs: "docx", "pdf", "markdown"
        include_subfolders: bool = True,  # Recursive scan
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use Google OAuth2 with service account or user credentials
- Required scopes: `https://www.googleapis.com/auth/drive.readonly`
- Environment variables: `GOOGLE_CREDENTIALS_FILE` or individual OAuth parameters

**Key Implementation Notes:**
- Google Docs/Sheets/Slides require export API calls (different from regular file downloads)
- Handle rate limiting (Google Drive API has quotas)
- Use the Drive API v3
- Include MIME type mapping for export formats

**Dependencies:**
- `google-api-python-client`
- `google-auth`
- `google-auth-httplib2`
- `google-auth-oauthlib`

---

### 2. Box.com Sync

**Script Name:** `box_daily_sync.py`

**File-like Unit:** Box files from enterprise storage
- Each file cached in original format
- Metadata includes file_id, modified_at, size, sha1 hash, version information

**Suggested ref_path Construction:**
- Use Box's folder paths: `"Folder Name/Subfolder/filename.ext"`
- Alternative: `"box_id:{file_id}"`
- **Recommendation**: Use paths for human readability, store file_id in metadata

**Useful Factory Filters:**
```python
class BoxFileProxyFactory:
    def scan_files(
        self,
        folder_id: str = "0",  # Starting folder ID ("0" = root)
        file_extensions: Optional[List[str]] = None,  # Filter by file types
        modified_after: Optional[datetime] = None,  # Date range filter
        modified_before: Optional[datetime] = None,  # Date range filter
        owned_by_user_id: Optional[str] = None,  # Filter by file owner
        include_subfolders: bool = True,  # Recursive scan
        exclude_trashed: bool = True,  # Skip trashed items
        max_size_mb: Optional[int] = None,  # Maximum file size
        min_size_mb: Optional[int] = None,  # Minimum file size
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use Box OAuth2 with JWT or developer token
- Environment variables: `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET`, `BOX_ENTERPRISE_ID`, `BOX_JWT_KEY_FILE`
- Support both JWT auth (for enterprises) and OAuth2 (for users)

**Key Implementation Notes:**
- Box provides SHA1 hashes for file integrity verification
- Support version tracking (Box keeps file versions)
- Handle shared folders and permissions
- Box API rate limits are generally generous but implement backoff

**Dependencies:**
- `boxsdk[jwt]` or `boxsdk`
- `cryptography` (for JWT authentication)

---

### 3. Microsoft 365 Email (Outlook/Exchange) Sync

**Script Name:** `outlook_email_sync.py`

**File-like Unit:** Individual emails and their attachments
- Emails: Cached as `.eml` or `.msg` files (`{message_id}.eml`)
- Attachments: Cached separately as original files with parent email metadata
- Metadata includes sender, recipients, subject, received_time, has_attachments

**Suggested ref_path Construction:**
- For email messages: `"email/Inbox/2024-10-14/message_id:{message_id}.eml"`
- For attachments: `"email/Inbox/2024-10-14/message_id:{message_id}/attachment_{attachment_name}"`
- **Recommendation**: Include folder path and date for organization, message ID for uniqueness

**Useful Factory Filters:**
```python
class OutlookEmailFileProxyFactory:
    def scan_messages(
        self,
        folder_path: str = "Inbox",  # Mail folder path
        received_after: datetime = None,  # REQUIRED for performance
        received_before: Optional[datetime] = None,  # Date range end
        from_address: Optional[str] = None,  # Sender email filter
        subject_contains: Optional[str] = None,  # Subject filter
        has_attachments: Optional[bool] = None,  # Filter by attachment presence
        is_read: Optional[bool] = None,  # Read/unread filter
        importance: Optional[str] = None,  # "low", "normal", "high"
        include_email_body: bool = True,  # Cache full email
        include_attachments: bool = True,  # Cache attachments separately
        max_results: Optional[int] = None,  # Limit for testing
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use Microsoft Graph API with OAuth2
- Required scopes: `Mail.Read`, `Mail.ReadBasic`
- Environment variables: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`, `AZURE_USER_EMAIL`
- Support both app-only (client credentials) and delegated auth

**Key Implementation Notes:**
- **Important**: `received_after` filter is REQUIRED to prevent scanning entire mailbox
- Email bodies can be large (especially with inline images)
- Attachment handling: Yield separate FileProxy for each attachment or embed in email file
- Consider both .eml (standard) and .msg (Outlook) formats
- Handle inline images vs. regular attachments differently
- Implement proper pagination for large mailboxes

**Dependencies:**
- `msal`
- `requests`
- `email` (stdlib, for .eml generation)

---

### 4. Gmail Sync

**Script Name:** `gmail_sync.py`

**File-like Unit:** Individual emails as EML files; attachments cached separately
- Emails: `{message_id}.eml`
- Attachments: Original files with email context metadata
- Metadata includes labels, thread_id, from, to, subject, date

**Suggested ref_path Construction:**
- For emails: `"gmail/{label}/2024-10-14/message_id:{message_id}.eml"`
- For attachments: `"gmail/{label}/2024-10-14/message_id:{message_id}/attachment_{attachment_id}_{filename}"`
- **Recommendation**: Include primary label and date for organization

**Useful Factory Filters:**
```python
class GmailFileProxyFactory:
    def scan_messages(
        self,
        label_ids: Optional[List[str]] = None,  # ["INBOX", "Important"]
        query: Optional[str] = None,  # Gmail search query
        after_date: Optional[str] = None,  # YYYY/MM/DD - REQUIRED for performance
        before_date: Optional[str] = None,  # YYYY/MM/DD
        has_attachment: Optional[bool] = None,  # Filter by attachments
        is_unread: Optional[bool] = None,  # Read/unread filter
        max_results: Optional[int] = None,  # Limit for testing
        include_email_body: bool = True,  # Cache full email
        include_attachments: bool = True,  # Cache attachments
        exclude_labels: Optional[List[str]] = None,  # ["SPAM", "TRASH"]
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use Google OAuth2 with Gmail API
- Required scopes: `https://www.googleapis.com/auth/gmail.readonly`
- Environment variables: `GOOGLE_GMAIL_CREDENTIALS_FILE` or OAuth parameters
- Support both service account and user auth

**Key Implementation Notes:**
- **Important**: Use `after_date` to limit scope and improve performance
- Gmail's query syntax is powerful—leverage it for filtering
- Message IDs are stable and unique
- Handle base64url encoding for message parts
- Gmail API has generous rate limits but implement backoff
- Labels != folders (messages can have multiple labels)

**Dependencies:**
- `google-api-python-client`
- `google-auth`
- `google-auth-httplib2`
- `google-auth-oauthlib`

---

### 5. Amazon S3 Sync

**Script Name:** `s3_daily_sync.py`

**File-like Unit:** S3 objects (individual files in buckets)
- Each S3 object cached as its original file type
- Metadata includes ETag, Content-Type, LastModified, size, storage_class

**Suggested ref_path Construction:**
- Use S3 key directly: `"s3://{bucket_name}/{key}"`
- Alternative: `"{bucket_name}/{key}"`
- **Recommendation**: Include bucket name to support multi-bucket scenarios

**Useful Factory Filters:**
```python
class S3FileProxyFactory:
    def scan_objects(
        self,
        bucket_name: str,  # REQUIRED: Target bucket
        prefix: str = "",  # Folder path/prefix filter
        file_extensions: Optional[List[str]] = None,  # Filter by extensions
        modified_after: Optional[datetime] = None,  # Date range filter
        modified_before: Optional[datetime] = None,  # Date range filter
        max_size_mb: Optional[int] = None,  # Maximum file size
        min_size_mb: Optional[int] = None,  # Minimum file size
        storage_class: Optional[str] = None,  # STANDARD, GLACIER, etc.
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use AWS credentials (access key + secret key)
- Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`
- Support IAM roles when running on EC2/ECS

**Key Implementation Notes:**
- Use `boto3` S3 client for API access
- ETags are MD5 hashes for simple uploads, different for multipart
- Consider S3 versioning if enabled on bucket
- Handle pagination for buckets with >1000 objects
- Some storage classes (GLACIER) require restore before download

**Dependencies:**
- `boto3`
- `botocore`

---

### 6. Slack Messages Sync

**Script Name:** `slack_messages_sync.py`

**File-like Unit:** Messages as JSON files; file attachments cached separately
- Messages: `{channel_id}_{timestamp}.json` containing full message data
- File attachments: Original files with message context in metadata
- Metadata includes user, timestamp, thread_ts, reactions, replies

**Suggested ref_path Construction:**
- For messages: `"slack/{channel_name}/message_{timestamp}.json"`
- For files: `"slack/{channel_name}/file_{file_id}_{filename}"`
- **Recommendation**: Use channel name for readability, timestamp for uniqueness

**Useful Factory Filters:**
```python
class SlackFileProxyFactory:
    def scan_messages(
        self,
        channel_id: str,  # REQUIRED: Target channel ID
        oldest: float,  # REQUIRED: Unix timestamp - performance critical
        latest: Optional[float] = None,  # Unix timestamp for end range
        user_id: Optional[str] = None,  # Filter to specific user
        has_files: Optional[bool] = None,  # Only messages with files
        thread_ts: Optional[str] = None,  # Specific thread only
        include_message_json: bool = True,  # Cache message JSON
        include_file_attachments: bool = True,  # Cache actual files
        max_results: Optional[int] = None,  # Limit for testing
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use Slack OAuth token or Bot token
- Required scopes: `channels:history`, `channels:read`, `files:read`
- Environment variables: `SLACK_BOT_TOKEN` or `SLACK_OAUTH_TOKEN`

**Key Implementation Notes:**
- **Important**: `oldest` timestamp is REQUIRED to prevent full channel scan
- Slack's timestamp format is unique: `"{seconds}.{microseconds}"`
- Handle threaded messages (replies have `thread_ts`)
- Files can be shared across messages (check file_id uniqueness)
- Respect Slack rate limits (Tier 3: ~50 requests/minute)
- Consider both public and private channels

**Dependencies:**
- `slack-sdk`
- `requests`

---

### 7. Microsoft Teams Messages Sync

**Script Name:** `teams_messages_sync.py`

**File-like Unit:** Messages as JSON; channel files cached separately
- Messages: `{team_id}_{channel_id}_{message_id}.json`
- Attachments/Files: Original files with metadata linking to messages
- Metadata includes from, created_datetime, importance, subject, message_type

**Suggested ref_path Construction:**
- For messages: `"teams/{team_name}/{channel_name}/message_{message_id}.json"`
- For attachments: `"teams/{team_name}/{channel_name}/message_{message_id}/attachment_{attachment_name}"`
- **Recommendation**: Use team/channel names for hierarchy, IDs for uniqueness

**Useful Factory Filters:**
```python
class TeamsFileProxyFactory:
    def scan_messages(
        self,
        team_id: str,  # REQUIRED: Target team ID
        channel_id: str,  # REQUIRED: Target channel ID
        created_after: datetime,  # REQUIRED for performance
        created_before: Optional[datetime] = None,  # Date range end
        from_user_id: Optional[str] = None,  # Filter by sender
        has_attachments: Optional[bool] = None,  # Filter by attachments
        message_type: Optional[str] = None,  # "message", "systemEventMessage"
        include_message_json: bool = True,  # Cache message JSON
        include_attachments: bool = True,  # Cache actual files
        max_results: Optional[int] = None,  # Limit for testing
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use Microsoft Graph API with OAuth2
- Required scopes: `ChannelMessage.Read.All`, `Files.Read.All`
- Environment variables: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`
- Use app-only (client credentials) authentication

**Key Implementation Notes:**
- **Important**: `created_after` is REQUIRED to limit message scope
- Teams uses Graph API (similar to Outlook email example)
- Message replies are separate entities (like threaded messages)
- Files in Teams may be stored in SharePoint (different download path)
- Handle both channel messages and chat messages differently
- Respect rate limits and implement throttling

**Dependencies:**
- `msal`
- `requests`

---

### 8. GitHub Repository Files Sync

**Script Name:** `github_repo_sync.py`

**File-like Unit:** Individual repository files at specific commit/branch
- Each file cached in original format
- Metadata includes sha (commit hash), path, size, commit info, last_modified

**Suggested ref_path Construction:**
- Use repository path: `"{repo_owner}/{repo_name}/{branch}/{file_path}"`
- Alternative with commit: `"{repo_owner}/{repo_name}/{commit_sha}/{file_path}"`
- **Recommendation**: Use branch-based paths for simplicity, track SHA in metadata

**Useful Factory Filters:**
```python
class GitHubFileProxyFactory:
    def scan_files(
        self,
        repo_owner: str,  # REQUIRED: Repository owner/org
        repo_name: str,  # REQUIRED: Repository name
        branch: str = "main",  # Branch name
        path_prefix: str = "",  # Subdirectory to scan
        file_extensions: Optional[List[str]] = None,  # Filter by file types
        exclude_patterns: Optional[List[str]] = None,  # Gitignore-style patterns
        include_deleted: bool = True,  # Track deletions
        since_commit: Optional[str] = None,  # Only files changed since this SHA
    ) -> Iterator[FileProxyBase]:
```

**Authentication:**
- Use GitHub Personal Access Token or GitHub App
- Environment variables: `GITHUB_TOKEN` or `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`
- Public repos work without auth (but rate limits are much lower)

**Key Implementation Notes:**
- Use GitHub REST API or GraphQL API
- File content is base64-encoded in API responses
- Large files (>1MB) require blob API
- Track file SHA for change detection (Git's content hash)
- Consider using Git tree API for efficient scanning
- Respect rate limits: 5000 requests/hour (authenticated)
- Handle binary files appropriately

**Dependencies:**
- `PyGithub` or `requests` (for REST API)
- `pygithub` provides nice abstractions

---

## General Implementation Specification

### Required Architecture

Each implementation MUST follow this structure:

#### 1. File Structure
```
{service}_sync.py
├── Module docstring (comprehensive 75-150 lines, includes API/auth guide)
├── Imports and constants
├── {Service}FileProxyFactory class
├── Simple{Service}Sync class
├── Helper functions (authentication, validation)
├── Main async sync function
└── CLI interface (Click)
```

#### 2. FileProxyFactory Class

The Factory pattern here is a **design approach**, not an inheritance relationship. You're creating a factory class that encapsulates the complexity of connecting to an external service and discovering files. The goal is to provide a simple, consistent interface for target selection across different data sources.

**Key principle**: Hide the messy details (API calls, pagination, authentication) behind a clean `scan_files()` or `scan_messages()` method that accepts intuitive filter parameters.

```python
class {Service}FileProxyFactory:
    """Factory for creating FileProxy instances from {Service}."""
    
    def __init__(self, <auth_params>, <connection_params>):
        """Initialize with credentials and connection details."""
        self._auth = <auth_params>
        # Store necessary connection info
    
    def scan_files(self, **filters) -> Iterator[FileProxyBase]:
        """
        Yield FileProxy instances for files matching filter criteria.
        
        Each proxy must implement:
        - ref_path(): Return unique identifier
        - fetch_data(): Return bytes or file-like object
        - get_metadata(): Return dict with at least 'modified_time'
        """
        # Enumerate files from external source
        for item in self._enumerate_items(**filters):
            yield self._create_proxy(item)
    
    def _create_proxy(self, item) -> FileProxyBase:
        """Create a FileProxy instance for a single item."""
        # Implement proxy creation logic
        pass
```

#### 3. Simple Sync Wrapper Class

```python
@dataclass
class SyncResult:
    """Result object with sync statistics."""
    insert_count: int
    update_count: int
    delete_count: int
    changes: List[ChangeNotice]
    failures: List[UpsertFailure]
    total_files_scanned: int


class Simple{Service}Sync:
    """Simplified {Service} synchronization class."""
    
    ChangeEventHandler = Callable[[ChangeNotice, Optional[FileProxyBase]], None]
    
    def __init__(
        self, 
        cache: CachedFileFolders, 
        grouping_key: str,
        config: dict,
        **service_params
    ):
        """Initialize sync instance."""
        self.cache = cache
        self.grouping_key = grouping_key
        self.config = config
        self._handlers: dict[str, Callable] = {}
    
    def set_change_handler(
        self, 
        file_extension: Union[str, Sequence[str]], 
        handler: ChangeEventHandler
    ):
        """Register callback for file type changes."""
        if isinstance(file_extension, str):
            file_extension = [file_extension.lower()]
        for ext in file_extension:
            self._handlers[ext] = handler
    
    async def sync(self, max_files: Optional[int] = None) -> SyncResult:
        """Execute synchronization with automatic change detection."""
        # 1. Authenticate with service
        # 2. Create FileProxyFactory
        # 3. Create file proxy iterator
        # 4. Call cache.resync_bulk()
        # 5. Process changes with handlers
        # 6. Return SyncResult
        pass
```

#### 4. CLI Interface

```python
@click.command()
@click.option('--cache-root', required=True, 
              help='Root directory for CachedFileFolders cache')
@click.option('--dir-key', required=True,
              help='Grouping key for organizing files')
# Add service-specific options here
@click.option('--max-files', type=int, 
              help='Limit files for testing')
@click.option('--debug', is_flag=True, 
              help='Enable debug logging')
def main(<all_params>):
    """
    {Service} Sync - Cache Design Patterns Example
    
    [Detailed docstring explaining usage]
    """
    configure_logging(debug)
    config = validate_environment()
    
    asyncio.run(sync_{service}(
        cache_root=cache_root,
        dir_key=dir_key,
        config=config,
        # ... other params
    ))
```

### Code Quality Requirements

#### Documentation

##### Module Docstring (CRITICAL REQUIREMENT)

Every example script MUST begin with a comprehensive module-level docstring (minimum 75-150 lines) that serves as complete documentation for the example. This docstring must include:

**1. Purpose and Overview**
- Clear explanation of what the example demonstrates
- Learning objectives and key design patterns showcased
- How this fits into the broader CachedFileFolders ecosystem

**2. API and Authentication Section**
- **Which API** is being used (e.g., "Microsoft Graph API v1.0", "Gmail API v1", "AWS S3 API")
- **Authentication/authorization scheme** employed (e.g., "OAuth2 client credentials flow", "Service Account with JWT", "API key authentication")
- **High-level credential acquisition process**:
  - Where to create an application/app registration (e.g., "Azure Portal → App Registrations")
  - What permissions/scopes are required (e.g., "Mail.Read, Files.Read.All")
  - What credential artifacts are needed (e.g., "Client ID, Client Secret, Tenant ID")
  - Links to official documentation for credential setup
  
**Example**:
```python
"""
## API and Authentication

This example uses the **Microsoft Graph API v1.0** to access Outlook/Exchange email.

### Authentication Scheme
Uses OAuth2 **Client Credentials Flow** (app-only authentication) via the Microsoft 
Authentication Library (MSAL). This allows the script to run unattended without user 
interaction.

### Required Credentials
You must register an application in the Azure Portal and grant it appropriate permissions:

1. Go to Azure Portal → Azure Active Directory → App Registrations
2. Create a new registration (note the Application/Client ID)
3. Under "Certificates & secrets", create a client secret
4. Under "API permissions", add:
   - Microsoft Graph → Application permissions → Mail.Read
   - Grant admin consent for your organization
5. Note your Tenant ID (Azure AD → Overview)

See: https://docs.microsoft.com/en-us/graph/auth-v2-service
"""
```

**3. Environment Variables**
- Complete list of required environment variables
- Example values (using placeholders, not real credentials)
- Clear explanation of what each variable represents

**4. Installation and Dependencies**
- Required Python packages
- Any system-level dependencies
- Installation commands

**5. Usage Examples**
- At least 2-3 realistic command-line examples
- Show both simple and complex usage scenarios
- Include examples with `--max-files` for testing

**6. File Organization**
- Explain where cached files will be stored
- Describe the directory structure created
- Explain the grouping pattern used

**7. Integration Notes**
- What production use cases this enables
- How to adapt the code for specific needs
- Known limitations or caveats

##### Other Documentation Requirements

- **Function/method docstrings**: All public functions and methods must have clear docstrings
- **Type hints**: Use throughout (Python 3.10+ syntax preferred)
- **Inline comments**: Explain non-obvious logic, especially around:
  - Authentication flows and token handling
  - API-specific quirks or limitations
  - Error handling strategies and retry logic
  - ref_path construction decisions

#### Error Handling
```python
# Example pattern for API calls
try:
    result = api_call()
except AuthenticationError as e:
    click.echo(f"❌ Authentication failed: {e}", err=True)
    sys.exit(1)
except RateLimitError as e:
    # Implement backoff/retry
    time.sleep(retry_delay)
except NetworkError as e:
    click.echo(f"⚠️  Network error: {e}", err=True)
    # Use upsert_fail_policy="RETAIN_OLD" to keep cached versions
```

#### Authentication Validation

**CRITICAL**: All connection and credential information must come from environment variables, never from command-line arguments. This function should validate that all required environment variables are set before proceeding.

```python
def validate_environment() -> dict:
    """
    Validate required environment variables are set.
    
    Returns a dictionary with lowercase keys for easy access.
    Exits with helpful error message if any required variables are missing.
    """
    # Define ALL required environment variables for this service
    required = [
        'SERVICE_CLIENT_ID',      # Application/Client ID
        'SERVICE_CLIENT_SECRET',  # API secret/key
        'SERVICE_TENANT_ID',      # Tenant/Organization ID (if applicable)
        'SERVICE_DOMAIN',         # Service domain/endpoint (if applicable)
        # Add all service-specific variables
    ]
    
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        click.echo(f"❌ Missing required environment variables: {', '.join(missing)}", 
                   err=True)
        click.echo("\nRequired environment variables:")
        for var in required:
            click.echo(f"  export {var}='your-value-here'")
        click.echo("\nSee the module docstring for credential setup instructions.")
        sys.exit(1)
    
    # Return lowercase keys for convenience
    return {var.lower(): os.getenv(var) for var in required}
```

#### Performance Optimization
```python
# Use concurrent processing
resync_result: ResyncBulkResult = await self.cache.resync_bulk(
    file_proxies=file_proxy_iterator(),
    grouping_key=[self.grouping_key],
    upsert_fail_policy="RETAIN_OLD",
    max_concurrent_requests=5  # Tune based on API rate limits
)
```

### Example Change Handlers

Demonstrate practical processing in handlers:

```python
def handle_document_change(change: ChangeNotice) -> None:
    """Handle document file changes - demonstrate slave directory usage."""
    if change.change_type in [ChangeType.INSERT, ChangeType.UPDATE]:
        # Access the cached file
        file_path = change.cur.file_path
        slave_dir = change.cur.slave_dir_path  # Pre-created directory for metadata
        
        # Example: Write processing metadata
        info_file = slave_dir / "processing_info.txt"
        info_file.write_text(
            f"File: {file_path.name}\n"
            f"Size: {file_path.stat().st_size}\n"
            f"Processed: {datetime.now().isoformat()}\n"
        )
        
        click.echo(f"📄 Processed: {file_path.name}")
    
    elif change.change_type == ChangeType.DELETE:
        # Access old file path (briefly available before cleanup)
        old_path = change.old.file_path
        click.echo(f"🗑️  Deleted: {old_path.name}")
        # Clean up any associated resources (database entries, etc.)
```

### Testing Support

Every implementation must support limited testing:

```python
def file_proxy_iterator():
    """Generator yielding FileProxy instances with optional limit."""
    files_processed = 0
    for proxy in factory.scan_files(**filter_params):
        if max_files is not None and files_processed >= max_files:
            break
        files_processed += 1
        yield proxy
```

### Dependencies Management

Create or update `pyproject.toml` section for each example:

```toml
[project.optional-dependencies]
{service}_sync = [
    "service-sdk>=1.0.0",
    "auth-library>=2.0.0",
    "click>=8.0.0",
]
```

### Logging Configuration

```python
def configure_logging(debug_enabled: bool = False):
    """Configure logging for external libraries."""
    log_level = logging.DEBUG if debug_enabled else logging.WARNING
    
    for logger_name in ['service_sdk', 'urllib3', 'requests', 'asyncio']:
        logging.getLogger(logger_name).setLevel(log_level)
    
    # Your application logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
```

---

## Quality Checklist

Before submitting any implementation, verify:

### Functionality
- [ ] Authenticates successfully with test credentials
- [ ] Discovers and enumerates files correctly with all filter options
- [ ] Downloads files without corruption (verify checksums)
- [ ] Detects INSERT/UPDATE/DELETE changes accurately
- [ ] Handles rate limits with exponential backoff
- [ ] Gracefully handles network failures and transient errors
- [ ] Respects `max_files` limit for testing
- [ ] Properly implements `ref_path` uniqueness and stability

### Code Quality
- [ ] Follows SharePoint example structure exactly
- [ ] Comprehensive module docstring with examples
- [ ] Type hints on all function signatures
- [ ] Docstrings on all public classes and methods
- [ ] Meaningful variable names (no single letters except loop counters)
- [ ] Private methods prefixed with underscore
- [ ] No hardcoded credentials (all from environment)
- [ ] Proper error messages that guide users

### Documentation
- [ ] **Module docstring is comprehensive (75-150 lines minimum)**
- [ ] **Module docstring includes API name and version**
- [ ] **Module docstring explains authentication/authorization scheme**
- [ ] **Module docstring provides step-by-step credential acquisition guide**
- [ ] **Module docstring includes links to official API documentation**
- [ ] Environment variable requirements clearly listed with example values
- [ ] At least 2-3 realistic usage examples that work as written
- [ ] Explanation of what each filter parameter does
- [ ] File organization and grouping pattern clearly explained
- [ ] Notes about service-specific quirks or limitations
- [ ] Integration notes mentioning required packages and use cases
- [ ] All public functions/methods have docstrings
- [ ] Type hints used throughout

### Testing
- [ ] Tested with `--max-files 5` for limited run
- [ ] Tested with full sync on small dataset
- [ ] Tested INSERT detection (new files)
- [ ] Tested UPDATE detection (modified files)
- [ ] Tested DELETE detection (removed files)
- [ ] Tested with various filter combinations
- [ ] Verified slave directory creation and cleanup

### Performance
- [ ] Uses async/await for concurrent downloads
- [ ] Implements appropriate `max_concurrent_requests` value
- [ ] No unnecessary API calls (efficient filtering)
- [ ] Streams large files instead of loading to memory
- [ ] Implements pagination for large result sets

### User Experience
- [ ] Clear progress indicators during sync
- [ ] Helpful error messages with actionable guidance
- [ ] Summary statistics at completion
- [ ] `--help` text is clear and complete
- [ ] `--debug` flag actually enables debug output

---

## Deliverables for Each Implementation

Submit the following for each example:

1. **Python script**: `{service}_sync.py` in `src/totodev_pub/cached_file_folders_support/examples/`
   - **Must include comprehensive module docstring (75-150 lines minimum)**
   - **Module docstring must explain API, authentication scheme, and credential acquisition**
   - **All code must follow the Factory pattern for consistency**
   - **All credentials/connection info via environment variables only**

2. **Test documentation**: Brief document showing:
   - Test environment setup steps (how you obtained credentials)
   - Complete list of environment variables set (values redacted)
   - Example commands run (at least 2-3 scenarios)
   - Screenshot or output of successful sync
   - Evidence of INSERT/UPDATE/DELETE detection working correctly

3. **Dependencies**: Any additions to `pyproject.toml`
   - List all new packages required
   - Include version constraints

4. **Known limitations**: Document any service-specific limitations or gotchas
   - API rate limits and how they're handled
   - File size limitations
   - Special cases or edge conditions
   - Any features not implemented

---

## Development Process

For each implementation:

1. **Get approval**: Submit the spec and effort estimate to project maintainers
2. **Study references**: Read all files in "Required Reading" section
3. **Set up test environment**: Create test account, get credentials
4. **Implement skeleton**: Start with CLI and authentication
5. **Build FileProxy**: Implement the factory and proxy classes
6. **Integrate CachedFileFolders**: Wire up the sync logic
7. **Test thoroughly**: Use the quality checklist
8. **Document**: Ensure all documentation requirements are met
9. **Submit**: Provide all deliverables for review

---

## Support and Questions

If you encounter issues or have questions:

1. **Review the reference**: `sharepoint_daily_tree_sync.py` has examples of most patterns
2. **Check existing tests**: See how the library is tested
3. **Read API documentation**: For the external service
4. **Ask specific questions**: With code snippets and error messages

Remember: These are example/demonstration projects meant to showcase best practices. Prioritize code clarity and educational value over performance optimization.

---

## Notes on ref_path Design

The `ref_path` is the single most important aspect of your FileProxy implementation. Take time to design it well:

### Good ref_path Examples
- File-based: `"documents/2024/report.pdf"` (hierarchical, readable)
- Email: `"email/Inbox/2024-10-14/message_id:abc123.eml"` (organized by date)
- Message: `"slack/general/msg_1634567890.123456.json"` (includes timestamp)
- S3: `"s3://my-bucket/path/to/file.txt"` (includes bucket for multi-bucket support)

### Bad ref_path Examples
- Too short: `"file123"` (not meaningful)
- Not unique: `"report.pdf"` (could have duplicates)
- Unstable: Using temporary IDs that change
- Overly complex: Encoding too much metadata in the path

### Testing ref_path Stability
After implementation, verify:
1. Same file always gets same ref_path across runs
2. Different files never get the same ref_path
3. File moves/renames are detected appropriately (new ref_path = DELETE old + INSERT new)

---

## Key Requirements Summary

Before you begin, ensure you understand these critical requirements:

### ✅ Factory Pattern
- Your `{Service}FileProxyFactory` is a **design pattern**, not a base class
- Goal: Make target selection simple and consistent
- Hide complexity behind a clean `scan_files()` or `scan_messages()` method
- Accept intuitive filter parameters appropriate to the data source

### ✅ Environment Variables
- **ALL credentials and connection info via environment variables**
- **NO sensitive data in command-line arguments**
- CLI args only for cache config and operational parameters
- `validate_environment()` must check all required variables at startup

### ✅ Module Docstring
- **Minimum 75-150 lines of comprehensive documentation**
- **Must include**: API name/version, auth scheme, credential acquisition steps
- **Must provide**: Step-by-step guide to getting credentials
- **Must link**: Official API documentation
- This is the primary documentation—make it excellent

### ✅ ref_path Design
- Acts as primary key linking external source to cache
- Must be unique, stable, and meaningful
- File-based systems: Use hierarchical paths
- Message-based systems: Construct from IDs with context

### ✅ Follow the Pattern
- Study `sharepoint_daily_tree_sync.py` thoroughly
- Match its structure, style, and documentation quality
- Maintain consistency across all examples
- Educational value is as important as functionality

---

**Remember**: Do not begin implementation without approval. Each example must be scoped and approved individually.
