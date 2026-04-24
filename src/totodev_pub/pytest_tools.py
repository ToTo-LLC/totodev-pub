# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Pytest tools for faster feedback loops and clear configuration loading.

This module contains three complementary capabilities designed to reduce friction
in day-to-day test authoring while preserving high test quality:

1) very_lazy_test — Decorator for selectively skipping already-passing tests
2) make_config_fixture — Factory for a fixture that loads project config explicitly
3) skipif_env_type — Decorator to skip tests for specific inferred environment types

Motivation
----------
Running long or expensive tests (time, network, or token cost) often causes
developers to delay running the full suite. The ``@very_lazy_test`` decorator
lets stable, previously passing tests run less frequently by default—without
manual intervention—while still re-running when relevant inputs change. This
keeps feedback loops fast for everyday development and preserves trust in the
suite via dependency checks, stability windows, and periodic re-verification.

Some projects use configuration discovery patterns that may be unfamiliar
to many developers. Determining the "right" way to pull config into automated
tests can be confusing. ``make_config_fixture`` provides a deliberate and
transparent approach to load project configuration only when a test asks for it,
without globally affecting import resolution for the rest of the test session.

Capabilities
------------
1) very_lazy_test (decorator)
   Skip re-running tests that already passed unless something relevant changed.
   A sidecar pass file tracks which tests passed; the test is re-run when:
   - The test module or any declared dependency has changed
   - The pass record is too new to be considered "stable"
   - The pass record has aged beyond a calendar-day reverify window
   - A random sampling trigger is hit (optional)

   Key behaviors:
   - Pass records are stored in "<test_file>.passed_tests.tmp"
   - Supports both sync and async tests; preserves pytest metadata
   - Dependencies can be absolute paths, relative paths (resolved relative to
     the test file), or import-style dotted module paths
   - On failure, the pass record for that test name is removed so it re-runs next time

   When to use:
   - Tests that are long, network-bound, or token-expensive
   - Tests that are stable but depend on a small set of modules/files
   - Suites where you want fast feedback locally while still re-checking on change

   Tuning knobs:
   - stability_delay (seconds): how old the pass-file must be before a skip is allowed
   - random_period: if > 0, run with probability 1/random_period even if unchanged
   - reverify_days: after N calendar days, force a re-run to guard against drift

   Basic examples:
       >>> import pytest
       >>> from totodev_pub.pytest_tools import very_lazy_test
       >>>
       >>> @very_lazy_test(["totodev_pub.pipes.pipe_stat"])  # import path dependency
       ... def test_ensure_absolute_path():
       ...     # expensive but stable test logic
       ...     assert True
       >>>
       >>> @pytest.mark.asyncio
       ... @very_lazy_test([
       ...     'totodev_pub.cached_file_folders',
       ...     'totodev_pub.cached_file_folders_support.file_proxy_dummy'
       ... ], reverify_days=14)  # force periodic re-check
       ... async def test_parallel_performance():
       ...     assert True

2) make_config_fixture (helper)
   Factory for a pytest fixture that loads application configuration for tests
   without mutating ``sys.path``. It locates ``src/config_apply.py`` relative to
   the calling test/conftest file and loads it by absolute path, returning the
   project's ``CONFIG`` object.

   Why this design:
   - Configuration in projects that use this library can be somewhat
     unconventional, and ad-hoc imports can be confusing. This fixture
     provides a single, obvious way to access project config in tests.
   - The import is explicit and selective: it only happens when a test uses the
     fixture, and it avoids global path changes that could accidentally shadow
     modules or change unrelated imports.

   Usage in tests/conftest.py:
       >>> from totodev_pub.pytest_tools import make_config_fixture
       >>>
       >>> # Create a fixture named 'config' with session scope (default)
       ... config = make_config_fixture()
       >>>
       >>> # In a test file:
       ... def test_uses_config(config):
       ...     assert config is not None

3) skipif_env_type (decorator)
   Skip tests entirely when the inferred environment type matches one of a
   provided set (e.g. DEV/TEST/STAGE/PROD), based on the nearest
   ``config._THIS_IS_*_ENV_.sh`` file above the test file.

   Simple example:
       >>> from totodev_pub.pytest_tools import skipif_env_type
       >>>
       >>> @skipif_env_type("PROD", "TEST")
       ... def test_only_for_dev_like_envs():
       ...     ...

