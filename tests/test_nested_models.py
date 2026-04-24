# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
import pytest
import yaml
import json
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin, _LOCK_FILE_SUFFIX
import logging

# Define nested model structure
class Thing(BaseModel):
    """A simple nested model for testing."""
    x: int
    name: str = "default_thing"

class ThingOwner(BaseModel, FileMappedPydanticMixin):
    """A class that owns things."""
    things: List[Thing]
    name: str = "owner"

class NestedDict(BaseModel, FileMappedPydanticMixin):
    """A class with a nested dictionary."""
    items: Dict[str, Thing]
    description: str = "dict_container"

class DeepNested(BaseModel, FileMappedPydanticMixin):
    """A class with deeply nested structures."""
    owners: Dict[str, ThingOwner]
    metadata: Dict[str, str] = {}

@pytest.fixture
def yaml_nested_file(tmp_path: Path) -> Path:
    """Creates a temporary YAML file with nested model content."""
    file_path = tmp_path / "nested_data.yaml"
    sample_data = {
        "name": "test_owner",
        "things": [
            {"x": 1, "name": "thing1"},
            {"x": 2, "name": "thing2"},
            {"x": 3, "name": "thing3"}
        ]
    }
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(sample_data, f)
    return file_path

@pytest.fixture
def json_nested_file(tmp_path: Path) -> Path:
    """Creates a temporary JSON file with nested model content."""
    logger = logging.getLogger(__name__)
    file_path = tmp_path / "nested_data.json"
    sample_data = {
        "name": "json_owner",
        "things": [
            {"x": 10, "name": "json_thing1"},
            {"x": 20, "name": "json_thing2"}
        ]
    }
    logger.info(f"Writing sample data to {file_path}: {json.dumps(sample_data, indent=2)}")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sample_data, f, indent=2)
    
    # Verify what was written
    with open(file_path, "r") as f:
        content = f.read()
        logger.info(f"Actual file content: {content}")
    
    # Verify the file exists and is readable
    assert os.path.exists(file_path), f"File {file_path} was not created"
    assert os.access(file_path, os.R_OK), f"File {file_path} is not readable"
    
    return file_path

@pytest.fixture
def yaml_dict_nested_file(tmp_path: Path) -> Path:
    """Creates a temporary YAML file with dictionary of models."""
    file_path = tmp_path / "dict_nested.yaml"
    sample_data = {
        "description": "test_dict",
        "items": {
            "item1": {"x": 1, "name": "dict_thing1"},
            "item2": {"x": 2, "name": "dict_thing2"}
        }
    }
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(sample_data, f)
    return file_path

@pytest.fixture
def yaml_deep_nested_file(tmp_path: Path) -> Path:
    """Creates a temporary YAML file with deeply nested models."""
    file_path = tmp_path / "deep_nested.yaml"
    sample_data = {
        "metadata": {"version": "1.0", "author": "test"},
        "owners": {
            "deep_owner": {
                "name": "deep_owner",
                "things": [
                    {"x": 100, "name": "deep_thing1"},
                    {"x": 200, "name": "deep_thing2"}
                ]
            }
        }
    }
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(sample_data, f)
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
                        os.remove(os.path.join(root, file))
                    except OSError:
                        pass
    
    # Clean up the temporary directory
    remove_lock_files(tmp_path)
    
    yield  # Run the test
    
    # Teardown - clean up again
    remove_lock_files(tmp_path)

def test_load_nested_yaml(yaml_nested_file: Path) -> None:
    """Test loading a file with nested models from YAML."""
    model = ThingOwner.open(str(yaml_nested_file))
    
    # Verify the model was loaded correctly
    assert model.name == "test_owner"
    assert len(model.things) == 3
    
    # Verify the nested objects are proper Thing instances
    assert isinstance(model.things[0], Thing)
    assert model.things[0].x == 1
    assert model.things[0].name == "thing1"
    
    assert isinstance(model.things[1], Thing)
    assert model.things[1].x == 2
    assert model.things[1].name == "thing2"
    
    assert isinstance(model.things[2], Thing)
    assert model.things[2].x == 3
    assert model.things[2].name == "thing3"

