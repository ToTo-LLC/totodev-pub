#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Gmail connection test plugin.

This module provides Gmail API connectivity testing using service account authentication
with domain-wide delegation, including authentication verification, mailbox access,
and email retrieval capabilities.

## Authentication Setup

This test uses service account authentication (OAuth2 with domain-wide delegation):
1. Create a service account in Google Cloud Console
2. Download service account JSON credentials file
3. Enable domain-wide delegation for the service account
4. Grant the following OAuth scope in Google Workspace Admin:
   - https://www.googleapis.com/auth/gmail.readonly
5. Enable Gmail API in Google Cloud Console

## Credential Setup

The test requires service account credentials provided as individual parameters:

**Required Parameters:**
- `--service-account-email`: Service account email (client_email from JSON)
- `--user-email`: User's Gmail address to access

**Environment Variable:**
- `GMAIL_PRIVATE_KEY`: Service account private key (multi-line PEM format)

**Example:**
```bash
export GMAIL_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkq...
-----END PRIVATE KEY-----"

python -m totodev_pub.cli.conn_tester gmail \
  --service-account-email my-sa@my-project.iam.gserviceaccount.com \
  --user-email user@mydomain.com
```

## Common Issues

**Authentication Errors:**
- "invalid_grant" or "unauthorized_client": Domain-wide delegation not enabled
- "access_denied": OAuth scope not granted in Google Workspace Admin
- "User not found": Email address doesn't exist or wrong domain

**API Errors:**
- "API not enabled": Enable Gmail API in Google Cloud Console
- "Insufficient Permission": Wrong OAuth scopes granted
- "User Rate Limit Exceeded": Too many API calls, implement backoff

Environment Variables:
    GMAIL_PRIVATE_KEY: Service account private key (PEM format)
"""

from __future__ import annotations

import os
import json
import time
import asyncio
import hashlib
import sys
from typing import Any, Dict, List, Optional, Type
from datetime import datetime
from pathlib import Path

try:
    from totodev_pub.cli.conn_tester_support.core import TestTypeBase, _hash_credential
    from totodev_pub.cli.conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
    from totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve
except ModuleNotFoundError:  # pragma: no cover - fallback for legacy packaging
    from conn_tester_support.core import TestTypeBase, _hash_credential
    from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
    from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve



# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_FOLDER_LABEL = "INBOX"
DEFAULT_MAX_EMAILS_TO_FETCH = 1
RATE_LIMIT_TEST_DURATION_SECONDS = 30

# Gmail API endpoints
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

# OAuth scopes
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _hash_service_account(service_account_info: Dict[str, Any]) -> str:
    # Create safe logging identifier: client_email + hashed private key
    client_email = service_account_info.get('client_email') or 'unknown'
    private_key = service_account_info.get('private_key', '') or ''
    key_source = private_key.encode() if private_key else b""
    key_hash = hashlib.sha256(key_source).hexdigest()[:12]
    suffix = "" if private_key else " [private_key: unknown]"
    return f"{client_email} (key_hash: {key_hash}){suffix}"


def _resolve_callable(name: str):
    """Return a callable, honoring patches applied to the fully-qualified module path."""
    patched_module = sys.modules.get('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail')
    if patched_module is not None and hasattr(patched_module, name):
        return getattr(patched_module, name)
    return globals()[name]


def _load_service_account_info(
    file_path: Optional[str],
    json_blob: Optional[str],
) -> tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Legacy helper retained for backward compatibility with existing conn-tester
    tests. Attempts to load and validate service account credentials from either
    a JSON file path or a raw JSON string. Returns (success, data, error_message).
    """
    if not file_path and not json_blob:
        return (
            False,
            None,
            "No service account credentials provided. Supply a JSON file path or JSON string.",
        )

    account_data: Optional[Dict[str, Any]] = None
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                account_data = json.load(fh)
        except FileNotFoundError:
            return False, None, f"Service account file not found: {file_path}"
        except json.JSONDecodeError as exc:
            return False, None, f"Invalid JSON in service account file: {exc}"
        except OSError as exc:
            return False, None, f"Unable to read service account file: {exc}"
    else:
        try:
            account_data = json.loads(json_blob or "")
        except json.JSONDecodeError as exc:
            return False, None, f"Invalid JSON for service account credentials: {exc}"

    if not isinstance(account_data, dict):
        return False, None, "Service account data must be a JSON object."

    required_fields = {"type", "private_key", "client_email"}
    missing = [field for field in required_fields if field not in account_data or not account_data[field]]
    if missing:
        return (
            False,
            None,
            f"Service account JSON missing required fields: {', '.join(missing)}",
        )

    # Ensure token_uri is present for downstream consumers; default to Google OAuth endpoint.
    account_data.setdefault("token_uri", "https://oauth2.googleapis.com/token")

    return True, account_data, None


