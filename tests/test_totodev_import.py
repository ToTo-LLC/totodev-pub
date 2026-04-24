# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Basic tests to verify totodev_pub library import and basic functionality.
"""

from pathlib import Path
from typing import Iterable, List

import pytest


def test_totodev_import() -> None:
    """Test that the totodev_pub library can be imported."""
    try:
        import totodev_pub  # noqa: F401
    except ImportError as exc:
        pytest.fail(f"Failed to import totodev_pub library: {exc}")  # pragma: no cover


def test_totodev_has_modules() -> None:
    """Test that the totodev_pub library exposes expected module files."""
    package_paths = _package_paths()
    assert package_paths, "totodev_pub should expose at least one namespace path"

    primary_path = package_paths[0]

    expected_entries = [
        Path("dbjig.py"),
        Path("forgetful_reader.py"),
        Path("minor/flexargs.py"),
        Path("minor/sweep.py"),
        Path("dbjig_support/tbdict.py"),
        Path("pipes"),
        Path("cached_file_folders_support"),
    ]

    missing = [
        entry for entry in expected_entries
        if not (primary_path / entry).exists()
    ]

    assert not missing, f"Missing expected totodev_pub entries: {missing}"


def test_totodev_directory_structure() -> None:
    """Test that the totodev_pub namespace directory exists and is populated."""
    package_paths = _package_paths()
    assert package_paths, "totodev_pub should expose at least one namespace path"

    for path in package_paths:
        assert path.exists(), f"{path} should exist"
        assert path.is_dir(), f"{path} should be a directory"

    primary_path = package_paths[0]
    representative_nodes = [
        primary_path / "cached_file_folders.py",
        primary_path / "minor",
        primary_path / "cli",
        primary_path / "primitive_event_log.py",
    ]

    for node in representative_nodes:
        assert node.exists(), f"Expected to find {node.name} in totodev_pub package"


def _package_paths() -> List[Path]:
    import totodev_pub

    return [Path(path).resolve() for path in iter(totodev_pub.__path__)]  # type: ignore[attr-defined]


if __name__ == "__main__":
    pytest.main([__file__])
