# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
from pathlib import Path
from datetime import datetime, timedelta, date
from unittest.mock import Mock, patch

pytest.importorskip("luigi")  # 'pipes' extra; skip when luigi is unavailable

from totodev_pub.pipes.pipe_stat import PipeStat
from totodev_pub.pipes.pipe_state import PipeState
from totodev_pub.pipes.toto_example_pipe import SampleWordStatsPipe, TextContent
import tempfile
import shutil
import asyncio
import luigi
from totodev_pub.pipes.toto_pipe_base import ToToPipeBase
import yaml
import time
import threading
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname
from totodev_pub.pytest_tools import very_lazy_test


@pytest.fixture
def mock_date_tree_folder():
    """Fixture to mock DateTreeFolder."""
    with patch('totodev_pub.pipes.pipe_stat.DateTreeFolder') as mock:
        # Configure the mock to return a fixed date
        instance = Mock()
        instance.date = date(2025, 4, 8)
        mock.return_value = instance
        yield mock


def test_pipe_stat_creation(mock_date_tree_folder):
    """Test basic creation of PipeStat object."""
    test_path = Path("/test/path")
    pipe_stat = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=test_path,
        last_modified=datetime.now(),
        state=PipeState.INITIALIZED
    )
    assert pipe_stat.pipe_class_name == "TestPipe"
    assert pipe_stat.working_dir == test_path.resolve()
    assert pipe_stat.state == PipeState.INITIALIZED


def test_pipe_stat_comparison(mock_date_tree_folder):
    """Test comparison operators for PipeStat objects."""
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    
    # Same pipe class, different dates
    stat1 = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("/test/path1"),
        last_modified=now,
        state=PipeState.INITIALIZED
    )
    stat2 = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("/test/path2"),
        last_modified=yesterday,
        state=PipeState.INITIALIZED
    )
    
    # Different pipe classes
    stat3 = PipeStat(
        pipe_class_name="AnotherPipe",
        working_dir=Path("/test/path3"),
        last_modified=now,
        state=PipeState.INITIALIZED
    )
    
    # Test sorting by pipe_class
    assert stat3 < stat1  # AnotherPipe comes before TestPipe
    
    # Test sorting by last_modified (descending)
    assert stat1 < stat2  # newer comes before older


def test_pipe_stat_equality(mock_date_tree_folder):
    """Test equality comparison for PipeStat objects."""
    now = datetime.now()
    
    stat1 = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("/test/path"),
        last_modified=now,
        state=PipeState.INITIALIZED
    )
    
    stat2 = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("/test/path"),
        last_modified=now,
        state=PipeState.INITIALIZED
    )
    
    stat3 = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("/test/other"),
        last_modified=now,
        state=PipeState.INITIALIZED
    )
    
    assert stat1 == stat2
    assert stat1 != stat3


@very_lazy_test(['totodev_pub.pipes.pipe_stat'])
def test_ensure_absolute_path():
    """Test that working_dir is always converted to absolute path."""
    pipe_stat = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("relative/path"),
        last_modified=datetime.now(),
        state=PipeState.INITIALIZED
    )
    assert pipe_stat.working_dir.is_absolute()


@patch('totodev_pub.pipes.pipe_stat.ToToPipeBase')
@very_lazy_test(['totodev_pub.pipes.pipe_stat'])
def test_get_pipe_class(mock_toto_pipe_base):
    """Test getting the pipe class type."""
    # Setup mock
    mock_class = Mock()
    mock_toto_pipe_base.registered_pipe_classes.return_value = {"TestPipe": mock_class}
    
    pipe_stat = PipeStat(
        pipe_class_name="TestPipe",
        working_dir=Path("/test/path"),
        last_modified=datetime.now(),
        state=PipeState.INITIALIZED
    )
    
    assert pipe_stat.get_pipe_class() == mock_class
    
    # Test with unregistered class
    pipe_stat.pipe_class_name = "UnregisteredPipe"
    with pytest.raises(ValueError, match="Pipe class UnregisteredPipe not found in registered classes"):
        pipe_stat.get_pipe_class()


