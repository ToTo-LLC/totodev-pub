# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for LazyLoadedFileData class with multi-format support.
"""

import pytest
import tempfile
import os
import warnings
import time
from pathlib import Path
from types import MappingProxyType
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.pytest_tools import very_lazy_test


class TestLazyLoadedFileData:
    """Test the LazyLoadedFileData class with different file formats."""

    def test_yaml_loading_with_as_dict(self):
        """Test loading YAML files using the new as_dict() method."""
        yaml_content = """
name: "test"
value: 42
nested:
  key: "value"
  list: [1, 2, 3]
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Test that data is not loaded until first access
            assert not lazy_dict._loaded
            
            # Test accessing data using as_dict()
            data = lazy_dict.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            assert data['nested']['key'] == 'value'
            assert data['nested']['list'] == [1, 2, 3]
            
            # Test that data is now loaded
            assert lazy_dict._loaded
            
            # Test dictionary methods on the returned data
            assert len(data) == 3
            assert 'name' in data
            assert list(data.keys()) == ['name', 'value', 'nested']
            
        finally:
            os.unlink(yaml_file)

    def test_yaml_loading_backward_compatibility(self):
        """Test loading YAML files using backward compatibility mode."""
        yaml_content = """
name: "test"
value: 42
nested:
  key: "value"
  list: [1, 2, 3]
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, acts_as_dict_proxy=True)
            
            # Test that data is not loaded until first access
            assert not lazy_dict._loaded
            
            # Test accessing data using dict-like access
            assert lazy_dict['name'] == 'test'
            assert lazy_dict['value'] == 42
            assert lazy_dict['nested']['key'] == 'value'
            assert lazy_dict['nested']['list'] == [1, 2, 3]
            
            # Test that data is now loaded
            assert lazy_dict._loaded
            
            # Test dictionary methods
            assert len(lazy_dict) == 3
            assert 'name' in lazy_dict
            assert list(lazy_dict.keys()) == ['name', 'value', 'nested']
            
        finally:
            os.unlink(yaml_file)

    def test_json_loading_with_as_dict(self):
        """Test loading JSON files using the new as_dict() method."""
        json_content = '{"name": "test", "value": 42, "nested": {"key": "value", "list": [1, 2, 3]}}'

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(json_file)

            # Test that data is not loaded until first access
            assert not lazy_dict._loaded

            # Test accessing data using as_dict()
            data = lazy_dict.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            assert data['nested']['key'] == 'value'
            assert data['nested']['list'] == [1, 2, 3]

            # Test that data is now loaded
            assert lazy_dict._loaded

            # Test dictionary methods on the returned data
            assert len(data) == 3
            assert 'name' in data
            assert list(data.keys()) == ['name', 'value', 'nested']

        finally:
            os.unlink(json_file)

    def test_toml_loading_with_as_dict(self):
        """Test loading TOML files using the new as_dict() method."""
        toml_content = """
name = "test"
value = 42

[nested]
key = "value"
list = [1, 2, 3]
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            f.write(toml_content)
            toml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(toml_file)

            # Test that data is not loaded until first access
            assert not lazy_dict._loaded

            # Test accessing data using as_dict()
            data = lazy_dict.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            assert data['nested']['key'] == 'value'
            assert data['nested']['list'] == [1, 2, 3]

            # Test that data is now loaded
            assert lazy_dict._loaded

            # Test dictionary methods on the returned data
            assert len(data) == 3
            assert 'name' in data
            assert list(data.keys()) == ['name', 'value', 'nested']

        finally:
            os.unlink(toml_file)

    def test_yml_extension_with_as_dict(self):
        """Test loading files with .yml extension using as_dict()."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(yaml_content)
            yml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yml_file)
            data = lazy_dict.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            
        finally:
            os.unlink(yml_file)

    def test_unsupported_format(self):
        """Test that unsupported formats raise ValueError."""
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            txt_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(txt_file)
            with pytest.raises(ValueError, match="Unsupported file format"):
                _ = lazy_dict.as_dict()  # This should trigger the error
        finally:
            os.unlink(txt_file)

    def test_file_not_found(self):
        """Test that missing files raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            lazy_dict = LazyLoadedFileData("nonexistent.yaml")
            _ = lazy_dict.as_dict()  # This should trigger the error

    def test_empty_file(self):
        """Test handling of empty files."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write("")
            empty_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(empty_file)
            # Empty files should result in empty dict
            data = lazy_dict.as_dict()
            assert len(data) == 0
            assert data.get('key') is None
            assert data.get('key', 'default') == 'default'
            
        finally:
            os.unlink(empty_file)

    def test_modification_backward_compatibility(self):
        """Test that the dictionary is immutable even in backward compatibility mode.
        
        The internal cache is now a MappingProxyType, which prevents modifications
        to ensure the read-only nature of the class.
        """
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name
        
        try:
            lazy_dict = LazyLoadedFileData(yaml_file, acts_as_dict_proxy=True)
            
            # Load the data - reading should work fine
            assert lazy_dict['name'] == 'test'
            assert lazy_dict['value'] == 42
            
            # Attempt to modify - should raise TypeError
            with pytest.raises(TypeError, match="'mappingproxy' object does not support item assignment"):
                lazy_dict['name'] = 'modified'
            
            # Attempt to add new key - should raise TypeError
            with pytest.raises(TypeError, match="'mappingproxy' object does not support item assignment"):
                lazy_dict['new_key'] = 'new_value'
            
            # Attempt deletion - should raise TypeError
            with pytest.raises(TypeError, match="'mappingproxy' object does not support item deletion"):
                del lazy_dict['value']
            
            # Verify original data is unchanged
            assert lazy_dict['name'] == 'test'
            assert lazy_dict['value'] == 42
            assert len(lazy_dict) == 2
            
        finally:
            os.unlink(yaml_file)

    def test_modification_with_as_dict(self):
        """Test that as_dict() returns immutable data by default, but mutable=True returns modifiable copy."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Get data using as_dict() (default immutable behavior)
            data_immutable = lazy_dict.as_dict()
            assert data_immutable['name'] == 'test'
            
            # Test that immutable data cannot be modified
            with pytest.raises(TypeError):
                data_immutable['name'] = 'modified'
            
            # Get mutable copy using mutable=True
            data_mutable = lazy_dict.as_dict(mutable=True)
            assert data_mutable['name'] == 'test'
            
            # Modify the mutable copy (should not affect the original)
            data_mutable['name'] = 'modified'
            data_mutable['new_key'] = 'new_value'
            
            # Verify modifications in the mutable copy
            assert data_mutable['name'] == 'modified'
            assert data_mutable['new_key'] == 'new_value'
            assert len(data_mutable) == 3
            
            # Verify original data is unchanged (both immutable and internal)
            original_data = lazy_dict.as_dict()
            assert original_data['name'] == 'test'
            assert 'new_key' not in original_data
            assert len(original_data) == 2
            
        finally:
            os.unlink(yaml_file)

    def test_dict_like_access_disabled_by_default(self):
        """Test that dict-like access is disabled by default and raises AttributeError."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Test that dict-like access raises AttributeError
            with pytest.raises(AttributeError, match="Dict-like access via '__getitem__' is not enabled"):
                _ = lazy_dict['name']
            
            with pytest.raises(AttributeError, match="Dict-like access via '__len__' is not enabled"):
                _ = len(lazy_dict)
            
            with pytest.raises(AttributeError, match="Dict-like access via 'keys' is not enabled"):
                _ = lazy_dict.keys()
            
            # Test that as_dict() still works
            data = lazy_dict.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            
        finally:
            os.unlink(yaml_file)

    def test_convenience_methods(self):
        """Test the new convenience methods."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Test convenience methods before loading
            assert lazy_dict.get_filepath() == yaml_file
            assert lazy_dict.is_loaded() == False
            assert lazy_dict.get_file_format() == 'yaml'
            
            # Test convenience methods after loading
            _ = lazy_dict.as_dict()
            assert lazy_dict.is_loaded() == True
            
        finally:
            os.unlink(yaml_file)

    def test_backward_compatibility(self):
        """Dict-proxy YAML access matches former LazyLoadedYamlDict (acts_as_dict_proxy=True)."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, acts_as_dict_proxy=True)
            assert lazy_dict['name'] == 'test'
            assert lazy_dict['value'] == 42
            
        finally:
            os.unlink(yaml_file)

    def test_format_detection_caching(self):
        """Test that file format is detected and cached correctly."""
        json_content = '{"name": "test"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(json_file)
            
            # Format should not be detected yet
            assert lazy_dict._file_format is None
            
            # Access data to trigger format detection using as_dict()
            _ = lazy_dict.as_dict()
            
            # Format should now be cached
            assert lazy_dict._file_format == 'json'
            
            # Second call should use cached format
            assert lazy_dict._get_file_format() == 'json'
            
        finally:
            os.unlink(json_file)

    def test_lazy_loading_behavior(self):
        """Test that libraries are only imported when needed."""
        # This test verifies that the import functions work correctly
        # by testing that they return the expected functions
        
        from totodev_pub.lazy_loaded_file_data import _import_json, _import_yaml
        
        # Test JSON import
        json_loads, json_dumps = _import_json()
        assert json_loads('{"test": "value"}') == {"test": "value"}
        
        # Test YAML import  
        yaml_loads, yaml_dumps = _import_yaml()
        assert yaml_loads("test: value") == {"test": "value"}

    def test_toml_import_error_handling(self):
        """Test that TOML import errors are handled gracefully."""
        from totodev_pub.lazy_loaded_file_data import _import_toml
        
        # This should not raise an error during import, only when called
        try:
            loads_func, dumps_func = _import_toml()
            # If we get here, TOML libraries are available
            assert loads_func is not None
            assert dumps_func is not None
        except ImportError:
            # TOML libraries not available - this is expected in some environments
            pass

    def test_complex_nested_data_with_as_dict(self):
        """Test loading complex nested data structures using as_dict()."""
        json_content = '''
        {
            "users": [
                {"name": "Alice", "age": 30, "active": true},
                {"name": "Bob", "age": 25, "active": false}
            ],
            "settings": {
                "theme": "dark",
                "notifications": {
                    "email": true,
                    "push": false
                }
            },
            "metadata": {
                "version": "1.0.0",
                "tags": ["production", "stable"]
            }
        }
        '''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(json_file)
            data = lazy_dict.as_dict()
            
            # Test complex nested access
            assert len(data['users']) == 2
            assert data['users'][0]['name'] == 'Alice'
            assert data['users'][1]['active'] is False
            assert data['settings']['theme'] == 'dark'
            assert data['settings']['notifications']['email'] is True
            assert data['metadata']['tags'] == ['production', 'stable']
            
        finally:
            os.unlink(json_file)

    def test_iteration_and_items_backward_compatibility(self):
        """Test dictionary iteration and items() method in backward compatibility mode."""
        yaml_content = """
