# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Unit tests for CachedFileFolders orphaned file recovery feature.

This test module verifies the automatic recovery of orphaned files - files that exist
on the filesystem but are not tracked in the database. This feature prevents
SameFileError conditions that previously occurred in edge cases.

Historical Context:
-------------------
Originally, these tests were designed to reproduce a SameFileError bug that occurred when:
1. A file exists at the target location but isn't in the database
2. A temp file is created with the same name as the target
3. LocalFileProxy.deploy() tries to copy the temp file to the target location
4. Since they're the same file, shutil.copy2() raises SameFileError

Current Behavior (Orphaned File Recovery):
------------------------------------------
The system now automatically detects and recovers orphaned files by:
1. Checking if a file exists at the target path when database lookup returns None
2. If found, adding the file to the database (logged as "Database inconsistency detected and corrected")
3. Treating the operation as an UPDATE rather than INSERT
4. Proceeding normally without raising SameFileError

This makes the system more resilient to database inconsistencies and allows for graceful
recovery from interrupted operations or manual file manipulations.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy


class TestCachedFileFoldersSameFileError:
    """Test orphaned file recovery and handling of database inconsistencies in CachedFileFolders.
    
    Note: Class name retained for backward compatibility, but tests now verify
    the orphaned file recovery feature rather than SameFileError conditions."""
    
    @pytest.fixture
    def temp_cache(self, tmp_path):
        """Create a temporary CachedFileFolders instance for testing."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        
        cache = CachedFileFolders(
            grouping_pattern="test/{category}/",
            root_dir=str(cache_root),
        )
        return cache
    
    @pytest.fixture
    def temp_cache_global(self, tmp_path):
        """Create a temporary CachedFileFolders instance with global/sharepoint pattern for testing."""
        cache_root = tmp_path / "cache_global"
        cache_root.mkdir()
        
        cache = CachedFileFolders(
            grouping_pattern="test/{category}/{subcategory}/",
            root_dir=str(cache_root),
        )
        return cache
    
    @pytest.mark.asyncio
    async def test_samefile_error_reproduction(self, temp_cache):
        """Test that orphaned files (exist on filesystem but not in DB) are automatically recovered.
        
        This replaces the old test that expected SameFileError. The system now automatically
        recovers orphaned files by adding them to the database, preventing the SameFileError
        condition from occurring."""
        
        # Test data
        test_content = "Test content for orphaned file recovery"
        test_ref_path = "test_samefile_error.txt"
        test_grouping_key = ["test_category"]
        
        # Get target directory
        target_dir = Path(temp_cache.root_dir) / "test" / "test_category"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file_path = target_dir / test_ref_path
        
        # Step 1: Create a file at the target location WITHOUT using upsert
        # This simulates an orphaned file - exists on disk but not in database
        target_file_path.write_text(test_content)
        
        # Verify orphaned state: file exists but not in database
        assert target_file_path.exists()
        assert temp_cache.find_file(test_ref_path, test_grouping_key) is None
        
        # Step 2: Create a proxy pointing to a file with the same ref_path
        # In the real scenario, this would be a different file being upserted
        temp_file_path = target_dir / f"temp_{test_ref_path}"
        temp_file_path.write_text(test_content + " - updated version")
        
        proxy = LocalFileProxy(str(temp_file_path), ref_path=test_ref_path, delete_after_deploy=False)
        
        # Step 3: Upsert should automatically recover the orphaned file and treat as UPDATE
        result = await temp_cache.upsert_file(proxy, test_grouping_key, force=True)
        
        # Verify recovery occurred - should be treated as UPDATE (not INSERT)
        assert result is not None
        assert result.change_type.name == "UPDATE", "Orphaned file should be recovered and treated as UPDATE"
        
        # Verify file is now properly indexed in database
        file_ref = temp_cache.find_file(test_ref_path, test_grouping_key)
        assert file_ref is not None
        assert file_ref.file_path.exists()
        
        # Verify the content was updated
        actual_content = file_ref.file_path.read_text()
        assert actual_content == test_content + " - updated version"
    
    @pytest.mark.asyncio
    async def test_samefile_error_fix(self, temp_cache):
        """Test that the fix works when temp file has a different name."""
        
        # Test data
        test_content = "Test content for SameFileError fix"
        test_ref_path = "test_samefile_error_fix.txt"
        test_grouping_key = ["test_category"]
        
        # Create temp file in a different directory (this is the fix)
        with tempfile.TemporaryDirectory(prefix="samefile_fix_test_") as temp_dir:
            temp_path = Path(temp_dir)
            temp_file_path = temp_path / test_ref_path
            temp_file_path.write_text(test_content)
            
            # Step 1: First upsert (INSERT operation)
            proxy = LocalFileProxy(str(temp_file_path), ref_path=test_ref_path, delete_after_deploy=False)
            result = await temp_cache.upsert_file(proxy, test_grouping_key, force=True)
            
            # Should succeed
            assert result is not None
            assert result.change_type.name == "INSERT"
            assert result.cur.file_path.name == test_ref_path
            
            # Verify file is in database
            file_ref = temp_cache.find_file(test_ref_path, test_grouping_key)
            assert file_ref is not None
            assert file_ref.file_path.name == test_ref_path
            
            # Step 2: Second upsert (UPDATE operation)
            updated_content = test_content + " - updated"
            updated_temp_file = temp_path / f"updated_{test_ref_path}"
            updated_temp_file.write_text(updated_content)
            
            staged_paths = []
            captured_old_contents = []

            def change_receiver(notice, _proxy: Optional[object]):
                if notice.change_type.name == "UPDATE":
                    assert notice.old is not None
                    staged_paths.append((notice.old.file_path, notice.old.slave_dir_path))
                    assert notice.old.file_path.exists()
                    assert notice.old.slave_dir_path.exists()
                    captured_old_contents.append(notice.old.file_path.read_text())

            proxy = LocalFileProxy(str(updated_temp_file), ref_path=test_ref_path, delete_after_deploy=False)
            result = await temp_cache.upsert_file(
                proxy,
                test_grouping_key,
                force=True,
                change_receiver=change_receiver,
            )
            
            # Should succeed as UPDATE
            assert result is not None
            assert result.change_type.name == "UPDATE"
            assert result.old is not None
            for file_path, slave_dir in staged_paths:
                assert not file_path.exists()
                assert not slave_dir.exists()
            assert captured_old_contents and captured_old_contents[0] == test_content
            
            # Verify content was updated
            actual_content = file_ref.file_path.read_text()
            assert actual_content == updated_content
    
    @pytest.mark.asyncio
    async def test_update_operation_old_file_handling(self, temp_cache):
        """Test that UPDATE operations properly handle old file retention."""
        
        # Test data
        original_content = "Original content"
        updated_content = "Updated content"
        test_ref_path = "test_update_handling.txt"
        test_grouping_key = ["test_category"]
        
        # Step 1: Initial INSERT
        with tempfile.TemporaryDirectory(prefix="update_test_1_") as temp_dir:
            temp_path = Path(temp_dir)
            temp_file = temp_path / test_ref_path
            temp_file.write_text(original_content)

            proxy = LocalFileProxy(str(temp_file), ref_path=test_ref_path, delete_after_deploy=False)
            result = await temp_cache.upsert_file(proxy, test_grouping_key, force=True)
            
            assert result is not None
            assert result.change_type.name == "INSERT"
        
        # Step 2: UPDATE operation
        with tempfile.TemporaryDirectory(prefix="update_test_2_") as temp_dir:
            temp_path = Path(temp_dir)
            temp_file = temp_path / test_ref_path
            temp_file.write_text(updated_content)
            
            staged_paths = []
            captured_old_contents = []

            def change_receiver(notice, _proxy: Optional[object]):
                if notice.change_type.name == "UPDATE":
                    assert notice.old is not None
                    staged_paths.append((notice.old.file_path, notice.old.slave_dir_path))
                    assert notice.old.file_path.exists()
                    assert notice.old.slave_dir_path.exists()
                    captured_old_contents.append(notice.old.file_path.read_text())

            proxy = LocalFileProxy(str(temp_file), ref_path=test_ref_path, delete_after_deploy=False)
            result = await temp_cache.upsert_file(
                proxy,
                test_grouping_key,
                force=True,
                change_receiver=change_receiver,
            )
            
            # Should be UPDATE
            assert result is not None
            assert result.change_type.name == "UPDATE"
            
            # Old file should have been available during callback and cleaned afterwards
            assert result.old is not None
            for file_path, slave_dir in staged_paths:
                assert not file_path.exists()
                assert not slave_dir.exists()
            assert captured_old_contents and captured_old_contents[0] == original_content
            
            # Verify new file content
            new_content = result.cur.file_path.read_text()
            assert new_content == updated_content
            
            # Verify old and new files are different
            assert result.old.file_path != result.cur.file_path
    
    @pytest.mark.asyncio
    async def test_directory_mapping_script_scenario(self, temp_cache_global):
        """Test orphaned file recovery in the directory mapping script scenario.
        
        This replaces the old test that expected SameFileError. The system now automatically
        recovers orphaned files, which is the correct behavior for the directory mapping use case
        where files might exist on disk from previous runs but not be in the database."""
        
        # This test simulates the scenario from 0130_load_sharepoint_dir_map.py
        test_content = "Project directory mapping data"
        test_ref_path = "project_dirs.yaml"
        test_grouping_key = ["_global", "sharepoint"]
        
        target_dir = Path(temp_cache_global.root_dir) / "test" / "_global" / "sharepoint"
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Simulate orphaned file: file exists at target location but not in database
        orphaned_file = target_dir / "project_dirs.yaml"
        orphaned_file.write_text(test_content)
        
        # Verify orphaned state
        assert orphaned_file.exists()
        assert temp_cache_global.find_file(test_ref_path, test_grouping_key) is None
        
        # Attempt upsert with same ref_path - should automatically recover
        proxy = LocalFileProxy(str(orphaned_file), ref_path=test_ref_path, delete_after_deploy=False)
        result = await temp_cache_global.upsert_file(proxy, test_grouping_key, force=True)
        
        # Verify recovery occurred - treated as UPDATE since orphaned file was recovered
        assert result is not None
        # Note: With force=True and identical content, result might be UPDATE or None depending on comparison
        # The key is that it doesn't raise an error
        
        # Verify file is now properly indexed in database
        file_ref = temp_cache_global.find_file(test_ref_path, test_grouping_key)
        assert file_ref is not None
        assert file_ref.file_path.name == test_ref_path
        assert file_ref.file_path.exists()
        
        # Now test normal workflow: use a different temp file for update
        fixed_temp_file = target_dir / "temp_project_dirs.yaml"  # Different name!
        updated_content = test_content + "\n# Updated"
        fixed_temp_file.write_text(updated_content)
        
        # This should work as a normal UPDATE operation
        proxy = LocalFileProxy(str(fixed_temp_file), ref_path=test_ref_path, delete_after_deploy=False)
        result = await temp_cache_global.upsert_file(proxy, test_grouping_key, force=True)
        
        assert result is not None
        assert result.change_type.name == "UPDATE"
        
        # Verify content was updated
        file_ref = temp_cache_global.find_file(test_ref_path, test_grouping_key)
        assert file_ref is not None
        actual_content = file_ref.file_path.read_text()
        assert actual_content == updated_content
