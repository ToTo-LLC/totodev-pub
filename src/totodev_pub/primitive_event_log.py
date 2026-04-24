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
        
        if force:
            self.event_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_placeholders(self.event_dir)
    
    def events(
        self,
        label_glob: str = '*',
        value_glob: str = '*',
        recent_first: bool = True
    ) -> Generator[PrimitiveEventProxy, None, None]:
        """
        Generator yielding PrimitiveEventProxy objects matching glob patterns.
        
        Args:
            reverse: If True, newest first; if False, oldest first
            label_glob: Pattern to filter labels (default: '*' for all labels)
            value_glob: Pattern to filter values (default: '*' for all values)
            recent_first: events yielded in recent first order, else oldest first
            
        Yields:
            Matching events (newest first by default)
            
        Example:
            for event in log.events(label_glob='OCR-*', value_glob='COMPLETE*'):
                print(f"{event.label_value} at {event.mtime}")
        """
        if not self.event_dir.exists():
            return
        
        # Collect matching event proxies
        matching_proxies = []
        
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
            
            seq_num = int(match.group(1))
            label = match.group(2)
            value = match.group(3) or ""
            
            # Filter by label glob
            if not fnmatch.fnmatch(label, label_glob):
                continue
            
            # Filter by value glob
            if not fnmatch.fnmatch(value, value_glob):
                continue
            
            # Create PrimitiveEventProxy with parsed values
            proxy = PrimitiveEventProxy(
                file_path=file_path,
                label=label,
                value=value
            )
            matching_proxies.append(proxy)
        
        # Sort by (directory, filename) tuple - proxy's __lt__ handles this
        matching_proxies.sort(reverse=recent_first)
        
        # Yield results
        for proxy in matching_proxies:
            yield proxy
    
    def latest_values(
        self,
        label_glob: str = '*'
    ) -> MappingProxyType[str, str]:
        """
        Read-only dict mapping each label to its latest value.
        
        Args:
            label_glob: Pattern to filter labels (default: '*' for all labels)
        
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
        for event in self.events(label_glob=label_glob, recent_first=False):
            result[event.label] = event.value  # Later events overwrite earlier ones
        
        return MappingProxyType(result)
    
    def has_event(self, label: str) -> str | bool:
        """Check if event with exact label exists, returning value string, True, or False."""
        event = next(self.events(label_glob=label), None)
        return event.value if event and event.value else (True if event else False)
    
    def segment_events(
        self,
        start_label_globs: str | Sequence[str],
        start_value_glob: str = "*",
    ) -> Generator[tuple[PrimitiveEventProxy, ...], None, None]:
        """
        Yield chronological event segments partitioned by marker events.

        Each time an event matches any of the provided label globs in combination
        with the value glob, a new segment begins that includes the matching event.
        Events prior to the first marker are ignored.

        Args:
            start_label_globs: Glob pattern or sequence of patterns for label matching.
            start_value_glob: Glob pattern applied to event values (default '*').

        Yields:
            Tuples of PrimitiveEventProxy instances, each representing one segment.
            Each yielded tuple begins with an event that matches the start_label_globs and start_value_glob,
            and is followed by the sequential events that occur before the next marker event.
        """
        if isinstance(start_label_globs, str):
            label_patterns: Sequence[str] = (start_label_globs,)
        else:
            label_patterns = tuple(start_label_globs)

        if not label_patterns:
            return

        current_segment: list[PrimitiveEventProxy] = []
        for event in self.events(recent_first=False):
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


