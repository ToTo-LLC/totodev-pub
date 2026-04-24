# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from .sync_types import ChangeType
from .change_notice import ChangeNotice
from .cache_operations_protocol import CacheOperations
from .resync_sweep import AsyncSyncSession
from .async_operation_handlers import AsyncUpsertOperation, AsyncDeleteOperation
from .resync_orchestrator import ResyncOrchestrator, FileSnapshot
# Avoid eager import of SimpleCacheORM to prevent circular imports

__all__ = [
    "ChangeType",
    "ChangeNotice",
    "CacheOperations",
    "AsyncSyncSession",
    "AsyncUpsertOperation",
    "AsyncDeleteOperation",
    "ResyncOrchestrator",
    "FileSnapshot",
    "SimpleCacheORM",
    "PrimitiveSchemaResolver",
    "SchemaResolver",
    "SchemaKey",
    "SlugProvider",
]

def __getattr__(name: str):
    # Lazy import to avoid circular import during CachedFileFolders module initialization
    if name == "SimpleCacheORM":
        from .simple_cache_orm import SimpleCacheORM  # type: ignore
        return SimpleCacheORM
    if name == "PrimitiveSchemaResolver":
        from .primitive_schema_resolver import PrimitiveSchemaResolver  # type: ignore
        return PrimitiveSchemaResolver
    if name == "SlugProvider":
        from .primitive_schema_protocol import SlugProvider  # type: ignore
        return SlugProvider
    if name == "SchemaResolver":
        from .schema_resolver_protocol import SchemaResolver  # type: ignore
        return SchemaResolver
    if name == "SchemaKey":
        from .schema_resolver_protocol import SchemaKey  # type: ignore
        return SchemaKey
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


