# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Simple ORM-like helper built on SchemaResolver protocol
=======================================================

What this is
------------

- This class provides a simple mechanism for persisting in-memory data objects to the
  filesystem, using `CachedFileFolders` behind the scenes. It relies on a `SchemaResolver`
  (e.g., `PrimitiveSchemaResolver`) to translate semantic parameters into concrete grouping keys and
  file paths.
- One way to think about `SimpleCacheORM` is as a very lightweight DBMS for relatively
  stable or slowly changing data. It intentionally does NOT promise database guarantees
  such as ACID transactions, global concurrency controls, fast secondary indexes, or SQL
  queries. In many real-world applications, a large portion of data is accessed with very
  simple patterns that do not require those features—this utility targets that space.
- The behavior and API vary depending on which `SchemaResolver` implementation you use.
  `PrimitiveSchemaResolver` provides zero-configuration persistence with classes, while
  other resolvers may require explicit schema registration with path templates.

Recommended model shape
-----------------------

- Using `FileMappedPydanticMixin` with Pydantic v2 `BaseModel` types is strongly
  recommended for file-backed persistence. It streamlines reading/writing and can offer
  basic concurrency protection for individual files.
- Plain Pydantic `BaseModel` subclasses can also work, but typically require more manual
  effort for read/write operations.

Loaders and CachedFileRef
-------------------------

- Some loaders need information from both the file and its associated `slave_dir` (for
  example, when auxiliary assets live next to the main record). In these cases, provide a
  callable loader and pass `loader_accepts_ref=True`. The loader will receive a
  `CachedFileRef`, which includes both the `ref_path` and the `slave_dir` location.

Schema mapping guidance (for custom resolvers)
------------------------------------------------

Note: This section applies to custom `SchemaResolver` implementations that require explicit
schema registration. `PrimitiveSchemaResolver` does NOT require schema registration—see
examples below.

For custom resolvers, one practical way to structure your mapping dict is to define two kinds
of entries:

- Storage entries per record class (e.g., `"UserRecord"`, `"OrderRecord"`, `"ProductRecord"`)
  to search and operate across all instances of that type.
- Specialized "search" entries (e.g., `"completed_orders"`, `"active_users"`) to filter
  or iterate specific subsets efficiently.

Reminder on grouping keys (for custom resolvers)
------------------------------------------------

Note: This section applies to custom `SchemaResolver` implementations. `PrimitiveSchemaResolver`
uses a single grouping_key specified at construction time.

The `grouping_key` contains only placeholder values from the cache's `grouping_pattern`;
literal segments (like `"partX"` or `"users"`) never appear directly in the key. For example,
with `grouping_pattern="partX/{sub_1}/partY/{sub_2}"`, the schema should provide
`grouping_key_template=("{sub_1}", "{sub_2}")`, and the resolved `grouping_key` will be
`("value_for_sub_1", "value_for_sub_2")`.

On uniqueness and filenames
---------------------------

Files generally need a natural or synthetic unique element embedded in directory and/or
filenames. How this is handled depends on the resolver:

- With `PrimitiveSchemaResolver`: Uniqueness is handled automatically via slug generation
  (time-based or custom via SlugProvider protocol). You provide the `slug` parameter, and
  the resolver ensures unique file paths.
- With custom resolvers: You design the path template to include unique identifiers. For
  example:
  - `UserRecord` might include the email address or a slug in the filename.
  - An uploaded document might include a timestamp and the uploader's identifier.

API overview
------------

- For `PrimitiveSchemaResolver`: No schema registration needed. Pass classes directly as
  `schema_key` (e.g., `schema_key=User`). The only required parameter is `slug`.
- For custom resolvers: Register schema and model bindings once via `register_models` (or
  pass `schema=` to the constructor). The resolver then knows how to compute addresses
  for your records.
- `get` and `objects` hydrate records using either a class with a `load(path)` classmethod
  or a custom loader function (optionally accepting `CachedFileRef` via
  `loader_accepts_ref=True`).
- `upsert` writes or overwrites a record using its schema mapping. With
  `infer_params=True`, missing parameters are inferred from the source object's attributes
  or dict keys when possible (note: `PrimitiveSchemaResolver` only requires `slug`, which
  cannot be inferred).
- `delete` removes a record by computed address. `exists` checks for presence.

Examples
--------

