#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Azure AD SSO credential validation test plugin.

This module validates Azure AD SSO credentials (Client ID, Tenant ID, Client Secret)
by attempting OAuth2 client credentials authentication. This verifies that:
- Credentials are correct and valid
- App registration exists and is active
- Client secret has not expired
- Network connectivity to Azure AD is working

Note: This test validates credentials only. It does NOT test the full SSO flow
(redirect URI, user login, callback) which requires user interaction and browser-based
authentication. After credential validation passes, test the full SSO flow in your application.

Environment Variables:
    SSO_AZURE_CLIENT_SECRET: Client secret from Azure app registration
"""

from __future__ import annotations

import os
import time
import hashlib
from typing import Any, Dict, List, Optional, Type

import requests

from conn_tester_support.core import TestTypeBase
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_TIMEOUT_SECONDS = 30

# Azure AD endpoints
AZURE_AD_BASE = "https://login.microsoftonline.com"


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
    Acquire OAuth2 token using client credentials flow to validate SSO credentials.
    
    Uses the client ID with /.default suffix, which is the standard format for
    client credentials flow. This validates credentials without requiring specific
    API permissions, making it suitable for SSO credential validation.
    
    Returns:
        (success, token, error_type, error_message)
    """
    url = f"{AZURE_AD_BASE}/{tenant_id}/oauth2/v2.0/token"
    
    # For client credentials flow, scope must be in format: {resource}/.default
    # Using the client_id itself as the resource validates credentials without
    # requiring specific API permissions
    scope = f"{client_id}/.default"
    
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': scope,
        'grant_type': 'client_credentials'
    }
    
    try:
        response = requests.post(url, data=data, timeout=timeout)
        
        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get('access_token')
            if access_token:
                return (True, access_token, None, None)
            else:
                return (False, None, 'auth_failed', "Token response missing access_token")
        
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
            elif error_code == 'invalid_grant':
                # This can indicate expired secret or invalid credentials
                if 'expired' in error_desc.lower() or 'expir' in error_desc.lower():
                    return (False, None, 'secret_expired', f"Client secret may be expired: {error_desc}")
                return (False, None, 'invalid_client', f"Invalid grant: {error_desc}")
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


# =============================================================================
# TEST CLASS
# =============================================================================

