# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
from typing import Dict, Any, Tuple
import pytest
import yaml
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import tempfile
import shutil
from pathlib import Path
from totodev_pub.app_config_root_class import AppConfigRootClass, _AppConfigCacheKey, EnvironmentType, ConfigurationError, OS_ENVIRONMENT_SENTINEL

# The TEST_SCENARIO_DIR is just beneath the directory of this file and name of directory is app_config_test_scenarios

# Calculate the full path to the directory containing the test scenarios
TEST_SCENARIO_DIR = os.path.join(os.path.dirname(__file__), 'app_config_test_scenarios')
 
CASE_SUBDIR_NAMES = ['case1', # has config, env file is above
                     'case2', # has config and env file
                     'case3' # has no config, env file is above
                    ]

# create dict mapping the subdir names to the full path of the subdir
CASE_SUBDIRS = {case_subdir_name: os.path.join(TEST_SCENARIO_DIR, case_subdir_name) for case_subdir_name in CASE_SUBDIR_NAMES}  



def load_case_and_valid_pk(case_num:int, require_config_file:bool) -> Tuple[AppConfigRootClass,Dict[str,Any]]:
    """
    Given a case number, load the AppConfigRootClass instance and the expected valid primary key
       - This effectively triggers loading of the following two files:
         - config.py in the case subdir
         - config._THIS_IS_<env>_ENV_.sh in the case subdir
    """
    case_subdir_name = f'case{case_num}'
    case_subdir = CASE_SUBDIRS[case_subdir_name]
    
    # Load the AppConfigRootClass instance
    app_config = AppConfigRootClass.dynaload(case_subdir,require_config_file=require_config_file)

    # load the dict from the YAML file named valid_pi.yaml within the case subdir
    valid_pk_file = os.path.join(case_subdir, 'valid_pk.yaml')
    with open(valid_pk_file) as f:
        valid_pk = yaml.safe_load(f)

    return app_config, valid_pk

@pytest.fixture
def case1_info():
    return load_case_and_valid_pk(1, require_config_file=True)

@pytest.fixture
def case2_info():
    return load_case_and_valid_pk(2, require_config_file=True)

@pytest.fixture
def case3_info():
    return load_case_and_valid_pk(3, require_config_file=False)


@pytest.mark.skip(reason="Case fixture directories are not fully populated in this embedded project copy; skipping integration-style scenario test.")
def test_cases(case1_info, case2_info, case3_info):
    """Test basic loading scenarios from test data directories."""
    config1, valid_pk1 = case1_info
    config2, valid_pk2 = case2_info
    config3, valid_pk3 = case3_info
    
    # Verify each config has expected keys
    for key, expected_value in valid_pk1.items():
        assert config1[key] == expected_value, f"Case1: {key} mismatch"
    
    for key, expected_value in valid_pk2.items():
        assert config2[key] == expected_value, f"Case2: {key} mismatch"
    
    for key, expected_value in valid_pk3.items():
        assert config3[key] == expected_value, f"Case3: {key} mismatch"


def test_file_tracking(tmp_path):
    """Test that file modification tracking works correctly."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    env_file.write_text("TEST_VAR=test_value")
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    pass
""")
    
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Initially, no files should be updated
    assert config.files_were_updated() == []
    
    # Modify environment file
    import time
    time.sleep(0.1)  # Ensure mtime changes
    env_file.write_text("TEST_VAR=modified_value")
    
    # Should detect change
    updated = config.files_were_updated()
    assert len(updated) > 0
    assert any("config._THIS_IS_DEVTEST_ENV_.sh" in f for f in updated)


def test_optional_file_handling(tmp_path):
    """Test handling of optional config file."""
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    env_file.write_text("TEST_VAR=test_value")
    
    # Should work without config.py when require_config_file=False
    config = AppConfigRootClass.dynaload(str(tmp_path), require_config_file=False)
    assert config["TEST_VAR"] == "test_value"


def test_config_inheritance(tmp_path):
    """Test that configuration values are properly inherited through the class hierarchy."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    # Create environment file
    env_file.write_text("""
TEST_ENV_VAR=env_value
OVERRIDE_VAR=env_value
""")

    # Create config file with inheritance hierarchy
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class BaseConfig(AppConfigRootClass):
    BASE_VAR = "base_value"
    OVERRIDE_VAR = "base_value"
    BASE_PKD = "|key1=val1|key2=val2|"

    def CALCULATED_BASE(self):
        return "calc_base"

class AppConfigDEVTEST(BaseConfig):
    CHILD_VAR = "child_value"
    OVERRIDE_VAR = "child_value"

    def CALCULATED_CHILD(self):
        return f"{self['CALCULATED_BASE']}_child"
""")

    # Load config
    app_config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Test inheritance
    assert app_config["BASE_VAR"] == "base_value"
    assert app_config["CHILD_VAR"] == "child_value"
    assert app_config["OVERRIDE_VAR"] == "env_value"  # Env overrides child
    assert app_config["TEST_ENV_VAR"] == "env_value"  # From environment file
    
    # Test calculated values
    assert app_config["CALCULATED_BASE"] == "calc_base"
    assert app_config["CALCULATED_CHILD"] == "calc_base_child"
    
    # Test PKD parsing
    assert app_config["BASE_PKD"] == {"key1": "val1", "key2": "val2"}


def test_getitem_and_getattr_behavior(tmp_path):
    """Test the behavior of __getitem__ and __getattr__ methods."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    # Create environment file with test values
    env_file.write_text("""
