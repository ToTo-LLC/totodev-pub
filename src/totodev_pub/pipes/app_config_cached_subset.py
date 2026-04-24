# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""AppConfigCachedSubset - A cached subset of application configuration data.

This module provides the AppConfigCachedSubset class, which represents a point-in-time
snapshot of configuration data with automatic caching, change detection, and file persistence.
The class supports JSON and YAML formats and provides methods for regenerating subsets
of configuration data while tracking timestamps for efficient caching strategies.

Why Use This Class?
==================

AppConfigCachedSubset is particularly valuable when you need to:

1. **Cache Expensive Configuration Lookups**: When your application loads configuration
   from multiple sources (files, databases, APIs) and you want to avoid repeated lookups
   for unchanged data.

2. **Selective Configuration Updates**: When you only need to cache specific keys from
   a larger configuration object, rather than the entire configuration.

3. **Performance Optimization**: When configuration parsing is expensive but most
   configuration values change infrequently.

Real-World Business Examples:
=============================

**E-commerce Application - API Configuration Caching**
```python
# Cache API credentials and endpoints that rarely change
with AppConfigCachedSubset.open("api_config_cache.json") as cache:
    # Only update if payment processor config has changed
    if cache.regen(full_config, ["stripe_api_key", "stripe_webhook_secret", "payment_endpoint"]):
        cache.save()
    
    # Use cached values for high-frequency payment operations
    api_config = cache.proxy_dict()
    stripe_client = StripeClient(api_config["stripe_api_key"])
```

**CRM System - Database Connection Pooling**
```python
# Cache database connection settings separately from other config
with AppConfigCachedSubset.open("db_config_cache.yaml") as cache:
    # Only refresh if database settings changed
    if cache.regen(app_config, ["db_host", "db_port", "db_name", "connection_pool_size"]):
        cache.save()
        # Recreate connection pool only when needed
    
    # Use cached database config for frequent queries
    db_config = cache.proxy_dict()
    pool = ConnectionPool(**db_config)
```

**Analytics Dashboard - External Service Configuration**
```python
# Cache third-party service configurations that change rarely
with AppConfigCachedSubset.open("external_services_cache.json") as cache:
    # Update only specific service configs that might have changed
    if cache.regen(services_config, ["google_analytics_id", "mixpanel_token", "slack_webhook"]):
        cache.save()
    
    # Use cached config for dashboard initialization
    services = cache.proxy_dict()
    analytics = GoogleAnalytics(services["google_analytics_id"])
```

**Microservice Architecture - Service Discovery**
```python
# Cache service endpoint configurations across multiple services
with AppConfigCachedSubset.open("service_endpoints_cache.yaml") as cache:
    # Only update if service discovery config changed
    if cache.regen(service_registry, ["user_service_url", "payment_service_url", "notification_service_url"]):
        cache.save()
    
    # Use cached endpoints for inter-service communication
    endpoints = cache.proxy_dict()
    user_client = UserServiceClient(endpoints["user_service_url"])
```