first: 1
second: 2
third: 3
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, acts_as_dict_proxy=True)
            
            # Test iteration
            keys = list(lazy_dict)
            assert set(keys) == {'first', 'second', 'third'}
            
            # Test items()
            items = list(lazy_dict.items())
            assert len(items) == 3
            assert ('first', 1) in items
            assert ('second', 2) in items
            assert ('third', 3) in items
            
            # Test values()
            values = list(lazy_dict.values())
            assert set(values) == {1, 2, 3}
            
        finally:
            os.unlink(yaml_file)

    def test_iteration_and_items_with_as_dict(self):
        """Test dictionary iteration and items() method using as_dict()."""
        yaml_content = """
first: 1
second: 2
third: 3
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            data = lazy_dict.as_dict()
            
            # Test iteration
            keys = list(data)
            assert set(keys) == {'first', 'second', 'third'}
            
            # Test items()
            items = list(data.items())
            assert len(items) == 3
            assert ('first', 1) in items
            assert ('second', 2) in items
            assert ('third', 3) in items
            
            # Test values()
            values = list(data.values())
            assert set(values) == {1, 2, 3}
            
        finally:
            os.unlink(yaml_file)

    def test_mutable_parameter_in_as_dict(self):
        """Test the mutable parameter in as_dict() method."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Test default behavior (immutable)
            data_immutable = lazy_dict.as_dict()
            assert isinstance(data_immutable, MappingProxyType)
            
            # Test mutable=True
            data_mutable = lazy_dict.as_dict(mutable=True)
            assert isinstance(data_mutable, dict)
            assert not isinstance(data_mutable, MappingProxyType)
            
            # Test that immutable dict cannot be modified
            with pytest.raises(TypeError):
                data_immutable['new_key'] = 'new_value'
            
            # Test that mutable dict can be modified
            data_mutable['new_key'] = 'new_value'
            assert data_mutable['new_key'] == 'new_value'
            
        finally:
            os.unlink(yaml_file)

    def test_min_stability_secs_constant(self):
        """Test that MIN_STABILITY_SECS constant is defined."""
        assert hasattr(LazyLoadedFileData, 'MIN_STABILITY_SECS')
        assert LazyLoadedFileData.MIN_STABILITY_SECS == 0.1

    def test_change_detection_secs_parameter(self):
        """Test the change_detection_secs parameter in constructor."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Test default value
            lazy_dict1 = LazyLoadedFileData(yaml_file)
            assert lazy_dict1._change_detection_secs == 5*60  # 5 minutes
            
            # Test custom value
            lazy_dict2 = LazyLoadedFileData(yaml_file, change_detection_secs=10)
            assert lazy_dict2._change_detection_secs == 10
            
            # Test disabled change detection
            lazy_dict3 = LazyLoadedFileData(yaml_file, change_detection_secs=0)
            assert lazy_dict3._change_detection_secs == 0
            
        finally:
            os.unlink(yaml_file)

    def test_file_metadata_capture(self):
        """Test that file metadata is captured correctly after loading."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Before loading, metadata should be None
            assert lazy_dict._last_file_mtime is None
            assert lazy_dict._last_file_size is None
            
            # Load the file
            _ = lazy_dict.as_dict()
            
            # After loading, metadata should be captured
            assert lazy_dict._last_file_mtime is not None
            assert lazy_dict._last_file_size is not None
            
            # Verify the metadata matches the actual file
            stat = os.stat(yaml_file)
            assert lazy_dict._last_file_mtime == stat.st_mtime
            assert lazy_dict._last_file_size == stat.st_size
            
        finally:
            os.unlink(yaml_file)

    def test_has_changed_method(self):
        """Test the has_changed() method."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Before loading, has_changed() should return False (not "changed", just needs loading)
            assert lazy_dict.has_changed() is False
            
            # Load the file
            _ = lazy_dict.as_dict()
            
            # After loading, file should not have changed
            assert lazy_dict.has_changed() is False
            
            # Modify the file
            time.sleep(0.2)  # Ensure different mtime
            with open(yaml_file, 'w') as f:
                f.write("name: modified\nvalue: 100")
            
            # Now has_changed() should return True
            assert lazy_dict.has_changed() is True
            
            # Test with non-existent file
            os.unlink(yaml_file)
            with pytest.raises(FileNotFoundError):
                lazy_dict.has_changed()
            
        finally:
            # Clean up in case of early exit
            if os.path.exists(yaml_file):
                os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_automatic_change_detection_and_reload(self):
        """Test automatic change detection and reloading in as_dict()."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Create lazy_dict with short change detection interval
            lazy_dict = LazyLoadedFileData(yaml_file, change_detection_secs=0.1)
            
            # Load initial data
            data1 = lazy_dict.as_dict()
            assert data1['name'] == 'test'
            assert data1['value'] == 42
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # Modify the file
            with open(yaml_file, 'w') as f:
                f.write("name: modified\nvalue: 100")
            
            # Call as_dict() again - should automatically detect change and reload
            data2 = lazy_dict.as_dict()
            assert data2['name'] == 'modified'
            assert data2['value'] == 100
            
            # Verify that the data was actually reloaded
            assert lazy_dict._loaded is True  # Should be loaded after reload
            
        finally:
            os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_change_detection_disabled(self):
        """Test that change detection can be disabled."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Create lazy_dict with disabled change detection
            lazy_dict = LazyLoadedFileData(yaml_file, change_detection_secs=0)
            
            # Load initial data
            data1 = lazy_dict.as_dict()
            assert data1['name'] == 'test'
            
            # Modify the file
            with open(yaml_file, 'w') as f:
                f.write("name: modified\nvalue: 100")
            
            # Wait longer than normal change detection interval
            time.sleep(0.5)
            
            # Call as_dict() again - should NOT detect change because detection is disabled
            data2 = lazy_dict.as_dict()
            assert data2['name'] == 'test'  # Should still be old data
            assert data2['value'] == 42
            
        finally:
            os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_file_stability_check(self):
        """Test that files must be stable before loading (except first load)."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # First load should work immediately (no stability check)
            start_time = time.time()
            _ = lazy_dict.as_dict()
            first_load_time = time.time() - start_time
            assert first_load_time < 0.05  # Should be fast
            
            # Modify the file to make it "unstable"
            with open(yaml_file, 'w') as f:
                f.write("name: modified\nvalue: 100")
            
            # Force a reload by setting _loaded to False
            lazy_dict._loaded = False
            
            # This should wait for file stability before loading
            start_time = time.time()
            _ = lazy_dict.as_dict()
            second_load_time = time.time() - start_time
            assert second_load_time >= LazyLoadedFileData.MIN_STABILITY_SECS  # Should wait for stability
            
        finally:
            os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_exception_handling_in_change_detection(self):
        """Test exception handling during change detection."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, change_detection_secs=0.1)
            
            # Load initial data
            _ = lazy_dict.as_dict()
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # Delete the file to cause FileNotFoundError in has_changed()
            os.unlink(yaml_file)
            
            # This should not raise an exception - it should be caught and handled gracefully
            data = lazy_dict.as_dict()
            assert data['name'] == 'test'  # Should still return cached data
            
        finally:
            # Clean up in case of early exit
            if os.path.exists(yaml_file):
                os.unlink(yaml_file)

    def test_last_checked_at_tracking(self):
        """Test that _last_checked_at is properly tracked."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, change_detection_secs=0.1)
            
            # Before first access, _last_checked_at should be None
            assert lazy_dict._last_checked_at is None
            
            # First access should set _last_checked_at
            _ = lazy_dict.as_dict()
            assert lazy_dict._last_checked_at is not None
            
            initial_check_time = lazy_dict._last_checked_at
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # Second access should update _last_checked_at
            _ = lazy_dict.as_dict()
            assert lazy_dict._last_checked_at > initial_check_time
            
        finally:
            os.unlink(yaml_file)

    def test_race_condition_prevention(self):
        """Test that metadata is captured after successful loading to prevent race conditions."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            
            # Load the file
            _ = lazy_dict.as_dict()
            
            # Get the file stat right after loading
            stat_after_loading = os.stat(yaml_file)
            
            # The captured metadata should match the file state after loading
            assert lazy_dict._last_file_mtime == stat_after_loading.st_mtime
            assert lazy_dict._last_file_size == stat_after_loading.st_size
            
        finally:
            os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_complex_change_detection_scenario(self):
        """Test a complex scenario with multiple file modifications and change detection."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, change_detection_secs=0.1)
            
            # Initial load
            data1 = lazy_dict.as_dict()
            assert data1['name'] == 'test'
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # First modification
            with open(yaml_file, 'w') as f:
                f.write("name: first_change\nvalue: 100")
            
            data2 = lazy_dict.as_dict()
            assert data2['name'] == 'first_change'
            
            # Wait for change detection interval again
            time.sleep(0.15)
            
            # Second modification
            with open(yaml_file, 'w') as f:
                f.write("name: second_change\nvalue: 200")
            
            data3 = lazy_dict.as_dict()
            assert data3['name'] == 'second_change'
            assert data3['value'] == 200
            
            # Verify that all changes were detected and loaded
            assert data1['name'] == 'test'
            assert data2['name'] == 'first_change'
            assert data3['name'] == 'second_change'
            
        finally:
            os.unlink(yaml_file)

    def test_csv_loading_with_as_list(self):
        """Test loading CSV files using the new as_list() method."""
        csv_content = "name,age,city\nJohn,25,NYC\nJane,30,LA\nBob,35,Chicago"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # Test that data is not loaded until first access
            assert not lazy_data._loaded
            
            # Test accessing data using as_list()
            data = lazy_data.as_list()
            assert len(data) == 3
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            assert data[1]['name'] == 'Jane'
            assert data[2]['name'] == 'Bob'
            
            # Test that data is now loaded
            assert lazy_data._loaded
            
            # Test that headers are stored correctly
            assert lazy_data._headers == ['name', 'age', 'city']
            assert lazy_data._delimiter == ','
            
        finally:
            os.unlink(csv_file)

    def test_tsv_loading_with_as_list(self):
        """Test loading TSV files using the new as_list() method."""
        tsv_content = "product\tprice\tstock\nWidget\t10.99\t100\nGadget\t25.50\t50"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write(tsv_content)
            tsv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(tsv_file)
            
            # Test file format detection
            assert lazy_data.get_file_format() == 'tsv'
            
            # Test accessing data using as_list()
            data = lazy_data.as_list()
            assert len(data) == 2
            assert data[0]['product'] == 'Widget'
            assert data[0]['price'] == '10.99'
            assert data[0]['stock'] == '100'
            assert data[1]['product'] == 'Gadget'
            
            # Test that headers are stored correctly
            assert lazy_data._headers == ['product', 'price', 'stock']
            assert lazy_data._delimiter == '\t'
            
        finally:
            os.unlink(tsv_file)

    def test_csv_mutable_parameter_in_as_list(self):
        """Test the mutable parameter in as_list() method."""
        csv_content = "name,age\nJohn,25\nJane,30"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # Test default behavior (immutable)
            data_immutable = lazy_data.as_list()
            assert all(isinstance(row, MappingProxyType) for row in data_immutable)
            
            # Test mutable=True
            data_mutable = lazy_data.as_list(mutable=True)
            assert all(isinstance(row, dict) for row in data_mutable)
            assert all(not isinstance(row, MappingProxyType) for row in data_mutable)
            
            # Test that immutable dicts cannot be modified
            with pytest.raises(TypeError):
                data_immutable[0]['name'] = 'modified'
            
            # Test that mutable dicts can be modified
            data_mutable[0]['name'] = 'modified'
            assert data_mutable[0]['name'] == 'modified'
            
        finally:
            os.unlink(csv_file)

    def test_csv_empty_headers_handling(self):
        """Test that empty column headers are ignored."""
        csv_content = "name,,city\nJohn,25,NYC\nJane,,LA"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            data = lazy_data.as_list()
            
            # Should only have 'name' and 'city' columns (empty header ignored)
            assert len(data) == 2
            assert 'name' in data[0]
            assert 'city' in data[0]
            # The empty column should not be present in the data
            assert len(data[0]) == 2  # Only name and city
            
            # Headers should include the empty string
            assert lazy_data._headers == ['name', '', 'city']
            
        finally:
            os.unlink(csv_file)

    def test_csv_duplicate_headers_error(self):
        """Test that duplicate headers raise ValueError."""
        csv_content = "name,age,name\nJohn,25,Duplicate\nJane,30,AlsoDuplicate"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            with pytest.raises(ValueError, match="Duplicate column headers found"):
                _ = lazy_data.as_list()
                
        finally:
            os.unlink(csv_file)

    def test_csv_empty_file(self):
        """Test handling of empty CSV files."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("")
            empty_file = f.name

        try:
            lazy_data = LazyLoadedFileData(empty_file)
            # Empty CSV files should result in empty list
            data = lazy_data.as_list()
            assert len(data) == 0
            
        finally:
            os.unlink(empty_file)

    def test_csv_headers_only(self):
        """Test CSV files with only headers (no data rows)."""
        csv_content = "name,age,city"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            data = lazy_data.as_list()
            assert len(data) == 0
            assert lazy_data._headers == ['name', 'age', 'city']
            
        finally:
            os.unlink(csv_file)

    def test_csv_missing_columns(self):
        """Test CSV files where some rows have fewer columns than headers."""
        csv_content = "name,age,city\nJohn,25\nJane,30,LA,Extra"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            data = lazy_data.as_list()
            
            # First row missing 'city' column
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] is None  # Missing column should be None
            
            # Second row has extra column (should be ignored)
            assert data[1]['name'] == 'Jane'
            assert data[1]['age'] == '30'
            assert data[1]['city'] == 'LA'
            
        finally:
            os.unlink(csv_file)

    def test_csv_method_validation(self):
        """Test that calling as_dict() on CSV files raises ValueError."""
        csv_content = "name,age\nJohn,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            with pytest.raises(ValueError, match="This file contains list data and requires as_list\\(\\) method"):
                _ = lazy_data.as_dict()
                
        finally:
            os.unlink(csv_file)

    def test_yaml_method_validation(self):
        """Test that calling as_list() on YAML dict files raises ValueError."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file)
            with pytest.raises(ValueError, match="This file contains dictionary data and requires as_dict\\(\\) method"):
                _ = lazy_dict.as_list()
                
        finally:
            os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_csv_change_detection(self):
        """Test that CSV files support change detection like other formats."""
        csv_content = "name,age\nJohn,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, change_detection_secs=0.1)
            
            # Load initial data
            data1 = lazy_data.as_list()
            assert data1[0]['name'] == 'John'
            assert data1[0]['age'] == '25'
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # Modify the file
            with open(csv_file, 'w') as f:
                f.write("name,age\nJane,30")
            
            # Call as_list() again - should automatically detect change and reload
            data2 = lazy_data.as_list()
            assert data2[0]['name'] == 'Jane'
            assert data2[0]['age'] == '30'
            
        finally:
            os.unlink(csv_file)

    def test_csv_lazy_loading_behavior(self):
        """Test that CSV files are not loaded until as_list() is called."""
        csv_content = "name,age\nJohn,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # Data should not be loaded yet
            assert not lazy_data._loaded
            assert lazy_data._headers == []
            assert lazy_data._delimiter is None
            
            # Call as_list() to trigger loading
            _ = lazy_data.as_list()
            
            # Now data should be loaded
            assert lazy_data._loaded
            assert lazy_data._headers == ['name', 'age']
            assert lazy_data._delimiter == ','
            
        finally:
            os.unlink(csv_file)

    def test_csv_file_format_detection(self):
        """Test that CSV and TSV file formats are detected correctly."""
        csv_content = "name,age\nJohn,25"
        tsv_content = "name\tage\nJohn\t25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name
            
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write(tsv_content)
            tsv_file = f.name

        try:
            # Test CSV format detection
            lazy_csv = LazyLoadedFileData(csv_file)
            assert lazy_csv.get_file_format() == 'csv'
            assert lazy_csv._is_list_format() is True
            
            # Test TSV format detection
            lazy_tsv = LazyLoadedFileData(tsv_file)
            assert lazy_tsv.get_file_format() == 'tsv'
            assert lazy_tsv._is_list_format() is True
            
        finally:
            os.unlink(csv_file)
            os.unlink(tsv_file)

    def test_csv_import_function(self):
        """Test that CSV import function works correctly."""
        from totodev_pub.lazy_loaded_file_data import _import_csv
        
        # Test CSV import
        csv_reader = _import_csv()
        assert csv_reader is not None
        
        # Test that it returns a function that can be called
        csv_content = "name,age\nJohn,25"
        import io
        reader = csv_reader(io.StringIO(csv_content))
        rows = list(reader)
        assert rows == [['name', 'age'], ['John', '25']]

    def test_csv_embedded_newlines_preservation(self):
        """Test that CSV parsing preserves embedded newlines correctly."""
        # Test with Windows line endings
        csv_content_windows = 'name,description,age\r\nJohn,"This is a description\r\nwith multiple lines\r\nand embedded newlines",25\r\nJane,"Simple description",30'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content_windows)
            csv_file_windows = f.name

        try:
            lazy_data_windows = LazyLoadedFileData(csv_file_windows)
            data_windows = lazy_data_windows.as_list()
            
            # Check that Windows line endings are preserved
            john_desc_windows = data_windows[0]['description']
            assert '\r\n' in john_desc_windows, "Windows line endings should be preserved"
            assert john_desc_windows == 'This is a description\r\nwith multiple lines\r\nand embedded newlines'
            
        finally:
            os.unlink(csv_file_windows)
        
        # Test with Unix line endings
        csv_content_unix = 'name,description,age\nJohn,"This is a description\nwith multiple lines\nand embedded newlines",25\nJane,"Simple description",30'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content_unix)
            csv_file_unix = f.name

        try:
            lazy_data_unix = LazyLoadedFileData(csv_file_unix)
            data_unix = lazy_data_unix.as_list()
            
            # Check that Unix line endings are preserved
            john_desc_unix = data_unix[0]['description']
            assert '\n' in john_desc_unix, "Unix line endings should be preserved"
            assert '\r' not in john_desc_unix, "Unix line endings should not contain carriage returns"
            assert john_desc_unix == 'This is a description\nwith multiple lines\nand embedded newlines'
            
        finally:
            os.unlink(csv_file_unix)

    def test_yaml_list_loading_with_as_list(self):
        """Test loading YAML files with list at top level using as_list()."""
        yaml_content = """- name: John
  age: 25
- name: Jane
  age: 30"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Test file format detection
            assert lazy_data.get_file_format() == 'yaml'
            
            # Test accessing data using as_list()
            data = lazy_data.as_list()
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == 25
            assert data[1]['name'] == 'Jane'
            assert data[1]['age'] == 30
            
            # Test that calling as_dict() fails
            with pytest.raises(ValueError, match="This file contains list data and requires as_list\\(\\) method"):
                _ = lazy_data.as_dict()
            
        finally:
            os.unlink(yaml_file)

    def test_json_list_loading_with_as_list(self):
        """Test loading JSON files with list at top level using as_list()."""
        json_content = '''[
  {"name": "John", "age": 25},
  {"name": "Jane", "age": 30}
]'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_data = LazyLoadedFileData(json_file)
            
            # Test file format detection
            assert lazy_data.get_file_format() == 'json'
            
            # Test accessing data using as_list()
            data = lazy_data.as_list()
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == 25
            assert data[1]['name'] == 'Jane'
            assert data[1]['age'] == 30
            
            # Test that calling as_dict() fails
            with pytest.raises(ValueError, match="This file contains list data and requires as_list\\(\\) method"):
                _ = lazy_data.as_dict()
            
        finally:
            os.unlink(json_file)

    def test_yaml_dict_loading_with_as_dict(self):
        """Test that YAML files with dict at top level still work with as_dict()."""
        yaml_content = """name: test
