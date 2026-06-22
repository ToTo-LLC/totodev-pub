# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CachedFileRef is the per-file handle returned by CachedFileFolders operations.

It carries the two filesystem locations that define every cache entry — the body file
(file_path) and its companion slave directory (slave_dir_path) — together with the
logical identity (ref_path, grouping_key) used to look it up. Callers receive it
inside ChangeNotice (on insert/update/delete) and CachedFileFolders.find_file().

The metadata(), event_log(), is_truncated(), and truncation_info() accessors are thin
conveniences over the slave directory; none of them require network I/O.
"""

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel, PrivateAttr

from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.primitive_event_log import PrimitiveEventLog

# Type alias for grouping keys
GroupingKey = Sequence[str]

__all__ = ["CachedFileRef", "GroupingKey"]

# Lazy import to avoid circular imports at module level
def _truncation_support():
    from totodev_pub.cached_file_folders_support import truncation_support
    return truncation_support


class CachedFileRef(BaseModel):
    """A handle to a single entry in a CachedFileFolders cache."""
    ref_path: str # reference path - the original location/identifier of the file before caching
    grouping_key: Optional[GroupingKey] = None
    file_path: Path # actual file on the local filesystem
    slave_dir_path: Path # directory associated with this file for logs, parsing, etc.
    _metadata_filename: str = PrivateAttr(default="metadata.yaml")
    _is_truncated_memo: Optional[bool] = PrivateAttr(default=None)
    
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
        """Get a PrimitiveEventLog rooted in the slave directory.

        Pass subdir="" to place event files directly in slave_dir_path rather than
        the default "events" sub-folder.
        """
        if subdir:
            event_dir = self.slave_dir_path / subdir
        else:
            event_dir = self.slave_dir_path
        
        return PrimitiveEventLog(event_dir=event_dir, force=False)

    def is_truncated(self) -> bool:
        """Return True if this entry is truncated (zero-byte file with a valid sidecar).

        The first call performs a disk check and memoises the result. Producing
        code (in CachedFileFolders) pre-seeds `_is_truncated_memo` on INSERT/UPDATE
        notices so that receivers never touch potentially-transient files.
        """
        if self._is_truncated_memo is not None:
            return self._is_truncated_memo
        ts = _truncation_support()
        result = ts.is_truncated(self.file_path, self.slave_dir_path)
        self._is_truncated_memo = result
        return result

    def truncation_info(self):
        """Return the TruncationInfo sidecar for a truncated entry, or None for full entries."""
        ts = _truncation_support()
        return ts.read_truncation_info(self.slave_dir_path)

