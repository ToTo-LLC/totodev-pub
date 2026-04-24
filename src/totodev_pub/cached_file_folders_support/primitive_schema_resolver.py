# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Primitive Schema Resolver - Zero-configuration object persistence for early-phase development
==============================================================================================

Part of the file-based object storage ecosystem built on CachedFileFolders, PrimitiveSchemaResolver
provides a simple, zero-configuration way to persist Pydantic objects to flat text files. It's
designed for early-stage software development when you need object persistence but want to defer
decisions about data access patterns, storage locations, and schema organization until you better
understand your application's needs.


WHEN TO USE
-----------

Use PrimitiveSchemaResolver when:
- Building early prototypes or proof-of-concepts that need object persistence
- You want to start coding business logic without designing a database schema first
- You need simple defaults for data storage locations and don't want to think about it yet
- You're exploring data models and want to quickly save/load objects for testing
- You're working on small-scale applications where file-based storage is sufficient

Move to more sophisticated solutions when:
- Your application reaches production maturity and needs optimized data access patterns
- You need complex queries, relationships, or transactions
- Performance requirements exceed what file-based storage can provide
- You need concurrent access patterns that require a proper database
- You want to use a more sophisticated SchemaResolver implementation or migrate to RDBMS


VALUE PROPOSITION
-----------------

PrimitiveSchemaResolver eliminates the "where should I store this?" decision that often slows down
early development. Instead of spending time designing directory structures, naming conventions, or
database schemas, you can immediately start persisting objects and focus on building your application.

The resolver provides sensible defaults:
- Automatic organization by class name (no manual directory design needed)
- Time-based slug generation (no need to invent unique identifiers)
- Zero schema registration (works with any Pydantic model immediately)
- Simple, intuitive API (save, load, delete, iterate)

This allows developers to defer architectural decisions until they have a better understanding of
their data access patterns, at which point they can migrate to more sophisticated resolvers or
traditional databases.


QUICK EXAMPLE
-------------

```python
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.primitive_schema_resolver import PrimitiveSchemaResolver
from pydantic import BaseModel

class User(BaseModel):
    name: str
    email: str

# Create cache and resolver - zero configuration needed
cache = CachedFileFolders("prototype/", "/tmp/cache")
resolver = PrimitiveSchemaResolver(cache, grouping_key=None)

# Save objects immediately - no registration, no schema design
user1 = User(name="Alice", email="alice@example.com")
slug1 = await resolver.save(user1)  # Auto-generates slug like "1a2b3c4d"

# Load by class and slug
user = resolver.load(User, slug1)

# Iterate all objects of a type
for user in resolver.iter_objects(User):
    print(user.name)

# Works seamlessly with SimpleCacheORM
from totodev_pub.cached_file_folders_support import SimpleCacheORM
orm = SimpleCacheORM(cache, resolver=resolver)
await orm.upsert(user_obj, schema_key=User, slug=slug1)
```


CORE CONCEPTS
-------------

Zero Configuration:
    Works with any Pydantic BaseModel class without registration or schema definition.
    Just create your models and start saving - the resolver handles the rest.

Automatic Organization:
    Objects are stored in subdirectories by class name:
    - `User/User-1a2b3c4d.yaml`
    - `User/User-5e6f7g8h.yaml`
    - `Order/Order-9i0j1k2l.yaml`
    This keeps objects of the same type together while separating different types, without
    requiring you to design a directory structure.

Slug Generation:
    Each object gets a unique identifier (slug) automatically:
    1. If object implements `SlugProvider.generate_slug()`, uses that custom slug
    2. Otherwise, generates time-based slug (seconds since Jan 1, 2025, encoded in base36)
    Example: "1a2b3c4d" represents a specific timestamp, ensuring uniqueness.

SchemaResolver Protocol:
    Implements the SchemaResolver protocol, making it compatible with SimpleCacheORM and
    other components in the ecosystem. Can be swapped out for more sophisticated resolvers
    as your application matures.


