# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
import json
import asyncio
from pathlib import Path

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders, SLAVE_DIR_EXTENSION
from totodev_pub.cached_file_folders_support.cache_manifest import (
    CachedFileFoldersManifest,
    MANIFEST_FILENAME,
)


def _make_abs_tmp_dir(tmp_path: Path) -> str:
    # Ensure absolute path
    return str(tmp_path.resolve())


def test_absolute_root_required(tmp_path: Path):
    rel_root = str(Path("relative_root"))
    with pytest.raises(ValueError):
        CachedFileFolders("cache/", rel_root)


def test_root_dir_creation_when_parent_exists(tmp_path: Path):
    """Test that the final directory is created when parent exists but final directory doesn't."""
    parent_dir = tmp_path / "existing_parent"
    parent_dir.mkdir()
    
    root_dir = parent_dir / "new_cache_root"
    assert not root_dir.exists(), "Root directory should not exist initially"
    
    # This should succeed and create the directory
    cache = CachedFileFolders("cache/", str(root_dir))
    assert root_dir.exists(), "Root directory should be created automatically"
    assert cache is not None


def test_root_dir_creation_when_parent_missing(tmp_path: Path):
    """Test that ValueError is raised when parent directory doesn't exist."""
    nonexistent_parent = tmp_path / "nonexistent_parent"
    root_dir = nonexistent_parent / "cache_root"
    
    with pytest.raises(ValueError) as exc_info:
        CachedFileFolders("cache/", str(root_dir))
    
    assert "parent directory" in str(exc_info.value)
    assert str(nonexistent_parent) in str(exc_info.value)


def test_root_dir_creation_with_existing_directory(tmp_path: Path):
    """Test that existing root directory works normally."""
    root_dir = tmp_path / "existing_cache_root"
    root_dir.mkdir()
    
    # Should work normally with existing directory
    cache = CachedFileFolders("cache/", str(root_dir))
    assert root_dir.exists(), "Existing root directory should still exist"
    assert cache is not None


def test_root_dir_creation_permission_error_handling(tmp_path: Path):
    """Test that permission errors are properly handled and reported."""
    # Create a directory and make it read-only to simulate permission issues
    parent_dir = tmp_path / "readonly_parent"
    parent_dir.mkdir()
    
    # Make parent read-only (this won't work on all systems, but we can try)
    try:
        parent_dir.chmod(0o444)  # Read-only
        
        root_dir = parent_dir / "cache_root"
        
        with pytest.raises(ValueError) as exc_info:
            CachedFileFolders("cache/", str(root_dir))
        
        assert "Cannot create root_dir" in str(exc_info.value)
    except (OSError, PermissionError):
        # If we can't make it read-only on this system, skip this test
        pytest.skip("Cannot create read-only directory on this system")
    finally:
        # Restore permissions
        try:
            parent_dir.chmod(0o755)
        except (OSError, PermissionError):
            pass


def test_manifest_created_with_effective_params(tmp_path: Path):
    root = _make_abs_tmp_dir(tmp_path)
    grouping = "cache/"  # flat pattern
    use_xxhash = True
    char_map = {"?": "_", "/": "-"}

    cache = CachedFileFolders(
        grouping,
        root,
        use_xxhash=use_xxhash,
        char_replacement_map=char_map,
    )

    manifest_path = Path(root) / MANIFEST_FILENAME
    assert manifest_path.exists()

    manifest = CachedFileFoldersManifest.load(str(manifest_path), acquire_lock=False, format_override="json")
    assert manifest.root_dir == str(Path(root).resolve())
    assert manifest.grouping_pattern == grouping
    assert manifest.use_xxhash == use_xxhash
    assert manifest.slave_dir_extension == SLAVE_DIR_EXTENSION
    assert manifest.char_replacement_map == char_map


def test_adopt_existing_without_manifest_logs_warning(tmp_path: Path, caplog):
    root = _make_abs_tmp_dir(tmp_path)
    # Create existing data to trigger adoption warning
    (Path(root) / "some_existing_file.txt").write_text("data")

    grouping = "cache/"
    with caplog.at_level("WARNING"):
        CachedFileFolders(grouping, root)
    assert any("Adopting existing data" in rec.message for rec in caplog.records)


def test_rebuild_corrupt_manifest_with_explicit_params(tmp_path: Path, caplog):
    root = _make_abs_tmp_dir(tmp_path)
    manifest_path = Path(root) / MANIFEST_FILENAME
    os.makedirs(root, exist_ok=True)
    manifest_path.write_text("{ not valid json")

    grouping = "cache/"
    with caplog.at_level("WARNING"):
        CachedFileFolders(grouping, root, use_xxhash=False)

    # Should have warning about rebuilding
    assert any("Rebuilding corrupt/unreadable cache manifest" in rec.message for rec in caplog.records)

    # Manifest should now be valid JSON
    manifest = CachedFileFoldersManifest.load(str(manifest_path), acquire_lock=False, format_override="json")
    assert manifest.grouping_pattern == grouping


def test_manifest_mismatch_raises_with_detailed_keys(tmp_path: Path):
    root = _make_abs_tmp_dir(tmp_path)
    grouping = "cache/"
    cache = CachedFileFolders(grouping, root, use_xxhash=False)
    assert cache is not None

    # Attempt with different parameter (e.g., use_xxhash True)
    with pytest.raises(ValueError) as ei:
        CachedFileFolders(grouping, root, use_xxhash=True)
    msg = str(ei.value)
    assert "use_xxhash" in msg
    assert "Delete the manifest" in msg


def test_charmap_fallback_and_validation(tmp_path: Path):
    root = _make_abs_tmp_dir(tmp_path)
    grouping = "cache/"
    initial_map = {":": "-"}

    cf1 = CachedFileFolders(grouping, root, char_replacement_map=initial_map)
    assert cf1.char_replacement_map == initial_map

    # Second construction with None should adopt the manifest's map
    cf2 = CachedFileFolders(grouping, root, char_replacement_map=None)
    assert cf2.char_replacement_map == initial_map

    # Passing a different explicit map should raise
    with pytest.raises(ValueError):
        CachedFileFolders(grouping, root, char_replacement_map={":": "_"})


def test_from_root_success_and_errors(tmp_path: Path):
    root = _make_abs_tmp_dir(tmp_path)
    grouping = "cache/"
    cf = CachedFileFolders(grouping, root, use_xxhash=True)
    assert cf is not None

    # Happy path
    cf_attached = CachedFileFolders.from_root(root)
    assert cf_attached.use_xxhash is True
    assert cf_attached.slave_dir_extension == SLAVE_DIR_EXTENSION

    # Missing manifest error
    other_root = _make_abs_tmp_dir(tmp_path / "nomanifest")
    os.makedirs(other_root, exist_ok=True)
    with pytest.raises(ValueError):
        CachedFileFolders.from_root(other_root)


