# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for the SerializableDataProxy class.
"""

import pytest
import tempfile
import os
import json
import yaml
import asyncio
from pydantic import BaseModel
from typing import Dict, Any, List

from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy


class ExampleModel(BaseModel):
    """Example Pydantic model for testing."""
    name: str
    age: int
    active: bool = True


class TestSerializableDataProxy:
    """Test cases for SerializableDataProxy."""
    
    def test_init_with_pydantic_model_json(self):
        """Test initialization with Pydantic model and JSON ref_path."""
        model = ExampleModel(name="Alice", age=30)
        proxy = SerializableDataProxy(model, "test_data.json")
        
        assert proxy.ref_path() == "test_data.json"
        assert proxy._data == model
    
    def test_init_with_pydantic_model_yaml(self):
        """Test initialization with Pydantic model and YAML ref_path."""
        model = ExampleModel(name="Bob", age=25)
        proxy = SerializableDataProxy(model, "test_data.yaml")
        
        assert proxy.ref_path() == "test_data.yaml"
        assert proxy._data == model
    
    def test_init_with_dict_json(self):
        """Test initialization with dictionary and JSON ref_path."""
        data = {"name": "Charlie", "age": 35, "active": False}
        proxy = SerializableDataProxy(data, "test_data.json")
        
        assert proxy.ref_path() == "test_data.json"
        assert proxy._data == data
    
    def test_init_with_list_json(self):
        """Test initialization with list and JSON ref_path."""
        data = [1, 2, 3, {"nested": "data"}]
        proxy = SerializableDataProxy(data, "test_data.json")
        
        assert proxy.ref_path() == "test_data.json"
        assert proxy._data == data
    
    def test_init_with_yml_extension(self):
        """Test initialization with .yml extension."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test_data.yml")
        
        assert proxy.ref_path() == "test_data.yml"
    
    def test_init_invalid_extension(self):
        """Test initialization with invalid file extension."""
        data = {"test": "data"}
        
        with pytest.raises(ValueError, match="ref_path must have .json, .yaml, or .yml extension"):
            SerializableDataProxy(data, "test_data.txt")
        
        with pytest.raises(ValueError, match="ref_path must have .json, .yaml, or .yml extension"):
            SerializableDataProxy(data, "test_data")
    
    @pytest.mark.asyncio
    async def test_materialize_json_pydantic(self):
        """Test materialization of Pydantic model to JSON."""
        model = ExampleModel(name="David", age=40, active=False)
        proxy = SerializableDataProxy(model, "test_model.json")
        
        result = await proxy.materialize(0.0)
        assert result is True
        assert proxy._temp_file_path is not None
        assert os.path.exists(proxy._temp_file_path)
        assert proxy._temp_file_path.endswith('.json')
        
        # Verify content
        with open(proxy._temp_file_path, 'r') as f:
            content = json.load(f)
        
        expected = {"name": "David", "age": 40, "active": False}
        assert content == expected
    
    @pytest.mark.asyncio
    async def test_materialize_yaml_pydantic(self):
        """Test materialization of Pydantic model to YAML."""
        model = ExampleModel(name="Eve", age=28)
        proxy = SerializableDataProxy(model, "test_model.yaml")
        
        result = await proxy.materialize(0.0)
        assert result is True
        assert proxy._temp_file_path is not None
        assert os.path.exists(proxy._temp_file_path)
        assert proxy._temp_file_path.endswith('.yaml')
        
        # Verify content
        with open(proxy._temp_file_path, 'r') as f:
            content = yaml.safe_load(f)
        
        expected = {"name": "Eve", "age": 28, "active": True}
        assert content == expected
    
    @pytest.mark.asyncio
    async def test_materialize_json_dict(self):
        """Test materialization of dictionary to JSON."""
        data = {"users": ["Alice", "Bob"], "count": 2, "active": True}
        proxy = SerializableDataProxy(data, "test_dict.json")
        
        result = await proxy.materialize(0.0)
        assert result is True
        
        # Verify content
        with open(proxy._temp_file_path, 'r') as f:
            content = json.load(f)
        
        assert content == data
    
    @pytest.mark.asyncio
    async def test_materialize_yaml_list(self):
        """Test materialization of list to YAML."""
        data = [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}]
        proxy = SerializableDataProxy(data, "test_list.yaml")
        
        result = await proxy.materialize(0.0)
        assert result is True
        
        # Verify content
        with open(proxy._temp_file_path, 'r') as f:
            content = yaml.safe_load(f)
        
        assert content == data
    
    @pytest.mark.asyncio
    async def test_materialize_idempotent(self):
        """Test that materialization is idempotent."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        result1 = await proxy.materialize(0.0)
        temp_path1 = proxy._temp_file_path
        
        result2 = await proxy.materialize(0.0)
        temp_path2 = proxy._temp_file_path
        
        assert result1 is True
        assert result2 is True
        assert temp_path1 == temp_path2  # Same temp file
    
    def test_deploy_to_dev_null(self):
        """Test deployment to /dev/null."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        # Should not raise an error even without materialization
        proxy.deploy("/dev/null")
        assert proxy._was_deployed is True
    
    @pytest.mark.asyncio
    async def test_deploy_to_directory(self):
        """Test deployment to actual directory."""
        data = {"deployed": "data"}
        proxy = SerializableDataProxy(data, "deployed_data.json")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Materialize first
            await proxy.materialize(0.0)
            
            # Deploy
            proxy.deploy(temp_dir)
            
            # Verify file was created
            target_file = os.path.join(temp_dir, "deployed_data.json")
            assert os.path.exists(target_file)
            
            # Verify content
            with open(target_file, 'r') as f:
                content = json.load(f)
            assert content == data
            
            assert proxy._was_deployed is True
    
    def test_deploy_without_materialization(self):
        """Test that deployment fails without materialization."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with pytest.raises(RuntimeError, match="File must be materialized before deployment"):
                proxy.deploy(temp_dir)
    
    def test_deploy_twice_error(self):
        """Test that deployment fails if already deployed."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        # First deployment to /dev/null
        proxy.deploy("/dev/null")
        
        # Second deployment should fail
        with pytest.raises(RuntimeError, match="File has already been deployed"):
            proxy.deploy("/dev/null")
    
    def test_deploy_to_nonexistent_directory(self):
        """Test deployment to non-existent directory fails."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        with pytest.raises(FileNotFoundError, match="Target directory does not exist"):
            proxy.deploy("/nonexistent/directory")
    
    @pytest.mark.asyncio
    async def test_looks_same_with_same_data(self):
        """Test looks_same with identical data."""
        data = {"same": "data"}
        proxy1 = SerializableDataProxy(data, "test1.json")
        proxy2 = SerializableDataProxy(data, "test2.json")
        
        # Materialize both
        await proxy1.materialize(0.0)
        await proxy2.materialize(0.0)
        
        # Compare
        result = proxy1.looks_same(proxy2._temp_file_path)
        # Should be True since the serialized data is identical
        assert result is True
    
    @pytest.mark.asyncio
    async def test_looks_same_with_different_data(self):
        """Test looks_same with different data."""
        data1 = {"different": "data1"}
        data2 = {"different": "data2"}
        proxy1 = SerializableDataProxy(data1, "test1.json")
        proxy2 = SerializableDataProxy(data2, "test2.json")
        
        # Materialize both
        await proxy1.materialize(0.0)
        await proxy2.materialize(0.0)
        
        # Compare
        result = proxy1.looks_same(proxy2._temp_file_path)
        # Should be False since the data is different
        assert result is False
    
    def test_looks_same_with_nonexistent_file(self):
        """Test looks_same with non-existent file."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        result = proxy.looks_same("/nonexistent/file.json")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_looks_same_from_async_context_without_materialization(self):
        """Test looks_same returns None when called from async context without materialization.
        
        This tests the fix for the bug where asyncio.run() was called from an async context.
        When called from an async context, looks_same() should return None (can't determine)
        rather than raising RuntimeError.
        """
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        # Call looks_same from async context without materializing first
        # This should return None (can't determine) instead of raising RuntimeError
        result = proxy.looks_same("/some/file.json")
        assert result is None, "Should return None when called from async context without materialization"
        
        # Verify that materialization didn't happen (temp_file_path should still be None)
        assert proxy._temp_file_path is None
    
    def test_cleanup_on_destroy(self):
        """Test that temporary files are cleaned up when object is destroyed."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "test.json")
        
        # Materialize to create temp file
        asyncio.run(proxy.materialize(0.0))
        temp_path = proxy._temp_file_path
        
        # Verify temp file exists
        assert os.path.exists(temp_path)
        
        # Delete the proxy object
        del proxy
        
        # Temp file should be cleaned up
        assert not os.path.exists(temp_path)
    
    def test_file_name_method(self):
        """Test that file_name method works correctly."""
        data = {"test": "data"}
        proxy = SerializableDataProxy(data, "path/to/test_file.json")
        
        assert proxy.file_name() == "test_file.json"
        
        # Test with URL-like path
        proxy2 = SerializableDataProxy(data, "https://example.com/api/data.yaml")
        assert proxy2.file_name() == "data.yaml"