Key Benefits:
- **Reduced I/O**: Avoid repeated file parsing or database queries
- **Selective Updates**: Only process changed configuration keys
- **Automatic Cleanup**: Remove stale configuration entries
- **Thread Safety**: Built-in file locking for concurrent access
- **Format Flexibility**: Support for JSON and YAML formats
"""

from datetime import datetime
from typing import Any, Optional, TypeVar, Dict, Union, Mapping, List
from types import MappingProxyType
from pathlib import Path
from pydantic import BaseModel, Field
import time
import json
import os
import yaml

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin, _DEFAULT_ENCODING

T = TypeVar('T', bound=BaseModel)

class AppConfigCachedSubset(BaseModel, FileMappedPydanticMixin):
    """
    A cached subset of application configuration data with automatic persistence.
    
    This class manages a point-in-time snapshot of configuration data with built-in
    caching, change detection, and file persistence. It tracks when each key was
    last added/updated and provides methods for selective regeneration and cleanup
    of stale data.
    
    The class supports both JSON and YAML file formats and automatically handles
    file locking and dirty state tracking through the FileMappedPydanticMixin.
    
    Attributes:
        last_regen: Timestamp of the last regeneration operation
        data: Dictionary containing the cached configuration values
    
    Example Usage:
        ```python
        # Load existing cache or create new
        with AppConfigCachedSubset.open("config_cache.json") as cache:
            # Update specific keys from source config
            if cache.regen(source_config, ["database_url", "api_key"]):
                cache.save()  # Only saves if changes were made
            
            # Clean up old entries
            cache.def_purge_old_adds(3600)  # Remove entries older than 1 hour
            
            # Get read-only access to cached data
            config_view = cache.proxy_dict()
        ```
    """

    last_regen: datetime = Field(
        default_factory=datetime.now,
        description="Indicates the last time the file was regenerated. During testing you can set this in the future to avoid refresh or change of file"
    )

    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Represents the point in time values of the config"
    )

    def model_post_init(self, __context: Any) -> None:
        """Initialize non-persisted instance variables after model initialization.
        
        Sets up the internal _last_added dictionary to track when each key
        was last added or updated in the cache.
        """
        super().model_post_init(__context)
        # Initialize the non-persisted last_added tracking dict
        self._last_added: dict[str, datetime] = {}

    @classmethod
    def _get_format_handlers(cls, file_format: str) -> dict:
        # Get the appropriate format handlers with consistent serialization
        if file_format == 'json':
            return {
                'dump': lambda data: (
                    json.dumps(data, indent=2, default=str)
                ),
                'load': lambda content: (
                    json.loads(content) if content.strip() else {
                        "last_regen": datetime.now().isoformat(),
                        "data": {}
                    }
                ),
                'binary': False,
                'extensions': ['.json']
            }
        elif file_format == 'yaml':
            return {
                'dump': lambda data: yaml.safe_dump(data, sort_keys=False, indent=4),
                'load': lambda content: yaml.safe_load(content) or {},
                'binary': False,
                'extensions': ['.yaml']
            }
        else:
            raise ValueError(f"Unsupported format: {file_format}")

    def _write_to_file(self, data: Dict[str, Any], file_format: str, file_path: Optional[str] = None) -> None:
        # Write data to the file in the specified format
        target_path = file_path or self._file_path
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

        # Only serialize the necessary fields
        serializable_data = {
            "last_regen": self.last_regen.isoformat(),
            "data": self.data
        }
        
        handlers = self.__class__._get_format_handlers(file_format)
        content = handlers['dump'](serializable_data)
        
        if handlers['binary']:
            with open(target_path, 'wb') as f:
                f.write(content.encode(_DEFAULT_ENCODING))
        else:
            with open(target_path, 'w', encoding=_DEFAULT_ENCODING) as f:
                f.write(content)

    def def_purge_old_adds(self, age_secs: float) -> bool:
        """
        Remove cached entries older than the specified age threshold.
        
        This method removes both the data entries and their corresponding timestamp
        tracking from the cache, effectively cleaning up stale configuration data.
        
        Args:
            age_secs: Age threshold in seconds. Entries older than this will be removed.
            
        Returns:
            True if any entries were removed, False if no entries were old enough.
        """
        now = datetime.now()
        keys_to_remove = self._get_stale_keys(age_secs)
        
        if not keys_to_remove:
            return False
            
        for key in keys_to_remove:
            del self.data[key]
            del self._last_added[key]
            
        return True

    def regen(self, mappable: dict[str, Any], keys: list[str]) -> bool:
        """
        Regenerate cached values from a source configuration dictionary.
        
        Updates the cache with values from the provided source dictionary for the
        specified keys. Only updates keys that have actually changed, and tracks
        the timestamp when each key was last updated.
        
        Args:
            mappable: Source configuration dictionary to pull values from
            keys: List of keys to update or add to the cache
            
        Returns:
            True if any values were changed or added, False if no changes occurred
        """
        now = datetime.now()
        changed = False
        
        for key in keys:
            if key not in mappable:
                continue
                
            new_value = mappable[key]
            if key not in self.data or self.data[key] != new_value:
                self.data[key] = new_value
                self._last_added[key] = now
                changed = True
        
        if changed:
            self.last_regen = now
            try:
                self.mark_dirty()  # Use the new mark_dirty() method
            except RuntimeError:
                # If we're not in a context manager, we need to set _has_unsaved_changes directly
                # This maintains backward compatibility with code not using context managers
                self._has_unsaved_changes = True
            
        return changed

    def clear_cache(self) -> None:
        """
        Clear all cached data and reset timestamps.
        
        Removes all cached configuration data and timestamp tracking,
        effectively resetting the cache to an empty state. Updates the
        last_regen timestamp to the current time.
        """
        self.data.clear()
        self._last_added.clear()
        self.last_regen = datetime.now()

    def _get_last_add_time(self, key: str) -> Optional[datetime]:
        # Get the last time a key was added or updated
        return self._last_added.get(key)

    def _is_key_stale(self, key: str, age_secs: float) -> bool:
        # Check if a key's data is older than the specified age
        last_add = self._get_last_add_time(key)
        if not last_add:
            return False
            
        return (datetime.now() - last_add).total_seconds() > age_secs

    def _get_stale_keys(self, age_secs: float) -> list[str]:
        # Get list of keys older than specified age
        return [
            key for key in self.data.keys()
            if self._is_key_stale(key, age_secs)
        ]
        
    def save(self, file_path: Optional[Union[str, Path]] = None, retain_lock: bool = False) -> None:
        """
        Save the cached configuration to file and release lock by default.
        
        Saves the current state of the cache to the specified file path (or the
        original path if not specified). By default, releases any file lock after
        saving to prevent lock-related issues in tests and applications.
        
        Args:
            file_path: Optional path to save to. If None, uses the path the model
                      was loaded from or the current file path.
            retain_lock: If True, keep the file lock after saving. Defaults to False.
        """
        super().save(file_path=file_path, retain_lock=retain_lock)
        if not retain_lock:
            self.release_lock()

    def proxy_dict(self) -> Mapping[str, Any]:
        """
        Return a read-only view of the cached configuration data.
        
        Provides a read-only dictionary-like interface to the cached configuration
        data. The returned object cannot be modified and will raise TypeError on
        any modification attempts.
        
        Returns:
            A read-only mapping proxy of the current cached configuration data
        """
        return MappingProxyType(self.data)

    def _dump_data(self) -> str:
        # Dump the data to a string in the current format
        data = self.data
        if self._format == 'json':
            return json.dumps(data, indent=2)
        elif self._format == 'yaml':
            return yaml.safe_dump(data, sort_keys=False, indent=4)
        else:
            raise ValueError(f"Unsupported format: {self._format}")

    def _load_content(self, content: str) -> None:
        # Load content from a string in the current format
        if self._format == 'json':
            self.data = json.loads(content) if content and content.strip() else {}
        elif self._format == 'yaml':
            self.data = yaml.safe_load(content) or {}
        else:
            raise ValueError(f"Unsupported format: {self._format}")

    def model_dump(self, **kwargs) -> Dict[str, Any]:
        """Override model_dump to return only the data field.
        
        Returns a dictionary containing only the cached configuration data,
        omitting metadata like last_regen timestamp.
        
        Returns:
            Dictionary containing the cached configuration data
        """
        serializable_data = self.data
        return {"data": serializable_data}

    def _regen_from_source(self, keys: Optional[List[str]] = None) -> None:
        # Regenerate values from source - placeholder implementation
        if keys is None:
            keys = list(self.data.keys())

        for key in keys:
            if key not in self.data:
                continue

            new_value = self._get_value_from_source(key)
            if new_value is not None:
                self.data[key] = new_value
