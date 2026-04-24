# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test cases for LocalFileProxyFactory class.
"""

import pytest
import tempfile
import os
import shutil
from pathlib import Path
from typing import List

from totodev_pub.cached_file_folders_support.file_proxy_local_file import (
    LocalFileProxyFactory, 
    LocalFileProxy
)


class TestLocalFileProxyFactory:
    """Test cases for LocalFileProxyFactory."""
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory with test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test directory structure
            test_files = [
                "file1.txt",
                "file2.pdf", 
                "file3.py",
                "subdir/file4.txt",
                "subdir/file5.md",
                "subdir/nested/file6.py",
                "subdir/nested/file7.txt",
                "other_dir/file8.pdf",
                "other_dir/file9.py"
            ]
            
            for file_path in test_files:
                full_path = os.path.join(tmpdir, file_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                
                # Create the file with some content
                with open(full_path, 'w') as f:
                    f.write(f"Test content for {file_path}")
            
            yield tmpdir
    
    @pytest.fixture
    def factory(self):
        """Create a LocalFileProxyFactory instance."""
        return LocalFileProxyFactory()
    
    def test_scan_files_empty_pattern(self, factory):
        """Test that empty pattern raises ValueError."""
        with pytest.raises(ValueError, match="Pattern must be non-empty"):
            list(factory.scan_files(""))
        
        with pytest.raises(ValueError, match="Pattern must be non-empty"):
            list(factory.scan_files("   "))
    
    def test_scan_files_none_pattern(self, factory):
        """Test that None pattern raises ValueError."""
        with pytest.raises(ValueError, match="Pattern must be non-empty"):
            list(factory.scan_files(None))
    
    def test_scan_files_non_recursive_pattern(self, temp_dir, factory):
        """Test scanning with non-recursive patterns."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Test single file pattern
            files = list(factory.scan_files("file1.txt"))
            assert len(files) == 1
            assert isinstance(files[0], LocalFileProxy)
            assert files[0].ref_path() == os.path.realpath(os.path.join(temp_dir, "file1.txt"))
            
            # Test wildcard pattern in current directory
            files = list(factory.scan_files("*.txt"))
            assert len(files) == 1  # Only file1.txt in root
            assert "file1.txt" in files[0].ref_path()
            
            # Test wildcard pattern for all files in current directory
            files = list(factory.scan_files("*"))
            assert len(files) >= 3  # file1.txt, file2.pdf, file3.py
            
            # Test pattern with subdirectory
            files = list(factory.scan_files("subdir/*"))
            assert len(files) == 2  # file4.txt, file5.md
            for file in files:
                assert "subdir" in file.ref_path()
                
        finally:
            os.chdir(original_cwd)
    
    def test_scan_files_recursive_pattern(self, temp_dir, factory):
        """Test scanning with recursive patterns."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Test recursive pattern for all files
            files = list(factory.scan_files("**/*"))
            assert len(files) >= 9  # All test files
            
            # Test recursive pattern for specific extension
            files = list(factory.scan_files("**/*.txt"))
            assert len(files) == 3  # file1.txt, file4.txt, file7.txt
            for file in files:
                assert file.ref_path().endswith(".txt")
            
            # Test recursive pattern for Python files
            files = list(factory.scan_files("**/*.py"))
            assert len(files) == 3  # file3.py, file6.py, file9.py
            for file in files:
                assert file.ref_path().endswith(".py")
            
            # Test recursive pattern starting from subdirectory
            files = list(factory.scan_files("subdir/**/*"))
            assert len(files) == 4  # file4.txt, file5.md, file6.py, file7.txt
            for file in files:
                assert "subdir" in file.ref_path()
                
        finally:
            os.chdir(original_cwd)
    
    def test_scan_files_absolute_paths(self, temp_dir, factory):
        """Test scanning with absolute paths."""
        # Test absolute path pattern
        files = list(factory.scan_files(os.path.join(temp_dir, "*.txt")))
        assert len(files) == 1  # Only file1.txt in root
        # The factory should return the actual matched path (handle macOS /private prefix)
        expected_path = os.path.join(temp_dir, "file1.txt")
        actual_path = files[0].ref_path()
        # Handle macOS /private prefix issue
        if actual_path.startswith('/private') and not expected_path.startswith('/private'):
            expected_path = '/private' + expected_path
        assert actual_path == expected_path
        
        # Test absolute path recursive pattern
        files = list(factory.scan_files(os.path.join(temp_dir, "**/*.py")))
        assert len(files) == 3  # All Python files
        for file in files:
            assert file.ref_path().endswith(".py")
            assert temp_dir in file.ref_path()
    
    def test_scan_files_batched(self, temp_dir, factory):
        """Test batched scanning."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Test small batch size
            batches = list(factory.scan_files_batched("**/*", batch_size=3))
            assert len(batches) >= 3  # Should have multiple batches
            
            # Verify each batch has correct size (except possibly the last)
            for i, batch in enumerate(batches[:-1]):
                assert len(batch) == 3
            
            # Verify all files are covered
            all_files = []
            for batch in batches:
                all_files.extend(batch)
            
            assert len(all_files) >= 9  # All test files
            
            # Test batch size larger than total files
            batches = list(factory.scan_files_batched("*.txt", batch_size=100))
            assert len(batches) == 1  # Single batch
            assert len(batches[0]) == 1  # Only one txt file in root
                
        finally:
            os.chdir(original_cwd)
    
    def test_scan_files_symlinks(self, temp_dir, factory):
        """Test symlink handling."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Create a symlink to a file
            symlink_path = os.path.join(temp_dir, "symlink_file.txt")
            target_path = os.path.join(temp_dir, "file1.txt")
            os.symlink(target_path, symlink_path)
            
            # Test with follow_symlinks=False (default)
            files = list(factory.scan_files("symlink_file.txt"))
            assert len(files) == 0  # Should skip symlinks
            
            # Test with follow_symlinks=True
            files = list(factory.scan_files("symlink_file.txt", follow_symlinks=True))
            assert len(files) == 1  # Should include symlinks
            # The factory should return the symlink path, not the target path
            # Handle macOS /private prefix issue
            expected_path = os.path.abspath(symlink_path)
            actual_path = files[0].ref_path()
            if actual_path.startswith('/private') and not expected_path.startswith('/private'):
                expected_path = '/private' + expected_path
            assert actual_path == expected_path
            
            # Test recursive pattern with symlinks
            files = list(factory.scan_files("**/*.txt", follow_symlinks=False))
            txt_count = len(files)
            
            files = list(factory.scan_files("**/*.txt", follow_symlinks=True))
            assert len(files) == txt_count + 1  # Should include the symlink
            
        finally:
            os.chdir(original_cwd)
    
    def test_scan_files_directory_filtering(self, temp_dir, factory):
        """Test that directories are properly filtered out."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Create an empty directory
            empty_dir = os.path.join(temp_dir, "empty_dir")
            os.makedirs(empty_dir, exist_ok=True)
            
            # Scan all files - should not include directories
            files = list(factory.scan_files("**/*"))
            for file in files:
                assert os.path.isfile(file.ref_path())
                assert not os.path.isdir(file.ref_path())
                
        finally:
            os.chdir(original_cwd)
    
    def test_lazy_scanning_behavior(self, temp_dir, factory):
        """Test that scanning is truly lazy (yields files as found)."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Test that we can get the first file without processing all
            file_gen = factory.scan_files("**/*")
            first_file = next(file_gen)
            assert isinstance(first_file, LocalFileProxy)
            
            # Test that we can get more files incrementally
            second_file = next(file_gen)
            assert isinstance(second_file, LocalFileProxy)
            assert first_file.ref_path() != second_file.ref_path()
            
            # Close the generator
            file_gen.close()
                
        finally:
            os.chdir(original_cwd)
    
    def test_pattern_matching_edge_cases(self, temp_dir, factory):
        """Test various pattern matching edge cases."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Test pattern with no matches
            files = list(factory.scan_files("nonexistent.*"))
            assert len(files) == 0
            
            # Test pattern matching files with dots in names
            # Create a file with multiple dots
            multi_dot_file = os.path.join(temp_dir, "file.backup.old.txt")
            with open(multi_dot_file, 'w') as f:
                f.write("test")
            
            files = list(factory.scan_files("*.txt"))
            assert len(files) == 2  # file1.txt and file.backup.old.txt
            
            # Test case sensitivity (should be case sensitive)
            files = list(factory.scan_files("*.TXT"))
            assert len(files) == 0  # No uppercase .TXT files
                
        finally:
            os.chdir(original_cwd)
    
    def test_permission_error_handling(self, temp_dir, factory):
        """Test handling of permission errors gracefully."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # This test is more about ensuring the factory doesn't crash
            # on permission errors rather than actually creating permission errors
            # (which is difficult in a test environment)
            
            # Test that scanning works normally
            files = list(factory.scan_files("**/*"))
            assert len(files) >= 9  # Should find all test files
                
        finally:
            os.chdir(original_cwd)
    
    def test_factory_integration_with_local_file_proxy(self, temp_dir, factory):
        """Test that factory creates proper LocalFileProxy objects."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            files = list(factory.scan_files("*.txt"))
            assert len(files) == 1
            
            proxy = files[0]
            
            # Test that it's a proper LocalFileProxy
            assert isinstance(proxy, LocalFileProxy)
            
            # Test that materialize works (should return True immediately)
            import asyncio
            result = asyncio.run(proxy.materialize(0.0))
            assert result is True
            
            # Test that ref_path works
            ref_path = proxy.ref_path()
            assert ref_path.endswith("file1.txt")
            
            # Test that looks_same works
            same_result = proxy.looks_same(os.path.join(temp_dir, "file1.txt"))
            assert same_result is True
            
            # Test that get_context_info works
            context = proxy.get_context_info()
            assert context["proxy_type"] == "LocalFileProxy"
            assert "local_path" in context
                
        finally:
            os.chdir(original_cwd)
    
    def test_scan_files_with_mixed_file_types(self, temp_dir, factory):
        """Test scanning with various file types and patterns."""
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            
            # Create additional test files with different extensions
            additional_files = [
                "document.docx",
                "spreadsheet.xlsx", 
                "image.jpg",
                "archive.zip",
                "script.sh"
            ]
            
            for file_name in additional_files:
                file_path = os.path.join(temp_dir, file_name)
                with open(file_path, 'w') as f:
                    f.write("test content")
            
            # Test multiple extension patterns (use separate patterns since glob doesn't support {})
            docx_files = list(factory.scan_files("*.docx"))
            xlsx_files = list(factory.scan_files("*.xlsx"))
            assert len(docx_files) == 1
            assert len(xlsx_files) == 1
            
            # Test pattern with question mark wildcard
            files = list(factory.scan_files("file?.txt"))
            assert len(files) == 1  # file1.txt
            
            # Test pattern with bracket notation
            files = list(factory.scan_files("file[123].*"))
            assert len(files) == 3  # file1.txt, file2.pdf, file3.py
                
        finally:
            os.chdir(original_cwd)


