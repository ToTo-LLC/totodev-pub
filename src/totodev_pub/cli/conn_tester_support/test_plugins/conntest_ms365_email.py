#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Microsoft 365 Email connection test plugin.

This module provides Microsoft Graph API connectivity testing for M365 email access,
including authentication, permission verification, and optional rate limiting tests.

Uses app-only authentication (OAuth2 client credentials flow) which requires:
- Application permissions: Mail.Read and Mail.ReadBasic
- Admin consent granted in Microsoft Entra Admin Center

Environment Variables:
    MS365_CLIENT_SECRET: Client secret from Azure app registration
"""

from __future__ import annotations

import os
import time
import asyncio
import hashlib
from typing import Any, Dict, List, Optional, Type
from datetime import datetime

import requests

from conn_tester_support.core import TestTypeBase
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_FOLDER_PATH = "Inbox"
SAMPLE_EMAIL_COUNT = 3
RATE_LIMIT_TEST_DURATION_SECONDS = 30

# Graph API endpoints
AZURE_AD_BASE = "https://login.microsoftonline.com"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _hash_credential(credential: str) -> str:
    """Hash credential for safe display in logs."""
    if not credential:
        return "<empty>"
    return f"<hashed:sha256:{hashlib.sha256(credential.encode()).hexdigest()[:12]}>"


def _get_oauth_token(tenant_id: str, client_id: str, client_secret: str, 
                     timeout: int) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Acquire OAuth2 token using client credentials flow.
    
    Returns:
        (success, token, error_type, error_message)
    """
    url = f"{AZURE_AD_BASE}/{tenant_id}/oauth2/v2.0/token"
    
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials'
    }
    
    try:
        response = requests.post(url, data=data, timeout=timeout)
        
        if response.status_code == 200:
            token_data = response.json()
            return (True, token_data.get('access_token'), None, None)
        
        # Parse error response
        try:
            error_data = response.json()
            error_code = error_data.get('error', 'unknown')
            error_desc = error_data.get('error_description', str(response.content))
            
            # Map Azure AD error codes to our error types
            if error_code == 'invalid_client':
                return (False, None, 'invalid_client', f"Invalid client_id or client_secret: {error_desc}")
            elif error_code == 'invalid_request':
                if 'tenant' in error_desc.lower():
                    return (False, None, 'invalid_tenant', f"Invalid tenant_id: {error_desc}")
                return (False, None, 'invalid_request', f"Invalid request: {error_desc}")
            elif error_code == 'unauthorized_client':
                return (False, None, 'invalid_client', f"Client not authorized: {error_desc}")
            else:
                return (False, None, 'auth_failed', f"Authentication failed ({error_code}): {error_desc}")
                
        except Exception:
            return (False, None, 'auth_failed', f"Authentication failed with status {response.status_code}")
            
    except requests.Timeout:
        return (False, None, 'timeout', "Request timed out while acquiring OAuth token")
    except requests.ConnectionError as e:
        return (False, None, 'connection_error', f"Connection error: {e}")
    except Exception as e:
        return (False, None, 'auth_failed', f"Unexpected error: {e}")


def _test_graph_api_call(endpoint: str, token: str, timeout: int) -> tuple[bool, Optional[dict], Optional[str], Optional[str]]:
    """
    Make a Graph API call.
    
    Returns:
        (success, response_data, error_type, error_message)
    """
    headers = {'Authorization': f'Bearer {token}'}
    url = f"{GRAPH_API_BASE}{endpoint}"
    
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        
        if response.status_code == 200:
            return (True, response.json(), None, None)
        
        # Parse error response
        try:
            error_data = response.json()
            error_code = error_data.get('error', {}).get('code', 'unknown')
            error_msg = error_data.get('error', {}).get('message', str(response.content))
            
            # Map Graph API error codes
            if response.status_code == 401:
                return (False, None, 'auth_failed', f"Unauthorized: {error_msg}")
            elif response.status_code == 403:
                if 'consent' in error_msg.lower():
                    return (False, None, 'consent_required', 
                           f"Admin consent required: {error_msg}")
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


