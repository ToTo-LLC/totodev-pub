# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Primitive Schema Protocol - Custom slug generation for object identification
============================================================================

Defines the SlugProvider protocol, allowing Pydantic models to participate in slug generation
for PrimitiveSchemaResolver. This gives objects control over how they are identified in the
file-based storage system, enabling semantic naming and custom identifier strategies.


WHEN TO USE
-----------

Implement SlugProvider when:
- Your objects have natural unique identifiers (email addresses, usernames, codes)
- You want human-readable, meaningful filenames instead of time-based slugs
- You need predictable, stable identifiers that don't change between saves
- Your objects have business logic that determines their identity
- The identifier is durable (won't change over the object's lifetime)

Use auto-generated slugs (default) when:
- You don't have natural identifiers
- Your identifiers are ephemeral or might change (e.g., status fields, timestamps)
- Time-based uniqueness is sufficient
- You want the simplest possible implementation


VALUE PROPOSITION
-----------------

SlugProvider allows objects to define their own identity in the storage system. Instead of
relying on auto-generated time-based slugs, objects can provide meaningful identifiers that
reflect their domain semantics. This makes stored files more readable and enables patterns
like "load user by email" or "find order by order number" without additional lookup logic.


QUICK EXAMPLE
-------------

```python
from totodev_pub.cached_file_folders_support.primitive_schema_protocol import SlugProvider
from pydantic import BaseModel

class User(BaseModel, SlugProvider):
    email: str  # Durable identifier - won't change
    name: str
    status: str  # Ephemeral - changes over time, NOT used in slug
    
    def generate_slug(self) -> str:
        # Use email as the slug (durable, stable identifier)
        # Sanitize for filesystem compatibility
        return self.email.replace("@", "_at_").replace(".", "_")

# When saved, User objects will use their email as the slug
user = User(email="alice@example.com", name="Alice", status="active")
slug = await resolver.save(user)  # slug will be "alice_at_example_com"

# Even if status changes later, the slug remains the same
user.status = "inactive"
await resolver.save(user)  # Updates same file, slug unchanged
```


CORE CONCEPTS
-------------

Protocol-Based:
    SlugProvider is a runtime-checkable Protocol, meaning any object with a `generate_slug()`
    method automatically implements it. No explicit inheritance required, though you can
    inherit for clarity.

Optional Implementation:
    Objects don't need to implement SlugProvider. If they don't, PrimitiveSchemaResolver will
    auto-generate time-based slugs. This makes the protocol truly optional - implement it only
    when you need custom slugs.

Uniqueness Responsibility:
    Objects implementing SlugProvider are responsible for ensuring their generated slugs are
    unique. The resolver validates uniqueness and raises ValueError if a collision occurs.

Durable vs. Ephemeral Information:
    Slugs should be composed of durable, stable information that doesn't change over the
    object's lifetime. Good choices: unique identifiers (IDs, emails, codes), immutable
    attributes, or composite keys. Poor choices: status fields, timestamps, or any mutable
    state that can change.
    
    If an object's slug changes (e.g., because it's based on a status field that was updated),
    the object will NOT be automatically moved to a new file location. The old file remains
    at the old slug location, and saving the object with a new slug creates a new file. Users
    must manually handle moving objects between locations if they choose to use ephemeral
    information in slugs.

Integration:
    Works seamlessly with PrimitiveSchemaResolver - the resolver checks for SlugProvider
    implementation before falling back to auto-generation, making the protocol transparent to
    the storage layer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SlugProvider(Protocol):
    """
    Protocol for objects that can generate custom slugs for primitive schema resolution.
    
    Objects implementing this protocol can generate their own slug via `generate_slug()`.
    If not implemented, the resolver will auto-generate a time-based slug.
    """
    
    def generate_slug(self) -> str:
        """
        Generate a custom slug for this object.
        
        If implemented, this method should return a string that uniquely identifies
        this object. The resolver will use this slug instead of auto-generating one.
        
        Returns:
            A string slug for this object
            
        Note:
            If the returned slug conflicts with an existing file, the resolver will
            raise a ValueError. The object is responsible for ensuring uniqueness.
        """
        ...
