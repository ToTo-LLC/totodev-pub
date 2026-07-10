# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
PrimitiveEventLog: File-based event logging without a database.

A lightweight event logging system where each event is a file with a structured name.
Just point it at a folder and go.

Conceptually, stores a set of label-value pairs in creation order.
Intended that each "event" is a file that can stor payload data.

Labels might be things like "OCR-STATUS", "VALIDATION-STATUS", "ANALYSIS-STATUS", etc.
Values might be things like "QUEUED", "PROCESSING", "COMPLETED", "FAILED", etc.

Makes it easy to:
- ask, "What is the current 'OCR-STATUS'?" using `latest_values()['ANALYSIS-STATUS']`
- log an event with a dict of data with `create_event('OCR-STATUS', 'COMPLETED', {"pages": 5, "confidence": 0.95})`

Key Features:
- Human/AI-browsable: File names show sequence and status at a glance
- Multiple dimensions: Track independent status labels simultaneously  
- Concurrency-safe: Guaranteed in-order without needing database
- Easy payloads: easily save/load dicts or Pydantic models
- Atomic writes: Readers never see incomplete or corrupt event files

Best Practices:
- **Don't edit event files after creation**: Events are append-only by design.
  To update state, create a new event with the updated status/data.
  Editing existing events can break the chronological integrity of the log.

Quick Start:
    ```python
    from pathlib import Path
    from totodev_pub.primitive_event_log import PrimitiveEventLog
    
    # Create log
    log = PrimitiveEventLog(event_dir=Path("./document_events"))
    
    # Create events with different payload styles
    log.create_event("OCR-STATUS", "QUEUED")  # Marker (no data)
    log.create_event("OCR-STATUS", "PROCESSING", {"page": 1})  # Dict
    log.create_event("OCR-STATUS", "COMPLETED", {"pages": 5, "confidence": 0.95})
    
    # Check current status
    if log.has_event("OCR-STATUS") == "COMPLETED":
        print("OCR is done!")
    
    # Get values snapshot across all labels
    values = log.latest_values()
    # MappingProxyType({'OCR-STATUS': 'COMPLETED', 'VALIDATION-STATUS': 'QUEUED'})
    
    # Review history
    for event in log.events(label_glob="OCR-*"):
        print(f"{event.label_value} at {event.mtime}")
        data = event.contents()  # LazyLoadedFileData (dict-like)
        if data:
            print(f"  Pages: {data.as_dict().get('pages', 'N/A')}")
    ```

Typed Pydantic Payloads:
    ```python
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    class OCREnvelope(BaseModel, FileMappedPydanticMixin):
        pages: int
        confidence: float
    
    # Create with type-safe payload
    envelope = OCREnvelope(pages=5, confidence=0.95)
    log.create_event("OCR-STATUS", "COMPLETED", envelope)
    
    # Read back with type checking
    event = next(log.events(label_glob="OCR-STATUS"))
    data = event.contents(load_class=OCREnvelope)  # Typed!
    print(data.confidence)  # IDE knows this is a float
    ```

File Naming Convention:
    e{seq:03d}_{LABEL}@{VALUE}.{ext}
    
    Examples:
        e001_OCR-STATUS@QUEUED.yaml
        e002_OCR-STATUS@PROCESSING.yaml
        e003_VALIDATION-STATUS@QUEUED.yaml
        e004_OCR-STATUS@COMPLETED.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Type, Generator, Literal, Any, Mapping, Sequence, TYPE_CHECKING
from types import MappingProxyType
import fnmatch
import time
import yaml
import json
import shutil
import re

if TYPE_CHECKING:
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

from totodev_pub.primitive_event_log_support.event_proxy import (
    PrimitiveEventProxy,
    EVENT_FILE_PATTERN,
    SEQUENCE_PATTERN,
    PLACEHOLDER_SUFFIX,
    LOCK_SUFFIX
)


