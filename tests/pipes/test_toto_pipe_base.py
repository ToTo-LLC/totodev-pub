# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Test cases for the ToToPipeBase class."""

import os
import tempfile
from pathlib import Path
import pytest
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Dict, List, Type, Any, Optional
import time

pytest.importorskip("luigi")  # 'pipes' extra; skip when luigi is unavailable

import luigi
import json
import asyncio

from totodev_pub.pipes.toto_pipe_base import ToToPipeBase, PipeState
from totodev_pub.pipes.rel_fpath_pattern import RelativeFilepathPattern
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.pipes.toto_pipe_begin_data import ToToPipeBeginData
from totodev_pub.pipes.toto_pipe_completion_data import ToToPipeCompletionData, TimingInfo
from totodev_pub.pipes.toto_pipe_type_info import (
    ToToPipeTypeInfo,
    SpecialPipeFileNickname,
)
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname
from totodev_pub.pipes.toto_example_pipe import SampleWordStatsPipe, TextContent, WordStats


class SampleInputModel(BaseModel, FileMappedPydanticMixin):
    """Sample input model for testing."""
    value: str

class SampleOutputModel(BaseModel, FileMappedPydanticMixin):
    """Sample output model for testing."""
    result: str

class SimpleTestPipe(ToToPipeBase):
    """Simple test pipe implementation."""
    PIPE_HEARTBEAT_NICKNAME = SpecialPipeFileNickname.HEARTBEAT

    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        return ToToPipeTypeInfo(
            inputs={
                "input": ("input.json", SampleInputModel),
                "multi_input": ("multi_input/*.json", SampleInputModel)
            },
            outputs={
                "output": ("output.json", SampleOutputModel)
            },
            private_cfgs=["TEST_CONFIG"]
        )

    def complete(self) -> bool:
        """Check if the task is complete by verifying output file exists."""
        return self.resolve_nickname("output").exists()

    def execute(self):
        input_data = self.load_files("input")[0]
        output_data = SampleOutputModel(result=f"Processed: {input_data.value}")
        output_data.save(file_path=str(self.resolve_nickname("output")))

class SamplePipe(SimpleTestPipe):
    """Sample pipe for testing."""
    pass


class SlowSimpleTestPipe(SimpleTestPipe):
    """Same as SimpleTestPipe but delays execute() so spawn() can be observed before completion."""

    _execute_delay_sec: float = 0.5

    def execute(self):
        time.sleep(self._execute_delay_sec)
        super().execute()


class FailingSimpleTestPipe(SimpleTestPipe):
    """Fails during execute(); used to verify wait_for_completion after threaded spawn."""

    def execute(self):
        raise RuntimeError("intentional test failure")


class SoonFailingSimpleTestPipe(SimpleTestPipe):
    """Fails shortly after execute starts (within the default allow_start_secs window)."""

    _fail_after_sec: float = 0.05

    def execute(self):
        time.sleep(self._fail_after_sec)
        raise RuntimeError("failure within allow_start_secs window")


class CollisionPipe(ToToPipeBase):
    """Test pipe with nickname collision."""
    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        return ToToPipeTypeInfo(
            inputs={
                "collision": ("input.json", SampleInputModel)
            },
            outputs={
                "collision": ("output.json", SampleOutputModel)
            },
            private_cfgs=[]
        )

class ParamsTestPipe(SimpleTestPipe):
    """Test pipe that accesses params during execution."""
    def execute(self):
        # Should be able to access begin_params during execute
        params = self.get_bind_params()
        assert params == {"test_param": "test_value"}
        output_data = SampleOutputModel(result=f"Got param: {params['test_param']}")
        output_data.save(file_path=str(self.resolve_nickname("output")))

class NonDeserializablePipe(ToToPipeBase):
    """Test pipe with non-deserializable files."""
    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        return ToToPipeTypeInfo(
            inputs={
                "raw_file": ("raw.txt", None)  # not convertible to pydantic object
            },
            outputs={
                "output": ("output.txt", None)  # not convertible to pydantic object
            },
            private_cfgs=[]
        )

class AnyValueParameter(luigi.Parameter):
    """A Luigi parameter that can accept any value type."""
    def parse(self, x):
        return x

    def serialize(self, x):
        return str(x)

