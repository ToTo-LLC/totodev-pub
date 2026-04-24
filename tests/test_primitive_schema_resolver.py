# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for PrimitiveSchemaResolver.
"""

import json
from pathlib import Path
import pytest
import yaml
from pydantic import BaseModel, ValidationError

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.primitive_schema_resolver import (
    PrimitiveSchemaResolver,
)
from totodev_pub.cached_file_folders_support.primitive_schema_protocol import SlugProvider


# Test models
class User(BaseModel):
    name: str
    email: str


class Order(BaseModel):
    order_id: str
    amount: float


class UserWithSlug(User):
    """User that implements SlugProvider protocol."""
    
    def generate_slug(self) -> str:
        # Use email as slug (before @)
        return self.email.split("@")[0]


class UserWithEmptySlug(User):
    """User that returns empty string from generate_slug."""
    
    def generate_slug(self) -> str:
        return ""


class UserWithExceptionSlug(User):
    """User that raises exception in generate_slug."""
    
    def generate_slug(self) -> str:
        raise RuntimeError("Slug generation failed")


class UserWithNoneSlug(User):
    """User that returns None from generate_slug (shouldn't happen but test it)."""
    
    def generate_slug(self) -> str:
        return None  # type: ignore


@pytest.fixture
def cache(tmp_path: Path) -> CachedFileFolders:
    """Real cache for integration tests with a temp root."""
    pattern = "prototype/"
    return CachedFileFolders(pattern, tmp_path)


@pytest.fixture
def resolver(cache) -> PrimitiveSchemaResolver:
    """Resolver for integration tests."""
    return PrimitiveSchemaResolver(cache, grouping_key=None)


# ----------------------------
# Basic save/load operations
# ----------------------------

@pytest.mark.asyncio
async def test_save_and_load_basic(resolver: PrimitiveSchemaResolver) -> None:
    """Test basic save and load operations."""
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    # Time-based slug should be a base36 string
    assert slug
    assert isinstance(slug, str)
    assert len(slug) > 0
    
    loaded = resolver.load(User, slug)
    assert loaded is not None
    assert loaded.name == "Alice"
    assert loaded.email == "alice@example.com"


@pytest.mark.asyncio
async def test_save_multiple_objects(resolver: PrimitiveSchemaResolver) -> None:
    """Test saving multiple objects of the same type."""
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    
    slug1 = await resolver.save(user1)
    slug2 = await resolver.save(user2)
    
    # Time-based slugs should be different
    assert slug1 != slug2
    assert isinstance(slug1, str)
    assert isinstance(slug2, str)
    
    loaded1 = resolver.load(User, slug1)
    loaded2 = resolver.load(User, slug2)
    
    assert loaded1 is not None
    assert loaded2 is not None
    assert loaded1.name == "Alice"
    assert loaded2.name == "Bob"


@pytest.mark.asyncio
async def test_save_different_types(resolver: PrimitiveSchemaResolver) -> None:
    """Test saving objects of different types."""
    user = User(name="Alice", email="alice@example.com")
    order = Order(order_id="ORD-001", amount=99.99)
    
    user_slug = await resolver.save(user)
    order_slug = await resolver.save(order)
    
    # Different types can have same slug (unlikely with time-based, but possible)
    # Just verify they're valid slugs
    assert isinstance(user_slug, str)
    assert isinstance(order_slug, str)
    
    loaded_user = resolver.load(User, user_slug)
    loaded_order = resolver.load(Order, order_slug)
    
    assert loaded_user is not None
    assert loaded_order is not None
    assert loaded_user.name == "Alice"
    assert loaded_order.order_id == "ORD-001"


@pytest.mark.asyncio
async def test_load_nonexistent(resolver: PrimitiveSchemaResolver) -> None:
    """Test loading a non-existent object returns None."""
    result = resolver.load(User, "nonexistent-slug")
    assert result is None


# ----------------------------
# Slug generation
# ----------------------------

@pytest.mark.asyncio
async def test_auto_slug_time_based(resolver: PrimitiveSchemaResolver) -> None:
    """Test that auto-generated slugs are time-based."""
    import time
    from totodev_pub.cached_file_folders_support.primitive_schema_resolver import EPOCH_2025, _int_to_base36
    
    # Save a user
    user = User(name="User", email="user@example.com")
    slug = await resolver.save(user)
    
    # Slug should be a base36 string
    assert isinstance(slug, str)
    assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in slug)
    
    # Verify it's a valid time-based slug (seconds since epoch)
    # We can't easily verify exact value, but we can check format


