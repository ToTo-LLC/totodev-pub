#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Google Sheets connection test plugin.

This module provides Google Sheets API connectivity testing using service account authentication,
including spreadsheet access, metadata retrieval, and cell reading capabilities.

## Authentication Setup

This test uses service account authentication:
1. Create a service account in Google Cloud Console
2. Enable Google Sheets API in Google Cloud Console
3. Share target spreadsheet with service account email (grant Viewer or Editor access)
4. Extract service account email and private key from credentials JSON

## Credential Setup

The test requires service account credentials provided as individual parameters:

**Required Parameters:**
- `--service-account-email`: Service account email (client_email from JSON)
- `--spreadsheet`: Spreadsheet ID or URL

**Environment Variable:**
- `GOOGLE_PRIVATE_KEY`: Service account private key (multi-line PEM format)

**Example with ID:**
```bash
export GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkq...
-----END PRIVATE KEY-----"

python -m totodev_pub.cli.conn_tester gsheets \
  --service-account-email my-sa@my-project.iam.gserviceaccount.com \
  --spreadsheet 1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M
```

**Example with URL:**
```bash
python -m totodev_pub.cli.conn_tester gsheets \
  --service-account-email my-sa@my-project.iam.gserviceaccount.com \
  --spreadsheet "https://docs.google.com/spreadsheets/d/1h9qlLlo.../edit"
```

## Common Issues

**Authentication Errors:**
- "invalid_grant" or "unauthorized_client": Service account credentials invalid
- "access_denied": OAuth scope issue or service account misconfigured

**API Errors:**
- "API not enabled": Enable Google Sheets API in Google Cloud Console
- "Insufficient Permission": Share spreadsheet with service account email
- "Requested entity was not found": Spreadsheet ID incorrect or spreadsheet deleted

**Spreadsheet Access:**
- "Permission denied": Spreadsheet not shared with service account
- Share the spreadsheet with the service account email address
- Grant at least "Viewer" access

Environment Variables:
    GOOGLE_PRIVATE_KEY: Service account private key (PEM format)
"""

from __future__ import annotations

import os
import re
import time
import hashlib
from typing import Any, Dict, List, Optional, Type
from urllib.parse import urlparse

from conn_tester_support.core import TestTypeBase, _hash_credential
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_TIMEOUT_SECONDS = 30

# Google Sheets API endpoints
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

# OAuth scopes
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _hash_service_account(service_account_info: Dict[str, Any]) -> str:
    """Create safe logging identifier: client_email + hashed private key"""
    client_email = service_account_info.get('client_email', 'unknown')
    private_key = service_account_info.get('private_key', '')
    key_hash = hashlib.sha256(private_key.encode()).hexdigest()[:12]
    return f"{client_email} (key_hash: {key_hash})"


def _build_service_account_info(service_account_email: str, private_key: str) -> Dict[str, Any]:
    """Build minimal service account info dict from individual components."""
    # Private key may have literal \n sequences from shell - convert to actual newlines
    # This handles cases where the key is exported like: export KEY="line1\nline2\nline3"
    normalized_key = private_key.replace('\\n', '\n')
    
    return {
        "type": "service_account",
        "private_key": normalized_key,
        "client_email": service_account_email,
        "token_uri": "https://oauth2.googleapis.com/token"
    }


def _extract_spreadsheet_id(spreadsheet_id_or_url: str) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Extract spreadsheet ID from either a direct ID or a Google Sheets URL.
    
    Supports:
    - Direct ID: "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
    - Full URL: "https://docs.google.com/spreadsheets/d/{ID}/edit#gid=0"
    - Short URL: "https://docs.google.com/spreadsheets/d/{ID}"
    
    Returns:
        (success, extracted_id, error_message)
    """
    if not spreadsheet_id_or_url:
        return (False, None, "spreadsheet_id or spreadsheet_url is empty")
    
    input_str = spreadsheet_id_or_url.strip()
    
    # Check if it's a URL (contains http:// or https://)
    if input_str.startswith(('http://', 'https://')):
        # Try to extract ID from URL
        # Pattern: /spreadsheets/d/{ID}/ or /spreadsheets/d/{ID}
        match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', input_str)
        if match:
            extracted_id = match.group(1)
            return (True, extracted_id, None)
        else:
            return (False, None, f"Could not extract spreadsheet ID from URL: {input_str}")
    else:
        # Assume it's a direct ID - validate it looks reasonable
        # Google Sheets IDs are alphanumeric with hyphens and underscores
        if re.match(r'^[a-zA-Z0-9-_]+$', input_str):
            return (True, input_str, None)
        else:
            return (False, None, f"Invalid spreadsheet ID format: {input_str}")


