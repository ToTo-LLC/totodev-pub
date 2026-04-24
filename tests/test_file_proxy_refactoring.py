# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test to verify that the file proxy classes can be imported correctly after refactoring.
"""

import pytest
import tempfile
import os
from pathlib import Path
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy
from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import SharepointFileProxy
from totodev_pub.cached_file_folders import CachedFileFolders


def test_file_proxy_imports():
    """Test that all file proxy classes can be imported correctly."""
    # Test that the base class is abstract
    with pytest.raises(TypeError):
        FileProxyBase()
    
    # Test that concrete classes can be instantiated
    local_proxy = LocalFileProxy("/tmp/test.txt")
    assert local_proxy.ref_path() == "/tmp/test.txt"
    
    # Test SharePoint proxy instantiation
    sharepoint_proxy = SharepointFileProxy(
        site_id="test-site-id",
        drive_id="test-drive-id", 
        file_path="test/file.txt",
        access_token="test-token"
    )
    assert sharepoint_proxy.ref_path() == "sharepoint://test-site-id/test-drive-id/test/file.txt"


def test_cached_file_folders_import():
    """Test that CachedFileFolders can be imported and instantiated."""
    # Create a temporary directory for the test
    with tempfile.TemporaryDirectory() as temp_dir:
        # This should not raise any import errors
        cached_folders = CachedFileFolders("test/{group}/", temp_dir)
        assert cached_folders is not None


def test_file_proxy_inheritance():
    """Test that the file proxy classes properly inherit from FileProxyBase."""
    local_proxy = LocalFileProxy("/tmp/test.txt")
    sharepoint_proxy = SharepointFileProxy(
        site_id="test-site-id",
        drive_id="test-drive-id",
        file_path="test/file.txt", 
        access_token="test-token"
    )
    
    # Both should be instances of FileProxyBase
    assert isinstance(local_proxy, FileProxyBase)
    assert isinstance(sharepoint_proxy, FileProxyBase)
    
    # Both should have the required abstract methods
    assert hasattr(local_proxy, 'ref_path')
    assert hasattr(local_proxy, 'deploy')
    assert hasattr(local_proxy, 'materialize')
    assert hasattr(local_proxy, 'looks_same')
    
    assert hasattr(sharepoint_proxy, 'ref_path')
    assert hasattr(sharepoint_proxy, 'deploy')
    assert hasattr(sharepoint_proxy, 'materialize')
    assert hasattr(sharepoint_proxy, 'looks_same')
