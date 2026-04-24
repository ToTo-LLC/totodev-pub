# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# app_config_root_class.py

"""
Application Configuration Management System


WHY THIS MODULE EXISTS
======================
Managing configuration across different environments (development, test, staging, production)
is complex. You need to:
- Switch between environments easily
- Keep secrets out of code
- Share configuration between team members
- Support different settings per developer
- Validate required configuration exists
- Handle hierarchical configuration (common + environment-specific)

This module solves these problems with automatic configuration discovery and loading.
It is highly opinionated about the location and naming of config files and directories.


HOW IT WORKS
============
Place these file at project root:
1. config._THIS_IS_XXXX_ENV_.sh - Confidential "environment file" (e.g., config._THIS_IS_DEVALICE_ENV_.sh)
2. pyproject.toml - Project metadata (automatically created/managed by `uv`)

Place these files in your `src/` directory:
3. config.py - Python configuration classes, you may have separate config.py files for sub-projects and pytest scripts
4. config_apply.py - loads the CONFIG objet and initializes any globals from it, you may have multiple of these for sub-projects and pytest scripts.

The system searches upward through directory trees to find these files automatically.


SIMPLE USAGE - GET STARTED IN 30 SECONDS
=========================================
In your config._THIS_IS_DEVXXX_ENV_.sh file:
    ```bash
    export API_KEY="dev_key_12345"
    export CONFIDENTIAL_INFO="this is a secret"
    ```

In your config_apply.py file:
    ```python
    from totodev_pub.app_config_root_class import AppConfigRootClass
    
    CONFIG = AppConfigRootClass.dynaload(__file__) # searches near __file__ or higher for config files

    # fake examples below, replace with your own code
    initialize_api(CONFIG["API_KEY"]) # can access confidential vals
    initialize_tmp_dir(CONFIG["TMP_DIR"]) # can access config.py values
    initialize_project_root(CONFIG["MASTER_ROOT_DIR"]) # preset with absolute path to project root
    ```

In your config.py file:
    ```python
    from totodev_pub.app_config_root_class import AppConfigRootClass
    
    # the AppConfigRootClass.dynaload() method loads the class name based on the environment file name
    class AppConfigDEVXXX(AppConfigRootClass): 
        # All-caps methods and class attributes are evaluated into instance variables
        TMP_DIR = "tmp"
    ```

In your .gitignore file:
    Add a line to ignore the environment file:
    ```text
        config._THIS_IS_*_ENV_.sh
    ```

That's it! The system finds your config file automatically.


INTERMEDIATE USAGE - MULTI-DEVELOPER TEAMS
===========================================
For teams where each developer has their own environment:

In src/config.py (shared by all developers):
    ```python
    from totodev_pub.app_config_root_class import AppConfigRootClass
    
    class AppConfigDevCommon(AppConfigRootClass):
        # Shared configuration for all developers
        DEFAULT_TIMEOUT = 30
        LOG_LEVEL = "INFO"
        
    class AppConfigDEVALICE(AppConfigDevCommon):
        # Alice's personal dev environment config
        DEBUG_MODE = True
        LOCAL_DB_PORT = 5432
        
    class AppConfigDEVBOB(AppConfigDevCommon):
        # Bob's personal dev environment config
        DEBUG_MODE = True
        LOCAL_DB_PORT = 5433  # Different port to avoid conflicts
    ```

Each developer creates their own environment file at project root:
- Alice creates: config._THIS_IS_DEVALICE_ENV_.sh
- Bob creates: config._THIS_IS_DEVBOB_ENV_.sh

In src/config_apply.py (shared initialization):
    ```python
    from totodev_pub.app_config_root_class import AppConfigRootClass
    
    CONFIG = AppConfigRootClass.dynaload(__file__)
    # Automatically loads AppConfigDEVALICE or AppConfigDEVBOB based on env file
    
    print(CONFIG["DEFAULT_TIMEOUT"])  # 30 (from Common)
    print(CONFIG["DEBUG_MODE"])       # True (from developer-specific class)
    print(CONFIG["LOCAL_DB_PORT"])    # 5432 or 5433 depending on who runs it
    ```


ADVANCED USAGE - MULTI-ENVIRONMENT DEPLOYMENTS
===============================================
For production deployments with hierarchical configuration:

In src/config.py:
    ```python
    class AppConfigCommon(AppConfigRootClass):
        RETRY_ATTEMPTS = 3
        LOG_LEVEL = "INFO"
        
    class AppConfigTEST01(AppConfigCommon):
        DEBUG_MODE = True
        LOG_LEVEL = "DEBUG"
        
    class AppConfigPROD01(AppConfigCommon):
        DEBUG_MODE = False
        RETRY_ATTEMPTS = 5  # Override for production
        
        def DATABASE_URL(self):
            # Calculated from environment variables
            return f"postgresql://{self.DB_HOST}:{self.DB_PORT}/myapp"
    ```

Create environment files for each deployment:
- config._THIS_IS_TEST01_ENV_.sh (loads AppConfigTEST01)
- config._THIS_IS_PROD01_ENV_.sh (loads AppConfigPROD01)

In src/config_apply.py:
    ```python
    CONFIG = AppConfigRootClass.dynaload(__file__)
    # Automatically loads correct class based on which env file is present
    
    database_url = CONFIG["DATABASE_URL"]  # Calls method, returns calculated value
    retry_count = CONFIG["RETRY_ATTEMPTS"]
    ```


ADVANCED FEATURES
=================
- **Packed Key-Value Strings**: Keys ending in _PKD are parsed automatically
  In environment file: export FEATURES_PKD="|feature1=on|feature2=off|"
  Access: CONFIG["FEATURES_PKD"] returns {"feature1": "on", "feature2": "off"}

- **Dynamic Configuration**: Methods with all-caps names are evaluated at startup
  and return value cached for all future access.
  In config.py: `def DATABASE_URL(self): return f"postgresql://{self.DB_HOST}:{self.DB_PORT}/myapp"`
  Access: CONFIG["DATABASE_URL"] returns the calculated value from the method
  This allows for dynamic configuration based on the environment file variables
  and the config.py class attributes.

- **Configuration Validation**: Declare EXPECTED_KEYS to catch missing configuration at startup
  to spot missing configuration items at program startup.
  In config.py: EXPECTED_KEYS = {"API_KEY": "API key for external API", 
                                 "CONFIDENTIAL_INFO": "Confidential information for local development"
                                }
  See AppConfigRootClass docstring for full EXPECTED_KEYS documentation

- **File Change Detection**: Detect when config files change during runtime
  In config_apply.py: modified_files = CONFIG.files_were_updated()

- **Access Styles**: Both attribute and dictionary access supported
  CONFIG["API_KEY"] (always works) or CONFIG.API_KEY (only for non-method values)
  IMPORTANT: Use dictionary style for values defined by methods

- **Multiple Config Files**: Support sub-projects with their own config.py
  Each sub-project can have its own src/config.py and config_apply.py
  All share the same root environment file


PREDEFINED CONFIGURATION VALUES
================================
These values are automatically available in every CONFIG instance:

┌─────────────────────┬──────────────────────────────────────────────────────┐
│ Key                 │ Description                                          │
├─────────────────────┼──────────────────────────────────────────────────────┤
│ MASTER_ROOT_DIR     │ Absolute path to project root directory              │
│ ENV_FPATH           │ Absolute path to environment file (or "<OS_ENVIRONMENT>" in OS mode) │
│ ENV_LABEL           │ Full environment label (e.g., "DEVALICE", "PROD")     │
│ ENV_TYPE            │ Environment type prefix (DEV, TEST, STAGE, PROD)     │
│ ENV_SOURCE          │ Configuration source: "FILE" or "OS"                 │
│ CONFIG_CLASS_NAME   │ Name of the config class being used                  │
│ CONFIG_CLASS_FPATH  │ Absolute path to config.py file                      │
│ PYPROJECT_TOML      │ Parsed pyproject.toml contents (Python 3.11+ only)   │
└─────────────────────┴──────────────────────────────────────────────────────┘

Usage examples:
    root = CONFIG["MASTER_ROOT_DIR"]
    env_type = CONFIG["ENV_TYPE"]
    project_name = CONFIG.dig("PYPROJECT_TOML", "project", "name")


SEARCH BEHAVIOR
===============
dynaload(__file__) searches upward through directory tree from __file__ location.

Typical project structure:
    /project_root/
        config._THIS_IS_DEVALICE_ENV_.sh  ← Found by upward search
        pyproject.toml
        src/
            config.py              ← Your config classes
            config_apply.py        ← Loads CONFIG here
            myapp/
                main.py            ← Imports from config_apply
        tests/
            config.py          ← Test-specific config classes (optional)
            config_apply.py    ← Test initialization (optional)

The search starts from where you call dynaload(search_start_fpath) and climbs up until
it finds the environment file, likewise for config.py 


OS-BASED CONFIGURATION FOR DEPLOYMENT TOOLS
===========================================
For automated deployment tools (like Dokploy) that deploy from Git and cannot include
confidential environment files, the system supports loading configuration directly from
OS environment variables.

In deployment environments, set these OS environment variables:
    ```bash
    export ENV_LABEL="PROD01"  # Must start with DEV, TEST, STAGE, or PROD
    export API_KEY="your_api_key"
    export DATABASE_URL="postgresql://prod-server/db"
    # ... all other variables declared in EXPECTED_KEYS
    ```

In your config.py file, you MUST define EXPECTED_KEYS (even if empty):
    ```python
    from totodev_pub.app_config_root_class import AppConfigRootClass
    
    class AppConfigPROD01(AppConfigRootClass):
        EXPECTED_KEYS = ["API_KEY", "DATABASE_URL", "SECRET_KEY"]
        # Other configuration can go here too
        DEBUG_MODE = False
    ```

In your config_apply.py file (same as file-based mode):
    ```python
    from totodev_pub.app_config_root_class import AppConfigRootClass
    
    CONFIG = AppConfigRootClass.dynaload(__file__)
    
    # Check configuration source
    if CONFIG["ENV_SOURCE"] == "OS":
        # Using OS-based configuration (deployment tool)
        print("Configuration loaded from OS environment variables")
    else:
        # Using file-based configuration (local development)
        print("Configuration loaded from environment file")
    ```

How it works:
- If no confidential environment file is found, the system checks for ENV_LABEL in os.environ
- ENV_LABEL must be set and start with a valid EnvironmentType (DEV, TEST, STAGE, PROD)
- config.py must exist and define EXPECTED_KEYS (safety check to prevent accidental OS mode)
- All variables declared in EXPECTED_KEYS that were not defined in code must be set in the OS environment.
  Which, with a tool like Dokploy, is done by setting environment variables in the tool.
  

  Example: If EXPECTED_KEYS = ["MY_API_KEY"] and you wanted the environment to be "PROD01"
           then you would set the following environment variables in your deployment tool:
             - ENV_LABEL="PROD01"
             - MY_API_KEY="my_api_key"


PYTEST TESTING PATTERNS
========================
Most projects need a simple pytest fixture to load their configuration for tests.
This section shows the recommended patterns for using this configuration system with pytest.
Note that more complex uses of the class can allow pytest cases that use variations of the project configs.

Basic Pattern (recommended for most projects):
    In tests/conftest.py:
        ```python
        import pytest
        from pathlib import Path

        @pytest.fixture(scope="session")
        def config():
            # Load project configuration and trigger initialization
            import sys
            src_path = Path(__file__).parent.parent / "src"
            sys.path.insert(0, str(src_path))
            from config_apply import CONFIG
            return CONFIG
        ```
    
    In tests/test_myapp.py:
        ```python
        def test_api_connection(config):
            api_key = config["API_KEY"]
            # Your test logic here
            assert api_key is not None
        ```

Integration vs Disconnected Tests:
    Some tests require external systems (databases, APIs, cloud services). Mark these
    tests so they can be skipped in CI or disconnected environments.
    
    In tests/conftest.py (add pytest marker configuration):
        ```python
        import pytest
        
        def pytest_configure(config):
            config.addinivalue_line(
                "markers", "integration: mark test as requiring external systems"
            )
        ```
    
    In tests/test_myapp.py (mark integration tests):
        ```python
        import pytest
        
        @pytest.mark.integration
        def test_database_query(config):
            # This test requires a live database
            result = query_user_database(config["DATABASE_URL"])
            assert result is not None
        
        def test_validation_logic(config):
            # This test is isolated and always runs
            from myapp import validate_email
            assert validate_email("test@example.com") is True
        ```
    
    Run tests selectively:
        ```bash
        pytest -m "not integration"  # Skip integration tests (for CI/disconnected)
        pytest -m "integration"      # Run only integration tests
        pytest                       # Run all tests
        ```

Helper Function (optional convenience):
    For even simpler setup, use the provided helper from totodev_pub:
    
    In tests/conftest.py:
        ```python
        from totodev_pub.pytest_tools import make_config_fixture
        
        # Below line finds and uses the CONFIG object from config_apply.py
        CONFIG:AppConfigRootClass = make_config_fixture()  # Creates the fixture automatically
        ```

Advanced: Test-Specific Configuration (rarely needed):
    If you need different configuration classes for tests (e.g., in-memory databases,
    mock API endpoints), you can create test-specific config classes that inherit from
    your production configs. Use dynaload() with a list of search paths to locate both
    test-specific environment files and production config classes. This pattern is more
    complex and only needed for sophisticated test scenarios - see the advanced
    configuration documentation for details.
"""

