# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the sample word statistics pipe implementation.

These tests focus on demonstrating how to test a ToToPipeBase subclass,
rather than exhaustively testing the word statistics functionality.
"""

import pytest
from pathlib import Path
import tempfile
import yaml
import asyncio

pytest.importorskip("luigi")  # 'pipes' extra; skip when luigi is unavailable

import luigi
from totodev_pub.pytest_tools import very_lazy_test

from totodev_pub.pipes.toto_example_pipe import (
    SampleWordStatsPipe,
    TextContent,
    WordStats
)
from totodev_pub.pipes.toto_pipe_base import PipeState, ToToPipeBase


@pytest.fixture
def luigi_sync_spawn(monkeypatch):
    """Luigi Worker registers OS signals; it cannot run in spawn()'s background thread."""

    def _spawn(self, mode="in-process"):
        luigi.build([self], local_scheduler=True)

    monkeypatch.setattr(ToToPipeBase, "spawn", _spawn)


@very_lazy_test(["totodev_pub.pipes.toto_example_pipe"],reverify_days=21)
def test_basic_pipe_functionality():
    """Test the basic pipe workflow: setup → execute → verify output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup directories and input
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        
        # Create input file with proper YAML format
        input_file = input_dir / "text.yaml"
        input_data = {"text_content": "the quick brown fox"}
        with open(input_file, 'w') as f:
            yaml.dump(input_data, f)
        
        # Create and run pipe
        pipe = SampleWordStatsPipe(working_dir=tmpdir)
        pipe.bind_inputs({"input_text": str(input_file)}, ignore_missing_inputs=True)
        luigi.build([pipe], local_scheduler=True)

        # Verify output exists and contains reasonable data
        output_file = output_dir / "stats.yaml"
        assert output_file.exists()
        
        stats = WordStats.load(str(output_file))  # No need for [0]
        assert stats.total_words == 4
        assert stats.min_word_length_used is None  # No min length specified

@very_lazy_test(["totodev_pub.pipes.toto_example_pipe"],reverify_days=21)
def test_parameter_handling():
    """Test parameter handling through bind_inputs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup directories and input
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        
        # Create input file with proper YAML format
        input_file = input_dir / "text.yaml"
        input_data = {"text_content": "a an the some longer words"}
        with open(input_file, 'w') as f:
            yaml.dump(input_data, f)
        
        # Create and run pipe with min_word_length parameter
        pipe = SampleWordStatsPipe(working_dir=tmpdir)
        pipe.bind_inputs(
            {"input_text": str(input_file)},
            params={"min_word_length": 4},
            ignore_missing_inputs=True
        )
        luigi.build([pipe], local_scheduler=True)

        # Verify parameter was applied
        output_file = output_dir / "stats.yaml"
        stats = WordStats.load(str(output_file))  # No need for [0]
        assert stats.min_word_length_used == 4
        assert stats.total_words == 3  # only "some", "longer", "words" are >= 4 chars