value: 42
nested:
  key: value"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Test file format detection
            assert lazy_data.get_file_format() == 'yaml'
            
            # Test accessing data using as_dict()
            data = lazy_data.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            assert data['nested']['key'] == 'value'
            
            # Test that calling as_list() fails
            with pytest.raises(ValueError, match="This file contains dictionary data and requires as_dict\\(\\) method"):
                _ = lazy_data.as_list()
            
        finally:
            os.unlink(yaml_file)

    def test_json_dict_loading_with_as_dict(self):
        """Test that JSON files with dict at top level still work with as_dict()."""
        json_content = '''{"name": "test", "value": 42, "nested": {"key": "value"}}'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_data = LazyLoadedFileData(json_file)
            
            # Test file format detection
            assert lazy_data.get_file_format() == 'json'
            
            # Test accessing data using as_dict()
            data = lazy_data.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            assert data['nested']['key'] == 'value'
            
            # Test that calling as_list() fails
            with pytest.raises(ValueError, match="This file contains dictionary data and requires as_dict\\(\\) method"):
                _ = lazy_data.as_list()
            
        finally:
            os.unlink(json_file)

    def test_yaml_list_mutable_parameter(self):
        """Test the mutable parameter in as_list() with YAML list data."""
        yaml_content = """- name: John
  age: 25"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Test default behavior (immutable)
            data_immutable = lazy_data.as_list()
            assert all(isinstance(row, MappingProxyType) for row in data_immutable)
            
            # Test mutable=True
            data_mutable = lazy_data.as_list(mutable=True)
            assert all(isinstance(row, dict) for row in data_mutable)
            assert all(not isinstance(row, MappingProxyType) for row in data_mutable)
            
            # Test that immutable dicts cannot be modified
            with pytest.raises(TypeError):
                data_immutable[0]['name'] = 'modified'
            
            # Test that mutable dicts can be modified
            data_mutable[0]['name'] = 'modified'
            assert data_mutable[0]['name'] == 'modified'
            
        finally:
            os.unlink(yaml_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_json_list_change_detection(self):
        """Test that JSON list files support change detection."""
        json_content = '''[{"name": "John", "age": 25}]'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_data = LazyLoadedFileData(json_file, change_detection_secs=0.1)
            
            # Load initial data
            data1 = lazy_data.as_list()
            assert data1[0]['name'] == 'John'
            assert data1[0]['age'] == 25
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # Modify the file
            with open(json_file, 'w') as f:
                f.write('[{"name": "Jane", "age": 30}]')
            
            # Call as_list() again - should automatically detect change and reload
            data2 = lazy_data.as_list()
            assert data2[0]['name'] == 'Jane'
            assert data2[0]['age'] == 30
            
        finally:
            os.unlink(json_file)

    def test_empty_yaml_list(self):
        """Test handling of empty YAML list files."""
        yaml_content = "[]"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            data = lazy_data.as_list()
            assert len(data) == 0
            
        finally:
            os.unlink(yaml_file)

    def test_empty_json_list(self):
        """Test handling of empty JSON list files."""
        json_content = "[]"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_data = LazyLoadedFileData(json_file)
            data = lazy_data.as_list()
            assert len(data) == 0
            
        finally:
            os.unlink(json_file)

    def test_data_type_detection_methods(self):
        """Test the data type detection helper methods."""
        # Test with YAML list
        yaml_list_content = """- name: John
  age: 25"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_list_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Before loading, data type should be None
            assert lazy_data._data_type is None
            
            # Load the data
            _ = lazy_data.as_list()
            
            # After loading, should detect list type
            assert lazy_data._data_type == 'list'
            assert lazy_data._is_data_list_type() is True
            assert lazy_data._is_data_dict_type() is False
            
        finally:
            os.unlink(yaml_file)

        # Test with YAML dict
        yaml_dict_content = """name: test
value: 42"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_dict_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Load the data
            _ = lazy_data.as_dict()
            
            # After loading, should detect dict type
            assert lazy_data._data_type == 'dict'
            assert lazy_data._is_data_list_type() is False
            assert lazy_data._is_data_dict_type() is True
            
        finally:
            os.unlink(yaml_file)

    def test_flex_headers_basic_functionality(self):
        """Test basic flex headers functionality with CSV files."""
        csv_content = """Title: Sales Report
Description: Monthly sales data
Generated: 2024-01-01

name,age,city
John,25,NYC
Jane,30,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            # Test with flex_header_limit=5 (should find header row at line 4)
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            assert data[1]['name'] == 'Jane'
            assert data[1]['age'] == '30'
            assert data[1]['city'] == 'LA'
            
            # Verify headers are detected correctly
            assert lazy_data._headers == ['name', 'age', 'city']
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_standard_mode(self):
        """Test that flex_header_limit=0 works like standard CSV processing."""
        csv_content = "name,age,city\nJohn,25,NYC\nJane,30,LA"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            # Test with flex_header_limit=0 (standard mode)
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=0)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            
            # Verify headers are from first row
            assert lazy_data._headers == ['name', 'age', 'city']
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_standard_mode_ignores_column_filtering(self):
        """Test that flex_header_limit=0 does NOT apply flex_headers column filtering logic."""
        csv_content = "name,,#ignore,age,city\nJohn,skip,skip,25,NYC\nJane,skip,skip,30,LA"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            # Test with flex_header_limit=0 (standard mode)
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=0)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            # In standard mode, empty headers are ignored (existing behavior)
            # but #-prefixed headers should still appear (not filtered by flex_headers logic)
            assert data[0]['name'] == 'John'
            assert data[0]['#ignore'] == 'skip'  # #-prefixed header should appear
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            # Empty header column should not appear (existing standard behavior)
            assert len(data[0]) == 4  # name, #ignore, age, city
            
            # Verify headers include all columns (including empty and #-prefixed)
            assert lazy_data._headers == ['name', '', '#ignore', 'age', 'city']
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_ignore_columns_with_hash(self):
        """Test that columns with headers starting with '#' are ignored."""
        csv_content = """Title: Data Report
