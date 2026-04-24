# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CachedFileRef - Data structure for cached file references

A lightweight data class representing a file in the cache, with its reference path,
grouping key, filesystem path, and associated slave directory.
"""

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel, PrivateAttr

from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.primitive_event_log import PrimitiveEventLog

# Type alias for grouping keys
GroupingKey = Sequence[str]

__all__ = ["CachedFileRef", "GroupingKey"]


class CachedFileRef(BaseModel):
    """
    A class that represents a record of a file in the cached files folders.
    
    Use the metadata() method to access an optional standardized metadata file in the slave
    directory for tracking processing state and other per-file information. This is completely
    optional - zero cost if not used.
    """
    ref_path: str # reference path - the original location/identifier of the file before caching
    grouping_key: Optional[GroupingKey] = None
    file_path: Path # actual file on the local filesystem
    slave_dir_path: Path # directory associated with this file for logs, parsing, etc.
    _metadata_filename: str = PrivateAttr(default="metadata.yaml")
    
    def metadata(self, 
                 change_detection_secs: int = 300,
                 acts_as_dict_proxy: bool = True,
                 default_data: Optional[Dict[str, Any]] = None,
                 **kwargs) -> LazyLoadedFileData:
        """Get lazy loader for metadata file in slave directory (default: metadata.yaml)."""
        metadata_path = self.slave_dir_path / self._metadata_filename
        if default_data is None:
            default_data = {}
        return LazyLoadedFileData(
            str(metadata_path),
            change_detection_secs=change_detection_secs,
            acts_as_dict_proxy=acts_as_dict_proxy,
            default_data=default_data,
            **kwargs
        )
    
    def event_log(self, subdir: str = "events") -> PrimitiveEventLog:
        """
        Get a PrimitiveEventLog for tracking processing stages of this cached file.
        
        Args:
            subdir: Subdirectory within slave_dir for event log files.
                   If empty string, uses slave_dir_path directly.
                   Default: "events"
        
        Returns:
            PrimitiveEventLog instance (created with force=False)
            
        Example:
            # Track document processing stages
            log = cached_file.event_log()
            log.create_event("PROCESSING", "OCR-STARTED")
            log.create_event("PROCESSING", "OCR-COMPLETED", {"pages": 10})
            
            # Check current status
            if log.has_event("PROCESSING") == "OCR-COMPLETED":
                proceed_to_validation()
        """
        if subdir:
            event_dir = self.slave_dir_path / subdir
        else:
            event_dir = self.slave_dir_path
        
        return PrimitiveEventLog(event_dir=event_dir, force=False)

