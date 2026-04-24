# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for SimpleCacheORM with PrimitiveSchemaResolver.
"""

import os
from typing import Any, Dict, Optional, Tuple

import pytest
from pydantic import BaseModel

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
from totodev_pub.cached_file_folders_support.primitive_schema_resolver import PrimitiveSchemaResolver
from totodev_pub.cached_file_folders_support.simple_cache_orm import SimpleCacheORM


# -----------------------------
# Helper models and loader funcs
# -----------------------------

class User(BaseModel):
    name: str
    email: str
    
    @classmethod
    def load(cls, file_path: str):
        """Load method for SimpleCacheORM."""
        import json
        import yaml
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        try:
            data = json.loads(content)
        except Exception:
            data = yaml.safe_load(content)
        return cls.model_validate(data)


class Order(BaseModel):
    order_id: str
    amount: float
    
    @classmethod
    def load(cls, file_path: str):
        """Load method for SimpleCacheORM."""
        import json
        import yaml
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        try:
            data = json.loads(content)
        except Exception:
            data = yaml.safe_load(content)
        return cls.model_validate(data)


class UserRecord:
    """Minimal class with a load() classmethod used by SimpleCacheORM."""
    @classmethod
    def load(cls, file_path: str) -> Dict[str, Any]:
        # YAML/JSON auto-detection is not required here; just read text to ensure loader is invoked.
        try:
            import json
            import yaml
        except Exception:
            # As a very last resort, return the raw text
            with open(file_path, "r", encoding="utf-8") as f:
                return {"raw": f.read()}
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        try:
            return {"json": json.loads(content)}
        except Exception:
            try:
                return {"yaml": yaml.safe_load(content)}
            except Exception:
                return {"raw": content}


def loader_returns_path(path: str) -> str:
    return path


def loader_accepts_ref(ref: CachedFileRef) -> Tuple[bool, str]:
    return (os.path.exists(ref.file_path), os.path.basename(ref.file_path))


# -----------------------------
# Fixtures
# -----------------------------

@pytest.fixture()
def cache_root(tmp_path) -> str:
    # Project rule: use volatile/ for temp/working files
    volatile_root = tmp_path / "volatile" / "test_simple_cache_orm"
    volatile_root.mkdir(parents=True, exist_ok=True)
    return str(volatile_root.resolve())


@pytest.fixture()
def cache(cache_root: str) -> CachedFileFolders:
    # Flat pattern for PrimitiveSchemaResolver
    return CachedFileFolders("prototype/", cache_root)


@pytest.fixture()
def resolver(cache: CachedFileFolders) -> PrimitiveSchemaResolver:
    return PrimitiveSchemaResolver(cache, grouping_key=None)


@pytest.fixture()
def orm(cache: CachedFileFolders, resolver: PrimitiveSchemaResolver) -> SimpleCacheORM:
    # PrimitiveSchemaResolver doesn't need schema registration
    return SimpleCacheORM(cache, resolver=resolver)


# -----------------------------
# Tests
# -----------------------------

def test_register_and_resolve_path(orm: SimpleCacheORM):
    """Test resolve_path with PrimitiveSchemaResolver."""
    # PrimitiveSchemaResolver requires slug parameter
    grouping_key, ref_path = orm.resolve_path(User, slug="abc123")
    assert grouping_key == ()  # Flat pattern normalizes to empty tuple
    assert ref_path == "User/User-abc123.yaml"


@pytest.mark.asyncio
async def test_upsert_exists_get_with_class_loader(orm: SimpleCacheORM, cache: CachedFileFolders):
    """Test upsert, exists, and get with class loader."""
    # Create a user object
    user = User(name="Alice Example", email="alice@example.com")
    
    # Upsert - need to provide slug explicitly
    slug = "alice-user"
    await orm.upsert(
        user,
        schema_key=User,
        slug=slug,
    )
    
    assert orm.exists(User, slug=slug) is True

    # Retrieve using class loader
    loaded = orm.get(User, schema_key=User, slug=slug)
    assert loaded is not None
    assert loaded.name == "Alice Example"
    assert loaded.email == "alice@example.com"


@pytest.mark.asyncio
async def test_upsert_with_custom_slug(orm: SimpleCacheORM):
    """Test upsert with custom slug from SlugProvider."""
    class UserWithSlug(User):
        def generate_slug(self) -> str:
            return self.email.split("@")[0]
    
    # UserWithSlug implements SlugProvider protocol (duck typing)
    user = UserWithSlug(name="Bob", email="bob@example.com")
    
    # For PrimitiveSchemaResolver, we can use resolver.save() directly to get auto-generated slug
    # Or provide slug explicitly to SimpleCacheORM
    slug = "bob"  # Explicit slug for SimpleCacheORM
    await orm.upsert(user, schema_key=UserWithSlug, slug=slug)
    
    # Should be saved with slug "bob"
    assert orm.exists(UserWithSlug, slug="bob") is True
    
    loaded = orm.get(UserWithSlug, schema_key=UserWithSlug, slug="bob")
    assert loaded is not None
    assert loaded.name == "Bob"


@pytest.mark.asyncio
async def test_objects_iteration_and_loader_accepts_ref(orm: SimpleCacheORM):
    """Test objects iteration with different loader types."""
    # Insert two orders with explicit slugs
    order1 = Order(order_id="ORD-001", amount=99.99)
    order2 = Order(order_id="ORD-002", amount=199.99)
    
    await orm.upsert(order1, schema_key=Order, slug="order-001")
    await orm.upsert(order2, schema_key=Order, slug="order-002")

    # Iterate with loader that accepts path by default
    paths = list(
        orm.objects(
            loader_returns_path,
            schema_key=Order,
            slug="*",
        )
    )
    assert len(paths) == 2
    for p in paths:
        assert os.path.isabs(p)
        assert os.path.exists(p)

    # Iterate with loader that accepts CachedFileRef
    ref_results = list(
        orm.objects(
            loader_accepts_ref,
            schema_key=Order,
            loader_accepts_ref=True,
            slug="*",
        )
    )
    assert len(ref_results) == 2
    for exists, basename in ref_results:
        assert exists is True
        assert basename.endswith(".yaml")


@pytest.mark.asyncio
async def test_delete_and_exists(orm: SimpleCacheORM):
    """Test delete and exists operations."""
    order = Order(order_id="ORD-DELETE", amount=50.0)
    slug = "to-delete"
    
    await orm.upsert(order, schema_key=Order, slug=slug)
    assert orm.exists(Order, slug=slug) is True
    
    await orm.delete(Order, slug=slug)
    assert orm.exists(Order, slug=slug) is False


@pytest.mark.asyncio
async def test_infer_params_from_base_model(orm: SimpleCacheORM):
    """Test infer_params with BaseModel (not applicable to PrimitiveSchemaResolver)."""
    # PrimitiveSchemaResolver only accepts "slug" parameter
    # infer_params doesn't help since slug is required and can't be inferred
    user = User(name="Bob", email="bob@example.com")
    
    # Must provide slug explicitly
    await orm.upsert(user, schema_key=User, slug="bob-user")
    assert orm.exists(User, slug="bob-user") is True


@pytest.mark.asyncio
async def test_infer_params_from_dict(orm: SimpleCacheORM):
    """Test infer_params with dict (not applicable to PrimitiveSchemaResolver)."""
    # PrimitiveSchemaResolver only accepts "slug" parameter
    source = {
        "name": "Charlie",
        "email": "charlie@example.com",
    }
    
    # Must provide slug explicitly
    await orm.upsert(source, schema_key=User, slug="charlie-user")
    assert orm.exists(User, slug="charlie-user") is True


def test_objects_requires_schema_key_for_plain_callable(orm: SimpleCacheORM):
    """Test that objects() requires schema_key for plain callable."""
    with pytest.raises(ValueError):
        list(orm.objects(loader_returns_path, slug="*"))


def test_get_requires_schema_key_for_plain_callable(orm: SimpleCacheORM):
    """Test that get() requires schema_key for plain callable."""
    with pytest.raises(ValueError):
        orm.get(loader_returns_path, slug="test")


@pytest.mark.asyncio
async def test_missing_required_params_error(orm: SimpleCacheORM):
    """Test error when required slug parameter is missing."""
    user = User(name="Zed", email="zed@example.com")

    with pytest.raises(ValueError) as ei:
        await orm.upsert(user, schema_key=User, infer_params=False)
    msg = str(ei.value)
    assert "Missing required parameter" in msg
    assert "slug" in msg


@pytest.mark.asyncio
async def test_required_params(orm: SimpleCacheORM):
    """Test that required_params returns ['slug'] for PrimitiveSchemaResolver."""
    params = orm.resolver.required_params(User)
    assert params == ["slug"]


@pytest.mark.asyncio
async def test_iter_refs(orm: SimpleCacheORM):
    """Test iter_refs from SchemaResolver protocol."""
    user1 = User(name="Alice", email="alice@example.com")
    user2 = User(name="Bob", email="bob@example.com")
    
    await orm.upsert(user1, schema_key=User, slug="alice")
    await orm.upsert(user2, schema_key=User, slug="bob")
    
    refs = list(orm.resolver.iter_refs(User))
    assert len(refs) == 2


@pytest.mark.asyncio
async def test_resolve_ref(orm: SimpleCacheORM):
    """Test resolve_ref from SchemaResolver protocol."""
    user = User(name="Alice", email="alice@example.com")
    slug = "alice-user"
    
    await orm.upsert(user, schema_key=User, slug=slug)
    
    ref = orm.resolver.resolve_ref(User, slug=slug)
    assert ref is not None
    assert ref.ref_path == f"User/User-{slug}.yaml"
