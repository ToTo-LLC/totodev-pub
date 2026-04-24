# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the ToToPipeTypeInfo class."""

import pytest
from pathlib import Path
from typing import Type
from unittest.mock import patch, MagicMock

from pydantic import BaseModel

from totodev_pub.pipes.toto_pipe_type_info import (
    ToToPipeTypeInfo,
    SPECIAL_NICKNAMES,
)
from totodev_pub.pipes.toto_pipe_begin_data import ToToPipeBeginData
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin


# Test Models
class SimpleInputModel(BaseModel, FileMappedPydanticMixin):
    """Simple test model for inputs."""
    value: str

class SimpleOutputModel(BaseModel, FileMappedPydanticMixin):
    """Simple test model for outputs."""
    result: str


@pytest.fixture
def working_dir(tmp_path) -> Path:
    """Creates a temporary working directory for tests."""
    return tmp_path

@pytest.fixture
def simple_type_info() -> ToToPipeTypeInfo:
    """Creates a ToToPipeTypeInfo instance with simple patterns for testing."""
    return ToToPipeTypeInfo(
        inputs={
            "single_input": ("input.json", SimpleInputModel),
            "wildcard_inputs": ("inputs/*.json", SimpleInputModel),
        },
        outputs={
            "single_output": ("output.json", SimpleOutputModel),
            "wildcard_outputs": ("outputs/*.json", SimpleOutputModel),
        },
        private_cfgs=["API_KEY"]
    )

def test_init_basic():
    """Test basic initialization of ToToPipeTypeInfo."""
    type_info = ToToPipeTypeInfo(
        inputs={"input": ("input.json", SimpleInputModel)},
        outputs={"output": ("output.json", SimpleOutputModel)},
        private_cfgs=["API_KEY"]
    )
    
    assert len(type_info.inputs) == 1
    assert len(type_info.outputs) == 1
    assert type_info.required_private_configs == ["API_KEY"]

def test_init_nickname_collision():
    """Test that nickname collisions between inputs and outputs raise ValueError."""
    with pytest.raises(ValueError, match="Nicknames cannot appear in both inputs and outputs"):
        ToToPipeTypeInfo(
            inputs={"same_name": ("input.json", SimpleInputModel)},
            outputs={"same_name": ("output.json", SimpleOutputModel)},
            private_cfgs=[]
        )

def test_init_invalid_input_glob_type():
    """Test that non-string glob patterns in inputs raise TypeError."""
    with pytest.raises(TypeError, match="Input glob pattern for 'input' must be a string"):
        ToToPipeTypeInfo(
            inputs={"input": (123, SimpleInputModel)},  # type: ignore
            outputs={},
            private_cfgs=[]
        )

def test_init_invalid_input_model_type():
    """Test that invalid model types in inputs raise TypeError."""
    class NonMixinModel(BaseModel):
        value: str

    with pytest.raises(TypeError, match="Input model type for 'input' must be None or a subclass of FileMappedPydanticMixin"):
        ToToPipeTypeInfo(
            inputs={"input": ("input.json", NonMixinModel)},
            outputs={},
            private_cfgs=[]
        )

def test_init_invalid_output_glob_type():
    """Test that non-string glob patterns in outputs raise TypeError."""
    with pytest.raises(TypeError, match="Output glob pattern for 'output' must be a string"):
        ToToPipeTypeInfo(
            inputs={},
            outputs={"output": (123, SimpleOutputModel)},  # type: ignore
            private_cfgs=[]
        )

def test_init_invalid_output_model_type():
    """Test that invalid model types in outputs raise TypeError."""
    class NonMixinModel(BaseModel):
        value: str

    with pytest.raises(TypeError, match="Output model type for 'output' must be None or a subclass of FileMappedPydanticMixin"):
        ToToPipeTypeInfo(
            inputs={},
            outputs={"output": ("output.json", NonMixinModel)},
            private_cfgs=[]
        )

