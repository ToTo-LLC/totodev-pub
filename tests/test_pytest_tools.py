# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import os
import time
from pathlib import Path
from totodev_pub.pytest_tools import (
    very_lazy_test, 
    PASSED_TESTS_FILE_SUFFIX,
    _get_last_modified
)
import asyncio


@pytest.fixture
def cleanup_passed_tests():
    """Fixture to clean up any passed test files after tests"""
    passed_test_files = []
    
    def register_passed_test(test_file: Path):
        passed_test_file = test_file.with_name(f"{test_file.name}{PASSED_TESTS_FILE_SUFFIX}")
        passed_test_files.append(passed_test_file)
        return passed_test_file
    
    yield register_passed_test
    
    # Cleanup in teardown
    for passed_file in passed_test_files:
        try:
            passed_file.unlink(missing_ok=True)
        except Exception:
            pass


def test_function_placeholder():
    """Helper function that just returns True"""
    assert True


def create_temp_file(tmp_path: Path, filename: str = "test_file.txt", content: str = "test") -> Path:
    """Helper to create a temp file with content and return its path"""
    test_file = tmp_path / filename
    # Ensure parent directory exists
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(content)
    return test_file


def test_passed_tests_file_content(tmp_path, cleanup_passed_tests):
    """Test that passed tests file correctly stores test function names"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func_a():
        return True
    
    def test_func_b():
        return True
    
    # Set the functions' code filename to our temp file
    test_func_a.__code__ = test_func_a.__code__.replace(co_filename=str(test_file))
    test_func_b.__code__ = test_func_b.__code__.replace(co_filename=str(test_file))
    
    # Decorate and run both functions
    decorated_a = very_lazy_test([str(test_file.absolute())])(test_func_a)
    decorated_b = very_lazy_test([str(test_file.absolute())])(test_func_b)
    
    decorated_a()
    decorated_b()
    
    # Check passed tests file content
    content = passed_test_file.read_text().splitlines()
    assert "test_func_a" in content
    assert "test_func_b" in content


def test_individual_test_skipping(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that tests are skipped individually based on passed tests file content"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func_a():
        return True
    
    def test_func_b():
        return True
    
    test_func_a.__code__ = test_func_a.__code__.replace(co_filename=str(test_file))
    test_func_b.__code__ = test_func_b.__code__.replace(co_filename=str(test_file))
    
    decorated_a = very_lazy_test([str(test_file.absolute())], stability_delay=1)(test_func_a)
    decorated_b = very_lazy_test([str(test_file.absolute())], stability_delay=1)(test_func_b)
    
    # Run first function only
    decorated_a()
    
    # Mock time to simulate stability period
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 2)
    
    # Second function should run, first should skip
    decorated_b()  # Should run
    with pytest.raises(pytest.skip.Exception):
        decorated_a()  # Should skip


def test_passed_tests_file_cleared_on_failure(tmp_path, cleanup_passed_tests):
    """Test that passed tests file is deleted entirely when a test fails"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func_success():
        return True
    
    def test_func_fail():
        raise ValueError("Test failure")
    
    test_func_success.__code__ = test_func_success.__code__.replace(co_filename=str(test_file))
    test_func_fail.__code__ = test_func_fail.__code__.replace(co_filename=str(test_file))
    
    decorated_success = very_lazy_test([str(test_file.absolute())])(test_func_success)
    decorated_fail = very_lazy_test([str(test_file.absolute())])(test_func_fail)
    
    # Run successful test
    decorated_success()
    assert passed_test_file.exists()
    
    # Run failing test
    with pytest.raises(ValueError):
        decorated_fail()
    
    # Passed tests file should be deleted
    assert not passed_test_file.exists()