class TestTypeSsoAzure(TestTypeBase):
    """Azure AD SSO credential validation testing"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Validates Azure AD SSO credentials (Client ID, Tenant ID, Client Secret). "
                       "Note: This validates credentials only, not the full SSO flow. "
                       "Test the complete SSO flow (redirect URI, user login, callback) in your application.",
            config_fields={
                "tenant_id": "Azure AD tenant ID (Directory ID)",
                "client_id": "Application (client) ID from Azure app registration",
                "timeout_seconds": f"Request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})"
            },
            required_fields=["tenant_id", "client_id"],
            optional_fields=["timeout_seconds"],
            confidential_fields=["SSO_AZURE_CLIENT_SECRET"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def get_configs(self) -> Dict[str, Any]:
        """Return current configuration with credential hashing and defaults applied"""
        config = self.config.copy()
        
        # Apply defaults
        if config.get("timeout_seconds") is None:
            config["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
        
        # Hash client_secret if present in environment
        if 'SSO_AZURE_CLIENT_SECRET' in os.environ:
            secret = os.environ['SSO_AZURE_CLIENT_SECRET']
            config['client_secret'] = _hash_credential(secret)
        
        return config
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        """Execute Azure AD SSO credential validation test"""
        
        # =====================================================================
        # 1. VALIDATE CONFIGURATION
        # =====================================================================
        
        if not self.config.get("tenant_id"):
            raise ConfigurationError("tenant_id is required")
        if not self.config.get("client_id"):
            raise ConfigurationError("client_id is required")
        
        tenant_id = self.config.get("tenant_id")
        client_id = self.config.get("client_id")
        timeout_seconds = int(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        
        # Validate tenant_id format (should be a GUID)
        if not tenant_id or len(tenant_id.strip()) < 10:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid tenant_id format: {tenant_id}",
                advice=[
                    "tenant_id should be a GUID (e.g., xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)",
                    "Find tenant_id in Azure Portal > Azure Active Directory > Overview > Tenant ID"
                ]
            )
        
        # Validate client_id format (should be a GUID)
        if not client_id or len(client_id.strip()) < 10:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid client_id format: {client_id}",
                advice=[
                    "client_id should be a GUID (e.g., xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)",
                    "Find client_id in Azure Portal > App registrations > Your app > Overview > Application (client) ID"
                ]
            )
        
        # Get client secret from environment
        client_secret = os.getenv('SSO_AZURE_CLIENT_SECRET')
        if not client_secret:
            return self.create_env_var_error(
                env_var_name='SSO_AZURE_CLIENT_SECRET',
                description='Azure AD SSO client secret for OAuth2 authentication'
            )
        
        prerequisite_results = {}
        test_start_time = time.time()
        
        # =====================================================================
        # 2. DNS PREREQUISITE TESTS
        # =====================================================================
        
        # Test DNS for Azure AD
        dns_test_ad = TestTypeDnsResolve({"hostname": "login.microsoftonline.com"})
        dns_result_ad = dns_test_ad.run_test()
        
        if not dns_result_ad.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message="DNS resolution failed for login.microsoftonline.com",
                advice=dns_result_ad.advice,
                extra_detail={}
            )
        
        prerequisite_results['dns_azure_ad'] = dns_result_ad.extra_detail
        
        # =====================================================================
        # 3. OAUTH TOKEN ACQUISITION (CREDENTIAL VALIDATION)
        # =====================================================================
        
        token_start = time.time()
        success, token, error_type, error_msg = _get_oauth_token(
            tenant_id, client_id, client_secret, timeout_seconds
        )
        token_time_ms = (time.time() - token_start) * 1000
        
        if not success:
            advice_lines = [
                f"Azure AD error: {error_msg}",
                "",
                "This usually means one of the following:",
                "1. Client ID, Tenant ID, or Client Secret is incorrect",
                "2. The Azure app registration does not exist or is disabled",
                "3. The client secret has expired",
                "",
                "Troubleshooting steps:"
            ]
            
            if error_type == 'invalid_tenant':
                advice_lines.extend([
                    "1. Verify tenant_id (Directory ID) is correct",
                    "   - Find in Azure Portal > Azure Active Directory > Overview > Tenant ID",
                    "2. Ensure you're using the correct tenant for your organization",
                    "3. Check that the tenant ID is a valid GUID format"
                ])
            elif error_type == 'invalid_client' or error_type == 'secret_expired':
                advice_lines.extend([
                    "1. Verify client_id (Application ID) is correct",
                    "   - Find in Azure Portal > App registrations > Your app > Overview > Application (client) ID",
                    "2. Check that client_secret (Client Secret) is correct and not expired",
                    "   - Client secrets expire - generate a new one if needed",
                    "   - Find in Azure Portal > App registrations > Your app > Certificates & secrets",
                    "3. Ensure the app registration is not disabled",
                    "4. Verify the client secret value was copied correctly (no extra spaces or characters)"
                ])
            elif error_type == 'timeout':
                advice_lines.extend([
                    "1. Check network connectivity to login.microsoftonline.com",
                    "2. Verify firewall rules allow outbound HTTPS connections",
                    "3. Try increasing the timeout_seconds parameter"
                ])
            elif error_type == 'connection_error':
                advice_lines.extend([
                    "1. Check network connectivity to login.microsoftonline.com",
                    "2. Verify DNS resolution is working",
                    "3. Check firewall and proxy settings"
                ])
            else:
                advice_lines.extend([
                    "1. Verify all credentials are correct (tenant_id, client_id, client_secret)",
                    "2. Check that the app registration exists and is active",
                    "3. Ensure the client secret has not expired",
                    "4. Verify network connectivity to Azure AD"
                ])
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"SSO credential validation failed: {error_msg}",
                advice=advice_lines,
                extra_detail={
                    **prerequisite_results,
                    "credential_validation": {
                        "ok": False,
                        "error_type": error_type,
                        "error_message": error_msg,
                        "token_acquisition_time_ms": round(token_time_ms, 2)
                    }
                }
            )
        
        # =====================================================================
        # 4. SUCCESS - CREDENTIALS VALIDATED
        # =====================================================================
        
        total_time_ms = (time.time() - test_start_time) * 1000
        
        return TestResult(
            success=True,
            extra_detail={
                **prerequisite_results,
                "credential_validation": {
                    "ok": True,
                    "tenant_id": tenant_id,
                    "client_id": client_id,
                    "token_acquired": True,
                    "token_acquisition_time_ms": round(token_time_ms, 2),
                    "total_test_time_ms": round(total_time_ms, 2)
                },
                "validation_scope": {
                    "what_this_test_validates": [
                        "Client ID, Tenant ID, and Client Secret are correct and valid",
                        "App registration exists and is active in Azure AD",
                        "Client secret has not expired",
                        "Network connectivity to Azure AD (login.microsoftonline.com) is working",
                        "OAuth token can be acquired using client credentials flow"
                    ],
                    "what_this_test_does_not_validate": [
                        "Redirect URI configuration (must match exactly what's configured in Azure Portal)",
                        "Full SSO flow (user login, redirect to Microsoft, callback to application)",
                        "User authentication (requires actual user sign-in)",
                        "OAuth authorization code flow (this test only validates client credentials flow)",
                        "Application's ability to handle the OAuth callback",
                        "Redirect URI format and path matching"
                    ],
                    "next_steps": [
                        "Test the complete SSO flow in your application",
                        "Verify redirect URI matches Azure Portal configuration exactly",
                        "Test user authentication with actual Windows 365 accounts",
                        "Verify callback handling in your application"
                    ]
                }
            }
        )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeSsoAzure,
    shortname="sso-azure",
    is_public=True,
    description="Validates Azure AD SSO credentials (Client ID, Tenant ID, Client Secret)"
)