ENV_VAR=env_value
SHARED_VAR=env_value
""")

    # Create config file with test values
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    CLASS_VAR = "class_value"
    SHARED_VAR = "class_value"  # Will be overridden by env var

    def CALCULATED_VAR(self):
        return "calculated_value"

    def DEPENDENT_VAR(self):
        return f"{self['CLASS_VAR']}_dependent"
""")

    # Load config
    app_config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Test dictionary access (always works)
    assert app_config["ENV_VAR"] == "env_value"
    assert app_config["CLASS_VAR"] == "class_value"
    assert app_config["SHARED_VAR"] == "env_value"  # Env overrides class
    assert app_config["CALCULATED_VAR"] == "calculated_value"
    assert app_config["DEPENDENT_VAR"] == "class_value_dependent"
    
    # Test attribute access (only for non-method values)
    assert app_config.CLASS_VAR == "class_value"
    assert app_config.SHARED_VAR == "env_value"
    
    # Attribute access should fail for methods
    with pytest.raises(AttributeError):
        _ = app_config.CALCULATED_VAR


def test_dig_functionality(tmp_path):
    """Test the Ruby-style dig implementation for nested data access."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    # Create config file with test structures
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    SIMPLE_VALUE = "test_value"
    NESTED_DICT = {
        "level1": {
            "level2": {
                "value": "nested_value"
            }
        }
    }
    MIXED_STRUCTURE = {
        "list": [1, 2, {"key": "value"}, [4, 5, 6]],
        "dict": {
            "nested": ["a", "b", {"deep": "found_it"}]
        }
    }
    LIST_OF_DICTS = [
        {"name": "first"},
        {"name": "second"},
        {"name": "third"}
    ]

    def __init__(self):
        self.deduce_cache_key(__file__)
""")

    # Create minimal environment file
    env_file.write_text("TEST_ENV_VAR=test_value")

    # Load config
    app_config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Test simple value
    assert app_config.dig("SIMPLE_VALUE") == "test_value"
    
    # Test nested dict access
    assert app_config.dig("NESTED_DICT", "level1", "level2", "value") == "nested_value"
    
    # Test missing keys return None
    assert app_config.dig("NESTED_DICT", "level1", "missing") is None
    assert app_config.dig("MISSING_KEY") is None
    
    # Test list access
    assert app_config.dig("LIST_OF_DICTS", 0, "name") == "first"
    assert app_config.dig("LIST_OF_DICTS", 1, "name") == "second"
    assert app_config.dig("LIST_OF_DICTS", 99, "name") is None  # Out of bounds
    
    # Test mixed structures
    assert app_config.dig("MIXED_STRUCTURE", "list", 0) == 1
    assert app_config.dig("MIXED_STRUCTURE", "list", 2, "key") == "value"
    assert app_config.dig("MIXED_STRUCTURE", "list", 3, 1) == 5
    assert app_config.dig("MIXED_STRUCTURE", "dict", "nested", 2, "deep") == "found_it"


def test_cache_behavior(tmp_path):
    """Test the caching behavior of configuration values."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    env_file.write_text("TEST_VAR=test_value")

    # Create config with a calculated value that counts accesses
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    _counter = 0

    def CALCULATED_VALUE(self):
        self.__class__._counter += 1
        return f"value_{self._counter}"
""")

    # Create two instances with same cache key
    config1 = AppConfigRootClass.dynaload(str(tmp_path))
    config2 = AppConfigRootClass.dynaload(str(tmp_path))
    
    # First access should trigger calculation
    val1_first = config1["CALCULATED_VALUE"]
    val2_first = config2["CALCULATED_VALUE"]
    
    # Both should return same cached value
    assert val1_first == val2_first, "Second instance should use cached value"
    
    # Multiple accesses should return same value (cached)
    val1_second = config1["CALCULATED_VALUE"]
    assert val1_first == val1_second, "Second access should return cached value"


@pytest.mark.parametrize("scenario", [
    ("invalid_env", "config._THIS_IS_INVALID_ENV_.sh", "Invalid environment type"),
    ("missing_env", "config._THIS_IS_DEVTEST_ENV_.sh", "FileNotFoundError"),
    ("malformed_config", "config.py", "SyntaxError"),
])
def test_error_cases(tmp_path, scenario):
    """Test various error conditions and edge cases."""
    name, file_path, expected_error = scenario

    if name == "invalid_env":
        env_file = tmp_path / file_path
        config_file = tmp_path / "config.py"

        env_file.write_text("TEST_VAR=test_value")
        config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigINVALID(AppConfigRootClass):
    pass
""")

        with pytest.raises(ValueError, match=".*valid EnvironmentType.*"):
            AppConfigRootClass.dynaload(str(tmp_path))

    elif name == "missing_env":
        # No env file created
        with pytest.raises(FileNotFoundError):
            AppConfigRootClass.dynaload(str(tmp_path))

    elif name == "malformed_config":
        env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
        config_file = tmp_path / file_path
        
        env_file.write_text("TEST_VAR=test_value")
        config_file.write_text("invalid python syntax !!!")
        
        with pytest.raises(SyntaxError):
            AppConfigRootClass.dynaload(str(tmp_path))


def test_environment_variables(tmp_path):
    """Test interaction with environment variables."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    # Test DO_NOT_AUTOPORT and environment variable handling
    env_file.write_text("""
DO_NOT_AUTOPORT=SKIP_VAR,_PRIVATE_VAR
NORMAL_VAR=normal_value
SKIP_VAR=skip_value
_PRIvate_VAR=private_value
CamelCaseVar=camel_value
""")

    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    pass
""")

    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Normal vars should be accessible
    assert config["NORMAL_VAR"] == "normal_value"
    assert config["CAMEL_CASE_VAR"] == "camel_value"  # Normalized
    
    # Excluded vars should not be accessible
    assert "SKIP_VAR" not in config
    assert "_PRIVATE_VAR" not in config


def test_env_sourced_keys(tmp_path):
    """Test that env_sourced_keys correctly maps normalized keys to original environment variable names."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    # Create environment file with various variable formats
    env_file.write_text("""
DO_NOT_AUTOPORT=SKIP_VAR,_PRIVATE_VAR
NORMAL_VAR=normal_value
CamelCaseVar=camel_value
mixedCase=mixed_value
UPPER_CASE=upper_value
SKIP_VAR=skip_value
_PRIvate_VAR=private_value
""")

    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    # Add a class-defined variable to ensure it's not included in env_sourced_keys
    CLASS_VAR = "class_value"