def test_load_nested_json(json_nested_file: Path) -> None:
    """Test loading a file with nested models from JSON."""
    logger = logging.getLogger(__name__)
    logger.info(f"Starting test_load_nested_json with file: {json_nested_file}")
    
    # Verify the JSON file exists and has correct content
    with open(json_nested_file, "r") as f:
        content = f.read()
        logger.info(f"Initial JSON file content: {content}")
    
    model = ThingOwner.open(str(json_nested_file))
    logger.info(f"Loaded model state: {model.model_dump()}")
    
    # Verify the model was loaded correctly
    assert model.name == "json_owner"
    assert len(model.things) == 2
    
    # Verify the nested objects are proper Thing instances
    assert isinstance(model.things[0], Thing)
    assert model.things[0].x == 10
    assert model.things[0].name == "json_thing1"
    
    assert isinstance(model.things[1], Thing)
    assert model.things[1].x == 20
    assert model.things[1].name == "json_thing2"

def test_save_and_reload_nested(tmp_path: Path) -> None:
    """Test saving and reloading a model with nested objects."""
    file_path = tmp_path / "save_nested.yaml"
    
    # Create a model with nested objects
    things = [
        Thing(x=1, name="save_thing1"),
        Thing(x=2, name="save_thing2")
    ]
    model = ThingOwner(things=things, name="save_owner")
    
    # Save the model
    model._file_path = str(file_path)
    model._absolute_file_path = str(file_path.absolute())
    model._lock_acquired = True  # Simulate lock acquisition
    model.save()
    
    # Reload the model
    reloaded = ThingOwner.open(str(file_path))
    
    # Verify the reloaded model
    assert reloaded.name == "save_owner"
    assert len(reloaded.things) == 2
    assert isinstance(reloaded.things[0], Thing)
    assert reloaded.things[0].x == 1
    assert reloaded.things[0].name == "save_thing1"
    assert isinstance(reloaded.things[1], Thing)
    assert reloaded.things[1].x == 2
    assert reloaded.things[1].name == "save_thing2"

def test_dict_nested_models(yaml_dict_nested_file: Path) -> None:
    """Test loading a file with dictionary of nested models."""
    model = NestedDict.open(str(yaml_dict_nested_file))
    
    # Verify the model was loaded correctly
    assert model.description == "test_dict"
    assert len(model.items) == 2
    
    # Verify the nested objects in the dictionary are proper Thing instances
    assert isinstance(model.items["item1"], Thing)
    assert model.items["item1"].x == 1
    assert model.items["item1"].name == "dict_thing1"
    
    assert isinstance(model.items["item2"], Thing)
    assert model.items["item2"].x == 2
    assert model.items["item2"].name == "dict_thing2"

def test_deep_nested_models(yaml_deep_nested_file: Path) -> None:
    """Test loading a file with deeply nested models."""
    model = DeepNested.open(str(yaml_deep_nested_file))
    
    # Verify the model was loaded correctly
    assert model.metadata == {"version": "1.0", "author": "test"}
    
    # Verify the nested owner object
    assert isinstance(model.owners["deep_owner"], ThingOwner)
    assert model.owners["deep_owner"].name == "deep_owner"
    
    # Verify the nested things in the owner
    assert len(model.owners["deep_owner"].things) == 2
    assert isinstance(model.owners["deep_owner"].things[0], Thing)
    assert model.owners["deep_owner"].things[0].x == 100
    assert model.owners["deep_owner"].things[0].name == "deep_thing1"
    
    assert isinstance(model.owners["deep_owner"].things[1], Thing)
    assert model.owners["deep_owner"].things[1].x == 200
    assert model.owners["deep_owner"].things[1].name == "deep_thing2"

def test_modify_and_save_nested(tmp_path: Path) -> None:
    """Test modifying nested objects and saving the changes."""
    file_path = tmp_path / "modify_nested.yaml"
    
    # Create initial model
    things = [Thing(x=1, name="initial_thing")]
    model = ThingOwner(things=things, name="initial_owner")
    
    # Save the model
    model._file_path = str(file_path)
    model._absolute_file_path = str(file_path.absolute())
    model._lock_acquired = True
    model.save()
    
    # Explicitly release the lock
    model.release_lock()

    # Load, modify, and save again
    loaded = ThingOwner.open(str(file_path))
    loaded.things.append(Thing(x=99, name="added_thing"))
    loaded.things[0].name = "modified_thing"
    loaded.save()
    
    # Explicitly release the lock
    loaded.release_lock()

    # Reload and verify changes
    reloaded = ThingOwner.open(str(file_path))
    assert len(reloaded.things) == 2
    assert reloaded.things[0].name == "modified_thing"
    assert reloaded.things[1].x == 99
    assert reloaded.things[1].name == "added_thing"
    
    # Explicitly release the lock
    reloaded.release_lock() 