HOW IT WORKS
------------

Storage Layout:
    Objects are stored as YAML or JSON files in a CachedFileFolders cache. The resolver
    builds paths automatically using the pattern: `{ClassName}/{ClassName}-{slug}.{ext}`.
    All objects saved through a resolver instance go to the same grouping_key, providing
    logical separation when needed.

File Format:
    Defaults to YAML for human-readability during development, but supports JSON as well.
    Files are stored as serialized Pydantic models, preserving all model data and validation.

Slug Collision Handling:
    Custom slugs provided via SlugProvider are validated for uniqueness - conflicts raise
    ValueError. Time-based slugs are extremely unlikely to collide, but the resolver includes
    collision detection and fallback mechanisms.


ECOSYSTEM INTEGRATION
---------------------

PrimitiveSchemaResolver is part of a larger ecosystem for file-based object storage:

- CachedFileFolders: Provides the underlying file caching infrastructure with change detection,
  cleanup, and synchronization capabilities.

- SimpleCacheORM: Higher-level ORM-like interface that uses SchemaResolver implementations.
  PrimitiveSchemaResolver can be used directly with SimpleCacheORM for a complete object
  persistence solution.

- SchemaResolver Protocol: Defines the interface for resolving object storage locations.
  PrimitiveSchemaResolver is the simplest implementation; more sophisticated resolvers can
  provide custom path patterns, relationships, and query capabilities.

- Migration Path: As applications mature, developers can replace PrimitiveSchemaResolver with
  custom SchemaResolver implementations that provide optimized storage patterns, or migrate
  entirely to RDBMS or other database systems while preserving the same high-level API patterns.


USAGE PATTERNS
--------------

Pattern 1: Rapid Prototyping
    Create models, create resolver, start saving objects immediately. Focus on business logic
    while the resolver handles persistence details.

Pattern 2: Exploration Phase
    Use the resolver to quickly test different data models. Save objects, inspect the file
    structure, iterate on your models without committing to a schema design.

Pattern 3: Simple Applications
    For small-scale applications where file-based storage is sufficient, use PrimitiveSchemaResolver
    as a permanent solution. The automatic organization and simple API may be all you need.

Pattern 4: Stepping Stone
    Start with PrimitiveSchemaResolver to get your application working, then migrate to more
    sophisticated storage solutions once you understand your data access patterns and requirements.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple, Type, TypeVar

import yaml
from pydantic import BaseModel

from totodev_pub.cached_file_folders import CachedFileFolders, GroupingKey, RefPath
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping
from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy
from totodev_pub.cached_file_folders_support.primitive_schema_protocol import SlugProvider
from totodev_pub.cached_file_folders_support.schema_resolver_protocol import SchemaKey, SchemaResolver

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Epoch: January 1, 2025 00:00:00 UTC
EPOCH_2025 = 1735689600


def _int_to_base36(n: int) -> str:
    """Convert integer to base36 (0-9, a-z) string."""
    if n == 0:
        return "0"
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = []
    while n > 0:
        result.append(chars[n % 36])
        n //= 36
    return "".join(reversed(result))


def _generate_time_based_slug() -> str:
    """
    Generate a time-based slug.
    
    Returns seconds since Jan 1, 2025 encoded in base36.
    """
    seconds_since_epoch = int(time.time()) - EPOCH_2025
    if seconds_since_epoch < 0:
        # Handle dates before epoch by using absolute value with prefix
        return "n" + _int_to_base36(abs(seconds_since_epoch))
    return _int_to_base36(seconds_since_epoch)