def _build_service_account_info(service_account_email: str, private_key: str) -> Dict[str, Any]:
    """Build service account info dict from individual components."""
    # Private key may have literal \n sequences from shell - convert to actual newlines
    # This handles cases where the key is exported like: export KEY="line1\nline2\nline3"
    normalized_key = private_key.replace('\\n', '\n')
    
    return {
        "type": "service_account",
        "private_key": normalized_key,
        "client_email": service_account_email,
        "token_uri": "https://oauth2.googleapis.com/token"
    }


def _get_oauth_token(service_account_info: Dict[str, Any], user_email: str, 
                     timeout: int) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Acquire OAuth2 token using service account with domain-wide delegation.
    Impersonates user_email to access their mailbox.
    
    Returns:
        (success, token, error_type, error_message)
    """
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
    except ImportError:
        return (False, None, 'missing_dependency', 
                "google-auth library not installed. Install: pip install google-auth google-api-python-client")
    
    try:
        # Create credentials with domain-wide delegation
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=[GMAIL_READONLY_SCOPE],
            subject=user_email
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
            return (False, None, 'invalid_delegation', 
                   f"Domain-wide delegation error: {e}")
        elif 'invalid_scope' in error_str:
            return (False, None, 'invalid_scope', 
                   f"OAuth scope error: {e}")
        elif 'user not found' in error_str or 'invalid user' in error_str:
            return (False, None, 'user_not_found', 
                   f"User email not found: {e}")
        else:
            return (False, None, 'auth_failed', f"Authentication failed: {e}")


async def _make_async_request(endpoint: str, token: str, timeout: int) -> tuple[bool, float, bool]:
    """Async Gmail API request for rate limit testing. Returns (success, response_time_ms, was_rate_limited)."""
    import aiohttp
    
    headers = {'Authorization': f'Bearer {token}'}
    url = f"{GMAIL_API_BASE}{endpoint}"
    
    start_time = time.time()
    
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url, headers=headers) as response:
                await response.read()
                elapsed_ms = (time.time() - start_time) * 1000
                was_rate_limited = response.status == 429
                return (response.status in [200, 429], elapsed_ms, was_rate_limited)
    except Exception:
        elapsed_ms = (time.time() - start_time) * 1000
        return (False, elapsed_ms, False)


def _test_gmail_api_call(endpoint: str, token: str, timeout: int) -> tuple[bool, Optional[dict], Optional[str], Optional[str]]:
    """
    Make a Gmail API call and categorize errors by HTTP status.
    
    Returns:
        (success, response_data, error_type, error_message)
    """
    import requests
    
    headers = {'Authorization': f'Bearer {token}'}
    url = f"{GMAIL_API_BASE}{endpoint}"
    
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        
        if response.status_code == 200:
            return (True, response.json(), None, None)
        
        # Parse error response and categorize by HTTP status
        try:
            error_data = response.json()
            error_msg = error_data.get('error', {}).get('message', str(response.content))
            
            # Map Gmail API error codes by HTTP status
            if response.status_code == 401:
                return (False, None, 'auth_failed', f"Unauthorized: {error_msg}")
            elif response.status_code == 403:
                if 'not enabled' in error_msg.lower() or 'api has not been used' in error_msg.lower():
                    return (False, None, 'api_not_enabled', 
                           f"Gmail API not enabled: {error_msg}")
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

class TestTypeGmail(TestTypeBase):
    """Gmail connectivity and permission testing using service account authentication"""
    __test__ = False  # Prevent pytest from collecting this as a test class
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests Gmail API access via service account authentication with domain-wide delegation",
            config_fields={
                "user_email": "User's Gmail address (mailbox to access)",
                "service_account_email": "Service account email (client_email from JSON). Optional when providing service_account_file or service_account_json.",
                "service_account_file": "Path to service account JSON credentials file (alternative to environment variables)",
                "service_account_json": "Inline JSON string containing service account credentials (alternative to file)",
                "folder_label": f"Gmail label/folder to test (default: {DEFAULT_FOLDER_LABEL})",
                "max_emails": f"Maximum number of emails to fetch for testing (default: {DEFAULT_MAX_EMAILS_TO_FETCH})",
                "forbidden_email": "Email address that should NOT be accessible (tests permission boundaries)",
                "timeout_seconds": f"Request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
                "test_metadata_only": "If non-blank, only fetch email metadata (not full content)",
                "calls_per_ten_seconds": "Target API calls per 10 seconds for rate limit test (optional, float)"
            },
            required_fields=["user_email"],
            optional_fields=[
                "service_account_email",
                "service_account_file",
                "service_account_json",
                "folder_label",
                "max_emails",
                "forbidden_email",
                "timeout_seconds",
                "test_metadata_only",
                "calls_per_ten_seconds",
            ],
            confidential_fields=["GMAIL_PRIVATE_KEY", "GMAIL_SERVICE_ACCOUNT_JSON"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def get_configs(self) -> Dict[str, Any]:
        """Return configuration with defaults applied and credentials masked for safe logging."""
        config = self.config.copy()
        
        # Apply defaults
        if config.get("folder_label") is None:
            config["folder_label"] = DEFAULT_FOLDER_LABEL
        if config.get("max_emails") is None:
            config["max_emails"] = DEFAULT_MAX_EMAILS_TO_FETCH
        if config.get("timeout_seconds") is None:
            config["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
        
        if config.get("service_account_file"):
            path_value = config["service_account_file"]
            config["service_account_file"] = f"<file:{path_value}>"
        if config.get("service_account_json"):
            config["service_account_json"] = "<json:provided>"

        # Mask private key if present in environment
        if 'GMAIL_PRIVATE_KEY' in os.environ:
            config['private_key'] = _hash_credential(os.environ['GMAIL_PRIVATE_KEY'])
        if 'GMAIL_SERVICE_ACCOUNT_JSON' in os.environ:
            config['service_account_json_env'] = "<env_json:provided>"
        
        return config
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        """
        Execute Gmail connection tests.
        
        Test flow:
        1. Validate configuration parameters
        2. DNS prerequisite tests (gmail.googleapis.com, oauth2.googleapis.com)
        3. OAuth token acquisition with domain-wide delegation
        4. Mailbox access test (list messages in specified folder)
        5. Email retrieval test (metadata or full MIME content)
        6. Permission boundary test (optional: verify forbidden mailbox is inaccessible)
        7. Rate limit test (optional: test API throttling behavior)
        
        Returns:
            TestResult with success=True only if all required tests pass.
            Optional tests (permission boundary, rate limit) run only if enabled.
            Empty folders or retrieval failures cause overall test failure.
        """
        
        # =====================================================================
        # 1. VALIDATE CONFIGURATION
        # =====================================================================
        
        # Validate required parameters
        if not self.config.get("user_email"):
            raise ConfigurationError("user_email is required")
        
        # Extract configuration
        user_email = self.config.get("user_email")
        service_account_email = self.config.get("service_account_email")
        service_account_file = self.config.get("service_account_file")
        service_account_json = self.config.get("service_account_json")
        folder_label = self.config.get("folder_label") or DEFAULT_FOLDER_LABEL
        max_emails = int(self.config.get("max_emails") or DEFAULT_MAX_EMAILS_TO_FETCH)
        forbidden_email = self.config.get("forbidden_email")
        timeout_seconds = int(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        test_metadata_only = self.config.get("test_metadata_only")
        metadata_only_mode = test_metadata_only and str(test_metadata_only).strip()
        calls_per_ten_seconds = self.config.get("calls_per_ten_seconds")

        # Validate user email format early to surface configuration issues even if credentials are missing
        if '@' not in user_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid user_email format: {user_email}",
                advice=["user_email must be a valid email address"],
            )

        # Load service account credentials from file / inline JSON / environment
        service_account_info: Optional[Dict[str, Any]] = None

        if service_account_file or service_account_json:
            success, info, error = _load_service_account_info(service_account_file, service_account_json)
            if not success:
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=error or "Invalid service account credentials",
                    advice=[
                        "Verify the service account JSON contains client_email and private_key",
                        "Ensure the file path is correct and readable",
                    ],
                )
            service_account_info = info
            service_account_email = info.get("client_email", service_account_email)
        else:
            env_json = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON")
            if env_json:
                success, info, error = _load_service_account_info(None, env_json)
                if not success:
                    return TestResult(
                        success=False,
                        error_type="config_invalid",
                        error_message=f"GMAIL_SERVICE_ACCOUNT_JSON invalid: {error}",
                        advice=["Verify the environment variable contains valid JSON credentials."],
                    )
                service_account_info = info
                service_account_email = info.get("client_email", service_account_email)

        if service_account_info is None:
            private_key = os.getenv('GMAIL_PRIVATE_KEY')
            if not service_account_email or not private_key:
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=(
                        "No service account credentials provided. "
                        "Provide service_account_file/service_account_json, set GMAIL_SERVICE_ACCOUNT_JSON, "
                        "or supply service_account_email with GMAIL_PRIVATE_KEY."
                    ),
                    advice=[
                        "Download a service account JSON file and set service_account_file",
                        "OR set GMAIL_SERVICE_ACCOUNT_JSON environment variable",
                        "OR set service_account_email and GMAIL_PRIVATE_KEY environment variable",
                    ],
                )
            service_account_info = _build_service_account_info(service_account_email, private_key)
        else:
            private_key = service_account_info.get("private_key")

        # Final validation of service account details
        if not service_account_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message="Service account client_email missing from credentials.",
                advice=[
                    "Ensure the JSON includes client_email.",
                    "If providing service_account_email separately, set the config value.",
                ],
            )
        if '@' not in service_account_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid service_account_email format: {service_account_email}",
                advice=["service_account_email must be a valid email address (e.g., name@project.iam.gserviceaccount.com)"],
            )
        if not private_key:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message="Service account private key missing from credentials.",
                advice=[
                    "Verify the JSON includes private_key.",
                    "If using environment variables, ensure GMAIL_PRIVATE_KEY is populated.",
                ],
            )
        
        # Validate email format
        if '@' not in user_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid user_email format: {user_email}",
                advice=["user_email must be a valid email address"]
            )
        
        if '@' not in service_account_email:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid service_account_email format: {service_account_email}",
                advice=["service_account_email must be a valid email address (e.g., name@project.iam.gserviceaccount.com)"]
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
                        advice=["Provide a positive number for calls_per_ten_seconds (e.g., 10.0, 5.5)"]
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
        
        # Test DNS for Gmail API
        dns_test_gmail = TestTypeDnsResolve({"hostname": "gmail.googleapis.com"})
        dns_result_gmail = dns_test_gmail.run_test()
        
        if not dns_result_gmail.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message="DNS resolution failed for gmail.googleapis.com",
                advice=dns_result_gmail.advice,
                extra_detail={}
            )
        
        # Test DNS for OAuth
        dns_test_oauth = TestTypeDnsResolve({"hostname": "oauth2.googleapis.com"})
        dns_result_oauth = dns_test_oauth.run_test()
        
        if not dns_result_oauth.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message="DNS resolution failed for oauth2.googleapis.com",
                advice=dns_result_oauth.advice,
                extra_detail={}
            )
        
        # =====================================================================
        # 3. OAUTH TOKEN ACQUISITION
        # =====================================================================
        
        token_start = time.time()
        oauth_callable = _resolve_callable('_get_oauth_token')
        success, token, error_type, error_msg = oauth_callable(
            service_account_info, user_email, timeout_seconds
        )
        token_time_ms = (time.time() - token_start) * 1000
        
        if not success:
            advice_lines = [
                "Verify service account credentials are correct",
                "Ensure domain-wide delegation is enabled",
                "Check OAuth scopes in Google Workspace Admin Console"
            ]
            
            if error_type == 'invalid_delegation':
                # Check if the error mentions "Invalid email or User ID"
                is_user_not_found = error_msg and ('invalid email' in error_msg.lower() or 
                                                   'user id' in error_msg.lower())
                
                if is_user_not_found:
                    advice_lines = [
                        f"Google OAuth API error: {error_msg}",
                        "",
                        f"This usually means email address '{user_email}' is incorrect or not accessible.",
                        "Common causes:",
                        "  - Email address doesn't exist (check for typos)",
                        "  - Email address is in a different domain than expected",
                        "  - Service account doesn't have permission to access this user's mailbox",
                        "  - User account is suspended, deleted, or disabled",
                        "",
                        "Troubleshooting steps:",
                        f"  1. Verify '{user_email}' is spelled correctly (check for typos)",
                        "  2. Confirm user exists in your Google Workspace Admin Console",
                        "  3. Ensure user is in the same domain as the service account",
                        f"  4. Check domain-wide delegation is enabled for client ID: {service_account_info.get('client_id', 'N/A')}",
                        f"  5. Verify OAuth scope is granted: {GMAIL_READONLY_SCOPE}",
                        "",
                        "If you have access to another user in the same domain, try testing with that email first"
                    ]
                else:
                    advice_lines = [
                        f"Google OAuth API error: {error_msg}",
                        "",
                        "Domain-wide delegation not properly configured",
                        "In Google Cloud Console > IAM & Admin > Service Accounts:",
                        "  1. Click on your service account",
                        "  2. Click 'Show Domain-Wide Delegation'",
                        "  3. Enable domain-wide delegation",
                        "In Google Workspace Admin Console > Security > API Controls:",
                        "  1. Go to 'Manage Domain Wide Delegation'",
                        f"  2. Add client ID: {service_account_info.get('client_id', 'N/A')}",
                        f"  3. Add OAuth scope: {GMAIL_READONLY_SCOPE}",
                        "  4. Authorize",
                        "Wait a few minutes after making changes before retrying",
                        "",
                        "Note: This error can also occur if the email address is a Google Group (group email)",
                        "that only forwards messages rather than an actual mailbox. Service accounts cannot",
                        "access Google Groups - only real user mailboxes. Verify the email is a real user account."
                    ]
            elif error_type == 'user_not_found':
                advice_lines = [
                    f"Google OAuth API error: {error_msg}",
                    "",
                    f"User email '{user_email}' not found or not accessible",
                    "Verify the email address is correct",
                    "Ensure the user exists in your Google Workspace domain",
                    "Check that service account has access to this user's mailbox"
                ]
            elif error_type == 'missing_dependency':
                advice_lines = [
                    f"Error: {error_msg}",
                    "",
                    "Required Python packages not installed",
                    "Install with: pip install google-auth google-api-python-client"
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
        
        api_callable = _resolve_callable('_test_gmail_api_call')

        # =====================================================================
        # 4. MAILBOX ACCESS TEST - List Messages
        # =====================================================================
        
        mailbox_start = time.time()
        success, data, error_type, error_msg = api_callable(
            f"/users/{user_email}/messages?labelIds={folder_label}&maxResults={max_emails}",
            token,
            timeout_seconds
        )
        mailbox_time_ms = (time.time() - mailbox_start) * 1000
        
        if not success:
            # Generate specific advice based on error type
            # Check for invalid label (special case of api_error)
            is_invalid_label = (error_type == 'api_error' and 
                              error_msg and 'invalid label' in error_msg.lower())
            
            if is_invalid_label:
                advice_lines = [
                    f"Gmail API error: {error_msg}",
                    "",
                    f"Label '{folder_label}' does not exist or is invalid",
                    "Gmail uses specific label names (case-sensitive)",
                    "Common system labels to try:",
                    "  - INBOX (incoming mail)",
                    "  - SENT (sent mail)",
                    "  - DRAFT (drafts)",
                    "  - TRASH (deleted items)",
                    "  - SPAM (spam folder)",
                    "For custom labels, use the exact label ID (e.g., 'Label_123')",
                    "Check label names in Gmail web interface or via API"
                ]
            elif error_type == 'api_not_enabled':
                advice_lines = [
                    f"Gmail API error: {error_msg}",
                    "",
                    "Gmail API is not enabled for this project",
                    "In Google Cloud Console:",
                    "  1. Go to 'APIs & Services' > 'Library'",
                    "  2. Search for 'Gmail API'",
                    "  3. Click 'Enable'",
                    "Wait a few minutes after enabling before retrying"
                ]
            elif error_type == 'permission_denied':
                advice_lines = [
                    f"Gmail API error: {error_msg}",
                    "",
                    "Insufficient permissions to access Gmail",
                    f"Check that OAuth scope '{GMAIL_READONLY_SCOPE}' is granted",
                    "Verify domain-wide delegation is properly configured",
                    "Ensure service account has access to this user's mailbox"
                ]
            elif error_type == 'resource_not_found':
                advice_lines = [
                    f"Gmail API error: {error_msg}",
                    "",
                    f"User mailbox not found: {user_email}",
                    "Verify the email address is correct",
                    "Ensure the user has a Gmail account (not just Google Workspace)",
                    "Check for typos in the email address"
                ]
            else:
                # Generic error fallback
                advice_lines = [
                    f"Gmail API error: {error_msg}",
                    "",
                    f"Cannot access mailbox for user: {user_email}",
                    "Verify the user_email exists in your Google Workspace",
                    "Ensure Gmail API is enabled in Google Cloud Console",
                    f"Check OAuth scopes include {GMAIL_READONLY_SCOPE}"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"Mailbox access failed: {error_msg}",
                advice=advice_lines,
                extra_detail=prerequisite_results
            )
        
        messages = data.get('messages', [])
        message_count = len(messages)
        
        prerequisite_results['mailbox_access'] = {
            'ok': True,
            'folder_label': folder_label,
            'message_count': message_count,
            'response_time_ms': round(mailbox_time_ms, 2)
        }
        
        # Check if folder is empty - this means we can't fully test email retrieval
        if not messages:
            prerequisite_results['mailbox_access']['warning'] = 'EMPTY_MAILBOX'
            prerequisite_results['mailbox_access']['message'] = (
                f"No messages found in folder '{folder_label}'. Retrieval checks skipped."
            )
            prerequisite_results['email_metadata_retrieval'] = {
                'ok': True,
                'skipped': True,
                'reason': 'EMPTY_MAILBOX'
            }
            prerequisite_results['email_body_retrieval'] = {
                'ok': True,
                'skipped': True,
                'reason': 'EMPTY_MAILBOX'
            }
            prerequisite_results['email_retrieval'] = {
                'ok': False,
                'error': f"No messages found in folder '{folder_label}'",
                'reason': 'EMPTY_MAILBOX'
            }
            total_time_ms = (time.time() - test_start_time) * 1000
            return TestResult(
                success=True,
                extra_detail={
                    **prerequisite_results,
                    'summary': {
                        'total_test_time_ms': round(total_time_ms, 2),
                        'all_tests_passed': True,
                        'service_account_client_email': service_account_info.get('client_email', 'unknown'),
                        'user_email': user_email,
                        'folder_label': folder_label,
                        'note': 'Mailbox empty; retrieval tests skipped.'
                    }
                }
            )
        
        # =====================================================================
        # 5. EMAIL RETRIEVAL TEST
        # =====================================================================
        
        if messages:
            test_msg_id = messages[0]['id']
            
            # Determine format based on metadata_only_mode setting
            if metadata_only_mode:
                # Test metadata-only retrieval
                metadata_start = time.time()
                success, data, error_type, error_msg = api_callable(
                    f"/users/{user_email}/messages/{test_msg_id}?format=metadata",
                    token,
                    timeout_seconds
                )
                metadata_time_ms = (time.time() - metadata_start) * 1000
                
                if not success:
                    prerequisite_results['email_metadata_retrieval'] = {
                        'ok': False,
                        'error': error_msg,
                        'response_time_ms': round(metadata_time_ms, 2)
                    }
                    
                    # Email metadata retrieval failed - cannot proceed
                    return TestResult(
                        success=False,
                        error_type=error_type,
                        error_message=f"Email metadata retrieval failed: {error_msg}",
                        advice=[
                            f"Gmail API error: {error_msg}",
                            "",
                            f"Failed to retrieve email metadata for message {test_msg_id}",
                            "This usually indicates:",
                            "  - Message was deleted between listing and retrieval",
                            "  - Insufficient permissions to read message content",
                            "  - Temporary API error or timeout",
                            "",
                            "Troubleshooting:",
                            "  - Try running the test again (message may have been deleted)",
                            "  - Increase --timeout-seconds if network is slow",
                            f"  - Verify OAuth scope includes {GMAIL_READONLY_SCOPE}"
                        ],
                        extra_detail=prerequisite_results
                    )
                else:
                    # Extract some metadata info
                    headers = {h['name']: h['value'] for h in data.get('payload', {}).get('headers', [])}
                    subject = headers.get('Subject', '(No Subject)')
                    from_addr = headers.get('From', '(Unknown)')
                    
                    prerequisite_results['email_metadata_retrieval'] = {
                        'ok': True,
                        'message_id': test_msg_id,
                        'subject': subject[:100],  # Truncate long subjects
                        'from': from_addr,
                        'response_time_ms': round(metadata_time_ms, 2)
                    }
            else:
                # Test full email retrieval (raw MIME format)
                body_start = time.time()
                success, data, error_type, error_msg = api_callable(
                    f"/users/{user_email}/messages/{test_msg_id}?format=raw",
                    token,
                    timeout_seconds
                )
                body_time_ms = (time.time() - body_start) * 1000
                
                if not success:
                    prerequisite_results['email_body_retrieval'] = {
                        'ok': False,
                        'error': error_msg,
                        'response_time_ms': round(body_time_ms, 2)
                    }
                    
                    # Email body retrieval failed - cannot proceed
                    return TestResult(
                        success=False,
                        error_type=error_type,
                        error_message=f"Email body retrieval failed: {error_msg}",
                        advice=[
                            f"Gmail API error: {error_msg}",
                            "",
                            f"Failed to retrieve full email content for message {test_msg_id}",
                            "This usually indicates:",
                            "  - Message was deleted between listing and retrieval",
                            "  - Insufficient permissions to read message content",
                            "  - Message is too large or corrupted",
                            "  - Network timeout during download",
                            "",
                            "Troubleshooting:",
                            "  - Try running the test again (message may have been deleted)",
                            "  - Try --test-metadata-only yes to test without downloading full content",
                            "  - Increase --timeout-seconds if downloading large emails",
                            f"  - Verify OAuth scope includes {GMAIL_READONLY_SCOPE}"
                        ],
                        extra_detail=prerequisite_results
                    )
                else:
                    raw_content = data.get('raw', '')
                    mime_size_bytes = len(raw_content)
                    
                    prerequisite_results['email_body_retrieval'] = {
                        'ok': True,
                        'message_id': test_msg_id,
                        'mime_size_bytes': mime_size_bytes,
                        'response_time_ms': round(body_time_ms, 2)
                    }
        
        # =====================================================================
        # 6. PERMISSION BOUNDARY TEST (if forbidden_email provided)
        # =====================================================================
        
        if forbidden_email:
            forbidden_start = time.time()
            success, data, error_type, error_msg = api_callable(
                f"/users/{forbidden_email}/messages?labelIds={folder_label}&maxResults=1",
                token,
                timeout_seconds
            )
            forbidden_time_ms = (time.time() - forbidden_start) * 1000
            
            if success:
                # Security violation: Successfully accessed a forbidden mailbox
                # This indicates excessive permissions and must fail the overall test
                prerequisite_results['permission_boundary'] = {
                    'ok': False,
                    'warning': 'EXCESSIVE_PERMISSIONS',
                    'forbidden_email': forbidden_email,
                    'forbidden_access_granted': True,
                    'message': 'Service account has access to mailboxes it should not access',
                    'remediation': {
                        'problem': f"Service account can access forbidden mailbox '{forbidden_email}'. It likely has unrestricted access to ALL mailboxes in the organization!",
                        'gmail_limitation': 'Gmail API does not support mailbox-level access restrictions like Microsoft 365',
                        'solutions': [
                            "Option 1: Use separate service accounts for different user groups",
                            "Option 2: Implement access control in your application code (maintain allowlist)",
                            "Option 3: Use Google Groups to organize users and check membership before access",
                            "Option 4: Monitor access logs and alert on unauthorized mailbox access"
                        ],
                        'security_note': 'Domain-wide delegation inherently grants access to all users in the domain. You must implement application-level restrictions.'
                    }
                }
                
                # RETURN FAILURE immediately - don't continue to success
                return TestResult(
                    success=False,
                    error_type="excessive_permissions",
                    error_message=f"SECURITY VIOLATION: Service account has access to forbidden mailbox '{forbidden_email}'",
                    advice=[
                        f"Service account successfully accessed '{forbidden_email}' mailbox - this should not be possible!",
                        "This indicates excessive permissions - likely unrestricted access to ALL organization mailboxes",
                        "",
                        "Gmail API Limitation:",
                        "  Unlike Microsoft 365, Gmail API does not support per-mailbox access policies",
                        "  Domain-wide delegation grants access to ALL users in the domain",
                        "",
                        "Security Recommendations:",
                        "  1. Implement application-level access control (maintain allowlist of authorized users)",
                        "  2. Use separate service accounts for different user groups/purposes",
                        "  3. Monitor and audit service account access via Google Workspace logs",
                        "  4. Use Google Groups to organize users and verify membership before access",
                        "",
                        "See 'remediation' in extra_detail for more detailed solutions"
                    ],
                    extra_detail=prerequisite_results
                )
            else:
                # Access was denied - this is the correct/expected behavior for security
                if error_type in ['permission_denied', 'resource_not_found', 'invalid_delegation']:
                    prerequisite_results['permission_boundary'] = {
                        'ok': True,
                        'forbidden_email': forbidden_email,
                        'access_granted': False,
                        'message': 'Correctly denied access to forbidden mailbox',
                        'response_time_ms': round(forbidden_time_ms, 2),
                        'note': 'Access was properly denied - this is the expected behavior'
                    }
                else:
                    # Unexpected error type
                    prerequisite_results['permission_boundary'] = {
                        'ok': False,
                        'forbidden_email': forbidden_email,
                        'error': f"Unexpected error type when testing forbidden mailbox: {error_type}",
                        'error_message': error_msg,
                        'response_time_ms': round(forbidden_time_ms, 2)
                    }
        
        # =====================================================================
        # 7. RATE LIMIT TEST (if calls_per_ten_seconds provided)
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
                    
                    # Use simple message list endpoint for rate testing
                    endpoint = f"/users/{user_email}/messages?labelIds={folder_label}&maxResults=1"
                    
                    for i in range(total_calls):
                        success, response_time, was_rate_limited = await _make_async_request(
                            endpoint, token, timeout_seconds
                        )
                        
                        if was_rate_limited:
                            rate_limited_count += 1
                        
                        if success:
                            success_count += 1
                        else:
                            error_count += 1
                        
                        call_results.append({
                            'call_number': i + 1,
                            'success': success,
                            'response_time_ms': round(response_time, 2),
                            'was_rate_limited': was_rate_limited
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
                        # Average response time (protected against division by zero by ternary)
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
                    'service_account_client_email': service_account_info.get('client_email', 'unknown'),
                    'user_email': user_email,
                    'folder_label': folder_label
                }
            }
        )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeGmail,
    shortname="gmail",
    is_public=True,
    description="Tests Gmail API access via service account authentication"
)

