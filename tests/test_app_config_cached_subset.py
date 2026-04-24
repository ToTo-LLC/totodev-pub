# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
import tempfile
from datetime import datetime, timedelta
import time
import pytest
import logging

from totodev_pub.pipes.app_config_cached_subset import AppConfigCachedSubset

# Set up logging for the test module
logger = logging.getLogger(__name__)

@pytest.fixture(autouse=True)
def cleanup_lock_files():
    """Cleanup any stray lock files before and after each test.
    
    This fixture attempts to clean up any .lock files in the temp directory.
    It uses broad exception handling because:
    1. Files might be locked by other processes
    2. Permission issues might prevent deletion
    3. Files might have already been deleted
    In all these cases, we want the test to continue rather than fail.
    """
    # Run before test
    temp_dir = tempfile.gettempdir()
    for filename in os.listdir(temp_dir):
        if filename.endswith('.lock'):
            try:
                os.unlink(os.path.join(temp_dir, filename))
            except OSError as e:
                logger.warning(f"Failed to delete lock file {filename}: {e}")
    
    yield  # Run the test
    
    # Run after test
    for filename in os.listdir(temp_dir):
        if filename.endswith('.lock'):
            try:
                os.unlink(os.path.join(temp_dir, filename))
            except OSError as e:
                logger.warning(f"Failed to delete lock file {filename}: {e}")

def test_basic_cache_operations():
    """Test basic cache operations like adding and retrieving values."""
    test_config = {
        'key1': 'value1',
        'key2': 'value2',
        'key3': 'value3'
    }
    
    keys_to_cache = ['key1', 'key2']
    
    cache = AppConfigCachedSubset(last_regen=datetime.now())
    
    # Test initial state
    assert len(cache.data) == 0
    
    # Test regeneration with subset of keys
    changed = cache.regen(test_config, keys_to_cache)
    assert changed is True
    assert len(cache.data) == 2
    assert cache.data['key1'] == 'value1'
    assert cache.data['key2'] == 'value2'
    assert 'key3' not in cache.data

def test_stale_key_operations():
    """Test operations related to stale key management."""
    test_config = {
        'key1': 'value1',
        'key2': 'value2'
    }
    
    cache = AppConfigCachedSubset(last_regen=datetime.now())
    
    # Add initial data
    cache.regen(test_config, ['key1', 'key2'])
    
    # Wait a bit to make key1 stale
    time.sleep(0.1)
    
    # Purge keys older than 0.05 seconds
    removed = cache.def_purge_old_adds(0.05)
    assert removed is True
    assert len(cache.data) == 0

def test_clear_cache():
    """Test clearing the entire cache."""
    test_config = {
        'key1': 'value1',
        'key2': 'value2'
    }
    
    cache = AppConfigCachedSubset(last_regen=datetime.now())
    
    # Add initial data
    cache.regen(test_config, ['key1', 'key2'])
    assert len(cache.data) == 2
    
    # Clear cache
    cache.clear_cache()
    assert len(cache.data) == 0

@pytest.mark.parametrize("file_format", ['.json', '.yaml'])
def test_file_persistence(file_format):
    """
    Test file persistence operations for different file formats.
    
    Args:
        file_format: The file extension to test ('.json' or '.yaml')
    """
    with tempfile.NamedTemporaryFile(suffix=file_format, delete=False) as temp_file:
        temp_config_file = temp_file.name
        
    try:
        # Create and save cache with nested data to test format capabilities
        with AppConfigCachedSubset.open(temp_config_file, fallback_value={"last_regen": datetime.now()}) as cache1:
            test_data = {
                'key1': 'value1',
                'nested': {
                    'key2': 'value2',
                    'list': [1, 2, 3],
                    'dict': {'a': 1, 'b': 2}
                }
            }
            cache1.regen(test_data, ['key1', 'nested'])
            # Context manager will automatically save and release lock
            
        # Load cache and verify data
        with AppConfigCachedSubset.open(temp_config_file) as cache2:
            assert cache2.data['key1'] == 'value1'
            assert cache2.data['nested']['key2'] == 'value2'
            assert cache2.data['nested']['list'] == [1, 2, 3]
            assert cache2.data['nested']['dict'] == {'a': 1, 'b': 2}
            
            # Modify data
            cache2.regen({'key1': 'new_value'}, ['key1'])
            # Context manager will automatically save and release lock
        
        # Load again and verify updated data
        with AppConfigCachedSubset.open(temp_config_file) as cache3:
            assert cache3.data['key1'] == 'new_value'
            assert cache3.data['nested']['key2'] == 'value2'  # Unchanged nested data
            # Context manager will automatically release lock
    finally:
        os.unlink(temp_config_file)

def test_context_manager():
    """Test context manager functionality."""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as temp_file:
        temp_config_file = temp_file.name
        
    try:
        # Use context manager
        with AppConfigCachedSubset.open(temp_config_file, fallback_value={"last_regen": datetime.now()}) as cache:
            cache.regen({'key1': 'value1'}, ['key1'])
            
        # Verify data was saved
        loaded_cache = AppConfigCachedSubset.open(temp_config_file)
        assert loaded_cache.data['key1'] == 'value1'
    finally:
        os.unlink(temp_config_file)

def test_concurrent_access():
    """Test concurrent access handling."""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as temp_file:
        temp_config_file = temp_file.name
        lock_file = f"{temp_config_file}.lock"
        
    try:
        # Clean up any stale lock files
        if os.path.exists(lock_file):
            os.unlink(lock_file)
            
        # Create initial cache
        cache1 = AppConfigCachedSubset.open(temp_config_file, fallback_value={"last_regen": datetime.now()})
        
        # Try to open with timeout (should fail since cache1 has the lock)
        with pytest.raises(TimeoutError):
            AppConfigCachedSubset.open(temp_config_file, max_retry_secs=0.1)
            
        # Release lock by releasing first instance's lock
        cache1.release_lock()
        
        # Should now be able to open
        cache2 = AppConfigCachedSubset.open(temp_config_file)
        assert isinstance(cache2, AppConfigCachedSubset)
    finally:
        # Clean up both the config file and its lock file
        for file_to_cleanup in [temp_config_file, lock_file]:
            try:
                if os.path.exists(file_to_cleanup):
                    os.unlink(file_to_cleanup)
            except Exception:
                pass 