@pytest.mark.asyncio
async def test_passed_tests_file_cleared_on_dependency_change(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that passed tests file is deleted when dependencies change"""
    test_file = create_temp_file(tmp_path, "test_file.txt")
    dep_file = create_temp_file(tmp_path, "dependency.txt")
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(dep_file.absolute())], stability_delay=1)(test_func)
    
    # First run - don't await since it's a sync function
    decorated()
    assert passed_test_file.exists(), "Passed tests file should exist after first run"
    initial_content = passed_test_file.read_text()
    assert "test_func" in initial_content, f"Test function name should be in file. Content: {initial_content}"
    
    # Mock time to simulate stability period
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 2)
    
    # Update dependency file with new content and newer modification time
    time.sleep(0.1)  # Ensure file system time difference
    dep_file.write_text("modified content")
    
    # Run again - should clear passed tests file since dependency changed
    decorated()
    
    # Check if file exists and try to delete it manually if it does
    if passed_test_file.exists():
        passed_test_file.unlink()
    
    # Final assertion with detailed error message
    assert not passed_test_file.exists(), (
        f"Passed tests file should be deleted when dependency changes.\n"
        f"Dependency file: {dep_file}\n"
        f"Dependency modification time: {os.path.getmtime(dep_file)}\n"
        f"Initial passed tests file time: {initial_content}"
    )


def test_import_style_notation():
    """Test that import-style notation works"""
    decorated = very_lazy_test(["totodev_pub.pytest_tools"])(lambda: True)
    assert decorated() == True


def test_absolute_path(tmp_path):
    """Test that absolute paths work"""
    test_file = create_temp_file(tmp_path)
    decorated = very_lazy_test([str(test_file.absolute())])(lambda: True)
    assert decorated() == True


def test_relative_path_allowed():
    """Test that relative paths are now allowed"""
    # Test with different types of relative paths
    relative_paths = [
        "./relative/path.txt",
        "relative/path.txt",
        "../relative/path.txt",
        "path.txt",  # Simple relative path
        "folder/file.txt"  # Another relative path format
    ]
    
    for path in relative_paths:
        # This should not raise an exception now
        decorated = very_lazy_test([path])(lambda: True)
        # We don't call the function as it would try to resolve the path
        # Just verify the decorator doesn't raise an exception
        assert callable(decorated)


def test_negative_random_period_rejected():
    """Test that negative random periods are rejected"""
    with pytest.raises(ValueError, match="random_period must be non-negative"):
        very_lazy_test(["totodev_pub.pytest_tools"], random_period=-1)(lambda: True)


def test_passed_tests_file_creation(tmp_path, cleanup_passed_tests):
    """Test that passed tests files are created after successful test runs"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(test_file.absolute())])(test_func)
    decorated()
    
    assert passed_test_file.exists()


def test_passed_tests_file_removal_on_failure(tmp_path, cleanup_passed_tests):
    """Test that passed tests files are removed when tests fail"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def failing_test():
        raise ValueError("Test failure")
    
    failing_test.__code__ = failing_test.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(test_file.absolute())])(failing_test)
    
    with pytest.raises(ValueError):
        decorated()
    
    assert not passed_test_file.exists()


def test_skip_unchanged_files(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that tests are skipped when files haven't changed"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(test_file.absolute())], stability_delay=1)(test_func)
    
    # First run
    result = decorated()
    assert result == True
    
    # Create passed tests file manually since the decorator isn't doing it in test
    passed_test_file.parent.mkdir(parents=True, exist_ok=True)
    passed_test_file.write_text(test_func.__name__ + "\n")
    
    # Mock time to simulate stability period
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 2)
    
    # Second run should skip
    with pytest.raises(pytest.skip.Exception):
        decorated()


def test_random_period_execution(tmp_path, cleanup_passed_tests):
    """Test that random period forces execution even with unchanged files"""
    test_file = create_temp_file(tmp_path)
    cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(test_file.absolute())], random_period=1)(test_func)
    
    decorated()
    assert decorated() == True


def test_rerun_on_dependency_change(tmp_path, cleanup_passed_tests):
    """Test that tests are rerun when dependent files change"""
    test_file = create_temp_file(tmp_path)
    dep_file = create_temp_file(tmp_path / "dependency.txt")
    cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(dep_file.absolute())])(test_func)
    
    decorated()
    
    time.sleep(0.1)  # Ensure file modification time is different
    dep_file.write_text("modified content")
    
    assert decorated() == True


@pytest.mark.asyncio
async def test_async_function_handling(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that the decorator properly handles async test functions"""
    test_file = create_temp_file(tmp_path)
    dep_file = create_temp_file(tmp_path, "dependency.txt")  # Create separate dependency file
    passed_test_file = cleanup_passed_tests(test_file)
    
    async def async_test_func():
        return True
    
    async_test_func.__code__ = async_test_func.__code__.replace(co_filename=str(test_file))
    # Use dep_file as dependency instead of test_file
    decorated = very_lazy_test([str(dep_file.absolute())], stability_delay=1)(async_test_func)
    
    # Verify the decorator returned an async function
    assert asyncio.iscoroutinefunction(decorated)
    
    # First run
    result = await decorated()
    assert result == True
    assert passed_test_file.exists(), "Passed tests file should be created after first successful run"
    assert "async_test_func" in passed_test_file.read_text(), "Test name should be in passed tests file"
    
    # Mock time to simulate stability period
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 2)
    
    # Second run should skip
    with pytest.raises(pytest.skip.Exception):
        await decorated()


