# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Optional config override for TESTINTEGRATION environment"""
from totodev_pub.app_config_root_class import AppConfigRootClass


class AppConfigTESTINTEGRATION(AppConfigRootClass):
    """Integration test config with optional override in envconfigs directory"""
    expect_policy = "warning"
    
    # This demonstrates config.py can be placed in the same directory as env file
    OVERRIDE_FROM_LOCAL_CONFIG = "overridden_locally"
    
    def COMPUTED_VALUE(self):
        return f"integration_{self['INTEGRATION_VALUE']}"