""")

    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Get mapping
    mapping = config.env_sourced_keys()
    
    # Should map normalized keys to original names
    assert mapping["NORMAL_VAR"] == "NORMAL_VAR"
    assert mapping["CAMEL_CASE_VAR"] == "CamelCaseVar"
    assert mapping["MIXED_CASE"] == "mixedCase"
    assert mapping["UPPER_CASE"] == "UPPER_CASE"
    
    # Excluded vars should not be in mapping
    assert "SKIP_VAR" not in mapping
    assert "_PRIVATE_VAR" not in mapping
    
    # Class-defined vars should not be in mapping
    assert "CLASS_VAR" not in mapping


def test_dynamic_config_methods(tmp_path):
    """Test configuration values that are computed by methods.

    Note: The AppConfigRootClass eagerly loads and caches all method values during initialization.
    This means any method marked for caching (all-caps methods) will be called during object creation.
    """
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    env_file.write_text("BASE_VALUE=env_base")

    # Create config with method chain
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    def VALUE_A(self):
        return self['BASE_VALUE'] + "_a"

    def VALUE_B(self):
        return self['VALUE_A'] + "_b"

    def value_error(self):  # lowercase, so not eagerly loaded
        raise ValueError("Test error")

    def get_value_error(self):  # helper method to trigger error
        return self.value_error()
""")

    print("[DIAG] About to load config")
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Test method chain
    assert config["BASE_VALUE"] == "env_base"
    assert config["VALUE_A"] == "env_base_a"
    assert config["VALUE_B"] == "env_base_a_b"
    
    # Test that lowercase methods are not automatically called
    assert hasattr(config, 'value_error')
    # But can be called manually if needed
    with pytest.raises(ValueError, match="Test error"):
        config.get_value_error()


def test_concurrent_access(tmp_path):
    """Test concurrent access to configuration values."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    env_file.write_text("TEST_VAR=test_value")

    # Create config with a slow calculated value
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass
import time

class AppConfigDEVTEST(AppConfigRootClass):
    _counter = 0

    def SLOW_VALUE(self):
        time.sleep(0.1)  # Simulate slow calculation
        self.__class__._counter += 1
        return f"value_{self._counter}"
""")

    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Access from multiple threads
    results = []
    def access_config():
        results.append(config["SLOW_VALUE"])
    
    threads = [threading.Thread(target=access_config) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # All should get the same cached value
    assert len(set(results)) == 1, "All threads should get same cached value"


def test_performance(tmp_path):
    """Test performance characteristics of configuration access."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    # Create large environment file
    env_vars = "\n".join(f"VAR_{i}=value_{i}" for i in range(1000))
    env_file.write_text(env_vars)

    # Create config with many calculated values
    config_code = """
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
"""
    for i in range(100):
        config_code += f"""
    def CALC_{i}(self):
        return "calc_{i}"
"""
    config_file.write_text(config_code)

    # Test initialization time
    start_time = time.time()
    config = AppConfigRootClass.dynaload(str(tmp_path))
    init_time = time.time() - start_time
    
    # Should complete in reasonable time (less than 5 seconds for this test)
    assert init_time < 5.0, f"Initialization took too long: {init_time}s"
    
    # Test access time (should be fast due to caching)
    start_time = time.time()
    for i in range(100):
        _ = config[f"CALC_{i}"]
    access_time = time.time() - start_time
    
    # Access should be very fast (cached values)
    assert access_time < 1.0, f"Access took too long: {access_time}s"


def test_pkd_value_handling(tmp_path):
    """Test parsing of packed key-value strings (_PKD suffix)."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    env_file.write_text("""
FEATURES_PKD=|feature1=enabled|feature2=disabled|feature3=enabled|
ITEMS_PKD=~item1~item2~item3~
EMPTY_PKD=||
""")

    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    CLASS_FEATURES_PKD = "|class1=val1|class2=val2|"
    CLASS_ITEMS_PKD = "~a~b~c~"
""")

    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Test dict-style PKD from environment
    features = config["FEATURES_PKD"]
    assert isinstance(features, dict)
    assert features["feature1"] == "enabled"
    assert features["feature2"] == "disabled"
    assert features["feature3"] == "enabled"
    
    # Test list-style PKD from environment
    items = config["ITEMS_PKD"]
    assert isinstance(items, list)
    assert items == ["item1", "item2", "item3"]
    
    # Test empty PKD
    empty = config["EMPTY_PKD"]
    assert empty == {}
    
    # Test class-defined PKD
    class_features = config["CLASS_FEATURES_PKD"]
    assert isinstance(class_features, dict)
    assert class_features["class1"] == "val1"
    
    class_items = config["CLASS_ITEMS_PKD"]
    assert isinstance(class_items, list)
    assert class_items == ["a", "b", "c"]


def test_security(tmp_path):
    """Test security-related behavior (read-only config, no code execution from env vars)."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    env_file.write_text("SECRET_VAR=secret_value")

    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    TEST_VAR = "test"
""")

    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Test that uppercase keys cannot be modified
    with pytest.raises(AttributeError):
        config["TEST_VAR"] = "modified"
    
    with pytest.raises(AttributeError):
        config["SECRET_VAR"] = "modified"
    
    # Test that config values remain unchanged
    assert config["TEST_VAR"] == "test"
    assert config["SECRET_VAR"] == "secret_value"


def test_resource_management(tmp_path):
    """Test resource management (file handles, memory)."""
    # Create test files
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"

    env_file.write_text("TEST_VAR=test_value")

    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    pass
