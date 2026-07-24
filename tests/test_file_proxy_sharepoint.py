# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test cases for SharePoint file proxy functionality.

These tests focus on business logic by mocking the API calls, allowing us to test
the core functionality without requiring actual SharePoint connections.

NOTE: This test suite does NOT include tests for the API functions themselves
(_api_get_folder_contents and _api_download_file_content). Those functions require
actual SharePoint API calls and should be tested separately with integration tests
that have proper authentication and network access.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime
from pathlib import Path
import tempfile
import os

pytest.importorskip("aiohttp")  # 'connectors' extra; skip when unavailable

from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import (
    SharepointFileProxyFactory,
    SharepointFileProxy
)


class TestSharepointFileProxyFactory:
    """Test cases for SharepointFileProxyFactory business logic."""
    
    @pytest.fixture
    def factory(self):
        """Create a factory instance for testing."""
        return SharepointFileProxyFactory(
            site_id="test-site-id",
            drive_id="test-drive-id", 
            access_token="test-token",
            site_name="TestSite"
        )
    
    @pytest.fixture
    def mock_folder_contents(self):
        """Mock SharePoint folder contents response (files only, no folders)."""
        return {
            "value": [
                {
                    "id": "file1-id",
                    "name": "document.pdf",
                    "size": 1024,
                    "lastModifiedDateTime": "2024-01-01T12:00:00Z",
                    "webUrl": "https://test.sharepoint.com/file1",
                    "@microsoft.graph.downloadUrl": "https://test.sharepoint.com/download/file1",
                    "mimeType": "application/pdf"
                },
                {
                    "id": "file2-id",
                    "name": "report.docx",
                    "size": 2048,
                    "lastModifiedDateTime": "2024-01-02T10:30:00Z",
                    "webUrl": "https://test.sharepoint.com/file2",
                    "@microsoft.graph.downloadUrl": "https://test.sharepoint.com/download/file2",
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                }
            ]
        }
    
    @pytest.fixture
    def mock_empty_folder_contents(self):
        """Mock empty SharePoint folder contents response."""
        return {"value": []}
    
    def test_factory_initialization(self, factory):
        """Test factory initialization with all parameters."""
        assert factory.site_id == "test-site-id"
        assert factory.drive_id == "test-drive-id"
        assert factory.access_token == "test-token"
        assert factory.site_name == "TestSite"
        assert factory.base_url == "https://graph.microsoft.com/v1.0"
    
    def test_create_proxy(self, factory):
        """Test creating a SharepointFileProxy instance."""
        proxy = factory.create(
            file_path="Documents/test.pdf",
            file_size=1024,
            last_modified=datetime(2024, 1, 1, 12, 0, 0)
        )
        
        assert isinstance(proxy, SharepointFileProxy)
        assert proxy.file_path == "Documents/test.pdf"
        assert proxy.file_size == 1024
        assert proxy.site_name == "TestSite"
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_success(self, MockApiClient, factory, mock_folder_contents):
        """Test successful file finding by name."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        result = factory.find_file_by_name("document.pdf")
        
        assert result == "document.pdf"
        mock_api.get_folder_contents.assert_called_once()
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_case_insensitive(self, MockApiClient, factory, mock_folder_contents):
        """Test file finding with case insensitive matching."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        result = factory.find_file_by_name("DOCUMENT.PDF", case_sensitive=False)
        
        assert result == "document.pdf"
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_case_sensitive(self, MockApiClient, factory, mock_folder_contents):
        """Test file finding with case sensitive matching."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        result = factory.find_file_by_name("DOCUMENT.PDF", case_sensitive=True)
        
        assert result is None  # Should not find due to case mismatch
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_not_found(self, MockApiClient, factory, mock_folder_contents):
        """Test file finding when file doesn't exist."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        result = factory.find_file_by_name("nonexistent.pdf")
        
        assert result is None
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_empty_filename(self, MockApiClient, factory):
        """Test file finding with empty filename."""
        result = factory.find_file_by_name("")
        
        assert result is None
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_recursive_search(self, MockApiClient, factory):
        """Test recursive file search in subfolders."""
        # First call returns folder with subfolder
        # Second call returns files in subfolder
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(side_effect=[
            {
                "value": [
                    {
                        "id": "folder1-id",
                        "name": "subfolder", 
                        "folder": {},
                        "lastModifiedDateTime": "2024-01-01T12:00:00Z"
                    }
                ]
            },
            {
                "value": [
                    {
                        "id": "file1-id",
                        "name": "target.pdf",
                        "size": 1024,
                        "lastModifiedDateTime": "2024-01-01T12:00:00Z",
                        "webUrl": "https://test.sharepoint.com/file1",
                        "@microsoft.graph.downloadUrl": "https://test.sharepoint.com/download/file1",
                        "mimeType": "application/pdf"
                    }
                ]
            }
        ])
        
        result = factory.find_file_by_name("target.pdf", include_subfolders=True)
        
        assert result == "subfolder/target.pdf"
        assert mock_api.get_folder_contents.call_count == 2
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_find_file_by_name_no_subfolders(self, MockApiClient, factory):
        """Test file search without including subfolders."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value={
            "value": [
                {
                    "id": "folder1-id",
                    "name": "subfolder",
                    "folder": {},
                    "lastModifiedDateTime": "2024-01-01T12:00:00Z"
                }
            ]
        })
        
        result = factory.find_file_by_name("target.pdf", include_subfolders=False)
        
        assert result is None
        mock_api.get_folder_contents.assert_called_once()  # Should not recurse into subfolder
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_scan_files_with_filters(self, MockApiClient, factory, mock_folder_contents):
        """Test file scanning with various filters."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        proxies = list(factory.scan_files(
            file_extensions={'.pdf'},
            min_size_bytes=500,
            max_size_bytes=1500
        ))
        
        assert len(proxies) == 1  # Only document.pdf matches
        assert proxies[0].file_path == "document.pdf"
        assert proxies[0].file_size == 1024
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_scan_files_date_filtering(self, MockApiClient, factory, mock_folder_contents):
        """Test file scanning with date filters."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        proxies = list(factory.scan_files(
            modified_after=datetime(2024, 1, 1, 11, 0, 0),
            modified_before=datetime(2024, 1, 2, 12, 0, 0)
        ))
        
        assert len(proxies) == 2  # Both files match date range
        file_paths = [proxy.file_path for proxy in proxies]
        assert "document.pdf" in file_paths
        assert "report.docx" in file_paths
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_scan_files_name_pattern(self, MockApiClient, factory, mock_folder_contents):
        """Test file scanning with name pattern matching."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        proxies = list(factory.scan_files(name_pattern="*report*"))
        
        assert len(proxies) == 1
        assert proxies[0].file_path == "report.docx"
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_scan_directory_wrapper(self, MockApiClient, factory, mock_folder_contents):
        """Test scan_directory method as wrapper around scan_files."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value=mock_folder_contents)
        
        proxies = list(factory.scan_directory("Documents/Reports"))
        
        assert len(proxies) == 2
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_api_error_handling(self, MockApiClient, factory):
        """Test error handling when API calls fail."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(side_effect=Exception("Network error"))
        
        # Should not raise exception with default skip_errors=True
        result = factory.find_file_by_name("test.pdf")
        assert result is None
        
        # Test that the exception is properly caught and logged
        # The recursive function catches exceptions and logs them as warnings
        # but doesn't re-raise them, so the main function returns None
        mock_api.get_folder_contents = Mock(side_effect=Exception("Network error"))
        result = factory.find_file_by_name("test.pdf", skip_errors=True)
        assert result is None