def test_init_invalid_private_cfgs_type():
    """Test that non-list private_cfgs raises TypeError."""
    with pytest.raises(TypeError, match="private_cfgs must be a list of strings"):
        ToToPipeTypeInfo(
            inputs={},
            outputs={},
            private_cfgs={"API_KEY"}  # type: ignore
        )

def test_init_invalid_private_cfgs_element_type():
    """Test that non-string elements in private_cfgs raise TypeError."""
    with pytest.raises(TypeError, match="private_cfgs must be a list of strings"):
        ToToPipeTypeInfo(
            inputs={},
            outputs={},
            private_cfgs=[123]  # type: ignore
        )

def test_create_pattern_subdirs(working_dir, simple_type_info):
    """Test creation of subdirectories for patterns."""
    # Create subdirs
    simple_type_info.create_pattern_subdirs(working_dir)
    
    # Check that input/output dirs exist
    assert (working_dir / "inputs").is_dir()
    assert (working_dir / "outputs").is_dir()

def test_missing_inputs_empty_dir(working_dir, simple_type_info):
    """Test missing_inputs on an empty directory."""
    missing = simple_type_info.missing_inputs(working_dir)
    assert len(missing) == 2
    assert "single_input" in missing
    assert "wildcard_inputs" in missing

def test_missing_inputs_partial(working_dir, simple_type_info):
    """Test missing_inputs when some files exist."""
    # Create one of the expected input files
    (working_dir / "input.json").write_text('{"value": "test"}')
    
    missing = simple_type_info.missing_inputs(working_dir)
    assert len(missing) == 1
    assert "wildcard_inputs" in missing
    assert "single_input" not in missing

def test_files_no_matches(working_dir, simple_type_info):
    """Test files() when no files match the pattern."""
    files = simple_type_info.files(working_dir, "single_input")
    assert len(files) == 0

def test_files_with_matches(working_dir, simple_type_info):
    """Test files() when files exist."""
    # Create test files
    input_file = working_dir / "input.json"
    input_file.write_text('{"value": "test"}')
    
    files = simple_type_info.files(working_dir, "single_input")
    assert len(files) == 1
    assert files[0].name == "input.json"

def test_files_load_to_memory(working_dir, simple_type_info):
    """Test files() with load_to_memory=True."""
    # Create test file
    input_file = working_dir / "input.json"
    input_file.write_text('{"value": "test"}')
    
    objects = simple_type_info.files(working_dir, "single_input", load_to_memory=True)
    assert len(objects) == 1
    assert isinstance(objects[0], SimpleInputModel)
    assert objects[0].value == "test"

def test_persist_input_file(working_dir, simple_type_info, tmp_path):
    """Test persist_input with a file path."""
    # Create source file
    source_file = tmp_path / "source.json"
    source_file.write_text('{"value": "test"}')
    
    # Persist it
    target_path = simple_type_info.persist_input(
        working_dir=working_dir,
        nickname="single_input",
        input_value=str(source_file)
    )
    
    assert target_path.exists()
    assert target_path.read_text() == '{"value": "test"}'

def test_persist_input_object(working_dir, simple_type_info):
    """Test persist_input with a data object."""
    input_obj = SimpleInputModel(value="test")
    
    target_path = simple_type_info.persist_input(
        working_dir=working_dir,
        nickname="single_input",
        input_value=input_obj
    )
    
    assert target_path.exists()
    loaded_obj = SimpleInputModel.load(target_path)
    assert loaded_obj.value == "test"

def test_persist_input_wildcard_no_merge(working_dir, simple_type_info):
    """Test persist_input with wildcard pattern but no merge_str."""
    input_obj = SimpleInputModel(value="test")
    
    with pytest.raises(ValueError, match="contains wildcards but no merge_str was provided"):
        simple_type_info.persist_input(
            working_dir=working_dir,
            nickname="wildcard_inputs",
            input_value=input_obj
        )

