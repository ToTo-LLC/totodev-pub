# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub


from totodev_pub.app_config_root_class import AppConfigRootClass


class AppConfigCommon(AppConfigRootClass):
    COMMON_VAL_1 = 1
    COMPLEX_VAL_PKD = ";a=1;b=2"
    def XYZ(self):
        raise Exception("This should never be invoked because closest environment file is for DEV01")

class AppConfigTEST01(AppConfigCommon):
    def XYZ(self):
        raise Exception("This should never be invoked because closest environment file is for DEV01")


class AppConfigDEV01(AppConfigCommon):
    XYZ = 'xyz' 


class AppConfigDEVALICE(AppConfigDEV01):
    pass   # you need a class like this to run the tests in your own environment.
    