class TestSharepointFileProxy:
    """Test cases for SharepointFileProxy business logic."""
    
    @pytest.fixture
    def proxy(self):
        """Create a proxy instance for testing."""
        return SharepointFileProxy(
            site_id="test-site-id",
            drive_id="test-drive-id",
            file_path="Documents/test.pdf",
            access_token="test-token",
            file_size=1024,
            last_modified=datetime(2024, 1, 1, 12, 0, 0),
            site_name="TestSite"
        )
    
    def test_proxy_initialization(self, proxy):
        """Test proxy initialization with all parameters."""
        assert proxy.site_id == "test-site-id"
        assert proxy.drive_id == "test-drive-id"
        assert proxy.file_path == "Documents/test.pdf"
        assert proxy.access_token == "test-token"
        assert proxy.file_size == 1024
        assert proxy.site_name == "TestSite"
        assert proxy.base_url == "https://graph.microsoft.com/v1.0"
        assert not proxy._materialization_started
        assert not proxy._materialization_completed
        assert not proxy._was_deployed
    
    def test_ref_path_with_site_name(self, proxy):
        """Test ref_path generation with site name."""
        expected = "sharepoint://TestSite/Documents/test.pdf"
        assert proxy.ref_path() == expected
    
    def test_ref_path_without_site_name(self):
        """Test ref_path generation without site name."""
        proxy = SharepointFileProxy(
            site_id="test-site-id",
            drive_id="test-drive-id",
            file_path="Documents/test.pdf",
            access_token="test-token"
        )
        expected = "sharepoint://test-site-id/test-drive-id/Documents/test.pdf"
        assert proxy.ref_path() == expected
    
    def test_ref_path_removes_leading_slash(self):
        """Test that ref_path removes leading slash from file_path."""
        proxy = SharepointFileProxy(
            site_id="test-site-id",
            drive_id="test-drive-id", 
            file_path="/Documents/test.pdf",
            access_token="test-token",
            site_name="TestSite"
        )
        expected = "sharepoint://TestSite/Documents/test.pdf"
        assert proxy.ref_path() == expected
    
    @pytest.mark.asyncio
    async def test_materialize_success(self, proxy):
        """Test successful file materialization."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Mock the API call to write content matching the expected file size
            async def mock_download(url, temp_path):
                with open(temp_path, 'wb') as f:
                    # Write exactly the expected file size
                    content = b"x" * proxy.file_size
                    f.write(content)
            
            proxy._api_client.download_file_content = AsyncMock(side_effect=mock_download)
            result = await proxy.materialize(blocking_secs=1.0, temp_dir=Path(temp_dir))
            
            assert result is True
            assert proxy._materialization_started is True
            assert proxy._materialization_completed is True
            assert proxy._local_file_path is not None
    
    @pytest.mark.asyncio
    async def test_materialize_already_completed(self, proxy):
        """Test materialization when already completed."""
        with tempfile.TemporaryDirectory() as temp_dir:
            proxy._materialization_completed = True
            mock_download = AsyncMock()
            proxy._api_client.download_file_content = mock_download
            
            result = await proxy.materialize(blocking_secs=1.0, temp_dir=Path(temp_dir))
            
            assert result is True
            mock_download.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_materialize_missing_temp_dir(self, proxy):
        """Test materialization with missing temp_dir parameter."""
        with pytest.raises(ValueError, match="temp_dir must be provided"):
            await proxy.materialize(blocking_secs=1.0, temp_dir=None)
    
    @pytest.mark.asyncio
    async def test_materialize_download_error(self, proxy):
        """Test materialization when download fails."""
        with tempfile.TemporaryDirectory() as temp_dir:
            proxy._api_client.download_file_content = AsyncMock(side_effect=RuntimeError("Download failed"))
            
            with pytest.raises(RuntimeError, match="Failed to download SharePoint file"):
                await proxy.materialize(blocking_secs=1.0, temp_dir=Path(temp_dir))
    
    def test_deploy_success(self, proxy):
        """Test successful file deployment."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a temporary file to simulate materialized state
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_text("test content")
            
            proxy._local_file_path = str(test_file)
            proxy._materialization_completed = True
            
            deploy_dir = Path(temp_dir) / "deploy"
            deploy_dir.mkdir()
            
            proxy.deploy(str(deploy_dir))
            
            assert proxy._was_deployed is True
            assert (deploy_dir / "test.pdf").exists()
            assert not test_file.exists()  # Original temp file should be moved
    
    def test_deploy_already_deployed(self, proxy):
        """Test deployment when already deployed."""
        proxy._was_deployed = True
        
        with pytest.raises(RuntimeError, match="File has already been deployed"):
            proxy.deploy("/tmp")
    
    def test_deploy_not_materialized(self, proxy):
        """Test deployment when file not materialized."""
        with pytest.raises(RuntimeError, match="File must be materialized before deployment"):
            proxy.deploy("/tmp")
    
    def test_deploy_dev_null(self, proxy):
        """Test deployment to /dev/null (cleanup only)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_text("test content")
            
            proxy._local_file_path = str(test_file)
            proxy._materialization_completed = True
            
            proxy.deploy("/dev/null")
            
            assert proxy._was_deployed is True
            assert not test_file.exists()  # File should be deleted
    
    def test_deploy_target_dir_not_exists(self, proxy):
        """Test deployment when target directory doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_text("test content")
            
            proxy._local_file_path = str(test_file)
            proxy._materialization_completed = True
            
            with pytest.raises(RuntimeError, match="Target directory does not exist"):
                proxy.deploy("/nonexistent/directory")
    
    def test_deploy_preserves_modification_time(self, proxy):
        """Test that deployment preserves file modification time."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_text("test content")
            
            # Set a specific modification time
            test_time = datetime(2024, 1, 1, 12, 0, 0)
            proxy.last_modified = test_time
            proxy._local_file_path = str(test_file)
            proxy._materialization_completed = True
            
            deploy_dir = Path(temp_dir) / "deploy"
            deploy_dir.mkdir()
            
            proxy.deploy(str(deploy_dir))
            
            deployed_file = deploy_dir / "test.pdf"
            assert deployed_file.exists()
            
            # Check modification time is preserved
            deployed_mtime = datetime.fromtimestamp(deployed_file.stat().st_mtime)
            assert abs((deployed_mtime - test_time).total_seconds()) < 1  # Allow 1 second tolerance
    
    def test_looks_same_with_metadata(self, proxy):
        """Test looks_same method when metadata is available."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_bytes(b"test content")
            
            # Set modification time to match
            test_time = datetime(2024, 1, 1, 12, 0, 0)
            test_file.touch()
            os.utime(test_file, (test_time.timestamp(), test_time.timestamp()))
            
            proxy.last_modified = test_time
            proxy.file_size = len(b"test content")
            
            result = proxy.looks_same(str(test_file))
            assert result is True
    
    def test_looks_same_size_mismatch(self, proxy):
        """Test looks_same method when file size doesn't match."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_bytes(b"different content")
            
            proxy.file_size = 5  # Different size
            proxy.last_modified = datetime(2024, 1, 1, 12, 0, 0)
            
            result = proxy.looks_same(str(test_file))
            assert result is False
    
    def test_looks_same_no_metadata(self, proxy):
        """Test looks_same method when metadata is not available."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.pdf"
            test_file.write_text("test content")
            
            # No metadata set
            proxy.file_size = None
            proxy.last_modified = None
            
            result = proxy.looks_same(str(test_file))
            assert result is None
    
    def test_looks_same_file_not_exists(self, proxy):
        """Test looks_same method when file doesn't exist."""
        proxy.file_size = 1024
        proxy.last_modified = datetime(2024, 1, 1, 12, 0, 0)
        
        result = proxy.looks_same("/nonexistent/file.pdf")
        assert result is None
    
    def test_get_context_info(self, proxy):
        """Test get_context_info method."""
        context = proxy.get_context_info()
        
        assert context["proxy_type"] == "SharepointFileProxy"
        assert context["site_id"] == "test-site-id"
        assert context["site_name"] == "TestSite"
        assert context["drive_id"] == "test-drive-id"
        assert context["file_path"] == "Documents/test.pdf"
        assert context["file_size"] == 1024
        assert context["last_modified"] == datetime(2024, 1, 1, 12, 0, 0)
        assert context["local_file_path"] is None
        assert context["was_deployed"] is False
        assert context["materialization_started"] is False
        assert context["materialization_completed"] is False