def test_persist_input_wildcard_with_merge(working_dir, simple_type_info):
    """Test persist_input with wildcard pattern and merge_str."""
    input_obj = SimpleInputModel(value="test")
    
    target_path = simple_type_info.persist_input(
        working_dir=working_dir,
        nickname="wildcard_inputs",
        input_value=input_obj,
        merge_str="001"
    )
    
    assert target_path.exists()
    assert target_path.name == "001.json"
    loaded_obj = SimpleInputModel.load(target_path)
    assert loaded_obj.value == "test"

def test_persist_input_invalid_nickname(working_dir, simple_type_info):
    """Test persist_input with invalid nickname."""
    with pytest.raises(ValueError, match="not found in inputs"):
        simple_type_info.persist_input(
            working_dir=working_dir,
            nickname="nonexistent",
            input_value=SimpleInputModel(value="test")
        )

def test_persist_input_nonexistent_file(working_dir, simple_type_info):
    """Test persist_input with nonexistent source file."""
    with pytest.raises(FileNotFoundError):
        simple_type_info.persist_input(
            working_dir=working_dir,
            nickname="single_input",
            input_value="/nonexistent/file.json"
        )

def test_persist_input_merge_str_no_wildcard(working_dir, simple_type_info):
    """Test persist_input with merge_str but no wildcard in pattern."""
    input_obj = SimpleInputModel(value="test")
    
    with pytest.raises(ValueError, match="merge_str .* was provided but pattern .* has no wildcards"):
        simple_type_info.persist_input(
            working_dir=working_dir,
            nickname="single_input",
            input_value=input_obj,
            merge_str="001"
        )

def test_calc_filepath_basic(working_dir, simple_type_info):
    """Test basic filepath calculation for non-wildcard patterns."""
    path = simple_type_info.calc_filepath(working_dir, "single_input")
    assert path == working_dir / "input.json"
    assert isinstance(path, Path)

def test_calc_filepath_wildcard_with_merge(working_dir, simple_type_info):
    """Test filepath calculation for wildcard patterns with merge string."""
    path = simple_type_info.calc_filepath(
        working_dir=working_dir,
        nickname="wildcard_inputs",
        merge_str="001"
    )
    assert path == working_dir / "inputs" / "001.json"
    assert isinstance(path, Path)

def test_calc_filepath_special_files(working_dir, simple_type_info):
    """Test filepath calculation for special files (begin, end, and heartbeat)."""
    begin_path = simple_type_info.calc_filepath(working_dir, simple_type_info.PIPE_BEGIN_NICKNAME)
    completion_path = simple_type_info.calc_filepath(working_dir, simple_type_info.PIPE_COMPLETION_NICKNAME)
    heartbeat_path = simple_type_info.calc_filepath(working_dir, simple_type_info.PIPE_HEARTBEAT_NICKNAME)
    
    assert begin_path == working_dir / SPECIAL_NICKNAMES[simple_type_info.PIPE_BEGIN_NICKNAME][0]
    assert completion_path == working_dir / SPECIAL_NICKNAMES[simple_type_info.PIPE_COMPLETION_NICKNAME][0]
    assert heartbeat_path == working_dir / SPECIAL_NICKNAMES[simple_type_info.PIPE_HEARTBEAT_NICKNAME][0]

def test_calc_filepath_invalid_nickname(working_dir, simple_type_info):
    """Test that invalid nicknames raise ValueError."""
    with pytest.raises(ValueError, match="not found in inputs or outputs"):
        simple_type_info.calc_filepath(working_dir, "nonexistent")

def test_calc_filepath_wildcard_no_merge(working_dir, simple_type_info):
    """Test that wildcard patterns without merge_str raise ValueError."""
    with pytest.raises(ValueError, match=r"Pattern '.*' has wildcards but merge_str is not provided"):
        simple_type_info.calc_filepath(working_dir, "wildcard_inputs")

def test_calc_filepath_merge_str_no_wildcard(working_dir, simple_type_info):
    """Test that providing merge_str for non-wildcard patterns raises ValueError."""
    with pytest.raises(ValueError, match="merge_str .* was provided but pattern .* has no wildcards"):
        simple_type_info.calc_filepath(
            working_dir=working_dir,
            nickname="single_input",
            merge_str="001"
        )