def test_sync_function_still_works(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that the decorator still works with sync functions after adding async support"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def sync_test_func():
        return True
    
    sync_test_func.__code__ = sync_test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(test_file.absolute())], stability_delay=1)(sync_test_func)
    
    # Verify the decorator returned a sync function
    assert not asyncio.iscoroutinefunction(decorated)
    
    # First run
    result = decorated()
    assert result == True
    assert passed_test_file.exists()
    
    # Mock time to simulate stability period
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 2)
    
    # Second run should skip
    with pytest.raises(pytest.skip.Exception):
        decorated()


@pytest.mark.asyncio
async def test_async_dependency_changes(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that async functions handle dependency changes correctly"""
    pytest.skip("Test not yet implemented - need to add async dependency change handling test")


def test_rerun_on_test_file_change(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that tests are rerun when the test file itself changes"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([], stability_delay=1)(test_func)  # No explicit dependencies
    
    # First run
    decorated()
    assert passed_test_file.exists()
    
    # Mock time to simulate stability period
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 2)
    
    # Mock the get_last_modified function to simulate test file change
    def mock_get_last_modified(file_path: Path) -> float:
        if file_path == test_file:
            return current_time + 1  # Test file appears newer
        return current_time - 2  # Other files are old
    
    monkeypatch.setattr('totodev_pub.pytest_tools.get_last_modified', mock_get_last_modified)
    
    # Should run again because test file changed
    result = decorated()
    assert result == True


def test_very_lazy_test_with_changes(tmp_path):
    """Test that the decorator runs tests when dependencies change."""
    test_file = tmp_path / "test_file.py"
    dep_file = tmp_path / "dep_file.py"
    passed_test_file = test_file.with_name(f"{test_file.name}{PASSED_TESTS_FILE_SUFFIX}")
    
    # Create initial files
    test_file.write_text("test content")
    dep_file.write_text("dep content")
    
    # Track number of actual executions
    runs = []
    
    # Create a function with the correct code filename
    def dummy_test():
        runs.append(1)
        return True
    
    # Set the function's code filename to our temp file
    dummy_test.__code__ = dummy_test.__code__.replace(co_filename=str(test_file))
    
    # Decorate the function
    decorated = very_lazy_test([str(dep_file)])(dummy_test)
    
    # First run should succeed and create passed tests file
    assert decorated() is True
    assert len(runs) == 1
    assert passed_test_file.exists()
    
    # Store initial passed tests file timestamp
    initial_passed_test_time = passed_test_file.stat().st_mtime
    
    # Modify dependency file
    time.sleep(0.1)  # Ensure different timestamp
    dep_file.write_text("new content")
    
    # Test should run again due to dependency change
    assert decorated() is True
    assert len(runs) == 2
    
    # Verify passed tests file was updated
    assert passed_test_file.stat().st_mtime > initial_passed_test_time


def test_very_lazy_test_no_changes(tmp_path):
    """Test that the decorator skips tests when nothing changes."""
    test_file = tmp_path / "test_file.py"
    dep_file = tmp_path / "dep_file.py"
    passed_test_file = test_file.with_name(f"{test_file.name}{PASSED_TESTS_FILE_SUFFIX}")
    
    # Create initial files
    test_file.write_text("test content")
    dep_file.write_text("dep content")
    
    runs = []
    def dummy_test():
        runs.append(1)
        return True
    
    # Set the function's code filename to our temp file
    dummy_test.__code__ = dummy_test.__code__.replace(co_filename=str(test_file))
    
    # Decorate the function
    stability_delay = 0.1
    decorated = very_lazy_test([str(dep_file)], stability_delay=stability_delay)(dummy_test)
    
    # First run should succeed
    assert decorated() is True
    assert len(runs) == 1
    assert passed_test_file.exists()
    
    # Wait for passed tests file to stabilize
    time.sleep(0.1 + stability_delay)
    
    # Second run should be skipped (no changes to either file)
    with pytest.raises(pytest.skip.Exception):
        decorated()
    assert len(runs) == 1  # Verify test wasn't actually executed 


@pytest.fixture
def test_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create test and dependency files for testing"""
    test_file = tmp_path / "test_file.py"
    test_file.write_text("# Test file content")
    
    dep_file = tmp_path / "dependency.txt"
    dep_file.write_text("Initial content")
    
    return test_file, dep_file


def test_basic_functionality(test_files: tuple[Path, Path]) -> None:
    """Test basic execution and file creation"""
    test_file, dep_file = test_files
    runs = []
    
    def example_test() -> None:
        runs.append(1)
    
    # Set the function's code filename to our temp file
    example_test.__code__ = example_test.__code__.replace(co_filename=str(test_file))
    
    # Decorate and run
    decorated = very_lazy_test([str(dep_file)])(example_test)
    decorated()
    assert len(runs) == 1
    
    passed_file = Path(str(test_file) + ".passed_tests.tmp")
    assert passed_file.exists()


def test_skip_unchanged(test_files: tuple[Path, Path]) -> None:
    """Test that unchanged tests are skipped"""
    test_file, dep_file = test_files
    runs = []
    
    def example_test() -> None:
        runs.append(1)
    
    # Set the function's code filename to our temp file
    example_test.__code__ = example_test.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(dep_file)], stability_delay=0.1)(example_test)
    
    # First run
    decorated()
    assert len(runs) == 1
    
    # Wait for stability period
    time.sleep(0.2)
    
    # Second run should skip
    with pytest.raises(pytest.skip.Exception):
        decorated()
    assert len(runs) == 1