@pytest.mark.asyncio
async def test_custom_slug_from_protocol(resolver: PrimitiveSchemaResolver) -> None:
    """Test that objects implementing SlugProvider can provide custom slugs."""
    user = UserWithSlug(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Should use email prefix as slug
    assert slug == "alice"
    
    # Verify it was saved correctly
    loaded = resolver.load(UserWithSlug, slug)
    assert loaded is not None
    assert loaded.name == "Alice"


@pytest.mark.asyncio
async def test_custom_slug_collision_raises(resolver: PrimitiveSchemaResolver) -> None:
    """Test that custom slug collisions raise ValueError."""
    user1 = UserWithSlug(name="Alice", email="alice@example.com")
    user2 = UserWithSlug(name="Bob", email="alice@example.com")  # Same email prefix
    
    slug1 = await resolver.save(user1)
    assert slug1 == "alice"
    
    # Second save with same custom slug should raise
    with pytest.raises(ValueError, match="conflicts with existing file"):
        await resolver.save(user2)


# ----------------------------
# Delete operations
# ----------------------------

@pytest.mark.asyncio
async def test_delete_existing(resolver: PrimitiveSchemaResolver) -> None:
    """Test deleting an existing object."""
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    result = await resolver.delete(User, slug)
    assert result is True
    
    # Verify it's gone
    loaded = resolver.load(User, slug)
    assert loaded is None


@pytest.mark.asyncio
async def test_delete_nonexistent(resolver: PrimitiveSchemaResolver) -> None:
    """Test deleting a non-existent object returns False."""
    result = await resolver.delete(User, "nonexistent-slug")
    assert result is False


# ----------------------------
# Iteration
# ----------------------------

@pytest.mark.asyncio
async def test_iter_objects_single_type(resolver: PrimitiveSchemaResolver) -> None:
    """Test iterating over objects of a single type."""
    # Save multiple users
    users = [
        User(name="Alice", email="alice@example.com"),
        User(name="Bob", email="bob@example.com"),
        User(name="Charlie", email="charlie@example.com"),
    ]
    
    slugs = []
    for user in users:
        slug = await resolver.save(user)
        slugs.append(slug)
    
    # Iterate and collect
    loaded_users = list(resolver.iter_objects(User))
    
    assert len(loaded_users) == 3
    names = {user.name for user in loaded_users}
    assert names == {"Alice", "Bob", "Charlie"}


@pytest.mark.asyncio
async def test_iter_objects_multiple_types(resolver: PrimitiveSchemaResolver) -> None:
    """Test that iter_objects only returns objects of the specified type."""
    user = User(name="Alice", email="alice@example.com")
    order = Order(order_id="ORD-001", amount=99.99)
    
    await resolver.save(user)
    await resolver.save(order)
    
    # Iterate users - should only get user
    users = list(resolver.iter_objects(User))
    assert len(users) == 1
    assert users[0].name == "Alice"
    
    # Iterate orders - should only get order
    orders = list(resolver.iter_objects(Order))
    assert len(orders) == 1
    assert orders[0].order_id == "ORD-001"


@pytest.mark.asyncio
async def test_iter_objects_empty(resolver: PrimitiveSchemaResolver) -> None:
    """Test iterating when no objects exist."""
    users = list(resolver.iter_objects(User))
    assert users == []


# ----------------------------
# File organization
# ----------------------------

@pytest.mark.asyncio
async def test_files_organized_by_class(resolver: PrimitiveSchemaResolver, cache: CachedFileFolders) -> None:
    """Test that files are organized in subdirectories by class name."""
    user = User(name="Alice", email="alice@example.com")
    order = Order(order_id="ORD-001", amount=99.99)
    
    user_slug = await resolver.save(user)
    order_slug = await resolver.save(order)
    
    # Check that files exist in expected locations
    grouping = cache.grouping(None)
    
    # User file should be in User/ subdirectory
    user_ref = grouping.find_file(f"User/User-{user_slug}.yaml")
    assert user_ref is not None
    
    # Order file should be in Order/ subdirectory
    order_ref = grouping.find_file(f"Order/Order-{order_slug}.yaml")
    assert order_ref is not None


# ----------------------------
# Export schema
# ----------------------------

# Note: export_schema() method was removed in simplification


# ----------------------------
# File format options
# ----------------------------

@pytest.mark.asyncio
async def test_json_format(tmp_path: Path) -> None:
    """Test resolver with JSON format."""
    cache = CachedFileFolders("prototype/", tmp_path)
    resolver = PrimitiveSchemaResolver(cache, grouping_key=None, file_format="json")
    
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Verify file is JSON
    grouping = cache.grouping(None)
    file_ref = grouping.find_file(f"User/User-{slug}.json")
    assert file_ref is not None
    
    # Verify it can be loaded
    loaded = resolver.load(User, slug)
    assert loaded is not None
    assert loaded.name == "Alice"


@pytest.mark.asyncio
async def test_invalid_file_format(tmp_path: Path) -> None:
    """Test that invalid file format raises ValueError."""
    cache = CachedFileFolders("prototype/", tmp_path)
    with pytest.raises(ValueError, match="file_format must be"):
        PrimitiveSchemaResolver(cache, grouping_key=None, file_format="xml")


# ----------------------------
# Edge cases
# ----------------------------

@pytest.mark.asyncio
async def test_save_overwrites_existing(resolver: PrimitiveSchemaResolver) -> None:
    """Test that saving with same slug overwrites existing object."""
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    
    slug1 = await resolver.save(user1)
    
    # They should get different slugs (time-based)
    slug2 = await resolver.save(user2)
    assert slug1 != slug2


# ----------------------------
# Load error handling
# ----------------------------

@pytest.mark.asyncio
async def test_load_corrupted_yaml(resolver: PrimitiveSchemaResolver) -> None:
    """Test loading a file with corrupted YAML."""
    # Save a valid user first
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Corrupt the file
    grouping = resolver.grouping
    file_ref = grouping.find_file(f"User/User-{slug}.yaml")
    assert file_ref is not None
    
    with open(file_ref.file_path, "w", encoding="utf-8") as f:
        f.write("invalid: yaml: content: [")
    
    # Try to load - should return None with warning logged
    loaded = resolver.load(User, slug)
    assert loaded is None


@pytest.mark.asyncio
async def test_load_corrupted_json(tmp_path: Path) -> None:
    """Test loading a file with corrupted JSON."""
    cache = CachedFileFolders("prototype/", tmp_path)
    resolver = PrimitiveSchemaResolver(cache, grouping_key=None, file_format="json")
    
    # Save a valid user first
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Corrupt the file
    grouping = resolver.grouping
    file_ref = grouping.find_file(f"User/User-{slug}.json")
    assert file_ref is not None
    
    with open(file_ref.file_path, "w", encoding="utf-8") as f:
        f.write("{invalid json")
    
    # Try to load - should return None
    loaded = resolver.load(User, slug)
    assert loaded is None


@pytest.mark.asyncio
async def test_load_invalid_schema(resolver: PrimitiveSchemaResolver) -> None:
    """Test loading a file with valid YAML but invalid schema."""
    # Save a valid user first
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Corrupt the file with invalid schema (missing required field)
    grouping = resolver.grouping
    file_ref = grouping.find_file(f"User/User-{slug}.yaml")
    assert file_ref is not None
    
    with open(file_ref.file_path, "w", encoding="utf-8") as f:
        yaml.dump({"name": "Bob"}, f)  # Missing email field
    
    # Try to load - should return None (ValidationError caught)
    loaded = resolver.load(User, slug)
    assert loaded is None


@pytest.mark.asyncio
async def test_load_empty_file(resolver: PrimitiveSchemaResolver) -> None:
    """Test loading an empty file."""
    # Save a valid user first
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Empty the file
    grouping = resolver.grouping
    file_ref = grouping.find_file(f"User/User-{slug}.yaml")
    assert file_ref is not None
    
    with open(file_ref.file_path, "w", encoding="utf-8") as f:
        f.write("")
    
    # Try to load - should return None
    loaded = resolver.load(User, slug)
    assert loaded is None


# ----------------------------
# Iteration edge cases
# ----------------------------

@pytest.mark.asyncio
async def test_iter_objects_skips_unparseable_slugs(resolver: PrimitiveSchemaResolver) -> None:
    """Test that iteration skips files with unparseable slugs."""
    # Save valid users
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    await resolver.save(user1)
    await resolver.save(user2)
    
    # Create a file with invalid format (doesn't match pattern)
    grouping = resolver.grouping
    invalid_user = User(name="Invalid", email="invalid@example.com")
    from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy
    proxy = SerializableDataProxy(invalid_user, "User/Invalid-format.yaml")
    await grouping.upsert_file(proxy)
    
    # Iterate - should get 2 valid users, skip invalid one
    users = list(resolver.iter_objects(User))
    assert len(users) == 2
    names = {user.name for user in users}
    assert names == {"Alice", "Bob"}


@pytest.mark.asyncio
async def test_iter_objects_skips_corrupted_files(resolver: PrimitiveSchemaResolver) -> None:
    """Test that iteration skips corrupted files."""
    # Save valid users
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    slug1 = await resolver.save(user1)
    await resolver.save(user2)
    
    # Corrupt one file
    grouping = resolver.grouping
    file_ref = grouping.find_file(f"User/User-{slug1}.yaml")
    assert file_ref is not None
    
    with open(file_ref.file_path, "w", encoding="utf-8") as f:
        f.write("invalid: yaml: [")
    
    # Iterate - should get 1 valid user, skip corrupted one
    users = list(resolver.iter_objects(User))
    assert len(users) == 1
    assert users[0].name == "Bob"


@pytest.mark.asyncio
async def test_iter_objects_skips_invalid_schema_files(resolver: PrimitiveSchemaResolver) -> None:
    """Test that iteration skips files with invalid schema."""
    # Save valid users
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    slug1 = await resolver.save(user1)
    await resolver.save(user2)
    
    # Corrupt one file with invalid schema
    grouping = resolver.grouping
    file_ref = grouping.find_file(f"User/User-{slug1}.yaml")
    assert file_ref is not None
    
    with open(file_ref.file_path, "w", encoding="utf-8") as f:
        yaml.dump({"name": "Invalid"}, f)  # Missing email
    
    # Iterate - should get 1 valid user, skip invalid schema one
    users = list(resolver.iter_objects(User))
    assert len(users) == 1
    assert users[0].name == "Bob"


# ----------------------------
# SlugProvider edge cases
# ----------------------------

@pytest.mark.asyncio
async def test_slug_provider_empty_string_falls_back(resolver: PrimitiveSchemaResolver) -> None:
    """Test that empty string from SlugProvider falls back to auto-generation."""
    user = UserWithEmptySlug(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Should fall back to auto-generated slug (time-based)
    assert slug
    assert isinstance(slug, str)
    assert len(slug) > 0
    
    # Verify it was saved
    loaded = resolver.load(UserWithEmptySlug, slug)
    assert loaded is not None
    assert loaded.name == "Alice"


@pytest.mark.asyncio
async def test_slug_provider_exception_falls_back(resolver: PrimitiveSchemaResolver) -> None:
    """Test that exception in SlugProvider falls back to auto-generation."""
    user = UserWithExceptionSlug(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Should fall back to auto-generated slug (time-based)
    assert slug
    assert isinstance(slug, str)
    assert len(slug) > 0
    
    # Verify it was saved
    loaded = resolver.load(UserWithExceptionSlug, slug)
    assert loaded is not None
    assert loaded.name == "Alice"


@pytest.mark.asyncio
async def test_slug_provider_none_falls_back(resolver: PrimitiveSchemaResolver) -> None:
    """Test that None from SlugProvider falls back to auto-generation."""
    user = UserWithNoneSlug(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Should fall back to auto-generated slug (time-based)
    assert slug
    assert isinstance(slug, str)
    assert len(slug) > 0
    
    # Verify it was saved
    loaded = resolver.load(UserWithNoneSlug, slug)
    assert loaded is not None
    assert loaded.name == "Alice"


# ----------------------------
# Path parsing edge cases
# ----------------------------

@pytest.mark.asyncio
async def test_parse_slug_special_characters(resolver: PrimitiveSchemaResolver) -> None:
    """Test parsing slugs with special characters."""
    # Create file with special characters in slug
    grouping = resolver.grouping
    user = User(name="Test", email="test@example.com")
    from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy
    proxy = SerializableDataProxy(user, "User/User-my-slug-123.yaml")
    await grouping.upsert_file(proxy)
    
    # Parse slug
    slug = resolver._parse_slug_from_ref_path("User/User-my-slug-123.yaml", "User")
    assert slug == "my-slug-123"


@pytest.mark.asyncio
async def test_parse_slug_unicode(resolver: PrimitiveSchemaResolver) -> None:
    """Test parsing slugs with unicode characters."""
    # Create file with unicode slug
    grouping = resolver.grouping
    user = User(name="Test", email="test@example.com")
    from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy
    proxy = SerializableDataProxy(user, "User/User-测试.yaml")
    await grouping.upsert_file(proxy)
    
    # Parse slug
    slug = resolver._parse_slug_from_ref_path("User/User-测试.yaml", "User")
    assert slug == "测试"


@pytest.mark.asyncio
async def test_parse_slug_with_dots(resolver: PrimitiveSchemaResolver) -> None:
    """Test parsing slugs with dots."""
    # Create file with slug containing dots
    grouping = resolver.grouping
    user = User(name="Test", email="test@example.com")
    from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy
    proxy = SerializableDataProxy(user, "User/User-1.2.3.yaml")
    await grouping.upsert_file(proxy)
    
    # Parse slug
    slug = resolver._parse_slug_from_ref_path("User/User-1.2.3.yaml", "User")
    assert slug == "1.2.3"


@pytest.mark.asyncio
async def test_parse_slug_invalid_format_returns_none(resolver: PrimitiveSchemaResolver) -> None:
    """Test that invalid format returns None."""
    # Test various invalid formats
    assert resolver._parse_slug_from_ref_path("User/Other-1.yaml", "User") is None
    assert resolver._parse_slug_from_ref_path("User/1.yaml", "User") is None
    assert resolver._parse_slug_from_ref_path("Wrong/User-1.yaml", "User") is None
    assert resolver._parse_slug_from_ref_path("User-1.yaml", "User") is None


# ----------------------------
# File format edge cases
# ----------------------------

@pytest.mark.asyncio
async def test_file_format_case_insensitive(tmp_path: Path) -> None:
    """Test that file format is case insensitive."""
    cache = CachedFileFolders("prototype/", tmp_path)
    resolver = PrimitiveSchemaResolver(cache, grouping_key=None, file_format="YAML")
    
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    # Should work correctly
    assert slug
    loaded = resolver.load(User, slug)
    assert loaded is not None
    assert loaded.name == "Alice"


# ----------------------------
# Grouping key handling
# ----------------------------

@pytest.mark.asyncio
async def test_non_none_grouping_key(tmp_path: Path) -> None:
    """Test resolver with non-None grouping key."""
    cache = CachedFileFolders("projects/{project}/", tmp_path)
    resolver = PrimitiveSchemaResolver(cache, grouping_key=("webapp",))
    
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    assert slug
    
    # Verify it can be loaded
    loaded = resolver.load(User, slug)
    assert loaded is not None
    assert loaded.name == "Alice"
    
    # Verify file is in correct grouping directory
    grouping = cache.grouping(("webapp",))
    file_ref = grouping.find_file(f"User/User-{slug}.yaml")
    assert file_ref is not None


# ----------------------------
# Integration and edge cases
# ----------------------------

# Note: export_schema() method was removed in simplification


@pytest.mark.asyncio
async def test_delete_and_save_does_not_reuse_slug(resolver: PrimitiveSchemaResolver) -> None:
    """Test that deleting and saving doesn't reuse slug."""
    import time
    
    user1 = User(name="Alice", email="alice@example.com")
    slug1 = await resolver.save(user1)
    
    await resolver.delete(User, slug1)
    
    # Add delay to ensure different timestamp (time-based slugs use seconds)
    time.sleep(1.1)  # More than 1 second to ensure different base slug
    
    user2 = User(name="Bob", email="bob@example.com")
    slug2 = await resolver.save(user2)
    
    # Should get new slug (time-based), not reuse deleted one
    assert slug1 != slug2


@pytest.mark.asyncio
async def test_iter_objects_after_delete(resolver: PrimitiveSchemaResolver) -> None:
    """Test iteration after deletion."""
    # Save 3 users
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    user3 = User(name="Charlie", email="charlie@example.com")
    
    slug1 = await resolver.save(user1)
    slug2 = await resolver.save(user2)
    await resolver.save(user3)
    
    # Delete middle one
    await resolver.delete(User, slug2)
    
    # Iterate - should get 2 users
    users = list(resolver.iter_objects(User))
    assert len(users) == 2
    names = {user.name for user in users}
    assert names == {"Alice", "Charlie"}


# ----------------------------
# SchemaResolver protocol tests
# ----------------------------

@pytest.mark.asyncio
async def test_register_map(resolver: PrimitiveSchemaResolver) -> None:
    """Test register_map (no-op but tracks classes)."""
    schema = {
        User: {"grouping_key_template": (), "ref_path_template": "User/User-{slug}.yaml"},
    }
    resolver.register_map(schema)
    
    # Should track the class
    assert User in resolver._registered_classes


@pytest.mark.asyncio
async def test_resolve_path(resolver: PrimitiveSchemaResolver) -> None:
    """Test resolve_path from SchemaResolver protocol."""
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    grouping_key, ref_path = resolver.resolve_path(User, slug=slug)
    assert grouping_key == resolver.grouping_key
    assert ref_path == f"User/User-{slug}.yaml"


@pytest.mark.asyncio
async def test_resolve_ref(resolver: PrimitiveSchemaResolver) -> None:
    """Test resolve_ref from SchemaResolver protocol."""
    user = User(name="Alice", email="alice@example.com")
    slug = await resolver.save(user)
    
    ref = resolver.resolve_ref(User, slug=slug)
    assert ref is not None
    assert ref.ref_path == f"User/User-{slug}.yaml"


@pytest.mark.asyncio
async def test_iter_refs(resolver: PrimitiveSchemaResolver) -> None:
    """Test iter_refs from SchemaResolver protocol."""
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    await resolver.save(user1)
    await resolver.save(user2)
    
    refs = list(resolver.iter_refs(User))
    assert len(refs) == 2


@pytest.mark.asyncio
async def test_required_params(resolver: PrimitiveSchemaResolver) -> None:
    """Test required_params from SchemaResolver protocol."""
    params = resolver.required_params(User)
    assert params == ["slug"]