def _get_oauth_token(service_account_info: Dict[str, Any], 
                     timeout: int) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Acquire OAuth2 token using service account.
    
    Returns:
        (success, token, error_type, error_message)
    """
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
    except ImportError:
        return (False, None, 'missing_dependency', 
                "google-auth library not installed. Install: pip install google-auth")
    
    try:
        # Create credentials from service account info
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=[SHEETS_READONLY_SCOPE]
        )
        
        # Request token
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        
        if not credentials.token:
            return (False, None, 'auth_failed', "Failed to obtain access token")
        
        return (True, credentials.token, None, None)
        
    except Exception as e:
        error_str = str(e).lower()
        
        # Parse common Google auth errors
        if 'invalid_grant' in error_str or 'unauthorized_client' in error_str:
            return (False, None, 'invalid_credentials', 
                   f"Service account credentials invalid: {e}")
        elif 'invalid_scope' in error_str:
            return (False, None, 'invalid_scope', 
                   f"OAuth scope error: {e}")
        else:
            return (False, None, 'auth_failed', f"Authentication failed: {e}")


def _test_sheets_api_call(endpoint: str, token: str, timeout: int) -> tuple[bool, Optional[dict], Optional[str], Optional[str]]:
    """
    Make a Google Sheets API call and categorize errors by HTTP status.
    
    Returns:
        (success, response_data, error_type, error_message)
    """
    import requests
    
    headers = {'Authorization': f'Bearer {token}'}
    url = f"{SHEETS_API_BASE}{endpoint}"
    
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        
        if response.status_code == 200:
            return (True, response.json(), None, None)
        
        # Parse error response and categorize by HTTP status
        try:
            error_data = response.json()
            error_msg = error_data.get('error', {}).get('message', str(response.content))
            
            # Map Google Sheets API error codes by HTTP status
            if response.status_code == 401:
                return (False, None, 'auth_failed', f"Unauthorized: {error_msg}")
            elif response.status_code == 403:
                if 'not enabled' in error_msg.lower() or 'api has not been used' in error_msg.lower():
                    return (False, None, 'api_not_enabled', 
                           f"Google Sheets API not enabled: {error_msg}")
                return (False, None, 'permission_denied', f"Access denied: {error_msg}")
            elif response.status_code == 404:
                return (False, None, 'resource_not_found', f"Resource not found: {error_msg}")
            elif response.status_code == 429:
                return (False, None, 'rate_limited', "Rate limit exceeded")
            else:
                return (False, None, 'api_error', 
                       f"API error ({response.status_code}): {error_msg}")
                
        except Exception:
            return (False, None, 'api_error', 
                   f"API request failed with status {response.status_code}")
            
    except requests.Timeout:
        return (False, None, 'timeout', "Request timed out")
    except requests.ConnectionError as e:
        return (False, None, 'connection_error', f"Connection error: {e}")
    except Exception as e:
        return (False, None, 'api_error', f"Unexpected error: {e}")


# =============================================================================
# TEST CLASS
# =============================================================================

class TestTypeGoogleSheets(TestTypeBase):
    """Google Sheets connectivity and permission testing using service account authentication"""
    __test__ = False  # Prevent pytest from collecting this as a test class
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests Google Sheets API access via service account authentication",
            config_fields={
                "service_account_email": "Service account email (client_email from JSON)",
                "spreadsheet": "Google Spreadsheet (ID or URL)",
                "sheet_name": "Sheet/tab name to test (default: first sheet)",
                "timeout_seconds": f"Request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})"
            },
            required_fields=["service_account_email", "spreadsheet"],
            optional_fields=["sheet_name", "timeout_seconds"],
            confidential_fields=["GOOGLE_PRIVATE_KEY"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def get_configs(self) -> Dict[str, Any]:
        """Return configuration with defaults applied and credentials masked for safe logging."""
        config = self.config.copy()
        
        # Apply defaults
        if config.get("timeout_seconds") is None:
            config["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
        
        # Mask private key if present in environment
        if 'GOOGLE_PRIVATE_KEY' in os.environ:
            config['private_key'] = _hash_credential(os.environ['GOOGLE_PRIVATE_KEY'])
        
        return config
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        """
        Execute Google Sheets connection tests.
        
        Test flow:
        1. Validate configuration parameters
        2. DNS prerequisite tests (general network connectivity)
        3. OAuth token acquisition with service account
        4. Spreadsheet metadata access (verify spreadsheet exists and is accessible)
        5. Sheet selection (validate sheet name or use first sheet)
        6. Cell read test (read cell A1 to confirm full read access)
        
        Returns:
            TestResult with success=True only if all required tests pass.
            Empty cells are acceptable - we only need to verify API access works.
        """
        
        # =====================================================================
        # 1. VALIDATE CONFIGURATION
        # =====================================================================
        
        # Validate required parameters
        if not self.config.get("service_account_email"):
            raise ConfigurationError("service_account_email is required")
        
        if not self.config.get("spreadsheet"):
            raise ConfigurationError("spreadsheet is required")
        
        # Extract configuration
        service_account_email = self.config.get("service_account_email")
        spreadsheet = self.config.get("spreadsheet")
        sheet_name = self.config.get("sheet_name")
        timeout_seconds = int(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        
        # Validate email format
        if '@' not in service_account_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid service_account_email format: {service_account_email}",
                advice=["service_account_email must be a valid email address (e.g., name@project.iam.gserviceaccount.com)"]
            )
        
        # Extract spreadsheet ID (handles both direct IDs and URLs)
        success, extracted_id, error_msg = _extract_spreadsheet_id(spreadsheet)
        
        if not success:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=error_msg,
                advice=[
                    "The --spreadsheet parameter accepts either a Spreadsheet ID or a URL.",
                    "",
                    "Spreadsheet ID format (alphanumeric with hyphens/underscores):",
                    "  Example: 1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M",
                    "  How to find: Open your spreadsheet in browser, the ID is in the URL",
                    "  URL format: https://docs.google.com/spreadsheets/d/{ID}/edit",
                    "",
                    "Or provide the full URL directly:",
                    "  Example: https://docs.google.com/spreadsheets/d/1h9qlLlo.../edit",
                    "  How to get: Click 'Share' > 'Copy link' in Google Sheets",
                    "",
                    f"You provided: {spreadsheet}",
                    "This doesn't match either format (not a valid ID or Google Sheets URL)"
                ]
            )
        
        final_spreadsheet_id = extracted_id
        
        # Get private key from environment variable
        private_key = os.getenv('GOOGLE_PRIVATE_KEY')
        if not private_key:
            return self.create_env_var_error(
                env_var_name='GOOGLE_PRIVATE_KEY',
                description='Google service account private key'
            )
        
        # Build service account info from individual components
        service_account_info = _build_service_account_info(
            service_account_email, private_key
        )
        
        prerequisite_results = {}
        test_start_time = time.time()
        
        # =====================================================================
        # 2. DNS PREREQUISITE TESTS
        # =====================================================================
        
        # General DNS prerequisite is handled by prerequisite_tests() framework
        # No plugin-specific DNS checks needed
        
        # =====================================================================
        # 3. OAUTH TOKEN ACQUISITION
        # =====================================================================
        
        token_start = time.time()
        success, token, error_type, error_msg = _get_oauth_token(
            service_account_info, timeout_seconds
        )
        token_time_ms = (time.time() - token_start) * 1000
        
        if not success:
            advice_lines = [
                "Verify service account credentials are correct",
                "Ensure service account has proper permissions",
                f"Check that OAuth scope '{SHEETS_READONLY_SCOPE}' is accessible"
            ]
            
            if error_type == 'invalid_credentials':
                advice_lines = [
                    f"Google OAuth API error: {error_msg}",
                    "",
                    "Service account credentials are invalid",
                    "Common causes:",
                    "  - Private key is incorrect or corrupted",
                    "  - Service account email doesn't match the private key",
                    "  - Private key has been rotated/regenerated",
                    "",
                    "Troubleshooting steps:",
                    "  1. Download fresh service account credentials from Google Cloud Console",
                    "  2. Verify GOOGLE_PRIVATE_KEY environment variable is set correctly",
                    f"  3. Verify service account email: {service_account_email}",
                    "  4. Ensure private key includes BEGIN and END markers"
                ]
            elif error_type == 'missing_dependency':
                advice_lines = [
                    f"Error: {error_msg}",
                    "",
                    "Required Python packages not installed",
                    "Install with: pip install google-auth"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"OAuth token acquisition failed: {error_msg}",
                advice=advice_lines,
                extra_detail={'oauth_response_time_ms': round(token_time_ms, 2)}
            )
        
        prerequisite_results['oauth_token'] = {
            'ok': True,
            'response_time_ms': round(token_time_ms, 2)
        }
        
        # =====================================================================
        # 4. SPREADSHEET METADATA ACCESS
        # =====================================================================
        
        metadata_start = time.time()
        # Request spreadsheet metadata - only the fields we actually use
        # This is MUCH faster than requesting all metadata (which includes formatting, etc.)
        success, data, error_type, error_msg = _test_sheets_api_call(
            f"/{final_spreadsheet_id}?fields=spreadsheetId,properties(title),sheets(properties(title))",
            token,
            timeout_seconds
        )
        metadata_time_ms = (time.time() - metadata_start) * 1000
        
        if not success:
            # Generate specific advice based on error type
            if error_type == 'api_not_enabled':
                advice_lines = [
                    f"Google Sheets API error: {error_msg}",
                    "",
                    "Google Sheets API is not enabled for this project",
                    "In Google Cloud Console:",
                    "  1. Go to 'APIs & Services' > 'Library'",
                    "  2. Search for 'Google Sheets API'",
                    "  3. Click 'Enable'",
                    "Wait a few minutes after enabling before retrying"
                ]
            elif error_type == 'permission_denied':
                advice_lines = [
                    f"Google Sheets API error: {error_msg}",
                    "",
                    "Insufficient permissions to access spreadsheet",
                    f"Spreadsheet ID: {final_spreadsheet_id}",
                    "",
                    "The service account does not have access to this spreadsheet.",
                    "To fix:",
                    "  1. Open the spreadsheet in Google Sheets",
                    "  2. Click 'Share' button",
                    f"  3. Add email: {service_account_email}",
                    "  4. Grant 'Viewer' or 'Editor' access",
                    "  5. Click 'Send' or 'Done'",
                    "",
                    "Note: Service accounts are treated like regular users for sharing"
                ]
            elif error_type == 'resource_not_found':
                advice_lines = [
                    f"Google Sheets API error: {error_msg}",
                    "",
                    f"Spreadsheet not found: {final_spreadsheet_id}",
                    "This usually means:",
                    "  - Spreadsheet ID is incorrect",
                    "  - Spreadsheet doesn't exist or was deleted",
                    "  - Service account lacks permission to access it",
                    "",
                    "Troubleshooting:",
                    f"  1. Verify spreadsheet ID: {final_spreadsheet_id}",
                    "  2. Open spreadsheet in browser to confirm it exists",
                    f"  3. Share spreadsheet with service account: {service_account_email}",
                    "  4. Grant 'Viewer' or 'Editor' access"
                ]
            else:
                # Generic error fallback
                advice_lines = [
                    f"Google Sheets API error: {error_msg}",
                    "",
                    f"Cannot access spreadsheet: {final_spreadsheet_id}",
                    "Verify the spreadsheet exists and is accessible",
                    "Ensure Google Sheets API is enabled in Google Cloud Console",
                    f"Share spreadsheet with service account: {service_account_email}"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"Spreadsheet metadata access failed: {error_msg}",
                advice=advice_lines,
                extra_detail=prerequisite_results
            )
        
        # Extract spreadsheet metadata
        spreadsheet_title = data.get('properties', {}).get('title', '(Untitled)')
        sheets_list = data.get('sheets', [])
        sheet_names = [sheet.get('properties', {}).get('title', f'Sheet{i+1}') 
                      for i, sheet in enumerate(sheets_list)]
        
        prerequisite_results['spreadsheet_metadata'] = {
            'ok': True,
            'spreadsheet_id': final_spreadsheet_id,
            'spreadsheet_title': spreadsheet_title,
            'sheet_count': len(sheets_list),
            'sheet_names': sheet_names,
            'response_time_ms': round(metadata_time_ms, 2)
        }
        
        # =====================================================================
        # 5. SHEET SELECTION
        # =====================================================================
        
        # Check if spreadsheet has any sheets
        if not sheets_list:
            return TestResult(
                success=False,
                error_type="no_sheets",
                error_message="Spreadsheet has no sheets/tabs",
                advice=[
                    f"Spreadsheet '{spreadsheet_title}' is empty (no sheets/tabs)",
                    "This is unusual - spreadsheets normally have at least one sheet",
                    "Verify the spreadsheet is not corrupted"
                ],
                extra_detail=prerequisite_results
            )
        
        # Determine which sheet to use
        if sheet_name:
            # User specified a sheet name - find it
            if sheet_name not in sheet_names:
                advice_lines = [
                    f"Sheet '{sheet_name}' not found in spreadsheet.",
                    "",
                    f"Available sheets/tabs in '{spreadsheet_title}':"
                ]
                for name in sheet_names:
                    advice_lines.append(f"  - {name}")
                advice_lines.extend([
                    "",
                    "Try:",
                    f"  - Use --sheet-name \"{sheet_names[0]}\" to access first sheet",
                    "  - Omit --sheet-name to use the first sheet automatically",
                    "  - Check spelling (sheet names are case-sensitive)"
                ])
                
                return TestResult(
                    success=False,
                    error_type="invalid_sheet_name",
                    error_message=f"Sheet '{sheet_name}' not found",
                    advice=advice_lines,
                    extra_detail=prerequisite_results
                )
            
            selected_sheet_name = sheet_name
        else:
            # Use first sheet
            selected_sheet_name = sheet_names[0]
        
        # =====================================================================
        # 6. CELL READ TEST (A1)
        # =====================================================================
        
        cell_start = time.time()
        # Read cell A1 from selected sheet
        success, data, error_type, error_msg = _test_sheets_api_call(
            f"/{final_spreadsheet_id}/values/{selected_sheet_name}!A1",
            token,
            timeout_seconds
        )
        cell_time_ms = (time.time() - cell_start) * 1000
        
        if not success:
            advice_lines = [
                f"Google Sheets API error: {error_msg}",
                "",
                f"Failed to read cell A1 from sheet '{selected_sheet_name}'",
                "This usually indicates:",
                "  - Sheet name is invalid (though it passed validation)",
                "  - Temporary API error or timeout",
                "  - Permissions changed during test",
                "",
                "Troubleshooting:",
                "  - Try running the test again",
                "  - Increase --timeout-seconds if network is slow",
                f"  - Verify sheet '{selected_sheet_name}' still exists"
            ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"Cell read failed: {error_msg}",
                advice=advice_lines,
                extra_detail=prerequisite_results
            )
        
        # Extract cell value (may be empty)
        values = data.get('values', [])
        cell_value = values[0][0] if values and len(values) > 0 and len(values[0]) > 0 else ''
        cell_is_blank = not cell_value
        
        prerequisite_results['cell_read_test'] = {
            'ok': True,
            'sheet_name': selected_sheet_name,
            'cell_address': 'A1',
            'cell_value': cell_value,
            'cell_is_blank': cell_is_blank,
            'response_time_ms': round(cell_time_ms, 2)
        }
        
        # =====================================================================
        # SUCCESS RESULT
        # =====================================================================
        
        total_time_ms = (time.time() - test_start_time) * 1000
        
        return TestResult(
            success=True,
            extra_detail={
                **prerequisite_results,
                'summary': {
                    'total_test_time_ms': round(total_time_ms, 2),
                    'all_tests_passed': True,
                    'service_account_email': service_account_email,
                    'spreadsheet_id': final_spreadsheet_id,
                    'spreadsheet_title': spreadsheet_title
                }
            }
        )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeGoogleSheets,
    shortname="gsheets",
    is_public=True,
    description="Tests Google Sheets API access via service account authentication"
)