@pytest.fixture
def sample_working_dir():
    """Fixture to create a temporary working directory with a real SampleWordStatsPipe."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create and initialize a sample pipe
        working_dir = Path(temp_dir) / "SampleWordStatsPipe"  # Use the actual class name
        working_dir.mkdir(parents=True)
        pipe = SampleWordStatsPipe(str(working_dir))
        
        # Create some sample input and initialize the pipe
        text_content = TextContent(text_content="hello world")
        pipe.bind_inputs({"input_text": text_content}, ignore_missing_inputs=True)
        
        # Run the pipe to completion (main-thread luigi.build; threaded spawn breaks Luigi Worker signals)
        luigi.build([pipe], local_scheduler=True)
        
        yield working_dir
        # Cleanup happens automatically when the context manager exits


@pytest.mark.asyncio
@pytest.mark.slow
@very_lazy_test(['totodev_pub.pipes.pipe_stat', 'totodev_pub.pipes.pipe_state', 'totodev_pub.pipes.special_pipe_file_nickname'], reverify_days=21)
async def test_wait_for_completion(tmp_path):
    """Test waiting for completion."""
    # Create a temporary pipe directory
    pipe_dir = tmp_path / "SampleWordStatsPipe"
    pipe_dir.mkdir()

    # Create begin file
    begin_file = pipe_dir / SpecialPipeFileNickname.BEGIN.filename()
    with open(begin_file, "w") as f:
        yaml.dump({"task_classname": "SampleWordStatsPipe"}, f)

    # Create heartbeat file after a short delay
    def create_heartbeat():
        time.sleep(0.5)  # Reduced delay
        heartbeat_file = pipe_dir / SpecialPipeFileNickname.HEARTBEAT.filename()
        heartbeat_file.touch()

    # Start thread to create heartbeat
    thread = threading.Thread(target=create_heartbeat)
    thread.start()

    # Wait for completion
    pipe_stat = PipeStat.from_working_dir(pipe_dir)
    await pipe_stat.wait_for_completion(timeout_seconds=5)  # Increased timeout

    thread.join()
    assert pipe_stat.state == PipeState.INITIALIZED  # Initial state since we haven't started running


@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.pipe_stat", "totodev_pub.pipes.toto_pipe_base"])
def test_get_pipe(sample_working_dir):
    """Test getting a pipe instance."""
    pipe_stat = PipeStat.from_working_dir(sample_working_dir)
    
    # Test getting a valid pipe
    pipe = pipe_stat.get_pipe()
    assert isinstance(pipe, SampleWordStatsPipe)
    assert str(pipe.working_dir) == str(sample_working_dir)
    
    # Test with uninitialized state
    pipe_stat.state = PipeState.UNINITIALIZED
    with pytest.raises(RuntimeError, match="may not have been correctly initialized"):
        pipe_stat.get_pipe()


@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.pipe_stat", "totodev_pub.pipes.toto_pipe_base"])
def test_list_files(sample_working_dir):
    """Test listing files from a completed pipe."""
    pipe_stat = PipeStat.from_working_dir(sample_working_dir)
    assert pipe_stat.state.is_completed()  # Verify we're in completed state
    
    # Test listing files from completed pipe
    files = pipe_stat.list_files("word_stats")
    assert len(files) == 1
    assert str(files[0]).endswith("stats.yaml")
    
    # Test with non-completed pipe
    # Create a new pipe that's not completed
    new_working_dir = sample_working_dir.parent / "incomplete_test"
    new_working_dir.mkdir()
    pipe = SampleWordStatsPipe(str(new_working_dir))
    text_content = TextContent(text_content="hello world")
    pipe.bind_inputs({"input_text": text_content}, ignore_missing_inputs=True)
    
    incomplete_pipe_stat = PipeStat.from_working_dir(new_working_dir)
    with pytest.raises(RuntimeError, match="Cannot use this method to load files from pipe that is not completed"):
        incomplete_pipe_stat.list_files("word_stats")


@very_lazy_test(['totodev_pub.pipes.pipe_stat'])
def test_from_working_dir_validation():
    """Test validation in from_working_dir static method."""
    # Test with non-existent directory
    with pytest.raises(ValueError, match="Working directory must exist"):
        PipeStat.from_working_dir(Path("/nonexistent/path"))

    # Test with uninitialized directory
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        with pytest.raises(ValueError, match="Begin file must exist"):
            PipeStat.from_working_dir(temp_path) 