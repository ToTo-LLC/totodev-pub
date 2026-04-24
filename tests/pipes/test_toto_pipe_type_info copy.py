# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from pathlib import Path
import pytest
from totodev_pub.pipes.rel_fpath_pattern import RelativeFilepathPattern
from totodev_pub.pipes.toto_pipe_type_info import ToToPipeTypeInfo

def test_suggest_input_filenames_basic_match():
    """Test basic pattern matching with no duplicates."""
    working_dir = Path("/tmp/test")
    patterns = [
        RelativeFilepathPattern("data/*.txt"),
        RelativeFilepathPattern("images/*.jpg")
    ]
    source_files = [
        Path("doc1.txt"),
        Path("pic1.jpg"),
        Path("ignored.pdf")  # Should not match any pattern
    ]
    
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns(source_files, working_dir, patterns)
    
    assert len(results) == 3
    assert results[0] == working_dir / "data" / "doc1.txt"
    assert results[1] == working_dir / "images" / "pic1.jpg"
    assert results[2] is None  # No match for PDF

def test_suggest_input_filenames_duplicates():
    """Test handling of duplicate filenames."""
    working_dir = Path("/tmp/test")
    patterns = [RelativeFilepathPattern("data/*.txt")]
    source_files = [
        Path("same.txt"),
        Path("same.txt"),
        Path("same.txt")
    ]
    
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns(source_files, working_dir, patterns)
    
    assert len(results) == 3
    assert results[0] == working_dir / "data" / "same001.txt"
    assert results[1] == working_dir / "data" / "same002.txt"
    assert results[2] == working_dir / "data" / "same003.txt"

def test_suggest_input_filenames_pattern_priority():
    """Test that more specific patterns are matched first."""
    working_dir = Path("/tmp/test")
    patterns = [
        RelativeFilepathPattern("*.txt"),  # Less specific
        RelativeFilepathPattern("important/*.txt")  # More specific
    ]
    source_files = [Path("test.txt")]
    
    # Should match the more specific pattern
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns(source_files, working_dir, patterns)
    assert results[0] == working_dir / "important" / "test.txt"

def test_suggest_input_filenames_empty_inputs():
    """Test behavior with empty inputs."""
    working_dir = Path("/tmp/test")
    
    # Empty source files
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns([], working_dir, [RelativeFilepathPattern("*.txt")])
    assert results == []
    
    # Empty patterns
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns([Path("test.txt")], working_dir, [])
    assert results == [None]
    
    # Both empty
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns([], working_dir, [])
    assert results == []

def test_suggest_input_filenames_mixed_duplicates():
    """Test handling of mixed files with some duplicates."""
    working_dir = Path("/tmp/test")
    patterns = [RelativeFilepathPattern("data/*.txt")]
    source_files = [
        Path("unique1.txt"),
        Path("repeat.txt"),
        Path("unique2.txt"),
        Path("repeat.txt")
    ]
    
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns(source_files, working_dir, patterns)
    
    assert len(results) == 4
    assert results[0] == working_dir / "data" / "unique1.txt"
    assert results[1] == working_dir / "data" / "repeat001.txt"
    assert results[2] == working_dir / "data" / "unique2.txt"
    assert results[3] == working_dir / "data" / "repeat002.txt"

def test_suggest_input_filenames_complex_patterns():
    """Test matching with complex patterns including subdirectories."""
    working_dir = Path("/tmp/test")
    patterns = [
        RelativeFilepathPattern("data/nested/*.txt"),  # Matches txt files in nested dir
        RelativeFilepathPattern("*.log")  # Matches root log files
    ]
    source_files = [
        Path("test.txt"),
        Path("info.log"),
        Path("doc.txt")
    ]
    
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns(source_files, working_dir, patterns)
    
    assert len(results) == 3
    assert results[0] == working_dir / "data" / "nested" / "test.txt"
    assert results[1] == working_dir / "info.log"
    assert results[2] == working_dir / "data" / "nested" / "doc.txt"

def test_suggest_input_filenames_pattern_none():
    """Test behavior when patterns argument is None."""
    working_dir = Path("/tmp/test")
    source_files = [Path("test.txt")]
    
    results = ToToPipeTypeInfo.suggest_input_filenames_from_patterns(source_files, working_dir, None)
    assert results == [None]

def test_suggest_input_filenames_invalid_inputs():
    """Test that invalid inputs raise appropriate exceptions."""
    working_dir = Path("/tmp/test")
    patterns = [RelativeFilepathPattern("*.txt")]
    
    with pytest.raises(TypeError):
        ToToPipeTypeInfo.suggest_input_filenames_from_patterns("not_a_list", working_dir, patterns)
    
    with pytest.raises(TypeError):
        ToToPipeTypeInfo.suggest_input_filenames_from_patterns([Path("test.txt")], "not_a_path", patterns)
    
    with pytest.raises(TypeError):
        ToToPipeTypeInfo.suggest_input_filenames_from_patterns([Path("test.txt")], working_dir, "not_an_iterable")

def test_suggest_input_filenames_instance_method():
    """Test that the instance method correctly uses patterns from the instance."""
    working_dir = Path("/tmp/test")
    
    # Create a ToToPipeTypeInfo instance with some input patterns
    pipe_type_info = ToToPipeTypeInfo(
        inputs={
            "docs": ("docs/*.txt", None),
            "images": ("images/*.jpg", None),
            "data": ("data/nested/*.csv", None),
        }
    )
    
    source_files = [
        Path("doc1.txt"),
        Path("photo.jpg"),
        Path("data.csv"),
        Path("ignored.pdf")  # Should not match any pattern
    ]
    
    results = pipe_type_info.suggest_input_filenames(source_files, working_dir)
    
    # Verify results match expected patterns
    assert len(results) == 4
    assert results[0] == working_dir / "docs" / "doc1.txt"
    assert results[1] == working_dir / "images" / "photo.jpg"
    assert results[2] == working_dir / "data" / "nested" / "data.csv"
    assert results[3] is None  # No match for PDF

def test_suggest_input_filenames_instance_empty_patterns():
    """Test instance method behavior with no input patterns defined."""
    working_dir = Path("/tmp/test")
    
    # Create instance with no input patterns
    pipe_type_info = ToToPipeTypeInfo()
    source_files = [Path("test.txt")]
    
    results = pipe_type_info.suggest_input_filenames(source_files, working_dir)
    assert results == [None]  # No patterns to match against 