@very_lazy_test(["totodev_pub.pipes.toto_example_pipe"],reverify_days=21)
def test_state_management():
    """Test basic pipe state transitions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup minimal input
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        
        # Create input file with proper YAML format
        input_file = input_dir / "text.yaml"
        input_data = {"text_content": "test"}
        with open(input_file, 'w') as f:
            yaml.dump(input_data, f)
        
        # Create pipe and verify state transitions
        pipe = SampleWordStatsPipe(working_dir=tmpdir)
        assert pipe.get_state() == PipeState.UNINITIALIZED
        
        pipe.bind_inputs({"input_text": str(input_file)}, ignore_missing_inputs=True)
        assert pipe.get_state() == PipeState.INITIALIZED
        
        # Use luigi.build() to properly handle state transitions
        luigi.build([pipe], local_scheduler=True, workers=1)

        # Wait for completion before checking final state
        asyncio.run(pipe.wait_for_completion(tmpdir))
        assert pipe.get_state() == PipeState.COMPLETED 

@very_lazy_test(["totodev_pub.pipes.toto_example_pipe"],reverify_days=21)
def test_process_text_content(luigi_sync_spawn):
    """Test the process_text_content function directly."""
    from totodev_pub.pipes.toto_example_pipe import run_the_pipe, TextContent
    
    # Test with default parameters
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        text_content = TextContent(text_content="the quick brown fox")
        stats = run_the_pipe(text_content, work_dir)
        assert stats.total_words == 4
        assert stats.min_word_length_used is None
        
        # Verify directory structure is created
        assert (work_dir / "input").exists()
        assert (work_dir / "output").exists()
        assert (work_dir / "input" / "text.yaml").exists()
        assert (work_dir / "output" / "stats.yaml").exists()
    
    # Test with min_word_length parameter in a separate directory
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        text_content = TextContent(text_content="a an the some longer words")
        stats = run_the_pipe(
            text_content,
            work_dir,
            params={"min_word_length": 4}
        )
        assert stats.min_word_length_used == 4
        assert stats.total_words == 3  # only "some", "longer", "words" are >= 4 chars
        
        # Verify directory structure is created
        assert (work_dir / "input").exists()
        assert (work_dir / "output").exists()
        assert (work_dir / "input" / "text.yaml").exists()
        assert (work_dir / "output" / "stats.yaml").exists()

@very_lazy_test(["totodev_pub.pipes.toto_example_pipe"],reverify_days=21)
def test_cli_working_directory(luigi_sync_spawn):
    """Test the CLI working directory handling."""
    from click.testing import CliRunner
    from totodev_pub.pipes.toto_example_pipe import run_sample_pipe

    runner = CliRunner()

    # Test with existing directory
    with tempfile.TemporaryDirectory() as tmpdir:
        existing_dir = Path(tmpdir) / "existing-dir"
        existing_dir.mkdir()

        result = runner.invoke(run_sample_pipe, ['--working-dir', str(existing_dir), '--text', 'test text'])
        assert result.exit_code != 0
        assert isinstance(result.exception, FileExistsError)
    
    # Test with new directory
    with tempfile.TemporaryDirectory() as tmpdir:
        new_dir = Path(tmpdir) / "new-dir"
        
        result = runner.invoke(run_sample_pipe, ['--working-dir', str(new_dir), '--text', 'test text'])
        assert result.exit_code == 0
        assert new_dir.exists()
        assert (new_dir / "input").exists()
        assert (new_dir / "output").exists()
        
        # Verify stats file was created and contains expected data
        stats_file = new_dir / "output" / "stats.yaml"
        assert stats_file.exists()
        with open(stats_file) as f:
            stats = yaml.safe_load(f)
            assert stats['total_words'] == 2
    
    # Test default directory behavior
    result = runner.invoke(run_sample_pipe, ['--text', 'test text'])
    assert result.exit_code == 0
    default_dir = Path("sample-working-dir")
    try:
        assert default_dir.exists()
        assert (default_dir / "input").exists()
        assert (default_dir / "output").exists()
    finally:
        # Clean up the default directory
        import shutil
        shutil.rmtree(default_dir, ignore_errors=True)

@very_lazy_test(["totodev_pub.pipes.toto_example_pipe"],reverify_days=21)
def test_ignore_missing_inputs():
    """Test that ignore_missing_inputs parameter works correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup directories
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        
        # Create input file for text content
        input_file = input_dir / "text.yaml"
        input_data = {"text_content": "test text"}
        with open(input_file, 'w') as f:
            yaml.dump(input_data, f)
        
        # Create pipe
        pipe = SampleWordStatsPipe(working_dir=tmpdir)
        
        # Test with ignore_missing_inputs=True
        pipe.bind_inputs(
            {"input_text": str(input_file)},  # Only provide input_text, omit dummy_pdfs
            ignore_missing_inputs=True
        )
        assert pipe.get_state() == PipeState.INITIALIZED
        
        # Reset pipe
        pipe = SampleWordStatsPipe(working_dir=tmpdir)
        
        # Test with ignore_missing_inputs=False (default)
        with pytest.raises(ValueError, match="Missing required input"):
            pipe.bind_inputs(
                {"input_text": str(input_file)},  # Only provide input_text, omit dummy_pdfs
                ignore_missing_inputs=False
            ) 