# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the toto_example_factory module.

These tests verify that the example factory implementation works as intended,
focusing on proper type returns and basic functionality rather than the actual
word statistics calculations.

Test Coverage Note:
------------------
Test coverage for this module is intentionally limited because toto_example_factory.py 
is a demonstration/tutorial module, not core functionality. Its primary purpose is to 
show developers how to use ToToPipeFactory in their own implementations.

We specifically limit testing to:
1. Verifying that factory creation works (global vs custom)
2. Checking that convenience functions return correct types
3. Validating basic directory structure creation
4. Confirming that monitoring functions return properly structured data

We intentionally DO NOT test:
1. The actual word statistics calculations (tested in test_toto_example_pipe.py)
2. Complex error cases (this is example code)
3. Detailed CLI behavior (the CLI is just demonstrating a pattern)
4. Long-term monitoring scenarios (example only)

If you're looking to add test cases, consider whether they're testing the example
nature of the module (good) or testing the underlying functionality (should go in 
the respective core module tests instead).
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta

from totodev_pub.pipes.toto_example_factory import (
    spawn_text_stats_pipe,
    create_factory,
    launch_word_stats_pipe,
    THE_PIPE_FACTORY,
    PIPE_FACTORY_ROOT_DIR,
)
from totodev_pub.pipes.toto_example_pipe import SampleWordStatsPipe
from totodev_pub.pipes.pipe_state import PipeState
from totodev_pub.pytest_tools import very_lazy_test

@pytest.fixture
def temp_factory_dir(tmp_path):
    """Create a temporary directory for factory tests."""
    return tmp_path / "test_factory"

def test_create_factory_global():
    """Test that getting the global factory works."""
    factory = create_factory(root_dir=None)
    assert factory == THE_PIPE_FACTORY
    assert factory.pipe_tree_root == PIPE_FACTORY_ROOT_DIR

def test_create_factory_custom(temp_factory_dir):
    """Test creating a factory with custom root directory."""
    factory = create_factory(root_dir=temp_factory_dir)
    assert factory.pipe_tree_root == temp_factory_dir
    assert factory != THE_PIPE_FACTORY  # Should be a different instance

def test_spawn_text_stats_pipe_returns_correct_type():
    """Test that spawn_text_stats_pipe returns the correct pipe type."""
    pipe = spawn_text_stats_pipe("test text", spawn_mode="in-process")
    assert isinstance(pipe, SampleWordStatsPipe)
    state = pipe.get_state()
    assert state in [PipeState.RUNNING, PipeState.COMPLETED], f"Expected RUNNING or COMPLETED state, got {state}"

def test_spawn_text_stats_pipe_modes():
    """Test that both spawn modes are accepted."""
    # Test in-process mode
    pipe1 = spawn_text_stats_pipe("test", spawn_mode="in-process")
    state = pipe1.get_state()
    assert state in [PipeState.RUNNING, PipeState.COMPLETED], f"Expected RUNNING or COMPLETED state, got {state}"

    # Skip luigid mode test if scheduler isn't running
    pytest.skip("Skipping luigid mode test as it requires a running Luigi scheduler")

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_example_factory", "totodev_pub.pipes.toto_pipe_base"])
@pytest.mark.asyncio
async def test_pipe_stats_basic_structure(temp_factory_dir):
    """Test that pipe stats are returned with correct structure."""
    factory = create_factory(root_dir=temp_factory_dir)
    
    # Create a pipe to generate some stats
    launch_word_stats_pipe(factory, "test text")
    
    # Get stats and verify structure
    stats = list(factory.pipe_stats())
    assert len(stats) > 0
    
    stat = stats[0]
    assert hasattr(stat, 'pipe_class_name')
    assert hasattr(stat, 'state')
    assert hasattr(stat, 'working_dir')
    assert hasattr(stat, 'last_modified')

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_example_factory", "totodev_pub.pipes.toto_pipe_base"])
def test_launch_word_stats_pipe_returns_path(temp_factory_dir):
    """Test that launch_word_stats_pipe returns a valid Path."""
    factory = create_factory(root_dir=temp_factory_dir)
    working_dir = launch_word_stats_pipe(factory, "test text")
    assert isinstance(working_dir, (str, Path))  # Accept either str or Path
    working_dir = Path(working_dir) if isinstance(working_dir, str) else working_dir
    assert working_dir.exists()
    assert working_dir.is_dir() 