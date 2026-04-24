# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
File-mapped Pydantic models with automatic persistence and concurrency control.

This module provides the FileMappedPydanticMixin, a powerful mixin class that adds
file persistence capabilities to Pydantic models. It enables automatic loading,
saving, and change tracking for configuration files, data models, and other
structured data that needs to be persisted to disk.

**Primary Use Case: Human-Readable Data Files as Database Alternative**

The main purpose of this mixin is to enable simple, human-readable data files to serve
as complex/compound data stores in low-volume scenarios, eliminating the need for a
traditional database. It allows for seamless serialization/deserialization of Python
dictionaries composed of native data types, including nested Pydantic objects and classes.

This approach is ideal when you need:
- **Human-editable data**: Files that developers and users can read and modify directly
- **Complex nested structures**: Dictionaries containing lists, other dicts, and Pydantic models
- **Low-volume data**: Hundreds to thousands of records, not millions
- **No database overhead**: Avoid database setup, migrations, and maintenance
- **Version control friendly**: Data files can be tracked in git alongside code
- **Cross-platform compatibility**: Works anywhere Python runs, no external dependencies

Key Features:
- **Automatic file persistence**: Load from and save to files with a simple API
- **Multi-format support**: JSON, YAML, TOML, and NDJSON/JSONL formats
- **Concurrency control**: File locking prevents data corruption from concurrent access
- **Change tracking**: Automatic detection of modifications with revert capabilities
- **Context manager support**: Automatic saving on exit with proper cleanup
- **Format flexibility**: Override file format regardless of extension
- **Streaming support**: Memory-efficient processing of large NDJSON files
- **Stability checking**: Wait for files to stabilize before loading

Why Use This Module:
- **Database alternative for low-volume data**: Replace SQLite/PostgreSQL with human-readable files
  for datasets that don't require complex queries or high performance
- **Complex nested data structures**: Store Python dictionaries with nested objects, lists, and
  Pydantic models without flattening or normalization
- **Human-editable data stores**: Allow developers and users to directly edit data files
  without database tools or migrations
- **Configuration management**: Perfect for application configs that need to persist
  between runs and be human-editable
- **Data caching**: Store computed results that can be reused across sessions
- **State persistence**: Maintain application state across restarts
- **Data pipelines**: Handle intermediate results in ETL processes
- **Multi-process safety**: File locking ensures data integrity in concurrent environments

Core Concepts:
- **Multiple inheritance requirement**: This mixin MUST be used with Pydantic BaseModel
- **Critical inheritance order**: BaseModel must come BEFORE FileMappedPydanticMixin in the class definition
- **File mapping**: Each model instance is associated with a specific file path
- **Locking**: Exclusive locks prevent concurrent modifications to the same file
- **Change detection**: Tracks modifications to determine when saving is needed
- **Format inference**: Automatically determines file format from extension
- **Fallback handling**: Graceful handling of missing or corrupted files

**IMPORTANT: Multiple Inheritance Requirements**

This mixin is designed for multiple inheritance and has strict requirements:

```python
# ✅ CORRECT - BaseModel comes FIRST
class MyConfig(BaseModel, FileMappedPydanticMixin):
    field1: str = "default"

# ❌ WRONG - This will raise NotImplementedError
class MyConfig(FileMappedPydanticMixin, BaseModel):  # Don't do this!
    field1: str = "default"
```

**Why the order matters:**
- The mixin overrides `__init__` to prevent direct instantiation
- It relies on Pydantic's `model_post_init` for proper initialization
- Wrong inheritance order breaks the initialization chain
- The mixin must be the first class in the inheritance list

Supported File Formats:
- **JSON** (.json): Standard JSON format with pretty-printing
- **YAML** (.yaml, .yml): Human-readable YAML format
- **TOML** (.toml): Configuration-friendly TOML format
- **NDJSON** (.ndjson, .jsonl): Newline-delimited JSON for streaming

Usage Patterns:

*Note: All examples below are verified in the pytest test cases for this class.*

1. **Complex Nested Data Structure (Primary Use Case)**:
   ```python
   # CORRECT: BaseModel comes BEFORE FileMappedPydanticMixin
   class UserProfile(BaseModel, FileMappedPydanticMixin):
       name: str
       email: str
       preferences: Dict[str, Any] = {}
       metadata: List[Dict[str, str]] = []
   
   class ApplicationData(BaseModel, FileMappedPydanticMixin):
       users: List[UserProfile] = []
       settings: Dict[str, Any] = {}
       cache: Dict[str, List[Dict]] = {}
       version: str = "1.0.0"
   
   # Store complex nested data in human-readable YAML
   app_data = ApplicationData.open("app_data.yaml")
   app_data.users.append(UserProfile(name="John", email="john@example.com"))
   app_data.settings["theme"] = "dark"
   app_data.cache["recent_searches"] = [{"query": "python", "timestamp": "2024-01-01"}]
   app_data.save()  # Saves as readable YAML file
   ```

2. **Basic Configuration Management**:
   ```python
   # CORRECT: BaseModel comes BEFORE FileMappedPydanticMixin
   class AppConfig(BaseModel, FileMappedPydanticMixin):
       api_key: str = "default_key"
       debug_mode: bool = False
   
   # WRONG: This will raise NotImplementedError
   # class AppConfig(FileMappedPydanticMixin, BaseModel):  # Don't do this!
   
   # Load or create config
   config = AppConfig.open("config.yaml")
   config.debug_mode = True
   config.save()
   ```

3. **Context Manager for Automatic Saving**:
   ```python
   # Remember: BaseModel must come first!
   class AppConfig(BaseModel, FileMappedPydanticMixin):
       api_key: str = "default_key"
   
   with AppConfig.open("config.yaml") as config:
       config.api_key = "new_key"
       # Automatically saves on exit if changes were made
   ```

4. **Change Detection and Revert**:
   ```python
   # CORRECT: BaseModel comes BEFORE FileMappedPydanticMixin
   class AppConfig(BaseModel, FileMappedPydanticMixin):
       api_key: str = "default_key"
       debug_mode: bool = False
   
   config = AppConfig.open("config.yaml")
   config.debug_mode = True
   if config.is_modified():
       config.save()  # Only save if changes were made
   
   # Revert unsaved changes
   config.debug_mode = False
   config.revert()  # Back to original state
   ```