"""

import os
import sys
import pytest
import random
import time
import inspect
import functools
import logging
from dataclasses import dataclass
from typing import List, Optional, Callable, Any, Union, Dict, Set
from pathlib import Path
from importlib import util
from datetime import datetime

from totodev_pub.app_config_root_class import AppConfigRootClass, EnvironmentType

# Setup logger
logger = logging.getLogger(__name__)

# Configuration constants
DEFAULT_STABILITY_DELAY = 180  # Default seconds before considering passed test file stable
VERY_LAZY_TEST_STABILITY_DELAY_SECS = 180  # Alias for backward compatibility
PASSED_TESTS_FILE_SUFFIX = ".passed_tests.tmp"
FORCE_REVERIFY_DAYS = 30 #DEFAULT to reverifying tests every n days

_VALID_ENV_TYPES: Set[str] = {e.value for e in EnvironmentType}
_ENV_TYPE_CACHE: Dict[str, str] = {}


def _get_env_type_for_search_start(search_start: Union[str, Path]) -> str:
    """Resolve and cache the environment type for a given starting path.

    This helper uses AppConfigRootClass.infer_environment_type() to determine
    the current EnvironmentType based on the nearest config._THIS_IS_*_ENV_.sh
    at or above the provided search_start path. Results are cached per
    normalized absolute directory path to avoid repeated filesystem scans.

    Args:
        search_start: File or directory path where the upward search should begin.

    Returns:
        Environment type string (e.g. \"DEV\", \"TEST\", \"STAGE\", \"PROD\").

    Raises:
        FileNotFoundError: If no config._THIS_IS_*_ENV_.sh file can be found
            starting at or above search_start.
        ValueError: If the discovered environment file does not encode a valid
            EnvironmentType according to AppConfigRootClass rules.
    """
    base_path = Path(search_start).resolve()
    # Always cache by directory; if a file path is passed, use its parent
    if base_path.is_file():
        base_path = base_path.parent

    key = str(base_path)
    if key in _ENV_TYPE_CACHE:
        return _ENV_TYPE_CACHE[key]

    env_type_enum = AppConfigRootClass.infer_environment_type(key)
    env_type = env_type_enum.value
    _ENV_TYPE_CACHE[key] = env_type
    return env_type


def skipif_env_type(*env_types: str, search_start: Optional[Union[str, Path]] = None) -> Callable:
    """Skip a test when the inferred environment type is in a given set.

    The environment type (DEV/TEST/STAGE/PROD) is inferred from the nearest
    ``config._THIS_IS_*_ENV_.sh`` file found above the test file (or an
    optional ``search_start`` path).

    Example
    -------
        >>> from totodev_pub.pytest_tools import skipif_env_type
        >>>
        >>> @skipif_env_type("PROD", "TEST")
        ... def test_never_runs_in_prod_or_test():
        ...     ...

    Args:
        *env_types: One or more environment type strings to skip on.
        search_start: Optional file or directory path used as the starting
            point for locating ``config._THIS_IS_*_ENV_.sh``.

    Notes
    -----
    The absence of a ``config._THIS_IS_*_ENV_.sh`` file is treated as an error
    when this decorator is used.
    """
    invalid = [et for et in env_types if et not in _VALID_ENV_TYPES]
    if invalid:
        valid_sorted = sorted(_VALID_ENV_TYPES)
        raise ValueError(
            f"Invalid environment type value(s) {invalid!r} passed to skipif_env_type; "
            f"valid values are: {valid_sorted!r}."
        )

    env_types_set: Set[str] = set(env_types)

    def decorator(test_func: Callable) -> Callable:
        # Determine starting directory for environment discovery
        if search_start is None:
            test_file = Path(test_func.__code__.co_filename)
            start_dir = test_file.parent
        else:
            start_dir = Path(search_start)

        env_type = _get_env_type_for_search_start(start_dir)

        if env_type in env_types_set:
            reason = (
                f'skipped because current inferred environment type ("{env_type}") '
                f"is in skip list {sorted(env_types_set)!r}"
            )
            return pytest.mark.skip(reason=reason)(test_func)

        return test_func

    return decorator

@dataclass
class TestConfig:
    """Configuration for test execution"""
    dependent_files: List[str]
    random_period: int = 0
    stability_delay: float = DEFAULT_STABILITY_DELAY
    
    def __post_init__(self) -> None:
        if self.random_period < 0:
            raise ValueError("random_period must be non-negative")

class TestState:
    """Manages test execution state and file handling"""
    def __init__(self, test_func: Callable, config: TestConfig):
        self.test_func = test_func
        self.config = config
        
        # Get the test file path from the function's code location
        self.test_file = Path(test_func.__code__.co_filename)
        
        # If this is a test function created for testing, use its custom file path
        if hasattr(test_func, '_test_file_path'):
            self.test_file = Path(test_func._test_file_path)
        
        self.passed_file = self.test_file.with_name(f"{self.test_file.name}{PASSED_TESTS_FILE_SUFFIX}")
        self.test_name = test_func.__name__
        
    def should_skip(self) -> bool:
        """Determine if test should be skipped"""
        passed_time = self._get_file_time(self.passed_file)
        current_time = time.time()
        
        if passed_time == 0:
            return False
            
        is_stable = (current_time - passed_time) >= self.config.stability_delay
        force_run = self.config.random_period > 0 and random.randint(1, self.config.random_period) == 1
        
        if not is_stable or force_run:
            return False
            
        if self._check_dependencies(passed_time):
            self.passed_file.unlink(missing_ok=True)
            return False
            
        return self.test_name in self.passed_file.read_text().splitlines()
    
    def record_success(self) -> None:
        """Record successful test execution"""
        self.passed_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Read existing content
        existing_tests = set()
        if self.passed_file.exists():
            existing_tests = set(self.passed_file.read_text().splitlines())
        
        # Add new test and write all tests
        existing_tests.add(self.test_name)
        self.passed_file.write_text('\n'.join(sorted(existing_tests)) + '\n')
    
    def handle_failure(self) -> None:
        """Handle test failure"""
        self.passed_file.unlink(missing_ok=True)
    
    def _get_file_time(self, path: Path) -> float:
        """Get file modification time or 0 if file doesn't exist"""
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0
    
    def _resolve_dependency(self, file_ref: str) -> Optional[Path]:
        """Resolve dependency reference to file path"""
        # Add initial debug info
        logger.debug(f"Starting resolution for {file_ref}")
        logger.debug(f"Current working directory: {os.getcwd()}")
        logger.debug(f"sys.path: {sys.path}")
        logger.debug(f"Test file location: {self.test_file}")

        # First try import path for dotted notation without slashes
        if '.' in file_ref and not any(c in file_ref for c in r'\/'):
            try:
                # Try to find the module
                logger.debug(f"Attempting to find_spec for {file_ref}")
                spec = util.find_spec(file_ref)
                logger.debug(f"find_spec result: {spec}")
                if spec:
                    logger.debug(f"spec.origin: {spec.origin}")
                    logger.debug(f"spec.submodule_search_locations: {spec.submodule_search_locations}")
                    if spec.origin:
                        resolved_path = Path(spec.origin)
                        if not resolved_path.exists():
                            raise FileNotFoundError(
                                f"Dependency file not found: '{file_ref}'\n"
                                f"Resolved path: '{resolved_path}'\n"
                                "This file was listed as a dependency in @very_lazy_test but does not exist.\n"
                                "Please check:\n"
                                "1. The file path or import path is correct\n"
                                "2. The file exists in the specified location\n"
                                "3. You have the correct permissions to access the file"
                            )
                        return resolved_path
                    elif spec.submodule_search_locations:
                        # For packages without a direct file (like __init__.py), use the package directory
                        pkg_path = Path(spec.submodule_search_locations[0])
                        if pkg_path.exists():
                            # Look for __init__.py in the package directory
                            init_file = pkg_path / "__init__.py"
                            if init_file.exists():
                                return init_file
                            # If no __init__.py, use the package directory itself
                            return pkg_path
                
                # If we get here, try to handle as a module path
                parts = file_ref.split('.')
                for i in range(len(parts), 0, -1):
                    partial_path = '.'.join(parts[:i])
                    spec = util.find_spec(partial_path)
                    if spec and spec.origin:
                        base_path = Path(spec.origin)
                        if base_path.exists():
                            # Found a valid module, now look for the remaining path
                            remaining_parts = parts[i:]
                            if remaining_parts:
                                # Convert remaining parts to a path
                                remaining_path = Path(*remaining_parts).with_suffix('.py')
                                full_path = base_path.parent / remaining_path
                                if full_path.exists():
                                    return full_path
                            else:
                                return base_path
                        
            except Exception as e:
                # Add debug logging to help diagnose import issues
                logger.debug(f"Import resolution failed for {file_ref}")
                logger.debug(f"Exception type: {type(e).__name__}")
                logger.debug(f"Exception message: {str(e)}")
                if isinstance(e, FileNotFoundError):
                    raise
                # Continue to file path check if import resolution fails
                pass

        # Then check if it's a file path
        path = Path(os.path.normpath(file_ref))
        if not path.is_absolute():
            # First try to resolve relative to the test file's directory
            test_dir = self.test_file.parent
            relative_path = test_dir / path
            if relative_path.exists():
                return relative_path
                
            # If that fails, try with sys.path as before
            for path_entry in sys.path:
                try_path = Path(path_entry) / file_ref.replace('.', '/') 
                if try_path.exists():
                    return try_path
                try_path = try_path.with_suffix('.py')
                if try_path.exists():
                    return try_path

            # If we still can't find it, raise a more helpful error
            raise FileNotFoundError(
                f"Dependency file not found: '{file_ref}'\n"
                f"Tried as relative path: '{relative_path}'\n"
                "This file was listed as a dependency in @very_lazy_test but does not exist.\n"
                "Please check:\n"
                "1. The file path is correct\n"
                "2. The file exists in the specified location\n"
                "3. You have the correct permissions to access the file"
            )
        
        if not path.exists():
            raise FileNotFoundError(
                f"Dependency file not found: '{file_ref}'\n"
                f"Absolute path: '{path}'\n"
                "This file was listed as a dependency in @very_lazy_test but does not exist.\n"
                "Please check:\n"
                "1. The file path is correct\n"
                "2. The file exists in the specified location\n"
                "3. You have the correct permissions to access the file"
            )
        
        return path
    
    def _check_dependencies(self, marker_time: float) -> bool:
        """Check if any dependencies have changed"""
        all_deps = self.config.dependent_files + [str(self.test_file)]
        return any(
            self._get_file_time(dep) > marker_time
            for dep in map(self._resolve_dependency, all_deps)
            if dep is not None
        )

async def _run_async_test(state: TestState, args: tuple, kwargs: dict) -> Any:
    """Run async test with proper error handling"""
    try:
        result = await state.test_func(*args, **kwargs)
        state.record_success()
        return result
    except Exception:
        state.handle_failure()
        raise

def _run_sync_test(state: TestState, args: tuple, kwargs: dict) -> Any:
    """Run sync test with proper error handling"""
    try:
        result = state.test_func(*args, **kwargs)
        state.record_success()
        return result
    except Exception:
        state.handle_failure()
        raise

def very_lazy_test(dependencies: List[str], stability_delay: float = 0, random_period: float = 0, reverify_days: int = FORCE_REVERIFY_DAYS):
    """
    Decorator for test functions that should only run when either the test or its dependencies have changed.
    
    Tests that have passed are recorded in a .passed_tests.tmp file and will be skipped if:
    1. The passed test file exists and is stable (older than stability_delay)
    2. No dependencies have changed
    3. The test name is in the passed test file
    4. Not randomly selected to run
    5. The passed test file is not older than reverify_days calendar days
    
    Args:
        dependent_files: List of files that might affect test results. Files can be:
            - Absolute paths
            - Relative paths (resolved relative to the test file's directory)
            - Import path style (using dot notation)
        random_period: If > 0, represents a 1/random_period chance of running
        stability_delay: Number of seconds to wait before considering passed test file stable
        reverify_days: Number of calendar days after which to force re-verification. Set to 0 to disable.
        
    Returns:
        Decorated test function that implements lazy testing behavior
    """
    # Add path validation at the start of the decorator
    for dep in dependencies:
        # Skip validation for import paths (containing dots but no slashes/backslashes)
        if '.' in dep and not any(c in dep for c in r'\/'):
            continue
            
        # For file paths, we now allow relative paths (they'll be resolved relative to the test file)
        # No validation needed here, as resolution happens at runtime in _resolve_dependency

    config = TestConfig(dependencies, random_period, stability_delay)
    
    def should_delete_passed_tests(passed_tests_file: Path) -> bool:
        """Check if passed tests file should be deleted based on age"""
        if not reverify_days:
            return False
            
        if not passed_tests_file.exists():
            return False
            
        file_mtime = passed_tests_file.stat().st_mtime
        current_time = time.time()
        
        # Convert timestamps to calendar days
        file_days = file_mtime // (24 * 3600)
        current_days = current_time // (24 * 3600)
        
        return (current_days - file_days) > reverify_days

    def decorator(test_func: Callable) -> Callable:
        state = TestState(test_func, config)
        is_async = inspect.iscoroutinefunction(test_func)
        
        if is_async:
            async def check_and_run(*args, **kwargs):
                passed_tests_file = state.passed_file
                
                # Check if file needs deletion due to age
                if should_delete_passed_tests(passed_tests_file):
                    try:
                        passed_tests_file.unlink()
                    except FileNotFoundError:
                        pass
                
                if state.should_skip():
                    pytest.skip(f"very_lazy_test triggered to skip {state.test_name} - no changes detected")
                return await _run_async_test(state, args, kwargs)
        else:
            def check_and_run(*args, **kwargs):
                passed_tests_file = state.passed_file
                
                # Check if file needs deletion due to age
                if should_delete_passed_tests(passed_tests_file):
                    try:
                        passed_tests_file.unlink()
                    except FileNotFoundError:
                        pass
                
                if state.should_skip():
                    pytest.skip(f"very_lazy_test triggered to skip {state.test_name} - no changes detected")
                return _run_sync_test(state, args, kwargs)

        # Preserve test function metadata
        result = functools.wraps(test_func)(check_and_run)
        result.__signature__ = inspect.signature(test_func)
        result.__pytest_wrapped__ = getattr(test_func, '__pytest_wrapped__', test_func)
        result.is_async = is_async
        
        # Preserve custom file path if it exists
        if hasattr(test_func, '_test_file_path'):
            result._test_file_path = test_func._test_file_path
            
        return result

    return decorator

def _get_last_modified(file_path: Path) -> float:
    """Get last modification time of a file if it exists, or 0 if file doesn't exist."""
    try:
        return os.path.getmtime(file_path)
    except OSError:
        return 0.0

# Public alias for backward compatibility
get_last_modified = _get_last_modified


def make_config_fixture(scope: str = "session") -> Callable:
    """
    Factory function to create a pytest fixture for loading project configuration.
    
    This helper simplifies the creation of a config fixture in downstream projects
    that use the totodev_pub configuration system. It automatically discovers the src/
    directory and imports the CONFIG object from config_apply.py, triggering any
    application initialization logic.
    
    Args:
        scope: Pytest fixture scope. Defaults to "session" to load config once per
               test session. Other options: "module", "class", "function"
    
    Returns:
        A pytest fixture function that provides the CONFIG object to tests
    
    Example usage in tests/conftest.py:
        ```python
        from totodev_pub.pytest_tools import make_config_fixture
        
        # Create the fixture - name it 'config' for use in tests
        config = make_config_fixture()
        ```
    
    Then use in tests:
        ```python
        def test_something(config):
            assert config["API_KEY"] is not None
        ```
    
    Notes:
        - Assumes standard project structure with src/ directory at project root
        - Looks for config_apply.py in src/ which should contain CONFIG object
        - The fixture will fail if config_apply.py cannot be imported
        - For custom search paths, manually create a fixture instead
    """
    @pytest.fixture(scope=scope)
    def _config():
        """Load project configuration for tests without mutating sys.path."""
        from pathlib import Path

        # Auto-detect src/ directory and the config_apply.py file relative to the caller
        test_file = Path(inspect.currentframe().f_back.f_back.f_code.co_filename)
        src_path = test_file.parent.parent / "src"
        config_apply_path = src_path / "config_apply.py"

        if not config_apply_path.exists():
            raise ImportError(
                f"Failed to locate config_apply.py at expected path: {config_apply_path}\n"
                f"Make sure:\n"
                f"1. config_apply.py exists in {src_path}\n"
                f"2. config_apply.py contains: CONFIG = AppConfigRootClass.dynaload(__file__)\n"
                f"3. You have a config._THIS_IS_*_ENV_.sh file at project root"
            )

        # Load the module directly from its absolute path
        spec = util.spec_from_file_location("_td_config_apply", str(config_apply_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for: {config_apply_path}")

        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]

        try:
            CONFIG = module.CONFIG  # noqa: N806 - external constant by convention
        except AttributeError as e:
            raise ImportError(
                "CONFIG not found in config_apply.py. Ensure it defines:\n"
                "CONFIG = AppConfigRootClass.dynaload(__file__)"
            ) from e

        return CONFIG
    
    return _config 