class TestSharepointFileProxyIntegration:
    """Integration tests that test the interaction between factory and proxy."""
    
    @pytest.fixture
    def factory(self):
        """Create a factory instance for testing."""
        return SharepointFileProxyFactory(
            site_id="test-site-id",
            drive_id="test-drive-id",
            access_token="test-token",
            site_name="TestSite"
        )
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_sharepoint._SharePointGraphApiClient')
    def test_factory_create_proxy_workflow(self, MockApiClient, factory):
        """Test the complete workflow of factory creating proxies."""
        mock_api = factory._api_client
        mock_api.get_folder_contents = Mock(return_value={
            "value": [
                {
                    "id": "file1-id",
                    "name": "document.pdf",
                    "size": 1024,
                    "lastModifiedDateTime": "2024-01-01T12:00:00Z",
                    "webUrl": "https://test.sharepoint.com/file1",
                    "@microsoft.graph.downloadUrl": "https://test.sharepoint.com/download/file1",
                    "mimeType": "application/pdf"
                }
            ]
        })
        
        proxies = list(factory.scan_files())
        
        assert len(proxies) == 1
        proxy = proxies[0]
        
        # Verify proxy was created correctly
        assert isinstance(proxy, SharepointFileProxy)
        assert proxy.site_id == "test-site-id"
        assert proxy.drive_id == "test-drive-id"
        assert proxy.file_path == "document.pdf"
        assert proxy.file_size == 1024
        assert proxy.site_name == "TestSite"
        
        # Verify ref_path generation
        assert proxy.ref_path() == "sharepoint://TestSite/document.pdf"