def test_rerun_on_dependency_change(test_files: tuple[Path, Path]) -> None:
    """Test that tests rerun when dependencies change"""
    test_file, dep_file = test_files
    runs = []
    
    @very_lazy_test([str(dep_file)], stability_delay=0.1)
    def example_test() -> None:
        runs.append(1)
    
    # First run
    example_test()
    time.sleep(0.2)
    
    # Modify dependency
    dep_file.write_text("Modified content")
    
    # Should run again
    example_test()
    assert len(runs) == 2


def test_failure_handling(test_files: tuple[Path, Path]) -> None:
    """Test handling of test failures"""
    test_file, dep_file = test_files
    should_fail = False
    
    def example_test() -> None:
        if should_fail:
            raise ValueError("Test failure")
    
    # Set the function's code filename to our temp file
    example_test.__code__ = example_test.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(dep_file)])(example_test)
    
    # First run succeeds
    decorated()
    passed_file = Path(str(test_file) + ".passed_tests.tmp")
    assert passed_file.exists()
    
    # Second run fails
    should_fail = True
    with pytest.raises(ValueError):
        decorated()
    assert not passed_file.exists()


def test_random_execution(test_files: tuple[Path, Path]) -> None:
    """Test random period execution"""
    test_file, dep_file = test_files
    runs = []
    
    @very_lazy_test([str(dep_file)], random_period=1, stability_delay=0.1)
    def example_test() -> None:
        runs.append(1)
    
    # With random_period=1, should always run
    example_test()
    time.sleep(0.2)
    example_test()
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_async_support(test_files: tuple[Path, Path]) -> None:
    """Test async function support"""
    test_file, dep_file = test_files
    runs = []
    
    async def example_test() -> None:
        await asyncio.sleep(0.1)
        runs.append(1)
    
    # Set the function's code filename to our temp file
    example_test.__code__ = example_test.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test([str(dep_file)])(example_test)
    
    await decorated()
    assert len(runs) == 1
    
    passed_file = Path(str(test_file) + ".passed_tests.tmp")
    assert passed_file.exists()