""")

    # Create multiple instances
    configs = [AppConfigRootClass.dynaload(str(tmp_path)) for _ in range(10)]
    
    # All should work correctly
    for config in configs:
        assert config["TEST_VAR"] == "test_value"
    
    # Verify they share cache (same cache key)
    cache_keys = [c.cache_key for c in configs]
    assert len(set(cache_keys)) == 1, "All instances should share same cache key"


def test_integration(tmp_path):
    """Test integration with common usage patterns."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    
    # Create typical project structure
    (project_root / "src").mkdir()
    (project_root / "tests").mkdir()
    (project_root / "config").mkdir()
    
    # Create configuration files
    env_file = project_root / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = project_root / "config.py"  # Move config.py to root
    pyproject_file = project_root / "pyproject.toml"
    
    print(f"[DIAG] Project structure:")
    print(f"  project_root: {project_root}")
    print(f"  env_file: {env_file} (exists: {env_file.exists()})")
    print(f"  config_file: {config_file} (exists: {config_file.exists()})")
    print(f"  Directory contents: {list(project_root.glob('**/*'))}")
    
    env_file.write_text("""
APP_NAME=TestApp
APP_VERSION=1.0.0
DATABASE_URL=sqlite:///test.db
""")

    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    def DATABASE_CONFIG(self):
        return {
            'url': self['DATABASE_URL'],
            'echo': True
        }
""")

    pyproject_file.write_text("""
[tool.poetry]
name = "testapp"
version = "1.0.0"

[tool.testapp]
config_version = "1"
""")

    # Test loading from different directories
    try:
        config_from_root = AppConfigRootClass.dynaload(str(project_root))
        
        assert config_from_root["APP_NAME"] == "TestApp"
        assert config_from_root["APP_VERSION"] == "1.0.0"
        assert config_from_root["DATABASE_URL"] == "sqlite:///test.db"
        
        db_config = config_from_root["DATABASE_CONFIG"]
        assert db_config["url"] == "sqlite:///test.db"
        assert db_config["echo"] is True
        
        # Test pyproject.toml access
        project_data = config_from_root.dig("PYPROJECT_TOML", "tool", "poetry", "name")
        assert project_data == "testapp"
        
    except Exception as e:
        print(f"[DIAG] Error loading from root: {e}")
        raise


def test_multipath_search_basic(tmp_path):
    """Test basic multi-path search: env in path[0], config in path[1]"""
    # Create structure where env and config are in different locations
    env_dir = tmp_path / "envs" / "TESTENV"
    config_dir = tmp_path / "configs"
    env_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    
    # Create environment file
    env_file = env_dir / "config._THIS_IS_TESTENV_ENV_.sh"
    env_file.write_text("TEST_VAR=from_env")
    
    # Create config file
    config_file = config_dir / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigTESTENV(AppConfigRootClass):
    CONFIG_VAR = "from_config"
""")
    
    # Load using multi-path search
    config = AppConfigRootClass.dynaload([str(env_dir), str(config_dir)])
    
    # Verify values from both sources
    assert config["TEST_VAR"] == "from_env"
    assert config["CONFIG_VAR"] == "from_config"


def test_multipath_search_with_fallback(tmp_path):
    """Test that fallback to second path works when first path doesn't have config.py"""
    # Create structure with env in first location, config in second
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    
    # First directory has env file only
    env_file = first_dir / "config._THIS_IS_TESTFALLBACK_ENV_.sh"
    env_file.write_text("FALLBACK_VAR=fallback_value")
    
    # Second directory has config file only
    config_file = second_dir / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigTESTFALLBACK(AppConfigRootClass):
    SECOND_DIR_VAR = "from_second_dir"
""")
    
    # Load with fallback path
    config = AppConfigRootClass.dynaload([str(first_dir), str(second_dir)])
    
    # Verify both values are accessible
    assert config["FALLBACK_VAR"] == "fallback_value"
    assert config["SECOND_DIR_VAR"] == "from_second_dir"


def test_multipath_search_same_location(tmp_path):
    """Test that both files in same location still works with multi-path"""
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    
    # Both files in same location
    env_file = test_dir / "config._THIS_IS_TESTSAME_ENV_.sh"
    env_file.write_text("SAME_VAR=same_value")
    
    config_file = test_dir / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigTESTSAME(AppConfigRootClass):
    SAME_CONFIG = "same_config_value"
""")
    
    # Load using single path (backward compatibility)
    config = AppConfigRootClass.dynaload(str(test_dir))
    
    assert config["SAME_VAR"] == "same_value"
    assert config["SAME_CONFIG"] == "same_config_value"


def test_multipath_pyproject_search(tmp_path):
    """Test that pyproject.toml is found via upward search from env file location"""
    # Create nested structure
    project_root = tmp_path / "project"
    tests_dir = project_root / "tests" / "envs" / "TESTPROJ"
    tests_dir.mkdir(parents=True)
    
    # pyproject.toml at project root
    pyproject_file = project_root / "pyproject.toml"
    pyproject_file.write_text("""
[project]
name = "test-project"
version = "1.0.0"
""")
    
    # Environment file deep in subdirectory
    env_file = tests_dir / "config._THIS_IS_TESTPROJ_ENV_.sh"
    env_file.write_text("PROJ_VAR=proj_value")
    
    # Config file at project root
    config_file = project_root / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigTESTPROJ(AppConfigRootClass):
    pass
