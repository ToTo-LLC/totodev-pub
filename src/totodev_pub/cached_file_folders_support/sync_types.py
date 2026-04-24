# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, NamedTuple, List
from pydantic import BaseModel


class ChangeType(str, Enum):
    INSERT = "INSERT"
    DELETE = "DELETE"
    UPDATE = "UPDATE"


class UpsertFailure(BaseModel):
    """Represents a failed upsert operation."""
    model_config = {"arbitrary_types_allowed": True}
    
    grouping_key: Optional[Sequence[str]]
    file_proxy: 'FileProxyBase'  # Forward reference to avoid circular imports
    exception: Exception
    
    @property
    def ref_path(self) -> str:
        """Convenience property to get the ref_path from the file proxy."""
        return self.file_proxy.ref_path()


class ResyncBulkResult(NamedTuple):
    """Result of a bulk resync operation."""
    changes: List['ChangeNotice']
    failures: List['UpsertFailure']


# Rebuild Pydantic models to resolve forward references
# Import FileProxyBase to resolve the forward reference
from .file_proxy_base import FileProxyBase
UpsertFailure.model_rebuild()