# This is a comment line

name,#ignore,age,city
John,skip,25,NYC
Jane,skip,30,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            # The #ignore column should not be present
            assert '#ignore' not in data[0]
            assert len(data[0]) == 3  # Only name, age, city
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_ignore_blank_columns(self):
        """Test that blank column headers are ignored."""
        csv_content = """Title: Data Report

name,,age,city
John,,25,NYC
Jane,,30,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            # The blank column should not be present
            assert len(data[0]) == 3  # Only name, age, city
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_widest_row_detection(self):
        """Test that the widest row (most non-blank cells) is chosen as header."""
        csv_content = """Title: Sales Report
Short line
name,age,city,department,salary
John,25,NYC,IT,50000
Jane,30,LA,HR,60000"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            assert data[0]['department'] == 'IT'
            assert data[0]['salary'] == '50000'
            
            # Verify the widest row (line 3) was chosen as header
            assert lazy_data._headers == ['name', 'age', 'city', 'department', 'salary']
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_tie_breaking_earliest_wins(self):
        """Test that in case of tie, the earliest row is chosen."""
        csv_content = """name,age,city
title,description,date
John,25,NYC
Jane,30,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            # Should choose the first row (name,age,city) as header
            assert lazy_data._headers == ['name', 'age', 'city']
            assert len(data) == 3  # All rows after header are data rows
            assert data[0]['name'] == 'title'
            assert data[0]['age'] == 'description'
            assert data[0]['city'] == 'date'
            assert data[1]['name'] == 'John'
            assert data[1]['age'] == '25'
            assert data[1]['city'] == 'NYC'
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_tsv_format(self):
        """Test flex headers with TSV format."""
        tsv_content = """Title: Product Report
Description: Monthly product data

product\tprice\tstock
Widget\t10.99\t100
Gadget\t25.50\t50"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write(tsv_content)
            tsv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(tsv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['product'] == 'Widget'
            assert data[0]['price'] == '10.99'
            assert data[0]['stock'] == '100'
            assert data[1]['product'] == 'Gadget'
            
            # Verify TSV format detection
            assert lazy_data.get_file_format() == 'tsv'
            assert lazy_data._delimiter == '\t'
            
        finally:
            os.unlink(tsv_file)

    def test_flex_headers_empty_file(self):
        """Test flex headers with empty file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("")
            empty_file = f.name

        try:
            lazy_data = LazyLoadedFileData(empty_file, flex_header_limit=5)
            data = lazy_data.as_list()
            assert len(data) == 0
            
        finally:
            os.unlink(empty_file)

    def test_flex_headers_insufficient_rows(self):
        """Test flex headers when file has fewer rows than flex_header_limit."""
        csv_content = "name,age\nJohn,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=10)
            data = lazy_data.as_list()
            
            assert len(data) == 1
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_mixed_ignore_columns(self):
        """Test flex headers with both blank and hash-prefixed columns."""
        csv_content = """Title: Complex Report
# Comment line

name,,#ignore,age,city
John,,,25,NYC
Jane,,,30,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[0]['age'] == '25'
            assert data[0]['city'] == 'NYC'
            # Both blank and #ignore columns should be absent
            assert len(data[0]) == 3  # Only name, age, city
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_duplicate_validation(self):
        """Test that duplicate header validation works with flex headers."""
        csv_content = """Title: Report
name,age,name
John,25,Duplicate
Jane,30,AlsoDuplicate"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            with pytest.raises(ValueError, match="Duplicate column headers found"):
                _ = lazy_data.as_list()
                
        finally:
            os.unlink(csv_file)

    @very_lazy_test(['totodev_pub.lazy_loaded_file_data'], reverify_days=14)
    def test_flex_headers_change_detection(self):
        """Test that flex headers work with change detection."""
        csv_content = """Title: Report
name,age
John,25"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5, change_detection_secs=0.1)
            
            # Load initial data
            data1 = lazy_data.as_list()
            assert data1[0]['name'] == 'John'
            assert data1[0]['age'] == '25'
            
            # Wait for change detection interval
            time.sleep(0.15)
            
            # Modify the file
            with open(csv_file, 'w') as f:
                f.write("""Title: Updated Report
name,age,city
Jane,30,LA""")
            
            # Call as_list() again - should detect change and reload
            data2 = lazy_data.as_list()
            assert data2[0]['name'] == 'Jane'
            assert data2[0]['age'] == '30'
            assert data2[0]['city'] == 'LA'
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_constructor_parameter(self):
        """Test that flex_header_limit parameter is stored correctly."""
        csv_content = "name,age\nJohn,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            # Test default value
            lazy_data1 = LazyLoadedFileData(csv_file)
            assert lazy_data1._flex_header_limit == 0
            
            # Test custom value
            lazy_data2 = LazyLoadedFileData(csv_file, flex_header_limit=10)
            assert lazy_data2._flex_header_limit == 10
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_not_applicable_to_dict_formats(self):
        """Test that flex_header_limit parameter doesn't affect dict formats."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # flex_header_limit should be ignored for YAML files
            lazy_data = LazyLoadedFileData(yaml_file, flex_header_limit=10)
            data = lazy_data.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            
        finally:
            os.unlink(yaml_file)

    def test_flex_headers_header_detection_methods(self):
        """Test the header detection helper methods."""
        from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
        
        # Test _detect_header_row method
        candidate_rows = [
            ['title'],
            ['name', 'age', 'city'],
            ['short']
        ]
        
        # Create a temporary instance to test the method
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("name,age\nJohn,25")
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            header_idx, header_row = lazy_data._detect_header_row(candidate_rows)
            
            # Should choose the widest row (index 1)
            assert header_idx == 1
            assert header_row == ['name', 'age', 'city']
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_ignore_columns_methods(self):
        """Test the ignore columns helper methods."""
        from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
        
        # Test _get_ignore_columns method
        header_row = ['name', '', '#ignore', 'age', 'city']
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("name,age\nJohn,25")
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            ignore_columns = lazy_data._get_ignore_columns(header_row)
            
            # Should ignore columns at indices 1 (blank) and 2 (#ignore)
            assert ignore_columns == [1, 2]
            
            # Test _filter_columns method
            row = ['John', 'skip', 'skip', '25', 'NYC']
            filtered_row = lazy_data._filter_columns(row, ignore_columns)
            assert filtered_row == ['John', '25', 'NYC']
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_row_retriever_generator(self):
        """Test the row retriever generator functionality."""
        csv_content = """Title: Report
name,age
John,25
Jane,30"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            
            # Load the data to trigger the generator
            data = lazy_data.as_list()
            
            assert len(data) == 2
            assert data[0]['name'] == 'John'
            assert data[1]['name'] == 'Jane'
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_edge_case_all_blank_headers(self):
        """Test flex headers when all headers are blank or ignored.
        
        Note: Even when there are blank rows, the algorithm will choose the widest row
        as the header. In this case, the data row ['John', '25', 'NYC'] is chosen as
        the header because it has the most non-blank cells.
        """
        csv_content = """Title: Report
,,
,,
John,25,NYC
Jane,30,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            # The widest row ['John', '25', 'NYC'] is chosen as header
            assert len(data) == 1  # Only one row after the detected header
            assert data[0]['John'] == 'Jane'
            assert data[0]['25'] == '30'
            assert data[0]['NYC'] == 'LA'
            
        finally:
            os.unlink(csv_file)

    def test_flex_headers_edge_case_single_column(self):
        """Test flex headers with single column - pathological case.
        
        Note: Single-column tables are pathological for flex headers since all rows
        have the same width (1 cell). The algorithm will choose the first row as
        header, which may not be the intended header row. For flex headers to work
        properly, the header row must be the widest row in the first few rows.
        """
        csv_content = """Title: Report
name
John
Jane"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list()
            
            # In this pathological case, the first row is chosen as header
            # because all rows have the same width (1 cell)
            assert len(data) == 3  # All rows after detected header
            assert data[0]['Title: Report'] == 'name'
            assert data[1]['Title: Report'] == 'John'
            assert data[2]['Title: Report'] == 'Jane'
            
        finally:
            os.unlink(csv_file)

    def test_docstring_example_quick_start_lookup_table(self):
        """
        Test the Quick Start example: Define a lookup table as a global constant.
        
        This test corresponds to the first Quick Start example in the file-level docstring.
        """
        yaml_content = """
"4000": "Sales Revenue"
"4100": "Service Revenue"
"5000": "Cost of Goods Sold"
"6000": "Operating Expenses"
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "data", "gl_codes.yaml")
            # Here we use the temp file for testing purposes
            GL_CODES = LazyLoadedFileData(yaml_file)

            def get_account_description(code: str) -> str:
                gl_data = GL_CODES.as_dict()  # Loads file on first call
                return gl_data.get(code, "Unknown account")
            
            # Test the lookup functionality
            assert get_account_description("4000") == "Sales Revenue"
            assert get_account_description("4100") == "Service Revenue"
            assert get_account_description("5000") == "Cost of Goods Sold"
            assert get_account_description("6000") == "Operating Expenses"
            assert get_account_description("9999") == "Unknown account"
            
        finally:
            os.unlink(yaml_file)

    def test_docstring_example_quick_start_csv_flex_headers(self):
        """
        Test the Quick Start example: Load CSV data with automatic header detection.
        
        This test corresponds to the second Quick Start example in the file-level docstring.
        """
        csv_content = """Title: Monthly Sales Report
Generated: 2024-01-15

sku,amount,quantity
WIDGET-001,29.99,100
GADGET-002,15.50,250
TOOL-003,89.99,50"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "reports", "monthly_sales.csv")
            # Here we use the temp file for testing purposes
            SALES_DATA = LazyLoadedFileData(csv_file, flex_header_limit=5)  # Increased limit to include header row

            def get_monthly_totals():
                data = SALES_DATA.as_list()  # Automatically skips title rows
                return sum(float(row['amount']) for row in data)
            
            # Test the calculation functionality
            total = get_monthly_totals()
            expected_total = 29.99 + 15.50 + 89.99
            assert abs(total - expected_total) < 0.01  # Allow for floating point precision
            
        finally:
            os.unlink(csv_file)

    def test_docstring_example_quick_start_config_change_detection(self):
        """
        Test the Quick Start example: Configuration data with change detection.
        
        This test corresponds to the third Quick Start example in the file-level docstring.
        """
        yaml_content = """
database:
  host: "localhost"
  port: 5432
  name: "myapp_dev"
features:
  new_ui: true
  beta_features: false
limits:
  max_connections: 100
  timeout_seconds: 30
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "config", "settings.yaml")
            # Here we use the temp file for testing purposes
            APP_CONFIG = LazyLoadedFileData(yaml_file, change_detection_secs=60)

            def get_feature_flag(flag_name: str) -> bool:
                config = APP_CONFIG.as_dict()
                return config.get('features', {}).get(flag_name, False)
            
            # Test the feature flag functionality
            assert get_feature_flag('new_ui') is True
            assert get_feature_flag('beta_features') is False
            assert get_feature_flag('nonexistent_flag') is False
            
        finally:
            os.unlink(yaml_file)

    def test_docstring_example_real_world_gl_codes(self):
        """
        Test the Real-world example: General Ledger Codes Lookup.
        
        This test corresponds to the GL Codes example in the file-level docstring.
        """
        yaml_content = """
"4000": "Sales Revenue"
"4100": "Service Revenue"
"5000": "Cost of Goods Sold"
"6000": "Operating Expenses"
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "data", "gl_codes.yaml")
            # Here we use the temp file for testing purposes
            GL_CODES = LazyLoadedFileData(yaml_file)

            def format_transaction(amount: float, code: str) -> str:
                description = GL_CODES.as_dict()[code]
                return f"{code} - {description}: ${amount:,.2f}"
            
            # Test the transaction formatting functionality
            result1 = format_transaction(1500.00, "4000")
            assert result1 == "4000 - Sales Revenue: $1,500.00"
            
            result2 = format_transaction(750.50, "5000")
            assert result2 == "5000 - Cost of Goods Sold: $750.50"
            
        finally:
            os.unlink(yaml_file)

    def test_docstring_example_real_world_product_catalog(self):
        """
        Test the Real-world example: Product Catalog with Categories.
        
        This test corresponds to the Product Catalog example in the file-level docstring.
        """
        csv_content = """Title: Product Catalog
Generated: 2024-01-15
Notes: Updated pricing for Q1

sku,name,category,price,active
WIDGET-001,Deluxe Widget,Widgets,29.99,true
GADGET-002,Basic Gadget,Gadgets,15.50,true
TOOL-003,Pro Tool,Tools,89.99,false"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "data", "products.csv")
            # Here we use the temp file for testing purposes
            PRODUCTS = LazyLoadedFileData(csv_file, flex_header_limit=5)

            def get_products_by_category(category: str):
                products = PRODUCTS.as_list()
                return [p for p in products if p['category'] == category and p['active'] == 'true']
            
            # Test the category filtering functionality
            widgets = get_products_by_category('Widgets')
            assert len(widgets) == 1
            assert widgets[0]['sku'] == 'WIDGET-001'
            assert widgets[0]['name'] == 'Deluxe Widget'
            
            gadgets = get_products_by_category('Gadgets')
            assert len(gadgets) == 1
            assert gadgets[0]['sku'] == 'GADGET-002'
            
            # Test that inactive products are filtered out
            tools = get_products_by_category('Tools')
            assert len(tools) == 0  # TOOL-003 is inactive (active=false)
            
        finally:
            os.unlink(csv_file)

    def test_docstring_example_real_world_multi_env_config(self):
        """
        Test the Real-world example: Multi-environment Configuration.
        
        This test corresponds to the Multi-environment Configuration example in the file-level docstring.
        """
        json_content = '''{
  "database": {
    "host": "localhost",
    "port": 5432,
    "name": "myapp_dev"
  },
  "features": {
    "new_ui": true,
    "beta_features": false
  },
  "limits": {
    "max_connections": 100,
    "timeout_seconds": 30
  }
}'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "config", "development.json")
            # Here we use the temp file for testing purposes
            CONFIG = LazyLoadedFileData(json_file)

            def get_db_connection_string() -> str:
                db_config = CONFIG.as_dict()['database']
                return f"postgresql://{db_config['host']}:{db_config['port']}/{db_config['name']}"
            
            # Test the database connection string functionality
            connection_string = get_db_connection_string()
            assert connection_string == "postgresql://localhost:5432/myapp_dev"
            
        finally:
            os.unlink(json_file)

    def test_docstring_example_real_world_countries(self):
        """
        Test the Real-world example: Country/Region Reference Data.
        
        This test corresponds to the Countries example in the file-level docstring.
        """
        toml_content = """# data/countries.toml
