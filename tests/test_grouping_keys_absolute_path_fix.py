# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test for groupings() absolute path bug fix.

This test verifies that groupings() correctly extracts keys from absolute paths
and doesn't incorrectly match unintended parts of the absolute path prefix.

Bug: When patterns like {project_id}/{category} were used with absolute paths,
the regex would match the first occurrence of X/Y in the path (e.g., /Users/testuser)
instead of the intended grouping key components.

Fix: Convert absolute paths to relative paths before calling infer_key().

Note: This test was originally for grouping_keys() but has been updated to use
groupings() which returns CacheGrouping facets instead of raw tuples.
"""

import pytest
import tempfile
from pathlib import Path

from totodev_pub.cached_file_folders import CachedFileFolders


class TestGroupingKeysAbsolutePathFix:
    """Test that groupings() correctly handles absolute paths."""

    def test_grouping_keys_with_absolute_path_pattern(self):
        """
        Test that groupings() extracts correct keys even when absolute paths
        contain segments that match the pattern.
        
        This reproduces the bug where a pattern like {project_id}/{category}
        would incorrectly match /Users/testuser from the absolute path instead of
        the intended 20326/sharepoint grouping key.
        """
        with tempfile.TemporaryDirectory() as temp_root:
            # Create a cache root that contains path segments matching the pattern
            # This simulates the bug scenario where /Users/testuser/... exists
            cache_root = Path(temp_root) / "Users" / "testuser" / "cache" / "projects"
            cache_root.mkdir(parents=True, exist_ok=True)
            
            # Initialize cache with a simple pattern
            cache = CachedFileFolders(
                grouping_pattern="{project_id}/{category}",
                root_dir=str(cache_root),
                use_xxhash=False
            )
            
            # Create test directories
            test_keys = [
                ("20218", "sharepoint"),
                ("20326", "sharepoint"),
                ("20340", "database"),
            ]
            
            for project_id, category in test_keys:
                folder = cache._storage.category_folders.folder(
                    [project_id, category],
                    create=True
                )
            
            # Extract grouping keys via groupings() facets
            extracted_keys = [tuple(g.grouping_key) for g in cache.groupings()]
            
            # Verify we got the correct keys
            assert len(extracted_keys) == len(test_keys)
            for expected_key in test_keys:
                assert expected_key in extracted_keys
            
            # Verify we didn't get wrong keys from the absolute path
            wrong_keys = [
                ("Users", "testuser"),      # Would match if bug existed
                ("testuser", "cache"),      # Would match if bug existed
                ("cache", "projects"),    # Would match if bug existed
            ]
            for wrong_key in wrong_keys:
                assert wrong_key not in extracted_keys

    def test_grouping_keys_filtering_with_absolute_paths(self):
        """
        Test that groupings() filtering works correctly with absolute paths.
        """
        with tempfile.TemporaryDirectory() as temp_root:
            cache_root = Path(temp_root) / "var" / "data" / "cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            
            cache = CachedFileFolders(
                grouping_pattern="{project}/{category}",
                root_dir=str(cache_root),
                use_xxhash=False
            )
            
            # Create test directories
            test_keys = [
                ("webapp", "api"),
                ("webapp", "ui"),
                ("mobile", "api"),
                ("mobile", "db"),
            ]
            
            for project, category in test_keys:
                cache._storage.category_folders.folder(
                    [project, category],
                    create=True
                )
            
            # Test filtering by project
            webapp_keys = [tuple(g.grouping_key) for g in cache.groupings(filters={"project": "webapp", "category": "*"})]
            assert len(webapp_keys) == 2
            assert ("webapp", "api") in webapp_keys
            assert ("webapp", "ui") in webapp_keys
            
            # Test filtering by category
            api_keys = [tuple(g.grouping_key) for g in cache.groupings(filters={"project": "*", "category": "api"})]
            assert len(api_keys) == 2
            assert ("webapp", "api") in api_keys
            assert ("mobile", "api") in api_keys

    def test_grouping_keys_with_longer_pattern(self):
        """
        Test with a more complex pattern to ensure the fix works generally.
        """
        with tempfile.TemporaryDirectory() as temp_root:
            # Create path with multiple segments that could confuse the regex
            cache_root = Path(temp_root) / "home" / "user" / "apps" / "prod" / "cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            
            cache = CachedFileFolders(
                grouping_pattern="{app}/{env}/{module}",
                root_dir=str(cache_root),
                use_xxhash=False
            )
            
            # Create test directories
            test_keys = [
                ("webapp", "prod", "api"),
                ("webapp", "dev", "api"),
                ("mobile", "prod", "core"),
            ]
            
            for app, env, module in test_keys:
                cache._storage.category_folders.folder(
                    [app, env, module],
                    create=True
                )
            
            # Extract all keys via groupings() facets
            extracted_keys = [tuple(g.grouping_key) for g in cache.groupings()]
            
            # Verify correct extraction
            assert len(extracted_keys) == len(test_keys)
            for expected_key in test_keys:
                assert expected_key in extracted_keys
            
            # Verify no incorrect matches from absolute path
            # Should NOT match segments like ("home", "user", "apps") or ("user", "apps", "prod")
            wrong_keys = [
                ("home", "user", "apps"),
                ("user", "apps", "prod"),
                ("apps", "prod", "cache"),
            ]
            for wrong_key in wrong_keys:
                assert wrong_key not in extracted_keys

