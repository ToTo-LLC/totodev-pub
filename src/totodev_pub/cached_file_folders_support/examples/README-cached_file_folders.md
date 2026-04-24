# CachedFileFolders and CacheGrouping Tutorial Examples

This directory contains comprehensive tutorial examples demonstrating how to use the CachedFileFolders library effectively.  CachedFileFolders is a medium-duty tool for moderate sized business scenarios (thousands but not tens of thousands of items).

## Available Examples

### SharePoint Tutorial (`sharepoint_tutorial.py`)

A complete tutorial that demonstrates CachedFileFolders integration with SharePoint using Microsoft Graph API.

**What it demonstrates:**
- Intelligent file caching with automatic change detection
- Hierarchical file organization using grouping patterns
- Concurrent file downloads with retry logic
- Slave directory usage for metadata storage
- File retention policies for safety

**Key CachedFileFolders features shown:**
- `CachedFileFolders` configuration and setup
- `SharepointFileProxyFactory` for SharePoint integration
- `resync_bulk()` for efficient file synchronization
- Slave directories for storing related files alongside cached documents

**Prerequisites:**
- Microsoft Azure App Registration with SharePoint permissions
- Python packages: `msal`, `requests`
- SharePoint site access

**Usage:**
1. Update the configuration constants at the top of the file
2. Run the tutorial directly as a script:
   ```bash
   python sharepoint_tutorial.py
   ```

## Getting Started

1. **Choose an example** that matches your use case
2. **Update configuration** constants at the top of the example file
3. **Install dependencies** as specified in the example
4. **Run the example** to see CachedFileFolders in action

## Key Concepts Demonstrated

### File Proxies
File proxies are abstract representations of files from any source (SharePoint, S3, local files, etc.). They provide a consistent interface for CachedFileFolders to work with different file sources.

### Grouping Patterns
Templates for organizing cached files in a hierarchical structure:
- `"sites/{site_name}/"` - Organize by site name
- `"projects/{project_id}/files/"` - Organize by project
- `"users/{user_id}/documents/"` - Organize by user

### Change Detection
CachedFileFolders automatically detects file changes using:
- File size comparison
- Modification date comparison
- Optional content hashing (xxhash)

### Slave Directories
Automatic metadata directories created alongside each cached file:
```
Documents/
├── report.pdf
└── report.pdf._slave/          # Slave directory
    ├── ocr_text.txt        # OCR content
    ├── thumbnail.jpg       # Preview image
    └── metadata.yaml       # Standardized metadata file
```

### Lightweight Metadata Files

CachedFileFolders provides lightweight accessors for a metadata file (default: `metadata.yaml`) in each file's slave directory for tracking processing state and other per-file information.  This file allows you to 
easily read/write a simple dict-like structure.  For serious metadata needs, you might ignore this facility
and implement your own strategy (such as using a FileMappedPydanticMixin).

```python
# Write metadata after processing
notice = cache.upsert_file(document, ["project", "docs"])
if notice:
    meta = notice.metadata()  # Returns LazyLoadedFileData
    meta.overwrite_source_file({
        'processed_at': datetime.now().isoformat(),
        'ocr_completed': True,
        'indexed': False
    })

# Query metadata during sync
for change in resync_result.changes:
    meta = change.metadata()
    if meta and not meta.get('ocr_completed'):
        process_ocr(change.cur.file_path)
```

**Key Features:**

- Zero-cost abstraction (no overhead if unused)
- Dict-like access with `meta.get('key')`
- Can return a default dict if file doesn't exist (no existence checks needed)
- Automatic change detection (reloads when file modified, subject to frequency guidelines)
- Available on both `ChangeNotice` and `CachedFileRef` objects
- Customizable filename (default: `metadata.yaml`, can use `metadata.json`, etc.)

**Common Use Cases:**

- Track OCR/processing completion status
- Store embedding/vector database IDs
- Record active job identifiers
- Track validation or quality check results

### Lightweight Event Logging

Alongside the metadata helper, every cached file exposes a convenience method for event tracking.  Calling
`event_log()` on a `CachedFileRef` (or on `ChangeNotice.cur/old`) returns a ready-to-use `PrimitiveEventLog`
rooted in the file's slave directory (default subfolder: `events/`).  This is perfect for recording the
lightweight lifecycle of a document—queueing, processing milestones, validation status—without standing up a
database or message bus.  The log lives next to the file so workers, background jobs, and human operators can
inspect progress with simple tooling.

```python
# Record processing milestones per cached file
ref = cache.upsert_file(document, ["legal", "2025"])
if ref:
    log = ref.event_log()
    log.create_event("OCR-STATUS", "QUEUED")
    log.create_event("OCR-STATUS", "PROCESSING", {"pages": 12})
    log.create_event("OCR-STATUS", "COMPLETED", {"duration_secs": 18})

    # Snapshot current state for orchestration decisions
    values = log.latest_values()
    if values.get("OCR-STATUS") == "COMPLETED":
        schedule_embedding_job(ref.file_path)

# Later, reconcile any files that stalled
for change in resync_result.changes:
    log = change.cur.event_log()
    if log.has_event("VALIDATION-STATUS") != "APPROVED":
        trigger_review(change.cur.file_path)
```

**Key Features:**

- Zero-config: directories materialize automatically on first use (no `force=True` needed)
- Sequence-preserving filenames (`e001_LABEL@VALUE.yaml`) keep history human-inspectable
- Dict-like payloads with optional typed loading for rich event metadata
- Thread/process safe sequencing that works across distributed workers
- Works anywhere a `CachedFileRef` appears (bulk sync changes, factory results, manual lookups)

**Common Use Cases:**

- Track doc processing pipeline state (queued → textified → summarized)
- Attach audit breadcrumbs to compliance-sensitive documents
- Coordinate asynchronous jobs that need to observe each other's milestones
- Provide lightweight operator dashboards by enumerating event directories

### Concurrent Operations
CachedFileFolders handles multiple file operations simultaneously for better performance, with configurable concurrency limits and retry logic.

## Contributing

When adding new examples:
1. Follow the tutorial style with clear explanations
2. Include comprehensive docstrings and comments
3. Use clear configuration constants at the top
4. Demonstrate key CachedFileFolders features
5. Include error handling and progress reporting
6. Add a README entry describing the example