""")
    
    # Load config
    config = AppConfigRootClass.dynaload([str(tests_dir), str(project_root)])
    
    # Verify pyproject.toml was found and loaded
    project_name = config.dig("PYPROJECT_TOML", "project", "name")
    assert project_name == "test-project"


def test_pytest_pattern_integration():
    """Test the full pytest pattern using the app_config_pytest_scenarios structure"""
    # Get path to test scenarios
    tests_dir = Path(__file__).parent
    pytest_scenarios_dir = tests_dir / "app_config_pytest_scenarios"
    
    # Load TESTUNIT config
    unit_config = AppConfigRootClass.dynaload([
        str(pytest_scenarios_dir / "envconfigs" / "TESTUNIT"),
        str(pytest_scenarios_dir)
    ])
    
    # Verify it loaded correctly
    assert unit_config["ENV_LABEL"] == "TESTUNIT"
    # Check for any of the test variables from the env file
    assert "TEST_MODE" in unit_config or "SIMPLE_VALUE" in unit_config or "LOG_LEVEL" in unit_config
    
    # Load TESTINTEGRATION config
    integration_config = AppConfigRootClass.dynaload([
        str(pytest_scenarios_dir / "envconfigs" / "TESTINTEGRATION"),
        str(pytest_scenarios_dir)
    ])
    
    # Verify it loaded correctly
    assert integration_config["ENV_LABEL"] == "TESTINTEGRATION"


def test_backward_compatibility(tmp_path):
    """Ensure single string path still works (backward compatibility)"""
    test_dir = tmp_path / "backward"
    test_dir.mkdir()
    
    env_file = test_dir / "config._THIS_IS_TESTBACK_ENV_.sh"
    env_file.write_text("BACK_VAR=backward_value")
    
    config_file = test_dir / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigTESTBACK(AppConfigRootClass):
    BACK_CONFIG = "backward_config"
""")
    
    # Old single-string API should still work
    config = AppConfigRootClass.dynaload(str(test_dir))
    
    assert config["BACK_VAR"] == "backward_value"
    assert config["BACK_CONFIG"] == "backward_config"


def test_multipath_error_no_env_file(tmp_path):
    """Test that proper error is raised when no env file found in any path"""
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()
    
    # No env files in either directory
    with pytest.raises(FileNotFoundError) as excinfo:
        AppConfigRootClass.dynaload([str(dir1), str(dir2)])
    
    assert "No confidential environment file" in str(excinfo.value)


def test_multipath_error_no_config_file(tmp_path):
    """Test that proper error is raised when no config file found and required"""
    env_dir = tmp_path / "envs" / "TESTERR"
    env_dir.mkdir(parents=True)
    
    env_file = env_dir / "config._THIS_IS_TESTERR_ENV_.sh"
    env_file.write_text("TEST_VAR=test_value")
    
    # No config file, require_config_file=True (default)
    with pytest.raises(FileNotFoundError) as excinfo:
        AppConfigRootClass.dynaload(str(env_dir), require_config_file=True)
    
    assert "No config file" in str(excinfo.value)


def test_multipath_optional_config_file(tmp_path):
    """Test that missing config file is OK when require_config_file=False"""
    test_dir = tmp_path / "optional"
    test_dir.mkdir()
    
    # Create env file but no config file
    env_file = test_dir / "config._THIS_IS_TESTOPT_ENV_.sh"
    env_file.write_text("OPT_VAR=optional_value")
    
    # Should work with require_config_file=False
    config = AppConfigRootClass.dynaload(str(test_dir), require_config_file=False)
    
    assert config["OPT_VAR"] == "optional_value"
    assert config["ENV_LABEL"] == "TESTOPT"


# ============================================================================
# OS Mode Tests
# ============================================================================

def test_os_mode_happy_path(tmp_path, monkeypatch):
    """Test OS mode happy path - all required variables set correctly."""
    # Create config.py with EXPECTED_KEYS
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["API_KEY", "DATABASE_URL", "SECRET_KEY"]
    DEBUG_MODE = False
""")
    
    # Set OS environment variables
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("API_KEY", "prod_key_12345")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod-server/db")
    monkeypatch.setenv("SECRET_KEY", "prod_secret_abc")
    
    # Load config (no env file present)
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify OS mode
    assert config["ENV_SOURCE"] == "OS"
    assert config["ENV_LABEL"] == "PROD01"
    assert config["ENV_FPATH"] == OS_ENVIRONMENT_SENTINEL
    
    # Verify all EXPECTED_KEYS are accessible
    assert config["API_KEY"] == "prod_key_12345"
    assert config["DATABASE_URL"] == "postgresql://prod-server/db"
    assert config["SECRET_KEY"] == "prod_secret_abc"
    
    # Verify class attributes still work
    assert config["DEBUG_MODE"] is False


def test_os_mode_missing_env_label(tmp_path, monkeypatch):
    """Test OS mode error when ENV_LABEL is not set."""
    # Ensure ENV_LABEL is not set
    monkeypatch.delenv("ENV_LABEL", raising=False)
    
    # No env file, no ENV_LABEL
    with pytest.raises(FileNotFoundError) as excinfo:
        AppConfigRootClass.dynaload(str(tmp_path))
    
    error_msg = str(excinfo.value)
    assert "No confidential environment file" in error_msg
    assert "ENV_LABEL" in error_msg
    assert "Dokploy" in error_msg or "deployment tool" in error_msg


def test_os_mode_invalid_env_label(tmp_path, monkeypatch):
    """Test OS mode error when ENV_LABEL has invalid format."""
    # Create config.py
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigINVALID(AppConfigRootClass):
    EXPECTED_KEYS = []
""")
    
    # Set invalid ENV_LABEL
    monkeypatch.setenv("ENV_LABEL", "INVALID")
    
    # Should raise ValueError
    with pytest.raises(ValueError) as excinfo:
        AppConfigRootClass.dynaload(str(tmp_path))
    
    error_msg = str(excinfo.value)
    assert "ENV_LABEL" in error_msg
    assert "INVALID" in error_msg
    assert "DEV, TEST, STAGE, or PROD" in error_msg
    assert "Dokploy" in error_msg or "deployment tool" in error_msg