def test_calc_filepath_nonexistent_dir(simple_type_info):
    """Test that nonexistent working directory raises ValueError."""
    with pytest.raises(ValueError, match="Working directory must exist"):
        simple_type_info.calc_filepath(
            working_dir="/nonexistent/directory",
            nickname="single_input"
        )

def test_files_heartbeat_special_nickname(working_dir, simple_type_info):
    """Test accessing heartbeat file through the special nickname."""
    # Create a heartbeat file
    heartbeat_path = working_dir / SPECIAL_NICKNAMES[simple_type_info.PIPE_HEARTBEAT_NICKNAME][0]
    heartbeat_path.write_text("Heartbeat test content")

    # Access the heartbeat file
    heartbeat_files = simple_type_info.files(working_dir, simple_type_info.PIPE_HEARTBEAT_NICKNAME)
    assert len(heartbeat_files) == 1
    assert heartbeat_files[0].name == SPECIAL_NICKNAMES[simple_type_info.PIPE_HEARTBEAT_NICKNAME][0]
    
    # Since heartbeat file has no model class, load_to_memory should raise an error
    with pytest.raises(ValueError, match="Cannot load files for nickname.*as it has no model class"):
        simple_type_info.files(working_dir, simple_type_info.PIPE_HEARTBEAT_NICKNAME, load_to_memory=True)

def test_create_from_pipe_begin_with_obj():
    """Test create_from_pipe_begin with a ToToPipeBeginData object."""
    # Create a mock ToToPipeBeginData object
    begin_obj = MagicMock(spec=ToToPipeBeginData)
    begin_obj.inputs = {
        "input1": "input1.json",
        "input2": "inputs/*.json"
    }
    begin_obj.outputs = {
        "output1": "output1.json",
        "output2": "outputs/*.json"
    }
    begin_obj.private_configs = ["API_KEY", "SECRET"]
    begin_obj.heartbeat_timeout_secs = 180

    # Create ToToPipeTypeInfo from the begin object
    type_info = ToToPipeTypeInfo.create_from_pipe_begin(begin_obj=begin_obj)

    # Verify the type info was created correctly
    assert len(type_info.inputs) == 2
    assert len(type_info.outputs) == 2
    assert type_info.required_private_configs == ["API_KEY", "SECRET"]
    assert type_info.heartbeat_timeout_secs == 180

def test_create_from_pipe_begin_with_working_dir(tmp_path):
    """Test create_from_pipe_begin with a working directory."""
    # Create a mock begin file in the working directory
    begin_file_path = tmp_path / "_pipe_begin.yaml"

    # Create a mock for the load method
    mock_begin_obj = MagicMock(spec=ToToPipeBeginData)
    mock_begin_obj.inputs = {
        "input1": "input1.json",
        "input2": "inputs/*.json"
    }
    mock_begin_obj.outputs = {
        "output1": "output1.json",
        "output2": "outputs/*.json"
    }
    mock_begin_obj.private_configs = ["API_KEY", "SECRET"]
    mock_begin_obj.heartbeat_timeout_secs = 180

    # Create the begin file
    begin_file_path.touch()

    # Patch the load method to return our mock object
    with patch.object(ToToPipeBeginData, 'load', return_value=mock_begin_obj):
        # Create ToToPipeTypeInfo from the working directory
        type_info = ToToPipeTypeInfo.create_from_pipe_begin(working_dir=tmp_path)

        # Verify the type info was created correctly
        assert len(type_info.inputs) == 2
        assert len(type_info.outputs) == 2
        assert type_info.required_private_configs == ["API_KEY", "SECRET"]
        assert type_info.heartbeat_timeout_secs == 180

def test_create_from_pipe_begin_with_model_classes(tmp_path):
    """Test creating ToToPipeTypeInfo from a begin file with model classes."""
    # Create a begin file with model classes
    begin_obj = ToToPipeBeginData(
        task_classname="TestPipe",
        params={},
        private_configs=[],
        inputs={
            "input1": "TestModel1",
            "input2": "TestModel2"
        },
        outputs={}
    )
    begin_file = tmp_path / "_pipe_begin.yaml"
    begin_obj.save(str(begin_file))
    
    # Create type info from begin file
    type_info = ToToPipeTypeInfo.create_from_pipe_begin(tmp_path)
    
    # Verify inputs were loaded correctly
    assert len(type_info.inputs) == 2
    assert "input1" in type_info.inputs
    assert "input2" in type_info.inputs

