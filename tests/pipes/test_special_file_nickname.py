# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
from pathlib import Path
import time
import os
import platform
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname
from totodev_pub.pytest_tools import very_lazy_test

def test_enum_values():
    """Test that all expected enum values exist with correct string values."""
    assert SpecialPipeFileNickname.BEGIN.value == "begin"
    assert SpecialPipeFileNickname.COMPLETION.value == "completion"
    assert SpecialPipeFileNickname.HEARTBEAT.value == "heartbeat"
    assert SpecialPipeFileNickname.EXECUTE_FAILS.value == "execute_fails"

def test_filename_generation():
    """Test filename generation for different special file types."""
    # Test regular YAML files
    assert SpecialPipeFileNickname.BEGIN.filename() == "_pipe_begin.yaml"
    assert SpecialPipeFileNickname.COMPLETION.filename() == "_pipe_completion.yaml"
    assert SpecialPipeFileNickname.EXECUTE_FAILS.filename() == "_pipe_execute_fails.yaml"
    
    # Test special case for heartbeat
    assert SpecialPipeFileNickname.HEARTBEAT.filename() == "_pipe_heartbeat.txt"

def test_abspath_generation(tmp_path):
    """Test absolute path generation with different working directory types."""
    # Test with string path
    working_dir_str = str(tmp_path)
    assert SpecialPipeFileNickname.BEGIN.abspath(working_dir_str) == Path(working_dir_str) / "_pipe_begin.yaml"
    
    # Test with Path object
    working_dir_path = Path(tmp_path)
    assert SpecialPipeFileNickname.COMPLETION.abspath(working_dir_path) == working_dir_path / "_pipe_completion.yaml"

@pytest.fixture
def mock_files(tmp_path):
    """Create mock special files with controlled modification times."""
    files = {}
    
    # Create files with different ages
    for nickname in SpecialPipeFileNickname:
        file_path = nickname.abspath(tmp_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()
        files[nickname] = file_path
    
    # Set specific modification times with longer intervals
    files[SpecialPipeFileNickname.HEARTBEAT].touch()  # oldest
    time.sleep(0.5)  # Increased sleep time
    files[SpecialPipeFileNickname.COMPLETION].touch()  # middle
    time.sleep(0.5)  # Increased sleep time
    files[SpecialPipeFileNickname.BEGIN].touch()  # newest
    
    # Small additional sleep to ensure file operations are complete
    time.sleep(0.1)
    return tmp_path

@pytest.mark.slow
@very_lazy_test(["totodev_pub.pipes.special_pipe_file_nickname"])
def test_file_ages(mock_files):
    """Test file age calculation for existing and non-existing files."""
    # Get ages
    ages = SpecialPipeFileNickname.file_ages(mock_files)
    
    # All files should exist and have an age
    assert all(age is not None for age in ages.values())
    
    # Test relative ages (HEARTBEAT should be oldest, BEGIN newest)
    assert ages[SpecialPipeFileNickname.HEARTBEAT] > ages[SpecialPipeFileNickname.COMPLETION]
    assert ages[SpecialPipeFileNickname.COMPLETION] > ages[SpecialPipeFileNickname.BEGIN]
    
    # Delete a file and verify its age becomes None
    SpecialPipeFileNickname.BEGIN.abspath(mock_files).unlink()
    new_ages = SpecialPipeFileNickname.file_ages(mock_files)
    assert new_ages[SpecialPipeFileNickname.BEGIN] is None

def test_file_ages_filesystem_errors(tmp_path):
    """Test file_ages behavior with filesystem errors."""
    # Test with non-existent directory
    non_existent_dir = tmp_path / "does_not_exist"
    with pytest.raises(OSError):
        SpecialPipeFileNickname.file_ages(non_existent_dir)
    
    if platform.system() != "Windows":
        # Test with permission error on Unix-like systems
        no_access_dir = tmp_path / "no_access"
        no_access_dir.mkdir()
        
        try:
            # Remove all permissions
            no_access_dir.chmod(0o000)
            with pytest.raises(OSError):
                SpecialPipeFileNickname.file_ages(no_access_dir)
        finally:
            # Restore permissions so the directory can be cleaned up
            no_access_dir.chmod(0o700)
    else:
        # On Windows, create a directory and try to access it while holding a lock
        import msvcrt
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        
        try:
            # Create a temporary file in the directory to lock
            lock_file = locked_dir / "lock"
            lock_file.touch()
            
            # Try to hold an exclusive lock on the file
            with open(str(lock_file), 'rb') as f:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    with pytest.raises(OSError):
                        SpecialPipeFileNickname.file_ages(locked_dir)
                finally:
                    # Release the lock
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            # Clean up
            if lock_file.exists():
                lock_file.unlink()
            locked_dir.rmdir() 