def test_os_mode_missing_config_py(tmp_path, monkeypatch):
    """Test OS mode error when config.py is missing."""
    # Set ENV_LABEL but no config.py
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    
    # Should raise FileNotFoundError
    with pytest.raises(FileNotFoundError) as excinfo:
        AppConfigRootClass.dynaload(str(tmp_path))
    
    error_msg = str(excinfo.value)
    assert "config.py" in error_msg
    assert "EXPECTED_KEYS" in error_msg
    assert "Dokploy" in error_msg or "deployment tool" in error_msg


def test_os_mode_missing_expected_keys(tmp_path, monkeypatch):
    """Test OS mode error when EXPECTED_KEYS is not defined."""
    # Create config.py without EXPECTED_KEYS
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    DEBUG_MODE = False
""")
    
    # Set ENV_LABEL
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    
    # Should raise AttributeError
    with pytest.raises(AttributeError) as excinfo:
        AppConfigRootClass.dynaload(str(tmp_path))
    
    error_msg = str(excinfo.value)
    assert "EXPECTED_KEYS" in error_msg
    assert "AppConfigPROD01" in error_msg
    assert "Dokploy" in error_msg or "deployment tool" in error_msg


def test_os_mode_missing_expected_vars(tmp_path, monkeypatch):
    """Test OS mode error when some EXPECTED_KEYS are missing from os.environ."""
    # Create config.py with EXPECTED_KEYS
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["API_KEY", "DATABASE_URL", "SECRET_KEY", "MISSING_KEY"]
""")
    
    # Set ENV_LABEL and only some keys
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv("DATABASE_URL", "url")
    monkeypatch.setenv("SECRET_KEY", "secret")
    # MISSING_KEY is not set
    
    # Should raise ConfigurationError
    with pytest.raises(ConfigurationError) as excinfo:
        AppConfigRootClass.dynaload(str(tmp_path))
    
    error_msg = str(excinfo.value)
    assert "MISSING_KEY" in error_msg
    assert "EXPECTED_KEYS" in error_msg
    assert "Dokploy" in error_msg or "deployment tool" in error_msg


def test_os_mode_exact_matching(tmp_path, monkeypatch):
    """Test OS mode uses exact matching (no normalization)."""
    # Create config.py
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["MY_API_KEY", "DATABASE_URL"]
""")
    
    # Set env vars with exact names matching EXPECTED_KEYS
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("MY_API_KEY", "value_from_env")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify exact matching worked
    assert config["MY_API_KEY"] == "value_from_env"
    assert config["DATABASE_URL"] == "postgresql://db"


def test_os_mode_exact_matching_fails_wrong_case(tmp_path, monkeypatch):
    """Test OS mode fails when exact match not found (no normalization fallback)."""
    # Create config.py
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["MY_API_KEY", "DATABASE_URL"]
""")
    
    # Set env vars with wrong casing (should not match)
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("MyApiKey", "wrong_case_value")  # Different casing - should not match MY_API_KEY
    monkeypatch.setenv("DATABASE_URL", "postgresql://db")
    
    # Should fail because exact match MY_API_KEY not found
    with pytest.raises(ConfigurationError) as excinfo:
        AppConfigRootClass.dynaload(str(tmp_path))
    assert "MY_API_KEY" in str(excinfo.value)


def test_os_mode_priority_order(tmp_path, monkeypatch):
    """Test that class attributes override OS env vars (priority maintained)."""
    # Create config.py with class attribute
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["SOME_KEY"]
    SOME_KEY = "from_class"  # Should override OS env var
""")
    
    # Set OS env var
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("SOME_KEY", "from_env")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Env should win (higher priority than class attributes)
    assert config["SOME_KEY"] == "from_env"


def test_os_mode_pkd_parsing(tmp_path, monkeypatch):
    """Test that _PKD values are parsed in OS mode."""
    # Create config.py
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["FEATURES_PKD"]
""")
    
    # Set _PKD env var
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("FEATURES_PKD", "|a=1|b=2|")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify PKD parsing
    assert config["FEATURES_PKD"] == {"a": "1", "b": "2"}


def test_os_mode_master_root_dir(tmp_path, monkeypatch):
    """Test MASTER_ROOT_DIR behavior in OS mode."""
    # Create project structure with pyproject.toml
    project_root = tmp_path / "project"
    project_root.mkdir()
    subdir = project_root / "src" / "subdir"
    subdir.mkdir(parents=True)
    
    # Create pyproject.toml at project root
    pyproject_file = project_root / "pyproject.toml"
    pyproject_file.write_text("[project]\nname = 'test'\n")
    
    # Create config.py
    config_file = project_root / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = []
""")
    
    # Set ENV_LABEL
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    
    # Load from subdirectory
    config = AppConfigRootClass.dynaload(str(subdir))
    
    # MASTER_ROOT_DIR should find pyproject.toml
    assert config["MASTER_ROOT_DIR"] == str(project_root)


def test_os_mode_empty_expected_keys(tmp_path, monkeypatch):
    """Test OS mode with empty EXPECTED_KEYS (should still work)."""
    # Create config.py with empty EXPECTED_KEYS
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = []  # Empty but explicitly defined
    DEBUG_MODE = False
""")
    
    # Set ENV_LABEL only
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    
    # Should load successfully
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    assert config["ENV_SOURCE"] == "OS"
    assert config["ENV_LABEL"] == "PROD01"
    assert config["DEBUG_MODE"] is False


