# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Protocol for schema-based path resolution in CachedFileFolders.

This protocol defines the interface that SimpleCacheORM expects from schema resolvers.
Any class implementing this protocol can be used with SimpleCacheORM.

Implementations:
- PrimitiveSchemaResolver: Zero-config resolver for Pydantic BaseModel classes
- Custom resolvers: Users can implement their own resolvers following this protocol
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional, Protocol, Sequence, Tuple, Union

from totodev_pub.cached_file_folders import GroupingKey, RefPath
from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef

SchemaKey = Union[str, type]


class SchemaResolver(Protocol):
    """
    Protocol for schema-based path resolution in CachedFileFolders.
    
    This protocol defines the interface that resolvers must implement to work
    with SimpleCacheORM. The protocol uses structural typing (duck typing),
    so any class with these methods will work, even without explicit inheritance.
    
    Implementations:
    - PrimitiveSchemaResolver: Zero-config resolver for Pydantic BaseModel classes
    - Custom resolvers: Users can implement their own
    
    Example:
        # Using PrimitiveSchemaResolver (zero-config)
        from totodev_pub.cached_file_folders_support import PrimitiveSchemaResolver, SimpleCacheORM
        resolver = PrimitiveSchemaResolver(cache)
        orm = SimpleCacheORM(cache, resolver=resolver)
        await orm.upsert(user_obj, schema_key=User, slug="abc123")
    """
    
    def register_map(self, schema: Dict[SchemaKey, Dict[str, Any]]) -> None:
        """
        Register schema patterns from a mapping dictionary.
        
        The schema dictionary maps schema keys (str or type) to pattern definitions.
        Each pattern definition should include:
        - grouping_key_template: Template for grouping key (str or tuple)
        - ref_path_template: Template for file path within grouping (str)
        - defaults: Optional dict of default parameter values
        
        Args:
            schema: Dictionary mapping schema keys to pattern definitions
            
        Note:
            Some resolvers may support a "patterns" key at the root level for
            organizing patterns with metadata.
        """
        ...
    
    def resolve_path(
        self,
        name_or_type: SchemaKey,
        **params: Any,
    ) -> Tuple[GroupingKey, RefPath]:
        """
        Resolve schema pattern + parameters to concrete (grouping_key, ref_path).
        
        Args:
            name_or_type: Schema key (string name or type) to resolve
            **params: Parameters to fill template placeholders
            
        Returns:
            Tuple of (grouping_key, ref_path) for file location
            
        Raises:
            ValueError: If schema pattern not found
            KeyError: If required parameters are missing
        """
        ...
    
    def resolve_ref(
        self,
        name_or_type: SchemaKey,
        **params: Any,
    ) -> Optional[CachedFileRef]:
        """
        Resolve and return the first matching CachedFileRef, if it exists.
        
        This is a convenience method that combines resolve_path() with cache.find_file().
        
        Args:
            name_or_type: Schema key (string name or type) to resolve
            **params: Parameters to fill template placeholders
            
        Returns:
            CachedFileRef if file exists, None otherwise
            
        Raises:
            ValueError: If schema pattern not found
        """
        ...
    
    def iter_refs(
        self,
        name_or_type: SchemaKey,
        *,
        reverse: bool = False,
        **filters: Any,
    ) -> Iterator[CachedFileRef]:
        """
        Iterate references matching the rendered grouping and globbable ref path.
        
        Filters support wildcards (e.g., user_slug="*") for pattern matching.
        
        Args:
            name_or_type: Schema key (string name or type) to resolve
            reverse: If True, yield refs in reverse order
            **filters: Parameters to fill template placeholders (supports wildcards)
            
        Yields:
            CachedFileRef objects matching the pattern
            
        Raises:
            ValueError: If schema pattern not found
        """
        ...
    
    def required_params(self, name_or_type: SchemaKey) -> Sequence[str]:
        """
        Return the sorted parameter names required by the templates for an entry.
        
        This is used by SimpleCacheORM to determine which parameters need to be
        inferred from source objects or provided explicitly.
        
        Args:
            name_or_type: Schema key (string name or type) to query
            
        Returns:
            Sorted sequence of parameter names (e.g., ["tenant_slug", "user_slug"])
            
        Raises:
            ValueError: If schema pattern not found
        """
        ...
