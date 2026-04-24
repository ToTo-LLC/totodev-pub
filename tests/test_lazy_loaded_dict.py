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
        """Test that the backward compatibility proxy remains immutable."""
        yaml_content = "name: test\nvalue: 42"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            lazy_dict = LazyLoadedFileData(yaml_file, acts_as_dict_proxy=True)
            
            # Load the data
            assert lazy_dict['name'] == 'test'
            
            # Verify attempts to modify data raise errors
            with pytest.raises(TypeError):
                lazy_dict['name'] = 'modified'
            with pytest.raises(TypeError):
                lazy_dict['new_key'] = 'new_value'
            
            # Data remains unchanged
            assert lazy_dict['name'] == 'test'
            assert 'new_key' not in lazy_dict
            assert len(lazy_dict) == 2
            
            # Test deletion attempts also fail
            with pytest.raises(TypeError):
                del lazy_dict['value']
            assert 'value' in lazy_dict
            
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
