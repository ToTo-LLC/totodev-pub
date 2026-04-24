# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
PrimitiveEventProxy: Lazy-loading proxy for individual event files.

Provides metadata from filename (cheap) and lazy-loads event content on demand.
Designed to minimize I/O while providing convenient access to event data.

Event data can be loaded as:
- LazyLoadedFileData (dict-like, no type checking) - default
- Typed Pydantic models (with validation) - when load_class specified
"""

from pathlib import Path
from typing import Optional, Literal, TYPE_CHECKING, Type, Union
from datetime import datetime
from functools import total_ordering
import time
import re

if TYPE_CHECKING:
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData


# File pattern constants for event files
EVENT_FILE_PATTERN = r'e(\d+)_(.+?)(?:@(.+))?\.(yaml|json)$'
SEQUENCE_PATTERN = r'e(\d+)_'
PLACEHOLDER_SUFFIX = '_placeholder.tmp'
LOCK_SUFFIX = '.lock'


@total_ordering
class PrimitiveEventProxy:
    """
    Represents a single event file with lazy loading of contents.
    
    Metadata (filename, filesystem stats) is cheap to access.
    Event content is loaded only when accessed via contents() and cached.
    """
    
    def __init__(
        self,
        file_path: Path,
        label: str,
        value: str
    ):
        """
        Create a PrimitiveEventProxy for a specific event file.
        
        Args:
            file_path: Path to the event file
            label: Event label (e.g., 'OCR-STATUS')
            value: Event value (e.g., 'COMPLETED')
        """
        self._file_path = Path(file_path)
        self._label = label
        self._value = value
        
        # Cached values for contents
        self._contents_cache = {}  # Cache by load_class (None or class name)
        self._contents_load_times = {}
    
    # =========================================================================
    # Properties - No I/O (parsed from filename)
    # =========================================================================
    
    @property
    def label(self) -> str:
        """Event label (e.g., 'OCR-STATUS' from 'e003_OCR-STATUS@COMPLETED.yaml')."""
        return self._label
    
    @property
    def value(self) -> str:
        """Event value (e.g., 'COMPLETED' from 'e003_OCR-STATUS@COMPLETED.yaml')."""
        return self._value
    
    @property
    def label_value(self) -> str:
        """Combined label@value (e.g., 'OCR-STATUS@COMPLETED')."""
        if self._value:
            return f"{self._label}@{self._value}"
        return self._label
    
    @property
    def file_path(self) -> Path:
        """Full path to event file."""
        return self._file_path
    
    @property
    def file_format(self) -> str:
        """File format deduced from extension ('yaml' or 'json')."""
        return self._file_path.suffix[1:]  # ".yaml" -> "yaml"
    
    @property
    def exists(self) -> bool:
        """Quick check if file still exists (cheap filesystem check)."""
        return self._file_path.exists()
    
    # =========================================================================
    # Properties - Cheap I/O (single stat() call)
    # =========================================================================
    
    @property
    def mtime(self) -> datetime:
        """File modification time."""
        return datetime.fromtimestamp(self._file_path.stat().st_mtime)
    
    @property
    def ctime(self) -> datetime:
        """File creation time."""
        return datetime.fromtimestamp(self._file_path.stat().st_ctime)
    
    def age(
        self,
        interval: Literal["seconds", "minutes", "hours", "days"] = "seconds",
        relative: bool = False
    ) -> float:
        """
        Time since event in specified interval.
        
        Args:
            interval: Time unit for return value
            relative: If True, time since first event in directory; if False, time since now
            
        Returns:
            Age in specified interval
            
        Example:
            if event.age("hours") > 24:
                print("Event is over a day old")
            
            # Time since log started (first event in directory)
            lifecycle_time = event.age("minutes", relative=True)
        """
        if relative:
            # Find the first event in the directory by scanning for lowest sequence number
            first_mtime = self._find_first_event_mtime()
            if first_mtime is None:
                return 0.0
            delta_seconds = self.mtime.timestamp() - first_mtime
        else:
            delta_seconds = time.time() - self.mtime.timestamp()
        
        # Convert to requested interval
        conversions = {
            "seconds": 1.0,
            "minutes": 60.0,
            "hours": 3600.0,
            "days": 86400.0
        }
        return delta_seconds / conversions[interval]
    
    def _find_first_event_mtime(self) -> Optional[float]:
        """Find the mtime of the first event (lowest sequence number) in the directory."""
        event_dir = self._file_path.parent
        if not event_dir.exists():
            return None
        
        min_seq = None
        first_mtime = None
        
        for file_path in event_dir.iterdir():
            # Skip non-event files
            if file_path.name.endswith(PLACEHOLDER_SUFFIX) or file_path.name.endswith(LOCK_SUFFIX):
                continue
            
            # Parse sequence number
            match = re.match(SEQUENCE_PATTERN, file_path.name)
            if match:
                seq_num = int(match.group(1))
                if min_seq is None or seq_num < min_seq:
                    min_seq = seq_num
                    first_mtime = file_path.stat().st_mtime
        
        return first_mtime
    
    # =========================================================================
    # Content Loading - Lazy (loads file data)
    # =========================================================================
    
    def contents(
        self, 
        load_class: Optional[Type['FileMappedPydanticMixin']] = None,
        tolerate_secs: float = 1e6
    ) -> Optional[Union['FileMappedPydanticMixin', LazyLoadedFileData]]:
        """
        Load and return the event data (cached).
        
        Args:
            load_class: Optional Pydantic model class with FileMappedPydanticMixin for typed deserialization.
                       Must have .load() method for deserializing from YAML/JSON.
                       If None, returns LazyLoadedFileData (dict-like with conveniences).
            tolerate_secs: Maximum age of cached data in seconds.
                          Use 0 to force reload from disk.
                          Default 1e6 (~11.5 days) effectively means "cache forever"
                          
        Returns:
            - LazyLoadedFileData if load_class is None (dict-like access)
            - Pydantic instance if load_class provided (typed access)
            - None if file is empty (marker file)
            
        Examples:
            # Get dict-like data
            data = event.contents()
            if data:
                pages = data.as_dict().get('pages', 0)
            
            # Get typed Pydantic model (must include FileMappedPydanticMixin)
            from pydantic import BaseModel
            from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
            
            class OCREnvelope(BaseModel, FileMappedPydanticMixin):
                pages: int
                confidence: float
            
            ocr = event.contents(load_class=OCREnvelope)
            print(ocr.pages)  # Type-safe access
            
            # Force fresh load from disk
            data = event.contents(tolerate_secs=0)
        """
        # Check if file is empty (marker file)
        if not self._file_path.exists() or self._file_path.stat().st_size == 0:
            return None
        
        # Determine cache key
        cache_key = load_class.__name__ if load_class else None
        
        # Check if we need to reload
        current_time = time.time()
        cache_age = (current_time - self._contents_load_times.get(cache_key, 0)) 
        
        if cache_key not in self._contents_cache or cache_age > tolerate_secs:
            # Load data
            if load_class is not None:
                # Load as Pydantic model
                loaded = load_class.load(
                    str(self._file_path),
                    format_override=self.file_format,
                    acquire_lock=False  # Read-only access
                )
            else:
                # Load as LazyLoadedFileData
                loaded = LazyLoadedFileData(str(self._file_path))
            
            self._contents_cache[cache_key] = loaded
            self._contents_load_times[cache_key] = current_time
        
        return self._contents_cache[cache_key]
    
    
    # =========================================================================
    # Comparison & Hashing
    # =========================================================================
    
    def __lt__(self, other: 'PrimitiveEventProxy') -> bool:
        """
        Compare by fully decomposed path components for proper sorting.
        
        Uses file_path.parts which breaks the path into a tuple of all components.
        This allows events from different directories to group and sort predictably
        when mixed together.
        
        Example: Path('/tmp/log1/e001_X.yaml').parts -> ('/', 'tmp', 'log1', 'e001_X.yaml')
        """
        if not isinstance(other, PrimitiveEventProxy):
            return NotImplemented
        return self.file_path.parts < other.file_path.parts
    
    def __eq__(self, other: object) -> bool:
        """Compare by file_path for equality."""
        if not isinstance(other, PrimitiveEventProxy):
            return NotImplemented
        return self.file_path == other.file_path
    
    def __hash__(self) -> int:
        """Hash by file_path for use in sets/dicts."""
        return hash(self.file_path)
    
    # =========================================================================
    # Representation
    # =========================================================================
    
    def __repr__(self) -> str:
        """Returns filename without extension, e.g., 'e003_OCR-STATUS@PROCESSING'."""
        return self._file_path.stem