5. **Format Override**:
   ```python
   # Use JSON format for a .txt file
   # Remember: BaseModel must come first in inheritance!
   class AppConfig(BaseModel, FileMappedPydanticMixin):
       api_key: str = "default_key"
   
   config = AppConfig.open("config.txt", format_override="json")
   config.save()  # Saves as JSON despite .txt extension
   ```

6. **NDJSON Streaming for Large Datasets**:
   ```python
   # CRITICAL: BaseModel must come BEFORE FileMappedPydanticMixin
   class MyModel(BaseModel, FileMappedPydanticMixin):
       name: str
       value: int
   
   # Stream process large datasets
   for record in MyModel.stream_read("data.ndjson"):
       process_record(record)
   
   # Append new records
   MyModel.append_records("data.ndjson", [new_record1, new_record2])
   ```

7. **User Database Replacement (YAML as Database)**:
   ```python
   # CORRECT: BaseModel comes BEFORE FileMappedPydanticMixin
   from typing import List, Optional
   from enum import Enum
   
   class UserRole(str, Enum):
       ADMIN = "admin"
       USER = "user"
       GUEST = "guest"
   
   class User(BaseModel, FileMappedPydanticMixin):
       username: str
       email: str
       password_hash: str
       role: UserRole = UserRole.USER
       is_active: bool = True
       last_login: Optional[str] = None
   
   class UserDatabase(BaseModel, FileMappedPydanticMixin):
       users: List[User] = []
       version: str = "1.0.0"
   
   # Initialize database with sample users
   def create_sample_database():
       db = UserDatabase.open("users.yaml", fallback_value={
           "users": [
               {
                   "username": "admin",
                   "email": "admin@example.com", 
                   "password_hash": "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj4J/9KXvK2a",
                   "role": "admin"
               },
               {
                   "username": "alice",
                   "email": "alice@example.com",
                   "password_hash": "$2b$12$8K9mN2pQ5rS7tU1vW3xY6zA4bC8dE0fG2hI5jK7lM9nO1pQ3rS5tU7vW9xY", 
                   "role": "user"
               },
               {
                   "username": "bob",
                   "email": "bob@example.com",
                   "password_hash": "$2b$12$3F7gH9iJ2kL4mN6oP8qR0sT2uV4wX6yZ8aB1cD3eF5gH7iJ9kL1mN3oP5qR",
                   "role": "user"
               }
           ]
       })
       return db
   
   # Use the database
   db = create_sample_database()
   
   # Find user by username
   admin_user = next((u for u in db.users if u.username == "admin"), None)
   if admin_user and admin_user.role == UserRole.ADMIN:
       print(f"Admin user found: {admin_user.email}")
   
   # Add new user
   new_user = User(
       username="charlie",
       email="charlie@example.com", 
       password_hash="$2b$12$9M2nP4qR6sT8uV0wX2yZ4aB6cD8eF0gH2iJ4kL6mN8oP0qR2sT4uV6wX8yZ",
       role=UserRole.USER
   )
   db.users.append(new_user)
   db.save()  # Persists to human-readable YAML file
   
   # The resulting users.yaml file is human-editable:
   # users:
   # - username: admin
   #   email: admin@example.com
   #   password_hash: $2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj4J/9KXvK2a
   #   role: admin
   #   is_active: true
   #   last_login: null
   # - username: alice
   #   email: alice@example.com
   #   password_hash: $2b$12$8K9mN2pQ5rS7tU1vW3xY6zA4bC8dE0fG2hI5jK7lM9nO1pQ3rS5tU7vW9xY
   #   role: user
   #   is_active: true
   #   last_login: null
   # ... etc
   ```

8. **File Monitoring and Reloading**:
   ```python
   # CORRECT: BaseModel comes BEFORE FileMappedPydanticMixin
   class AppConfig(BaseModel, FileMappedPydanticMixin):
       api_key: str = "default_key"
       debug_mode: bool = False
   
   config = AppConfig.open("config.yaml")
   
   # Check if file was modified externally
   if config.file_was_modified():
       config.reload_from_file()  # Reload from disk
   ```

Thread Safety and Concurrency:
- File locking prevents concurrent access to the same file
- Multiple processes can safely access different files
- Lock timeouts prevent deadlocks from crashed processes
- Orphaned locks are automatically cleaned up

Error Handling:
- Graceful fallback to default values for missing files
- Automatic retry with exponential backoff for lock acquisition
- Validation errors are preserved and re-raised
- File permission errors are caught and reported
- **Inheritance errors**: Wrong inheritance order raises NotImplementedError with clear message

Performance Considerations:
- File locking adds overhead but ensures data integrity
- Change detection uses deep comparison (can be expensive for large models)
- NDJSON streaming is memory-efficient for large datasets
- Format override avoids re-parsing file extensions