def test_file_mode_unaffected(tmp_path, monkeypatch):
    """Test that file mode is unaffected by OS mode changes."""
    # Create env file (normal file mode)
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    env_file.write_text("TEST_VAR=from_file")
    
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    CLASS_VAR = "from_class"
""")
    
    # Set ENV_LABEL in OS (should be ignored when file exists)
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("TEST_VAR", "from_os")  # Should be ignored
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Should use file mode
    assert config["ENV_SOURCE"] == "FILE"
    assert config["ENV_LABEL"] == "DEVTEST"
    assert config["TEST_VAR"] == "from_file"  # From file, not OS
    assert config["ENV_FPATH"] == str(env_file)


def test_os_mode_with_class_overrides(tmp_path, monkeypatch):
    """Test OS mode with class attributes that override OS env vars."""
    # Create config.py with some keys defined as class attributes
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["API_KEY", "DATABASE_URL", "OVERRIDE_KEY"]
    OVERRIDE_KEY = "from_class"  # Will be overridden by OS env var
    STATIC_KEY = "static_value"  # Not in EXPECTED_KEYS, so not loaded from OS
""")
    
    # Set all keys in OS
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("API_KEY", "from_os")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db")
    monkeypatch.setenv("OVERRIDE_KEY", "from_os")  # Will override class
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify OS vars are loaded
    assert config["API_KEY"] == "from_os"
    assert config["DATABASE_URL"] == "postgresql://db"
    
    # Verify OS overrides class
    assert config["OVERRIDE_KEY"] == "from_os"
    
    # Verify static class values work
    assert config["STATIC_KEY"] == "static_value"


def test_override_warnings_file_mode(tmp_path):
    """Test that warnings are generated when env values override code values in FILE mode."""
    import warnings
    
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file with value that will override class
    env_file.write_text("TEST_KEY=env_value\n")
    
    # Create config class with same key (not in EXPECTED_KEYS)
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    TEST_KEY = "class_value"
""")
    
    # Load config and capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = AppConfigRootClass.dynaload(str(tmp_path))
        
        # Filter for override warnings (exclude EXPECTED_KEYS warnings)
        override_warnings = [warning for warning in w if issubclass(warning.category, UserWarning) and "overridden" in str(warning.message).lower()]
        
        # Verify warning was generated
        assert len(override_warnings) == 1
        assert "TEST_KEY" in str(override_warnings[0].message)
        assert "AppConfigDEVTEST" in str(override_warnings[0].message)
        assert "overridden" in str(override_warnings[0].message).lower()
        # Verify actual values are NOT in warning (confidentiality)
        assert "env_value" not in str(override_warnings[0].message)
        assert "class_value" not in str(override_warnings[0].message)
    
    # Verify env value wins
    assert config["TEST_KEY"] == "env_value"


def test_override_warnings_os_mode(tmp_path, monkeypatch):
    """Test that warnings are generated appropriately in OS mode."""
    import warnings
    
    config_file = tmp_path / "config.py"
    
    # Create config with EXPECTED_KEYS
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["API_KEY", "WARN_KEY"]
    API_KEY = "default_api_key"  # In EXPECTED_KEYS - no warning
    WARN_KEY = "default_warn"  # In EXPECTED_KEYS - no warning (override expected)
    NOT_EXPECTED_KEY = "default_not_expected"  # Not in EXPECTED_KEYS - should warn
""")
    
    # Set OS environment
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("API_KEY", "os_api_key")
    monkeypatch.setenv("WARN_KEY", "os_warn")
    monkeypatch.setenv("NOT_EXPECTED_KEY", "os_not_expected")
    
    # Load config and capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = AppConfigRootClass.dynaload(str(tmp_path))
        
        # Filter for override warnings
        override_warnings = [warning for warning in w if issubclass(warning.category, UserWarning) and "overridden" in str(warning.message).lower()]
        warn_messages = [str(warning.message) for warning in override_warnings]
        
        # NOT_EXPECTED_KEY is not in EXPECTED_KEYS, so it's not loaded from OS at all
        # Therefore, there's no override happening, and no warning should be generated
        # Keys in EXPECTED_KEYS are loaded from OS and override class, but no warning (expected)
        assert not any("NOT_EXPECTED_KEY" in msg for msg in warn_messages)
        assert not any("API_KEY" in msg for msg in warn_messages)
        assert not any("WARN_KEY" in msg for msg in warn_messages)
    
    # Verify OS values win for EXPECTED_KEYS
    assert config["API_KEY"] == "os_api_key"
    assert config["WARN_KEY"] == "os_warn"
    # NOT_EXPECTED_KEY is not in EXPECTED_KEYS, so it's not loaded from OS
    # Should use class default value
    assert config["NOT_EXPECTED_KEY"] == "default_not_expected"


def test_override_warnings_inheritance(tmp_path):
    """Test that warnings mention the correct class in inheritance scenarios."""
    import warnings
    
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file
    env_file.write_text("PARENT_KEY=env_value\n")
    
    # Create parent class with key, child class without override
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigCommon(AppConfigRootClass):
    PARENT_KEY = "parent_value"

class AppConfigDEVTEST(AppConfigCommon):
    pass  # No override of PARENT_KEY
""")
    
    # Load config and capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = AppConfigRootClass.dynaload(str(tmp_path))
        
        # Filter for override warnings
        override_warnings = [warning for warning in w if issubclass(warning.category, UserWarning) and "overridden" in str(warning.message).lower()]
        
        # Verify warning mentions parent class (most-derived that defines it)
        assert len(override_warnings) == 1
        assert "PARENT_KEY" in str(override_warnings[0].message)
        assert "AppConfigCommon" in str(override_warnings[0].message)
    
    # Verify env value wins
    assert config["PARENT_KEY"] == "env_value"


def test_override_warnings_most_derived(tmp_path):
    """Test that warnings mention the most-derived class when both parent and child define key."""
    import warnings
    
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file
    env_file.write_text("SHARED_KEY=env_value\n")
    
    # Create parent and child classes, both defining the key
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigCommon(AppConfigRootClass):
    SHARED_KEY = "parent_value"

class AppConfigDEVTEST(AppConfigCommon):
    SHARED_KEY = "child_value"  # Overrides parent
""")
    
    # Load config and capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = AppConfigRootClass.dynaload(str(tmp_path))
        
        # Filter for override warnings
        override_warnings = [warning for warning in w if issubclass(warning.category, UserWarning) and "overridden" in str(warning.message).lower()]
        
        # Verify warning mentions CHILD class (most-derived), not parent
        assert len(override_warnings) == 1
        assert "SHARED_KEY" in str(override_warnings[0].message)
        assert "AppConfigDEVTEST" in str(override_warnings[0].message)
        assert "AppConfigCommon" not in str(override_warnings[0].message)
    
    # Verify env value wins
    assert config["SHARED_KEY"] == "env_value"


