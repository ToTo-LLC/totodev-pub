# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from pathlib import Path

import pytest

from totodev_pub.pipes.rel_fpath_pattern import RelativeFilepathPattern


@pytest.fixture
def temp_dir(tmp_path):
    """Create a temporary directory with some test files"""
    # Create test directory structure
    (tmp_path / "inputs").mkdir()
    (tmp_path / "outputs").mkdir()
    (tmp_path / "logs").mkdir()
    
    # Create some test files
    (tmp_path / "inputs" / "test1.pdf").touch()
    (tmp_path / "inputs" / "test2.pdf").touch()
    (tmp_path / "outputs" / "result.yaml").touch()
    (tmp_path / "process.log").touch()
    (tmp_path / "logs" / "logfile01.log").touch()
    (tmp_path / "logs" / "logfile02.log").touch()
    
    return tmp_path

def test_init():
    # Test basic initialization
    pattern = RelativeFilepathPattern("inputs/*.pdf")
    assert pattern.pattern == "inputs/*.pdf"
    assert pattern.nickname is None
    assert pattern.root_folder is None

    # Test with nickname and root_folder
    pattern = RelativeFilepathPattern("inputs/*.pdf", "pdf_files", "/root")
    assert pattern.pattern == "inputs/*.pdf"
    assert pattern.nickname == "pdf_files"
    assert pattern.root_folder == Path("/root")

def test_repr():
    """Test the string representation of RelativeFilepathPattern"""
    # Test repr without nickname
    pattern = RelativeFilepathPattern("inputs/*.pdf")
    assert repr(pattern) == "RelativeFilepathPattern(pattern='inputs/*.pdf')"

    # Test repr with nickname
    pattern = RelativeFilepathPattern("inputs/*.pdf", "pdf_files")
    assert repr(pattern) == "RelativeFilepathPattern(nickname='pdf_files', pattern='inputs/*.pdf')"

def test_equality_and_hashing():
    """Test equality and hashing behavior of RelativeFilepathPattern"""
    # Same pattern, different nicknames - should be equal
    pattern1 = RelativeFilepathPattern("inputs/*.pdf", "pdf_files")
    pattern2 = RelativeFilepathPattern("inputs/*.pdf", "documents")
    assert pattern1 == pattern2
    assert hash(pattern1) == hash(pattern2)

    # Same pattern, one with nickname, one without - should be equal
    pattern3 = RelativeFilepathPattern("inputs/*.pdf")
    assert pattern1 == pattern3
    assert hash(pattern1) == hash(pattern3)

    # Same pattern, different root_folders - should be equal
    pattern4 = RelativeFilepathPattern("inputs/*.pdf", "pdf_files", "/root1")
    pattern5 = RelativeFilepathPattern("inputs/*.pdf", "pdf_files", "/root2")
    assert pattern4 == pattern5
    assert hash(pattern4) == hash(pattern5)

    # Different patterns - should not be equal
    pattern6 = RelativeFilepathPattern("outputs/*.pdf", "pdf_files")
    assert pattern1 != pattern6
    assert hash(pattern1) != hash(pattern6)

    # Test dictionary behavior
    pattern_dict = {pattern1: "value1"}
    assert pattern_dict[pattern2] == "value1"  # Can retrieve with equal pattern
    assert pattern_dict[pattern3] == "value1"  # Can retrieve with unnamed pattern
    assert pattern_dict[pattern4] == "value1"  # Can retrieve with different root_folder

def test_init_validation():
    # Test absolute path validation
    with pytest.raises(ValueError, match="must be a relative path"):
        RelativeFilepathPattern("/inputs/*.pdf")

    # Test parent directory validation
    with pytest.raises(ValueError, match="cannot contain '..'"):
        RelativeFilepathPattern("../inputs/*.pdf")

    # Test wildcard in directory validation
    with pytest.raises(ValueError, match="may not contain wildcards"):
        RelativeFilepathPattern("inputs/*/test.pdf")

def test_wildcard_count():
    pattern = RelativeFilepathPattern("inputs/*.pdf")
    assert pattern.wildcard_count() == 1

    pattern = RelativeFilepathPattern("inputs/test_*_*.pdf")
    assert pattern.wildcard_count() == 2

    pattern = RelativeFilepathPattern("inputs/test.pdf")
    assert pattern.wildcard_count() == 0

def test_calc_path():
    pattern = RelativeFilepathPattern("inputs/*.pdf", root_folder="/root")
    
    # Test basic path calculation
    assert pattern.calc_path(["test"]) == Path("/root/inputs/test.pdf")
    
    # Test with multiple wildcards
    pattern = RelativeFilepathPattern("inputs/*_*.pdf", root_folder="/root")
    assert pattern.calc_path(["test", "001"]) == Path("/root/inputs/test_001.pdf")
    
    # Test error when merge components don't match wildcards
    with pytest.raises(ValueError, match="does not match the number of merge components"):
        pattern.calc_path(["test"])

