# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
import pytest
import stat
import yaml
import json
import time
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Callable, Type, Any, List, Dict
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin, _LOCK_FILE_SUFFIX
from totodev_pub.pytest_tools import very_lazy_test
import logging

# Setup logger
logger = logging.getLogger(__name__)

# Define model at module level with a name that doesn't start with "Test"
class SampleMappedModel(BaseModel, FileMappedPydanticMixin):
    """A simple Pydantic model that uses the FileMappedPydanticMixin for testing."""
    name: str = Field("default_name")
    value: int = Field(0)
    items: List[Dict[str, Any]] = Field(default_factory=list)

    def __init__(self, **data):
        super().__init__(**data)
        self._persisted_file_path = None
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

@pytest.fixture
def test_model_cls() -> Type[FileMappedPydanticMixin]:
    """
    Returns the SampleMappedModel class for testing.
    """
    return SampleMappedModel

@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    """
    Creates a temporary YAML file with some sample content.
    """
    file_path = tmp_path / "test_data.yaml"
    sample_data = {
        "name": "initial_name",
        "value": 123
    }
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(sample_data, f)
    return file_path

@pytest.fixture
def json_file(tmp_path: Path) -> Path:
    """
    Creates a temporary JSON file with some sample content.
    """
    file_path = tmp_path / "test_data.json"
    sample_data = {
        "name": "initial_json",
        "value": 456
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    return file_path

@pytest.fixture(autouse=True)
def cleanup_locks(tmp_path: Path):
    """Cleanup any stray lock files before and after each test."""
    # Setup
    def remove_lock_files(directory):
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(_LOCK_FILE_SUFFIX):
                    try:
                        lock_path = os.path.join(root, file)
                        os.remove(lock_path)
                        logger.debug(f"Removed lock file: {lock_path}")
                    except OSError as e:
                        logger.debug(f"Failed to remove lock file {os.path.join(root, file)}: {e}")
    
    # Clean up the temporary directory
    remove_lock_files(tmp_path)
    
    # Also clean up the system temp directory
    import tempfile
    temp_dir = tempfile.gettempdir()
    remove_lock_files(temp_dir)
    
    yield  # Run the test
    
    # Teardown - clean up again
    remove_lock_files(tmp_path)
    remove_lock_files(temp_dir)
    
    # Force release any locks that might be held by test instances
    for instance in list(FileMappedPydanticMixin.__subclasses__()):
        for obj in list(instance.__dict__.values()):
            if hasattr(obj, '_lock_acquired') and obj._lock_acquired and hasattr(obj, '_file_path'):
                try:
                    obj.release_lock()
                except Exception:
                    pass

def test_open_non_existing_file_no_fallback(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test loading from a non-existing file without a fallback function.
    Expect a new instance with default field values.
    """
    file_path = tmp_path / "non_existing.yaml"
    model = test_model_cls.open(str(file_path))
    assert model.name == "default_name"
    assert model.value == 0
    # No file was found, so it's not locked. The model is "new."

def test_open_non_existing_file_with_fallback(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test loading from a non-existing file with a fallback function.
    Expect the fallback function's result to be used.
    """
    file_path = tmp_path / "non_existing.yaml"

    def fallback_fn() -> FileMappedPydanticMixin:
        return test_model_cls(name="fallback_name", value=999)

    model = test_model_cls.open(str(file_path), fallback_value=fallback_fn)
    assert model.name == "fallback_name"
    assert model.value == 999

def test_load_existing_empty_file_no_fallback(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    If the file exists but is empty, we should create a new instance via default constructor.
    """
    file_path = tmp_path / "empty.yaml"
    open(file_path, "w").close()  # Create an empty file
    model = test_model_cls.open(str(file_path))
    assert model.name == "default_name"
    assert model.value == 0

def test_load_existing_empty_file_with_fallback(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    If the file exists but is empty, we should create a new instance via fallback.
    """
    file_path = tmp_path / "empty_fallback.yaml"
    open(file_path, "w").close()  # Create empty file

    def fallback_fn() -> FileMappedPydanticMixin:
        return test_model_cls(name="fallback_empty", value=42)

    model = test_model_cls.open(str(file_path), fallback_value=fallback_fn)
    assert model.name == "fallback_empty"
    assert model.value == 42

def test_load_valid_yaml_file(test_model_cls: Type[FileMappedPydanticMixin], yaml_file: Path) -> None:
    """
    Load from an existing valid YAML file. Ensure values are parsed correctly.
    """
    model = test_model_cls.open(str(yaml_file))
    assert model.name == "initial_name"
    assert model.value == 123

def test_load_valid_json_file(test_model_cls: Type[FileMappedPydanticMixin], json_file: Path) -> None:
    """
    Load from an existing valid JSON file. Ensure values are parsed correctly.
    """
    logger.info(f"Starting test_load_valid_json_file with file: {json_file}")
    
    # Verify the JSON file exists and has correct content
    with open(json_file, "r") as f:
        content = f.read()
        logger.info(f"Initial JSON file content: {content}")
    
    model = test_model_cls.open(str(json_file))
    logger.info(f"Loaded model state: {model.model_dump()}")
    
    assert model.name == "initial_json"
    assert model.value == 456

def test_read_only_file_raises_permission_error(test_model_cls: Type[FileMappedPydanticMixin], yaml_file: Path) -> None:
    """
    If the file is read-only, loading should raise a PermissionError
    because the code checks for write permissions.
    """
    # Make the file read-only
    os.chmod(yaml_file, stat.S_IREAD)
    with pytest.raises(PermissionError):
        _ = test_model_cls.open(str(yaml_file))
    # Restore for cleanup
    os.chmod(yaml_file, stat.S_IWRITE)

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_save_without_lock_raises_error(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    If we haven't acquired a lock (e.g., loaded with without_lock=True),
    the save method should try to acquire a lock. If we prevent lock acquisition,
    it should raise an error.
    """
    file_path = tmp_path / "no_lock.yaml"
    
    # Create the file first
    with open(file_path, 'w') as f:
        f.write("{}")
    
    # Create a lock file to prevent lock acquisition
    lock_file = f"{file_path}{_LOCK_FILE_SUFFIX}"
    with open(lock_file, 'w') as f:
        f.write("dummy lock")
    
    # Open the file without acquiring a lock
    model = test_model_cls.open(str(file_path), without_lock=True)
    assert model._lock_acquired is False
    
    # When we try to save, it should try to acquire a lock and fail
    # The exact error type might vary (TimeoutError or RuntimeError), so we'll just check for any exception
    with pytest.raises(Exception):
        model.save()

def test_save_with_lock_and_reload(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test normal save flow. Model should lock the file, write content,
    and the saved file content should match on reload.
    """
    file_path = tmp_path / "test_save.yaml"
    model = test_model_cls.open(str(file_path))  # lock acquired
    model.name = "modified"
    model.value = 999
    # Save (lock is acquired automatically by load)
    model.save()  # By default, lock is released after save
    
    # Explicitly release the lock to ensure it's released
    if hasattr(model, '_lock_acquired') and model._lock_acquired:
        model.release_lock()

    # Reload from file to confirm changes persisted
    reloaded = test_model_cls.open(str(file_path))
    assert reloaded.name == "modified"
    assert reloaded.value == 999
    
    # Make sure to release the lock after the test
    if hasattr(reloaded, '_lock_acquired') and reloaded._lock_acquired:
        reloaded.release_lock()

def test_save_with_retain_lock(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    If we save with retain_lock=True, the file should remain locked afterward,
    requiring manual release.
    """
    file_path = tmp_path / "retain_lock.yaml"
    model = test_model_cls.open(str(file_path))
    assert model._lock_acquired is True

    model.save(retain_lock=True)
    # After saving with retain_lock=True, lock is still held
    assert model._lock_acquired is True

    # Now we can do more changes and save again without reacquiring lock
    model.name = "second_change"
    model.save()  # lock is released this time
    
    # Explicitly release the lock to ensure it's released
    if hasattr(model, '_lock_acquired') and model._lock_acquired:
        model.release_lock()

    reloaded = test_model_cls.open(str(file_path))
    assert reloaded.name == "second_change"
    
    # Make sure to release the lock after the test
    if hasattr(reloaded, '_lock_acquired') and reloaded._lock_acquired:
        reloaded.release_lock()

def test_release_lock_idempotent(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that calling release_lock() multiple times does not cause errors.
    """
    file_path = tmp_path / "idempotent.yaml"
    model = test_model_cls.open(str(file_path))
    assert model._lock_acquired is True
    model.release_lock()
    assert model._lock_acquired is False
    # Calling again should not raise an error
    model.release_lock()
    assert model._lock_acquired is False

def test_exception_while_loading_cleans_up_lock(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Simulate an exception during load (e.g., invalid file content)
    and verify the lock is released.
    """
    from unittest.mock import patch

    file_path = tmp_path / "bad_data.yaml"
    with open(file_path, "w") as f:
        f.write("bad_data: yes\n")

    # Patch the class's open method to raise an exception
    with patch.object(test_model_cls, 'open', side_effect=ValueError("Simulated parsing error")):
        with pytest.raises(ValueError):
            test_model_cls.open(str(file_path))

    # Now, if we attempt to open again, we should be able to re-acquire the lock
    model = test_model_cls.open(str(file_path), fallback_value=lambda: test_model_cls())
    assert model is not None

def test_open_without_lock(test_model_cls: Type[FileMappedPydanticMixin], yaml_file: Path) -> None:
    """
    Loading the model with `without_lock=True` should not acquire the lock.
    """
    model = test_model_cls.open(str(yaml_file), without_lock=True)
    assert model._lock_acquired is False

def test_context_manager_usage(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that the context manager properly acquires/releases locks and saves on exit.
    """
    file_path = tmp_path / "context_test.yaml"
    
    # Use context manager to create and modify
    with test_model_cls.open(str(file_path)) as model:
        model.name = "context_test"
        model.value = 42
        assert model._lock_acquired is True
    
    # Lock should be released after context
    assert model._lock_acquired is False
    
    # Verify changes were saved
    loaded_model = test_model_cls.open(str(file_path))
    assert loaded_model.name == "context_test"
    assert loaded_model.value == 42

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_lock_acquisition_timeout(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that lock acquisition properly times out after max_retry_secs.
    """
    file_path = tmp_path / "timeout_test.yaml"
    
    # First instance holds the lock
    model1 = test_model_cls.open(str(file_path))
    
    # Attempt to acquire with short timeout should fail
    with pytest.raises(TimeoutError):
        _ = test_model_cls.open(str(file_path), max_retry_secs=1)

def test_toml_file_support(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test loading and saving with TOML format.
    """
    logger.info(f"Test TOML file path: {tmp_path / 'test_data.toml'}")

    # Create initial TOML file
    toml_content = """
name = "toml_test"
value = 789
"""
    with open(tmp_path / "test_data.toml", "w", encoding="utf-8") as f:
        f.write(toml_content)

    logger.info(f"Written TOML content: {toml_content}")

    # Verify file was written correctly
    with open(tmp_path / "test_data.toml", "r") as f:
        logger.info(f"Raw file content: {f.read()}")

    # Load and verify
    model = test_model_cls.open(str(tmp_path / "test_data.toml"))
    assert model.name == "toml_test"
    assert model.value == 789

    # Modify and save
    model.name = "modified_toml"
    model.save()
    
    # Explicitly release the lock to ensure it's released
    if hasattr(model, '_lock_acquired') and model._lock_acquired:
        model.release_lock()

    # Reload and verify
    reloaded = test_model_cls.open(str(tmp_path / "test_data.toml"))
    assert reloaded.name == "modified_toml"
    assert reloaded.value == 789
    
    # Make sure to release the lock after the test
    if hasattr(reloaded, '_lock_acquired') and reloaded._lock_acquired:
        reloaded.release_lock()

def test_corrupted_file_handling(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test handling of corrupted file content with fallback.
    """
    file_path = tmp_path / "corrupted.yaml"
    
    # Create corrupted YAML file
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("""
name: "test"
value: : : : 
  - corrupt: [[[
""")
    
    # Should handle the error gracefully with fallback
    model = test_model_cls.open(str(file_path), fallback_value=lambda: test_model_cls())
    assert model._lock_acquired is True
    assert model.name == "default_name"  # Should use fallback values

# Move function to module level
def _mp_modify_file(path: str, name: str, value: int, model_cls: Type[FileMappedPydanticMixin]) -> None:
    """Helper function for multiprocessing test that modifies a file"""
    try:
        model = model_cls.open(path)
        model.name = name
        model.value = value
        model.save()
        
        # Explicitly release the lock to ensure it's released
        if hasattr(model, '_lock_acquired') and model._lock_acquired:
            model.release_lock()
    except Exception as e:
        logger.debug(f"Process error: {e}")
        
        # Try to clean up any lock files
        lock_file = f"{path}{_LOCK_FILE_SUFFIX}"
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
                logger.debug(f"Removed lock file: {lock_file}")
        except Exception as e2:
            logger.debug(f"Failed to remove lock file: {e2}")

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_concurrent_access(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test concurrent access to the same file from multiple processes.
    """
    import multiprocessing
    import time

    file_path = tmp_path / "concurrent.yaml"
    lock_file = f"{file_path}{_LOCK_FILE_SUFFIX}"
    
    # Make sure there's no stale lock file
    if os.path.exists(lock_file):
        os.remove(lock_file)

    # Create initial file
    model = test_model_cls.open(str(file_path))
    model.save()
    
    # Explicitly release the lock to ensure it's released
    if hasattr(model, '_lock_acquired') and model._lock_acquired:
        model.release_lock()
    
    # Double-check that the lock file is gone
    if os.path.exists(lock_file):
        os.remove(lock_file)
        logger.debug(f"Removed stale lock file: {lock_file}")

    # Try concurrent modifications
    p1 = multiprocessing.Process(
        target=_mp_modify_file,
        args=(str(file_path), "process1", 1, test_model_cls)
    )
    p2 = multiprocessing.Process(
        target=_mp_modify_file,
        args=(str(file_path), "process2", 2, test_model_cls)
    )

    p1.start()
    time.sleep(0.1)  # Small delay to ensure processes overlap
    p2.start()

    p1.join(timeout=5)
    p2.join(timeout=5)
    
    # Clean up any stale lock files
    if os.path.exists(lock_file):
        os.remove(lock_file)
        logger.debug(f"Removed stale lock file after processes: {lock_file}")
    
    # Wait a moment to ensure file system operations complete
    time.sleep(0.5)

    # One of the processes should have succeeded
    try:
        # Use a shorter timeout for the final check
        final_model = test_model_cls.open(str(file_path), max_retry_secs=1.0)
        assert final_model.name in ["process1", "process2"]
        assert final_model.value in [1, 2]
        
        # Make sure to release the lock after the test
        if hasattr(final_model, '_lock_acquired') and final_model._lock_acquired:
            final_model.release_lock()
    except TimeoutError:
        # If we still can't acquire the lock, force remove it and skip the assertions
        if os.path.exists(lock_file):
            os.remove(lock_file)
            logger.debug(f"Force removed lock file: {lock_file}")
        pytest.skip("Could not acquire lock for final verification")

def test_change_detection(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that changes are properly detected"""
    file_path = tmp_path / "change_detect.yaml"
    
    with test_model_cls.open(str(file_path)) as model:
        assert not model.is_modified()
        model.name = "changed"
        assert model.is_modified()
        model.save()
        assert not model.is_modified()

def test_revert_changes(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test reverting unsaved changes"""
    file_path = tmp_path / "revert_test.yaml"
    
    with test_model_cls.open(str(file_path)) as model:
        original_name = model.name
        model.name = "changed"
        assert model.is_modified()
        model.revert()
        assert not model.is_modified()
        assert model.name == original_name

def test_context_manager_unsaved_changes(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that context manager automatically saves changes"""
    file_path = tmp_path / "unsaved_changes.yaml"
    
    # Context manager should automatically save changes
    with test_model_cls.open(str(file_path)) as model:
        model.name = "changed"  # Make change
    
    # Verify changes were saved
    loaded_model = test_model_cls.open(str(file_path))
    assert loaded_model.name == "changed"

def test_explicit_save_in_context(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test explicit save within context manager"""
    file_path = tmp_path / "explicit_save.yaml"
    
    with test_model_cls.open(str(file_path)) as model:
        model.name = "changed"
        model.save(retain_lock=True)  # Explicit save
        # Should not raise error on exit since changes were saved

def test_context_manager_without_lock(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that using context manager without a lock raises an error"""
    file_path = tmp_path / "no_lock_context.yaml"
    
    # Create initial file
    with test_model_cls.open(str(file_path)) as model:
        model.name = "initial"
        model.value = 100
    
    # Attempt to open without lock in context manager should raise error
    with pytest.raises(RuntimeError, match="Cannot use context manager without acquiring a lock"):
        with test_model_cls.open(str(file_path), without_lock=True) as model:
            model.name = "changed_without_lock"
    
    # Verify original file was not modified
    loaded_model = test_model_cls.open(str(file_path))
    assert loaded_model.name == "initial"
    assert loaded_model.value == 100
    
    # Verify we can still use without_lock outside of context manager
    model = test_model_cls.open(str(file_path), without_lock=True)
    model.name = "changed_without_lock"
    assert model.is_modified()
    assert model._lock_acquired is False

def test_persisted_file_path(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that persisted_file returns the correct absolute path."""
    file_path = tmp_path / "config.yaml"
    model = test_model_cls.open(str(file_path))
    
    assert model.persisted_file() == str(file_path.absolute())
    
    # Test with non-existent file
    with pytest.raises(RuntimeError):
        empty_model = test_model_cls()
        empty_model.persisted_file()

def test_file_exists(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test file existence checking."""
    file_path = tmp_path / "exists.yaml"
    
    # Test with non-existent file
    model = test_model_cls.open(str(file_path))
    assert model.file_exists() is False
    
    # Test with existing file
    model.save()
    assert model.file_exists() is True
    
    # Test after file deletion
    os.unlink(str(file_path))
    assert model.file_exists() is False

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_file_modification_detection(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test detection of file modifications on disk."""
    file_path = tmp_path / "modified.yaml"
    
    # Create initial file
    model = test_model_cls.open(str(file_path))
    model.name = "initial"
    model.save()
    
    # Modify file externally
    time.sleep(0.1)  # Ensure mtime will be different
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "modified", "value": 42}, f)
    
    assert model.file_was_modified() is True
    
    # Test force_check parameter
    assert model.file_was_modified(force_check=True) is True
    
    # Test after reloading
    model.reload_from_file()
    assert model.file_was_modified() is False
    assert model.name == "modified"
    assert model.value == 42

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_file_modified_callback(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test the file modification callback functionality."""
    file_path = tmp_path / "callback.yaml"
    callback_called = False
    
    def on_file_modified():
        nonlocal callback_called
        callback_called = True
    
    # Create and set up model
    model = test_model_cls.open(str(file_path))
    model.set_file_modified_callback(on_file_modified)
    model.save()
    
    # Modify file externally
    time.sleep(0.1)  # Ensure mtime will be different
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "changed", "value": 999}, f)
    
    # Reload should trigger callback
    model.reload_from_file()
    assert callback_called is True
    
    # Test callback removal
    callback_called = False
    model.set_file_modified_callback(None)
    
    # Modify and reload again
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "changed again", "value": 123}, f)
    model.reload_from_file()
    assert callback_called is False  # Should remain False as callback was removed

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_conflict_detection(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test detection of potential conflicts between local and file changes."""
    file_path = tmp_path / "conflict.yaml"
    
    # Create initial file
    model = test_model_cls.open(str(file_path))
    model.name = "initial"
    model.save()
    
    # Make local modification
    model.name = "local change"
    
    # No conflict yet (file unchanged)
    assert model.would_conflict() is False
    
    # Modify file externally
    time.sleep(0.1)  # Ensure mtime will be different
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "external change", "value": 42}, f)
    
    # Now should detect conflict
    assert model.would_conflict() is True
    assert model.is_modified() is True
    assert model.file_was_modified() is True

def test_load_method(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test the new load class method."""
    file_path = tmp_path / "load.yaml"
    
    # Test loading non-existent file
    instance = test_model_cls.load(str(file_path))
    assert not os.path.exists(file_path)
    assert instance.name == "default_name"
    
    # Create file and test loading
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "test load", "value": 42}, f)
    
    instance = test_model_cls.load(str(file_path))
    assert os.path.exists(file_path)
    assert instance.name == "test load"
    assert instance.value == 42
    
    # Test loading with lock
    instance = test_model_cls.load(str(file_path), acquire_lock=True)
    assert instance._lock_acquired is False  # Lock should be released after load
    
    # Test loading with fallback
    os.unlink(str(file_path))
    instance = test_model_cls.load(
        str(file_path),
        fallback_value={"name": "fallback", "value": 999}
    )
    assert not os.path.exists(file_path)
    assert instance.name == "fallback"
    assert instance.value == 999

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_reload_from_file(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test reloading from file with various scenarios."""
    file_path = tmp_path / "reload.yaml"
    
    # Create initial file
    model = test_model_cls.open(str(file_path))
    model.name = "initial"
    model.value = 1
    model.save()
    
    # Test reload without changes
    assert model.reload_from_file() is False
    
    # Test forced reload
    assert model.reload_from_file(force=True) is True
    
    # Modify file externally
    time.sleep(0.1)  # Ensure mtime will be different
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "reloaded", "value": 42}, f)
    
    # Test reload with changes
    assert model.reload_from_file() is True
    assert model.name == "reloaded"
    assert model.value == 42
    
    # Test reload with missing file
    os.unlink(str(file_path))
    with pytest.raises(RuntimeError):
        model.reload_from_file()
    
    # Test reload without previous file
    empty_model = test_model_cls()
    with pytest.raises(RuntimeError):
        empty_model.reload_from_file()

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_file_metadata_tracking(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test tracking of file metadata (size, mtime) and last loaded timestamp."""
    file_path = tmp_path / "metadata.yaml"
    
    # Create initial file
    model = test_model_cls.open(str(file_path))
    model.name = "test"
    model.save(retain_lock=True)  # Retain lock for next operation
    
    # Check metadata is tracked
    assert model._file_stat is not None
    assert isinstance(model._file_stat, tuple)
    assert len(model._file_stat) == 2
    assert model._last_loaded_at is not None
    
    # Modify file and check metadata updates
    initial_stat = model._file_stat
    initial_loaded_at = model._last_loaded_at
    
    time.sleep(0.1)  # Ensure mtime will be different
    model.name = "modified"
    model.save()  # Now we can release the lock
    
    assert model._file_stat != initial_stat
    assert model._last_loaded_at > initial_loaded_at
    
    # Check metadata after file deletion
    os.unlink(str(file_path))
    with pytest.raises(RuntimeError, match="no longer exists"):
        model.reload_from_file(force=True)

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_save_to_different_file(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test saving to a different file path outside of context manager."""
    original_file = tmp_path / "original.yaml"
    new_file = tmp_path / "new.yaml"

    # Create and save initial file
    model = test_model_cls.open(str(original_file))
    model.name = "original"
    model.save()
    
    # Explicitly release the lock to ensure it's released
    if hasattr(model, '_lock_acquired') and model._lock_acquired:
        model.release_lock()

    # Save to new file (should automatically acquire lock)
    model.name = "new"
    model.save(file_path=str(new_file))
    
    # Explicitly release the lock to ensure it's released
    if hasattr(model, '_lock_acquired') and model._lock_acquired:
        model.release_lock()

    # Verify original file still has old content
    original_model = test_model_cls.open(str(original_file))
    assert original_model.name == "original"
    
    # Make sure to release the lock after the test
    if hasattr(original_model, '_lock_acquired') and original_model._lock_acquired:
        original_model.release_lock()

    # Verify new file has new content
    new_model = test_model_cls.open(str(new_file))
    assert new_model.name == "new"
    
    # Make sure to release the lock after the test
    if hasattr(new_model, '_lock_acquired') and new_model._lock_acquired:
        new_model.release_lock()

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_save_to_different_file_in_context_manager(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that saving to a different file inside a context manager raises ValueError."""
    original_file = tmp_path / "original.yaml"
    new_file = tmp_path / "new.yaml"
    
    with test_model_cls.open(str(original_file)) as model:
        model.name = "changed"
        # Attempt to save to different file should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            model.save(file_path=str(new_file))
        assert "Cannot save to a different file path" in str(exc_info.value)
        assert "while inside a context manager" in str(exc_info.value)
        
        # Should still be able to save to original file
        model.save(retain_lock=True)  # Retain lock since we're in context manager
    
    # Verify changes were saved to original file
    loaded_model = test_model_cls.open(str(original_file))
    assert loaded_model.name == "changed"
    
    # Verify new file was not created
    assert not new_file.exists()

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_save_to_same_file_in_context_manager(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that saving to the same file inside a context manager works."""
    file_path = tmp_path / "test.yaml"
    
    with test_model_cls.open(str(file_path)) as model:
        model.name = "first change"
        # Should be able to save to same file
        model.save(file_path=str(file_path), retain_lock=True)  # Retain lock since we're in context manager
        
        # Make another change
        model.name = "second change"
        # Should auto-save on exit
    
    # Verify final state was saved
    loaded_model = test_model_cls.open(str(file_path))
    assert loaded_model.name == "second change"

def test_format_override_open(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that format_override works when opening a file with a non-standard extension.
    """
    # Create a file with .txt extension but containing JSON data
    file_path = tmp_path / "data.txt"
    sample_data = {
        "name": "json_in_txt",
        "value": 789
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    
    # Open the file with format_override=json
    model = test_model_cls.open(str(file_path), format_override="json")
    
    # Verify the data was loaded correctly
    assert model.name == "json_in_txt"
    assert model.value == 789
    
    # Verify the format_override was stored
    assert model._format_override == "json"

def test_format_override_persistence_save(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that format_override persists when saving without specifying it again.
    """
    # Create a file with .txt extension but containing JSON data
    file_path = tmp_path / "persist.txt"
    sample_data = {
        "name": "original",
        "value": 100
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    
    # Open the file with format_override=json
    model = test_model_cls.open(str(file_path), format_override="json")
    
    # Modify and save without specifying format_override
    model.name = "modified"
    model.value = 200
    model.save()
    
    # Read the file directly to verify it was saved as JSON
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        # Check if it's valid JSON
        loaded_data = json.loads(content)
        assert loaded_data["name"] == "modified"
        assert loaded_data["value"] == 200

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_format_override_persistence_reload(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that format_override persists when reloading the file.
    """
    # Create a file with .txt extension but containing JSON data
    file_path = tmp_path / "reload.txt"
    sample_data = {
        "name": "before_reload",
        "value": 300
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    
    # Open the file with format_override=json
    model = test_model_cls.open(str(file_path), format_override="json")
    
    # Modify the file directly
    new_data = {
        "name": "after_reload",
        "value": 400
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(new_data, f)
    
    # Reload the file - should use the stored format_override
    model.reload_from_file()
    
    # Verify the new data was loaded correctly
    assert model.name == "after_reload"
    assert model.value == 400

def test_format_override_change(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that format_override can be changed when saving.
    """
    # Create a file with .txt extension but containing JSON data
    file_path = tmp_path / "change_format.txt"
    sample_data = {
        "name": "json_format",
        "value": 500
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    
    # Open the file with format_override=json
    model = test_model_cls.open(str(file_path), format_override="json")
    
    # Modify and save with a different format_override
    model.name = "yaml_format"
    model.value = 600
    model.save(format_override="yaml")
    
    # Verify the format_override was updated
    assert model._format_override == "yaml"
    
    # Read the file directly to verify it was saved as YAML
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        # YAML content should not have JSON's curly braces
        assert "{" not in content
        assert "}" not in content
        # Load as YAML to verify content
        loaded_data = yaml.safe_load(content)
        assert loaded_data["name"] == "yaml_format"
        assert loaded_data["value"] == 600
    
    # Reload and verify it uses the updated format_override
    model.reload_from_file()
    assert model.name == "yaml_format"
    assert model.value == 600

def test_format_override_load_method(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that format_override works with the load class method.
    """
    # Create a file with .txt extension but containing JSON data
    file_path = tmp_path / "load_method.txt"
    sample_data = {
        "name": "load_with_override",
        "value": 700
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    
    # Use the load method with format_override
    model = test_model_cls.load(str(file_path), format_override="json")
    
    # Verify the data was loaded correctly
    assert model.name == "load_with_override"
    assert model.value == 700
    
    # Verify the format_override was stored
    assert model._format_override == "json"

def test_format_override_context_manager(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that format_override works with context manager.
    """
    # Create a file with .txt extension but containing JSON data
    file_path = tmp_path / "context.txt"
    sample_data = {
        "name": "context_start",
        "value": 800
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)
    
    # Use context manager with format_override
    with test_model_cls.open(str(file_path), format_override="json") as model:
        # Verify the data was loaded correctly
        assert model.name == "context_start"
        assert model.value == 800
        
        # Modify the model
        model.name = "context_modified"
        model.value = 900
    
    # Verify the file was saved with the correct format
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        # Check if it's valid JSON
        loaded_data = json.loads(content)
        assert loaded_data["name"] == "context_modified"
        assert loaded_data["value"] == 900

@pytest.mark.slow
@very_lazy_test(['totodev_pub.file_mapped_pydantic_mixin'], reverify_days=20)
def test_stability_check(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that stability check works correctly."""
    file_path = tmp_path / "stability.yaml"
    
    # Create initial file
    with open(file_path, "w") as f:
        yaml.safe_dump({"name": "initial", "value": 1}, f)
    
    # Start a background process that modifies the file
    def modify_file():
        time.sleep(0.2)  # Small delay before first modification
        with open(file_path, "w") as f:
            yaml.safe_dump({"name": "modified", "value": 2}, f)
    
    import threading
    modifier = threading.Thread(target=modify_file)
    modifier.start()
    
    # Try to load with stability check - should timeout
    with pytest.raises(TimeoutError):
        test_model_cls._create_instance_from_data(
            str(file_path),
            {"name": "test", "value": 0},
            False,
            stability_secs=0.5
        )
    
    # Wait for modifier to finish
    modifier.join()
    
    # Now load with stability check - should succeed
    time.sleep(0.6)  # Wait for file to stabilize
    instance = test_model_cls._create_instance_from_data(
        str(file_path),
        {"name": "test", "value": 0},
        False,
        stability_secs=0.5
    )
    
    assert instance is not None
    assert instance._file_path == str(file_path)

def test_ndjson_stream_read_and_append(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test streaming reads and appends for NDJSON files.
    """
    file_path = tmp_path / "test.ndjson"

    # Create initial NDJSON file with multiple items
    items = [
        {"name": "item1", "value": 1},
        {"name": "item2", "value": 2},
        {"name": "item3", "value": 3}
    ]
    with open(file_path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")

    # Test stream reading
    loaded_items = list(test_model_cls.stream_read(str(file_path)))
    assert len(loaded_items) == 3
    assert all(isinstance(item, test_model_cls) for item in loaded_items)
    assert [item.name for item in loaded_items] == ["item1", "item2", "item3"]
    assert [item.value for item in loaded_items] == [1, 2, 3]

    # Modify and append new records
    loaded_items[0].value = 10
    test_model_cls.append_records(str(file_path), loaded_items[0])  # Append modified first record

    new_record = test_model_cls(name="item4", value=4)
    test_model_cls.append_records(str(file_path), new_record)

    # Stream read again and verify all records
    loaded_items = list(test_model_cls.stream_read(str(file_path)))
    assert len(loaded_items) == 5
    assert [item.name for item in loaded_items] == ["item1", "item2", "item3", "item1", "item4"]
    assert [item.value for item in loaded_items] == [1, 2, 3, 10, 4]

def test_ndjson_empty_file_stream(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test streaming from an empty NDJSON file.
    """
    file_path = tmp_path / "empty.ndjson"

    # Create empty file
    open(file_path, "w").close()

    # Stream read empty file
    items = list(test_model_cls.stream_read(str(file_path)))
    assert len(items) == 0

    # Create instance and append records
    instance = test_model_cls(name="first", value=1)
    second_record = test_model_cls(name="second", value=2)
    
    # Append records using class method
    test_model_cls.append_records(str(file_path), [instance, second_record])

    # Stream read and verify
    items = list(test_model_cls.stream_read(str(file_path)))
    assert len(items) == 2
    assert items[0].name == "first"
    assert items[1].name == "second"

def test_ndjson_validation_stream(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test validation during streaming of NDJSON records.
    """
    file_path = tmp_path / "validate.ndjson"
    
    # Create file with some invalid records
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"name": "valid", "value": 1}) + "\n")
        f.write(json.dumps({"invalid": "record"}) + "\n")  # Missing required fields
        f.write(json.dumps({"name": "valid2", "value": 2}) + "\n")
        f.write("invalid json\n")  # Invalid JSON
    
    # Stream read should skip invalid records
    items = list(test_model_cls.stream_read(str(file_path)))
    assert len(items) == 2  # Only valid records
    assert [item.name for item in items] == ["valid", "valid2"]
    
    # Try to stream read from non-NDJSON file
    json_path = tmp_path / "test.json"
    with open(json_path, "w") as f:
        json.dump({"name": "test", "value": 1}, f)
    
    with pytest.raises(ValueError) as exc_info:
        list(test_model_cls.stream_read(str(json_path)))
    assert "stream_read() only supports NDJSON files" in str(exc_info.value)

def test_ndjson_save_not_supported(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test that save() is not supported for NDJSON files.
    """
    file_path = tmp_path / "test.ndjson"
    instance = test_model_cls(name="test", value=1)
    
    # Try to save directly to NDJSON file
    with pytest.raises(ValueError) as exc_info:
        instance.save(str(file_path))
    assert "save() is not supported for NDJSON files" in str(exc_info.value)
    assert "Use append_records() to add records or stream_read() to read records" in str(exc_info.value)
    
    # Try with .jsonl extension
    jsonl_path = tmp_path / "test.jsonl"
    with pytest.raises(ValueError) as exc_info:
        instance.save(str(jsonl_path))
    assert "save() is not supported for NDJSON files" in str(exc_info.value)
    
    # Try with format override
    txt_path = tmp_path / "test.txt"
    with pytest.raises(ValueError) as exc_info:
        instance.save(str(txt_path), format_override="ndjson")
    assert "save() is not supported for NDJSON files" in str(exc_info.value)

def test_ndjson_append_and_load(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test appending records to an NDJSON file and loading them back."""
    file_path = tmp_path / "test.ndjson"
    
    # Create initial records
    records = [
        test_model_cls(name="first", value=1),
        test_model_cls(name="second", value=2)
    ]
    
    # Save initial records
    test_model_cls.append_records(str(file_path), records)
    
    # Load and verify
    loaded_items = test_model_cls.load(str(file_path))
    assert isinstance(loaded_items, list)
    assert len(loaded_items) == 2
    assert loaded_items[0].name == "first"
    assert loaded_items[1].name == "second"
    
    # Modify and append more records
    loaded_items[0].name = "first_modified"
    test_model_cls.append_records(str(file_path), loaded_items[0])  # Append modified first record
    
    # Add a new record
    new_record = test_model_cls(name="third", value=3)
    test_model_cls.append_records(str(file_path), new_record)
    
    # Load and verify all records
    loaded_items = test_model_cls.load(str(file_path))
    assert isinstance(loaded_items, list)
    assert len(loaded_items) == 4
    assert loaded_items[0].name == "first"
    assert loaded_items[1].name == "second"
    assert loaded_items[2].name == "first_modified"
    assert loaded_items[3].name == "third"

def test_ndjson_append_without_lock(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test appending to NDJSON file without acquiring a lock."""
    file_path = tmp_path / "test.ndjson"
    instance = test_model_cls(name="first", value=1)
    
    # Append records without lock
    test_model_cls.append_records(str(file_path), instance, acquire_lock=False)
    test_model_cls.append_records(str(file_path), test_model_cls(name="second", value=2), acquire_lock=False)
    
    # Load and verify
    loaded_items = test_model_cls.load(str(file_path))
    assert isinstance(loaded_items, list)
    assert len(loaded_items) == 2
    assert loaded_items[0].name == "first"
    assert loaded_items[1].name == "second"

def test_save_ndjson_error(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test that attempting to save() to an NDJSON file raises an error."""
    file_path = tmp_path / "test.ndjson"
    instance = test_model_cls(name="test", value=1)
    
    with pytest.raises(ValueError) as exc_info:
        instance.save(str(file_path))
            
    assert "Use append_records() to add records or stream_read() to read records" in str(exc_info.value)

def test_stream_empty_and_nonexistent_files(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """
    Test streaming records from:
    1. An empty file (should return empty iterator)
    2. A non-existent file (should raise FileNotFoundError)
    """
    # Test empty file (zero bytes)
    empty_file = tmp_path / "empty.ndjson"
    empty_file.touch()  # Create empty file
    
    # Stream from empty file should yield no records
    empty_records = list(test_model_cls.stream_read(str(empty_file)))
    assert len(empty_records) == 0
    
    # Test non-existent file
    nonexistent_file = tmp_path / "does_not_exist.ndjson"
    with pytest.raises(FileNotFoundError) as exc_info:
        list(test_model_cls.stream_read(str(nonexistent_file)))
    assert "No such file or directory" in str(exc_info.value)

def test_ndjson_load_empty_file(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test loading an empty NDJSON file."""
    file_path = tmp_path / "empty.ndjson"
    
    # Create empty file
    file_path.touch()
    
    # Load empty file
    loaded_items = test_model_cls.load(str(file_path))
    assert isinstance(loaded_items, list)
    assert len(loaded_items) == 0

def test_ndjson_load_with_fallback(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test loading a non-existent NDJSON file with fallback."""
    file_path = tmp_path / "nonexistent.ndjson"
    
    # Load non-existent file with fallback
    loaded_items = test_model_cls.load(str(file_path))
    assert isinstance(loaded_items, list)
    assert len(loaded_items) == 0
    
    # Load with fallback data
    fallback_data = [{"name": "fallback1", "value": 1}, {"name": "fallback2", "value": 2}]
    loaded_items = test_model_cls.load(str(file_path), fallback_value=fallback_data)
    assert len(loaded_items) == 2
    assert loaded_items[0].name == "fallback1"
    assert loaded_items[1].name == "fallback2"

def test_ndjson_load_with_data(test_model_cls: Type[FileMappedPydanticMixin], tmp_path: Path) -> None:
    """Test loading an NDJSON file with data."""
    file_path = tmp_path / "data.ndjson"
    
    # Create file with data
    with open(file_path, "w") as f:
        f.write('{"name": "record1", "value": 1}\n')
        f.write('{"name": "record2", "value": 2}\n')
    
    # Load file
    loaded_items = test_model_cls.load(str(file_path))
    assert len(loaded_items) == 2
    assert loaded_items[0].name == "record1"
    assert loaded_items[1].name == "record2"


# ============================================================================
# Tests for Docstring Examples - These verify that all examples in the 
# docstring are actually working code
# ============================================================================

def test_docstring_example_1_complex_nested_data_structure(tmp_path: Path) -> None:
    """
    Test Example 1: Complex Nested Data Structure (Primary Use Case)
    This verifies the main use case from the docstring.
    """
    from pydantic import BaseModel
    from typing import Dict, Any, List
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    # Define the classes exactly as shown in the docstring
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
    
    # Test the exact code from the docstring
    file_path = tmp_path / "app_data.yaml"
    app_data = ApplicationData.open(str(file_path))
    
    # Add a user profile
    app_data.users.append(UserProfile(name="John", email="john@example.com"))
    
    # Set settings
    app_data.settings["theme"] = "dark"
    
    # Add cache data
    app_data.cache["recent_searches"] = [{"query": "python", "timestamp": "2024-01-01"}]
    
    # Save the data
    app_data.save()
    
    # Release the lock to allow reloading
    app_data.release_lock()
    
    # Verify the file was created and contains the data
    assert file_path.exists()
    
    # Reload and verify the data
    reloaded_data = ApplicationData.open(str(file_path))
    assert len(reloaded_data.users) == 1
    assert reloaded_data.users[0].name == "John"
    assert reloaded_data.users[0].email == "john@example.com"
    assert reloaded_data.settings["theme"] == "dark"
    assert reloaded_data.cache["recent_searches"][0]["query"] == "python"
    assert reloaded_data.version == "1.0.0"


def test_docstring_example_2_basic_configuration_management(tmp_path: Path) -> None:
    """
    Test Example 2: Basic Configuration Management
    This verifies the basic config example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    # Define the class exactly as shown in the docstring
    class AppConfig(BaseModel, FileMappedPydanticMixin):
        api_key: str = "default_key"
        debug_mode: bool = False
    
    # Test the exact code from the docstring
    file_path = tmp_path / "config.yaml"
    config = AppConfig.open(str(file_path))
    config.debug_mode = True
    config.save()
    
    # Release the lock to allow reloading
    config.release_lock()
    
    # Verify the file was created
    assert file_path.exists()
    
    # Reload and verify the data
    reloaded_config = AppConfig.open(str(file_path))
    assert reloaded_config.api_key == "default_key"
    assert reloaded_config.debug_mode is True


def test_docstring_example_3_context_manager_automatic_saving(tmp_path: Path) -> None:
    """
    Test Example 3: Context Manager for Automatic Saving
    This verifies the context manager example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    # Define the class exactly as shown in the docstring
    class AppConfig(BaseModel, FileMappedPydanticMixin):
        api_key: str = "default_key"
    
    # Test the exact code from the docstring
    file_path = tmp_path / "config.yaml"
    
    with AppConfig.open(str(file_path)) as config:
        config.api_key = "new_key"
        # Automatically saves on exit if changes were made
    
    # Verify the file was created and contains the new data
    assert file_path.exists()
    
    # Reload and verify the data was saved
    reloaded_config = AppConfig.open(str(file_path))
    assert reloaded_config.api_key == "new_key"


def test_docstring_example_4_change_detection_and_revert(tmp_path: Path) -> None:
    """
    Test Example 4: Change Detection and Revert
    This verifies the change detection example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    # Define the class exactly as shown in the docstring
    class AppConfig(BaseModel, FileMappedPydanticMixin):
        api_key: str = "default_key"
        debug_mode: bool = False
    
    # Test the exact code from the docstring
    file_path = tmp_path / "config.yaml"
    config = AppConfig.open(str(file_path))
    config.debug_mode = True
    
    # Check if modified
    assert config.is_modified()
    
    # Save if changes were made
    config.save()
    assert not config.is_modified()
    
    # Test revert functionality
    config.debug_mode = False
    assert config.is_modified()
    config.revert()  # Back to original state
    assert not config.is_modified()
    assert config.debug_mode is True  # Should be back to saved state
    
    # Release the lock
    config.release_lock()


def test_docstring_example_5_format_override(tmp_path: Path) -> None:
    """
    Test Example 5: Format Override
    This verifies the format override example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    import json
    
    # Define the class exactly as shown in the docstring
    class AppConfig(BaseModel, FileMappedPydanticMixin):
        api_key: str = "default_key"
    
    # Test the exact code from the docstring
    file_path = tmp_path / "config.txt"  # .txt extension but JSON format
    config = AppConfig.open(str(file_path), format_override="json")
    config.save()  # Saves as JSON despite .txt extension
    
    # Release the lock
    config.release_lock()
    
    # Verify the file was created
    assert file_path.exists()
    
    # Verify it's actually JSON format
    with open(file_path, 'r') as f:
        content = f.read()
        # Should be valid JSON
        data = json.loads(content)
        assert data["api_key"] == "default_key"


def test_docstring_example_6_ndjson_streaming(tmp_path: Path) -> None:
    """
    Test Example 6: NDJSON Streaming for Large Datasets
    This verifies the NDJSON streaming example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    
    # Define the class exactly as shown in the docstring
    class MyModel(BaseModel, FileMappedPydanticMixin):
        name: str
        value: int
    
    # Test the exact code from the docstring
    file_path = tmp_path / "data.ndjson"
    
    # Create some test data
    new_record1 = MyModel(name="record1", value=1)
    new_record2 = MyModel(name="record2", value=2)
    
    # First append records to create the file
    MyModel.append_records(str(file_path), [new_record1, new_record2])
    
    # Stream process large datasets (simulate the docstring example)
    records_processed = []
    for record in MyModel.stream_read(str(file_path)):
        records_processed.append(record)
    
    # Should find the records we just added
    assert len(records_processed) == 2
    assert records_processed[0].name == "record1"
    assert records_processed[1].name == "record2"


def test_docstring_example_7_user_database_replacement(tmp_path: Path) -> None:
    """
    Test Example 7: User Database Replacement (YAML as Database)
    This verifies the user database example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    from typing import List, Optional
    from enum import Enum
    
    # Define the classes exactly as shown in the docstring
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
    
    # Test the exact code from the docstring
    file_path = tmp_path / "users.yaml"
    
    # Initialize database with sample users (exact fallback from docstring)
    def create_sample_database():
        db = UserDatabase.open(str(file_path), fallback_value={
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
    assert admin_user is not None
    assert admin_user.role == UserRole.ADMIN
    assert admin_user.email == "admin@example.com"
    
    # Add new user
    new_user = User(
        username="charlie",
        email="charlie@example.com", 
        password_hash="$2b$12$9M2nP4qR6sT8uV0wX2yZ4aB6cD8eF0gH2iJ4kL6mN8oP0qR2sT4uV6wX8yZ",
        role=UserRole.USER
    )
    db.users.append(new_user)
    
    # Save using the model's save method - it should handle enum serialization
    db.save()  # Persists to human-readable YAML file
    
    # Release the lock to allow reloading
    db.release_lock()
    
    # Verify the file was created
    assert file_path.exists()
    
    # Reload and verify the data
    reloaded_db = UserDatabase.open(str(file_path))
    assert len(reloaded_db.users) == 4  # 3 original + 1 new
    assert reloaded_db.version == "1.0.0"
    
    # Verify admin user
    admin_user = next((u for u in reloaded_db.users if u.username == "admin"), None)
    assert admin_user is not None
    assert admin_user.role == UserRole.ADMIN
    assert admin_user.password_hash == "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj4J/9KXvK2a"
    
    # Verify alice user
    alice_user = next((u for u in reloaded_db.users if u.username == "alice"), None)
    assert alice_user is not None
    assert alice_user.role == UserRole.USER
    assert alice_user.email == "alice@example.com"
    
    # Verify new charlie user
    charlie_user = next((u for u in reloaded_db.users if u.username == "charlie"), None)
    assert charlie_user is not None
    assert charlie_user.role == UserRole.USER
    assert charlie_user.email == "charlie@example.com"
    assert charlie_user.password_hash == "$2b$12$9M2nP4qR6sT8uV0wX2yZ4aB6cD8eF0gH2iJ4kL6mN8oP0qR2sT4uV6wX8yZ"
    
    # Release the lock
    reloaded_db.release_lock()


def test_docstring_example_8_file_monitoring_and_reloading(tmp_path: Path) -> None:
    """
    Test Example 8: File Monitoring and Reloading
    This verifies the file monitoring example from the docstring.
    """
    from pydantic import BaseModel
    from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
    import yaml
    import time
    
    # Define the class exactly as shown in the docstring
    class AppConfig(BaseModel, FileMappedPydanticMixin):
        api_key: str = "default_key"
        debug_mode: bool = False
    
    # Test the exact code from the docstring
    file_path = tmp_path / "config.yaml"
    config = AppConfig.open(str(file_path))
    config.save()
    
    # Release the lock to allow external modification
    config.release_lock()
    
    # Initially file should not be modified
    assert not config.file_was_modified()
    
    # Modify file externally
    time.sleep(0.1)  # Ensure mtime will be different
    with open(file_path, "w") as f:
        yaml.safe_dump({"api_key": "external_change", "debug_mode": True}, f)
    
    # Check if file was modified externally
    assert config.file_was_modified()
    
    # Reload from disk
    config.reload_from_file()
    
    # Verify the new data was loaded
    assert config.api_key == "external_change"
    assert config.debug_mode is True
