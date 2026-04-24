# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from pathlib import Path

import pytest

from totodev_pub.app_config_root_class import AppConfigRootClass

_SCENARIO = Path(__file__).resolve().parent / "app_config_pytest_scenarios"
_ENV_UNIT = _SCENARIO / "envconfigs" / "TESTUNIT"
CONFIG: AppConfigRootClass = AppConfigRootClass.dynaload(
    [str(_ENV_UNIT), str(_SCENARIO)],
    require_config_file=False,
)


@pytest.fixture
def app_config() -> AppConfigRootClass:
    """Shared AppConfig for policy tests (uses packaged pytest scenario files)."""
    return CONFIG


@pytest.mark.skip(reason="Test needs to be updated to use new AppConfig class instead of the old one")
def test_confirm_dumpfile_uptodate(app_config):
    """
    Test to ensure that the dumpfile is up-to-date with the current _THIS_IS_*_ENV_ file.
    """
    pass


@pytest.mark.skip(reason="Test needs to be updated to use new AppConfig class instead of the old one")
def test_env_dumpfile_mismatch(app_config):
    """
    Test to ensure that the then current environment file is up-to-date with the code of others.

    IMPORTANT NOTE: This only generates warnings if problems exist... never failures.
    """
    pass


def test_confirm_not_using_requirements_text(app_config: AppConfigRootClass):
    """
    Confirm that the requirements.txt file is not being used.
    In the standard project structure for this library, use a pyproject.toml file instead.
    """
    requirements_file = Path(app_config["MASTER_ROOT_DIR"]) / "requirements.txt"
    assert not requirements_file.exists(), (
        "requirements.txt file exists.  This is not allowed.  Use a pyproject.toml file instead."
    )