Example 1: Using PrimitiveSchemaResolver (zero-configuration)
--------------------------------------------------------------

```python
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.primitive_schema_resolver import PrimitiveSchemaResolver
from totodev_pub.cached_file_folders_support import SimpleCacheORM
from pydantic import BaseModel

class User(BaseModel):
    name: str
    email: str

# No schema registration needed with PrimitiveSchemaResolver
cache = CachedFileFolders("prototype/", "/cache/root")
resolver = PrimitiveSchemaResolver(cache, grouping_key=None)
orm = SimpleCacheORM(cache, resolver=resolver)

# Use classes directly as schema_key - only slug parameter needed
user = User(name="Alice", email="alice@example.com")
await orm.upsert(user, schema_key=User, slug="alice-user")

# Load by class and slug
loaded = orm.get(User, schema_key=User, slug="alice-user")

# Iterate all users
for user in orm.objects(User, schema_key=User, slug="*"):
    print(user.name)

# Delete
await orm.delete(User, slug="alice-user")
```

Example 2: Using custom resolver with schema registration
----------------------------------------------------------

```python
# For custom resolvers, register schema patterns
schema = {
    "UserRecord": dict(
        grouping_key_template=("tenant", "{tenant_slug}", "users"),
        ref_path_template="{user_slug}/profile.yaml",
        class_name="UserRecord",
    ),
    "uploaded_doc": dict(
        grouping_key_template=("tenant", "{tenant_slug}", "uploads"),
        ref_path_template="{doc_id}.json",
    ),
}

cache = CachedFileFolders("tenants/{tenant}/{category}/", "/cache/root")
# Using a custom resolver (not PrimitiveSchemaResolver)
from myapp.custom_resolver import CustomSchemaResolver
resolver = CustomSchemaResolver(cache)
orm = SimpleCacheORM(cache, schema=schema, resolver=resolver)

# Uses UserRecord.load(path) automatically if available
user = orm.get(UserRecord, tenant_slug="acme", user_slug="jdoe")
await orm.upsert(user, schema_key="UserRecord", tenant_slug="acme", user_slug="jdoe")

# String-based access with explicit loader (receives path by default)
from myapp.loaders import load_uploaded_doc

doc = orm.get(
    load_uploaded_doc,
    schema_key="uploaded_doc",
    tenant_slug="acme",
    doc_id="123",
)

# Loader that needs both file path and slave_dir can accept CachedFileRef
from myapp.loaders import load_with_assets

doc2 = orm.get(
    load_with_assets,
    schema_key="uploaded_doc",
    loader_accepts_ref=True,  # loader(ref: CachedFileRef)
    tenant_slug="acme",
    doc_id="123",
)
```
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Union

from pydantic import BaseModel

from totodev_pub.cached_file_folders import CachedFileFolders, RefPath
from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef
from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy
from totodev_pub.cached_file_folders_support.primitive_schema_resolver import PrimitiveSchemaResolver
from totodev_pub.cached_file_folders_support.schema_resolver_protocol import SchemaResolver, SchemaKey
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin


@dataclass
class _ModelBinding:
    name: str
    class_name: Optional[str]
    extra: Dict[str, Any]