def test_no_warning_for_expected_keys(tmp_path):
    """Test that no warnings are generated for keys in EXPECTED_KEYS."""
    import warnings
    
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file
    env_file.write_text("TEST_KEY=env_value\n")
    
    # Create config with EXPECTED_KEYS
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    EXPECTED_KEYS = ["TEST_KEY"]
    TEST_KEY = "class_value"
""")
    
    # Load config and capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = AppConfigRootClass.dynaload(str(tmp_path))
        
        # Should not generate warning for keys in EXPECTED_KEYS
        warn_messages = [str(warning.message) for warning in w if issubclass(warning.category, UserWarning) and "TEST_KEY" in str(warning.message)]
        assert len(warn_messages) == 0
    
    # Verify env value still wins
    assert config["TEST_KEY"] == "env_value"


def test_no_warning_for_builtin_keys(tmp_path):
    """Test that no warnings are generated for built-in keys."""
    import warnings
    
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file (note: MASTER_ROOT_DIR is a method, so env can't override it)
    # But we can test with a different built-in key if it exists as an attribute
    env_file.write_text("ENV_SOURCE=FILE\n")
    
    # Create config file
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    pass
""")
    
    # Load config and capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = AppConfigRootClass.dynaload(str(tmp_path))
        
        # Should not generate warning for built-in keys
        warn_messages = [str(warning.message) for warning in w if issubclass(warning.category, UserWarning) and "ENV_SOURCE" in str(warning.message)]
        assert len(warn_messages) == 0


def test_methods_still_override_env(tmp_path):
    """Test that methods still have highest priority and override env values."""
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file
    env_file.write_text("TEST_KEY=env_value\n")
    
    # Create config with class attribute and method
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    TEST_KEY = "class_value"
    
    def TEST_KEY(self):
        return "method_value"
""")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify method value wins (highest priority)
    assert config["TEST_KEY"] == "method_value"


def test_methods_access_env_values(tmp_path):
    """Test that methods can access environment-provided values when evaluated."""
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file with values
    env_file.write_text("DB_HOST=prod.example.com\nDB_PORT=5432\n")
    
    # Create config class with defaults
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigDEVTEST(AppConfigRootClass):
    DB_HOST = "localhost"  # Default, will be overridden by env
    DB_PORT = 3306  # Default, will be overridden by env
    
    def DATABASE_URL(self):
        return f"postgresql://{self['DB_HOST']}:{self['DB_PORT']}/myapp"
""")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify method uses environment values (not class defaults)
    assert config["DATABASE_URL"] == "postgresql://prod.example.com:5432/myapp"
    # Also verify env values are in config
    assert config["DB_HOST"] == "prod.example.com"
    assert config["DB_PORT"] == "5432"


def test_env_overrides_class_attributes(tmp_path):
    """Comprehensive test: env file values override class attributes."""
    env_file = tmp_path / "config._THIS_IS_DEVTEST_ENV_.sh"
    config_file = tmp_path / "config.py"
    
    # Create env file with multiple values
    env_file.write_text("""
KEY1=env_value1
KEY2=env_value2
KEY3=env_value3
""")
    
    # Create config with inheritance
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigCommon(AppConfigRootClass):
    KEY1 = "common_value1"
    KEY2 = "common_value2"

class AppConfigDEVTEST(AppConfigCommon):
    KEY2 = "dev_value2"  # Overrides parent
    KEY3 = "dev_value3"
    KEY4 = "dev_value4"  # Not in env, should use class value
""")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify env values override class attributes
    assert config["KEY1"] == "env_value1"  # Env overrides parent
    assert config["KEY2"] == "env_value2"  # Env overrides child (most-derived)
    assert config["KEY3"] == "env_value3"  # Env overrides child
    # Verify class value used when env doesn't have it
    assert config["KEY4"] == "dev_value4"


def test_os_mode_only_expected_keys(tmp_path, monkeypatch):
    """Test that OS mode only loads EXPECTED_KEYS from OS environment."""
    config_file = tmp_path / "config.py"
    
    # Create config with EXPECTED_KEYS
    config_file.write_text("""
from totodev_pub.app_config_root_class import AppConfigRootClass

class AppConfigPROD01(AppConfigRootClass):
    EXPECTED_KEYS = ["API_KEY", "DATABASE_URL"]
    API_KEY = "default_api"
    DATABASE_URL = "default_db"
    NOT_IN_EXPECTED = "default_not_expected"  # Not in EXPECTED_KEYS
""")
    
    # Set OS environment with all keys
    monkeypatch.setenv("ENV_LABEL", "PROD01")
    monkeypatch.setenv("API_KEY", "os_api")
    monkeypatch.setenv("DATABASE_URL", "os_db")
    monkeypatch.setenv("NOT_IN_EXPECTED", "os_not_expected")
    
    # Load config
    config = AppConfigRootClass.dynaload(str(tmp_path))
    
    # Verify EXPECTED_KEYS are loaded from OS
    assert config["API_KEY"] == "os_api"
    assert config["DATABASE_URL"] == "os_db"
    
    # Verify keys NOT in EXPECTED_KEYS are NOT loaded from OS (use class default)
    assert config["NOT_IN_EXPECTED"] == "default_not_expected"