[US]
name = "United States"
currency = "USD"
timezone = "America/New_York"
phone_prefix = "+1"

[CA]
name = "Canada"
currency = "CAD"
timezone = "America/Toronto"
phone_prefix = "+1"

[GB]
name = "United Kingdom"
currency = "GBP"
timezone = "Europe/London"
phone_prefix = "+44"
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            f.write(toml_content)
            toml_file = f.name

        try:
            # This mimics the global constant pattern from the docstring
            # Note: In real usage, you would use: os.path.join(os.path.dirname(__file__), "data", "countries.toml")
            # Here we use the temp file for testing purposes
            COUNTRIES = LazyLoadedFileData(toml_file)

            def format_phone_number(country_code: str, number: str) -> str:
                country = COUNTRIES.as_dict().get(country_code, {})
                prefix = country.get('phone_prefix', '+')
                return f"{prefix} {number}"
            
            # Test the phone number formatting functionality
            us_number = format_phone_number("US", "5551234567")
            assert us_number == "+1 5551234567"
            
            ca_number = format_phone_number("CA", "4165551234")
            assert ca_number == "+1 4165551234"
            
            gb_number = format_phone_number("GB", "2071234567")
            assert gb_number == "+44 2071234567"
            
            # Test unknown country (should use default prefix)
            unknown_number = format_phone_number("XX", "1234567890")
            assert unknown_number == "+ 1234567890"
            
        finally:
            os.unlink(toml_file)

    def test_file_exists_method_with_existing_file(self):
        """Test file_exists() method returns True for existing files."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # File should exist
            assert lazy_data.file_exists() is True
            
            # Should be able to check multiple times
            assert lazy_data.file_exists() is True
            
        finally:
            os.unlink(yaml_file)

    def test_file_exists_method_with_nonexistent_file(self):
        """Test file_exists() method returns False for non-existent files."""
        nonexistent_file = "/tmp/this_file_does_not_exist_12345.yaml"
        
        lazy_data = LazyLoadedFileData(nonexistent_file)
        
        # File should not exist
        assert lazy_data.file_exists() is False
        
        # Should be able to check multiple times without exception
        assert lazy_data.file_exists() is False

    def test_file_exists_method_does_not_trigger_loading(self):
        """Test that file_exists() does not trigger data loading."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Check existence
            assert lazy_data.file_exists() is True
            
            # Data should NOT be loaded yet
            assert lazy_data.is_loaded() is False
            
        finally:
            os.unlink(yaml_file)

    def test_file_exists_method_graceful_handling(self):
        """Test file_exists() for graceful handling of optional files."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Pattern: Check existence before loading
            config = LazyLoadedFileData(yaml_file)
            if config.file_exists():
                data = config.as_dict()
                assert data['name'] == 'test'
            else:
                # This branch won't execute in this test
                data = {}
            
            # Delete the file
            os.unlink(yaml_file)
            
            # Now check again - should return False
            assert config.file_exists() is False
            
        finally:
            # Clean up in case of early exit
            if os.path.exists(yaml_file):
                os.unlink(yaml_file)

    def test_file_exists_with_different_file_types(self):
        """Test file_exists() works with all supported file formats."""
        test_contents = {
            '.yaml': 'name: test',
            '.json': '{"name": "test"}',
            '.toml': 'name = "test"',
            '.csv': 'name,age\nJohn,25',
            '.tsv': 'name\tage\nJohn\t25'
        }
        
        for suffix, content in test_contents.items():
            with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False) as f:
                f.write(content)
                temp_file = f.name

            try:
                lazy_data = LazyLoadedFileData(temp_file)
                assert lazy_data.file_exists() is True
                
            finally:
                os.unlink(temp_file)

    def test_require_exists_parameter_with_existing_file(self):
        """Test require_exists=True with an existing file."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Should not raise an exception
            lazy_data = LazyLoadedFileData(yaml_file, require_exists=True)
            
            # Should be able to access data normally
            data = lazy_data.as_dict()
            assert data['name'] == 'test'
            assert data['value'] == 42
            
        finally:
            os.unlink(yaml_file)

    def test_require_exists_parameter_with_nonexistent_file(self):
        """Test require_exists=True raises FileNotFoundError for non-existent files."""
        nonexistent_file = "/tmp/this_file_does_not_exist_12345.yaml"
        
        # Should raise FileNotFoundError at construction time
        with pytest.raises(FileNotFoundError, match="No such file"):
            _ = LazyLoadedFileData(nonexistent_file, require_exists=True)

    def test_require_exists_default_behavior(self):
        """Test that require_exists defaults to False (backward compatible)."""
        nonexistent_file = "/tmp/this_file_does_not_exist_12345.yaml"
        
        # Should NOT raise an exception at construction time (default behavior)
        lazy_data = LazyLoadedFileData(nonexistent_file)
        
        # But should raise when trying to access data
        with pytest.raises(FileNotFoundError):
            _ = lazy_data.as_dict()

    def test_require_exists_fail_fast_pattern(self):
        """Test require_exists=True for fail-fast validation at startup."""
        yaml_content1 = "name: test1\nvalue: 42"
        yaml_content2 = "name: test2\nvalue: 100"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f1:
            f1.write(yaml_content1)
            yaml_file1 = f1.name
            
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f2:
            f2.write(yaml_content2)
            yaml_file2 = f2.name

        try:
            # Simulate application startup validation
            try:
                CONFIG = LazyLoadedFileData(yaml_file1, require_exists=True)
                DATA = LazyLoadedFileData(yaml_file2, require_exists=True)
                OPTIONAL = LazyLoadedFileData("/tmp/nonexistent.yaml", require_exists=True)
                # This line should not be reached
                assert False, "Should have raised FileNotFoundError"
            except FileNotFoundError:
                # Expected behavior - fail fast at startup
                pass
            
            # Now test that valid files work
            CONFIG = LazyLoadedFileData(yaml_file1, require_exists=True)
            DATA = LazyLoadedFileData(yaml_file2, require_exists=True)
            
            # Data should load normally later
            assert CONFIG.as_dict()['name'] == 'test1'
            assert DATA.as_dict()['name'] == 'test2'
            
        finally:
            os.unlink(yaml_file1)
            os.unlink(yaml_file2)

    def test_require_exists_with_all_file_formats(self):
        """Test require_exists=True works with all supported file formats."""
        test_contents = {
            '.yaml': 'name: test',
            '.json': '{"name": "test"}',
            '.toml': 'name = "test"',
            '.csv': 'name,age\nJohn,25',
            '.tsv': 'name\tage\nJohn\t25'
        }
        
        for suffix, content in test_contents.items():
            with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False) as f:
                f.write(content)
                temp_file = f.name

            try:
                # Should not raise an exception
                lazy_data = LazyLoadedFileData(temp_file, require_exists=True)
                
                # Should be able to access data
                if suffix in ['.csv', '.tsv']:
                    data = lazy_data.as_list()
                    assert len(data) > 0
                else:
                    data = lazy_data.as_dict()
                    assert 'name' in data
                
            finally:
                os.unlink(temp_file)

    def test_require_exists_with_other_parameters(self):
        """Test require_exists works correctly with other constructor parameters."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Test with acts_as_dict_proxy
            lazy_data1 = LazyLoadedFileData(yaml_file, acts_as_dict_proxy=True, require_exists=True)
            assert lazy_data1['name'] == 'test'
            
            # Test with change_detection_secs
            lazy_data2 = LazyLoadedFileData(yaml_file, change_detection_secs=10, require_exists=True)
            assert lazy_data2.as_dict()['name'] == 'test'
            
            # Test with flex_header_limit (CSV)
            csv_content = "Title: Report\nname,age\nJohn,25"
            with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f_csv:
                f_csv.write(csv_content)
                csv_file = f_csv.name
            
            try:
                lazy_data3 = LazyLoadedFileData(csv_file, flex_header_limit=5, require_exists=True)
                assert len(lazy_data3.as_list()) == 1
            finally:
                os.unlink(csv_file)
            
        finally:
            os.unlink(yaml_file)

    def test_require_exists_error_message_clarity(self):
        """Test that require_exists provides clear error messages."""
        nonexistent_file = "/tmp/very_specific_nonexistent_file_12345.yaml"
        
        try:
            _ = LazyLoadedFileData(nonexistent_file, require_exists=True)
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError as e:
            # Check that error message includes the filename
            assert nonexistent_file in str(e)
            assert "No such file" in str(e)

    def test_file_exists_and_require_exists_combined(self):
        """Test using file_exists() and require_exists together."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Create instance with require_exists=True
            lazy_data = LazyLoadedFileData(yaml_file, require_exists=True)
            
            # file_exists() should still work
            assert lazy_data.file_exists() is True
            
            # Data should be accessible
            assert lazy_data.as_dict()['name'] == 'test'
            
        finally:
            os.unlink(yaml_file)

    def test_backward_compatibility_default_parameters(self):
        """Test that new parameters maintain backward compatibility."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            # Old usage pattern should still work (no require_exists parameter)
            lazy_data = LazyLoadedFileData(yaml_file)
            assert lazy_data.as_dict()['name'] == 'test'
            
            # With other parameters but not require_exists
            lazy_data2 = LazyLoadedFileData(yaml_file, change_detection_secs=0)
            assert lazy_data2.as_dict()['name'] == 'test'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_yaml_dict(self):
        """Test static method overwrite_data_file with YAML dict data."""
        data = {'name': 'test', 'value': 42, 'nested': {'key': 'value'}}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml_file = f.name

        try:
            # Write data
            LazyLoadedFileData.overwrite_data_file(data, yaml_file)
            
            # Read back and verify
            loader = LazyLoadedFileData(yaml_file)
            loaded_data = loader.as_dict()
            assert loaded_data['name'] == 'test'
            assert loaded_data['value'] == 42
            assert loaded_data['nested']['key'] == 'value'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_yaml_list(self):
        """Test static method overwrite_data_file with YAML list data."""
        data = [{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml_file = f.name

        try:
            # Write data
            LazyLoadedFileData.overwrite_data_file(data, yaml_file)
            
            # Read back and verify
            loader = LazyLoadedFileData(yaml_file)
            loaded_data = loader.as_list()
            assert len(loaded_data) == 2
            assert loaded_data[0]['name'] == 'Alice'
            assert loaded_data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_json_dict(self):
        """Test static method overwrite_data_file with JSON dict data."""
        data = {'name': 'test', 'value': 42, 'nested': {'key': 'value'}}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json_file = f.name

        try:
            # Write data
            LazyLoadedFileData.overwrite_data_file(data, json_file)
            
            # Read back and verify
            loader = LazyLoadedFileData(json_file)
            loaded_data = loader.as_dict()
            assert loaded_data['name'] == 'test'
            assert loaded_data['value'] == 42
            assert loaded_data['nested']['key'] == 'value'
            
        finally:
            os.unlink(json_file)

    def test_overwrite_data_file_json_list(self):
        """Test static method overwrite_data_file with JSON list data."""
        data = [{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json_file = f.name

        try:
            # Write data
            LazyLoadedFileData.overwrite_data_file(data, json_file)
            
            # Read back and verify
            loader = LazyLoadedFileData(json_file)
            loaded_data = loader.as_list()
            assert len(loaded_data) == 2
            assert loaded_data[0]['name'] == 'Alice'
            assert loaded_data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(json_file)

    def test_overwrite_data_file_forbids_toml(self):
        """Test that TOML format is forbidden for writing."""
        data = {'name': 'test', 'value': 42}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            toml_file = f.name

        try:
            with pytest.raises(ValueError, match="TOML format is not supported for writing"):
                LazyLoadedFileData.overwrite_data_file(data, toml_file)
                
        finally:
            os.unlink(toml_file)

    def test_overwrite_data_file_forbids_csv(self):
        """Test that CSV format is forbidden for writing."""
        data = [{'name': 'Alice', 'age': 30}]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            csv_file = f.name

        try:
            with pytest.raises(ValueError, match="CSV format is not supported for writing"):
                LazyLoadedFileData.overwrite_data_file(data, csv_file)
                
        finally:
            os.unlink(csv_file)

    def test_overwrite_data_file_forbids_tsv(self):
        """Test that TSV format is forbidden for writing."""
        data = [{'name': 'Alice', 'age': 30}]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            tsv_file = f.name

        try:
            with pytest.raises(ValueError, match="TSV format is not supported for writing"):
                LazyLoadedFileData.overwrite_data_file(data, tsv_file)
                
        finally:
            os.unlink(tsv_file)

    def test_overwrite_data_file_invalid_data_type(self):
        """Test that invalid data types raise TypeError."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml_file = f.name

        try:
            # Try to write string (not dict or list)
            with pytest.raises(TypeError, match="Data must be dict or list"):
                LazyLoadedFileData.overwrite_data_file("invalid", yaml_file)
            
            # Try to write int
            with pytest.raises(TypeError, match="Data must be dict or list"):
                LazyLoadedFileData.overwrite_data_file(42, yaml_file)
                
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_unsupported_extension(self):
        """Test that unsupported file extensions raise ValueError."""
        data = {'name': 'test'}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            txt_file = f.name

        try:
            with pytest.raises(ValueError, match="Unsupported file format"):
                LazyLoadedFileData.overwrite_data_file(data, txt_file)
                
        finally:
            os.unlink(txt_file)

    def test_overwrite_data_file_atomic_write(self):
        """Test atomic write functionality."""
        data = {'name': 'test', 'value': 42}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml_file = f.name

        try:
            # Write with atomic=True (default)
            LazyLoadedFileData.overwrite_data_file(data, yaml_file, atomic=True)
            
            # Verify file exists and is readable
            assert os.path.exists(yaml_file)
            loader = LazyLoadedFileData(yaml_file)
            loaded_data = loader.as_dict()
            assert loaded_data['name'] == 'test'
            
            # Verify no temp files left behind
            dir_name = os.path.dirname(yaml_file)
            base_name = os.path.basename(yaml_file)
            temp_files = [f for f in os.listdir(dir_name) if f.startswith(base_name) and 'DELETETHIS_TEMP' in f]
            assert len(temp_files) == 0
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_non_atomic_write(self):
        """Test non-atomic write functionality."""
        data = {'name': 'test', 'value': 42}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml_file = f.name

        try:
            # Write with atomic=False
            LazyLoadedFileData.overwrite_data_file(data, yaml_file, atomic=False)
            
            # Verify file exists and is readable
            assert os.path.exists(yaml_file)
            loader = LazyLoadedFileData(yaml_file)
            loaded_data = loader.as_dict()
            assert loaded_data['name'] == 'test'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_json_idempotency(self):
        """Test that JSON writes are idempotent (same output each time)."""
        data = {'name': 'test', 'nested': {'key': 'value'}, 'list': [1, 2, 3]}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json_file = f.name

        try:
            # Write twice
            LazyLoadedFileData.overwrite_data_file(data, json_file)
            with open(json_file, 'r') as f:
                content1 = f.read()
            
            LazyLoadedFileData.overwrite_data_file(data, json_file)
            with open(json_file, 'r') as f:
                content2 = f.read()
            
            # Content should be identical
            assert content1 == content2
            
            # Verify formatting (indent, sorted keys)
            assert '"list"' in content1  # Should be formatted
            assert '\n' in content1  # Should have newlines
            
        finally:
            os.unlink(json_file)

    def test_overwrite_source_file_with_data(self):
        """Test instance method overwrite_source_file with explicit data."""
        yaml_content = "name: original\nvalue: 1"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            loader = LazyLoadedFileData(yaml_file)
            
            # Load original data
            original = loader.as_dict()
            assert original['name'] == 'original'
            
            # Write new data
            new_data = {'name': 'updated', 'value': 2, 'new_key': 'new_value'}
            loader.overwrite_source_file(new_data)
            
            # Verify cache was invalidated
            assert loader._loaded is False
            
            # Read back and verify
            updated = loader.as_dict()
            assert updated['name'] == 'updated'
            assert updated['value'] == 2
            assert updated['new_key'] == 'new_value'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_source_file_without_data(self):
        """Test instance method overwrite_source_file without explicit data."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            loader = LazyLoadedFileData(yaml_file)
            
            # Load data first
            data = loader.as_dict()
            assert data['name'] == 'test'
            
            # Write back current data
            loader.overwrite_source_file()  # No data argument
            
            # Verify cache was invalidated
            assert loader._loaded is False
            
            # Read back and verify (should be same)
            reloaded = loader.as_dict()
            assert reloaded['name'] == 'test'
            assert reloaded['value'] == 42
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_source_file_error_if_not_loaded(self):
        """Test that overwrite_source_file raises error if data not loaded."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml_file = f.name

        try:
            loader = LazyLoadedFileData(yaml_file)
            
            # Try to write without loading first
            with pytest.raises(ValueError, match="Cannot write back unloaded data"):
                loader.overwrite_source_file()  # Should raise error
                
        finally:
            os.unlink(yaml_file)

    def test_overwrite_source_file_round_trip_dict(self):
        """Test round-trip: read dict, modify, write back."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            loader = LazyLoadedFileData(yaml_file)
            
            # Read, modify, write
            data = loader.as_dict(mutable=True)
            data['name'] = 'modified'
            data['new_field'] = 'added'
            loader.overwrite_source_file(data)
            
            # Reload and verify
            updated = loader.as_dict()
            assert updated['name'] == 'modified'
            assert updated['value'] == 42
            assert updated['new_field'] == 'added'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_source_file_round_trip_list(self):
        """Test round-trip: read list, modify, write back."""
        json_content = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            loader = LazyLoadedFileData(json_file)
            
            # Read, modify, write
            data = loader.as_list(mutable=True)
            data.append({'name': 'Charlie', 'age': 35})
            data[0]['age'] = 31  # Modify existing
            loader.overwrite_source_file(data)
            
            # Reload and verify
            updated = loader.as_list()
            assert len(updated) == 3
            assert updated[0]['age'] == 31
            assert updated[2]['name'] == 'Charlie'
            
        finally:
            os.unlink(json_file)

    def test_overwrite_yaml_loses_comments(self):
        """Test that YAML comments are lost during round-trip (documented behavior)."""
        yaml_with_comments = """# Top comment