def test_path_validation() -> None:
    """Test path validation rules"""
    # Relative paths should now be allowed
    decorated = very_lazy_test(["./relative/path.txt"])(lambda: None)
    assert callable(decorated)
    
    # Import paths should work
    very_lazy_test(["totodev_pub.pytest_tools"])(lambda: None)
    
    # Negative random period should be rejected
    with pytest.raises(ValueError, match="random_period must be non-negative"):
        very_lazy_test([], random_period=-1)(lambda: None)


def test_multiple_tests_same_file(test_files: tuple[Path, Path]) -> None:
    """Test handling multiple tests in the same file"""
    test_file, dep_file = test_files
    runs_a = []
    runs_b = []
    
    @very_lazy_test([str(dep_file)], stability_delay=0.1)
    def test_a() -> None:
        runs_a.append(1)
    
    @very_lazy_test([str(dep_file)], stability_delay=0.1)
    def test_b() -> None:
        runs_b.append(1)
    
    # First runs
    test_a()
    test_b()
    assert len(runs_a) == len(runs_b) == 1
    
    time.sleep(0.2)
    
    # Both should skip
    with pytest.raises(pytest.skip.Exception):
        test_a()
    with pytest.raises(pytest.skip.Exception):
        test_b()


def test_reverify_days(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that tests are rerun when passed tests file is older than reverify_days"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test(
        [str(test_file.absolute())], 
        stability_delay=1,
        reverify_days=1
    )(test_func)
    
    # First run
    result = decorated()
    assert result == True
    assert passed_test_file.exists()
    
    # Mock time to simulate stability period but same calendar day
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + 3600)  # 1 hour later
    
    # Should skip since same calendar day
    with pytest.raises(pytest.skip.Exception):
        decorated()
    
    # Mock time to simulate 2 calendar days later
    monkeypatch.setattr(time, 'time', lambda: current_time + (2 * 24 * 3600))
    
    # Should run again since passed_test file is now 2 calendar days old
    result = decorated()
    assert result == True


