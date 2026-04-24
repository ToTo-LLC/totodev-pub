# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Test configuration classes for pytest scenarios"""
from totodev_pub.app_config_root_class import AppConfigRootClass


class AppConfigTestBase(AppConfigRootClass):
    """Base class for all test configurations"""
    expect_policy = "warning"  # Don't fail tests on missing optional keys
    
    # Test-specific settings
    TEST_MODE = True
    SHARED_TEST_VALUE = "shared_across_all_tests"


class AppConfigTESTUNIT(AppConfigTestBase):
    """Unit test config - no external dependencies"""
    EXPECTED_KEYS = {
        "USE_MOCK_DATABASE": "Should be 'true' for unit tests",
        "MOCK_EXTERNAL_APIS": "Should be 'true' for unit tests"
    }
    
    def DATABASE_URL(self):
        # Use in-memory SQLite for unit tests
        return "sqlite:///:memory:"
    
    def API_ENDPOINT(self):
        return "mock://api.example.com"


# Note: AppConfigTESTINTEGRATION is defined in envconfigs/TESTINTEGRATION/config.py
# This demonstrates the optional override pattern