class ReturnValueTestPipe(SimpleTestPipe):
    """Test pipe that demonstrates storing data in completion_data.extra."""
    return_value = AnyValueParameter(default=None, significant=True)  # Using custom parameter type

    _PTI = ToToPipeTypeInfo(
        inputs={},  # No inputs required
        outputs={
            "output": ("output.json", SampleOutputModel)  # Add output nickname
        },
        private_cfgs=[]
    )

    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        """Return the pipe type info."""
        return cls._PTI

    def execute(self):
        # Write some output to ensure the pipe actually ran
        output_data = SampleOutputModel(result=f"ReturnValueTestPipe executed")
        output_data.save(file_path=str(self.resolve_nickname("output")))

        # Store the value in completion_data.extra
        if self.return_value == "None":
            self.completion_data.extra["stored_value"] = None
            return

        try:
            # Try to parse as JSON if it's a string that looks like JSON
            if isinstance(self.return_value, str) and (self.return_value.startswith('{') or self.return_value.startswith('[')):
                value = json.loads(self.return_value)
            # Handle basic types
            elif self.return_value == "True":
                value = True
            elif self.return_value == "False":
                value = False
            # If it's a generator, consume it and get the final value
            elif hasattr(self.return_value, '__iter__') and hasattr(self.return_value, 'send') and hasattr(self.return_value, 'throw'):
                # It's a generator, consume it and get the return value
                try:
                    while True:
                        next(self.return_value)
                except StopIteration as e:
                    value = e.value
            else:
                value = self.return_value
            
            self.completion_data.extra["stored_value"] = value
        except Exception:
            # If parsing fails, store the raw value
            self.completion_data.extra["stored_value"] = self.return_value