def test_create_from_pipe_begin_with_begin_obj():
    """Test creating ToToPipeTypeInfo directly from a begin object."""
    # Create a begin object with model classes
    mock_begin_obj = ToToPipeBeginData(
        task_classname="TestPipe",
        params={},
        private_configs=[],
        inputs={
            "input1": "TestModel1",
            "input2": "TestModel2"
        },
        outputs={}
    )
    
    # Create type info from begin object
    type_info = ToToPipeTypeInfo.create_from_pipe_begin(begin_obj=mock_begin_obj)
    
    # Verify inputs were loaded correctly
    assert len(type_info.inputs) == 2
    assert "input1" in type_info.inputs
    assert "input2" in type_info.inputs

def test_files_raises_error_for_unknown_nickname():
    """Test that files() raises an error for unknown nicknames."""
    type_info = ToToPipeTypeInfo(
        inputs={},
        outputs={},
        private_cfgs=[]
    )
    
    with pytest.raises(ValueError, match="not found in inputs"):
        type_info.files("/tmp", "unknown", load_to_memory=True)

def test_calc_filepath_raises_error_for_unknown_nickname():
    """Test that calc_filepath() raises an error for unknown nicknames."""
    type_info = ToToPipeTypeInfo(
        inputs={},
        outputs={},
        private_cfgs=[]
    )

    with pytest.raises(ValueError, match="not found in inputs or outputs"):
        type_info.calc_filepath("/tmp", "unknown")

def test_persist_input_same_file(working_dir, simple_type_info):
    """Test persist_input when source and target paths are the same."""
    # Create the input file directly at the target location
    input_file = working_dir / "input.json"
    input_content = '{"value": "test"}'
    input_file.write_text(input_content)
    
    # Get file stats before operation
    original_stats = input_file.stat()
    
    # Try to persist the same file
    target_path = simple_type_info.persist_input(
        working_dir=working_dir,
        nickname="single_input",
        input_value=str(input_file)
    )
    
    # Verify the file wasn't modified
    assert target_path == input_file.resolve()
    assert target_path.read_text() == input_content
    assert target_path.stat() == original_stats  # Ensure file wasn't touched 

def test_calc_filepath_working_dir_handling(working_dir, simple_type_info):
    """Test that working directory is not duplicated in path calculation.
    
    This test verifies that:
    1. The working directory is only added once in the path
    2. Relative paths are correctly calculated
    3. Both absolute and relative paths work correctly
    """
    # Test with absolute path
    abs_path = simple_type_info.calc_filepath(working_dir, "single_input")
    assert abs_path == working_dir / "input.json"
    assert abs_path.is_absolute()
    
    # Test with wildcard pattern
    abs_path_wildcard = simple_type_info.calc_filepath(
        working_dir=working_dir,
        nickname="wildcard_inputs",
        merge_str="001"
    )
    assert abs_path_wildcard == working_dir / "inputs" / "001.json"
    assert abs_path_wildcard.is_absolute()
    
    # Test with special file
    abs_path_special = simple_type_info.calc_filepath(
        working_dir=working_dir,
        nickname=simple_type_info.PIPE_BEGIN_NICKNAME
    )
    assert abs_path_special == working_dir / SPECIAL_NICKNAMES[simple_type_info.PIPE_BEGIN_NICKNAME][0]
    assert abs_path_special.is_absolute()
    
    # Verify that the working directory is not duplicated in any path
    working_dir_str = str(working_dir)
    assert str(abs_path).count(working_dir_str) == 1
    assert str(abs_path_wildcard).count(working_dir_str) == 1
    assert str(abs_path_special).count(working_dir_str) == 1 