name: test  # Inline comment
value: 42
# Another comment
nested:
  key: value  # More comments
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_with_comments)
            yaml_file = f.name

        try:
            loader = LazyLoadedFileData(yaml_file)
            data = loader.as_dict()
            
            # Write back
            loader.overwrite_source_file()
            
            # Read file content directly
            with open(yaml_file, 'r') as f:
                new_content = f.read()
            
            # Verify comments are gone (expected behavior)
            assert '# Top comment' not in new_content
            assert '# Inline comment' not in new_content
            assert '# Another comment' not in new_content
            assert '# More comments' not in new_content
            
            # But data should be preserved
            reloaded = loader.as_dict()
            assert reloaded['name'] == 'test'
            assert reloaded['value'] == 42
            assert reloaded['nested']['key'] == 'value'
            
        finally:
            os.unlink(yaml_file)

    def test_overwrite_data_file_yml_extension(self):
        """Test that .yml extension works (not just .yaml)."""
        data = {'name': 'test', 'value': 42}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            yml_file = f.name

        try:
            LazyLoadedFileData.overwrite_data_file(data, yml_file)
            
            # Read back and verify
            loader = LazyLoadedFileData(yml_file)
            loaded_data = loader.as_dict()
            assert loaded_data['name'] == 'test'
            assert loaded_data['value'] == 42
            
        finally:
            os.unlink(yml_file)

    def test_overwrite_forbids_unsupported_formats_in_instance_method(self):
        """Test that instance method also forbids unsupported formats."""
        csv_content = "name,age\nAlice,30"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            loader = LazyLoadedFileData(csv_file)
            data = loader.as_list()
            
            # Should raise error when trying to write CSV
            with pytest.raises(ValueError, match="CSV format is not supported for writing"):
                loader.overwrite_source_file(data)
                
        finally:
            os.unlink(csv_file)

    # ==================== iter_list() method tests ====================
    
    def test_iter_list_csv_basic(self):
        """Test basic CSV iteration with iter_list()."""
        csv_content = "name,age,city\nAlice,30,NYC\nBob,25,LA\nCharlie,35,Chicago"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # Test iter_list returns an iterator
            rows = list(lazy_data.iter_list())
            assert len(rows) == 3
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == '30'
            assert rows[0]['city'] == 'NYC'
            assert rows[1]['name'] == 'Bob'
            assert rows[2]['name'] == 'Charlie'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_matches_as_list(self):
        """Test that iter_list() produces same results as as_list()."""
        csv_content = "name,age,city\nAlice,30,NYC\nBob,25,LA"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            iter_rows = list(lazy_data.iter_list())
            as_list_rows = lazy_data.as_list()
            
            assert len(iter_rows) == len(as_list_rows)
            assert iter_rows == as_list_rows
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_mutable(self):
        """Test iter_list() with mutable=True option."""
        csv_content = "name,age\nAlice,30"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # Test mutable=True
            rows_mutable = list(lazy_data.iter_list(mutable=True))
            rows_mutable[0]['test'] = 'value'
            assert rows_mutable[0]['test'] == 'value'
            assert rows_mutable[0]['name'] == 'Alice'
            
            # Test mutable=False (default)
            rows_immutable = list(lazy_data.iter_list(mutable=False))
            with pytest.raises(TypeError):
                rows_immutable[0]['test'] = 'value'
                
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_empty_file(self):
        """Test iter_list() with empty CSV file."""
        csv_content = "name,age\n"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 0
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_headers_only(self):
        """Test iter_list() with CSV file that has headers but no data."""
        csv_content = "name,age,city\n"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 0
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_empty_rows(self):
        """Test iter_list() skips empty rows."""
        csv_content = "name,age\nAlice,30\n\nBob,25\n"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            rows = list(lazy_data.iter_list())
            # Should have 2 rows (empty rows skipped)
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_missing_columns(self):
        """Test iter_list() handles missing columns (fills with None)."""
        csv_content = "name,age,city\nAlice,30\nBob,25,LA,extra"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            rows = list(lazy_data.iter_list())
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == '30'
            assert rows[0]['city'] is None  # Missing column
            assert rows[1]['name'] == 'Bob'
            assert rows[1]['age'] == '25'
            assert rows[1]['city'] == 'LA'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_tsv(self):
        """Test iter_list() with TSV format."""
        tsv_content = "name\tage\tcity\nAlice\t30\tNYC\nBob\t25\tLA"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write(tsv_content)
            tsv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(tsv_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == '30'
            
        finally:
            os.unlink(tsv_file)

    def test_iter_list_yaml_list(self):
        """Test iter_list() with YAML list data."""
        yaml_content = """
- name: Alice
  age: 30
- name: Bob
  age: 25
- name: Charlie
  age: 35
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 3
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == 30
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(yaml_file)

    def test_iter_list_json_list(self):
        """Test iter_list() with JSON list data."""
        json_content = '''[
  {"name": "Alice", "age": 30},
  {"name": "Bob", "age": 25},
  {"name": "Charlie", "age": 35}
]'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_data = LazyLoadedFileData(json_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 3
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == 30
            
        finally:
            os.unlink(json_file)

    def test_iter_list_yaml_list_mutable(self):
        """Test iter_list() mutable option with YAML list."""
        yaml_content = """
- name: Alice
  age: 30
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            
            # Test mutable=True
            rows_mutable = list(lazy_data.iter_list(mutable=True))
            rows_mutable[0]['test'] = 'value'
            assert rows_mutable[0]['test'] == 'value'
            
            # Test mutable=False (default)
            rows_immutable = list(lazy_data.iter_list(mutable=False))
            with pytest.raises(TypeError):
                rows_immutable[0]['test'] = 'value'
                
        finally:
            os.unlink(yaml_file)

    def test_iter_list_yaml_list_non_dict_items(self):
        """Test iter_list() with YAML list containing non-dict items."""
        yaml_content = """
- "string item"
- 42
- name: dict item
  value: test
- [1, 2, 3]
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            items = list(lazy_data.iter_list())
            assert len(items) == 4
            assert items[0] == "string item"
            assert items[1] == 42
            # Dict items are wrapped in MappingProxyType when mutable=False (default)
            from types import MappingProxyType
            assert isinstance(items[2], (dict, MappingProxyType))
            assert items[2]['name'] == 'dict item'
            assert items[3] == [1, 2, 3]
            
        finally:
            os.unlink(yaml_file)

    def test_iter_list_yaml_empty_list(self):
        """Test iter_list() with empty YAML list."""
        yaml_content = "[]"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 0
            
        finally:
            os.unlink(yaml_file)

    def test_iter_list_json_empty_list(self):
        """Test iter_list() with empty JSON list."""
        json_content = "[]"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json_content)
            json_file = f.name

        try:
            lazy_data = LazyLoadedFileData(json_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 0
            
        finally:
            os.unlink(json_file)

    def test_iter_list_error_dict_data(self):
        """Test iter_list() raises ValueError for dict data."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            with pytest.raises(ValueError, match="dictionary data and requires as_dict"):
                list(lazy_data.iter_list())
                
        finally:
            os.unlink(yaml_file)

    def test_iter_list_error_file_not_found(self):
        """Test iter_list() raises FileNotFoundError for missing file."""
        lazy_data = LazyLoadedFileData("/nonexistent/file.csv")
        with pytest.raises(FileNotFoundError):
            list(lazy_data.iter_list())

    def test_iter_list_default_data(self):
        """Test iter_list() with default_data parameter."""
        default_data = [
            {'name': 'Alice', 'age': 30},
            {'name': 'Bob', 'age': 25}
        ]
        
        lazy_data = LazyLoadedFileData("/nonexistent/file.csv", default_data=default_data)
        rows = list(lazy_data.iter_list())
        assert len(rows) == 2
        assert rows[0]['name'] == 'Alice'
        assert rows[1]['name'] == 'Bob'

    def test_iter_list_default_data_mutable(self):
        """Test iter_list() mutable option with default_data."""
        default_data = [{'name': 'Alice', 'age': 30}]
        
        lazy_data = LazyLoadedFileData("/nonexistent/file.csv", default_data=default_data)
        
        # Test mutable=True
        rows_mutable = list(lazy_data.iter_list(mutable=True))
        rows_mutable[0]['test'] = 'value'
        assert rows_mutable[0]['test'] == 'value'
        
        # Test mutable=False
        rows_immutable = list(lazy_data.iter_list(mutable=False))
        with pytest.raises(TypeError):
            rows_immutable[0]['test'] = 'value'

    def test_iter_list_flex_headers_basic(self):
        """Test iter_list() with flex headers."""
        csv_content = """Title: Sales Report
Generated: 2024-01-01

name,age,city,salary
Alice,30,NYC,50000
Bob,25,LA,60000"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['salary'] == '50000'
            assert 'name' in rows[0]
            assert 'age' in rows[0]
            assert 'city' in rows[0]
            assert 'salary' in rows[0]
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_flex_headers_ignore_columns(self):
        """Test iter_list() with flex headers and ignored columns."""
        csv_content = """name,age,#notes,city
Alice,30,Some note,NYC
Bob,25,Another note,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=3)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 2
            assert 'name' in rows[0]
            assert 'age' in rows[0]
            assert 'city' in rows[0]
            assert '#notes' not in rows[0]  # Should be ignored
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_fresh_read(self):
        """Test that iter_list() always reads fresh from file."""
        csv_content = "name,age\nAlice,30"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # First iteration
            rows1 = list(lazy_data.iter_list())
            assert len(rows1) == 1
            assert rows1[0]['name'] == 'Alice'
            
            # Modify file
            with open(csv_file, 'w') as f:
                f.write("name,age\nBob,25\nCharlie,35")
            
            # Second iteration should see new data (fresh read)
            rows2 = list(lazy_data.iter_list())
            assert len(rows2) == 2
            assert rows2[0]['name'] == 'Bob'
            assert rows2[1]['name'] == 'Charlie'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_duplicate_headers_error(self):
        """Test iter_list() raises error for duplicate headers."""
        csv_content = "name,age,name\nAlice,30,NYC"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            with pytest.raises(ValueError, match="Duplicate column headers"):
                list(lazy_data.iter_list())
                
        finally:
            os.unlink(csv_file)

    def test_iter_list_csv_whitespace_in_headers(self):
        """Test iter_list() strips whitespace from headers."""
        csv_content = " name , age , city \nAlice,30,NYC"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            rows = list(lazy_data.iter_list())
            assert 'name' in rows[0]  # Should be stripped
            assert 'age' in rows[0]
            assert 'city' in rows[0]
            assert rows[0]['name'] == 'Alice'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_unsupported_format_error(self):
        """Test iter_list() raises error for unsupported format."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            txt_file = f.name

        try:
            lazy_data = LazyLoadedFileData(txt_file)
            with pytest.raises(ValueError, match="Unsupported file format"):
                list(lazy_data.iter_list())
                
        finally:
            os.unlink(txt_file)

    # ==================== Additional coverage improvements ====================
    
    def test_import_toml_fallback(self):
        """Test TOML import fallback mechanism."""
        from totodev_pub.lazy_loaded_file_data import _import_toml
        
        # This should either work or raise ImportError, but not crash
        try:
            loads_func, dumps_func = _import_toml()
            assert loads_func is not None
            assert dumps_func is not None
        except ImportError:
            # TOML libraries not available - this is expected in some environments
            pass

    def test_import_csv_function(self):
        """Test CSV import function."""
        from totodev_pub.lazy_loaded_file_data import _import_csv
        csv_reader = _import_csv()
        assert csv_reader is not None
        assert callable(csv_reader)

    # ==================== JSONL/NDJSON format tests ====================
    
    def test_jsonl_loading_with_as_list(self):
        """Test loading JSONL files using as_list()."""
        jsonl_content = '{"name": "Alice", "age": 30}\n{"name": "Bob", "age": 25}\n{"name": "Charlie", "age": 35}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            
            # Test that data is not loaded until first access
            assert not lazy_data._loaded
            
            # Test accessing data using as_list()
            data = lazy_data.as_list()
            assert len(data) == 3
            assert data[0]['name'] == 'Alice'
            assert data[0]['age'] == 30
            assert data[1]['name'] == 'Bob'
            assert data[2]['name'] == 'Charlie'
            
            # Test that data is now loaded
            assert lazy_data._loaded
            
        finally:
            os.unlink(jsonl_file)

    def test_ndjson_loading_with_as_list(self):
        """Test loading NDJSON files using as_list()."""
        ndjson_content = '{"name": "Alice", "age": 30}\n{"name": "Bob", "age": 25}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ndjson', delete=False) as f:
            f.write(ndjson_content)
            ndjson_file = f.name

        try:
            lazy_data = LazyLoadedFileData(ndjson_file)
            data = lazy_data.as_list()
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(ndjson_file)

    def test_jsonl_iter_list_basic(self):
        """Test basic JSONL iteration with iter_list()."""
        jsonl_content = '{"name": "Alice", "age": 30}\n{"name": "Bob", "age": 25}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == 30
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_ndjson_iter_list_basic(self):
        """Test basic NDJSON iteration with iter_list()."""
        ndjson_content = '{"name": "Alice", "age": 30}\n{"name": "Bob", "age": 25}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ndjson', delete=False) as f:
            f.write(ndjson_content)
            ndjson_file = f.name

        try:
            lazy_data = LazyLoadedFileData(ndjson_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(ndjson_file)

    def test_jsonl_iter_list_matches_as_list(self):
        """Test that iter_list() produces same results as as_list() for JSONL."""
        jsonl_content = '{"name": "Alice", "age": 30}\n{"name": "Bob", "age": 25}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            
            iter_rows = list(lazy_data.iter_list())
            as_list_rows = lazy_data.as_list()
            
            assert len(iter_rows) == len(as_list_rows)
            assert iter_rows == as_list_rows
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_empty_file(self):
        """Test JSONL with empty file."""
        jsonl_content = ""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 0
            
            data = lazy_data.as_list()
            assert len(data) == 0
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_empty_lines(self):
        """Test JSONL with empty lines (should be skipped)."""
        jsonl_content = '{"name": "Alice"}\n\n{"name": "Bob"}\n\n'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            rows = list(lazy_data.iter_list())
            # Should have 2 rows (empty lines skipped)
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_whitespace_only_lines(self):
        """Test JSONL with whitespace-only lines (should be skipped)."""
        jsonl_content = '{"name": "Alice"}\n   \n{"name": "Bob"}\n\t\n'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            rows = list(lazy_data.iter_list())
            # Should have 2 rows (whitespace-only lines skipped)
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_mutable_immutable(self):
        """Test JSONL with mutable and immutable options."""
        jsonl_content = '{"name": "Alice", "age": 30}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            
            # Test mutable=True
            rows_mutable = list(lazy_data.iter_list(mutable=True))
            rows_mutable[0]['test'] = 'value'
            assert rows_mutable[0]['test'] == 'value'
            
            # Test mutable=False (default)
            rows_immutable = list(lazy_data.iter_list(mutable=False))
            with pytest.raises(TypeError):
                rows_immutable[0]['test'] = 'value'
                
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_empty_lines_with_as_list(self):
        """Test JSONL with empty lines using as_list() (should be skipped)."""
        jsonl_content = '{"name": "Alice"}\n\n{"name": "Bob"}\n\n'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            rows = lazy_data.as_list()
            # Should have 2 rows (empty lines skipped)
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_invalid_json_error(self):
        """Test JSONL with invalid JSON on a line (should report line number)."""
        jsonl_content = '{"name": "Alice"}\n{"invalid": json}\n{"name": "Bob"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            with pytest.raises(ValueError, match="Error parsing JSONL file at line 2"):
                list(lazy_data.iter_list())
                
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_invalid_json_error_with_as_list(self):
        """Test JSONL with invalid JSON on a line using as_list() (should report line number)."""
        jsonl_content = '{"name": "Alice"}\n{"invalid": json}\n{"name": "Bob"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            with pytest.raises(ValueError, match="Error parsing JSONL file at line 2"):
                lazy_data.as_list()
                
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_non_dict_items(self):
        """Test JSONL with non-dict JSON objects (strings, numbers, arrays)."""
        jsonl_content = '"string item"\n42\n[1, 2, 3]\n{"name": "dict item"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            items = list(lazy_data.iter_list())
            assert len(items) == 4
            assert items[0] == "string item"
            assert items[1] == 42
            assert items[2] == [1, 2, 3]
            # Dict items are wrapped in MappingProxyType when mutable=False (default)
            from types import MappingProxyType
            assert isinstance(items[3], (dict, MappingProxyType))
            assert items[3]['name'] == 'dict item'
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_fresh_read(self):
        """Test that iter_list() always reads fresh from JSONL file."""
        jsonl_content = '{"name": "Alice"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            
            # First iteration
            rows1 = list(lazy_data.iter_list())
            assert len(rows1) == 1
            assert rows1[0]['name'] == 'Alice'
            
            # Modify file
            with open(jsonl_file, 'w') as f:
                f.write('{"name": "Bob"}\n{"name": "Charlie"}')
            
            # Second iteration should see new data (fresh read)
            rows2 = list(lazy_data.iter_list())
            assert len(rows2) == 2
            assert rows2[0]['name'] == 'Bob'
            assert rows2[1]['name'] == 'Charlie'
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_error_file_not_found(self):
        """Test JSONL raises FileNotFoundError for missing file."""
        lazy_data = LazyLoadedFileData("/nonexistent/file.jsonl")
        with pytest.raises(FileNotFoundError):
            list(lazy_data.iter_list())

    def test_jsonl_default_data(self):
        """Test JSONL with default_data parameter."""
        default_data = [
            {'name': 'Alice', 'age': 30},
            {'name': 'Bob', 'age': 25}
        ]
        
        lazy_data = LazyLoadedFileData("/nonexistent/file.jsonl", default_data=default_data)
        rows = list(lazy_data.iter_list())
        assert len(rows) == 2
        assert rows[0]['name'] == 'Alice'
        assert rows[1]['name'] == 'Bob'

    # ==================== ignore_comments parameter tests ====================
    
    def test_csv_ignore_comments_basic(self):
        """Test CSV with ignore_comments=True skips comment lines in data rows."""
        csv_content = "name,age,city\nAlice,30,NYC\n# This is a comment\nBob,25,LA\n# Another comment\nCharlie,35,Chicago"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # Without ignore_comments (default)
            data_default = lazy_data.as_list()
            assert len(data_default) == 5  # Includes comment lines as data
            
            # With ignore_comments=True
            data_no_comments = lazy_data.as_list(ignore_comments=True)
            assert len(data_no_comments) == 3
            assert data_no_comments[0]['name'] == 'Alice'
            assert data_no_comments[1]['name'] == 'Bob'
            assert data_no_comments[2]['name'] == 'Charlie'
            
        finally:
            os.unlink(csv_file)

    def test_csv_ignore_comments_header_not_skipped(self):
        """Test CSV with ignore_comments=True does NOT skip header even if it starts with '#'."""
        csv_content = "#name,age,city\nAlice,30,NYC\nBob,25,LA"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            data = lazy_data.as_list(ignore_comments=True)
            # Header should NOT be skipped - it's the first row, treated as header
            assert len(data) == 2
            assert '#name' in data[0]  # Header name includes '#'
            assert data[0]['#name'] == 'Alice'
            
        finally:
            os.unlink(csv_file)

    def test_csv_ignore_comments_with_whitespace(self):
        """Test CSV with ignore_comments=True skips lines with leading whitespace before '#'."""
        csv_content = "name,age\nAlice,30\n  # Comment with spaces\nBob,25\n\t# Comment with tab"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            data = lazy_data.as_list(ignore_comments=True)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(csv_file)

    def test_csv_ignore_comments_flex_headers(self):
        """Test CSV with ignore_comments=True and flex headers."""
        csv_content = """Title: Report