class PrimitiveEventLog:
    """
    File-based event logging system with human-browsable directory structure.
    
    See module docstring for full documentation, examples, and use cases.
    """
    
    def __init__(
        self,
        event_dir: str | Path,
        digits: int = 3,
        force: bool = False,
        file_format: Literal["yaml", "json"] = "yaml"
    ):
        """
        Initialize the event log.
        
        Args:
            digits: Zero-padded width for sequence numbers (e.g., 3 → e001, e002, ...)
            force: If True, creates event_dir immediately and cleans stale placeholders
            file_format: Serialization format for payload data
        """
        self.event_dir = Path(event_dir)
        self.digits = digits
        self.file_format = file_format.lower()
        
        # Validate file format
        if self.file_format not in ("yaml", "json"):
            raise ValueError(f"file_format must be 'yaml' or 'json', got '{file_format}'")
        
        self._file_extension = self.file_format

        # Lazily-populated shared raw scan: (monotonic timestamp, unfiltered
        # proxies). Stays None — no memory/behavior cost — until some caller
        # passes cache_msecs > 0. See `_cached_scan`.
        self._cache_scan: Optional[tuple[float, list[PrimitiveEventProxy]]] = None

        if force:
            self.event_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_placeholders(self.event_dir)

    def events(
        self,
        label_glob: str = '*',
        value_glob: str = '*',
        recent_first: bool = True,
        cache_msecs: int = 0,
    ) -> Generator[PrimitiveEventProxy, None, None]:
        """
        Generator yielding PrimitiveEventProxy objects matching glob patterns.
        
        Args:
            reverse: If True, newest first; if False, oldest first
            label_glob: Pattern to filter labels (default: '*' for all labels)
            value_glob: Pattern to filter values (default: '*' for all values)
            recent_first: events yielded in recent first order, else oldest first
            cache_msecs: Tolerate a directory scan up to this many milliseconds
                old instead of always re-walking the folder. 0 (default)
                disables caching entirely: every call re-scans, exactly like
                before this parameter existed. A positive value shares ONE
                cached scan across every caller on this instance that also
                passes cache_msecs > 0 — label_glob/value_glob are applied
                in-memory afterward, so they don't fragment the cache. Any
                create_event() or purge() on this instance invalidates the
                cache, so a writer always sees its own writes regardless of
                cache_msecs. A race between threads just costs a redundant
                scan, never stale-forever data.
            
        Yields:
            Matching events (newest first by default)
            
        Example:
            for event in log.events(label_glob='OCR-*', value_glob='COMPLETE*'):
                print(f"{event.label_value} at {event.mtime}")
        """
        proxies = self._cached_scan(cache_msecs) if cache_msecs > 0 else self._scan_all()

        matching_proxies = [
            proxy for proxy in proxies
            if fnmatch.fnmatch(proxy.label, label_glob) and fnmatch.fnmatch(proxy.value, value_glob)
        ]

        # Sort by (directory, filename) tuple - proxy's __lt__ handles this
        matching_proxies.sort(reverse=recent_first)

        # Yield results
        for proxy in matching_proxies:
            yield proxy

    def _scan_all(self) -> list[PrimitiveEventProxy]:
        """Walk the event directory and parse every real event file into a
        proxy. Unfiltered and unsorted — callers apply glob filtering and
        ordering themselves. The one place that touches the filesystem to
        discover events; `events()` calls this directly (cache_msecs <= 0)
        or via `_cached_scan` (cache_msecs > 0)."""
        if not self.event_dir.exists():
            return []

        proxies = []

        # NOTE: Could optimize by using self.event_dir.glob() as pre-filter, then fnmatch for exact matching
        for file_path in self.event_dir.iterdir():
            # Skip placeholder, lock, and any temp files
            # Placeholders serve as atomic write temp files (written to, then renamed)
            if (file_path.name.endswith(PLACEHOLDER_SUFFIX) or 
                file_path.name.endswith(LOCK_SUFFIX) or
                '.tmp' in file_path.suffixes):
                continue
            
            # Parse filename: e{seq}_{label}@{value}.{ext}
            match = re.match(EVENT_FILE_PATTERN, file_path.name)
            if not match:
                # Skip silently if invalid format
                continue

            label = match.group(2)
            value = match.group(3) or ""

            proxies.append(PrimitiveEventProxy(
                file_path=file_path,
                label=label,
                value=value
            ))

        return proxies

    def _cached_scan(self, cache_msecs: int) -> list[PrimitiveEventProxy]:
        """Serve the shared raw scan if it's no older than `cache_msecs`,
        otherwise refresh it. Assumes cache_msecs > 0 — callers check that."""
        now = time.monotonic()
        if self._cache_scan is not None:
            scanned_at, cached_proxies = self._cache_scan
            if (now - scanned_at) * 1000 <= cache_msecs:
                return cached_proxies

        fresh = self._scan_all()
        self._cache_scan = (now, fresh)
        return fresh
    
    def latest_values(
        self,
        label_glob: str = '*',
        cache_msecs: int = 0,
    ) -> MappingProxyType[str, str]:
        """
        Read-only dict mapping each label to its latest value.
        
        Args:
            label_glob: Pattern to filter labels (default: '*' for all labels)
            cache_msecs: Forwarded to `events()` — see its docstring.
        
        Returns:
            Read-only dict {label: most_recent_value} for all matching labels
            
        Example:
            # Basic usage
            values = log.latest_values()
            # MappingProxyType({'OCR-STATUS': 'COMPLETED', 'VALIDATION': 'PASSED'})
            
            if values.get('OCR-STATUS') == 'COMPLETED':
                proceed_to_next_stage()
            
        """
        # Build fresh result
        result = {}
        for event in self.events(label_glob=label_glob, recent_first=False, cache_msecs=cache_msecs):
            result[event.label] = event.value  # Later events overwrite earlier ones
        
        return MappingProxyType(result)
    
    def has_event(self, label: str, cache_msecs: int = 0) -> str | bool:
        """Check if event with exact label exists, returning value string, True, or False.

        cache_msecs is forwarded to `events()` — see its docstring."""
        event = next(self.events(label_glob=label, cache_msecs=cache_msecs), None)
        return event.value if event and event.value else (True if event else False)
    
    def segment_events(
        self,
        start_label_globs: str | Sequence[str],
        start_value_glob: str = "*",
        cache_msecs: int = 0,
    ) -> Generator[tuple[PrimitiveEventProxy, ...], None, None]:
        """
        Group the event history into chronological runs, each starting at a matching event.

        Use this to break a log into repeated "episodes"—e.g. every time a state
        machine re-enters a state, or every retry attempt—so you can inspect each
        episode's events on their own. Every event that matches start_label_globs
        (by label) and start_value_glob (by value) begins a new segment; that
        matching event is included, followed by all subsequent events up to
        (but not including) the next matching event.

        Args:
            start_label_globs: Glob pattern or sequence of patterns for label matching.
            start_value_glob: Glob pattern applied to event values (default '*').
            cache_msecs: Forwarded to `events()` — see its docstring.

        Yields:
            Tuples of PrimitiveEventProxy, oldest segment first. Each tuple starts
            with a matching event and contains the events that follow it, up to
            the next match.

        Edge cases:
            - Events before the first match are dropped entirely (no leading partial segment).
            - The final segment runs to the end of the log; it doesn't need a closing match.
            - No matches at all yields nothing (an empty iterator).

        Example:
            log.create_event('STATE', 'IDLE')      # dropped: before first match
            log.create_event('STATE', 'ENTER')      # starts segment 1
            log.create_event('ACTION', 'RUNNING')   # in segment 1
            log.create_event('STATE', 'ENTER')      # starts segment 2
            log.create_event('ACTION', 'DONE')      # in segment 2

            segments = list(log.segment_events('STATE', start_value_glob='ENTER'))
            # [(STATE@ENTER, ACTION@RUNNING), (STATE@ENTER, ACTION@DONE)]
        """
        if isinstance(start_label_globs, str):
            label_patterns: Sequence[str] = (start_label_globs,)
        else:
            label_patterns = tuple(start_label_globs)

        if not label_patterns:
            return

        current_segment: list[PrimitiveEventProxy] = []
        for event in self.events(recent_first=False, cache_msecs=cache_msecs):
            if any(fnmatch.fnmatch(event.label, pattern) for pattern in label_patterns) and fnmatch.fnmatch(event.value, start_value_glob):
                if current_segment:
                    yield tuple(current_segment)
                current_segment = [event]
                continue

            if current_segment:
                current_segment.append(event)

        if current_segment:
            yield tuple(current_segment)

    def create_event(
        self,
        label: str,
        value: str,
        data: Optional[FileMappedPydanticMixin | Mapping | dict] = None
    ) -> PrimitiveEventProxy:
        """
        Create new event with concurrency-safe sequence numbering.
        
        The 'data' parameter accepts three styles:
        - None: Empty marker file (status flag only)
        - dict/Mapping: Quick and flexible (serialized to YAML/JSON)
        - Pydantic with FileMappedPydanticMixin: Type-safe (validated on write)
        
        Note:
            Labels and values become part of the filename (e{seq}_{label}@{value}.ext),
            so they cannot contain filesystem-illegal characters like: / \\ : * ? " < > |
        
        Returns:
            PrimitiveEventProxy for the created event
            
        Raises:
            RuntimeError: If unable to claim sequence number after 100 attempts
            TypeError: If data is not None, dict/Mapping, or Pydantic with mixin
            
        Examples:
            # Marker file
            log.create_event('STATUS', 'READY')
            
            # Dict payload
            log.create_event('OCR', 'DONE', {'pages': 5, 'confidence': 0.95})
            
            # Typed Pydantic payload
            envelope = OCREnvelope(pages=5, confidence=0.95)
            log.create_event('OCR', 'DONE', envelope)
        """
        # Ensure event directory exists
        self.event_dir.mkdir(parents=True, exist_ok=True)
        
        # Get starting sequence number
        next_seq = self._get_next_sequence_number(self.event_dir)
        
        # Try to claim a sequence number
        for attempt in range(100):
            seq = next_seq + attempt
            placeholder_path = self.event_dir / f"e{seq:0{self.digits}d}{PLACEHOLDER_SUFFIX}"
            
            try:
                # Try to create placeholder (fails if exists)
                placeholder_path.touch(exist_ok=False)
                
                # We got the sequence number! Now write to placeholder and rename
                try:
                    # Build final filename
                    filename = f"e{seq:0{self.digits}d}_{label}@{value}.{self._file_extension}"
                    file_path = self.event_dir / filename
                    
                    # Write data directly to the placeholder file
                    # This is our atomic write: placeholder → final name
                    if data is None:
                        # Placeholder is already created (empty), just rename it
                        pass
                    elif isinstance(data, Mapping):
                        # Dict/Mapping - serialize to placeholder
                        with open(placeholder_path, 'w') as f:
                            if self.file_format == 'yaml':
                                yaml.safe_dump(dict(data), f, default_flow_style=False, sort_keys=False)
                            else:
                                json.dump(dict(data), f, indent=2)
                    elif hasattr(data, 'save') and callable(getattr(data, 'save')):
                        # Pydantic model with FileMappedPydanticMixin
                        data.save(str(placeholder_path), format_override=self.file_format)
                    else:
                        raise TypeError(
                            f"data must be None, dict/Mapping, or Pydantic model with FileMappedPydanticMixin. "
                            f"Got {type(data).__name__}"
                        )
                    
                    # Atomic rename: placeholder → final event file
                    # Readers never see the placeholder because events() filters them out
                    placeholder_path.rename(file_path)

                    # Invalidate any cached scan: a caller that both writes and
                    # reads through this same instance must always see its own
                    # write, regardless of a positive cache_msecs on the read.
                    self._cache_scan = None

                    # Create and return PrimitiveEventProxy
                    proxy = PrimitiveEventProxy(
                        file_path=file_path,
                        label=label,
                        value=value
                    )
                    
                    return proxy
                    
                finally:
                    # Best effort cleanup of placeholder if rename failed
                    # (If rename succeeded, this will silently fail - that's fine)
                    try:
                        placeholder_path.unlink()
                    except:
                        pass  # Don't care if this fails
                        
            except FileExistsError:
                # Someone else got this sequence number, try next
                continue
        
        # If we get here, we failed after 100 attempts
        raise RuntimeError(f"Failed to create event after 100 attempts")
    
    def purge(self) -> None:
        """
        Delete the entire event directory and all contents (irreversible).
        
        The event log tolerates the missing directory and recreates it on next write.
        
        Example:
            log.purge()  # All events gone
            log.create_event('STATUS', 'RESET')  # Directory recreated
        """
        if self.event_dir.exists():
            shutil.rmtree(self.event_dir)
        # A stale cached scan would hold proxies pointing at now-deleted files.
        self._cache_scan = None
    
    @staticmethod
    def _cleanup_placeholders(event_dir: Path, age_seconds: float = 60) -> int:
        """Remove stale placeholder files from crashed processes."""
        if not event_dir.exists():
            return 0
        current_time = time.time()
        removed = 0
        for f in event_dir.iterdir():
            if f.name.endswith(PLACEHOLDER_SUFFIX) and (current_time - f.stat().st_mtime) > age_seconds:
                try:
                    f.unlink()
                    removed += 1
                except:
                    pass
        return removed
    
    @staticmethod
    def _get_next_sequence_number(event_dir: Path) -> int:
        """Get the next available sequence number by scanning existing files."""
        if not event_dir.exists():
            return 1
        
        max_seq = 0
        for file_path in event_dir.iterdir():
            match = re.match(SEQUENCE_PATTERN, file_path.name)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        
        return max_seq + 1


################### END PrimitiveEventLog class ###################