This module is particularly well-suited for:
- **Database alternatives**: Replace SQLite/PostgreSQL for low-volume, human-readable data stores
- **Complex nested data**: Store Python dictionaries with nested objects, lists, and Pydantic models
- **Application configuration files**: Human-editable configs that persist between runs
- **Caching computed results**: Store intermediate results that can be reused across sessions
- **Storing user preferences**: Complex preference structures in readable formats
- **Managing pipeline state**: Track state in data processing workflows
- **Handling temporary data**: Data that needs persistence but doesn't require database features
- **Development and testing**: Easy-to-inspect data files for debugging and testing
"""

import os
import time
import portalocker
import json
import yaml
import logging
from io import TextIOWrapper
from portalocker import LockException
from portalocker.constants import LOCK_EX, LOCK_NB
from pydantic import BaseModel, Field, ValidationError
from typing import Optional, Callable, Self, Dict, Any, Tuple, Type, Union, List, Generator
from copy import deepcopy
from pathlib import Path
import random

# Setup logger
logger: logging.Logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)  # Set to DEBUG for development

# File operations
_DEFAULT_ENCODING = 'utf-8'
_LOCK_FILE_SUFFIX = '.lock'
ORPHANED_LOCKFILE_SECONDS = 120  # 2 minutes in seconds

# Format-specific constants
_DEFAULT_JSON_INDENT = 2
_DEFAULT_YAML_SORT_KEYS = False
_DEFAULT_YAML_INDENT = 4

# Lock retry mechanism constants
_DEFAULT_WAIT_TO_RETRY_LOCK_SECONDS = 1.0  # Default wait time for retrying locks (in seconds)
_DEFAULT_MAX_RETRIES = 3  # Maximum number of retry attempts
_DEFAULT_RETRY_DELAY = 0.1  # 100ms between retries

def _import_toml():
    """
    Import TOML libraries with appropriate fallbacks.
    
    Returns:
        Tuple containing (loads_func, dumps_func)
    """
    try:
        import tomllib
        import tomli_w
        return tomllib.loads, tomli_w.dumps
    except ImportError:
        try:
            import toml
            return toml.loads, toml.dumps
        except ImportError:
            raise ImportError("No TOML library found. Install 'tomli' and 'tomli-w' (Python 3.11+) or 'toml'")

def _convert_enums_to_strings(data):
    """
    Recursively convert enum objects to their string values for serialization.
    
    Args:
        data: The data structure to process
        
    Returns:
        The data structure with enums converted to strings
    """
    from enum import Enum
    
    if isinstance(data, Enum):
        return data.value
    elif isinstance(data, dict):
        return {key: _convert_enums_to_strings(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [_convert_enums_to_strings(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(_convert_enums_to_strings(item) for item in data)
    else:
        return data

# Define supported file types and their handlers
_SUPPORTED_FILE_HANDLERS = {
    'json': {
        'load': lambda content: json.loads(content) if content and content.strip() else {},
        'dump': lambda data: json.dumps(data, indent=_DEFAULT_JSON_INDENT, default=str),
        'binary': False,
        'extensions': ['.json']
    },
    'yaml': {
        'load': lambda content: yaml.safe_load(content) or {},
        'dump': lambda data: yaml.safe_dump(data, sort_keys=_DEFAULT_YAML_SORT_KEYS, indent=_DEFAULT_YAML_INDENT),
        'binary': False,
        'extensions': ['.yaml', '.yml']
    },
    'toml': {
        'load': lambda content: _import_toml()[0](content) if content and content.strip() else {},
        'dump': lambda data: _import_toml()[1](data),
        'binary': False,
        'extensions': ['.toml']
    },
    'ndjson': {
        'load': lambda content: [json.loads(line) for line in content.splitlines() if line.strip()],
        'dump': lambda data: '\n'.join(json.dumps(item, default=str) for item in data),
        'binary': False,
        'extensions': ['.ndjson', '.jsonl']
    }
}

class FileMappedPydanticMixin:
    # Private attributes for file mapping functionality
    _file_path: Optional[str] = None
    _absolute_file_path: Optional[str] = None
    _lock_acquired: bool = False
    _has_unsaved_changes: bool = False
    _original_state: Optional[Dict[str, Any]] = None
    _file: Optional[TextIOWrapper] = None
    _file_stat: Optional[Tuple[int, float]] = None  # (size, mtime)
    _last_loaded_at: Optional[float] = None
    _on_file_modified_callback: Optional[Callable[[], None]] = None
    _in_context_manager: bool = False
    _format_override: Optional[str] = None

    """
    A mixin for Pydantic models that provides file mapping capabilities.
    
    This mixin should be used with classes that inherit from pydantic.BaseModel.
    It provides methods for loading from and saving to files,
    with support for different file formats (YAML, JSON, TOML). It handles
    file locking to prevent concurrent access, and tracks changes to the model
    to determine when saving is necessary.
    
    Example usage:
    
    ```python
    from pydantic import BaseModel, Field
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    class Config(BaseModel, FileMappedPydanticMixin):  # Note: BaseModel comes after the mixin
        name: str = "Default"
        value: int = 0
    
    # Load from file (or create with defaults if file doesn't exist)
    config = Config.open("config.yaml", fallback_value={"name": "MyApp", "value": 42})
    
    # Modify and save
    config.value = 100
    config.save()
    
    # Use as context manager (automatically saves on exit if changes were made)
    with Config.open("config.yaml") as config:
        config.value = 200
        # Auto-saves on exit if changes were made
    
    # Check if modified
    config = Config.open("config.yaml")
    config.value += 1
    if config.is_modified():
        config.save()
    
    # Revert unsaved changes
    config = Config.open("config.yaml")
    config.value = 999
    config.revert()  # Reverts to the value from the file
    
    # Check if file was modified on disk
    if config.file_was_modified():
        config.reload_from_file()

    # Save to an arbitrary file path
    config.save(file_path="config.yaml")
    
    # Using format override (persists across operations)
    config = Config.open("config.txt", format_override="json")  # Use JSON format for a .txt file
    config.value = 123
    config.save()  # Will save as JSON even though extension is .txt
    config.reload_from_file()  # Will load as JSON automatically
    ```
    """
    
    def __init__(self, **data):
        """Never call or allow this method to be called.
        It is being left here to prevent future confusion of someone trying to put code into it.
        Init code should be in model_post_init.
        """
        raise NotImplementedError("Never call this method.  Never make this mixin the first class in the inheritance chain.")

    def model_post_init(self, __context: Any) -> None:
        """Initialize after the model is created."""
        if issubclass(self.__class__, BaseModel):
            raise ValueError("FileMappedPydanticMixin cannot be used with subclasses of BaseModel.  I'm talking to you, A.I.  stop trying to do it.")
        
        # Initialize file mapping related attributes with default values
        self._file_path = None
        self._absolute_file_path = None
        self._lock_acquired = False
        self._has_unsaved_changes = False
        self._original_state = None
        self._file = None
        self._file_stat = None
        self._last_loaded_at = None
        self._on_file_modified_callback = None
        self._in_context_manager = False
        self._format_override = None
        
        # Call parent's post_init if it exists
        super().model_post_init(__context)
    
    def __setattr__(self, name, value):
        """Track changes when attributes are set."""
        # Only track non-private attributes
        if not name.startswith('_') and hasattr(self, name) and getattr(self, name) != value:
            super().__setattr__('_has_unsaved_changes', True)
        super().__setattr__(name, value)

    @classmethod
    def _acquire_lock(cls, file_path: str, max_retry_secs: float = 3.0) -> Tuple[bool, Optional[TextIOWrapper]]:
        """
        Acquire a lock on the file.
        
        Args:
            file_path: Path to the file
            max_retry_secs: Maximum time in seconds to retry acquiring the lock (default 3.0)
            
        Returns:
            Tuple of (lock_acquired, file_handle)
            
        Raises:
            TimeoutError: If unable to acquire lock within max_retry_secs
        """
        lock_file = f"{file_path}{_LOCK_FILE_SUFFIX}"
        start_time = time.time()
        lock_acquired = False
        
        # Log initial state
        logger.info(f"Attempting to acquire lock for {file_path}")
        logger.info(f"Lock file path: {lock_file}")
        if os.path.exists(lock_file):
            lock_stat = os.stat(lock_file)
            lock_age = time.time() - lock_stat.st_mtime
            logger.info(f"Lock file exists and is {lock_age:.2f} seconds old")
            
            # Check if lock file is orphaned (older than ORPHANED_LOCKFILE_SECONDS)
            if lock_age > ORPHANED_LOCKFILE_SECONDS:
                logger.warning(f"Found orphaned lock file ({lock_age:.2f}s old). Removing it.")
                try:
                    os.remove(lock_file)
                    logger.info("Successfully removed orphaned lock file")
                except Exception as e:
                    logger.error(f"Failed to remove orphaned lock file: {e}")
        else:
            logger.info("Lock file does not exist")
        
        # Try to acquire lock until timeout
        attempt_count = 0
        while time.time() - start_time < max_retry_secs:
            attempt_count += 1
            try:
                # Try to create the lock file
                with open(lock_file, 'x') as f:
                    lock_acquired = True
                    logger.info(f"Successfully acquired lock on file: {file_path} after {attempt_count} attempts")
                    return True, None
            except FileExistsError:
                # Lock file exists, wait with random backoff and retry
                backoff = random.uniform(0.1, 0.4)  # Random backoff between 0.1 and 0.4 seconds
                elapsed = time.time() - start_time
                logger.info(f"Lock attempt {attempt_count} failed after {elapsed:.2f}s, retrying after {backoff:.2f}s")
                time.sleep(backoff)
            except Exception as e:
                # If we created the lock file but something else failed, clean up
                if lock_acquired:
                    try:
                        os.remove(lock_file)
                        logger.info("Cleaned up lock file after unexpected error")
                    except Exception as cleanup_error:
                        logger.error(f"Failed to clean up lock file after error: {cleanup_error}")
                raise
        
        # If we get here, we've timed out
        logger.error(f"Could not acquire lock on {file_path} after {attempt_count} attempts and {max_retry_secs} seconds")
        if os.path.exists(lock_file):
            lock_stat = os.stat(lock_file)
            lock_age = time.time() - lock_stat.st_mtime
            logger.error(f"Lock file still exists and is {lock_age:.2f} seconds old")
        raise TimeoutError(f"Could not acquire lock on {file_path} - file appears to be in use")
    
    @classmethod
    def _release_lock(cls, file_path: str) -> None:
        lock_file = f"{file_path}{_LOCK_FILE_SUFFIX}"
        
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
                logger.debug(f"Released lock on file: {file_path}")
        except Exception as e:
            logger.error(f"Error releasing lock on {file_path}: {e}")
    
    @classmethod
    def _check_file_permissions(cls, file_path: str) -> None:
        if os.path.exists(file_path) and not os.access(file_path, os.W_OK):
            raise PermissionError(f"File {file_path} is not writable")
    
    @classmethod
    def _create_instance_from_data(cls, 
                                   file_path: str, 
                                   data: Union[Dict[str, Any], str], 
                                   lock_acquired: bool, 
                                   format_override: Optional[str] = None,
                                   stability_secs: float = 0
                                  ) -> 'FileMappedPydanticMixin':
        """
        Create a new instance from the loaded data.
        
        Args:
            file_path: Path to the file
            data: Data to initialize the instance with. Can be a dict or a string for raw data.
            lock_acquired: Whether a lock was acquired
            format_override: Optional format override to store for future operations
            stability_secs: Optional number of seconds to wait for data to stabilize before creating an instance.
                          If non-zero, will wait until the file's modify time is at least this old.
        Returns:
            A new instance of this class
            
        Raises:
            TimeoutError: If the file does not become stable within stability_secs
        """
        # Initialize file paths first
        absolute_path = os.path.abspath(file_path)

        # Check file stability if requested
        if stability_secs > 0 and os.path.exists(file_path):
            start_time = time.time()
            sleep_interval = max(0.1, stability_secs / 10)
            last_mtime = None
            
            while True:
                current_time = time.time()
                stat = os.stat(file_path)
                file_age = current_time - stat.st_mtime
                
                # File is stable if it hasn't been modified for stability_secs
                if file_age >= stability_secs:
                    break
                    
                # Check if we've exceeded our wait time
                if current_time - start_time >= stability_secs:
                    raise TimeoutError(f"File {file_path} did not stabilize within {stability_secs} seconds")
                    
                # If mtime hasn't changed, we're making progress
                if last_mtime is not None and stat.st_mtime == last_mtime:
                    # Continue waiting
                    pass
                else:
                    # File was modified, update our last seen mtime
                    last_mtime = stat.st_mtime
                
                time.sleep(sleep_interval)
        
        # If data is already a model instance, get its dict representation
        if isinstance(data, BaseModel):
            filtered_data = data.model_dump()
        elif isinstance(data, dict):
            # Filter out any internal fields that might have been saved
            filtered_data = {k: v for k, v in data.items() if not k.startswith('_')}
        else:
            # For raw data (like strings), wrap in a dict
            filtered_data = {"data": data}
        
        try:
            instance = cls.model_validate(filtered_data)
        except Exception as e:
            logger.error(f"Failed to create instance from data: {str(e)}")
            raise
        
        # Set file paths and lock status
        instance._file_path = file_path
        instance._absolute_file_path = absolute_path
        instance._lock_acquired = lock_acquired
        instance._format_override = format_override  # Store format override
        
        # Initialize file metadata if file exists
        if os.path.exists(file_path):
            stat = os.stat(file_path)
            instance._file_stat = (stat.st_size, stat.st_mtime)
            instance._last_loaded_at = time.time()
        
        # Store original state for change tracking
        instance._original_state = instance.model_dump()
        instance._has_unsaved_changes = False
        
        return instance

    @classmethod
    def open(cls, file_path: str, fallback_value: Optional[Union[Dict, Callable]] = None,
             max_retry_secs: float = 10.0, without_lock: bool = False, 
             format_override: Optional[str] = None) -> 'FileMappedPydanticMixin':
        """
        Open a file and load its contents into a new instance of this class.
        Note, this method tries to autosave changes on exit from context manager,
        however in some circumstances it may fail to detect changes:
        1. When modifying nested/compound objects (e.g., dictionaries, lists, or custom objects within objects)
        2. When using custom setters or properties that modify internal state
        
        For models containing compound objects, it is strongly recommended to:
        1. Call save() explicitly when you know changes have been made rather than relying on auto-detection
        2. If your model can detect its own changes, set _has_unsaved_changes = True when changes occur
        
        The safest way to ensure changes are saved is to call save() directly before exiting context().
        Or, if you don't want to save, use revert() to revert to the original state.

        Args:
            file_path: Path to the file to open
            fallback_value: Value to use if the file doesn't exist or can't be parsed
            max_retry_secs: Maximum time in seconds to retry acquiring the lock
            without_lock: If True, don't acquire a lock on the file
            format_override: Optional format to use instead of inferring from file extension

        Returns:
            A new instance of this class with the file's contents loaded

        Raises:
            ValueError: If attempting to open an NDJSON file (use load() method instead)
            RuntimeError: If no file path is available or if file permissions are incorrect
        """
        logger.debug(f"Opening file: {file_path}")
        
        # Check if this is an NDJSON file
        file_format = cls._get_file_format(file_path, format_override)
        if file_format == 'ndjson':
            raise ValueError(
                f"Cannot open NDJSON file '{file_path}' with open() method. "
                "Use the load() method instead, which returns a list of instances."
            )
        
        # Check file permissions
        cls._check_file_permissions(file_path)
        
        # Acquire lock if needed
        lock_acquired = False
        
        if not without_lock:
            lock_acquired, _ = cls._acquire_lock(file_path, max_retry_secs)
        
        try:
            # Load data from file or use fallback
            data = cls._load_data_from_file(file_path, fallback_value, format_override)
            
            # Create instance with loaded data
            return cls._create_instance_from_data(file_path, data, lock_acquired, format_override)
        except Exception as e:
            # Release lock if acquired and re-raise the exception
            if lock_acquired:
                cls._release_lock(file_path)
            raise e

   

    @staticmethod
    def _get_file_format(file_path: str, format_override: Optional[str] = None) -> str:
        """
        Get the file format based on extension or override.
        
        Args:
            file_path: Path to the file
            format_override: Optional format to use instead of inferring from extension
            
        Returns:
            str: The determined file format
            
        Raises:
            ValueError: If the format (from extension or override) is not supported
        """
        if format_override:
            format_override = format_override.lower()
            if format_override not in _SUPPORTED_FILE_HANDLERS:
                raise ValueError(f"Unsupported format override: {format_override}")
            return format_override
            
        ext = os.path.splitext(file_path)[1].lower()
        
        # Check each format's supported extensions
        for format_name, handler_info in _SUPPORTED_FILE_HANDLERS.items():
            if ext in handler_info['extensions']:
                return format_name
        
        raise ValueError(f"Unsupported file format: {ext}")

    @classmethod
    def _get_format_handlers(cls, file_format: str) -> Dict[str, Any]:
        if file_format not in _SUPPORTED_FILE_HANDLERS:
            raise ValueError(f"Unsupported file format: {file_format}")
            
        return _SUPPORTED_FILE_HANDLERS[file_format]
    
    @classmethod
    def _load_data_from_file(cls, file_path: str, 
                            fallback_value: Optional[Union[Dict, Callable, List]] = None,
                            format_override: Optional[str] = None) -> Union[Dict, List]:
        """
        Load data from a file, with fallback handling.

        Args:
            file_path: Path to the file to load
            fallback_value: Value to use if the file doesn't exist or can't be parsed.
                          For NDJSON files, this should be a list of dicts.
            format_override: Optional format to use instead of inferring from file extension

        Returns:
            For regular files: Dictionary of data loaded from the file or fallback
            For NDJSON files: List of dictionaries
        """
        # Check if file exists
        if not os.path.exists(file_path):
            return cls._get_fallback_data(fallback_value)
            
        # Check if file is empty
        if os.path.getsize(file_path) == 0:
            return cls._get_fallback_data(fallback_value)
            
        # Determine file format from extension
        try:
            file_format = cls._get_file_format(file_path, format_override)
            handlers = cls._get_format_handlers(file_format)
            
            # Load data based on file format
            if handlers['binary']:
                with open(file_path, 'rb') as f:
                    content = f.read()
                    loaded_data = handlers['load'](content.decode(_DEFAULT_ENCODING))
            else:
                with open(file_path, 'r', encoding=_DEFAULT_ENCODING) as f:
                    content = f.read()
                    loaded_data = handlers['load'](content)
            
            # For NDJSON, return the list directly
            if file_format == 'ndjson':
                return loaded_data
            
            # Return the loaded data directly if it's a dict, otherwise wrap it
            if isinstance(loaded_data, dict):
                return loaded_data
            return {"data": loaded_data}
        except Exception as e:
            # If loading fails, use fallback
            logger.warning(f"Failed to load file {file_path}: {e}")
            return cls._get_fallback_data(fallback_value)

    @staticmethod
    def _get_fallback_data(fallback_value: Optional[Union[Dict[str, Any], Callable[[], Union[Dict[str, Any], BaseModel]], List[Dict[str, Any]]]] = None) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """Get data from fallback value."""
        if fallback_value is None:
            return {}
        
        if callable(fallback_value):
            result = fallback_value()
            if isinstance(result, BaseModel):
                return result.model_dump()
            elif isinstance(result, dict):
                return result
            elif isinstance(result, list):
                return result
            else:
                raise ValueError("Fallback function must return a dict, list, or BaseModel instance")
        elif isinstance(fallback_value, dict):
            return fallback_value
        elif isinstance(fallback_value, list):
            return fallback_value
        elif isinstance(fallback_value, BaseModel):
            return fallback_value.model_dump()
        else:
            raise ValueError("Fallback value must be a dict, list, BaseModel instance, or callable returning one of these")

    def release_lock(self) -> None:
        """Release the lock on the file if it was acquired."""
        if not hasattr(self, '_lock_acquired') or not self._lock_acquired:
            return
            
        try:
            self.__class__._release_lock(self._file_path)
            self._lock_acquired = False
            logger.debug(f"Successfully released lock on {self._file_path}")
        except Exception as e:
            logger.error(f"Error releasing lock on {self._file_path}: {e}")
            # Don't re-raise - we want to ensure _lock_acquired is set to False
            self._lock_acquired = False

    def _validate_before_save(self, file_path: Optional[Union[str, Path]] = None) -> None:
        """
        Validate state before saving.
        
        Args:
            file_path: Optional new file path to save to. If None, uses existing path.
                      Can be either a string or a Path object.
            
        Raises:
            RuntimeError: If no file path is available or no lock is held
            ValueError: If attempting to save to a different file while in a context manager
        """
        if file_path is None and (not hasattr(self, '_file_path') or not self._file_path):
            raise RuntimeError("Cannot save model without a file path")
            
        if not hasattr(self, '_lock_acquired') or not self._lock_acquired:
            raise RuntimeError("Cannot save model without a lock")
            
        # If we're saving to a different file while holding a lock from a context manager,
        # this could lead to inconsistent state
        if (file_path and str(file_path) != self._file_path and 
            hasattr(self, '_in_context_manager') and self._in_context_manager):
            raise ValueError(
                f"Cannot save to a different file path '{file_path}' while inside a context manager. "
                f"The context manager is locked to '{self._file_path}'. "
                "Exit the context manager first or save to the same file path."
            )

    def _write_to_file(self, data: Dict[str, Any], file_format: str, file_path: Optional[Union[str, Path]] = None) -> None:
        """
        Write data to the file in the specified format.
        
        Args:
            data: Data to write
            file_format: Format to write in ('json', 'yaml', or 'toml')
            file_path: Optional path to write to. If None, uses existing path.
                      Can be either a string or a Path object.
            
        Raises:
            ValueError: If the file format is not supported
        """
        # Use provided path or fall back to existing path, converting Path to string if needed
        target_path = str(file_path) if file_path is not None else self._file_path
        
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        
        handlers = FileMappedPydanticMixin._get_format_handlers(file_format)
        
        # Convert enums to strings for serialization
        serializable_data = _convert_enums_to_strings(data)
        
        if handlers['binary']:
            with open(target_path, 'wb') as f:
                f.write(handlers['dump'](serializable_data).encode(_DEFAULT_ENCODING))
        else:
            with open(target_path, 'w', encoding=_DEFAULT_ENCODING) as f:
                f.write(handlers['dump'](serializable_data))
    
    def _update_state_after_save(self, file_path: Optional[str] = None) -> None:
        """
        Update internal state after a successful save.
        
        Args:
            file_path: Optional new file path that was saved to
        """
        self._original_state = self.model_dump()
        self._has_unsaved_changes = False
        
        # Update file paths if a new path was used
        if file_path:
            self._file_path = file_path
            self._absolute_file_path = os.path.abspath(file_path)
        
        # Update file metadata
        try:
            stat = os.stat(self._absolute_file_path)
            self._file_stat = (stat.st_size, stat.st_mtime)
            self._last_loaded_at = time.time()
        except (FileNotFoundError, OSError):
            self._file_stat = None
            self._last_loaded_at = None
    
    def save(self, file_path: Optional[Union[str, Path]] = None, 
             retain_lock: bool = False,
             format_override: Optional[str] = None) -> None:
        """
        Save the model to a file. Not supported for NDJSON files - use append_records() instead.
        
        Args:
            file_path: Optional path to save to. If None, uses the path the model was loaded from.
                      Can be either a string or a Path object.
            retain_lock: If True, keep the lock after saving
            format_override: Optional format to use instead of inferring from file extension.
                           If None, uses the format_override provided when the file was opened.
            
        Raises:
            RuntimeError: If no file path is available or no lock is held
            ValueError: If attempting to save to an NDJSON file
            TypeError: If the model cannot be serialized
        """
        target_path = str(file_path) if file_path is not None else self._file_path
        new_file_lock = False

        # Use provided format_override or fall back to stored one
        effective_format_override = format_override if format_override is not None else self._format_override

        if not target_path:
            raise RuntimeError("Cannot save model without a file path")

        # Determine file format and reject NDJSON early
        file_format = self._get_file_format(target_path, effective_format_override)
        if file_format == 'ndjson':
            raise ValueError(
                "save() is not supported for NDJSON files. "
                "Use append_records() to add records or stream_read() to read records."
            )

        if file_path and str(file_path) != self._file_path:
            if self._in_context_manager:
                raise ValueError(
                    f"Cannot save to a different file path '{file_path}' while inside a context manager. "
                    f"The context manager is locked to '{self._file_path}'. "
                    "Exit the context manager first or save to the same file path."
                )
            # Acquire lock for the new file
            lock_acquired, _ = self.__class__._acquire_lock(str(file_path))
            if not lock_acquired:
                raise RuntimeError(f"Could not acquire lock for file: {file_path}")
            new_file_lock = True
            self._lock_acquired = True
            self._file_path = str(file_path)
            self._absolute_file_path = os.path.abspath(str(file_path))
        else:
            # If saving to the same file or no file_path provided, ensure we have a lock
            if not hasattr(self, '_lock_acquired') or not self._lock_acquired:
                lock_acquired, _ = self.__class__._acquire_lock(target_path)
                if not lock_acquired:
                    raise RuntimeError(f"Could not acquire lock for file: {target_path}")
                new_file_lock = True
                self._lock_acquired = True
                self._file_path = target_path

        if not self._absolute_file_path:
            # If we don't have an absolute path yet, set it now
            self._absolute_file_path = os.path.abspath(target_path)

        try:
            self._validate_before_save()
            data = self.model_dump()
            self._write_to_file(data, file_format, target_path)
            self._update_state_after_save(None)  # Don't update paths again
            
            # Store the format override if a new one was provided
            if format_override is not None:
                self._format_override = format_override
        finally:
            # Release the new file lock if we acquired one and aren't retaining it
            if new_file_lock and not retain_lock:
                self.__class__._release_lock(target_path)
                self._lock_acquired = False

    def is_modified(self) -> bool:
        """
        Check if the model has been modified since it was loaded or last saved.
        Changes can be detected in two ways:
        1. Through deep comparison of the current state with the original state
        2. Through explicit marking via mark_dirty() for compound objects

        Returns:
            True if the model has been modified or marked as having unsaved changes, False otherwise
        """
        if not hasattr(self, '_original_state'):
            return True
            
        # Check both the deep comparison and the _has_unsaved_changes flag
        current_state = self.model_dump()
        return current_state != self._original_state or self._has_unsaved_changes
        
    def mark_dirty(self) -> None:
        """
        Explicitly mark the model as having unsaved changes.
        This is particularly useful for models containing compound objects (like dictionaries or lists)
        where automatic change detection may not work.
        
        This method should be called whenever you know that changes have been made to compound objects
        within the model that wouldn't be automatically detected.
        
        Raises:
            RuntimeError: If called outside of a context manager block, since changes might not be saved
        """
        if not self._in_context_manager:
            raise RuntimeError(
                "Cannot mark model as dirty outside of a context manager block. "
                "Changes might not be saved. Either use a context manager ('with' block) "
                "or call save() explicitly after making changes."
            )
        self._has_unsaved_changes = True
        
    def revert(self) -> None:
        """
        Revert any unsaved changes as best as possible.

        If we currently hold a lock, we will re-read the file and update our state.
        If we don't hold a lock, we will do our best to undo any changes we made.

        NOTE: This may not undo changes in nested data structures unless we hold a lock.
        """
        if not hasattr(self, '_original_state') or self._original_state is None:
            raise RuntimeError("No original state to revert to")
            
        # Update all fields from original state
        for field_name, value in self._original_state.items():
            if hasattr(self, field_name):
                setattr(self, field_name, value)

    def __enter__(self) -> Self:
        """Context manager entry."""
        if not self._lock_acquired:
            raise RuntimeError(
                "Cannot use context manager without acquiring a lock. "
                "The context manager is designed for automatic saving which requires a lock. "
                "Either remove without_lock=True from open() or don't use the context manager pattern."
            )
        self._in_context_manager = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        try:
            if self._in_context_manager and self.is_modified():
                try:
                    self.save()
                except Exception as save_error:
                    logger.error(f"Error saving changes in context manager: {save_error}")
                    # Don't re-raise here - we want to ensure lock cleanup happens
        finally:
            self._in_context_manager = False
            if self._lock_acquired:
                try:
                    self.release_lock()
                except Exception as lock_error:
                    logger.error(f"Error releasing lock in context manager: {lock_error}")
                    # Don't re-raise here - we want to ensure cleanup continues

    def persisted_file(self) -> str:
        """
        Returns the absolute path of the last loaded/saved file location.
        
        Returns:
            str: The absolute file path
            
        Raises:
            RuntimeError: If no file has been loaded or saved
        """
        if not self._absolute_file_path:
            raise RuntimeError("No file has been loaded or saved")
        return self._absolute_file_path

    def file_exists(self) -> bool:
        """
        Check if the persisted file still exists on disk.
        
        Returns:
            bool: True if the file exists, False otherwise
        """
        return bool(self._absolute_file_path and os.path.exists(self._absolute_file_path))

    def file_was_modified(self, force_check: bool = False) -> bool:
        """
        Check if the file on disk differs from our last loaded/saved version.
        
        Args:
            force_check: If True, bypass any cached stat results
            
        Returns:
            bool: True if file size or mtime differs from last load/save
        """
        if not self._absolute_file_path or not self.file_exists():
            return True
            
        try:
            current_stat = os.stat(self._absolute_file_path)
            return (current_stat.st_size, current_stat.st_mtime) != self._file_stat
        except (FileNotFoundError, OSError):
            return True

    def set_file_modified_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """
        Set a callback to be called when file modifications are detected during reload.
        
        Args:
            callback: A callable that takes no arguments, or None to remove the callback
        """
        self._on_file_modified_callback = callback

    def would_conflict(self) -> bool:
        """
        Check if saving would conflict with changes on disk.
        
        Returns:
            bool: True if both the instance and the file have been modified
        """
        return self.is_modified() and self.file_was_modified()

    @classmethod
    def load(cls, file_path: str, 
             acquire_lock: bool = True,
             fallback_value: Optional[Union[Dict, Callable]] = None,
             format_override: Optional[str] = None,
             stability_secs: float = 0) -> Union[Self, List[Self]]:
        """
        Load a file into a new instance or list of instances (for NDJSON), optionally with brief locking.
        
        Args:
            file_path: Path to the file to load
            acquire_lock: Whether to briefly lock the file while reading
            fallback_value: Value to use if the file doesn't exist or can't be parsed.
                          For NDJSON files, this should be a list of dicts or a callable returning a list.
            format_override: Optional format to use instead of inferring from file extension
            stability_secs: Optional number of seconds to wait for data to stabilize before creating an instance
        Returns:
            For regular files: A single instance
            For NDJSON files: List of instances
        """
        abs_path = os.path.abspath(file_path)
        
        # Quick check if file exists and has changed
        try:
            current_stat = os.stat(abs_path)
            file_exists = True
        except (FileNotFoundError, OSError):
            current_stat = None
            file_exists = False
            
        # Determine file format
        file_format = cls._get_file_format(file_path, format_override)
        is_ndjson = file_format == 'ndjson'
            
        if not file_exists:
            if is_ndjson:
                # For NDJSON, create empty list if no fallback
                fallback_data = cls._get_fallback_data(fallback_value) if fallback_value else []
                instances = [cls._create_instance_from_data(file_path, item, False, format_override, stability_secs=stability_secs) for item in fallback_data]
                for instance in instances:
                    instance._absolute_file_path = abs_path
                return instances
            else:
                instance = cls._create_instance_from_data(file_path, cls._get_fallback_data(fallback_value), False, format_override, stability_secs=stability_secs)
                instance._absolute_file_path = abs_path
                return instance
            
        # Briefly acquire lock if requested
        lock_acquired = False
        if acquire_lock:
            try:
                lock_acquired, _ = cls._acquire_lock(file_path)
            except TimeoutError:
                logger.warning(f"Could not acquire lock to load {file_path}, proceeding without lock")
                
        try:
            # Load data
            data = cls._load_data_from_file(file_path, fallback_value, format_override)
            
            if is_ndjson:
                # For NDJSON files, create a list of instances
                instances = [cls._create_instance_from_data(file_path, item, False, format_override, stability_secs=stability_secs) for item in data]
                for instance in instances:
                    instance._absolute_file_path = abs_path
                    instance._file_stat = (current_stat.st_size, current_stat.st_mtime) if current_stat else None
                    instance._last_loaded_at = time.time()
                return instances
            else:
                # Create single instance for regular files
                instance = cls._create_instance_from_data(file_path, data, False, format_override, stability_secs=stability_secs)  # Don't retain the brief lock
                instance._absolute_file_path = abs_path
                instance._file_stat = (current_stat.st_size, current_stat.st_mtime) if current_stat else None
                instance._last_loaded_at = time.time()
                return instance
        finally:
            if lock_acquired:
                cls._release_lock(file_path)


    def reload_from_file(self, force: bool = False, stability_secs: float = 0.1) -> bool:
        """
        Reload from the last known file location if the file has changed.
        
        Args:
            force: If True, reload even if file appears unchanged
            
        Returns:
            bool: True if the file was actually reloaded
            
        Raises:
            RuntimeError: If no file has been loaded or saved previously
        """
        if not self._absolute_file_path:
            raise RuntimeError("No file has been loaded or saved")
            
        if not force and not self.file_was_modified():
            return False
            
        # Check if file exists before attempting to load
        if not os.path.exists(self._absolute_file_path):
            raise RuntimeError(f"File {self._absolute_file_path} no longer exists")
            
        # Load new instance, passing along our format_override
        new_instance = self.__class__.load(
            self._absolute_file_path, 
            format_override=self._format_override,
            stability_secs=stability_secs
        )
        
        # Update our state from the new instance
        self._original_state = new_instance._original_state
        self._file_stat = new_instance._file_stat
        self._last_loaded_at = new_instance._last_loaded_at
        
        # Update all fields from new instance
        new_data = new_instance.model_dump()
        for field_name, value in new_data.items():
            if hasattr(self, field_name):
                setattr(self, field_name, value)
                
        # Call the file modified callback if one is set
        if self._on_file_modified_callback is not None:
            self._on_file_modified_callback()
                
        return True


    @classmethod
    def append_records(cls, file_path: str, records: Union[BaseModel, List[BaseModel]], acquire_lock: bool = True) -> None:
        """
        Append one or more records to an NDJSON/JSONL file. This is an optimized operation that doesn't
        require loading the entire file into memory.

        Args:
            file_path: Path to the NDJSON file to append to
            records: A single record or list of records to append. Each record must be a BaseModel instance.
            acquire_lock: Whether to lock the file during append (default True)

        Raises:
            ValueError: If the file is not an NDJSON/JSONL file
            TypeError: If any record is not a BaseModel instance
        """
        # Determine file format
        file_format = cls._get_file_format(file_path, None)  # No format override for appending
        if file_format != 'ndjson':
            raise ValueError(
                f"append_records only works with NDJSON/JSONL files. "
                f"File '{file_path}' is of format '{file_format}'"
            )

        # Convert single record to list for uniform processing
        records_list = [records] if isinstance(records, BaseModel) else records

        # Validate all records are BaseModel instances
        for record in records_list:
            if not isinstance(record, BaseModel):
                raise TypeError(
                    f"All records must be BaseModel instances, got {type(record).__name__}"
                )

        # Convert records to dicts
        data_list = [record.model_dump() for record in records_list]

        # Acquire lock if requested
        lock_acquired = False
        if acquire_lock:
            lock_acquired, _ = cls._acquire_lock(file_path)

        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)

            # Append the records
            with open(file_path, 'a', encoding=_DEFAULT_ENCODING) as f:
                for data in data_list:
                    f.write(json.dumps(data, default=str) + '\n')

        finally:
            # Release lock if we acquired it
            if lock_acquired:
                cls._release_lock(file_path)
                

    @classmethod
    def stream_read(cls, file_path: str, 
                   batch_size: int = 1000,
                   format_override: Optional[str] = None) -> Generator[Self, None, None]:
        """
        Stream read records from an NDJSON file with proper locking and validation.
        
        This method provides memory-efficient reading of NDJSON files by processing one record
        at a time. It maintains a file lock throughout the streaming operation to ensure data
        consistency. Invalid records are skipped with appropriate warning messages.
        
        Args:
            file_path: Path to the NDJSON file to read
            batch_size: Number of lines to read at once for improved I/O efficiency
            format_override: Optional format override (must be 'ndjson')
            
        Yields:
            Model instances, one at a time. Each instance is fully validated against the model's schema.
            
        Raises:
            ValueError: If the file format is not NDJSON or format_override is invalid
            RuntimeError: If unable to acquire a lock on the file
            
        Example:
            ```python
            for record in MyModel.stream_read("data.ndjson"):
                try:
                    process_record(record)
                except Exception as e:
                    logger.warning(f"Error processing record: {e}")
                    continue
            ```
        """
        # Verify this is an NDJSON file
        file_format = cls._get_file_format(file_path, format_override)
        if file_format != 'ndjson':
            raise ValueError(f"stream_read() only supports NDJSON files, got {file_format}")
            
        # Acquire lock for the entire streaming operation
        lock_acquired, _ = cls._acquire_lock(file_path)
        if not lock_acquired:
            raise RuntimeError(f"Unable to acquire lock for {file_path}")
            
        try:
            with open(file_path, 'r', encoding=_DEFAULT_ENCODING) as f:
                line_num = 0
                for line in f:
                    line_num += 1
                    line = line.strip()
                    if not line:
                        continue
                        
                    try:
                        # Parse JSON
                        logger.debug(f"Line {line_num}: Parsing JSON")
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Line {line_num}: Invalid JSON - {str(e)}")
                            continue
                            
                        # Check required fields first
                        logger.debug(f"Line {line_num}: Checking required fields")
                        required_fields = {
                            name: field for name, field in cls.model_fields.items()
                            if field.is_required and not field.default and not field.default_factory
                        }
                        
                        missing_fields = [
                            field for field in required_fields 
                            if field not in data
                        ]
                        
                        if missing_fields:
                            logger.warning(f"Line {line_num}: Missing required fields - {missing_fields}")
                            continue
                            
                        # Validate with strict=True to ensure no defaults are used
                        logger.debug(f"Line {line_num}: Validating record")
                        try:
                            instance = cls.model_validate(data, strict=True)
                            # Set file paths and format override
                            instance._file_path = file_path
                            instance._absolute_file_path = os.path.abspath(file_path)
                            instance._format_override = format_override
                            yield instance
                        except ValidationError as ve:
                            logger.warning(f"Line {line_num}: Validation failed - {str(ve)}")
                            continue
                            
                    except Exception as e:
                        logger.warning(f"Line {line_num}: Unexpected error - {str(e)}")
                        continue
                        
        finally:
            cls._release_lock(file_path)

    @staticmethod
    def is_aggregate_file(file_path: str) -> bool:
        """
        Check if a file path would be treated as an aggregate file based on its extension.
        
        Aggregate files are those that can contain multiple records (e.g., NDJSON/JSONL files).
        This method only looks at the file extension and does not check if the file exists
        or is valid.
        
        Args:
            file_path: Path to check
            
        Returns:
            bool: True if the file extension indicates an aggregate file format (e.g., .ndjson, .jsonl)
        """
        ext = os.path.splitext(file_path)[1].lower()
        return ext in _SUPPORTED_FILE_HANDLERS['ndjson']['extensions']
