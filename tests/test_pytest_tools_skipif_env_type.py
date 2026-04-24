# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
from pathlib import Path

import pytest

from totodev_pub.pytest_tools import skipif_env_type, _get_env_type_for_search_start, _VALID_ENV_TYPES


def _write_env_file(dir_path: Path, label: str) -> Path:
    """Helper to create a minimal environment file with the given label."""
    env_file = dir_path / f"config._THIS_IS_{label}_ENV_.sh"
    env_file.write_text('export DUMMY="value"\n')
    return env_file


def test_get_env_type_for_search_start_uses_nearest_env_file(tmp_path: Path) -> None:
    # Arrange: create a TEST-specific environment file in a temporary directory
    _write_env_file(tmp_path, "TESTLOCAL")

    # Act
    env_type = _get_env_type_for_search_start(tmp_path)

    # Assert: The type is derived from the TEST* prefix
    assert env_type == "TEST"


def test_skipif_env_type_raises_on_invalid_env_type(tmp_path: Path) -> None:
    # Arrange
    _write_env_file(tmp_path, "DEVLOCAL")

    def dummy() -> None:
        pass

    # Act / Assert
    with pytest.raises(ValueError) as excinfo:
        # lower-case is not a valid value and should fail fast
        skipif_env_type("dev", search_start=tmp_path)(dummy)

    msg = str(excinfo.value)
    assert "Invalid environment type value(s)" in msg
    for valid in sorted(_VALID_ENV_TYPES):
        assert valid in msg


def test_skipif_env_type_skips_when_env_matches(tmp_path: Path) -> None:
    # Arrange: active env type should resolve to TEST
    _write_env_file(tmp_path, "TESTLOCAL")

    def dummy() -> None:
        pass

    decorated = skipif_env_type("TEST", search_start=tmp_path)(dummy)

    # Assert: the decorated function has a pytest skip marker with the expected reason
    marks = getattr(decorated, "pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]

    skip_marks = [m for m in marks if getattr(m, "name", "") == "skip"]
    assert skip_marks, "Expected a pytest skip mark to be attached"
    reason = skip_marks[0].kwargs.get("reason", "")
    assert 'current inferred environment type ("TEST")' in reason
    assert "TEST" in reason


def test_skipif_env_type_does_not_skip_when_env_not_in_list(tmp_path: Path) -> None:
    # Arrange: active env type should resolve to DEV
    _write_env_file(tmp_path, "DEVLOCAL")

    called = {"value": False}

    def dummy() -> None:
        called["value"] = True

    decorated = skipif_env_type("TEST", "PROD", search_start=tmp_path)(dummy)

    # Act
    decorated()

    # Assert: underlying test function should run
    assert called["value"] is True