# Comment before header
name,age,city
Alice,30,NYC
# Comment in data
Bob,25,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            data = lazy_data.as_list(ignore_comments=True)
            # Should have 2 data rows (comments skipped)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(csv_file)

    def test_tsv_ignore_comments(self):
        """Test TSV with ignore_comments=True."""
        tsv_content = "name\tage\nAlice\t30\n# Comment\nBob\t25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            f.write(tsv_content)
            tsv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(tsv_file)
            data = lazy_data.as_list(ignore_comments=True)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(tsv_file)

    def test_jsonl_ignore_comments_basic(self):
        """Test JSONL with ignore_comments=True skips comment lines."""
        jsonl_content = '{"name": "Alice"}\n# This is a comment\n{"name": "Bob"}\n  # Comment with spaces'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            data = lazy_data.as_list(ignore_comments=True)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_jsonl_ignore_comments_with_whitespace(self):
        """Test JSONL with ignore_comments=True and leading whitespace before '#'."""
        jsonl_content = '{"name": "Alice"}\n  # Comment with spaces\n{"name": "Bob"}\n\t# Comment with tab'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            data = lazy_data.as_list(ignore_comments=True)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_ndjson_ignore_comments(self):
        """Test NDJSON with ignore_comments=True."""
        ndjson_content = '{"name": "Alice"}\n# Comment\n{"name": "Bob"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ndjson', delete=False) as f:
            f.write(ndjson_content)
            ndjson_file = f.name

        try:
            lazy_data = LazyLoadedFileData(ndjson_file)
            data = lazy_data.as_list(ignore_comments=True)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(ndjson_file)

    def test_iter_list_csv_ignore_comments(self):
        """Test iter_list() with ignore_comments=True for CSV."""
        csv_content = "name,age\nAlice,30\n# Comment\nBob,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            rows = list(lazy_data.iter_list(ignore_comments=True))
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_jsonl_ignore_comments(self):
        """Test iter_list() with ignore_comments=True for JSONL."""
        jsonl_content = '{"name": "Alice"}\n# Comment\n{"name": "Bob"}'
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(jsonl_content)
            jsonl_file = f.name

        try:
            lazy_data = LazyLoadedFileData(jsonl_file)
            rows = list(lazy_data.iter_list(ignore_comments=True))
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[1]['name'] == 'Bob'
            
        finally:
            os.unlink(jsonl_file)

    def test_ignore_comments_cache_invalidation(self):
        """Test that changing ignore_comments flag invalidates cache."""
        csv_content = "name,age\nAlice,30\n# Comment\nBob,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # First call with ignore_comments=False (default)
            data1 = lazy_data.as_list(ignore_comments=False)
            assert len(data1) == 3  # Includes comment line
            
            # Second call with ignore_comments=True - should reload
            data2 = lazy_data.as_list(ignore_comments=True)
            assert len(data2) == 2  # Comments skipped
            
            # Third call with same flag - should use cache
            data3 = lazy_data.as_list(ignore_comments=True)
            assert len(data3) == 2
            assert data2 == data3
            
        finally:
            os.unlink(csv_file)

    def test_ignore_comments_cache_same_flag(self):
        """Test that same ignore_comments flag value uses cache."""
        csv_content = "name,age\nAlice,30\n# Comment\nBob,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            
            # First call
            data1 = lazy_data.as_list(ignore_comments=True)
            assert len(data1) == 2
            
            # Second call with same flag - should use cache
            data2 = lazy_data.as_list(ignore_comments=True)
            assert len(data2) == 2
            assert data1 == data2
            
        finally:
            os.unlink(csv_file)

    def test_ignore_comments_empty_line_vs_comment(self):
        """Test that empty lines and comment lines are handled separately."""
        csv_content = "name,age\nAlice,30\n\n# Comment\n\nBob,25"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file)
            data = lazy_data.as_list(ignore_comments=True)
            # Should have 2 data rows (empty lines and comments both skipped)
            assert len(data) == 2
            assert data[0]['name'] == 'Alice'
            assert data[1]['name'] == 'Bob'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_flex_headers_fewer_rows_than_limit(self):
        """Test iter_list() with flex headers when file has fewer rows than limit."""
        csv_content = """name,age
Alice,30"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 1
            assert rows[0]['name'] == 'Alice'
            
        finally:
            os.unlink(csv_file)

    def test_iter_list_flex_headers_empty_after_reading_candidates(self):
        """Test iter_list() with flex headers when file is empty after reading candidates."""
        csv_content = ""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 0
            
        finally:
            os.unlink(csv_file)


    def test_iter_list_toml_list(self):
        """Test iter_list() with TOML list data (if TOML supports lists)."""
        # TOML doesn't support top-level lists, but we can test the error path
        toml_content = """name = "test"