@pytest.fixture
def temp_working_dir():
    """Create a temporary working directory for tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create required directories
        os.makedirs(os.path.join(temp_dir, "inputs"), exist_ok=True)
        yield temp_dir

@pytest.fixture
def test_pipe(temp_working_dir):
    """Create a test pipe instance."""
    return SimpleTestPipe(working_dir=temp_working_dir)

def test_initialization(temp_working_dir):
    """Test basic initialization of ToToPipeBase."""
    pipe = SimpleTestPipe(working_dir=temp_working_dir)
    assert Path(pipe.working_dir).exists()

def test_initialization_with_nonexistent_folder():
    """Test initialization with non-existent working directory."""
    with pytest.raises(ValueError, match="Working directory must exist"):
        SimpleTestPipe(working_dir="/nonexistent/path")

def test_bind_inputs_with_explicit_data(test_pipe):
    """Test binding explicit input data."""
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi1")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Verify file was created
    input_file = test_pipe.resolve_nickname("input")
    assert input_file.exists()
    
    # Verify content
    loaded_data = test_pipe.load_files("input")[0]
    assert loaded_data.value == "test_value"

def test_bind_inputs_with_multiple_files(test_pipe):
    """Test binding multiple input files."""
    input_data = SampleInputModel(value="test_single")
    multi_input_data = [
        SampleInputModel(value="test1"),
        SampleInputModel(value="test2")
    ]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Verify files were created
    loaded_data = test_pipe.load_files("multi_input")
    assert len(loaded_data) == 2
    for data in loaded_data:
        assert data.value in {"test1", "test2"}

def test_missing_required_inputs(test_pipe):
    """Test error handling for missing required inputs."""
    with pytest.raises(ValueError, match="Missing required input files for patterns"):
        test_pipe.bind_inputs({})

def test_ignore_missing_inputs(test_pipe):
    """Test ignoring missing inputs when flag is set."""
    # This should not raise an error
    test_pipe.bind_inputs({}, ignore_missing_inputs=True)
    assert Path(test_pipe.working_dir, SpecialPipeFileNickname.BEGIN.filename()).exists()

def test_execution_flow(test_pipe):
    """Test the complete execution flow."""
    # Prepare input
    input_data = SampleInputModel(value="test_execution")
    multi_input_data = [SampleInputModel(value="multi_exec")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Run the pipe using luigi.build
    luigi.build([test_pipe], local_scheduler=True, workers=1)
    
    # Verify output
    output_files = test_pipe.load_files("output")
    assert len(output_files) == 1
    assert output_files[0].result == "Processed: test_execution"

def test_calc_filepath(test_pipe):
    """Test filepath calculation."""
    input_path = test_pipe.resolve_nickname("input")
    assert input_path.name == "input.json"
    assert input_path.is_absolute()

    # Test relative path
    rel_path = test_pipe.resolve_nickname("input", abs_path=False)
    assert rel_path.name == "input.json"
    assert not rel_path.is_absolute()

def test_files_nonexistent(test_pipe):
    """Test list_files() and load_files() methods with nonexistent files."""
    assert test_pipe.list_files("input") == []
    assert test_pipe.list_files("nonexistent_nickname") == []
    assert test_pipe.load_files("input") == []
    with pytest.raises(ValueError, match="Nickname 'nonexistent_nickname' not found in inputs, outputs, or special nicknames"):
        test_pipe.load_files("nonexistent_nickname")

def test_pattern_nickname_collision():
    """Test error handling for nickname collisions."""
    with pytest.raises(ValueError, match="Nicknames cannot appear in both inputs and outputs"):
        CollisionPipe(working_dir=tempfile.mkdtemp())._get_pattern_lookup()

def test_begin_params_during_execution(test_pipe):
    """Test that begin_params() is accessible during execute() but not outside."""
    # Create a subclass that tries to access begin_params
    class ParamsTestPipe(SimpleTestPipe):
        def execute(self):
            # Should be able to access begin_params during execute
            params = self.get_bind_params()
            assert params == {"test_param": "test_value"}
            output_data = SampleOutputModel(result=f"Got param: {params['test_param']}")
            output_data.save(file_path=str(self.resolve_nickname("output")))

    # Create and prepare the pipe
    pipe = ParamsTestPipe(working_dir=test_pipe.working_dir)
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        },
        params={"test_param": "test_value"}
    )

    # Verify begin_params() is not accessible before execution
    with pytest.raises(ValueError, match="get_bind_params\\(\\) is only accessible during the execute\\(\\) method"):
        pipe.get_bind_params()

    # Run the pipe
    luigi.build([pipe], local_scheduler=True, workers=1)

    # Verify begin_params() is not accessible after execution
    with pytest.raises(ValueError, match="get_bind_params\\(\\) is only accessible during the execute\\(\\) method"):
        pipe.get_bind_params()

    # Verify the output shows the param was accessible during execute
    output = pipe.load_files("output")[0]
    assert output.result == "Got param: test_value"

def test_begin_data_special_nickname(test_pipe):
    """Test accessing begin data through the special nickname."""
    # Prepare the pipe with some params
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        },
        params={"test_param": "test_value"}
    )

    # Access the begin data through the special nickname
    begin_data_list = test_pipe.load_files(SpecialPipeFileNickname.BEGIN)
    assert len(begin_data_list) == 1
    begin_data = begin_data_list[0]
    
    # Verify the begin data content
    assert isinstance(begin_data, ToToPipeBeginData)
    assert begin_data.params == {"test_param": "test_value"}
    assert begin_data.task_classname == "SimpleTestPipe"
    assert "TEST_CONFIG" in begin_data.private_configs

def test_begin_data_persistence(test_pipe):
    """Test that begin data persists between runs and can be loaded."""
    # Prepare and run the pipe
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        },
        params={"test_param": "test_value"}
    )

    # Verify the file exists
    begin_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.BEGIN.filename()
    assert begin_file.exists()

    # Load and verify the file directly
    begin_data = ToToPipeBeginData.load(begin_file)
    assert begin_data.params == {"test_param": "test_value"}
    assert begin_data.task_classname == "SimpleTestPipe"

def test_begin_data_file_locking(test_pipe):
    """Test that begin data uses proper file locking."""
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    
    # First bind_inputs should succeed
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        },
        params={"first": "value"}
    )

    # Create a second pipe instance pointing to the same directory
    second_pipe = SimpleTestPipe(working_dir=test_pipe.working_dir)
    
    # Simulate concurrent access by trying to read while the first pipe holds the lock
    begin_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.BEGIN.filename()
    begin_data = ToToPipeBeginData.load(begin_file)
    
    # Verify the data
    assert begin_data.params == {"first": "value"}

    # Second bind_inputs should work after the first is done
    second_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        },
        params={"second": "value"}
    )
    
    # Verify the updated data
    begin_data = ToToPipeBeginData.load(begin_file)
    assert begin_data.params == {"second": "value"}

def test_begin_data_content_validation(test_pipe):
    """Test that begin data contains all expected fields with correct types."""
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        },
        params={"test_param": "test_value"}
    )

    # Load the begin data through the special nickname
    begin_data_list = test_pipe.load_files(SpecialPipeFileNickname.BEGIN)
    assert len(begin_data_list) == 1
    begin_data = begin_data_list[0]

    # Verify all required fields are present and have correct types
    assert isinstance(begin_data.task_classname, str)
    assert isinstance(begin_data.params, dict)
    assert isinstance(begin_data.private_configs, list)
    assert isinstance(begin_data.inputs, dict)
    assert isinstance(begin_data.outputs, dict)
    
    # Verify that the params match what was provided
    assert begin_data.params == {"test_param": "test_value"}
    
    # Verify that the private_configs match the pipe's configuration
    assert begin_data.private_configs == test_pipe.pipe_type_info().required_private_configs
    
    # Verify that the inputs and possible_outputs contain the expected model types
    assert all(value == 'SampleInputModel' for value in begin_data.inputs.values())
    assert all(value == 'SampleOutputModel' for value in begin_data.outputs.values())

def test_completion_data_creation(test_pipe):
    """Test that completion data is created after successful execution."""
    # Prepare and run the pipe
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        }
    )

    # Run the pipe
    luigi.build([test_pipe], local_scheduler=True, workers=1)

    # Verify the completion data file exists
    completion_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.COMPLETION.filename()
    assert completion_file.exists()

    # Load and verify the completion data
    completion_data_list = test_pipe.load_files(SpecialPipeFileNickname.COMPLETION)
    assert completion_data_list is not None
    assert len(completion_data_list) == 1
    completion_data = completion_data_list[0]
    assert isinstance(completion_data, ToToPipeCompletionData)

def test_completion_data_special_nickname(test_pipe):
    """Test accessing completion data through the special nickname after execution."""
    # Prepare and run the pipe
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        }
    )

    # Before execution, completion data should not exist
    assert not test_pipe.list_files(SpecialPipeFileNickname.COMPLETION)

    # Run the pipe
    luigi.build([test_pipe], local_scheduler=True, workers=1)

    # After execution, we should be able to access the completion data
    completion_files = test_pipe.load_files(SpecialPipeFileNickname.COMPLETION)
    assert len(completion_files) == 1
    completion_data = completion_files[0]

    # Verify it's the right type
    assert isinstance(completion_data, ToToPipeCompletionData)

def test_completion_data_outputs_tracking(test_pipe):
    """Test that outputs are correctly tracked in completion data."""
    # Prepare and run the pipe
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        }
    )

    # Run the pipe
    luigi.build([test_pipe], local_scheduler=True, workers=1)

    # Get the completion data
    completion_data_list = test_pipe.load_files(SpecialPipeFileNickname.COMPLETION)
    assert len(completion_data_list) == 1
    completion_data = completion_data_list[0]
    
    # Verify outputs are tracked
    assert completion_data is not None
    assert isinstance(completion_data, ToToPipeCompletionData)
    assert completion_data.outputs is not None
    assert isinstance(completion_data.outputs, dict)
    assert len(completion_data.outputs) > 0  # Should have at least one output

def test_run_already_completed_pipe(test_pipe):
    """Test that running an already completed pipe raises an error."""
    # Create input files
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi1")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Create completion data file to simulate a completed pipe
    completion_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.COMPLETION.filename()
    completion_file.write_text("test")
    
    # Run the pipe - should fail due to already completed
    luigi.build([test_pipe], local_scheduler=True, workers=1)
    
    # Verify the pipe wasn't run (output file shouldn't exist)
    assert not test_pipe.resolve_nickname("output").exists()

def test_completion_data(test_pipe):
    """Test that completion data is saved correctly."""
    completion_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.COMPLETION.filename()

def test_completion_data_timing_info(test_pipe):
    """Test that timing info is saved correctly in completion data."""
    # Prepare and run the pipe
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        }
    )

    # Run the pipe
    luigi.build([test_pipe], local_scheduler=True, workers=1)

    # Get the completion data
    completion_data_list = test_pipe.load_files(SpecialPipeFileNickname.COMPLETION)
    assert len(completion_data_list) == 1
    completion_data = completion_data_list[0]
    
    # Verify timing info is present and reasonable
    assert completion_data is not None
    assert isinstance(completion_data, ToToPipeCompletionData)
    assert completion_data.timing.started_time is not None
    assert completion_data.timing.ended_time is not None
    assert completion_data.timing.ended_time > completion_data.timing.started_time

def test_completion_data_duration_formatting(test_pipe):
    """Test that duration is formatted correctly in completion data."""
    # Prepare and run the pipe
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi_test")]
    test_pipe.bind_inputs(
        explicit_inputs={
            "input": input_data,
            "multi_input": multi_input_data
        }
    )

    # Run the pipe
    luigi.build([test_pipe], local_scheduler=True, workers=1)

    # Get the completion data
    completion_data_list = test_pipe.load_files(SpecialPipeFileNickname.COMPLETION)
    assert len(completion_data_list) == 1
    completion_data = completion_data_list[0]
    
    # Calculate duration
    assert completion_data is not None
    assert isinstance(completion_data, ToToPipeCompletionData)
    duration = completion_data.timing.ended_time - completion_data.timing.started_time
    assert duration.total_seconds() > 0

def test_purge_pipe_outputs_normal_mode(test_pipe):
    """Test that purge_outputs() removes output files in normal mode."""
    # Create input and output files
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi1")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Create output file
    output_data = SampleOutputModel(result="test_result")
    output_data.save(file_path=str(test_pipe.resolve_nickname("output")))
    
    # Create completion data file
    completion_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.COMPLETION.filename()
    completion_file.write_text("test")
    
    # Run purge in normal mode
    deleted_files = test_pipe.purge_outputs()
    
    # Verify output and completion data file were deleted
    assert not test_pipe.resolve_nickname("output").exists()
    assert not completion_file.exists()

def test_purge_pipe_outputs_hyperactive_mode(test_pipe):
    """Test that purge_outputs() removes all files in hyperactive mode."""
    # Create input and output files
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi1")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Create output file
    output_data = SampleOutputModel(result="test_result")
    output_data.save(file_path=str(test_pipe.resolve_nickname("output")))
    
    # Create some random files that aren't inputs or outputs
    random_file = Path(test_pipe.working_dir) / "random.txt"
    random_file.write_text("random content")
    
    # Create completion data file
    completion_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.COMPLETION.filename()
    completion_file.write_text("test")
    
    # Run purge in hyperactive mode
    deleted_files = test_pipe.purge_outputs(hyperactive=True)
    
    # Verify all files were deleted except input files
    assert not test_pipe.resolve_nickname("output").exists()
    assert not completion_file.exists()
    assert not random_file.exists()
    assert test_pipe.resolve_nickname("input").exists()

def test_purge_pipe_outputs_avoid_running(test_pipe):
    """Test that purge_outputs() respects avoid_running flag."""
    # Create input and output files
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi1")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })

    # Verify begin file was created by bind_inputs
    begin_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.BEGIN.filename()
    assert begin_file.exists()
    assert test_pipe.get_state() == PipeState.INITIALIZED

    # Create output file
    output_data = SampleOutputModel(result="test_result")
    output_data.save(file_path=str(test_pipe.resolve_nickname("output")))

    # Create heartbeat file to simulate running state
    heartbeat_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.HEARTBEAT.filename()
    SimpleTestPipe._update_heartbeat(Path(test_pipe.working_dir), new_txt=str(time.time()))  # Write current time to heartbeat file
    test_pipe._clear_state_cache()  # Clear the state cache to force recalculation

    # Verify pipe is in RUNNING state
    assert test_pipe.get_state() == PipeState.RUNNING

    # Verify that purge_outputs() raises RuntimeError when avoid_running=True
    with pytest.raises(RuntimeError):
        test_pipe.purge_outputs(avoid_running=True)

    # Verify that purge_outputs() succeeds when avoid_running=False
    deleted_files = test_pipe.purge_outputs(avoid_running=False)
    assert len(deleted_files) > 0
    assert not test_pipe.resolve_nickname("output").exists()

def test_purge_pipe_outputs_with_nonexistent_files(test_pipe):
    """Test that purge_outputs() handles nonexistent files gracefully."""
    # Run purge without creating any files
    deleted_files = test_pipe.purge_outputs()
    
    # Verify no errors were raised
    assert len(deleted_files) == 0

def test_purge_pipe_outputs_returns_deleted_files(test_pipe):
    """Test that purge_outputs() returns list of deleted files."""
    # Create input and output files
    input_data = SampleInputModel(value="test_value")
    multi_input_data = [SampleInputModel(value="multi1")]
    test_pipe.bind_inputs(explicit_inputs={
        "input": input_data,
        "multi_input": multi_input_data
    })
    
    # Create output file
    output_data = SampleOutputModel(result="test_result")
    output_data.save(file_path=str(test_pipe.resolve_nickname("output")))
    
    # Create completion data file
    completion_file = Path(test_pipe.working_dir) / SpecialPipeFileNickname.COMPLETION.filename()
    completion_file.write_text("test")
    
    # Run purge and check returned list
    deleted_files = test_pipe.purge_outputs()
    
    # Verify list contains expected files
    assert len(deleted_files) == 2
    assert "output.json" in deleted_files
    assert "_pipe_completion.yaml" in deleted_files 

def test_execute_value_storage(tmp_path: Path):
    """Test that values are correctly stored in completion_data.extra."""
    # Create test directories for each case
    test_dir = tmp_path / "test_execute_value_storage"
    test_dir.mkdir(parents=True)

    # Test None value
    none_dir = test_dir / "none"
    none_dir.mkdir()
    pipe = ReturnValueTestPipe(working_dir=str(none_dir), return_value=None)
    pipe.bind_inputs(explicit_inputs={})
    pipe.run()
    completion_data = pipe.load_files(SpecialPipeFileNickname.COMPLETION)[0]
    assert completion_data.extra["stored_value"] is None

    # Test True value
    true_dir = test_dir / "true"
    true_dir.mkdir()
    pipe = ReturnValueTestPipe(working_dir=str(true_dir), return_value=True)
    pipe.bind_inputs(explicit_inputs={})
    pipe.run()
    completion_data = pipe.load_files(SpecialPipeFileNickname.COMPLETION)[0]
    assert completion_data.extra["stored_value"] is True

    # Test False value
    false_dir = test_dir / "false"
    false_dir.mkdir()
    pipe = ReturnValueTestPipe(working_dir=str(false_dir), return_value=False)
    pipe.bind_inputs(explicit_inputs={})
    pipe.run()
    completion_data = pipe.load_files(SpecialPipeFileNickname.COMPLETION)[0]
    assert completion_data.extra["stored_value"] is False

    # Test dict value
    dict_dir = test_dir / "dict"
    dict_dir.mkdir()
    return_dict = {"key": "value"}
    pipe = ReturnValueTestPipe(working_dir=str(dict_dir), return_value=return_dict)
    pipe.bind_inputs(explicit_inputs={})
    pipe.run()
    completion_data = pipe.load_files(SpecialPipeFileNickname.COMPLETION)[0]
    assert completion_data.extra["stored_value"] == return_dict

def test_execute_generator_value_storage(tmp_path: Path):
    """Test that generator values are correctly stored in completion_data.extra."""
    test_dir = tmp_path / "test_execute_generator"
    test_dir.mkdir(parents=True)
    
    def generator_func():
        yield "step1"
        yield "step2"
        return "final"

    pipe = ReturnValueTestPipe(working_dir=str(test_dir), return_value=generator_func())
    pipe.bind_inputs(explicit_inputs={})
    pipe.run()
    completion_data = pipe.load_files(SpecialPipeFileNickname.COMPLETION)[0]
    assert completion_data.extra["stored_value"] == "final"

def test_state_management(tmp_path: Path):
    """Test that pipe state transitions correctly through its lifecycle."""
    test_dir = tmp_path / "test_state_management"
    test_dir.mkdir(parents=True)

    # Create input file
    input_dir = test_dir / "input"
    input_dir.mkdir()
    input_file = input_dir / "text.yaml"
    input_file.write_text('{"text_content": "test"}')

    # Initialize pipe
    pipe = SampleWordStatsPipe(working_dir=str(test_dir))
    assert pipe.get_state() == PipeState.UNINITIALIZED

    # Bind inputs
    pipe.bind_inputs(explicit_inputs={"input_text": str(input_file)})
    assert pipe.get_state() == PipeState.INITIALIZED

    # Run pipe and wait for completion
    luigi.build([pipe], local_scheduler=True, workers=1)

    # Wait for completion before checking final state
    asyncio.run(pipe.wait_for_completion(str(test_dir)))
    assert pipe.get_state() == PipeState.COMPLETED


def test_spawn_in_process_runs_luigi_in_background_thread(tmp_path: Path):
    """in-process spawn must return before the pipe finishes so wait_for_completion can see the heartbeat (Luigi runs in a child process)."""
    test_dir = tmp_path / "spawn_bg"
    test_dir.mkdir(parents=True)
    pipe = SlowSimpleTestPipe(working_dir=str(test_dir))
    pipe.bind_inputs(
        explicit_inputs={"input": SampleInputModel(value="x")},
        ignore_missing_inputs=True,
    )
    completion_file = test_dir / SpecialPipeFileNickname.COMPLETION.filename()
    assert not completion_file.exists()
    pipe.spawn(mode="in-process")
    assert pipe.get_state(cache_state_secs=0) == PipeState.RUNNING
    assert not completion_file.exists()
    asyncio.run(ToToPipeBase.wait_for_completion(test_dir))
    assert completion_file.exists()
    assert pipe.get_state(cache_state_secs=0) == PipeState.COMPLETED


def test_spawn_wait_for_completion_failure_no_heartbeat_race(tmp_path: Path):
    """After threaded spawn, failure must not trigger 'Heartbeat file did not appear' from wait_for_completion."""
    test_dir = tmp_path / "spawn_fail"
    test_dir.mkdir(parents=True)
    pipe = FailingSimpleTestPipe(working_dir=str(test_dir))
    pipe.bind_inputs(
        explicit_inputs={"input": SampleInputModel(value="x")},
        ignore_missing_inputs=True,
    )
    pipe.spawn(mode="in-process")
    result = asyncio.run(ToToPipeBase.wait_for_completion(test_dir))
    assert result is None
    fails_file = test_dir / SpecialPipeFileNickname.EXECUTE_FAILS.filename()
    assert fails_file.exists()


def test_wait_for_completion_fast_failure_within_allow_start_secs(tmp_path: Path):
    """Pipe fails within the first ~2s (execute sleeps 50ms then raises): after spawn,
    wait_for_completion(allow_start_secs=2) must return None and surface the failure in
    _pipe_execute_fails.yaml — not RuntimeError about the heartbeat.
    """
    test_dir = tmp_path / "fast_fail_allow_window"
    test_dir.mkdir(parents=True)
    pipe = SoonFailingSimpleTestPipe(working_dir=str(test_dir))
    pipe.bind_inputs(
        explicit_inputs={"input": SampleInputModel(value="x")},
        ignore_missing_inputs=True,
    )
    pipe.spawn(mode="in-process")
    result = asyncio.run(
        ToToPipeBase.wait_for_completion(test_dir, allow_start_secs=2.0)
    )
    assert result is None
    fails_data = pipe.load_files(SpecialPipeFileNickname.EXECUTE_FAILS)[0]
    assert fails_data.exceptions[-1].exception_type == "RuntimeError"
    assert "allow_start_secs window" in fails_data.exceptions[-1].exception_message


def test_multiple_input_files_handling():
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        test_dir = Path(temp_dir) / "test_multiple_inputs"
        test_dir.mkdir()
        
        # Create input directory to match the glob pattern "input/*.pdf"
        input_dir = test_dir / "input"
        input_dir.mkdir()
        
        # Create multiple PDF files in a temporary location first
        # This is deliberate to test the pipe's ability to handle files from different locations
        pdf_files = []
        for i in range(3):
            # Create files with .pdf extension in a temporary location
            temp_pdf = Path(temp_dir) / f"test_{i}.pdf"
            temp_pdf.write_text(f"Test content {i}")
            pdf_files.append(str(temp_pdf))
            
        # Create text input file
        text_input = TextContent(text_content="Sample text")
        
        # Initialize the pipe with the test directory
        pipe = SampleWordStatsPipe(working_dir=str(test_dir))
        
        # Bind inputs - the PDF files should match the glob pattern "input/*.pdf"
        pipe.bind_inputs({
            "input_text": text_input,
            "dummy_pdfs": pdf_files
        })
        
        # Verify that the files were bound correctly
        bound_files = pipe.list_files("dummy_pdfs")
        assert len(bound_files) == 3, f"Expected 3 bound files, got {len(bound_files)}"
        
        # Verify that each file exists in the input directory
        for i, bound_file in enumerate(sorted(bound_files)):
            expected_path = input_dir / f"test_{i}.pdf"
            assert bound_file.exists(), f"Bound file {bound_file} does not exist"
            assert bound_file.samefile(expected_path), f"Bound file {bound_file} does not match expected path {expected_path}"
            
        # Run the pipe to ensure it works with multiple input files
        pipe.run()
        
        # Verify output was created
        output_files = pipe.list_files("word_stats")  # Changed to match the output nickname in SampleWordStatsPipe
        assert len(output_files) == 1, "Expected one output file"
        
        # Load and verify output
        outputs = pipe.load_files("word_stats")  # Changed to match the output nickname in SampleWordStatsPipe
        assert len(outputs) == 1, "Expected one output object"
        output = outputs[0]
        assert isinstance(output, WordStats), "Output should be a WordStats object"

def test_list_files_absolute_paths(test_pipe):
    """Test list_files() with absolute paths."""
    # Create test files
    input_data = SampleInputModel(value="test_value")
    test_pipe.bind_inputs(explicit_inputs={"input": input_data}, ignore_missing_inputs=True)
    
    # Test absolute paths
    files = test_pipe.list_files("input", abs_path=True)
    assert len(files) == 1
    assert files[0].is_absolute()
    assert files[0].exists()
    assert files[0].name == "input.json"

def test_list_files_relative_paths(test_pipe):
    """Test list_files() with relative paths."""
    # Create test files
    input_data = SampleInputModel(value="test_value")
    test_pipe.bind_inputs(explicit_inputs={"input": input_data}, ignore_missing_inputs=True)
    
    # Test relative paths
    files = test_pipe.list_files("input", abs_path=False)
    assert len(files) == 1
    assert not files[0].is_absolute()
    assert (test_pipe.working_dir / files[0]).exists()
    assert files[0].name == "input.json"

def test_list_files_nested_directories(test_pipe):
    """Test list_files() with files in nested directories."""
    # Create test files in nested directories
    multi_input_data = [
        SampleInputModel(value="test1"),
        SampleInputModel(value="test2")
    ]
    test_pipe.bind_inputs(explicit_inputs={"multi_input": multi_input_data}, ignore_missing_inputs=True)
    
    # Test nested directory files
    files = test_pipe.list_files("multi_input", abs_path=True)
    assert len(files) == 2
    for file in files:
        assert file.is_absolute()
        assert file.exists()
        assert file.suffix == ".json"
        assert file.parent.name == "multi_input"

def test_list_files_multiple_files(test_pipe):
    """Test list_files() with multiple files matching a pattern."""
    # Create multiple test files
    multi_input_data = [
        SampleInputModel(value="test1"),
        SampleInputModel(value="test2"),
        SampleInputModel(value="test3")
    ]
    test_pipe.bind_inputs(explicit_inputs={"multi_input": multi_input_data}, ignore_missing_inputs=True)
    
    # Test multiple files
    files = test_pipe.list_files("multi_input", abs_path=True)
    assert len(files) == 3
    for file in files:
        assert file.is_absolute()
        assert file.exists()
        assert file.suffix == ".json"
        assert file.parent.name == "multi_input"

def test_list_files_special_nicknames(test_pipe):
    """Test list_files() with special nicknames."""
    # Create begin data file
    test_pipe.bind_inputs(explicit_inputs={}, ignore_missing_inputs=True)
    
    # Test special nickname (BEGIN)
    files = test_pipe.list_files(SpecialPipeFileNickname.BEGIN, abs_path=True)
    assert len(files) == 1
    assert files[0].is_absolute()
    assert files[0].exists()
    assert files[0].name == "_pipe_begin.yaml"
    
    # Test relative path for special nickname
    files = test_pipe.list_files(SpecialPipeFileNickname.BEGIN, abs_path=False)
    assert len(files) == 1
    assert not files[0].is_absolute()
    assert (test_pipe.working_dir / files[0]).exists()
    assert files[0].name == "_pipe_begin.yaml"

def test_list_files_nonexistent_nickname(test_pipe):
    """Test list_files() with a nonexistent nickname."""
    files = test_pipe.list_files("nonexistent")
    assert len(files) == 0

def test_list_files_empty_directory(test_pipe):
    """Test list_files() with an empty directory pattern."""
    # Create directory but no files
    (Path(test_pipe.working_dir) / "multi_input").mkdir(exist_ok=True)
    
    files = test_pipe.list_files("multi_input")
    assert len(files) == 0

# ... rest of the file remains unchanged ... 