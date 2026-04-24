# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from totodev_pub.app_config_root_class import AppConfigRootClass


class AppConfigCommon(AppConfigRootClass):
    #COMMON_VAL_1 = 1   # we want the environment file to dominate 
    COMMON_VAL_2 = 2
    COMMON_VAL_3 = 3


class AppConfigTEST01(AppConfigCommon):
    ALPHA_1 = 'alpha'

    def COMMON_VAL_2(self):  #should shadow/override the value in AppConfigCommon
        return 'beta'   
    
    def COMMON_VAL_1_TYPENAME(self):
        return type(self['COMMON_VAL_1']).__name__


class AppConfigDEVALICE(AppConfigCommon):
    """Config for DEVALICE environment"""
    ENV_TYPE = "DEVALICE"
    DEVALICE_VAL_1 = 11
    DEVALICE_VAL_2 = 22
    
    