class SimpleCacheORM:
    """
    High-level convenience layer for resolving, loading, and mutating cached records.

    Responsibilities:
    - Uses a SchemaResolver (e.g., PrimitiveSchemaResolver) for translating semantic params
      into concrete addresses.
    - Provides model-aware helpers for get/list/upsert/delete operations.
    - Coerces common inputs (dicts, Pydantic BaseModels) into SerializableDataProxy.

    Resolver-specific behavior:
    - With PrimitiveSchemaResolver: No schema registration needed. Use classes directly as
      `schema_key` (e.g., `schema_key=User`). Only `slug` parameter is required.
    - With custom resolvers: Schema registration required via `register_models()` or
      constructor `schema=` parameter. Use string keys or registered class names.

    Loading behavior:
    - When a class is passed (e.g., UserRecord), its `load(path)` classmethod is used
      automatically. If the class is a FileMappedPydanticMixin, this integrates naturally
      with its load API.
    - When using string keys or callable loaders, supply `loader=` and optionally
      `loader_accepts_ref=True` if the loader expects a CachedFileRef instead of a
      filesystem path.

    Notes:
    - This utility focuses on simple, file-backed persistence and deliberately avoids
      database features like secondary indexes or transactions.
    - Parameter requirements vary by resolver: PrimitiveSchemaResolver only needs `slug`,
      while custom resolvers may require multiple parameters based on their schema patterns.
    """

    def __init__(
        self,
        cache: CachedFileFolders,
        *,
        schema: Optional[Dict[SchemaKey, Dict[str, Any]]] = None,
        resolver: Optional[SchemaResolver] = None,
    ) -> None:
        self.cache: CachedFileFolders = cache
        self.resolver: SchemaResolver = resolver or PrimitiveSchemaResolver(cache)
        self._bindings_by_name: dict[str, _ModelBinding] = {}
        self._bindings_by_class: dict[str, _ModelBinding] = {}
        if schema:
            self.register_models(schema)

    def register_models(self, schema: Dict[SchemaKey, Dict[str, Any]]) -> None:
        """
        Register model bindings and ensure the resolver knows about associated patterns.

        - Keys can be a `str` (schema name) or a class/type. Class keys enable automatic
          lookups by class when calling `get`, `objects`, or `upsert`.
        - Extra fields beyond resolver-specific keys are recorded for future use but
          ignored by the resolver itself.
        """
        self.resolver.register_map(schema)

        for key, options in schema.items():
            binding = self._build_binding(key, options)
            if binding.name:
                self._bindings_by_name[binding.name] = binding
            if binding.class_name:
                self._bindings_by_class[binding.class_name] = binding

    def resolve_path(self, name_or_type: SchemaKey, **params: Any) -> tuple:
        """
        Return raw `(grouping_key, ref_path)` for a model and parameters.

        Raises:
        - KeyError if required parameters are missing and no defaults are available.
        """
        return self.resolver.resolve_path(name_or_type, **params)

    def iter_refs(
        self,
        name_or_type: SchemaKey,
        *,
        reverse: bool = False,
        **filters: Any,
    ) -> Iterator[CachedFileRef]:
        """
        Iterate raw CachedFileRef objects for a model.

        - `reverse=True` yields refs in reverse order per resolver's ordering policy.
        - `filters` are forwarded to the resolver (e.g., partial grouping params).
        """
        return self.resolver.iter_refs(name_or_type, reverse=reverse, **filters)

    def objects(
        self,
        loader: Union[type, Callable[..., Any]],
        *,
        schema_key: Optional[str] = None,
        loader_accepts_ref: bool = False,
        reverse: bool = False,
        **filters: Any,
    ) -> Iterator[Any]:
        """
        Iterate hydrated objects for a model.

        - If `loader` is a class, it must define a `load(path)` classmethod. For
          `FileMappedPydanticMixin` subclasses this is used automatically.
        - If `loader` is a function, you must also provide `schema_key`. Set
          `loader_accepts_ref=True` if the loader expects a `CachedFileRef` instead of a path.
        """
        binding, pattern_name = self._binding_for_loader(loader, schema_key)
        loader_func, accepts_ref = self._resolve_loader_callable(
            loader,
            binding,
            loader_accepts_ref=loader_accepts_ref,
        )
        for ref in self.resolver.iter_refs(pattern_name, reverse=reverse, **filters):
            yield loader_func(ref if accepts_ref else ref.file_path)

    def get(
        self,
        loader: Union[type, Callable[..., Any]],
        *,
        schema_key: Optional[str] = None,
        loader_accepts_ref: bool = False,
        **params: Any,
    ) -> Optional[Any]:
        """
        Load a single record by exact parameters.

        Returns:
        - The hydrated object if found; otherwise `None`.

        See `objects()` for loader resolution rules.
        """
        binding, pattern_name = self._binding_for_loader(loader, schema_key)
        loader_func, accepts_ref = self._resolve_loader_callable(
            loader,
            binding,
            loader_accepts_ref=loader_accepts_ref,
        )
        ref = self.resolver.resolve_ref(pattern_name, **params)
        if ref is None:
            return None
        return loader_func(ref if accepts_ref else ref.file_path)

    async def upsert(
        self,
        source: Union[BaseModel, Dict[str, Any], list],
        *,
        schema_key: Optional[str] = None,
        infer_params: bool = True,
        force: bool = False,
        change_receiver: Optional[Callable[[Any, Optional[SerializableDataProxy]], None]] = None,
        **params: Any,
    ):
        """
        Upsert a record for the given model.

        `source` must be a structured data object (BaseModel, dict, or list) that can be
        serialized via SerializableDataProxy. For raw files or custom proxies, use the
        lower-level cache APIs directly.

        If `infer_params=True` (default), missing required parameters will be automatically
        extracted from attributes/properties of the source object (e.g., source.user_slug
        for a BaseModel or source["user_slug"] for a dict).

        Parameters:
        - schema_key: Explicit pattern name. If omitted, and `source` is a BaseModel,
          its class name is used to locate a registered binding.
        - infer_params: When True, fills missing required params from `source` fields.
        - force: When True, allows overwriting existing files.
        - change_receiver: Optional callable invoked with (source, proxy) after write.

        Raises:
        - ValueError if required parameters are still missing after inference/defaults.
        """
        pattern_name = self._determine_schema_key_from_source(source, schema_key)
        
        if infer_params:
            required_params = set(self.resolver.required_params(pattern_name))
            missing_params = required_params - set(params.keys())
            if missing_params:
                inferred = self._infer_params_from_source(source, missing_params)
                params = {**inferred, **params}
        
        # Try to resolve path - this will raise KeyError if params are still missing after defaults
        try:
            grouping_key, ref_path = self.resolver.resolve_path(pattern_name, **params)
        except KeyError as e:
            missing_key = str(e).strip("'\"")
            raise ValueError(
                f"Missing required parameter '{missing_key}' for schema '{pattern_name}'. "
                f"This could not be inferred from the source object and no default is available. "
                f"Please provide it explicitly in the params."
            ) from e
        proxy = SerializableDataProxy(source, ref_path)
        return await self.cache.upsert_file(
            proxy,
            grouping_key=grouping_key,
            force=force,
            change_receiver=change_receiver,
        )

    async def delete(
        self,
        schema_key: SchemaKey,
        **params: Any,
    ):
        """
        Delete a record by model + parameters.

        Returns True if a file was deleted, False if nothing matched.
        """
        # Handle type directly (for PrimitiveSchemaResolver)
        if isinstance(schema_key, type):
            pattern_name = schema_key
        else:
            pattern_name = self._resolve_pattern_name(schema_key)
        grouping_key, ref_path = self.resolver.resolve_path(pattern_name, **params)
        return await self.cache.delete_file(ref_path, grouping_key=grouping_key)

    def exists(self, schema_key: SchemaKey, **params: Any) -> bool:
        """
        Return True if a matching record exists.
        """
        # Handle type directly (for PrimitiveSchemaResolver)
        if isinstance(schema_key, type):
            pattern_name = schema_key
        else:
            pattern_name = self._resolve_pattern_name(schema_key)
        return self.resolver.resolve_ref(pattern_name, **params) is not None

    def _build_binding(self, key: SchemaKey, options: Dict[str, Any]) -> _ModelBinding:
        name = options.get("name")
        class_name = options.get("class_name")

        if isinstance(key, str):
            name = name or key
        elif isinstance(key, type):
            name = name or key.__name__
            class_name = class_name or key.__name__
        else:
            raise TypeError(f"Schema key must be str or type, got {key!r}")

        extra = {
            k: v
            for k, v in options.items()
            if k
            not in {
                "name",
                "class_name",
                "grouping_template",  # legacy
                "grouping_key_template",
                "ref_path_template",
                "defaults",
            }
        }

        return _ModelBinding(
            name=name or "",
            class_name=class_name,
            extra=extra,
        )

    def _binding_for_loader(
        self,
        loader: Union[type, Callable[..., Any]],
        schema_key: Optional[SchemaKey],
    ) -> tuple[_ModelBinding, SchemaKey]:
        # For PrimitiveSchemaResolver, can use class types directly
        from totodev_pub.cached_file_folders_support.primitive_schema_resolver import PrimitiveSchemaResolver
        if isinstance(self.resolver, PrimitiveSchemaResolver):
            if schema_key is not None:
                if isinstance(schema_key, type):
                    return _ModelBinding(name="", class_name=schema_key.__name__, extra={}), schema_key
                # String schema_key not supported by PrimitiveSchemaResolver
                raise ValueError(f"PrimitiveSchemaResolver only works with class types, not string keys like '{schema_key}'")
            
            if isinstance(loader, type):
                return _ModelBinding(name="", class_name=loader.__name__, extra={}), loader
            
            raise ValueError(
                "schema_key must be provided when loader is a callable that is not a class."
            )
        
        # Original logic for other resolvers
        if schema_key is not None:
            if isinstance(schema_key, type):
                schema_key = schema_key.__name__
            binding = self._bindings_by_name.get(schema_key)
            if binding is None:
                raise ValueError(f"No schema registered under name '{schema_key}'")
            return binding, binding.name

        if isinstance(loader, type):
            class_name = loader.__name__
            binding = self._bindings_by_class.get(class_name) or self._bindings_by_name.get(class_name)
            if binding is None:
                raise ValueError(
                    f"No schema registered for class '{class_name}'. "
                    "Pass schema_key=... to target a specific pattern."
                )
            if binding.class_name and binding.class_name != class_name:
                raise ValueError(
                    f"Schema '{binding.name}' is bound to class '{binding.class_name}', "
                    f"but loader '{class_name}' was provided."
                )
            return binding, binding.name

        raise ValueError(
            "schema_key must be provided when loader is a callable that is not a class."
        )

    def _resolve_pattern_name(self, name: str) -> str:
        binding = self._bindings_by_name.get(name)
        if binding is None:
            raise ValueError(f"No schema registered under name '{name}'")
        return binding.name

    def _determine_schema_key_from_source(
        self,
        source: Union[BaseModel, Dict[str, Any], list],
        schema_key: Optional[SchemaKey],
    ) -> SchemaKey:
        # If schema_key is a type (class), return it directly (for PrimitiveSchemaResolver)
        if schema_key is not None and isinstance(schema_key, type):
            return schema_key
        
        if schema_key is not None:
            return self._resolve_pattern_name(schema_key)

        inferred_name: Optional[str] = None
        if isinstance(source, BaseModel):
            # For PrimitiveSchemaResolver, can use class directly
            from totodev_pub.cached_file_folders_support.primitive_schema_resolver import PrimitiveSchemaResolver
            if isinstance(self.resolver, PrimitiveSchemaResolver):
                return type(source)
            inferred_name = type(source).__name__
        elif isinstance(source, (dict, list)):
            inferred_name = None

        if inferred_name:
            binding = self._bindings_by_class.get(inferred_name) or self._bindings_by_name.get(inferred_name)
            if binding:
                return binding.name

        raise ValueError(
            "Unable to determine schema key for upsert. Provide schema_key=... explicitly."
        )

    def _infer_params_from_source(
        self,
        source: Union[BaseModel, Dict[str, Any], list],
        missing_params: set[str],
    ) -> Dict[str, Any]:
        """
        Attempt to extract missing parameter values from the source object.

        For BaseModel objects, checks attributes/properties (e.g., source.user_slug).
        For dict objects, checks dictionary keys (e.g., source["user_slug"]).
        For list objects, cannot extract parameters, so returns empty dict.
        """
        inferred = {}
        
        if isinstance(source, BaseModel):
            for param_name in missing_params:
                value = getattr(source, param_name, None)
                if value is not None:
                    inferred[param_name] = value
        elif isinstance(source, dict):
            for param_name in missing_params:
                if param_name in source:
                    inferred[param_name] = source[param_name]
        # For list objects, we can't extract individual params, so return empty dict
        
        return inferred

    def _resolve_loader_callable(
        self,
        loader: Union[type, Callable[..., Any]],
        binding: _ModelBinding,
        *,
        loader_accepts_ref: bool,
    ) -> tuple[Callable, bool]:
        if isinstance(loader, type):
            # Prefer explicit support for FileMappedPydanticMixin subclasses
            if issubclass(loader, FileMappedPydanticMixin):
                candidate = getattr(loader, "load", None)
                if not callable(candidate):
                    raise ValueError(
                        f"Class '{loader.__name__}' derives from FileMappedPydanticMixin "
                        f"but does not define a callable load(path) method."
                    )
                return candidate, False
            candidate = getattr(loader, "load", None)
            if not callable(candidate):
                raise ValueError(
                    f"Class '{loader.__name__}' must define a callable load(path) method "
                    f"to be used with schema '{binding.name}'."
                )
            return candidate, False

        # Callable loader case
        return loader, loader_accepts_ref