from dotenv import dotenv_values,load_dotenv
import os
import re
import importlib.util
import inspect
import glob
from pydantic import BaseModel
from typing import Optional,List,Generator,Dict,Any, Union, Tuple, Literal, Set, Sequence
import types
from enum import Enum
import inspect
try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # Python 3.9-3.10, TOML parsing disabled
from datetime import datetime


class ConfigurationError(Exception):
    """Exception raised when configuration validation fails.
    
    Raised when required configuration keys (declared in EXPECTED_KEYS) are missing
    and expect_policy is set to "error" (the default).
    
    Example:
        If AppConfigDEV declares EXPECTED_KEYS = ["DATABASE_URL"] but the environment
        file doesn't export DATABASE_URL, this exception will be raised during
        configuration initialization.
    """
    pass


# Module-level constants defining configuration file naming patterns
# These are public so library maintainers understand the file discovery mechanism
ENV_VARS_DUMPFILENAME_PATTERN = "env_vars_dump_*.txt"
ENV_FILENAME_PATTERN = "config._THIS_IS_*_ENV_.sh"
ENV_FILENAME_RX = "^" + ENV_FILENAME_PATTERN.replace("*", r"(\w+)").replace(".", r"\.") + "$"
CONFIG_FILENAME_PATTERN = "config.py"
OS_ENVIRONMENT_SENTINEL = "<OS_ENVIRONMENT>"


class EnvironmentType(Enum):
    """Valid environment type prefixes for environment file names.
    
    Environment files must be named: config._THIS_IS_XXXX_ENV_.sh
    where XXXX starts with one of these prefixes, optionally followed by additional characters.
    
    Valid examples:
        - config._THIS_IS_DEV_ENV_.sh (label: "DEV", type: "DEV")
        - config._THIS_IS_DEVJOHN_ENV_.sh (label: "DEVJOHN", type: "DEV")
        - config._THIS_IS_PROD_ENV_.sh (label: "PROD", type: "PROD")
        - config._THIS_IS_TEST_ENV_.sh (label: "TEST", type: "TEST")
        - config._THIS_IS_STAGE_ENV_.sh (label: "STAGE", type: "STAGE")
    
    The environment type determines which config class is loaded:
        - DEV* -> AppConfigDEV
        - TEST* -> AppConfigTEST
        - STAGE* -> AppConfigSTAGE
        - PROD* -> AppConfigPROD
    """
    DEV = "DEV"
    TEST = "TEST"
    STAGE = "STAGE"
    PROD = "PROD"



class _AppConfigCacheKey(BaseModel):
    """Internal cache key that uniquely identifies a configuration file set.
    
    This immutable key identifies a specific combination of configuration files:
    - Environment file (config._THIS_IS_XXXX_ENV_.sh)
    - Config class file (config.py)
    - Config class name to instantiate
    
    Purpose:
    - Enables caching of configuration values across multiple instances
    - Tracks file modification times to detect configuration changes
    - Extracts environment metadata (label, type) from filenames
    
    Attributes:
        environment_file_fpath: Absolute path to config._THIS_IS_XXXX_ENV_.sh or OS_ENVIRONMENT_SENTINEL for OS mode
        config_py_fpath: Absolute path to config.py (or this module if none found)
        config_class_name: Name of config class to load (e.g., "AppConfigDEV")
        search_start_path: Optional path used for MASTER_ROOT_DIR lookup in OS mode (only set when environment_file_fpath is OS_ENVIRONMENT_SENTINEL)
        _file_stats: Dict mapping file paths to (mtime, size) tuples for change detection
    
    Note:
        This is an internal class. Users should not need to instantiate it directly.
        It's created automatically by dynaload() and deduce_cache_key().
    """
    environment_file_fpath: str # of the config._THIS_IS_XXXX_ENV_.sh file, or OS_ENVIRONMENT_SENTINEL for OS mode
    config_py_fpath: str   # of the config.py file
    config_class_name: str         # Expected to be defined within the config.py file, inferred from the XXXX part of the environment file name
    search_start_path: Optional[str] = None  # Used for MASTER_ROOT_DIR lookup in OS mode
    _file_stats: Dict[str, Tuple[float, int]] = {}  # Maps filepath to (mtime, size) tuple

    model_config = {
        'frozen': True  # Makes the model immutable and hashable
        }

    def __init__(self, **data):
        """Initialize the cache key and capture initial file stats."""
        super().__init__(**data)
        self._capture_file_stats()

    def _capture_file_stats(self) -> None:
        # Capture modification time and size for all tracked files
        # Only tracks files that currently exist
        pass
        # Clear existing stats before capturing new ones
        self._file_stats.clear()
        
        for fpath in [self.environment_file_fpath, self.config_py_fpath]:
            # Skip sentinel value (OS mode) - it's not a real file path
            if fpath == OS_ENVIRONMENT_SENTINEL:
                continue
            if os.path.exists(fpath):
                stats = os.stat(fpath)
                self._file_stats[fpath] = (stats.st_mtime, stats.st_size)
        
        # pyproject.toml is optional, only track if it exists
        toml_path = self.pyproject_toml_fpath()
        if toml_path and os.path.exists(toml_path):
            stats = os.stat(toml_path)
            self._file_stats[toml_path] = (stats.st_mtime, stats.st_size)

    def reset_file_stats(self) -> None:
        """Reset file statistics to current state for change tracking.
        
        Updates the stored modification times and file sizes to match the current
        state of all tracked configuration files. Use this when you want to start
        tracking changes from the current point in time.
        """
        self._capture_file_stats()

    def files_were_updated(self) -> List[str]:
        """Check if any configuration files have been modified since initialization.
        
        Returns:
            List of absolute paths to files that were modified since the cache key
            was created. Empty list if no files changed.
            
        Detects changes in:
        - Environment file (config._THIS_IS_XXXX_ENV_.sh)
        - Config file (config.py)
        - pyproject.toml (if present)
        """
        updated_files = []
        
        # Build list of paths to check - includes both current paths and tracked paths
        check_paths_set = set([self.environment_file_fpath, self.config_py_fpath])
        toml_path = self.pyproject_toml_fpath()
        if toml_path:
            check_paths_set.add(toml_path)
        
        # Also include any paths that were tracked (in case they were removed)
        check_paths_set.update(self._file_stats.keys())
        
        for fpath in check_paths_set:
            was_tracked = fpath in self._file_stats
            exists_now = os.path.exists(fpath)
            
            if was_tracked and not exists_now:
                # File existed during init but now doesn't
                updated_files.append(fpath)
            elif exists_now:
                stats = os.stat(fpath)
                current_stats = (stats.st_mtime, stats.st_size)
                
                if was_tracked and self._file_stats[fpath] != current_stats:
                    # File was modified since init
                    updated_files.append(fpath)
                elif not was_tracked:
                    # New file appeared that wasn't present during init
                    updated_files.append(fpath)
                    
        return updated_files

    def newest_loaded(self) -> Optional[datetime]:
        """Get the most recent modification time among loaded configuration files.
        
        Returns:
            The datetime of the most recently modified file that was loaded,
            or None if no files were tracked.
        """
        latest_mtime = 0
        
        for fpath, (mtime, _) in self._file_stats.items():
            latest_mtime = max(latest_mtime, mtime)
                
        return datetime.fromtimestamp(latest_mtime) if latest_mtime > 0 else None

    def blanks(self) -> List[str]:
        """Get names of fields that are empty or None."""
        return [k for k,v in self.dict().items() if not v] 

    def pyproject_toml_fpath(self) -> Optional[str]:
        """Get the full path to pyproject.toml by searching upward from environment file.
        
        Searches upward through parent directories starting from the environment file's
        directory until pyproject.toml is found or the filesystem root is reached.
        
        Returns:
            Absolute path to pyproject.toml if found, None otherwise
        """
        current = os.path.dirname(self.environment_file_fpath)
        for _ in range(10):  # Max 10 levels up
            toml_path = os.path.join(current, "pyproject.toml")
            if os.path.exists(toml_path):
                return toml_path
            parent = os.path.dirname(current)
            if parent == current:  # Reached filesystem root
                break
            current = parent
        return None

    def has_config_file(self) -> bool:
        """Check if a custom config.py file was found and is being used.
        
        Returns True if a specific config.py file was found, False if the
        default AppConfigRootClass is being used due to no config.py being found.
        """
        return self.config_py_fpath != __file__ # the __file__ is the file that imported this module
    
    def env_label(self) -> str:
        """Extract the full environment label from the environment file name.
        
        The label is the XXXX portion in config._THIS_IS_XXXX_ENV_.sh
        
        Examples:
            config._THIS_IS_DEV_ENV_.sh -> "DEV"
            config._THIS_IS_DEVJOHN_ENV_.sh -> "DEVJOHN"
            config._THIS_IS_PROD_ENV_.sh -> "PROD"
        
        Returns:
            Environment label string
        """
        return re.match(ENV_FILENAME_RX, os.path.basename(self.environment_file_fpath)).group(1)
    
    def env_type(self) -> str:
        """Extract the environment type prefix from the environment label.
        
        Returns the first part of the label that matches a valid EnvironmentType
        (DEV, TEST, STAGE, or PROD).
        
        Examples:
            Label "DEV" -> type "DEV"
            Label "DEVJOHN" -> type "DEV"
            Label "PROD" -> type "PROD"
        
        Side effect: Sets os.environ['ENV_TYPE'] to the returned value
        
        Returns:
            Environment type string (DEV, TEST, STAGE, or PROD)
            
        Raises:
            ValueError: If label doesn't start with a valid environment type
        """
        etype_match = re.match(fr"^({'|'.join(e.value for e in EnvironmentType)})(.*)", self.env_label()) 
        if not etype_match:
            raise ValueError(f"Environment file {self.environment_file_fpath} does not start with a valid EnvironmentType value {[e.value for e in EnvironmentType]}.")
        return etype_match.group(1)
    

    @classmethod
    def _search(cls, fpath_for_env_file_search:str,fpath_for_config_file_search:Optional[str] = None, config_class_name:Optional[str] = None,require_config_file:bool = True) -> '_AppConfigCacheKey':
        """Discover configuration files by searching upward through directory tree.
        
        Starting from the given path, searches parent directories until finding:
        1. Environment file matching config._THIS_IS_*_ENV_.sh pattern
        2. Config file named config.py (optional)
        
        The environment file name determines the expected config class name.
        For example: config._THIS_IS_DEV_ENV_.sh expects AppConfigDEV class.
        
        Args:
            fpath_for_env_file_search: Starting path for environment file search
            fpath_for_config_file_search: Starting path for config.py search (defaults to env search path)
            config_class_name: Override expected class name (defaults to AppConfig{XXXX} from env file)
            require_config_file: If False, allows missing config.py (returns AppConfigRootClass)
            
        Returns:
            Cache key identifying the discovered configuration file set
            
        Raises:
            FileNotFoundError: If no environment file found, or if config.py required but not found
        """
        env_file = cls.find_files_at_or_above(fpath_for_env_file_search, ENV_FILENAME_PATTERN)
        if len(env_file) != 1:
            raise FileNotFoundError(f"Expecting exactly one environment file ({ENV_FILENAME_PATTERN}) at or above the file {fpath_for_env_file_search}.  Found {len(env_file)} files: {env_file}")

        config_file = cls.find_files_at_or_above(fpath_for_config_file_search or fpath_for_env_file_search, CONFIG_FILENAME_PATTERN)
        if len(config_file) > 1 or (len(config_file) == 0 and require_config_file):
            raise FileNotFoundError(f"Expecting exactly one config file ({CONFIG_FILENAME_PATTERN}) at or above the file {fpath_for_config_file_search}.  Found {len(config_file)} files: {config_file}")
        if len(config_file) == 0:
            # if no config.py file is present, use the root class
            return cls(environment_file_fpath=env_file[0], config_py_fpath=__file__, config_class_name = "AppConfigRootClass")

        env_label = re.match(ENV_FILENAME_RX, os.path.basename(env_file[0])).group(1)
        expected_config_class_name = config_class_name or f"AppConfig{env_label}"
        return cls(environment_file_fpath=env_file[0], config_py_fpath=config_file[0], config_class_name = expected_config_class_name)

    def _load_config_class(self) -> type:    
        # Dynamically load and return the configuration class from config.py file
        if self.config_class_name == "AppConfigRootClass":
            return AppConfigRootClass  # don't dynamically load this one

        spec = importlib.util.spec_from_file_location("config", self.config_py_fpath)
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        try: 
            config_class = getattr( config,self.config_class_name)
        except AttributeError:
            raise AttributeError(f"Expecting a class named {self.config_class_name} in the config.py file at {self.config_py_fpath}.")

        #config_instance = config_class()
        if not issubclass(config_class, AppConfigRootClass):
            raise TypeError(f"Expecting class {self.config_class_name} to inherit from AppConfigRootClass in the config.py file at {self.config_py_fpath}.")

        return config_class   # caller responseible for instantiating

    @staticmethod
    def find_files_at_or_above(near_or_above_file: str, filename_glob:str, max_climb:int =9999, stop_at_git: bool = False) -> List[str]:
        """Search for files matching a pattern at or above the given path.
        
        Args:
            near_or_above_file: Starting file or directory path
            filename_glob: Glob pattern to match (e.g., "config.*.sh")
            max_climb: Maximum number of directories to search upward
            stop_at_git: Stop searching when .git directory is found
            
        Returns:
            List of full paths to matching files, empty list if none found
        """
        current_dir = near_or_above_file if os.path.isdir(near_or_above_file) else os.path.dirname(near_or_above_file)
        for _ in range(max_climb):
            # Find all matches of filename glob can be found in the current_dir
            files = glob.glob(os.path.join(current_dir, filename_glob))
            if files:
                return files
            # We assume the folder containing the .git repository is the root of the overall project and rise no further
            parent_dir= os.path.dirname(current_dir) # next step upward
            if stop_at_git and ".git" in os.listdir(current_dir) or parent_dir == current_dir:
                return [] # indicate failure to find before hitting stop point
            current_dir = parent_dir
        return [] # indicate failure to find after max climb


