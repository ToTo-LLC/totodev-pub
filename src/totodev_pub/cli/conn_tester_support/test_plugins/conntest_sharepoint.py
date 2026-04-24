#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SharePoint connection test plugin.

OVERVIEW
--------
Tests SharePoint connectivity by authenticating, accessing a site, searching for a file,
and downloading it to verify permissions.

AZURE AD APP REGISTRATION
--------------------------
1. Navigate to Azure Portal (portal.azure.com)
2. Go to Azure Active Directory > App registrations > New registration
3. Set name (e.g., "SharePoint Connection Tester")
4. Set redirect URI: None needed (daemon/service app)
5. Register and note the Application (client) ID

GRANTING API PERMISSIONS
-------------------------
Required Microsoft Graph API Permissions (Application permissions):
- Sites.Read.All - Required to read all site collections
- Files.Read.All - Required to read files in all site collections

Alternative (more restrictive):
- Sites.Selected - For access to specific sites only (requires additional configuration)

Steps to grant permissions:
1. In your app registration, go to API permissions
2. Add permission > Microsoft Graph > Application permissions
3. Select: Sites.Read.All and Files.Read.All
4. Click "Grant admin consent" (requires Global Admin or Application Admin role)
5. Wait 5-10 minutes for permission propagation

Note: If you encounter "Access Denied" errors, verify these permissions are granted
and that admin consent has been provided.

CREATING CLIENT SECRET
-----------------------
1. In your app, go to Certificates & secrets
2. New client secret
3. Add description and set expiration
4. Copy the secret VALUE immediately (shown only once)

FINDING YOUR TENANT ID
-----------------------
Azure Portal > Azure Active Directory > Overview > Tenant ID

FINDING SITE NAME AND DOMAIN
-----------------------------
Your SharePoint URL format: https://{domain}/sites/{site_name}
Example: https://mycompany.sharepoint.com/sites/ProjectSite
- domain: mycompany.sharepoint.com
- site_name: ProjectSite

DRIVE ID AND DRIVE NAME (DOCUMENT LIBRARY)
-------------------------------------------
You can specify either a drive ID or drive name, or omit both for auto-discovery.

Drive IDs have a specific format: they start with "b!" followed by a long alphanumeric string.
Example: b!zdWM8Urp8k68K4dE9vEBtGU3KzqQN-xKiKPdUlydX8OojUfUj_9rR7Pm3GuC9d2i

Method 1 - Auto-Discovery (Recommended):
- Omit both --drive-id and --drive-name parameters
- The test will automatically discover available drives
- It will select "Documents" or "Shared Documents" if available
- All discovered drives will be listed in the output

Method 2 - Drive Name (Easier):
- Use --drive-name "Documents" or --drive-name "Shared Documents"
- The test will find the drive by name (case-insensitive)
- Available drive names will be shown if the specified name is not found