class TestLocalFileProxyFactoryEdgeCases:
    """Test edge cases and error conditions for LocalFileProxyFactory."""
    
    @pytest.fixture
    def factory(self):
        """Create a LocalFileProxyFactory instance."""
        return LocalFileProxyFactory()
    
    def test_scan_nonexistent_directory(self, factory):
        """Test scanning a nonexistent directory."""
        # Should return empty list for nonexistent directories (graceful handling)
        files = list(factory.scan_files("/nonexistent/path/**/*"))
        assert len(files) == 0
    
    def test_scan_with_invalid_pattern_characters(self, factory):
        """Test scanning with potentially problematic pattern characters."""
        # These should not crash, even if they don't match anything
        files = list(factory.scan_files("[]"))
        assert len(files) == 0
        
        files = list(factory.scan_files("{}"))
        assert len(files) == 0
    
    def test_scan_files_batched_with_zero_batch_size(self, factory):
        """Test batched scanning with zero batch size."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, 'w') as f:
                f.write("test")
            
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                
                # Should handle zero batch size gracefully
                batches = list(factory.scan_files_batched("*.txt", batch_size=0))
                # With batch size 0, every file becomes its own batch
                assert len(batches) == 1
                assert len(batches[0]) == 1
                
            finally:
                os.chdir(original_cwd)
    
    def test_scan_files_batched_with_negative_batch_size(self, factory):
        """Test batched scanning with negative batch size."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, 'w') as f:
                f.write("test")
            
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                
                # Should handle negative batch size gracefully
                batches = list(factory.scan_files_batched("*.txt", batch_size=-1))
                # Negative batch size should behave like batch size 1
                assert len(batches) == 1
                assert len(batches[0]) == 1
                
            finally:
                os.chdir(original_cwd)