async def _make_async_request(endpoint: str, token: str, timeout: int) -> tuple[bool, float]:
    """
    Make an async Graph API request for rate limit testing.
    
    Returns:
        (success, response_time_ms)
    """
    import aiohttp
    
    headers = {'Authorization': f'Bearer {token}'}
    url = f"{GRAPH_API_BASE}{endpoint}"
    
    start_time = time.time()
    
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url, headers=headers) as response:
                await response.read()
                elapsed_ms = (time.time() - start_time) * 1000
                return (response.status in [200, 429], elapsed_ms)
    except Exception:
        elapsed_ms = (time.time() - start_time) * 1000
        return (False, elapsed_ms)


# =============================================================================
# TEST CLASS
# =============================================================================

class TestTypeMs365Email(TestTypeBase):
    """Microsoft 365 email connectivity and permission testing"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests Microsoft 365 email API access via Graph API (app-only auth)",
            config_fields={
                "tenant_id": "Azure AD tenant ID (Directory ID)",
                "client_id": "Application (client) ID from Azure app registration",
                "user_email": "User's email address (UPN) for mailbox access",
                "folder_name": f"Mail folder to test (default: {DEFAULT_FOLDER_PATH})",
                "forbidden_email": "Email address that should NOT be accessible (tests permission boundaries)",
                "verify_write_permission": "If non-blank, verifies Mail.ReadWrite permission by updating an email's follow-up flag",
                "calls_per_ten_seconds": "Target API calls per 10 seconds for rate limit test (optional, float)",
                "timeout_seconds": f"Request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})"
            },
            required_fields=["tenant_id", "client_id", "user_email"],
            optional_fields=["folder_name", "forbidden_email", "verify_write_permission", "calls_per_ten_seconds", "timeout_seconds"],
            confidential_fields=["MS365_CLIENT_SECRET"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def get_configs(self) -> Dict[str, Any]:
        """Return current configuration with credential hashing and defaults applied"""
        config = self.config.copy()
        
        # Apply defaults
        if config.get("folder_name") is None:
            config["folder_name"] = DEFAULT_FOLDER_PATH
        if config.get("timeout_seconds") is None:
            config["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
        
        # Hash client_secret if present in environment
        if 'MS365_CLIENT_SECRET' in os.environ:
            secret = os.environ['MS365_CLIENT_SECRET']
            config['client_secret'] = _hash_credential(secret)
        
        return config
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        """Execute Microsoft 365 email connection tests"""
        
        # =====================================================================
        # 1. VALIDATE CONFIGURATION
        # =====================================================================
        
        if not self.config.get("tenant_id"):
            raise ConfigurationError("tenant_id is required")
        if not self.config.get("client_id"):
            raise ConfigurationError("client_id is required")
        if not self.config.get("user_email"):
            raise ConfigurationError("user_email is required")
        
        tenant_id = self.config.get("tenant_id")
        client_id = self.config.get("client_id")
        user_email = self.config.get("user_email")
        folder_name = self.config.get("folder_name") or DEFAULT_FOLDER_PATH
        forbidden_email = self.config.get("forbidden_email")
        verify_write_permission = self.config.get("verify_write_permission")
        # Check if verify_write_permission is non-blank (not None, not empty, not just whitespace)
        should_verify_write = verify_write_permission and str(verify_write_permission).strip()
        timeout_seconds = int(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        calls_per_ten_seconds = self.config.get("calls_per_ten_seconds")
        
        # Validate email format
        if '@' not in user_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid user_email format: {user_email}",
                advice=["user_email must be a valid email address"]
            )
        
        # Get client secret from environment
        client_secret = os.getenv('MS365_CLIENT_SECRET')
        if not client_secret:
            return self.create_env_var_error(
                env_var_name='MS365_CLIENT_SECRET',
                description='Microsoft 365 client secret for OAuth2 authentication'
            )
        
        # Validate rate limit parameter if provided
        if calls_per_ten_seconds is not None:
            try:
                calls_per_ten_seconds = float(calls_per_ten_seconds)
                if calls_per_ten_seconds <= 0:
                    return TestResult(
                        success=False,
                        error_type="config_invalid",
                        error_message=f"calls_per_ten_seconds must be positive: {calls_per_ten_seconds}",
                        advice=["Provide a positive number for calls_per_ten_seconds"]
                    )
            except (ValueError, TypeError):
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Invalid calls_per_ten_seconds: {calls_per_ten_seconds}",
                    advice=["calls_per_ten_seconds must be a number (e.g., 10.0, 5.5)"]
                )
        
        prerequisite_results = {}
        test_start_time = time.time()
        
        # =====================================================================
        # 2. DNS PREREQUISITE TESTS
        # =====================================================================
        
        # Test DNS for Azure AD
        dns_test_ad = TestTypeDnsResolve({"hostname": "login.microsoftonline.com"})
        dns_result_ad = dns_test_ad.run_test()
        # Don't add verbose DNS details to standard log
        
        if not dns_result_ad.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message="DNS resolution failed for login.microsoftonline.com",
                advice=dns_result_ad.advice,
                extra_detail={}
            )
        
        # Test DNS for Graph API
        dns_test_graph = TestTypeDnsResolve({"hostname": "graph.microsoft.com"})
        dns_result_graph = dns_test_graph.run_test()
        # Don't add verbose DNS details to standard log
        
        if not dns_result_graph.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message="DNS resolution failed for graph.microsoft.com",
                advice=dns_result_graph.advice,
                extra_detail={}
            )
        
        # =====================================================================
        # 3. OAUTH TOKEN ACQUISITION
        # =====================================================================
        
        token_start = time.time()
        success, token, error_type, error_msg = _get_oauth_token(
            tenant_id, client_id, client_secret, timeout_seconds
        )
        token_time_ms = (time.time() - token_start) * 1000
        
        if not success:
            advice_lines = [
                "Verify tenant_id, client_id, and client_secret are correct",
                "Check that the Azure app registration is not disabled",
                "Ensure client secret has not expired"
            ]
            
            if error_type == 'invalid_tenant':
                advice_lines = [
                    "Check that tenant_id (Directory ID) is correct",
                    "Find tenant_id in Azure Portal > Azure Active Directory > Overview"
                ]
            elif error_type == 'invalid_client':
                advice_lines = [
                    "Verify client_id (Application ID) is correct",
                    "Check that client_secret (Client Secret) is correct and not expired",
                    "Client secrets expire - generate a new one if needed",
                    "Find in Azure Portal > App registrations > Your app > Certificates & secrets"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"OAuth token acquisition failed: {error_msg}",
                advice=advice_lines,
                extra_detail={}
            )
        
        # Don't add oauth_token timing to standard log
        
        # =====================================================================
        # 4. MAILBOX ACCESS TEST
        # =====================================================================
        
        mailbox_start = time.time()
        success, data, error_type, error_msg = _test_graph_api_call(
            f"/users/{user_email}/messages?$top=1",
            token,
            timeout_seconds
        )
        mailbox_time_ms = (time.time() - mailbox_start) * 1000
        
        if not success:
            advice_lines = [
                "Verify that the user_email exists in your tenant",
                "Ensure Mail.Read or Mail.ReadBasic application permissions are granted",
                "Check that admin consent has been granted for these permissions",
                "Grant permissions in Azure Portal > App registrations > API permissions"
            ]
            
            if error_type == 'permission_denied' or error_type == 'consent_required':
                advice_lines = [
                    "Application permissions Mail.Read or Mail.ReadBasic are missing or not consented",
                    "In Azure Portal > App registrations > Your app > API permissions:",
                    "  1. Click 'Add a permission' > Microsoft Graph > Application permissions",
                    "  2. Add Mail.Read and Mail.ReadBasic",
                    "  3. Click 'Grant admin consent' button",
                    "Wait a few minutes after granting consent before retrying"
                ]
            elif error_type == 'resource_not_found':
                advice_lines = [
                    "User mailbox not found - verify user_email is correct",
                    "Ensure the user exists in your Azure AD tenant",
                    "Check for typos in the email address"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"Mailbox access failed: {error_msg}",
                advice=advice_lines,
                extra_detail={}
            )
        
        # Don't add mailbox_access timing to standard log
        
        # =====================================================================
        # 5. FOLDER ACCESS TEST
        # =====================================================================
        
        folder_name_normalized = folder_name.lower().replace(' ', '')
        folder_start = time.time()
        success, data, error_type, error_msg = _test_graph_api_call(
            f"/users/{user_email}/mailFolders/{folder_name_normalized}/messages?$top={SAMPLE_EMAIL_COUNT}",
            token,
            timeout_seconds
        )
        folder_time_ms = (time.time() - folder_start) * 1000
        
        if not success:
            advice_lines = [
                f"Cannot access folder '{folder_name}'",
                "Verify the folder exists in the user's mailbox",
                "Try 'Inbox', 'SentItems', 'Drafts', or 'DeletedItems'",
                "For custom folders, use the exact folder name"
            ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"Folder access failed: {error_msg}",
                advice=advice_lines,
                extra_detail={}
            )
        
        # Extract email list (but don't include sample_emails in standard log)
        messages = data.get('value', [])
        
        prerequisite_results['folder_access'] = {
            'ok': True,
            'folder_name': folder_name,
            'message_count': len(messages)
        }
        
        # =====================================================================
        # 6. EMAIL BODY RETRIEVAL TEST
        # =====================================================================
        
        if messages and len(messages) > 0:
            test_msg_id = messages[0]['id']
            body_start = time.time()
            
            # Test $value endpoint (MIME format)
            headers = {'Authorization': f'Bearer {token}'}
            url = f"{GRAPH_API_BASE}/users/{user_email}/messages/{test_msg_id}/$value"
            
            try:
                response = requests.get(url, headers=headers, timeout=timeout_seconds)
                body_time_ms = (time.time() - body_start) * 1000
                
                if response.status_code == 200:
                    mime_content = response.content
                    prerequisite_results['email_body_retrieval'] = {
                        'ok': True,
                        'mime_size_bytes': len(mime_content),
                        'response_time_ms': round(body_time_ms, 2)
                    }
                else:
                    prerequisite_results['email_body_retrieval'] = {
                        'ok': False,
                        'error': f"Failed with status {response.status_code}",
                        'response_time_ms': round(body_time_ms, 2)
                    }
            except Exception as e:
                body_time_ms = (time.time() - body_start) * 1000
                prerequisite_results['email_body_retrieval'] = {
                    'ok': False,
                    'error': str(e),
                    'response_time_ms': round(body_time_ms, 2)
                }
        else:
            prerequisite_results['email_body_retrieval'] = {
                'ok': False,
                'error': 'No messages in folder to test retrieval'
            }
        
        # =====================================================================
        # 7. WRITE PERMISSION VERIFICATION (if verify_write_permission enabled)
        # =====================================================================
        
        if should_verify_write:
            if not messages or len(messages) == 0:
                prerequisite_results['write_permission_verification'] = {
                    'ok': False,
                    'error': 'No messages available in folder to verify write permission',
                    'advice': 'Folder must contain at least one message to verify Mail.ReadWrite permission'
                }
            else:
                flag_test_msg_id = messages[0]['id']
                flag_test_start = time.time()
                
                try:
                    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
                    msg_url = f"{GRAPH_API_BASE}/users/{user_email}/messages/{flag_test_msg_id}"
                    
                    # Step 1: Get current flag status
                    get_response = requests.get(msg_url, headers=headers, params={'$select': 'flag'}, timeout=timeout_seconds)
                    
                    if get_response.status_code != 200:
                        error_data = get_response.json() if get_response.status_code != 404 else {}
                        error_msg = error_data.get('error', {}).get('message', f"HTTP {get_response.status_code}")
                        
                        if get_response.status_code == 403:
                            prerequisite_results['write_permission_verification'] = {
                                'ok': False,
                                'error': 'Permission denied',
                                'error_message': error_msg,
                                'advice': [
                                    'Mail.ReadWrite application permission is required to update message flags',
                                    'Current permissions only allow read access (Mail.Read or Mail.ReadBasic)',
                                    'In Azure Portal > App registrations > Your app > API permissions:',
                                    '  1. Add Mail.ReadWrite application permission',
                                    '  2. Remove Mail.Read if you want write access',
                                    '  3. Grant admin consent',
                                    'Note: Mail.ReadWrite includes all Mail.Read capabilities'
                                ],
                                'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                            }
                        else:
                            prerequisite_results['write_permission_verification'] = {
                                'ok': False,
                                'error': f"Failed to retrieve current flag status: {error_msg}",
                                'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                            }
                    
                    if get_response.status_code == 200:
                        original_flag = get_response.json().get('flag', {})
                        original_flag_status = original_flag.get('flagStatus', 'notFlagged')
                        get_time_ms = (time.time() - flag_test_start) * 1000
                        
                        # Step 2: Determine new flag status (toggle)
                        if original_flag_status == 'notFlagged':
                            new_flag_status = 'flagged'
                        else:
                            new_flag_status = 'notFlagged'
                        
                        # Step 3: Update flag
                        update_start = time.time()
                        update_payload = {
                            'flag': {
                                'flagStatus': new_flag_status
                            }
                        }
                        patch_response = requests.patch(msg_url, headers=headers, json=update_payload, timeout=timeout_seconds)
                        update_time_ms = (time.time() - update_start) * 1000
                        
                        if patch_response.status_code not in [200, 204]:
                            error_data = patch_response.json() if patch_response.status_code not in [404, 500] else {}
                            error_msg = error_data.get('error', {}).get('message', f"HTTP {patch_response.status_code}")
                            
                            if patch_response.status_code == 403 or 'access' in error_msg.lower() or 'denied' in error_msg.lower() or 'permission' in error_msg.lower():
                                prerequisite_results['write_permission_verification'] = {
                                    'ok': False,
                                    'error': 'Permission denied - Mail.ReadWrite permission required',
                                    'error_message': error_msg,
                                    'original_flag_status': original_flag_status,
                                    'attempted_new_status': new_flag_status,
                                    'advice': [
                                        'Mail.ReadWrite application permission is required to update message flags',
                                        'Current permissions only allow read access (Mail.Read or Mail.ReadBasic)',
                                        'In Azure Portal > App registrations > Your app > API permissions:',
                                        '  1. Add Mail.ReadWrite application permission',
                                        '  2. Remove Mail.Read if you only want write access (Mail.ReadWrite includes Mail.Read)',
                                        '  3. Grant admin consent',
                                        'Note: Mail.ReadWrite includes all Mail.Read capabilities plus write/update/delete'
                                    ],
                                    'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                                }
                            else:
                                prerequisite_results['write_permission_verification'] = {
                                    'ok': False,
                                    'error': f"Failed to update flag: {error_msg}",
                                    'original_flag_status': original_flag_status,
                                    'attempted_new_status': new_flag_status,
                                    'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                                }
                        elif patch_response.status_code in [200, 204]:
                            # Step 4: Verify update took effect
                            verify_start = time.time()
                            verify_response = requests.get(msg_url, headers=headers, params={'$select': 'flag'}, timeout=timeout_seconds)
                            verify_time_ms = (time.time() - verify_start) * 1000
                            
                            if verify_response.status_code == 200:
                                updated_flag = verify_response.json().get('flag', {})
                                updated_flag_status = updated_flag.get('flagStatus', 'notFlagged')
                                
                                if updated_flag_status != new_flag_status:
                                    prerequisite_results['write_permission_verification'] = {
                                        'ok': False,
                                        'error': f"Flag update did not take effect (expected {new_flag_status}, got {updated_flag_status})",
                                        'original_flag_status': original_flag_status,
                                        'expected_status': new_flag_status,
                                        'actual_status': updated_flag_status,
                                        'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                                    }
                                else:
                                    # Step 5: Restore original flag
                                    restore_start = time.time()
                                    restore_payload = {
                                        'flag': {
                                            'flagStatus': original_flag_status
                                        }
                                    }
                                    restore_response = requests.patch(msg_url, headers=headers, json=restore_payload, timeout=timeout_seconds)
                                    restore_time_ms = (time.time() - restore_start) * 1000
                                    
                                    if restore_response.status_code not in [200, 204]:
                                        # Restoration failed - this is concerning but not critical
                                        error_data = restore_response.json() if restore_response.status_code != 404 else {}
                                        error_msg = error_data.get('error', {}).get('message', f"HTTP {restore_response.status_code}")
                                        
                                        prerequisite_results['write_permission_verification'] = {
                                            'ok': False,
                                            'error': f"WARNING: Flag update succeeded but restoration failed: {error_msg}",
                                            'warning': 'Test email flag was changed but could not be restored to original state',
                                            'original_flag_status': original_flag_status,
                                            'current_flag_status': new_flag_status,
                                            'update_time_ms': round(update_time_ms, 2),
                                            'restore_time_ms': round(restore_time_ms, 2),
                                            'total_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                                        }
                                    else:
                                        # Success - all operations completed
                                        total_time_ms = (time.time() - flag_test_start) * 1000
                                        prerequisite_results['write_permission_verification'] = {
                                            'ok': True,
                                            'original_flag_status': original_flag_status,
                                            'test_flag_status': new_flag_status,
                                            'restored_flag_status': original_flag_status,
                                            'get_time_ms': round(get_time_ms, 2),
                                            'update_time_ms': round(update_time_ms, 2),
                                            'verify_time_ms': round(verify_time_ms, 2),
                                            'restore_time_ms': round(restore_time_ms, 2),
                                            'total_time_ms': round(total_time_ms, 2)
                                        }
                
                except requests.Timeout:
                    prerequisite_results['write_permission_verification'] = {
                        'ok': False,
                        'error': 'Request timeout during write permission verification',
                        'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                    }
                except Exception as e:
                    prerequisite_results['write_permission_verification'] = {
                        'ok': False,
                        'error': f"Unexpected error during write permission verification: {e}",
                        'response_time_ms': round((time.time() - flag_test_start) * 1000, 2)
                    }
        
        # =====================================================================
        # 8. PERMISSION BOUNDARY TEST (if forbidden_email provided)
        # =====================================================================
        
        if forbidden_email:
            forbidden_start = time.time()
            success, data, error_type, error_msg = _test_graph_api_call(
                f"/users/{forbidden_email}/messages?$top=1",
                token,
                timeout_seconds
            )
            forbidden_time_ms = (time.time() - forbidden_start) * 1000
            
            if success:
                # This is BAD - we should NOT have access
                # This must cause the OVERALL TEST TO FAIL
                prerequisite_results['permission_boundary'] = {
                    'ok': False,
                    'warning': 'EXCESSIVE_PERMISSIONS',
                    'forbidden_email': forbidden_email,
                    'forbidden_access_granted': True,
                    'message': 'Application has access to mailboxes it should not access',
                    'remediation': {
                        'problem': 'No restrictions prevent access to forbidden email. It\'s likely the login can access any organization mailbox!',
                        'solution': 'Use Exchange Online Application Access Policy to restrict access',
                        'steps': [
                            '1. Install Exchange Online PowerShell: Install-Module ExchangeOnlineManagement',
                            '2. Connect: Connect-ExchangeOnline',
                            '3. Create mail-enabled security group with authorized users',
                            f"4. Apply policy: New-ApplicationAccessPolicy -AppId '{client_id}' -PolicyScopeGroupId <group_id> -AccessRight RestrictAccess -Description 'Restrict to authorized mailboxes'",
                            f"5. Test policy: Test-ApplicationAccessPolicy -Identity '{user_email}' -AppId '{client_id}'",
                            '6. Wait 15-60 minutes for policy to propagate'
                        ],
                        'documentation': 'https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access',
                        'note': 'Application Access Policies allow you to grant Mail.Read at tenant level but restrict which mailboxes the app can actually access'
                    }
                }
                
                # RETURN FAILURE immediately - don't continue to success
                return TestResult(
                    success=False,
                    error_type="excessive_permissions",
                    error_message=f"SECURITY VIOLATION: Application has access to forbidden mailbox '{forbidden_email}'",
                    advice=[
                        "Application has excessive permissions - it can access mailboxes it should not",
                        "Use Exchange Online Application Access Policy to restrict mailbox access",
                        "See remediation steps in extra_detail for configuration instructions"
                    ],
                    extra_detail=prerequisite_results
                )
            else:
                # This is GOOD - we correctly got access denied
                if error_type in ['permission_denied', 'resource_not_found']:
                    prerequisite_results['permission_boundary'] = {
                        'ok': True,
                        'forbidden_email': forbidden_email,
                        'access_granted': False,
                        'message': 'Correctly denied access to forbidden mailbox',
                        'response_time_ms': round(forbidden_time_ms, 2)
                    }
                else:
                    prerequisite_results['permission_boundary'] = {
                        'ok': False,
                        'forbidden_email': forbidden_email,
                        'error': f"Unexpected error type: {error_type}",
                        'response_time_ms': round(forbidden_time_ms, 2)
                    }
        
        # =====================================================================
        # 9. RATE LIMIT TEST (if calls_per_ten_seconds provided)
        # =====================================================================
        
        if calls_per_ten_seconds is not None:
            try:
                # Calculate total calls to make in 30 seconds
                total_calls = int(calls_per_ten_seconds * 3)  # 30 seconds = 3 * 10 seconds
                delay_between_calls = 10.0 / calls_per_ten_seconds  # seconds between each call
                
                rate_limit_start = time.time()
                call_results = []
                rate_limited_count = 0
                success_count = 0
                error_count = 0
                
                # Use asyncio for concurrent requests
                async def run_rate_limit_test():
                    nonlocal rate_limited_count, success_count, error_count
                    
                    endpoint = f"/users/{user_email}/messages?$top=1"
                    
                    for i in range(total_calls):
                        call_start = time.time()
                        success, response_time = await _make_async_request(
                            endpoint, token, timeout_seconds
                        )
                        
                        if success:
                            success_count += 1
                        else:
                            error_count += 1
                        
                        call_results.append({
                            'call_number': i + 1,
                            'success': success,
                            'response_time_ms': round(response_time, 2)
                        })
                        
                        # Check if we should stop (30 seconds elapsed)
                        elapsed = time.time() - rate_limit_start
                        if elapsed >= RATE_LIMIT_TEST_DURATION_SECONDS:
                            break
                        
                        # Delay before next call
                        if i < total_calls - 1:
                            await asyncio.sleep(delay_between_calls)
                
                # Check if aiohttp is available
                try:
                    import aiohttp
                    asyncio.run(run_rate_limit_test())
                    
                    rate_limit_duration = time.time() - rate_limit_start
                    actual_rate = len(call_results) / (rate_limit_duration / 10)  # calls per 10 seconds
                    
                    prerequisite_results['rate_limit_test'] = {
                        'ok': True,
                        'target_calls_per_10s': calls_per_ten_seconds,
                        'actual_calls_per_10s': round(actual_rate, 2),
                        'total_calls': len(call_results),
                        'success_count': success_count,
                        'error_count': error_count,
                        'rate_limited_count': rate_limited_count,
                        'test_duration_seconds': round(rate_limit_duration, 2),
                        'avg_response_time_ms': round(
                            sum(r['response_time_ms'] for r in call_results) / len(call_results), 2
                        ) if call_results else 0
                    }
                    
                except ImportError:
                    prerequisite_results['rate_limit_test'] = {
                        'ok': False,
                        'error': 'aiohttp library not installed (pip install aiohttp)'
                    }
                    
            except Exception as e:
                prerequisite_results['rate_limit_test'] = {
                    'ok': False,
                    'error': f"Rate limit test failed: {e}"
                }
        
        # =====================================================================
        # CHECK FOR VERIFICATION FAILURES
        # =====================================================================
        
        # If write permission verification was attempted and failed, return failure
        if 'write_permission_verification' in prerequisite_results:
            verification_result = prerequisite_results['write_permission_verification']
            if not verification_result.get('ok', True):
                total_time_ms = (time.time() - test_start_time) * 1000
                return TestResult(
                    success=False,
                    error_type='write_permission_denied',
                    error_message=verification_result.get('error', 'Write permission verification failed'),
                    advice=verification_result.get('advice', [
                        'Mail.ReadWrite permission is required for this test',
                        'Current permissions only allow read access',
                        'Update application permissions in Azure Portal'
                    ]),
                    extra_detail={
                        **prerequisite_results,
                        'summary': {
                            'total_test_time_ms': round(total_time_ms, 2),
                            'write_permission_verification_failed': True
                        }
                    }
                )
        
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
                    'all_tests_passed': True
                }
            }
        )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeMs365Email,
    shortname="ms365_email",
    is_public=True,
    description="Tests Microsoft 365 email API access via Graph API"
)