Method 3 - Drive ID (Most Specific):
- Use --drive-id with the full drive ID
- Microsoft Graph Explorer (https://developer.microsoft.com/graph/graph-explorer):
  1. Sign in with admin account
  2. GET https://graph.microsoft.com/v1.0/sites/{domain}:/sites/{site_name}:/drives
  3. Find your document library in the response
  4. Copy the "id" field value (should start with "b!")

COMMON DOCUMENT LIBRARY NAMES
------------------------------
- "Documents" (default library)
- "Shared Documents"
- "Document Library"
- Custom library names you've created

USAGE EXAMPLES
--------------
export SHAREPOINT_CLIENT_ID="a1b2c3d4-..."
export SHAREPOINT_CLIENT_SECRET="secret_value_here"
export SHAREPOINT_TENANT_ID="e5f6g7h8-..."

# Auto-discovery (recommended - discovers drives automatically)
python conn_tester.py sharepoint \\
  --site-name ProjectSite \\
  --domain mycompany.sharepoint.com \\
  --find-file report.pdf \\
  --file logfile.yaml \\
  --verbose

# Using drive name (easier than drive ID)
python conn_tester.py sharepoint \\
  --site-name ProjectSite \\
  --domain mycompany.sharepoint.com \\
  --drive-name "Documents" \\
  --find-file report.pdf \\
  --file logfile.yaml \\
  --verbose

# Using specific drive ID (most specific)
python conn_tester.py sharepoint \\
  --site-name ProjectSite \\
  --domain mycompany.sharepoint.com \\
  --drive-id b!zdWM8Urp8k68K4dE9vEBtGU3... \\
  --find-file report.pdf \\
  --file logfile.yaml \\
  --verbose
"""

from __future__ import annotations

import fnmatch
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, Type

from conn_tester_support.core import TestTypeBase, _hash_credential
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve
from conn_tester_support.test_plugins.conntest_https_responds import TestTypeHttpsResponds

try:
    import msal
    import requests
except ImportError:
    # Will be handled in run_test method
    pass


class TestTypeSharepoint(TestTypeBase):
    """SharePoint connectivity and file access testing with automatic drive discovery.
    
    This test plugin provides comprehensive SharePoint connectivity testing including:
    - Authentication using Azure AD app credentials
    - Automatic discovery of available document libraries (drives)
    - Intelligent drive selection by name or ID
    - File search and download verification
    - Detailed error reporting with actionable advice
    
    The plugin supports three modes of operation:
    1. Auto-discovery: Automatically finds and selects appropriate drives
    2. Drive name selection: Selects drives by user-friendly names
    3. Drive ID specification: Uses exact drive IDs for precise control
    """
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests SharePoint connectivity and file access permissions",
            config_fields={
                "site_name": "SharePoint site name (e.g., 'ProjectSite')",
                "domain": "SharePoint domain (e.g., 'mycompany.sharepoint.com')",
                "drive_id": "SharePoint drive/document library ID (optional - will auto-discover if not provided)",
                "drive_name": "SharePoint drive/document library name for selection (optional - used when drive_id not provided)",
                "find_file": "Filename to search for and download (supports glob patterns like *.pdf)",
                "start_folder": "Starting directory path for search (default: 'root')",
                "timeout_s": "Connection timeout in seconds (default: 60)",
                "case_sensitive": "Whether filename matching should be case-sensitive (default: false)",
                "client_id": "SharePoint application client ID",
                "tenant_id": "SharePoint tenant ID"
            },
            required_fields=["site_name", "domain", "find_file", "client_id", "tenant_id"],
            optional_fields=["drive_id", "drive_name", "start_folder", "timeout_s", "case_sensitive"],
            confidential_fields=["SHAREPOINT_CLIENT_SECRET"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve, TestTypeHttpsResponds]
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Check for required dependencies
        try:
            import msal
            import requests
        except ImportError as e:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Required libraries not available: {e}",
                advice=[
                    "Install required dependencies: pip install msal requests",
                    "These libraries are needed for SharePoint authentication and API calls"
                ]
            )
        
        # Validate configuration
        if not self.config.get("site_name"):
            raise ConfigurationError("site_name is required")
        if not self.config.get("domain"):
            raise ConfigurationError("domain is required")
        if not self.config.get("find_file"):
            raise ConfigurationError("find_file is required")
        
        site_name = self.config.get("site_name")
        domain = self.config.get("domain")
        drive_id = self.config.get("drive_id")  # Optional - will be resolved if not provided
        drive_name = self.config.get("drive_name")  # Optional - used for drive selection
        find_file = self.config.get("find_file")
        start_folder = self.config.get("start_folder") or "root"
        timeout_s = self.config.get("timeout_s") or 60
        case_sensitive = self.config.get("case_sensitive") or False
        
        # Validate timeout
        if timeout_s is None:
            timeout_s = 60
        try:
            timeout_s = int(timeout_s)
            if timeout_s <= 0:
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Invalid timeout value: {timeout_s}",
                    advice=["Timeout must be a positive integer"]
                )
        except (ValueError, TypeError):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid timeout value: {timeout_s}",
                advice=["Timeout must be a valid integer"]
            )
        
        # Validate credentials - check config first, then environment variables
        client_id = self.config.get('client_id') or os.getenv('SHAREPOINT_CLIENT_ID')
        client_secret = os.getenv('SHAREPOINT_CLIENT_SECRET')  # Always from environment for security
        tenant_id = self.config.get('tenant_id') or os.getenv('SHAREPOINT_TENANT_ID')
        
        if not client_id:
            return self.create_env_var_error(
                env_var_name='SHAREPOINT_CLIENT_ID',
                description='SharePoint authentication',
                alternative_suggestion='Or provide --client-id option'
            )
        if not client_secret:
            return self.create_env_var_error(
                env_var_name='SHAREPOINT_CLIENT_SECRET',
                description='SharePoint authentication'
            )
        if not tenant_id:
            return self.create_env_var_error(
                env_var_name='SHAREPOINT_TENANT_ID',
                description='SharePoint authentication',
                alternative_suggestion='Or provide --tenant-id option'
            )
        
        # Run prerequisite tests
        prerequisite_results = {}
        
        # DNS resolution
        dns_test = TestTypeDnsResolve({"hostname": domain})
        dns_result = dns_test.run_test()
        prerequisite_results.update(dns_result.extra_detail)
        
        if not dns_result.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message="DNS resolution failed",
                advice=dns_result.advice,
                extra_detail=prerequisite_results
            )
        
        # HTTPS connectivity
        https_test = TestTypeHttpsResponds({"host": domain, "port": 443})
        https_result = https_test.run_test()
        prerequisite_results.update(https_result.extra_detail)
        
        if not https_result.success:
            return TestResult(
                success=False,
                error_type="tcp_connect",
                error_message="HTTPS connectivity failed",
                advice=https_result.advice,
                extra_detail=prerequisite_results
            )
        
        # Perform SharePoint authentication and testing
        drive_discovery_info = {}  # Initialize empty drive discovery info
        try:
            # Authenticate
            if logger:
                logger.info("🔐 Authenticating with SharePoint...")
            
            access_token = self._authenticate(client_id, client_secret, tenant_id)
            
            if logger:
                logger.info("✅ Authentication successful")
            
            # Verify site access
            if logger:
                logger.info(f"🏢 Accessing SharePoint site: {site_name}")
            
            site_id = self._verify_site_access(access_token, domain, site_name, drive_id)
            
            if logger:
                logger.info("✅ Site access confirmed")
            
            # Resolve drive ID if not provided
            resolved_drive_id, resolved_drive_name, drive_discovery_info = self._resolve_drive_id(
                access_token, site_id, drive_id, drive_name, logger
            )
            
            # Search for file
            if logger:
                logger.info(f"🔍 Searching for file: {find_file}")
            
            file_path, directories_scanned, search_time_ms = self._search_for_file(
                access_token, resolved_drive_id, find_file, start_folder, case_sensitive, logger
            )
            
            if file_path is None:
                return TestResult(
                    success=False,
                    error_type="file_not_found",
                    error_message=f"Target file '{find_file}' not found in SharePoint site",
                    advice=[
                        f"Verify the file '{find_file}' exists in the site",
                        f"Check if the file is in a subdirectory of '{start_folder}'",
                        "Try searching from 'root' folder if using a specific start folder",
                        "Verify the filename is spelled correctly",
                        "Check if the file has been moved or deleted",
                        "Use glob patterns like '*.pdf' or '*.docx' to find files by extension"
                    ],
                    extra_detail=self._build_sharepoint_extra_detail(
                        drive_discovery_info,
                        target_filename=find_file,
                        error="file_not_found"
                    )
                )
            
            if logger:
                logger.info(f"✅ Found file: {file_path}")
            
            # Download file to verify permissions
            if logger:
                logger.info("📥 Downloading file to verify permissions...")
            
            temp_file_path, file_size_bytes, download_time_ms = self._download_file(
                access_token, resolved_drive_id, file_path, timeout_s
            )
            
            if logger:
                logger.info(f"✅ File download successful ({file_size_bytes} bytes)")
            
            # Clean up temporary file
            if logger:
                logger.info("🗑️ Cleaning up downloaded file...")
            
            try:
                os.remove(temp_file_path)
                if logger:
                    logger.info("✅ Test completed successfully")
            except OSError as e:
                if logger:
                    logger.warning(f"⚠️ Could not clean up temporary file: {e}")
            
            # Calculate total time
            total_time_ms = search_time_ms + download_time_ms
            
            return TestResult(
                success=True,
                extra_detail=self._build_sharepoint_extra_detail(
                    drive_discovery_info,
                    file_found=file_path,
                    file_size_bytes=file_size_bytes
                )
            )
            
        except Exception as e:
            # Handle various SharePoint-specific errors
            error_msg = str(e)
            error_details = {
                "error": error_msg,
                "exception_type": type(e).__name__
            }
            
            if "authentication" in error_msg.lower() or "unauthorized" in error_msg.lower():
                error_type = "sharepoint_auth_failed"
                advice = [
                    "Check your SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET, and SHAREPOINT_TENANT_ID",
                    "Verify the client secret has not expired",
                    "Ensure the Azure AD app has Sites.Read.All and Files.Read.All permissions",
                    "Confirm admin consent has been granted for the permissions",
                    "Wait 5-10 minutes after granting permissions for propagation"
                ]
            elif "access denied" in error_msg.lower() or "forbidden" in error_msg.lower():
                error_type = "sharepoint_access_denied"
                advice = [
                    "Verify your app has Sites.Read.All and Files.Read.All permissions",
                    "Check that admin consent has been granted",
                    "Ensure the app is registered in the correct tenant",
                    "Verify the site name and domain are correct",
                    "Check if the drive ID is valid and accessible"
                ]
            elif "not found" in error_msg.lower() or "404" in error_msg.lower():
                error_type = "sharepoint_site_not_found"
                advice = [
                    "Verify the site name is correct",
                    "Check that the domain is correct",
                    "Ensure the site exists and is accessible",
                    "Verify the drive ID corresponds to a valid document library"
                ]
            elif "invalid" in error_msg.lower() and "drive id" in error_msg.lower():
                error_type = "sharepoint_drive_id_invalid"
                advice = [
                    "Verify the drive ID format is correct (should start with 'b!')",
                    "Use Microsoft Graph Explorer to find the correct drive ID",
                    "GET https://graph.microsoft.com/v1.0/sites/{domain}:/sites/{site_name}:/drives",
                    "Copy the 'id' field from the document library you want to access",
                    "Ensure the drive ID corresponds to a document library (not a regular folder)",
                    "Alternatively, omit --drive-id and use --drive-name to auto-discover drives"
                ]
            elif "drive discovery failed" in error_msg.lower() or "unable to list available drives" in error_msg.lower():
                error_type = "sharepoint_drive_discovery_failed"
                advice = [
                    "Ensure your app has 'Sites.Read.All' permission with admin consent granted",
                    "This permission is required to discover document libraries",
                    "Wait 5-10 minutes after granting permissions for propagation",
                    "Verify the site name and domain are correct",
                    "Check that the SharePoint site exists and is accessible"
                ]
            elif "drive" in error_msg.lower() and "not found" in error_msg.lower():
                error_type = "sharepoint_drive_not_found"
                advice = [
                    "Verify the drive name is spelled correctly (case-insensitive)",
                    "Check that the drive exists in the SharePoint site",
                    "Use --drive-name to specify a different drive",
                    "Or omit drive parameters to auto-discover available drives",
                    "Ensure the drive corresponds to a document library (not a regular folder)"
                ]
            elif "timeout" in error_msg.lower():
                error_type = "sharepoint_timeout"
                advice = [
                    "Check network connectivity to SharePoint",
                    "Try increasing the timeout value",
                    "Verify the SharePoint service is available",
                    "Check for firewall or proxy issues"
                ]
            else:
                error_type = "sharepoint_connect_failed"
                advice = [
                    "Check SharePoint service availability",
                    "Verify network connectivity",
                    "Check firewall and proxy settings",
                    "Review SharePoint service logs for additional details"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"SharePoint connection error: {error_msg}",
                advice=advice,
                extra_detail=self._build_sharepoint_extra_detail(
                    drive_discovery_info,
                    error=error_msg,
                    exception_type=type(e).__name__
                )
            )
    
    def _authenticate(self, client_id: str, client_secret: str, tenant_id: str) -> str:
        """Authenticate with SharePoint using MSAL client credentials flow."""
        try:
            app = msal.ConfidentialClientApplication(
                client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
                client_credential=client_secret
            )
            
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            
            if "access_token" not in result:
                error_msg = result.get('error_description', 'Unknown authentication error')
                raise RuntimeError(f"Failed to get access token: {error_msg}")
            
            return result["access_token"]
            
        except Exception as e:
            raise RuntimeError(f"Authentication failed: {e}")
    
    def _list_available_drives(self, access_token: str, site_id: str) -> List[Dict[str, str]]:
        """Query Microsoft Graph API to get all available drives for a SharePoint site."""
        headers = {'Authorization': f'Bearer {access_token}'}
        
        # Get all drives for the site
        drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        response = requests.get(drives_url, headers=headers, timeout=30)
        
        if response.status_code == 403:
            raise RuntimeError("Unable to list available drives. Ensure your app has 'Sites.Read.All' permission with admin consent granted. This permission is required to discover document libraries.")
        elif response.status_code != 200:
            raise RuntimeError(f"Failed to list drives: HTTP {response.status_code}")
        
        drives_data = response.json()
        drives = drives_data.get('value', [])
        
        if not drives:
            raise RuntimeError("No document libraries found in the SharePoint site. Verify site access and permissions.")
        
        # Format drives for return
        formatted_drives = []
        for drive in drives:
            formatted_drives.append({
                'id': drive['id'],
                'name': drive.get('name', 'Unknown'),
                'description': drive.get('description', '')
            })
        
        return formatted_drives
    
    def _select_drive(self, drives: List[Dict[str, str]], drive_name: Optional[str]) -> Tuple[str, str, str]:
        """Select a drive from available drives based on name or default selection logic."""
        if drive_name:
            # Search for drive by name (case-insensitive)
            drive_name_lower = drive_name.lower()
            for drive in drives:
                if drive['name'].lower() == drive_name_lower:
                    return drive['id'], drive['name'], f"specified_name: '{drive_name}'"
            
            # Drive name not found - create helpful error message
            available_names = [drive['name'] for drive in drives]
            available_info = [f"{drive['name']} ({drive['id'][:20]}...)" for drive in drives]
            raise RuntimeError(f"Drive '{drive_name}' not found. Available drives: {', '.join(available_info)}. Use one of these names or specify the drive ID directly.")
        
        # Auto-select default drive
        default_names = ['Documents', 'Shared Documents', 'Document Library']
        
        for default_name in default_names:
            for drive in drives:
                if drive['name'].lower() == default_name.lower():
                    return drive['id'], drive['name'], f"auto_default: '{default_name}'"
        
        # Fallback to first available drive
        if drives:
            first_drive = drives[0]
            return first_drive['id'], first_drive['name'], f"fallback_first: '{first_drive['name']}'"
        
        raise RuntimeError("No drives available for selection")
    
    def _resolve_drive_id(self, access_token: str, site_id: str, drive_id: Optional[str], 
                         drive_name: Optional[str], logger: Optional[Any]) -> Tuple[str, str, Dict[str, Any]]:
        """Orchestrate drive resolution logic with comprehensive logging and error handling."""
        if drive_id:
            # Drive ID provided - verify it exists and get its name
            if logger:
                logger.info(f"🔍 Verifying provided drive ID: {drive_id[:20]}...")
            
            headers = {'Authorization': f'Bearer {access_token}'}
            drive_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}"
            drive_response = requests.get(drive_url, headers=headers, timeout=30)
            
            if drive_response.status_code == 404:
                raise RuntimeError(f"SharePoint drive not found: {drive_id}")
            elif drive_response.status_code == 403:
                raise RuntimeError(f"Access denied to SharePoint drive: {drive_id}. Check permissions.")
            elif drive_response.status_code != 200:
                raise RuntimeError(f"Failed to access SharePoint drive: HTTP {drive_response.status_code}")
            
            drive_data = drive_response.json()
            resolved_name = drive_data.get('name', 'Unknown')
            
            if logger:
                logger.info(f"✅ Verified drive: '{resolved_name}' (ID: {drive_id[:20]}...)")
            
            return drive_id, resolved_name, {
                "selection_method": "specified_id",
                "selected_drive": {"id": drive_id, "name": resolved_name}
            }
        
        # Drive ID not provided - discover and select
        if logger:
            logger.info("🔍 Discovering available document libraries...")
        
        try:
            drives = self._list_available_drives(access_token, site_id)
            
            if logger:
                logger.info(f"📚 Found {len(drives)} drives:")
                for drive in drives:
                    logger.info(f"  - {drive['name']} (ID: {drive['id'][:20]}...)")
            
            selected_id, selected_name, selection_reason = self._select_drive(drives, drive_name)
            
            if logger:
                logger.info(f"✅ Selected drive: '{selected_name}' (ID: {selected_id[:20]}...) - {selection_reason}")
            
            return selected_id, selected_name, {
                "selection_method": selection_reason.split(':')[0],
                "all_available_drives": drives,
                "selected_drive": {"id": selected_id, "name": selected_name}
            }
            
        except Exception as e:
            if logger:
                logger.error(f"❌ Drive discovery failed: {str(e)}")
            raise e
    
    def _verify_site_access(self, access_token: str, domain: str, site_name: str, drive_id: Optional[str] = None) -> str:
        """Verify access to SharePoint site and return site ID."""
        headers = {'Authorization': f'Bearer {access_token}'}
        
        # Get site information
        site_url = f"https://graph.microsoft.com/v1.0/sites/{domain}:/sites/{site_name}"
        response = requests.get(site_url, headers=headers, timeout=30)
        
        if response.status_code == 404:
            raise RuntimeError(f"SharePoint site not found: {site_name} on {domain}")
        elif response.status_code == 403:
            raise RuntimeError(f"Access denied to SharePoint site: {site_name}. Check permissions.")
        elif response.status_code != 200:
            raise RuntimeError(f"Failed to access SharePoint site: HTTP {response.status_code}")
        
        site_data = response.json()
        site_id = site_data['id']
        
        # Only verify drive access if drive_id is provided
        if drive_id:
            # Verify drive access
            drive_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}"
            drive_response = requests.get(drive_url, headers=headers, timeout=30)
            
            if drive_response.status_code == 404:
                raise RuntimeError(f"SharePoint drive not found: {drive_id}")
            elif drive_response.status_code == 403:
                raise RuntimeError(f"Access denied to SharePoint drive: {drive_id}. Check permissions.")
            elif drive_response.status_code == 400:
                raise RuntimeError(f"Invalid SharePoint drive ID format: {drive_id}. Drive ID should start with 'b!' and be a valid SharePoint drive identifier.")
            elif drive_response.status_code != 200:
                raise RuntimeError(f"Failed to access SharePoint drive: HTTP {drive_response.status_code}")
        
        return site_id
    
    def _search_for_file(self, access_token: str, drive_id: str, target_filename: str, 
                        start_folder: str, case_sensitive: bool, logger: Optional[Any]) -> Tuple[Optional[str], int, float]:
        """Search for a file recursively in SharePoint."""
        start_time = time.time()
        directories_scanned = [0]  # Use list to allow modification by reference
        
        try:
            file_path = self._search_recursive(
                access_token, drive_id, target_filename, start_folder, 
                case_sensitive, logger, "", directories_scanned
            )
            search_time_ms = (time.time() - start_time) * 1000
            return file_path, directories_scanned[0], search_time_ms
        except Exception as e:
            search_time_ms = (time.time() - start_time) * 1000
            raise RuntimeError(f"File search failed: {e}")
    
    def _search_recursive(self, access_token: str, drive_id: str, target_filename: str,
                         folder_path: str, case_sensitive: bool, logger: Optional[Any],
                         current_path: str, directories_scanned: List[int]) -> Optional[str]:
        """Recursively search for a file in SharePoint folders."""
        if logger:
            logger.info(f"📂 Searching in folder: {current_path or 'root'}")
        
        directories_scanned[0] += 1
        
        try:
            items = self._scan_folder_contents(access_token, drive_id, folder_path)
            
            for item in items:
                item_name = item['name']
                item_path = f"{current_path}/{item_name}" if current_path else item_name
                
                if 'folder' in item:
                    # Search subfolder
                    if logger:
                        logger.info(f"📂 Searching subfolder: {item_path}")
                    
                    result = self._search_recursive(
                        access_token, drive_id, target_filename, item['id'],
                        case_sensitive, logger, item_path, directories_scanned
                    )
                    if result is not None:
                        return result
                else:
                    # Check if this file matches the target pattern (supports glob patterns)
                    item_filename = item_name if case_sensitive else item_name.lower()
                    target = target_filename if case_sensitive else target_filename.lower()
                    
                    # Use fnmatch for glob pattern matching
                    if fnmatch.fnmatch(item_filename, target):
                        if logger:
                            logger.info(f"✅ Found file: {item_path}")
                        return item_path
            
            return None
            
        except requests.exceptions.Timeout:
            if logger:
                logger.warning(f"⏰ Timeout searching folder: {current_path or 'root'}")
            return None
        except Exception as e:
            if logger:
                logger.warning(f"❌ Error searching folder {current_path or 'root'}: {str(e)}")
            return None
    
    def _scan_folder_contents(self, access_token: str, drive_id: str, folder_path: str) -> List[Dict[str, Any]]:
        """Get contents of a SharePoint folder."""
        headers = {'Authorization': f'Bearer {access_token}'}
        
        if folder_path == "root":
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
        else:
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{folder_path}/children"
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 404:
            raise RuntimeError(f"Folder not found: {folder_path}")
        elif response.status_code == 403:
            raise RuntimeError(f"Access denied to folder: {folder_path}")
        elif response.status_code != 200:
            raise RuntimeError(f"Failed to access folder: HTTP {response.status_code}")
        
        data = response.json()
        return data.get('value', [])
    
    def _download_file(self, access_token: str, drive_id: str, file_path: str, timeout_s: int) -> Tuple[str, int, float]:
        """Download a file from SharePoint to verify access."""
        start_time = time.time()
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/octet-stream'
        }
        
        # Create temporary file
        temp_fd, temp_file_path = tempfile.mkstemp()
        os.close(temp_fd)
        
        try:
            # Download file
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{file_path}:/content"
            response = requests.get(url, headers=headers, timeout=timeout_s, stream=True)
            
            if response.status_code == 404:
                raise RuntimeError(f"File not found: {file_path}")
            elif response.status_code == 403:
                raise RuntimeError(f"Access denied to file: {file_path}")
            elif response.status_code != 200:
                raise RuntimeError(f"Failed to download file: HTTP {response.status_code}")
            
            # Write file content
            with open(temp_file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # Get file size
            file_size_bytes = os.path.getsize(temp_file_path)
            download_time_ms = (time.time() - start_time) * 1000
            
            return temp_file_path, file_size_bytes, download_time_ms
            
        except Exception as e:
            # Clean up on error
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise e
    
    def _build_sharepoint_extra_detail(self, drive_discovery_info: Dict[str, Any], 
                                     file_found: Optional[str] = None, 
                                     file_size_bytes: Optional[int] = None,
                                     target_filename: Optional[str] = None,
                                     error: Optional[str] = None,
                                     exception_type: Optional[str] = None) -> Dict[str, Any]:
        """Build standardized SharePoint extra_detail structure."""
        sharepoint_detail = {
            "available_drives": drive_discovery_info.get("all_available_drives", []),
            "selected_drive": drive_discovery_info.get("selected_drive", {}),
            "selection_method": drive_discovery_info.get("selection_method", "unknown")
        }
        
        if file_found:
            sharepoint_detail["file_found"] = file_found
        if file_size_bytes is not None:
            sharepoint_detail["file_size_bytes"] = file_size_bytes
        if target_filename:
            sharepoint_detail["target_filename"] = target_filename
        if error:
            sharepoint_detail["error"] = error
        if exception_type:
            sharepoint_detail["exception_type"] = exception_type
            
        return {"sharepoint_connection": sharepoint_detail}
    
    def get_configs(self) -> Dict[str, Any]:
        """Return current configuration as dictionary with credential hashing and defaults applied"""
        config = self.config.copy()
        
        # Apply defaults for parameters that weren't explicitly set
        if config.get("start_folder") is None:
            config["start_folder"] = "root"
        if config.get("timeout_s") is None:
            config["timeout_s"] = 60
        if config.get("case_sensitive") is None:
            config["case_sensitive"] = False
        
        # Include drive_name if provided
        if config.get("drive_name"):
            config["drive_name"] = config["drive_name"]
        
        # Hash only the confidential credential (client secret)
        if 'SHAREPOINT_CLIENT_SECRET' in os.environ:
            client_secret = os.environ['SHAREPOINT_CLIENT_SECRET']
            config['client_secret'] = _hash_credential(client_secret)
        
        # Display non-confidential credentials in plaintext (from config or environment)
        client_id = config.get('client_id') or os.getenv('SHAREPOINT_CLIENT_ID')
        if client_id:
            config['client_id'] = client_id
            
        tenant_id = config.get('tenant_id') or os.getenv('SHAREPOINT_TENANT_ID')
        if tenant_id:
            config['tenant_id'] = tenant_id
        
        return config


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeSharepoint,
    shortname="sharepoint",
    is_public=True,
    description="Tests SharePoint connectivity and file access permissions"
)