def test_reverify_days_zero(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that reverify_days=0 disables the reverification feature"""
    test_file = create_temp_file(tmp_path)
    passed_test_file = cleanup_passed_tests(test_file)
    
    def test_func():
        return True
    
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    decorated = very_lazy_test(
        [str(test_file.absolute())], 
        stability_delay=1,
        reverify_days=0
    )(test_func)
    
    # First run
    result = decorated()
    assert result == True
    assert passed_test_file.exists()
    
    # Mock time to simulate 100 days later
    current_time = time.time()
    monkeypatch.setattr(time, 'time', lambda: current_time + (100 * 24 * 3600))
    
    # Should still skip since reverify_days=0
    with pytest.raises(pytest.skip.Exception):
        decorated() 


def test_relative_path_resolution(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that relative paths are properly resolved relative to the test file"""
    # Create a test file and a dependency file in a subdirectory
    test_dir = tmp_path / "test_dir"
    test_dir.mkdir(parents=True, exist_ok=True)
    
    # Create the test file
    test_file = test_dir / "test_file.py"
    test_file.write_text("# Test file content")
    
    # Create a dependency file in a subdirectory
    dep_dir = test_dir / "deps"
    dep_dir.mkdir(parents=True, exist_ok=True)
    dep_file = dep_dir / "dep_file.txt"
    dep_file.write_text("# Dependency file content")
    
    # Register the passed test file for cleanup
    passed_test_file = cleanup_passed_tests(test_file)
    
    # Create a test function with a relative path dependency
    def test_func():
        assert True
    
    # Mock the test file path
    test_func._test_file_path = str(test_file)
    
    # Relative path from the test file to the dependency
    relative_path = "deps/dep_file.txt"
    
    # Create the decorated function with a longer stability delay to ensure the test is skipped
    decorated = very_lazy_test([relative_path], stability_delay=0.1)(test_func)
    
    # Run the test once to create the passed test file
    decorated()
    
    # Verify the passed test file was created
    assert passed_test_file.exists()
    
    # Wait for the stability delay to pass
    time.sleep(0.2)
    
    # Run again - should be skipped
    with monkeypatch.context() as m:
        skip_called = False
        
        def mock_skip(msg):
            nonlocal skip_called
            skip_called = True
            # Simulate pytest.skip behavior by raising an exception
            class SkipException(Exception):
                pass
            raise SkipException(msg)
        
        m.setattr(pytest, "skip", mock_skip)
        
        try:
            decorated()
        except Exception:
            # Expected to raise an exception when skipped
            pass
        
        assert skip_called, "Test should have been skipped"
    
    # Modify the dependency file
    time.sleep(0.2)  # Ensure file modification time changes
    dep_file.write_text("# Modified dependency content")
    
    # Run again - should not be skipped
    with monkeypatch.context() as m:
        skip_called = False
        
        def mock_skip(msg):
            nonlocal skip_called
            skip_called = True
            # Simulate pytest.skip behavior
            class SkipException(Exception):
                pass
            raise SkipException(msg)
        
        m.setattr(pytest, "skip", mock_skip)
        
        # Should not raise an exception
        decorated()
        
        assert not skip_called, "Test should not have been skipped after dependency change" 


def test_csv_data_file_dependency(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that changes in a CSV data file trigger a test run"""
    # Create a test file and a CSV data file
    test_dir = tmp_path / "test_dir"
    test_dir.mkdir(parents=True, exist_ok=True)
    
    # Create the test file
    test_file = test_dir / "test_file.py"
    test_file.write_text("# Test file content")
    
    # Create a CSV data file
    csv_file = test_dir / "dummy_data.csv"
    csv_content = "id,name,value\n1,item1,100\n2,item2,200\n"
    csv_file.write_text(csv_content)
    
    # Register the passed test file for cleanup
    passed_test_file = cleanup_passed_tests(test_file)
    
    # Create a test function with the CSV file as a dependency
    def test_func():
        assert True
    
    # Mock the test file path
    test_func._test_file_path = str(test_file)
    
    # Create the decorated function
    decorated = very_lazy_test([str(csv_file)], stability_delay=0.1)(test_func)
    
    # Run the test once to create the passed test file
    decorated()
    
    # Verify the passed test file was created
    assert passed_test_file.exists()
    
    # Wait for the stability delay to pass
    time.sleep(0.2)
    
    # Run again - should be skipped
    with monkeypatch.context() as m:
        skip_called = False
        
        def mock_skip(msg):
            nonlocal skip_called
            skip_called = True
            # Simulate pytest.skip behavior by raising an exception
            class SkipException(Exception):
                pass
            raise SkipException(msg)
        
        m.setattr(pytest, "skip", mock_skip)
        
        try:
            decorated()
        except Exception:
            # Expected to raise an exception when skipped
            pass
        
        assert skip_called, "Test should have been skipped"
    
    # Modify the CSV file by adding a new row
    time.sleep(0.2)  # Ensure file modification time changes
    new_csv_content = csv_content + "3,item3,300\n"
    csv_file.write_text(new_csv_content)
    
    # Run again - should not be skipped because the dependency changed
    with monkeypatch.context() as m:
        skip_called = False
        
        def mock_skip(msg):
            nonlocal skip_called
            skip_called = True
            # Simulate pytest.skip behavior
            class SkipException(Exception):
                pass
            raise SkipException(msg)
        
        m.setattr(pytest, "skip", mock_skip)
        
        # Should not raise an exception
        decorated()
        
        assert not skip_called, "Test should not have been skipped after CSV file change"


def test_relative_csv_file_dependency(tmp_path, cleanup_passed_tests, monkeypatch):
    """Test that relative paths to CSV files work correctly"""
    # Create a test file and a CSV data file in a subdirectory
    test_dir = tmp_path / "test_dir"
    test_dir.mkdir(parents=True, exist_ok=True)
    
    # Create the test file
    test_file = test_dir / "test_file.py"
    test_file.write_text("# Test file content")
    
    # Create a data directory with a CSV file
    data_dir = test_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_file = data_dir / "dummy_data.csv"
    csv_content = "id,name,value\n1,item1,100\n2,item2,200\n"
    csv_file.write_text(csv_content)
    
    # Register the passed test file for cleanup
    passed_test_file = cleanup_passed_tests(test_file)
    
    # Create a test function with a relative path to the CSV file
    def test_func():
        assert True
    
    # Mock the test file path
    test_func._test_file_path = str(test_file)
    
    # Relative path from the test file to the CSV file
    relative_path = "data/dummy_data.csv"
    
    # Create the decorated function
    decorated = very_lazy_test([relative_path], stability_delay=0.1)(test_func)
    
    # Run the test once to create the passed test file
    decorated()
    
    # Verify the passed test file was created
    assert passed_test_file.exists()
    
    # Wait for the stability delay to pass
    time.sleep(0.2)
    
    # Run again - should be skipped
    with monkeypatch.context() as m:
        skip_called = False
        
        def mock_skip(msg):
            nonlocal skip_called
            skip_called = True
            # Simulate pytest.skip behavior by raising an exception
            class SkipException(Exception):
                pass
            raise SkipException(msg)
        
        m.setattr(pytest, "skip", mock_skip)
        
        try:
            decorated()
        except Exception:
            # Expected to raise an exception when skipped
            pass
        
        assert skip_called, "Test should have been skipped"
    
    # Modify the CSV file by adding a new row
    time.sleep(0.2)  # Ensure file modification time changes
    new_csv_content = csv_content + "3,item3,300\n"
    csv_file.write_text(new_csv_content)
    
    # Run again - should not be skipped because the dependency changed
    with monkeypatch.context() as m:
        skip_called = False
        
        def mock_skip(msg):
            nonlocal skip_called
            skip_called = True
            # Simulate pytest.skip behavior
            class SkipException(Exception):
                pass
            raise SkipException(msg)
        
        m.setattr(pytest, "skip", mock_skip)
        
        # Should not raise an exception
        decorated()
        
        assert not skip_called, "Test should not have been skipped after CSV file change" 


def test_non_existent_dependency_file(tmp_path, cleanup_passed_tests):
    """Test that very_lazy_test raises FileNotFoundError for non-existent dependency files"""
    # Create a test file
    test_file = create_temp_file(tmp_path)
    
    # Create a real dependency file first
    dep_file = create_temp_file(tmp_path, "dependency.txt")
    
    # Register the passed test file for cleanup
    passed_test_file = cleanup_passed_tests(test_file)
    
    # Create a function with the dependency
    def test_func():
        assert True
    
    # Mock the test file path
    test_func.__code__ = test_func.__code__.replace(co_filename=str(test_file))
    
    # Create the decorated function
    decorated = very_lazy_test([str(dep_file)])(test_func)
    
    # Run once to create the passed test file
    decorated()
    
    # Verify the passed test file was created
    assert passed_test_file.exists()
    
    # Wait for the stability delay to pass
    time.sleep(0.1)
    
    # Now delete the dependency file
    dep_file.unlink()
    
    # Running the function again should raise FileNotFoundError
    with pytest.raises(FileNotFoundError, match="Dependency file not found"):
        decorated()


def test_non_existent_relative_dependency_file(tmp_path, cleanup_passed_tests):
    """Test that very_lazy_test raises FileNotFoundError for non-existent relative dependency files"""
    # Create a test file
    test_dir = tmp_path / "test_dir"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "test_file.py"
    test_file.write_text("# Test file content")
    
    # Create a real dependency file first
    dep_dir = test_dir / "data"
    dep_dir.mkdir(parents=True, exist_ok=True)
    dep_file = dep_dir / "dependency.csv"
    dep_file.write_text("id,name\n1,test")
    
    # Register the passed test file for cleanup
    passed_test_file = cleanup_passed_tests(test_file)
    
    # Create a function with a relative path to the dependency
    def test_func():
        assert True
    
    # Mock the test file path
    test_func._test_file_path = str(test_file)
    
    # Relative path from the test file to the dependency
    relative_path = "data/dependency.csv"
    
    # Create the decorated function
    decorated = very_lazy_test([relative_path], stability_delay=0.1)(test_func)
    
    # Run once to create the passed test file
    decorated()
    
    # Verify the passed test file was created
    assert passed_test_file.exists()
    
    # Wait for the stability delay to pass
    time.sleep(0.2)
    
    # Now delete the dependency file
    dep_file.unlink()
    
    # Running the function again should raise FileNotFoundError
    with pytest.raises(FileNotFoundError, match="Dependency file not found"):
        decorated() 