value = 42"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            f.write(toml_content)
            toml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(toml_file)
            # TOML files are typically dicts, so this should raise an error
            with pytest.raises(ValueError, match="dictionary data and requires as_dict"):
                list(lazy_data.iter_list())
                
        finally:
            os.unlink(toml_file)

    def test_iter_list_yaml_empty_content(self):
        """Test iter_list() with YAML file containing only whitespace."""
        yaml_content = "   \n  \n  "
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_data = LazyLoadedFileData(yaml_file)
            rows = list(lazy_data.iter_list())
            # Empty content should result in empty list
            assert len(rows) == 0
            
        finally:
            os.unlink(yaml_file)

    def test_iter_list_flex_headers_missing_column(self):
        """Test iter_list() with flex headers and missing columns."""
        csv_content = """Title: Report

name,age,city
Alice,30
Bob,25,LA"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_file = f.name

        try:
            lazy_data = LazyLoadedFileData(csv_file, flex_header_limit=5)
            rows = list(lazy_data.iter_list())
            assert len(rows) == 2
            assert rows[0]['name'] == 'Alice'
            assert rows[0]['age'] == '30'
            assert rows[0]['city'] is None  # Missing column
            assert rows[1]['name'] == 'Bob'
            assert rows[1]['age'] == '25'
            assert rows[1]['city'] == 'LA'
            
        finally:
            os.unlink(csv_file)
