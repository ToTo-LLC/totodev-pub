# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the PipeState class and its state inference logic."""

import os
import tempfile
from pathlib import Path
import time
import pytest
import yaml
from totodev_pub.pytest_tools import very_lazy_test

from totodev_pub.pipes.pipe_state import PipeState
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname
from totodev_pub.pipes.toto_pipe_begin_data import ToToPipeBeginData

def create_special_file(working_dir: Path, nickname: SpecialPipeFileNickname, content: str = "") -> Path:
    """Helper function to create special files for testing."""
    file_path = working_dir / nickname.filename()
    file_path.write_text(content)
    return file_path

def test_uninitialized_state():
    """Test UNINITIALIZED state when no special files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.UNINITIALIZED

def test_invalid_state():
    """Test INVALID state when special files exist without begin file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create heartbeat without begin file
        create_special_file(working_dir, SpecialPipeFileNickname.HEARTBEAT, str(time.time()))
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.INVALID

def test_initialized_state():
    """Test INITIALIZED state when only begin file exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={}
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.INITIALIZED

def test_running_state():
    """Test RUNNING state with fresh heartbeat."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file with 60s timeout
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={},
            heartbeat_timeout_secs=60
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        # Create fresh heartbeat
        create_special_file(working_dir, SpecialPipeFileNickname.HEARTBEAT, str(time.time()))
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.RUNNING

@pytest.mark.slow
@very_lazy_test(['totodev_pub.pipes.pipe_state', 'totodev_pub.pipes.special_pipe_file_nickname', 'totodev_pub.pipes.toto_pipe_begin_data'], reverify_days=14)
def test_stalled_state():
    """Test STALLED state with stale heartbeat."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file with 1s timeout
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={},
            heartbeat_timeout_secs=1
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        # Create stale heartbeat (2 seconds old)
        heartbeat_file = create_special_file(working_dir, SpecialPipeFileNickname.HEARTBEAT, str(time.time()))
        # Wait for heartbeat to become stale
        time.sleep(2)
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.STALLED

def test_completed_state():
    """Test COMPLETED state when both begin and completion files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={}
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        # Create completion file
        create_special_file(working_dir, SpecialPipeFileNickname.COMPLETION, "{}")
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.COMPLETED

def test_failed_state():
    """Test FAILURES state when execute fails file exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={}
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        # Create execute fails file
        create_special_file(working_dir, SpecialPipeFileNickname.EXECUTE_FAILS, "{}")
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.FAILURES

@pytest.mark.slow
@very_lazy_test(['totodev_pub.pipes.pipe_state', 'totodev_pub.pipes.special_pipe_file_nickname', 'totodev_pub.pipes.toto_pipe_begin_data'], reverify_days=14)
def test_custom_heartbeat_timeout():
    """Test using custom heartbeat timeout parameter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={}
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        # Create heartbeat file and wait for it to age
        heartbeat_file = create_special_file(working_dir, SpecialPipeFileNickname.HEARTBEAT, str(time.time()))
        time.sleep(1)  # Wait for 1 second
        
        # Test with custom timeout of 0.5 seconds (should be stalled)
        state = PipeState.infer_state(working_dir, heartbeat_timeout_secs=0.5)
        assert state == PipeState.STALLED
        
        # Test with custom timeout of 2 seconds (should be running)
        state = PipeState.infer_state(working_dir, heartbeat_timeout_secs=2)
        assert state == PipeState.RUNNING

def test_state_precedence():
    """Test state precedence when multiple special files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir)
        # Create begin file
        begin_data = ToToPipeBeginData(
            task_classname="TestPipe",
            params={},
            private_configs=[],
            inputs={},
            outputs={}
        )
        create_special_file(working_dir, SpecialPipeFileNickname.BEGIN, yaml.dump(begin_data.model_dump()))
        
        # Create completion file (should take precedence)
        create_special_file(working_dir, SpecialPipeFileNickname.COMPLETION, "{}")
        
        # Create fresh heartbeat (should be ignored due to completion)
        create_special_file(working_dir, SpecialPipeFileNickname.HEARTBEAT, str(time.time()))
        
        # Create execute fails file (should be ignored due to completion)
        create_special_file(working_dir, SpecialPipeFileNickname.EXECUTE_FAILS, "{}")
        
        state = PipeState.infer_state(working_dir)
        assert state == PipeState.COMPLETED 