def test_matched_files(temp_dir):
    # Test PDF pattern
    pdf_pattern = RelativeFilepathPattern("inputs/*.pdf", root_folder=temp_dir)
    matched = pdf_pattern.matched_files()
    assert len(matched) == 2
    assert all(f.name.endswith(".pdf") for f in matched)

    # Test specific file pattern
    yaml_pattern = RelativeFilepathPattern("outputs/result.yaml", root_folder=temp_dir)
    matched = yaml_pattern.matched_files()
    assert len(matched) == 1
    assert matched[0].name == "result.yaml"

    # Test pattern with no matches
    no_match_pattern = RelativeFilepathPattern("inputs/*.txt", root_folder=temp_dir)
    assert len(no_match_pattern.matched_files()) == 0

def test_is_match(temp_dir):
    pattern = RelativeFilepathPattern("inputs/*.pdf", root_folder=temp_dir)
    
    # Test matching file
    assert pattern.is_match(temp_dir / "inputs/test1.pdf")
    
    # Test non-matching file
    assert not pattern.is_match(temp_dir / "inputs/test.txt")
    assert not pattern.is_match(temp_dir / "outputs/test.pdf")

def test_has_subdir():
    # Test pattern with subdir
    pattern = RelativeFilepathPattern("inputs/*.pdf")
    assert pattern.has_subdir()

    # Test pattern without subdir
    pattern = RelativeFilepathPattern("test.pdf")
    assert not pattern.has_subdir()

def test_create_subdir(temp_dir):
    # Test creating new subdirectory
    new_pattern = RelativeFilepathPattern("newdir/*.txt", root_folder=temp_dir)
    new_pattern.create_subdir()
    assert (temp_dir / "newdir").is_dir()

    # Test with existing directory
    existing_pattern = RelativeFilepathPattern("inputs/*.pdf", root_folder=temp_dir)
    existing_pattern.create_subdir()  # Should not raise error
    assert (temp_dir / "inputs").is_dir()

def test_affirm_subdir(temp_dir):
    # Test affirming new subdirectory
    new_pattern = RelativeFilepathPattern("newdir/*.txt", root_folder=temp_dir)
    new_pattern.affirm_subdir()
    assert (temp_dir / "newdir").is_dir()

    # Test with pattern having no subdir
    no_subdir_pattern = RelativeFilepathPattern("test.txt", root_folder=temp_dir)
    no_subdir_pattern.affirm_subdir()  # Should do nothing
    assert not (temp_dir / "test.txt").exists()

def test_affirm_subdirs(temp_dir):
    """Test the static affirm_subdirs method with various pattern types."""
    # Test with string patterns
    patterns = [
        "newdir1/*.txt",
        "newdir2/subdir/*.pdf",
        "test.log",  # No subdir
        "newdir3/*.yaml"
    ]
    
    RelativeFilepathPattern.affirm_subdirs(temp_dir, patterns)
    
    # Check directories were created
    assert (temp_dir / "newdir1").is_dir()
    assert (temp_dir / "newdir2" / "subdir").is_dir()
    assert not (temp_dir / "test.log").exists()  # Should not create file
    assert (temp_dir / "newdir3").is_dir()
    
    # Test with mixed pattern types (strings and RelativeFilepathPattern instances)
    more_patterns = [
        RelativeFilepathPattern("newdir4/*.txt"),
        "newdir5/*.pdf",
        RelativeFilepathPattern("root.log"),  # No subdir
    ]
    
    RelativeFilepathPattern.affirm_subdirs(temp_dir, more_patterns)
    
    # Check new directories were created
    assert (temp_dir / "newdir4").is_dir()
    assert (temp_dir / "newdir5").is_dir()
    assert not (temp_dir / "root.log").exists()  # Should not create file
    
    # Test error case - no root folder
    with pytest.raises(ValueError, match="root_folder must be provided"):
        RelativeFilepathPattern.affirm_subdirs(None, patterns)

def test_from_dict(temp_dir):
    patterns_dict = {
        "pdfs": "inputs/*.pdf",
        "yaml": "outputs/result.yaml",
        "logs": "logs/logfile[0-9][0-9].log"
    }
    
    patterns = list(RelativeFilepathPattern.from_dict(patterns_dict, temp_dir))
    assert len(patterns) == 3
    assert all(isinstance(p, RelativeFilepathPattern) for p in patterns)
    assert {p.nickname for p in patterns} == {"pdfs", "yaml", "logs"}

def test_affirm_subdirs_absolute_paths(temp_dir):
    """Test that affirm_subdirs returns absolute paths when given an absolute root folder."""
    # Create patterns with subdirectories
    patterns = [
        "subdir1/*.txt",
        "subdir2/nested/*.pdf",
        "file.log",  # No subdir
        "subdir3/*.yaml"
    ]
    
    # Get absolute path to temp_dir
    abs_root = Path(temp_dir).resolve()
    
    # Create subdirectories
    created_dirs = RelativeFilepathPattern.affirm_subdirs(abs_root, patterns)
    
    # Verify all returned paths are absolute
    assert all(p.is_absolute() for p in created_dirs)
    
    # Verify the correct directories were created and are absolute
    expected_dirs = [
        abs_root / "subdir1",
        abs_root / "subdir2" / "nested",
        abs_root / "subdir3"
    ]
    assert set(created_dirs) == set(expected_dirs)
    
    # Verify all directories exist
    assert all(p.exists() for p in created_dirs) 