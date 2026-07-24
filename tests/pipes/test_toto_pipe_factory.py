# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the ToToPipeFactory class.

This test suite verifies the core functionality of ToToPipeFactory using SampleWordStatsPipe
as a test subject since it's a simple, well-documented example pipe.
"""

import pytest
from pathlib import Path
import shutil
from datetime import datetime, timedelta
import time
from typing import Generator

pytest.importorskip("luigi")  # 'pipes' extra; skip when luigi is unavailable

import luigi

from totodev_pub.pipes.toto_pipe_factory import ToToPipeFactory
from totodev_pub.pipes.toto_example_pipe import SampleWordStatsPipe, TextContent, WordStats
from totodev_pub.pipes.pipe_state import PipeState
from totodev_pub.pipes.pipe_stat import PipeStat
from totodev_pub.pytest_tools import very_lazy_test

@pytest.fixture
def temp_factory_dir(tmp_path) -> Path:
    """Create a temporary directory for factory tests."""
    return tmp_path / "test_factory"

@pytest.fixture
def factory(temp_factory_dir) -> ToToPipeFactory:
    """Create a ToToPipeFactory instance for testing."""
    return ToToPipeFactory(temp_factory_dir, {})

@pytest.fixture
def sample_text_content() -> TextContent:
    """Create a sample TextContent for testing."""
    return TextContent(text_content="The quick brown fox jumps over the lazy dog")

@pytest.mark.asyncio
async def test_make_basic(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test basic pipe creation."""
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": sample_text_content},
        uniq_src="test_pipe"
    )
    assert pipe.get_state() == PipeState.INITIALIZED
    assert Path(pipe.working_dir).exists()

@pytest.mark.asyncio
async def test_make_with_uniq_src(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test pipe creation with unique source."""
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": sample_text_content},
        uniq_src="test_pipe_unique"
    )
    assert pipe.get_state() == PipeState.INITIALIZED
    assert "test_pipe_unique" in str(pipe.working_dir)

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_pipe_factory", "totodev_pub.pipes.toto_pipe_base"])
@pytest.mark.asyncio
async def test_make_with_params(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test pipe creation with parameters."""
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": sample_text_content},
        params={"min_word_length": 5},
        uniq_src="test_pipe"
    )
    # Main-thread luigi.build (threaded spawn() is incompatible with Luigi Worker + signals)
    luigi.build([pipe], local_scheduler=True)
    assert pipe.get_state() == PipeState.COMPLETED

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_pipe_factory"])
@pytest.mark.asyncio
async def test_make_with_file_inputs(factory: ToToPipeFactory, tmp_path: Path):
    """Test pipe creation with file inputs."""
    # Create a test input file
    input_file = tmp_path / "text.yaml"
    text_content = TextContent(text_content="test content")
    text_content.save(str(input_file))
    assert input_file.exists(), "Input file should exist"

    # Create input_nicknames mapping
    input_nicknames = {"input_text": "text.yaml"}

    # Create pipe with file input
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": input_file},
        uniq_src="test_pipe"
    )

    # Verify the original input file still exists
    assert input_file.exists(), "Original input file should still exist"

    # Verify the input file was copied correctly
    input_dir = Path(pipe.working_dir) / "input"
    target_file = input_dir / "text.yaml"
    assert input_dir.exists(), "Input directory should exist"
    assert target_file.exists(), "Target file should exist"

    # Verify the content was copied correctly
    copied_content = TextContent.load(str(target_file))
    assert copied_content.text_content == "test content", "File content should match"

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_pipe_factory", "totodev_pub.pipes.toto_pipe_base"])
@pytest.mark.asyncio
async def test_list_pipe_stats(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test listing pipe statistics."""
    # Create a few pipes
    for i in range(3):
        pipe = factory.make(
            pipe_class_name="SampleWordStatsPipe",
            explicit_inputs={"input_text": sample_text_content},
            uniq_src=f"test_pipe_{i}"
        )
        luigi.build([pipe], local_scheduler=True)

    # Get stats
    stats = await factory.list_pipe_stats()
    assert len(stats) == 3
    for stat in stats:
        assert isinstance(stat, PipeStat)
        assert stat.pipe_class_name == "SampleWordStatsPipe"
        assert stat.state == PipeState.COMPLETED

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_pipe_factory", "totodev_pub.pipes.toto_pipe_base"])
@pytest.mark.asyncio
async def test_pipe_stats_age_filter(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test filtering pipe stats by age."""
    # Create a pipe
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": sample_text_content},
        uniq_src="test_pipe"
    )
    luigi.build([pipe], local_scheduler=True)

    # Get stats with age filter
    stats = await factory.list_pipe_stats(age_days=1)
    assert len(stats) > 0
    for stat in stats:
        assert isinstance(stat, PipeStat)
        assert stat.pipe_class_name == "SampleWordStatsPipe"
        assert stat.state == PipeState.COMPLETED

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_pipe_factory", "totodev_pub.pipes.toto_pipe_base"])
@pytest.mark.asyncio
async def test_recently_completed(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test getting recently completed pipes."""
    # Create and complete a pipe
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": sample_text_content},
        uniq_src="test_pipe"
    )
    luigi.build([pipe], local_scheduler=True)

    # Get recently completed pipes
    one_hour_ago = datetime.now() - timedelta(hours=1)
    completed = list(factory.recently_completed(one_hour_ago))
    assert len(completed) > 0
    for stat in completed:
        assert stat.state == PipeState.COMPLETED
        assert stat.last_modified > one_hour_ago

def test_make_invalid_pipe_class(factory: ToToPipeFactory):
    """Test that making a pipe with an invalid class name raises an error."""
    with pytest.raises(ValueError, match="not found in ToToPipeBase.registered_pipe_classes"):
        factory.make(
            pipe_class_name="NonexistentPipe",
            explicit_inputs={},
            uniq_src="test_pipe"
        )

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.toto_pipe_factory"])
def test_make_with_private_configs(factory: ToToPipeFactory, sample_text_content: TextContent):
    """Test making a pipe with private configs."""
    # The factory already has config_obj from initialization
    pipe = factory.make(
        pipe_class_name="SampleWordStatsPipe",
        explicit_inputs={"input_text": sample_text_content},
        uniq_src="test_pipe"
    )
    
    # The pipe should have access to private configs through the factory's config_obj
    assert pipe is not None
    assert pipe.get_state() == PipeState.INITIALIZED 