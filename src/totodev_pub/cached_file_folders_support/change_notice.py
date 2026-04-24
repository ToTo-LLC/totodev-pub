# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
ChangeNotice - Cache Change Notification Data Model

This module provides the ChangeNotice class which describes changes (INSERT, UPDATE, DELETE)
made to cached files. It uses CachedFileRef objects to represent current and old file states.

This class is the primary return type from cache upsert and delete operations, providing
detailed information about what changed and access to both old and new file artifacts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel, PrivateAttr, model_validator

from .sync_types import ChangeType

if TYPE_CHECKING:
    from typing import Sequence
    from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
    from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
    
    GroupingKey = Sequence[str]


class ChangeNotice(BaseModel):
    """Describes a cache change: INSERT, UPDATE, or DELETE.

    Uses CachedFileRef objects to represent current and old file states.
    Access file paths via `cur.file_path` and `old.file_path`.
    Convenience properties `ref_path` and `grouping_key` are available at the root level.

    If the change_type parameter is absent, deduces the ChangeType.

    Path Field Requirements by Change Type:
    ======================================
    | Change Type | cur (CachedFileRef) | old (CachedFileRef) |
    |-------------|---------------------|---------------------|
    | INSERT      | Present             | None                |
    | UPDATE      | Present             | Present             |
    | DELETE      | None                | Present             |

    NOTE: The old CachedFileRef is only guaranteed to exist while any change_receiver
    callback provided to the cache is executing. Once the callback returns, the files
    referenced by `old` may already be deleted even though the structure remains.
    
    Metadata Access:
    ================
    Use `metadata()` on cached file references to access standardized metadata files within
    slave directories. When inspecting the old file, do so inside the change_receiver before
    the cache removes the staged copy.
    """
    change_type: Optional[ChangeType] = None  # INSERT, DELETE, UPDATE (auto-deduced if not provided)
    file_name: Optional[str] = None  # basename of the file on local filesystem within the cache
    
    # Core file references using CachedFileRef objects
    cur: Optional['CachedFileRef'] = None  # Current file reference (None for DELETE)
    old: Optional['CachedFileRef'] = None  # Old file reference (None for INSERT)
    
    _metadata_filename: str = PrivateAttr(default="metadata.yaml")
    
    # DEPRECATED (November 5, 2025): The following flat attributes were replaced with cur/old CachedFileRef objects.
    # To migrate your code:
    #   - change.ref_path -> change.ref_path (convenience property still works)
    #   - change.grouping_key -> change.grouping_key (convenience property still works)
    #   - change.file_path -> change.cur.file_path
    #   - change.slave_dir_path -> change.cur.slave_dir_path
    #   - change.old_file_path -> change.old.file_path
    #   - change.old_slave_dir_path -> change.old.slave_dir_path
    #
    # ref_path: str
    # grouping_key: Optional[GroupingKey] = None
    # file_path: Optional[Path] = None
    # slave_dir_path: Optional[Path] = None
    # old_file_path: Optional[Path] = None
    # old_slave_dir_path: Optional[Path] = None
    
    @property
    def ref_path(self) -> str:
        """Get ref_path, preferring cur over old."""
        if self.cur is not None:
            return self.cur.ref_path
        if self.old is not None:
            return self.old.ref_path
        raise ValueError("Both cur and old are None - invalid ChangeNotice state")
    
    @property
    def grouping_key(self) -> Optional[GroupingKey]:
        """Get grouping_key, preferring cur over old."""
        if self.cur is not None:
            return self.cur.grouping_key
        if self.old is not None:
            return self.old.grouping_key
        return None
    
    @model_validator(mode='after')
    def _deduce_change_type_if_needed(self) -> 'ChangeNotice':
        """Automatically deduce change_type if not provided, based on cur/old presence."""
        if self.change_type is not None:
            return self  # Already provided, nothing to deduce
        
        # Deduce change_type based on cur/old presence
        has_cur = self.cur is not None
        has_old = self.old is not None
        
        if has_cur and not has_old:
            self.change_type = ChangeType.INSERT
        elif has_cur and has_old:
            self.change_type = ChangeType.UPDATE
        elif not has_cur and has_old:
            self.change_type = ChangeType.DELETE
        else:
            raise ValueError("Both cur and old are None - cannot deduce change_type")
        
        return self
    
    @model_validator(mode='after')
    def _validate_refs_match_change_type(self) -> 'ChangeNotice':
        """Validate that cur/old presence matches the change_type."""
        if self.change_type == ChangeType.INSERT:
            if self.cur is None:
                raise ValueError("INSERT requires cur")
            if self.old is not None:
                raise ValueError("INSERT should not have old")
        elif self.change_type == ChangeType.DELETE:
            if self.old is None:
                raise ValueError("DELETE requires old")
            if self.cur is not None:
                raise ValueError("DELETE should not have cur")
        elif self.change_type == ChangeType.UPDATE:
            if self.cur is None or self.old is None:
                raise ValueError("UPDATE requires both cur and old")
        
        # Validate ref_path and grouping_key match when both exist
        if self.cur and self.old:
            if self.cur.ref_path != self.old.ref_path:
                raise ValueError("cur and old must have same ref_path")
            if self.cur.grouping_key != self.old.grouping_key:
                raise ValueError("cur and old must have same grouping_key")
        
        return self

    def metadata(self, 
                 change_detection_secs: int = 300,
                 acts_as_dict_proxy: bool = True,
                 default_data: Optional[Dict[str, Any]] = None,
                 **kwargs) -> Optional[LazyLoadedFileData]:
        """Get lazy loader for current file's metadata file in slave directory. Returns None for DELETE operations."""
        if self.cur is None:
            return None
        return self.cur.metadata(
            change_detection_secs=change_detection_secs,
            acts_as_dict_proxy=acts_as_dict_proxy,
            default_data=default_data,
            **kwargs
        )

# Rebuild Pydantic model to resolve forward references
# Import the actual classes after the model is defined so Pydantic can resolve them
from .cached_file_ref import CachedFileRef  # noqa: F811
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData  # noqa: F811

ChangeNotice.model_rebuild()