######## End of AppConfigCacheKey class definition

class AppConfigRootClass():
    """Base class for application configuration with automatic discovery and loading.
    
    STANDARD USAGE PATTERN
    ======================
    In src/config_apply.py (recommended approach):
        ```python
        from totodev_pub.app_config_root_class import AppConfigRootClass
        
        CONFIG = AppConfigRootClass.dynaload(__file__)
        # CONFIG is now loaded and ready to use
        ```
    
    In other modules:
        ```python
        from config_apply import CONFIG
        
        api_key = CONFIG["API_KEY"]
        db_url = CONFIG["DATABASE_URL"]  # Works even if DATABASE_URL is a method
        ```
    
    Manual instantiation (advanced, rarely needed):
        ```python
        class AppConfigDEVALICE(AppConfigRootClass):
            def __init__(self):
                self.deduce_cache_key(__file__)
        
        cfg = AppConfigDEVALICE()
        ```
    
    
    ACCESSING CONFIGURATION
    =======================
    Dictionary-style (recommended - always works):
        ```python
        value = CONFIG["DATABASE_URL"]  # Works for both attributes and methods
        root_dir = CONFIG["MASTER_ROOT_DIR"]
        timeout = CONFIG["DEFAULT_TIMEOUT"]
        ```
    
    Attribute-style (only for non-method values):
        ```python
        # Only works if DATABASE_URL is a class attribute, NOT a method
        value = CONFIG.DATABASE_URL  # ⚠️ FAILS if DATABASE_URL is defined as def DATABASE_URL(self)
        
        # Safe for built-in values that are always methods:
        root_dir = CONFIG.MASTER_ROOT_DIR  # Works (built-in method)
        ```
    
    IMPORTANT: Methods with ALL_CAPS names cannot be accessed with attribute notation.
    Always use dictionary-style CONFIG["KEY"] for values that might be methods.
    
    Safe traversal of nested structures:
        ```python
        # Returns None if any part is missing instead of raising KeyError
        project_name = CONFIG.dig("PYPROJECT_TOML", "project", "name")
        first_dep = CONFIG.dig("PYPROJECT_TOML", "project", "dependencies", 0)
        ```
    
    
    CONFIGURATION PRIORITY (HIGHEST TO LOWEST)
    ==========================================
    1. Methods with ALL_CAPS names (calculated, evaluated lazily)
    2. Environment file/OS variables (from config._THIS_IS_*_ENV_.sh or os.environ)
    3. Class attributes (defined in your config classes, used as defaults)
    
    
    DEFINING CONFIGURATION CLASSES
    ===============================
    In src/config.py, define classes for each environment/developer:
    
    Single developer (basic structure):
        ```python
        from totodev_pub.app_config_root_class import AppConfigRootClass
        
        class AppConfigDEVALICE(AppConfigRootClass):
            # Static values override environment variables
            DEFAULT_TIMEOUT = 30
            DEBUG_MODE = True
            
            # Methods provide calculated values (evaluated once, then cached)
            def DATABASE_URL(self):
                return f"postgresql://localhost/{self.ENV_LABEL}"
        ```
    
    Multi-developer team (typical structure):
        ```python
        from totodev_pub.app_config_root_class import AppConfigRootClass
        
        class AppConfigCommon(AppConfigRootClass):
            # Shared configuration across all developers
            DEFAULT_TIMEOUT = 30
            RETRY_ATTEMPTS = 3
        
        class AppConfigDEVALICE(AppConfigCommon):
            DEBUG_MODE = True
            LOCAL_PORT = 8000
            
        class AppConfigDEVBOB(AppConfigCommon):
            DEBUG_MODE = True
            LOCAL_PORT = 8001  # Different port per developer
        ```
    
    Then in src/config_apply.py:
        ```python
        from totodev_pub.app_config_root_class import AppConfigRootClass
        
        CONFIG = AppConfigRootClass.dynaload(__file__)
        # Automatically selects AppConfigDEVALICE or AppConfigDEVBOB
        # based on which environment file is present
        ```
    
    
    CONFIGURATION VALIDATION WITH EXPECTED_KEYS
    ===========================================
    Declare required configuration to catch missing values at startup.
    This prevents runtime failures from missing environment variables.
    
    In src/config.py - Simple list format:
        ```python
        class AppConfigDEVALICE(AppConfigRootClass):
            EXPECTED_KEYS = ["DATABASE_URL", "API_KEY", "SECRET_KEY"]
        ```
    
    In src/config.py - Dictionary format (with help text):
        ```python
        class AppConfigPROD(AppConfigRootClass):
            EXPECTED_KEYS = {
                "DATABASE_URL": "PostgreSQL connection string from secrets",
                "API_KEY": "External API authentication key",
                "SECRET_KEY": "Application secret for session signing"
            }
        ```
    
    Validation policy (controls behavior when keys are missing):
        ```python
        class AppConfigPROD(AppConfigRootClass):
            expect_policy = "error"  # Raises ConfigurationError (default)
            EXPECTED_KEYS = ["DATABASE_URL", "API_KEY"]
        
        class AppConfigDEVALICE(AppConfigRootClass):
            expect_policy = "warning"  # Logs warning but continues
            EXPECTED_KEYS = ["DEV_DATABASE_URL"]
        ```
    
    Inheritance (subclasses automatically inherit parent EXPECTED_KEYS):
        ```python
        class AppConfigCommon(AppConfigRootClass):
            EXPECTED_KEYS = ["API_KEY"]  # Required by all
        
        class AppConfigDEVALICE(AppConfigCommon):
            EXPECTED_KEYS = ["DEV_DATABASE_URL"]  # Also requires API_KEY
            
        class AppConfigPROD(AppConfigCommon):
            EXPECTED_KEYS = ["PROD_DATABASE_URL"]  # Also requires API_KEY
        ```
    
    In src/config_apply.py:
        ```python
        CONFIG = AppConfigRootClass.dynaload(__file__)
        # If any EXPECTED_KEYS are missing, ConfigurationError is raised here
        # (unless expect_policy = "warning")
        ```
    
    
    SPECIAL FEATURES
    ================
    Packed Key-Value Strings (_PKD suffix):
        Keys ending in _PKD are automatically parsed from delimited strings:
        
        In environment file:
            export FEATURES_PKD="|feature1=enabled|feature2=disabled|"
        
        In config_apply.py:
            features = CONFIG["FEATURES_PKD"]  # Returns: {"feature1": "enabled", "feature2": "disabled"}
        
        Also works with lists:
            In environment: export ITEMS_PKD="~item1~item2~item3~"
            Access: items = CONFIG["ITEMS_PKD"]  # Returns: ["item1", "item2", "item3"]
    
    File Change Detection:
        ```python
        # In config_apply.py or other modules
        modified = CONFIG.files_were_updated()  # Returns list of changed files
        when = CONFIG.newest_loaded()  # Returns datetime of most recent config file
        ```
    
    Built-in Configuration Values:
        ```python
        # Available automatically in any CONFIG instance
        root = CONFIG["MASTER_ROOT_DIR"]   # Project root directory path
        label = CONFIG["ENV_LABEL"]        # e.g., "DEVALICE", "PROD", "TEST"
        env_type = CONFIG["ENV_TYPE"]      # e.g., "DEV", "PROD", "TEST", "STAGE"
        
        # Build paths relative to project root
        data_file = CONFIG.abspath("data", "input.csv")
        logs_dir = CONFIG.abspath("logs")
        ```
    
    PyProject.toml Access:
        If pyproject.toml exists at project root, it's automatically loaded:
        ```python
        # In config_apply.py or other modules
        project_name = CONFIG.dig("PYPROJECT_TOML", "project", "name")
        dependencies = CONFIG.dig("PYPROJECT_TOML", "project", "dependencies")
        version = CONFIG.dig("PYPROJECT_TOML", "project", "version")
        ```
    
    
    IMPORTANT NOTES
    ===============
    - **Access Style Limitation**: 
      ⚠️ Use CONFIG["KEY"] (dictionary-style) not CONFIG.KEY (attribute-style)
      Attribute-style access FAILS for values defined as methods in config.py
      Dictionary-style access ALWAYS works for all configuration values
    
    - **File Locations**: 
      Root: config._THIS_IS_*_ENV_.sh (secrets/confidential), pyproject.toml
      src/: config.py (classes), config_apply.py (initialization)
    
    - **Read-Only**: Configuration values (uppercase keys) cannot be modified after loading
    
    - **Caching**: All values are cached after first access for performance
    
    - **Lazy Evaluation**: Methods with ALL_CAPS names are evaluated once on first access
    
    - **Name Normalization**: CamelCase environment variables become CAMEL_CASE
      Example: export MyVariable="value" becomes CONFIG["MY_VARIABLE"]
    
    - **Exclusions**: Variables starting with _ or listed in DO_NOT_AUTOPORT are not imported
    
    - **Scope**: This is for application configuration, not library settings
      Each application should have its own config system
    
    - **Multi-Developer**: Each developer has their own environment file and config class
      but shares the same config.py file with the team
    """
    
    # Class-level configuration validation policy
    expect_policy: Optional[Literal["error", "warning"]] = None

    @staticmethod
    def infer_environment_type(search_at_or_above_file: str) -> EnvironmentType:
        """Quickly infer the EnvironmentType from a nearby environment file.
        
        This is a lightweight helper that:
        1. Searches upward from the given path for a matching environment file
        2. Uses the *filename only* (does NOT load the file) to infer the type
        
        Args:
            search_at_or_above_file: A file or directory path to start the upward search from.
        
        Returns:
            EnvironmentType corresponding to the first matching environment file found.
        
        Raises:
            FileNotFoundError: If no environment file is found (or more than one is found)
            ValueError: If the environment label does not start with a valid EnvironmentType
        """
        env_files = _AppConfigCacheKey.find_files_at_or_above(
            search_at_or_above_file,
            ENV_FILENAME_PATTERN,
        )
        if len(env_files) != 1:
            raise FileNotFoundError(
                f"Expecting exactly one environment file ({ENV_FILENAME_PATTERN}) at or above the path "
                f"{search_at_or_above_file}. Found {len(env_files)} files: {env_files}"
            )

        env_filename = os.path.basename(env_files[0])
        match = re.match(ENV_FILENAME_RX, env_filename)
        if not match:
            raise ValueError(
                f"Environment file '{env_filename}' does not match expected pattern {ENV_FILENAME_RX!r}."
            )
        env_label = match.group(1)

        # Infer type from prefix of the label using the defined EnvironmentType values
        for etype in EnvironmentType:
            if env_label.startswith(etype.value):
                return etype

        raise ValueError(
            f"Environment label '{env_label}' from file '{env_filename}' does not start with a valid "
            f"EnvironmentType value {[e.value for e in EnvironmentType]}."
        )

    def MASTER_ROOT_DIR(self) -> str:
        """Get the absolute path to the project root directory.
        
        The root directory is where the environment file (config._THIS_IS_*_ENV_.sh) is located,
        or (in OS mode) the directory containing pyproject.toml or the current working directory.
        This is typically the top-level directory of your project.
        
        Returns:
            Absolute path to project root directory
            
        Usage:
            ```python
            root = CONFIG["MASTER_ROOT_DIR"]
            data_dir = os.path.join(root, "data")
            ```
        """
        if self.cache_key.environment_file_fpath == OS_ENVIRONMENT_SENTINEL:
            # OS mode: search from search_start_path or cwd
            if self.cache_key.search_start_path:
                toml_path = self._find_pyproject_toml_upward(self.cache_key.search_start_path)
                if toml_path:
                    return os.path.dirname(toml_path)
            # Second attempt: search from cwd
            toml_path = self._find_pyproject_toml_upward(os.getcwd())
            if toml_path:
                return os.path.dirname(toml_path)
            # Fallback
            return os.getcwd()
        else:
            # FILE mode: existing logic
            return os.path.abspath(os.path.dirname(self.cache_key.environment_file_fpath))

    def ENV_FPATH(self) -> str:
        """Get the absolute path to the environment file being used.
        
        In FILE mode, returns the absolute path to the config._THIS_IS_*_ENV_.sh file.
        In OS mode, returns OS_ENVIRONMENT_SENTINEL to indicate OS-based configuration.
        
        Returns:
            Absolute path to the config._THIS_IS_*_ENV_.sh file, or OS_ENVIRONMENT_SENTINEL in OS mode
            
        Example:
            FILE mode: /Users/dave/project/config._THIS_IS_DEV_ENV_.sh
            OS mode: "<OS_ENVIRONMENT>"
        """
        if self.cache_key.environment_file_fpath == OS_ENVIRONMENT_SENTINEL:
            return OS_ENVIRONMENT_SENTINEL
        else:
            return self.cache_key.environment_file_fpath    

    def ENV_LABEL(self) -> str:
        """Get the full environment label extracted from the environment file name or OS environment.
        
        In FILE mode, the label is extracted from the filename: config._THIS_IS_XXXX_ENV_.sh
        In OS mode, the label is read from os.environ["ENV_LABEL"]
        
        Returns:
            Environment label string
            
        Examples:
            FILE mode:
                config._THIS_IS_DEV_ENV_.sh -> "DEV"
                config._THIS_IS_DEVJOHN_ENV_.sh -> "DEVJOHN"
                config._THIS_IS_PROD_ENV_.sh -> "PROD"
            OS mode:
                os.environ["ENV_LABEL"] = "PROD01" -> "PROD01"
        """
        if self.cache_key.environment_file_fpath == OS_ENVIRONMENT_SENTINEL:
            return os.environ["ENV_LABEL"]  # Should always exist at this point
        else:
            return self.cache_key.env_label()
    
    def ENV_TYPE(self) -> str:
        """Get the environment type prefix (DEV, TEST, STAGE, or PROD).
        
        Extracts the type prefix from the environment label. This is the standard
        environment category that determines which config class gets loaded.
        Works for both FILE mode (extracts from filename) and OS mode (extracts from ENV_LABEL).
        
        Side effect: Sets os.environ['ENV_TYPE'] to the returned value
        
        Returns:
            Environment type string: "DEV", "TEST", "STAGE", or "PROD"
            
        Examples:
            Label "DEV" -> type "DEV"
            Label "DEVJOHN" -> type "DEV"
            Label "PROD" -> type "PROD"
        """
        # Access ENV_LABEL from cache (it's already set during initialization)
        kv_cache = object.__getattribute__(self, '_kv_cache')
        if "ENV_LABEL" in kv_cache:
            env_label = kv_cache["ENV_LABEL"]
        else:
            # Fallback: get it directly (for FILE mode, use cache_key; for OS mode, use os.environ)
            if self.cache_key.environment_file_fpath == OS_ENVIRONMENT_SENTINEL:
                env_label = os.environ["ENV_LABEL"]
            else:
                env_label = self.cache_key.env_label()
        
        # Extract type prefix from label
        for etype in EnvironmentType:
            if env_label.startswith(etype.value):
                os.environ['ENV_TYPE'] = etype.value
                return etype.value
        # Should not happen if validation worked, but handle gracefully
        raise ValueError(f"Environment label '{env_label}' does not start with a valid EnvironmentType value {[e.value for e in EnvironmentType]}.")
    
    def CONFIG_CLASS_NAME(self) -> str:
        """Get the name of the configuration class being used.
        
        Returns:
            Class name string (e.g., "AppConfigDEV", "AppConfigPROD")
        """
        return self.cache_key.config_class_name
    
    def CONFIG_CLASS_FPATH(self) -> str:
        """Get the absolute path to the file defining the configuration class.
        
        Returns:
            Absolute path to config.py file (or this module if no custom config.py)
        """
        return self.cache_key.config_py_fpath

    def ENV_SOURCE(self) -> str:
        """Get the source of configuration (FILE or OS).
        
        Indicates whether configuration values were loaded from a confidential
        environment file (FILE mode) or from OS environment variables (OS mode).
        
        Returns:
            "FILE" if loaded from environment file, "OS" if from OS environment variables
            
        Examples:
            ```python
            if CONFIG["ENV_SOURCE"] == "OS":
                # Using OS-based configuration (deployment tool)
            else:
                # Using file-based configuration (local development)
            ```
        """
        # Access cache directly to avoid recursion during lazy evaluation
        kv_cache = object.__getattribute__(self, '_kv_cache')
        return kv_cache["ENV_SOURCE"]  # Set in _reset_cached_keyvals

    def abspath(self, *path_parts) -> str:
        """Construct absolute path relative to project root directory.
        
        Convenience method to build paths relative to your project root without
        hardcoding absolute paths in your code.
        
        Args:
            *path_parts: Path components to join (relative to project root)
            
        Returns:
            Absolute path: MASTER_ROOT_DIR / path_parts
            
        Example:
            ```python
            # If MASTER_ROOT_DIR is /Users/dave/project
            CONFIG.abspath("data", "input.csv")  # -> /Users/dave/project/data/input.csv
            CONFIG.abspath("logs")  # -> /Users/dave/project/logs
            ```
        """
        return os.path.join(self['MASTER_ROOT_DIR'], *path_parts)

    def dig(self, *path_parts) -> Any:
        """Safely traverse nested structures without raising KeyError/IndexError.
        
        Like Ruby's dig() method - traverses nested dicts, lists, and config objects,
        returning None if any part of the path doesn't exist instead of raising exceptions.
        
        Args:
            *path_parts: Keys (for dicts/config) or indices (for lists) to traverse
            
        Returns:
            Value at the specified path, or None if any part is missing
            
        Examples:
            ```python
            # Accessing nested dict from pyproject.toml
            name = CONFIG.dig("PYPROJECT_TOML", "project", "name")
            # Returns project name or None if pyproject.toml doesn't exist
            
            # Accessing list element
            first_dep = CONFIG.dig("PYPROJECT_TOML", "project", "dependencies", 0)
            # Returns first dependency or None if not found
            
            # Chaining through complex structures
            value = CONFIG.dig("SOME_DICT", "nested", "deeply", "key")
            # Returns value or None (never raises KeyError)
            ```
        """
        val = self
        for part in path_parts:
            try:
                # Handle different types of access
                if isinstance(val, (dict, self.__class__)):
                    # Dictionary or config object access
                    val = val[part] if isinstance(val, dict) else getattr(val, part)
                elif isinstance(val, (list, tuple)):
                    # Sequence access
                    if not isinstance(part, int):
                        return None
                    if 0 <= part < len(val):
                        val = val[part]
                    else:
                        return None
                else:
                    # Can't traverse further
                    return None
            except (KeyError, AttributeError, IndexError, TypeError):
                return None
        return val

    _cached_env_values: Dict[_AppConfigCacheKey, Dict[str,Any]] = {}  # class level caching

    def deduce_cache_key(self,config_py_fpath: str = None) -> None:
        """Initialize configuration by discovering and loading config files.
        
        Call this in your config class __init__() to trigger file discovery and loading.
        Searches upward from the given path to find config files, then loads all
        configuration values into the cache.
        
        Args:
            config_py_fpath: Starting path for file search (typically __file__ from your config.py)
            
        Usage:
            ```python
            class AppConfigDEV(AppConfigRootClass):
                def __init__(self):
                    self.deduce_cache_key(__file__)  # Required!
            ```
            
        Note:
            Not needed if using dynaload() to instantiate (dynaload handles this automatically).
        """
        # Needed?: Assert that the current class name matches the config class name deduced from the environment file?
        ck: _AppConfigCacheKey = _AppConfigCacheKey._search(config_py_fpath,config_py_fpath,self.__class__.__name__)  #DEV COMMENT: is this right?
        self.cache_key = ck
    
    @classmethod
    def dynaload(cls, search_paths: Union[str, Sequence[str]], require_config_file:bool = True) -> "AppConfigRootClass":
        """Automatically discover, load, and instantiate the appropriate config class (RECOMMENDED).
        
        Searches for environment file and config.py independently across multiple paths,
        using the first location where each file is found.
        
        This is the preferred way to get your configuration. It handles everything automatically:
        1. Searches upward from each path to find config._THIS_IS_XXXX_ENV_.sh
        2. Searches upward from each path to find config.py (optional)
        3. Determines which config class to load from the environment file name
        4. Dynamically imports and instantiates the config class
        5. Loads and caches all configuration values
        
        Args:
            search_paths: Single path or sequence of paths to search. For each file type
                         (environment file and config.py), searches are tried in order
                         and the first successful find is used. Each path is searched
                         upward through parent directories.
            require_config_file: If False, returns AppConfigRootClass when no config.py found
            
        Returns:
            Instance of the discovered config class (e.g., AppConfigDEV, AppConfigPROD)
            
        Raises:
            FileNotFoundError: If required files are not found during upward search
            AttributeError: If expected config class is not defined in config.py
            TypeError: If config class doesn't inherit from AppConfigRootClass
            
        Examples:
            ```python
            # Simple case - backward compatible
            from totodev_pub.app_config_root_class import AppConfigRootClass
            
            CONFIG = AppConfigRootClass.dynaload(__file__)
            database_url = CONFIG["DATABASE_URL"]
            
            # Test scenario - env in test dir, config.py fallback to src
            CONFIG = AppConfigRootClass.dynaload([
                "tests/envconfigs/TESTDISCONNECTED",
                "tests",
                "src"
            ])
            
            # Environment in one place, config.py in another
            CONFIG = AppConfigRootClass.dynaload([
                "tests/envconfigs/PROD01FAKE",  # Has env file
                "tests"                          # Has config.py
            ])
            ```
        """
        # Normalize to list
        paths = [search_paths] if isinstance(search_paths, str) else list(search_paths)
        
        # Search for environment file (first path that finds one wins)
        env_file = None
        config_file = None
        expected_config_class_name = None
        for path in paths:
            found = _AppConfigCacheKey.find_files_at_or_above(path, ENV_FILENAME_PATTERN)
            if len(found) == 1:
                env_file = found[0]
                break
            elif len(found) > 1:
                raise FileNotFoundError(
                    f"Found multiple environment files searching from {path}: {found}"
                )
        
        # Handle OS mode if no environment file found
        search_start_path = None
        if env_file is None:
            # Check for ENV_LABEL in os.environ (OS mode)
            if "ENV_LABEL" not in os.environ:
                raise FileNotFoundError(
                    "No confidential environment file found (config._THIS_IS_*_ENV_.sh) and ENV_LABEL "
                    "environment variable is not set in the operating system environment.\n\n"
                    "This configuration system supports two modes:\n\n"
                    "1. FILE-BASED CONFIGURATION (for local development):\n"
                    "   Create a confidential environment file at your project root with the pattern:\n"
                    "   config._THIS_IS_{ENV_LABEL}_ENV_.sh\n"
                    "   \n"
                    "   Example: config._THIS_IS_DEVALICE_ENV_.sh\n"
                    "   \n"
                    "   This file should contain your environment variables in shell format:\n"
                    "   export API_KEY=\"your_api_key\"\n"
                    "   export DATABASE_URL=\"postgresql://localhost/mydb\"\n"
                    "   etc.\n\n"
                    "2. OS-BASED CONFIGURATION (for automated deployment tools like Dokploy):\n"
                    "   If you are using an automated deployment tool that sets environment variables\n"
                    "   directly in the operating system (rather than using a file), you must:\n"
                    "   \n"
                    "   a. Set the ENV_LABEL environment variable before launching the application.\n"
                    "      Example: export ENV_LABEL=\"PROD01\"\n"
                    "      \n"
                    "   b. The ENV_LABEL value must start with one of: DEV, TEST, STAGE, or PROD\n"
                    "      Valid examples: \"PROD01\", \"TEST01\", \"DEVALICE\", \"STAGE\", \"PROD\"\n"
                    "      \n"
                    "   c. Define EXPECTED_KEYS in your config.py file (even if empty) to explicitly\n"
                    "      declare which environment variables should be loaded from the OS.\n"
                    "      \n"
                    "   d. Set all environment variables declared in EXPECTED_KEYS in your deployment\n"
                    "      tool's environment variable configuration.\n\n"
                    "If you are deploying via an automated tool (like Dokploy), copy all the export\n"
                    "statements from your confidential environment file to your deployment tool's\n"
                    "environment variable settings, and ensure ENV_LABEL is set appropriately."
                )
            
            env_label = os.environ["ENV_LABEL"]
            
            # Validate ENV_LABEL format (must start with valid EnvironmentType)
            valid_prefix = False
            for etype in EnvironmentType:
                if env_label.startswith(etype.value):
                    valid_prefix = True
                    break
            
            if not valid_prefix:
                raise ValueError(
                    f"The ENV_LABEL environment variable value \"{env_label}\" is invalid.\n\n"
                    "No confidential environment file (config._THIS_IS_*_ENV_.sh) was found, so the\n"
                    "system is attempting to use OS-based configuration (typically for automated\n"
                    "deployment tools like Dokploy).\n\n"
                    "The ENV_LABEL environment variable must start with one of: DEV, TEST, STAGE, or PROD\n\n"
                    "Valid examples:\n"
                    "  - \"PROD01\" or \"PROD\" for production environments\n"
                    "  - \"TEST01\" or \"TEST\" for test environments\n"
                    "  - \"DEVALICE\" or \"DEV\" for development environments\n"
                    "  - \"STAGE\" for staging environments\n\n"
                    f"Current ENV_LABEL value: \"{env_label}\"\n\n"
                    "If you are using an automated deployment tool, please update the ENV_LABEL\n"
                    "environment variable in your deployment tool's configuration to a valid value."
                )
            
            # Search for config.py (required for OS mode)
            config_file = None
            for path in paths:
                found = _AppConfigCacheKey.find_files_at_or_above(path, CONFIG_FILENAME_PATTERN)
                if len(found) == 1:
                    config_file = found[0]
                    break
                elif len(found) > 1:
                    raise FileNotFoundError(
                        f"Found multiple config files searching from {path}: {found}"
                    )
            
            if config_file is None:
                raise FileNotFoundError(
                    "No confidential environment file (config._THIS_IS_*_ENV_.sh) was found, and the\n"
                    "system is attempting to use OS-based configuration (typically for automated\n"
                    "deployment tools like Dokploy).\n\n"
                    "However, no config.py file was found. The config.py file is required for OS-based\n"
                    "configuration because it must define EXPECTED_KEYS to explicitly declare which\n"
                    "environment variables should be loaded from the operating system.\n\n"
                    "When using OS-based configuration:\n"
                    "  1. You must have a config.py file that defines your configuration classes\n"
                    "  2. Your configuration class must define EXPECTED_KEYS (even if empty)\n"
                    "  3. All environment variables declared in EXPECTED_KEYS must be set in your\n"
                    "     deployment tool's environment variable configuration\n\n"
                    "If you are using an automated deployment tool (like Dokploy):\n"
                    "  - Ensure your config.py file is checked into Git (it should be, unlike the\n"
                    "    confidential environment file)\n"
                    "  - Ensure your deployment tool deploys the config.py file from Git\n"
                    "  - In your config.py, define EXPECTED_KEYS in your configuration class\n"
                    "  - Set all variables from EXPECTED_KEYS in your deployment tool's environment\n"
                    "    variable settings\n\n"
                    "Example config.py structure for OS-based configuration:\n"
                    "    from totodev_pub.app_config_root_class import AppConfigRootClass\n"
                    "    \n"
                    "    class AppConfigPROD01(AppConfigRootClass):\n"
                    "        EXPECTED_KEYS = [\"API_KEY\", \"DATABASE_URL\", \"SECRET_KEY\"]\n"
                    "        # Other configuration can go here too"
                )
            
            expected_config_class_name = f"AppConfig{env_label}"
            
            # Load config module dynamically to validate EXPECTED_KEYS exists
            spec = importlib.util.spec_from_file_location("config", config_file)
            config_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(config_module)
            
            try:
                config_class = getattr(config_module, expected_config_class_name)
            except AttributeError:
                raise AttributeError(f"Expecting a class named {expected_config_class_name} in the config.py file at {config_file}.")
            
            if not issubclass(config_class, AppConfigRootClass):
                raise TypeError(f"Expecting class {expected_config_class_name} to inherit from AppConfigRootClass in the config.py file at {config_file}.")
            
            # Check if EXPECTED_KEYS is defined in the class hierarchy
            has_expected_keys = False
            for c in config_class.__mro__:
                if hasattr(c, 'EXPECTED_KEYS'):
                    has_expected_keys = True
                    break
            
            if not has_expected_keys:
                raise AttributeError(
                    f"No confidential environment file (config._THIS_IS_*_ENV_.sh) was found, and the\n"
                    "system is attempting to use OS-based configuration (typically for automated\n"
                    "deployment tools like Dokploy).\n\n"
                    f"However, the configuration class '{expected_config_class_name}' does not define EXPECTED_KEYS.\n\n"
                    "When using OS-based configuration, EXPECTED_KEYS must be explicitly defined in\n"
                    "your config.py file (even if it's an empty list or dict) to prevent accidental\n"
                    "triggering of OS mode and to clearly declare which environment variables should\n"
                    "be loaded from the operating system.\n\n"
                    "To fix this, add EXPECTED_KEYS to your config class:\n\n"
                    "Example 1 (list format):\n"
                    "    class AppConfigPROD01(AppConfigRootClass):\n"
                    "        EXPECTED_KEYS = [\"API_KEY\", \"DATABASE_URL\", \"SECRET_KEY\"]\n\n"
                    "Example 2 (dict format with help text):\n"
                    "    class AppConfigPROD01(AppConfigRootClass):\n"
                    "        EXPECTED_KEYS = {\n"
                    "            \"API_KEY\": \"External API authentication key\",\n"
                    "            \"DATABASE_URL\": \"PostgreSQL connection string\",\n"
                    "            \"SECRET_KEY\": \"Application secret for session signing\"\n"
                    "        }\n\n"
                    "Example 3 (explicitly empty - valid but means no env vars will be loaded):\n"
                    "    class AppConfigPROD01(AppConfigRootClass):\n"
                    "        EXPECTED_KEYS = []\n\n"
                    "If you intended to use file-based configuration, create a confidential environment\n"
                    f"file at your project root: config._THIS_IS_{env_label}_ENV_.sh"
                )
            
            # Set up OS mode
            env_file = OS_ENVIRONMENT_SENTINEL
            search_start_path = os.path.abspath(paths[0] if os.path.isdir(paths[0]) else os.path.dirname(paths[0]))
        else:
            # File mode - search for config.py
            for path in paths:
                found = _AppConfigCacheKey.find_files_at_or_above(path, CONFIG_FILENAME_PATTERN)
                if len(found) == 1:
                    config_file = found[0]
                    break
                elif len(found) > 1:
                    raise FileNotFoundError(
                        f"Found multiple config files searching from {path}: {found}"
                    )
            
            if config_file is None and require_config_file:
                raise FileNotFoundError(
                    f"No config file ({CONFIG_FILENAME_PATTERN}) found searching paths: {paths}"
                )
            
            # Extract expected class name from environment file
            env_label = re.match(ENV_FILENAME_RX, os.path.basename(env_file)).group(1)
            expected_config_class_name = f"AppConfig{env_label}"
            
            # If require_config_file=False and we found a config.py via upward search,
            # verify that the expected class exists in that file. If not, treat it as
            # if no config.py was found (fall back to AppConfigRootClass).
            if config_file is not None and not require_config_file:
                # Check if the expected class exists in the found config.py
                try:
                    spec = importlib.util.spec_from_file_location("config", config_file)
                    config_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(config_module)
                    if not hasattr(config_module, expected_config_class_name):
                        # Expected class doesn't exist in this config.py - treat as if no config.py found
                        config_file = None
                        expected_config_class_name = "AppConfigRootClass"
                except Exception:
                    # If we can't load/check the config file, treat as if no config.py found
                    config_file = None
                    expected_config_class_name = "AppConfigRootClass"
        
        # Create cache key
        ck = _AppConfigCacheKey(
            environment_file_fpath=env_file,
            config_py_fpath=config_file or __file__,
            config_class_name=expected_config_class_name if (config_file or env_file == OS_ENVIRONMENT_SENTINEL) else "AppConfigRootClass",
            search_start_path=search_start_path
        )
        
        # Instantiate config class
        if ck.has_config_file():
            cfg = ck._load_config_class()()  # instance of the config class
        else:
            cfg = AppConfigRootClass()  # instance of the generic config class, unbound to a config file
        
        if not cfg.has_cache_key():
            cfg.cache_key = ck
        
        # Warn if EXPECTED_KEYS is not declared to encourage good practices
        cfg._warn_if_no_expected_keys()
        
        return cfg


    @property
    def cache_key(self) -> _AppConfigCacheKey:
        """Get the cache key identifying this configuration's source files.
        
        Returns:
            Cache key containing paths to environment file, config file, and class name
            
        Raises:
            AttributeError: If cache key not initialized (forgot to call deduce_cache_key?)
        """
        try:
            return self._ckey
        except AttributeError:
            raise AttributeError(f"The instance attribute cache_key should be set or the deduce_cache_key() called within the __init__() method of your derived config class [{self.__class__.__name__}].")

    @cache_key.setter
    def cache_key(self, new_cache_key_val: _AppConfigCacheKey):
        """Set the cache key and trigger configuration loading (internal use).
        
        Setting the cache key triggers the complete configuration loading process:
        - Loads environment file variables
        - Collects class attributes and methods
        - Parses packed values
        - Validates EXPECTED_KEYS
        - Caches all values
        
        This should only be called once per instance (usually automatically by
        deduce_cache_key() or dynaload()).
        
        Args:
            new_cache_key_val: Cache key identifying config files to load
            
        Raises:
            AttributeError: If cache key already set (can't set twice)
            TypeError: If value is not an _AppConfigCacheKey instance
        """
        if hasattr(self, "_ckey"):
            raise AttributeError("The cache key should not be set more than once.")
        if not isinstance(new_cache_key_val, _AppConfigCacheKey):
            raise TypeError("The new cache key value must be an instance of AppConfigCacheKey.")
        self._reset_cached_keyvals(new_cache_key_val)

    @staticmethod
    def _decompose_packed_kv(packed_kv: str, parent_key_name:str) -> Union[Dict[str,str], List[str]]:
        """Parse packed key-value strings into dictionaries or lists.
        
        Packed strings allow complex data structures in environment variables.
        Format: starts with delimiter, then delimited entries.
        
        Returns dict if entries contain '=', otherwise returns list.
        
        Examples:
            "|key1=val1|key2=val2|" -> {"key1": "val1", "key2": "val2"}
            "~item1~item2~item3~" -> ["item1", "item2", "item3"]
            
        Args:
            packed_kv: The packed string to parse
            parent_key_name: Key name (for error messages)
            
        Raises:
            ValueError: If string doesn't start with non-alphanumeric delimiter
        """
        if not packed_kv or packed_kv[0].isalnum():
            raise ValueError(f"The value underneath the key ['{parent_key_name}'] has the indicator that is 'packed' (e.g. key name ends in '_PKD'). The packed string must start with a non-alphanumeric delimiter character.")
        
        delimiter = packed_kv[0]
        entries = [entry for entry in packed_kv[1:].split(delimiter) if entry.strip()]
        
        if not entries:
            # When empty (only delimiters, no content), default to empty dict
            # since dict-style PKD is more common and empty dict is more useful than empty list
            return {}
            
        # If first non-empty entry contains '=', treat as key-value pairs
        if '=' in entries[0]:
            return {k.strip():v.strip() for k,v in (pair.split("=",1) for pair in entries if pair.strip())}
        
        # Otherwise treat as list
        return entries

    @classmethod
    def _load_env_vars(cls,env_vars_fpath) -> Dict[str,str]:
        """Load and normalize environment variables from the environment file.
        
        Reads the .sh environment file, loads variables into os.environ, and returns
        a normalized dictionary suitable for configuration access.
        
        Normalization:
            - CamelCase -> CAMEL_CASE (inserts underscores before capital letters)
            - Variables starting with _ are excluded (private)
            - Variables in DO_NOT_AUTOPORT are excluded
            
        Args:
            env_vars_fpath: Path to config._THIS_IS_*_ENV_.sh file
            
        Returns:
            Dictionary of normalized configuration keys and their values
        """
        env_vals = dotenv_values(env_vars_fpath)
        load_dotenv(env_vars_fpath)  # load the environment variables into the os.environ dictionary
        # Before first access, import the environment variables into the class namespace
        dont_autoport = [var.strip() for var in env_vals.get("DO_NOT_AUTOPORT", "").split(",")]
        normalized_kv = {}
        for key, val in env_vals.items():
            if key == "DO_NOT_AUTOPORT":
                continue # skip the DO_NOT_AUTOPORT variable itself
            if key.startswith("_") or key in dont_autoport:
                continue
            const_name = re.sub(r"(?<!^)(([A-Z])[a-z])", r"_\1", key).upper() #camel case -> insert underscores
            # and also put the value into a class variable with same name
            normalized_kv[const_name] = val
        return normalized_kv

    @classmethod
    def _load_env_vars_from_os(cls, config_class: type) -> Dict[str, str]:
        """Load environment variables from os.environ for keys declared in EXPECTED_KEYS.
        
        This method is used in OS mode (when no confidential environment file exists).
        It loads only the keys declared in EXPECTED_KEYS from the operating system
        environment variables.
        
        Args:
            config_class: The config class to get EXPECTED_KEYS from (walk __mro__)
        
        Returns:
            Dictionary of normalized configuration keys and their values
        
        Raises:
            ConfigurationError: If any EXPECTED_KEYS are missing from os.environ
        """
        # Collect EXPECTED_KEYS from class hierarchy
        expected_keys = []
        help_text = {}
        for c in config_class.__mro__:
            if hasattr(c, 'EXPECTED_KEYS'):
                keys = c.EXPECTED_KEYS
                if isinstance(keys, dict):
                    expected_keys.extend(keys.keys())
                    help_text.update({k: v for k, v in keys.items() if v is not None})
                elif isinstance(keys, list):
                    expected_keys.extend(keys)
        
        # Deduplicate
        expected_keys = list(set(expected_keys))
        
        # Load values from os.environ
        env_vars_dict = {}
        missing_keys = []
        
        for key in expected_keys:
            # In OS mode, use exact matching only (no normalization).
            # This differs from file mode where variable names are normalized (CamelCase -> CAMEL_CASE).
            # The difference is deliberate: OS mode uses exact matching for simplicity and explicitness,
            # since EXPECTED_KEYS explicitly declares what should be loaded and deployment tools set
            # environment variables with specific names.
            if key in os.environ:
                env_vars_dict[key] = os.environ[key]
            else:
                missing_keys.append(key)
        
        # If any keys are missing, raise ConfigurationError
        if missing_keys:
            error_msg = (
                "Attempting to source configuration from OS environment variables (OS-based mode,\n"
                "typically used with automated deployment tools like Dokploy), but the following\n"
                "required configuration keys are missing from the operating system environment:\n"
                f"{missing_keys}\n\n"
                "These keys are declared in EXPECTED_KEYS in your config.py file but were not\n"
                "found in os.environ when the application started.\n\n"
                "If you are using an automated deployment tool (such as Dokploy):\n"
                "  1. Open your confidential environment file (config._THIS_IS_*_ENV_.sh) from\n"
                "     your local development setup\n"
                "  2. Copy each \"export KEY=value\" line to your deployment tool's environment\n"
                "     variable configuration\n"
                "  3. Ensure all variables declared in EXPECTED_KEYS are set in the deployment tool\n"
                "  4. Redeploy the application\n\n"
                f"Missing required keys: {missing_keys}"
            )
            
            # Add help text if available
            help_msgs = []
            for key in missing_keys:
                if key in help_text:
                    help_msgs.append(f"{key}: {help_text[key]}")
            
            if help_msgs:
                error_msg += f"\nHelp: {'; '.join(help_msgs)}"
            
            raise ConfigurationError(error_msg)
        
        return env_vars_dict

    def env_sourced_keys(self) -> Dict[str, str]:
        """Get mapping of normalized config keys to original environment variable names.
        
        Shows which configuration keys came from the environment file (FILE mode) or OS
        environment (OS mode) and what their original variable names were before normalization.
        Useful for debugging name normalization or finding the source of configuration values.
        
        In FILE mode, returns mapping of normalized keys to original variable names from the
        environment file (handles CamelCase -> CAMEL_CASE normalization).
        
        In OS mode, returns mapping of EXPECTED_KEYS to themselves (no normalization occurs
        in OS mode, so keys match exactly).
        
        Returns:
            Dict mapping normalized key names (UPPER_CASE) to original env var names
            
        Examples:
            ```python
            # FILE mode - Environment file has: export CamelCase="value"
            # After normalization: CONFIG["CAMEL_CASE"] == "value"
            mapping = CONFIG.env_sourced_keys()
            # Returns: {"CAMEL_CASE": "CamelCase"}
            
            # FILE mode - Environment file has: export DATABASE_URL="..."
            mapping = CONFIG.env_sourced_keys()
            # Returns: {"DATABASE_URL": "DATABASE_URL"}
            
            # OS mode - EXPECTED_KEYS = ["API_KEY", "DATABASE_URL"]
            mapping = CONFIG.env_sourced_keys()
            # Returns: {"API_KEY": "API_KEY", "DATABASE_URL": "DATABASE_URL"}
            ```
        """
        if self.cache_key.environment_file_fpath == OS_ENVIRONMENT_SENTINEL:
            # OS mode: return mapping of EXPECTED_KEYS to themselves (no normalization in OS mode)
            expected_keys, _ = self._get_expected_keys()
            return {key: key for key in expected_keys if key in self._kv_cache}
        
        # FILE mode: existing logic with normalization
        env_vals = dotenv_values(self.ENV_FPATH())
        dont_autoport = [var.strip() for var in env_vals.get("DO_NOT_AUTOPORT", "").split(",")]
        result = {}
        
        for orig_key in env_vals:
            if orig_key == "DO_NOT_AUTOPORT" or orig_key.startswith("_") or orig_key in dont_autoport:
                continue
            normalized_key = re.sub(r"(?<!^)(([A-Z])[a-z])", r"_\1", orig_key).upper()
            if normalized_key in self._kv_cache:  # Only include if it made it into our instance
                result[normalized_key] = orig_key
                
        return result

    class _LazyEvalMarker():
        """Internal marker indicating a configuration value needs lazy evaluation.
        
        When a configuration method hasn't been called yet, it's stored in the cache
        as an instance of this marker class. On first access, the method is evaluated,
        the result is cached, and the marker is replaced with the actual value.
        """
        pass

    def has_cache_key(self) -> bool:
        """Check if the cache key has been initialized.
        
        Returns True if the cache key has been set, False otherwise.
        Mainly useful for custom config class implementations.
        """
        # Use object.__getattribute__ to avoid recursion through __getattr__
        return '_ckey' in object.__getattribute__(self, '__dict__')

    def files_were_updated(self) -> List[str]:
        """Check if any configuration files have been modified since initialization.
        
        Returns:
            List of absolute paths to modified files, empty if none changed.
        """
        return self.cache_key.files_were_updated()

    def newest_loaded(self) -> Optional[datetime]:
        """Get the most recent modification time among loaded configuration files."""
        return self.cache_key.newest_loaded()

    @staticmethod
    def _find_pyproject_toml_upward(start_dir: str, max_levels: int = 10) -> Optional[str]:
        """Search upward from start_dir for pyproject.toml.
        
        Args:
            start_dir: Starting directory path
            max_levels: Maximum number of parent directories to search
            
        Returns:
            Absolute path to pyproject.toml if found, None otherwise
        """
        current = os.path.abspath(start_dir)
        for _ in range(max_levels):
            toml_path = os.path.join(current, "pyproject.toml")
            if os.path.exists(toml_path):
                return toml_path
            parent = os.path.dirname(current)
            if parent == current:  # Reached filesystem root
                break
            current = parent
        return None

    def _reset_cached_keyvals(self, ckey: _AppConfigCacheKey) -> None:
        """Build the complete configuration cache from all sources.
        
        This is the core loading method that assembles configuration from:
        1. Environment file/OS variables (base layer, can override class attributes)
        2. Class attributes from inheritance hierarchy (default layer)
        3. Methods with ALL_CAPS names (calculated layer, lazy evaluated, highest priority)
        
        Process:
        - Loads environment file and normalizes variable names
        - Parses _PKD (packed) values into dicts/lists
        - Loads pyproject.toml if present
        - Walks class hierarchy to collect attributes and methods
        - Marks methods for lazy evaluation (evaluated on first access)
        - Forces evaluation of all methods to cache values
        - Validates EXPECTED_KEYS if declared
        
        Side effects:
        - Sets self._ckey and self._kv_cache
        - Loads variables into os.environ
        - Updates class-level cache _cached_env_values
        
        Args:
            ckey: Cache key identifying which config files to load
        """
        assert isinstance(ckey,_AppConfigCacheKey)
        self._ckey = ckey
        cls = self.__class__

        if ckey in cls._cached_env_values:
            self._kv_cache = cls._cached_env_values[ckey]
            return cls._cached_env_values[ckey]

        # Determine ENV_SOURCE
        if ckey.environment_file_fpath == OS_ENVIRONMENT_SENTINEL:
            env_source = "OS"
        else:
            env_source = "FILE"
            # Warn if ENV_LABEL is set in OS environment (it will be ignored in FILE mode)
            if "ENV_LABEL" in os.environ and os.environ["ENV_LABEL"]:
                import warnings
                warnings.warn(
                    f"ENV_LABEL environment variable is set to '{os.environ['ENV_LABEL']}' but will be ignored "
                    f"because configuration is being loaded from file mode (confidential environment file found at "
                    f"{ckey.environment_file_fpath}). The environment label will be determined from the environment "
                    f"file name instead.",
                    UserWarning
                )
        
        # Load environment variables
        if env_source == "OS":
            # OS mode: load from os.environ for EXPECTED_KEYS
            env_vars_dict = cls._load_env_vars_from_os(self.__class__)
        else:
            # FILE mode: load from environment file
            env_vars_dict = cls._load_env_vars(ckey.environment_file_fpath)
        
        # Add ENV_SOURCE to env_vars_dict
        env_vars_dict["ENV_SOURCE"] = env_source
        
        # Process any _PKD values in environment variables
        for key in list(env_vars_dict.keys()):
            if key.endswith("_PKD") and not isinstance(env_vars_dict[key], cls._LazyEvalMarker):
                val = env_vars_dict[key]
                if not isinstance(val, dict):  # Only parse if not already a dictionary
                    env_vars_dict[key] = cls._decompose_packed_kv(val, key)
        
        # Search upward for pyproject.toml
        if env_source == "OS":
            # OS mode: search from search_start_path or cwd
            pyproject_fpath = None
            if ckey.search_start_path:
                pyproject_fpath = cls._find_pyproject_toml_upward(ckey.search_start_path)
            if not pyproject_fpath:
                pyproject_fpath = cls._find_pyproject_toml_upward(os.getcwd())
        else:
            # FILE mode: search upward from environment file directory
            env_dir = os.path.dirname(ckey.environment_file_fpath)
            pyproject_fpath = cls._find_pyproject_toml_upward(env_dir)
        if pyproject_fpath:
            if tomllib is not None:
                with open(pyproject_fpath, "rb") as f:  # Open in binary mode as required by tomllib
                    env_vars_dict["PYPROJECT_TOML"] = tomllib.load(f)
            else:
                import warnings
                warnings.warn(
                    f"pyproject.toml found at {pyproject_fpath} but cannot be loaded. "
                    f"TOML parsing requires Python 3.11+. Current Python version does not include tomllib. "
                    f"PYPROJECT_TOML will not be available in configuration.",
                    UserWarning
                )

        # Next, process class attributes
        method_list = [] # all instance methods with all-caps names
        cls_vars_dict = {}
        for c in cls.__mro__:
            for attr_nm in dir(c):
                if attr_nm.upper() == attr_nm and attr_nm not in cls_vars_dict:  # only capitalized names are presumed to be config values
                    attr = getattr(c, attr_nm)
                    if callable(attr):
                        # Skip ENV_SOURCE - it's already in env_vars_dict and shouldn't be lazily evaluated
                        if attr_nm != "ENV_SOURCE":
                            cls_vars_dict[attr_nm] = cls._LazyEvalMarker()  # mark for later evaluation
                            method_list.append(attr_nm)
                    else:
                        # Process _PKD values from class attributes immediately
                        if attr_nm.endswith("_PKD") and not isinstance(attr, (dict, cls._LazyEvalMarker)):
                            if not isinstance(attr, dict):  # Only parse if not already a dictionary
                                attr = cls._decompose_packed_kv(attr, attr_nm)
                        cls_vars_dict[attr_nm] = attr

        # put the calculated values in the cache, overwriting any existing values
        # But preserve ENV_SOURCE from env_vars_dict since it's a special value, not a lazy method
        # Environment values override class attributes (env wins)
        # BUT: Methods (marked with _LazyEvalMarker) have highest priority and should not be overwritten
        merged = cls_vars_dict | env_vars_dict
        # Restore any methods that were overwritten by env values (methods have highest priority)
        for key, value in cls_vars_dict.items():
            if isinstance(value, cls._LazyEvalMarker):
                merged[key] = value  # Method overrides env value
        if "ENV_SOURCE" in env_vars_dict:
            merged["ENV_SOURCE"] = env_vars_dict["ENV_SOURCE"]
        cls._cached_env_values[ckey] = merged
        
        # Note, because of the way this works, recalc will not alter the cache of other, already created instances
        self._kv_cache = cls._cached_env_values[ckey]  # set the cache for any other references

        # Generate warnings for overridden code values (before methods are evaluated)
        self._generate_override_warnings(env_vars_dict, cls_vars_dict)

        # now force caching of the lazy-evaluated methods
        for method_name in method_list:
            # beware circular references
            self.__getitem__(method_name) # force a retrieval and caching of the methods
        
        # validate expected configuration after all loading is complete
        self._validate_expectations()

    def __setitem__(self, key: str, value: str) -> None:
        # Prevent modification of uppercase configuration keys
        if key.isupper():
            raise AttributeError("Configuration values (uppercase keys) are read-only")
        self.__dict__[key] = value

    def __delitem__(self, key: str) -> None:
        # Prevent deletion of uppercase configuration keys
        if key.isupper():
            raise AttributeError("Configuration values (uppercase keys) are read-only")
        del self.__dict__[key]

    def __setattr__(self, key: str, value: Any) -> None:
        # Prevent modification of uppercase configuration attributes
        if key.isupper() and not key.startswith('_'):
            raise AttributeError("Configuration values (uppercase keys) are read-only")
        object.__setattr__(self, key, value)

    def __getattribute__(self, key: str) -> Any:
        # Intercept attribute access to provide configuration values for uppercase keys
        # when accessed as attributes (not method calls)
        # These names must use object.__getattribute__ to avoid recursion
        if key in ('_kv_cache', '_ckey', '__dict__', '__class__', 'ENV_FPATH'):
            return object.__getattribute__(self, key)
            
        # For uppercase keys that match their uppercase version, try to get config value
        if key == key.upper():
            try:
                # Get the frame where this attribute was accessed
                import inspect
                frame = inspect.currentframe()
                if frame:
                    try:
                        next_frame = frame.f_back
                        if next_frame:
                            # Check if this is being called as a function
                            # The code object will have '(' as the next character if it's a function call
                            code_str = next_frame.f_code.co_code[next_frame.f_lasti + 1:next_frame.f_lasti + 2]
                            if not code_str or code_str != b'(':  # Not a function call
                                # Before allowing attribute access, check if this key is a method
                                # Methods should only be accessible via dictionary-style access
                                cls = object.__getattribute__(self, '__class__')
                                for c in cls.__mro__:
                                    if hasattr(c, key):
                                        attr = getattr(c, key)
                                        if callable(attr) and not isinstance(attr, type):
                                            # This is a method - attribute access should fail
                                            raise AttributeError(
                                                f"Configuration methods (like '{key}') cannot be accessed with "
                                                f"attribute notation. Use dictionary-style access instead: "
                                                f"CONFIG['{key}']"
                                            )
                                
                                # Not a method, so allow attribute access
                                # Use object.__getattribute__ to avoid recursion
                                get_value = object.__getattribute__(self, '_get_value')
                                return get_value(key)
                    finally:
                        del frame  # Always delete frame references to prevent reference cycles
                
            except KeyError:
                # If the key isn't in our config, fall through to normal attribute access
                pass
                
        # For all other cases, use normal attribute access
        return object.__getattribute__(self, key)

    def _get_value(self, key: str) -> str:
        """Retrieve a configuration value from cache, evaluating methods lazily.
        
        This is the internal getter that powers both [] access and attribute access.
        
        Behavior:
        - If value is cached and ready, returns it immediately
        - If value is a _LazyEvalMarker (unevaluated method), calls the method,
          caches the result, and returns it
        - Handles _PKD parsing for method return values
        
        Args:
            key: Configuration key name
            
        Returns:
            Configuration value
            
        Raises:
            KeyError: If cache key not initialized or key not found
            
        Note:
            Uses object.__getattribute__ throughout to avoid recursion.
        """
        if not object.__getattribute__(self, 'has_cache_key')():
            raise KeyError("Cache key is not set. Did you forget to call deduce_cache_key()?")

        if not self.__contains__(key):
            raise KeyError(f"Key '{key}' was not found in the config key-value pairs. Available keys are: {list(self.keys())}")
            
        kv_cache = object.__getattribute__(self, '_kv_cache')
        val = kv_cache[key]
        
        # During early initialization, keys that are methods are marked for lazy evaluation.  If the key is a method, evaluate it now.
        if isinstance(val, object.__getattribute__(self, '_LazyEvalMarker')):
            meth = object.__getattribute__(self, key)  # force evaluation of the method
            val = meth()
            # Process _PKD values from lazy-evaluated methods
            if key.endswith("_PKD") and not isinstance(val, dict):
                val = object.__getattribute__(self, '_decompose_packed_kv')(val, key)
            kv_cache[key] = val  # cache the returned value of the method
        return val

    def __getitem__(self, key: str) -> str:
        """Get configuration value by key (dictionary-style access).
        
        Args:
            key: Configuration key name
            
        Returns:
            Configuration value
            
        Raises:
            KeyError: If key is not found in configuration
        """
        return self._get_value(key)

    def keys(self) -> Generator[str, None, None]:
        """Get all available configuration keys."""
        self.cache_key  # force an error if the cache key is not set
        
        for k in self._kv_cache.keys():
            yield k

    def missing_vars(self, *keynames: str) -> List[str]:
        """Check which configuration keys are missing from configuration.
        
        Useful for checking optional configuration before use, or for debugging.
        
        Args:
            *keynames: Configuration key names to check for existence
            
        Returns:
            List of key names that don't exist (empty list if all present)
            
        Example:
            ```python
            missing = CONFIG.missing_vars("DATABASE_URL", "API_KEY", "DEBUG_MODE")
            if missing:
                print(f"Warning: Missing optional config: {missing}")
            ```
        """
        self.cache_key
        return [k for k in keynames if k not in self._kv_cache]


    def to_dict(self) -> dict:
        """Get a read-only dictionary view of all configuration settings.
        
        Returns an immutable MappingProxyType containing all configuration keys and values.
        Useful for passing configuration to functions expecting a dict, or for debugging.
        
        Returns:
            Read-only dict-like object with all configuration
            
        Note:
            The returned object is read-only - attempts to modify it will raise TypeError.
            
        Example:
            ```python
            all_config = CONFIG.to_dict()
            print(f"All settings: {all_config}")
            # all_config["NEW_KEY"] = "value"  # Raises TypeError
            ```
        """
        self.cache_key  # force an error if the cache key is not set
        return types.MappingProxyType(self._kv_cache) 

    def __contains__(self, keyname: str) -> bool:
        """Check if a configuration key exists.
        
        Args:
            keyname: Configuration key to check
            
        Returns:
            True if key exists, False otherwise
        """
        self.cache_key  # force an error if the cache key is not set
        return keyname in self._kv_cache


    def set_env(self,source_keyname:str,as_env_keyname:Optional[str] = None) -> None:
        """Copy a configuration value into an os.environ variable.
        
        Useful for passing configuration to subprocesses or libraries that read from os.environ.
        
        Args:
            source_keyname: Configuration key to get the value from
            as_env_keyname: Environment variable name (defaults to source_keyname)
            
        Raises:
            KeyError: If source_keyname is not found in configuration
            
        Example:
            ```python
            CONFIG.set_env("DATABASE_URL")  # Sets os.environ["DATABASE_URL"]
            CONFIG.set_env("API_KEY", "EXTERNAL_API_KEY")  # Sets os.environ["EXTERNAL_API_KEY"]
            ```
        """
        if source_keyname not in self:
            raise KeyError(f"Key '{source_keyname}' was not found in the config key-value pairs.  Values that are present are: {list(self.keys())}")
        if as_env_keyname is None:
            as_env_keyname = source_keyname
        os.environ[as_env_keyname] = self[source_keyname]
    
    def _get_expected_keys(self) -> Tuple[List[str], Dict[str, str]]:
        """Collect all expected keys declared across the class inheritance hierarchy.
        
        Walks up the class hierarchy (via __mro__) collecting EXPECTED_KEYS from each class.
        Subclass requirements are combined with parent requirements (not replaced).
        
        Supports two EXPECTED_KEYS formats:
        - List: ["KEY1", "KEY2"] - keys without help text
        - Dict: {"KEY1": "help text", "KEY2": "help text"} - keys with descriptions
        
        Returns:
            Tuple of (deduplicated list of required keys, dict of help text for keys)
            
        Example:
            class Parent(AppConfigRootClass):
                EXPECTED_KEYS = ["DATABASE_URL"]
            
            class Child(Parent):
                EXPECTED_KEYS = {"API_KEY": "External API key"}
            
            # Child._get_expected_keys() returns:
            # (["DATABASE_URL", "API_KEY"], {"API_KEY": "External API key"})
        """
        expected_keys = []
        help_text = {}
        
        for cls in self.__class__.__mro__:
            if hasattr(cls, 'EXPECTED_KEYS'):
                keys = cls.EXPECTED_KEYS
                if isinstance(keys, dict):
                    expected_keys.extend(keys.keys())
                    help_text.update({k: v for k, v in keys.items() if v is not None})
                elif isinstance(keys, list):
                    expected_keys.extend(keys)
        
        return list(set(expected_keys)), help_text
    
    def _warn_if_no_expected_keys(self) -> None:
        """Issue warning if no EXPECTED_KEYS declared in any class in hierarchy.
        
        This encourages good configuration practices by reminding developers to
        explicitly declare their required configuration keys. The warning is just
        a nudge - not declaring EXPECTED_KEYS is allowed.
        """
        # Check if any class in the inheritance hierarchy has EXPECTED_KEYS declared
        has_expected_keys_declared = False
        for cls in self.__class__.__mro__:
            if hasattr(cls, 'EXPECTED_KEYS'):
                has_expected_keys_declared = True
                break
        
        if not has_expected_keys_declared:
            import warnings
            warning_msg = (
                f"Configuration class '{self.__class__.__name__}' does not declare EXPECTED_KEYS. "
                f"Consider adding EXPECTED_KEYS to declare required configuration keys from the environment file. "
                f"This helps catch missing configuration early and provides better error messages. "
                f"Example: EXPECTED_KEYS = ['DATABASE_URL', 'API_KEY'] or "
                f"EXPECTED_KEYS = {{'DATABASE_URL': 'Database connection string'}}"
            )
            warnings.warn(warning_msg, UserWarning, stacklevel=3)
    
    def _validate_expectations(self) -> None:
        """Validate that all EXPECTED_KEYS are present in configuration.
        
        Called automatically during configuration loading. Checks that every key
        declared in EXPECTED_KEYS (across the entire inheritance hierarchy) exists
        in the loaded configuration.
        
        Behavior depends on expect_policy:
        - "error" (default): Raises ConfigurationError if keys missing
        - "warning": Issues UserWarning if keys missing but continues
        - None: Same as "error"
        
        If no EXPECTED_KEYS declared, validation is skipped.
        
        Raises:
            ConfigurationError: If required keys missing and expect_policy is "error"
        """
        # Determine effective policy (None = "error" behavior)
        policy = self.expect_policy if self.expect_policy is not None else "error"
        
        # Get expected keys and help text from inheritance hierarchy
        expected_keys, expected_help = self._get_expected_keys()
        
        # If no expected keys are defined, skip validation
        if not expected_keys:
            return
        
        # Check for missing expected keys
        missing_keys = []
        for key in expected_keys:
            if key not in self:
                missing_keys.append(key)
        
        # Handle missing keys based on policy
        if missing_keys:
            error_msg = self._format_missing_keys_error(missing_keys, expected_help)
            
            if policy == "warning":
                import warnings
                warnings.warn(error_msg, UserWarning)
            else:  # "error"
                raise ConfigurationError(error_msg)
    
    def _format_missing_keys_error(self, missing_keys: List[str], expected_help: Dict[str, str]) -> str:
        """Format a helpful error message listing missing configuration keys.
        
        Creates a message showing which keys are missing and includes help text
        if available.
        
        Args:
            missing_keys: List of key names that are required but not present
            expected_help: Dict mapping key names to help text
            
        Returns:
            Formatted error message string
            
        Example output:
            Missing required configuration keys: ['DATABASE_URL', 'API_KEY']
            Help: DATABASE_URL: PostgreSQL connection string; API_KEY: External API key
        """
        error_msg = f"Missing required configuration keys: {missing_keys}"
        
        # Add help text if available
        help_msgs = []
        for key in missing_keys:
            if key in expected_help:
                help_msgs.append(f"{key}: {expected_help[key]}")
        
        if help_msgs:
            error_msg += f"\nHelp: {'; '.join(help_msgs)}"
        
        return error_msg

    @staticmethod
    def _find_most_derived_class_for_key(cls: type, key: str) -> Optional[str]:
        """Find the most-derived class in MRO that defines a given key.
        
        Walks MRO from most-derived to least-derived (child to parent) and
        returns the first class that defines the key as a non-method attribute.
        
        Args:
            cls: Class to search (walk its __mro__)
            key: Configuration key name to find
            
        Returns:
            Class name string if found, None otherwise
        """
        for c in cls.__mro__:
            # Check if the key is directly defined in this class's __dict__
            # (not inherited from a parent class)
            if key in c.__dict__:
                attr = c.__dict__[key]
                # Skip methods - only return non-callable attributes
                if not callable(attr) or isinstance(attr, type):
                    return c.__name__
        return None

    def _generate_override_warnings(self, env_vars_dict: Dict[str, Any], cls_vars_dict: Dict[str, Any]) -> None:
        """Generate warnings when environment values override code values.
        
        Warns when:
        - Key exists in both env_vars_dict and cls_vars_dict
        - Key is NOT in EXPECTED_KEYS
        - Key is NOT a built-in/system key
        
        Does not warn when:
        - Key is in EXPECTED_KEYS (override is expected)
        - Key is a built-in key (MASTER_ROOT_DIR, ENV_LABEL, etc.)
        
        Args:
            env_vars_dict: Environment variables dictionary
            cls_vars_dict: Class attributes dictionary
        """
        import warnings
        
        # Built-in keys that should not generate warnings
        builtin_keys = {
            "MASTER_ROOT_DIR", "ENV_FPATH", "ENV_LABEL", "ENV_TYPE", 
            "ENV_SOURCE", "CONFIG_CLASS_NAME", "CONFIG_CLASS_FPATH", "PYPROJECT_TOML"
        }
        
        # Get EXPECTED_KEYS from class hierarchy
        expected_keys, _ = self._get_expected_keys()
        expected_keys_set = set(expected_keys)
        
        # Check each key in environment variables
        for key in env_vars_dict:
            if key in cls_vars_dict:
                # Only warn if key is not in EXPECTED_KEYS and not a built-in key
                if key not in expected_keys_set and key not in builtin_keys:
                    # Find the most-derived class that defines this key
                    class_name = self._find_most_derived_class_for_key(self.__class__, key)
                    if class_name:
                        warning_msg = (
                            f"The Python-defined value of '{key}' in class {class_name} "
                            f"was overridden by the environment-variable value."
                        )
                        warnings.warn(warning_msg, UserWarning, stacklevel=3)


################################


if __name__ == "__main__":
    cfg = AppConfigRootClass.dynaload(__file__,require_config_file = False)
        # handle the case were no config file is found adn AppConfigRootClass is used
    print(
        "Derive your project config classes from AppConfigRootClass and add your own configuration settings.  Note that different sup-projects within the master project may have their own config.py."
    )
    print("Below are some shared settings that are available to all sub-projects.")
    print(f"The root project directory is: {cfg['MASTER_ROOT_DIR']}")
    print(f"The environment file is: {cfg}")
    print(f"The config file is: {cfg['CONFIG_CLASS_FPATH']}")
    print(f"The environment label (ENV_LABEL) is: {cfg['ENV_LABEL']}")  
    print(f"The deduced environment type (ENV_TYPE) is: {cfg['ENV_TYPE']}")  # should be DEV, TEST, STAGE, or PROD
    print(f"The list of all keys in the app config class: {list(cfg.keys())}")
    