class PrimitiveSchemaResolver:
    """
    Zero-configuration resolver for persisting Pydantic objects.
    
    This resolver automatically handles slug generation and file organization,
    making it ideal for early-phase development when you want to quickly
    prototype data persistence without schema design decisions.
    
    Objects are stored in subdirectories by class name:
    - `{ClassName}/{ClassName}-{slug}.yaml`
    
    The resolver is bound to a specific grouping_key at construction time.
    All objects saved through this resolver go to that grouping.
    
    Example:
        resolver = PrimitiveSchemaResolver(cache, grouping_key=None)
        slug = await resolver.save(user_object)
        user = resolver.load(User, slug)
    """
    
    def __init__(
        self,
        cache: CachedFileFolders,
        grouping_key: Optional[GroupingKey] = None,
        file_format: str = "yaml",
    ) -> None:
        """
        Initialize the primitive schema resolver.
        
        Args:
            cache: The CachedFileFolders instance to use
            grouping_key: The grouping key to bind to (None for flat patterns)
            file_format: File format for serialization ("yaml" or "json")
        """
        self.cache = cache
        self.grouping_key = cache._storage.normalize_grouping_key(grouping_key)
        # Pass original grouping_key to cache.grouping() - it will normalize internally
        self.grouping = cache.grouping(grouping_key)
        self.file_format = file_format.lower()
        
        if self.file_format not in ("yaml", "json"):
            raise ValueError(f"file_format must be 'yaml' or 'json', got {file_format!r}")
        
        # Track which classes have been used (for register_map compatibility)
        self._registered_classes: set[type] = set()
    
    def _get_class_name(self, cls: Type[BaseModel]) -> str:
        """Get the class name for a Pydantic model."""
        return cls.__name__
    
    def _build_ref_path(self, class_name: str, slug: str) -> str:
        """Build the ref_path for an object."""
        extension = "yaml" if self.file_format == "yaml" else "json"
        return f"{class_name}/{class_name}-{slug}.{extension}"
    
    def _parse_slug_from_ref_path(self, ref_path: str, class_name: str) -> Optional[str]:
        """
        Extract slug from a ref_path.
        
        Expected format: {ClassName}/{ClassName}-{slug}.{ext}
        """
        pattern = rf"^{re.escape(class_name)}/{re.escape(class_name)}-(.+)\.(yaml|yml|json)$"
        match = re.match(pattern, ref_path)
        if match:
            return match.group(1)
        return None
    
    def generate_slug(
        self,
        obj: BaseModel,
        class_name: Optional[str] = None,
    ) -> str:
        """
        Generate a slug for an object with collision handling.
        
        Args:
            obj: The Pydantic object
            class_name: Optional class name (defaults to obj.__class__.__name__)
            
        Returns:
            A unique slug for this object
            
        Raises:
            ValueError: If object provides a custom slug that conflicts with existing file
        """
        if class_name is None:
            class_name = self._get_class_name(type(obj))
        
        # Check if object implements SlugProvider protocol
        if isinstance(obj, SlugProvider):
            try:
                custom_slug = obj.generate_slug()
                if custom_slug:
                    # Validate custom slug doesn't conflict
                    ref_path = self._build_ref_path(class_name, custom_slug)
                    if self.grouping.file_exists(ref_path):
                        raise ValueError(
                            f"Custom slug {custom_slug!r} conflicts with existing file: {ref_path}"
                        )
                    return custom_slug
            except (AttributeError, RuntimeError):
                # Object's generate_slug() raised an exception, fall through to auto-generation
                pass
            except ValueError:
                # Re-raise ValueError (collision detection) - don't catch it
                raise
        
        # Auto-generate time-based slug
        # Use a small delay to ensure uniqueness if called multiple times rapidly
        slug = _generate_time_based_slug()
        
        # Check for collision (very unlikely with time-based, but handle it)
        ref_path = self._build_ref_path(class_name, slug)
        if self.grouping.file_exists(ref_path):
            # If collision occurs (shouldn't with time-based), add microsecond component
            import time as time_module
            time_module.sleep(0.001)  # 1ms delay
            # Add random component to ensure uniqueness
            import random
            slug = _generate_time_based_slug() + _int_to_base36(random.randint(0, 1295))  # 0-zz in base36
        
        return slug
    
    async def save(self, obj: BaseModel) -> str:
        """
        Save a Pydantic object and return its assigned slug.
        
        Args:
            obj: The Pydantic object to save
            
        Returns:
            The slug assigned to this object
        """
        class_name = self._get_class_name(type(obj))
        self._registered_classes.add(type(obj))
        
        # Generate slug (this checks for collisions)
        slug = self.generate_slug(obj, class_name)
        
        # Build ref_path
        ref_path = self._build_ref_path(class_name, slug)
        
        # Double-check collision right before saving (in case file was created between generate_slug and here)
        if self.grouping.file_exists(ref_path):
            # This shouldn't happen if generate_slug worked correctly, but handle it
            raise ValueError(
                f"Slug {slug!r} conflicts with existing file: {ref_path}"
            )
        
        # Create file proxy and save
        proxy = SerializableDataProxy(obj, ref_path)
        await self.grouping.upsert_file(proxy)
        
        return slug
    
    def load(self, cls: Type[T], slug: str) -> Optional[T]:
        """
        Load a Pydantic object by class and slug.
        
        Args:
            cls: The Pydantic model class
            slug: The slug of the object to load
            
        Returns:
            The loaded object, or None if not found
        """
        class_name = self._get_class_name(cls)
        ref_path = self._build_ref_path(class_name, slug)
        
        file_ref = self.grouping.find_file(ref_path)
        if file_ref is None:
            return None
        
        # Load file content
        try:
            with open(file_ref.file_path, "r", encoding="utf-8") as f:
                if self.file_format == "yaml":
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)
            
            # Validate and create model instance
            return cls.model_validate(data)
        except Exception as e:
            logger.warning(
                f"Failed to load {cls.__name__} with slug {slug!r} from {ref_path}: {e}"
            )
            return None
    
    async def delete(self, cls: Type[BaseModel], slug: str) -> bool:
        """
        Delete a Pydantic object by class and slug.
        
        Args:
            cls: The Pydantic model class
            slug: The slug of the object to delete
            
        Returns:
            True if the object was deleted, False if it didn't exist
        """
        class_name = self._get_class_name(cls)
        ref_path = self._build_ref_path(class_name, slug)
        
        if not self.grouping.file_exists(ref_path):
            return False
        
        await self.grouping.delete_file(ref_path)
        return True
    
    def iter_objects(self, cls: Type[T]) -> Iterator[T]:
        """
        Iterate over all objects of a given class.
        
        Args:
            cls: The Pydantic model class to iterate
            
        Yields:
            Instances of the specified class
            
        Note:
            Invalid or unparseable files are skipped with a warning.
        """
        class_name = self._get_class_name(cls)
        if self.file_format == "yaml":
            # Match both .yaml and .yml extensions
            glob_pattern = f"{class_name}/{class_name}-*.yaml"
        else:
            glob_pattern = f"{class_name}/{class_name}-*.{self.file_format}"
        
        for file_ref in self.grouping.files(ref_path_glob=glob_pattern):
            # Extract slug from ref_path
            slug = self._parse_slug_from_ref_path(file_ref.ref_path, class_name)
            if slug is None:
                logger.warning(
                    f"Could not parse slug from ref_path: {file_ref.ref_path}"
                )
                continue
            
            # Load the object
            obj = self.load(cls, slug)
            if obj is not None:
                yield obj
    
    # SchemaResolver protocol implementation
    def register_map(self, schema: Dict[SchemaKey, Dict[str, Any]]) -> None:
        """
        Register schema patterns (no-op for PrimitiveSchemaResolver).
        
        PrimitiveSchemaResolver doesn't require explicit registration - it works
        with any Pydantic BaseModel class automatically. This method tracks which
        classes have been "registered" for compatibility with SimpleCacheORM.
        
        Args:
            schema: Dictionary mapping schema keys to pattern definitions
            
        Note:
            Only class-type keys are tracked. String keys are ignored since
            PrimitiveSchemaResolver works with classes, not string names.
        """
        for key in schema.keys():
            if isinstance(key, type) and issubclass(key, BaseModel):
                self._registered_classes.add(key)
            elif isinstance(key, str):
                # String keys are not supported by PrimitiveSchemaResolver
                logger.warning(
                    f"PrimitiveSchemaResolver: String schema key '{key}' is not supported. "
                    "PrimitiveSchemaResolver only works with Pydantic BaseModel classes."
                )
    
    def resolve_path(
        self,
        name_or_type: SchemaKey,
        **params: Any,
    ) -> Tuple[GroupingKey, RefPath]:
        """
        Resolve class + slug to (grouping_key, ref_path).
        
        Args:
            name_or_type: Must be a Pydantic BaseModel class
            **params: Must include "slug" parameter
            
        Returns:
            Tuple of (grouping_key, ref_path)
            
        Raises:
            ValueError: If name_or_type is not a BaseModel class
            KeyError: If "slug" parameter is missing
        """
        if not isinstance(name_or_type, type) or not issubclass(name_or_type, BaseModel):
            raise ValueError(
                f"PrimitiveSchemaResolver only works with Pydantic BaseModel classes, "
                f"got {name_or_type!r}"
            )
        
        if "slug" not in params:
            raise KeyError("slug")
        
        slug = params["slug"]
        class_name = name_or_type.__name__
        ref_path = self._build_ref_path(class_name, slug)
        
        return self.grouping_key, ref_path
    
    def resolve_ref(
        self,
        name_or_type: SchemaKey,
        **params: Any,
    ) -> Optional[CachedFileRef]:
        """
        Resolve and return CachedFileRef if file exists.
        
        Args:
            name_or_type: Must be a Pydantic BaseModel class
            **params: Must include "slug" parameter
            
        Returns:
            CachedFileRef if file exists, None otherwise
        """
        grouping_key, ref_path = self.resolve_path(name_or_type, **params)
        return self.grouping.find_file(ref_path)
    
    def iter_refs(
        self,
        name_or_type: SchemaKey,
        *,
        reverse: bool = False,
        **filters: Any,
    ) -> Iterator[CachedFileRef]:
        """
        Iterate references matching the class pattern.
        
        Supports wildcard matching via slug="*" or slug=None.
        
        Args:
            name_or_type: Must be a Pydantic BaseModel class
            reverse: If True, yield refs in reverse order
            **filters: Can include "slug" for filtering (supports "*" wildcard)
            
        Yields:
            CachedFileRef objects matching the pattern
        """
        if not isinstance(name_or_type, type) or not issubclass(name_or_type, BaseModel):
            raise ValueError(
                f"PrimitiveSchemaResolver only works with Pydantic BaseModel classes, "
                f"got {name_or_type!r}"
            )
        
        class_name = name_or_type.__name__
        slug = filters.get("slug")
        
        if slug is None or slug == "*":
            # Iterate all objects of this class
            if self.file_format == "yaml":
                glob_pattern = f"{class_name}/{class_name}-*.yaml"
            else:
                glob_pattern = f"{class_name}/{class_name}-*.{self.file_format}"
        else:
            # Specific slug - build exact path
            ref_path = self._build_ref_path(class_name, slug)
            ref = self.grouping.find_file(ref_path)
            if ref is not None:
                yield ref
            return
        
        # Iterate matching files
        files = list(self.grouping.files(ref_path_glob=glob_pattern))
        if reverse:
            files.reverse()
        
        for ref in files:
            yield ref
    
    def required_params(self, name_or_type: SchemaKey) -> Sequence[str]:
        """
        Return required parameters (always ["slug"] for PrimitiveSchemaResolver).
        
        Args:
            name_or_type: Schema key (ignored, but validated)
            
        Returns:
            Sequence containing only "slug"
        """
        if not isinstance(name_or_type, type) or not issubclass(name_or_type, BaseModel):
            raise ValueError(
                f"PrimitiveSchemaResolver only works with Pydantic BaseModel classes, "
                f"got {name_or_type!r}"
            )
        
        return ["slug"]
