#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test suite for Gmail connection test plugin.

Tests the Gmail API connectivity testing functionality including:
- Configuration validation
- Service account credential loading
- OAuth token acquisition
- Mailbox access
- Email retrieval (metadata and full content)
- Error handling and advice generation
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
from typing import Dict, Any

import pytest

# Import core testing infrastructure
from totodev_pub.cli.conn_tester_support.core import TestTypeBase
from totodev_pub.cli.conn_tester_support.models import TestResult, ConfigurationError

# Load the Gmail plugin using the dynamic plugin loader
# This avoids import path issues with relative imports
TestTypeGmail = TestTypeBase.load_test_plugin('gmail')

# Get access to the plugin module to test helper functions
import sys
gmail_module = sys.modules[TestTypeGmail.__module__]

# Import helper functions and constants from the loaded module
_load_service_account_info = gmail_module._load_service_account_info
_hash_service_account = gmail_module._hash_service_account
_get_oauth_token = gmail_module._get_oauth_token
_test_gmail_api_call = gmail_module._test_gmail_api_call
DEFAULT_TIMEOUT_SECONDS = gmail_module.DEFAULT_TIMEOUT_SECONDS
DEFAULT_FOLDER_LABEL = gmail_module.DEFAULT_FOLDER_LABEL
DEFAULT_MAX_EMAILS_TO_FETCH = gmail_module.DEFAULT_MAX_EMAILS_TO_FETCH


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def valid_service_account_json():
    """Return a valid service account JSON structure"""
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key123",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC...\n-----END PRIVATE KEY-----\n",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/test%40test-project.iam.gserviceaccount.com"
    }


@pytest.fixture
def mock_gmail_messages_response():
    """Return a mock Gmail API messages.list response"""
    return {
        "messages": [
            {"id": "msg123", "threadId": "thread123"},
            {"id": "msg456", "threadId": "thread456"}
        ],
        "resultSizeEstimate": 2
    }


@pytest.fixture
def mock_gmail_message_metadata():
    """Return a mock Gmail API message metadata response"""
    return {
        "id": "msg123",
        "threadId": "thread123",
        "labelIds": ["INBOX"],
        "snippet": "This is a test email...",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test Email"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "recipient@example.com"},
                {"name": "Date", "value": "Mon, 1 Jan 2025 10:00:00 +0000"}
            ]
        },
        "sizeEstimate": 1234
    }


@pytest.fixture
def mock_gmail_message_raw():
    """Return a mock Gmail API message raw response"""
    return {
        "id": "msg123",
        "threadId": "thread123",
        "raw": "RnJvbTogc2VuZGVyQGV4YW1wbGUuY29tClRvOiByZWNpcGllbnRAZXhhbXBsZS5jb20KU3ViamVjdDogVGVzdCBFbWFpbApEYXRlOiBNb24sIDEgSmFuIDIwMjUgMTA6MDA6MDAgKzAwMDAKCkhlbGxvLCB0aGlzIGlzIGEgdGVzdCBlbWFpbC4="
    }


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestHelperFunctions:
    """Test helper functions"""
    
    def test_hash_service_account(self, valid_service_account_json):
        """Test service account hashing for logging"""
        hashed = _hash_service_account(valid_service_account_json)
        
        assert "test@test-project.iam.gserviceaccount.com" in hashed
        assert "test-project" in hashed
        assert "key_hash:" in hashed
        assert len(hashed) > 50  # Has meaningful content
    
    def test_hash_service_account_missing_fields(self):
        """Test service account hashing with missing fields"""
        partial_account = {"client_email": "test@example.com"}
        hashed = _hash_service_account(partial_account)
        
        assert "test@example.com" in hashed
        assert "unknown" in hashed  # Default for missing fields
    
    def test_load_service_account_from_file(self, valid_service_account_json, tmp_path):
        """Test loading service account from file"""
        # Create temporary JSON file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        success, account_info, error = _load_service_account_info(str(json_file), None)
        
        assert success is True
        assert account_info == valid_service_account_json
        assert error is None
    
    def test_load_service_account_from_env_var(self, valid_service_account_json):
        """Test loading service account from environment variable"""
        json_string = json.dumps(valid_service_account_json)
        
        success, account_info, error = _load_service_account_info(None, json_string)
        
        assert success is True
        assert account_info == valid_service_account_json
        assert error is None
    
    def test_load_service_account_file_not_found(self):
        """Test loading service account with non-existent file"""
        success, account_info, error = _load_service_account_info("/nonexistent/file.json", None)
        
        assert success is False
        assert account_info is None
        assert "not found" in error.lower()
    
    def test_load_service_account_invalid_json(self, tmp_path):
        """Test loading service account with invalid JSON"""
        json_file = tmp_path / "invalid.json"
        json_file.write_text("{ invalid json }")
        
        success, account_info, error = _load_service_account_info(str(json_file), None)
        
        assert success is False
        assert account_info is None
        assert "invalid json" in error.lower()
    
    def test_load_service_account_missing_required_fields(self, tmp_path):
        """Test loading service account with missing required fields"""
        incomplete_json = {"type": "service_account", "client_email": "test@example.com"}
        json_file = tmp_path / "incomplete.json"
        with open(json_file, 'w') as f:
            json.dump(incomplete_json, f)
        
        success, account_info, error = _load_service_account_info(str(json_file), None)
        
        assert success is False
        assert account_info is None
        assert "missing required fields" in error.lower()
        assert "private_key" in error  # Should mention which fields are missing
    
    def test_load_service_account_no_credentials(self):
        """Test loading service account with no credentials provided"""
        success, account_info, error = _load_service_account_info(None, None)
        
        assert success is False
        assert account_info is None
        assert "no service account credentials" in error.lower()


# =============================================================================
# TestTypeGmail Class Tests
# =============================================================================

class TestGmailTestType:
    """Test TestTypeGmail class"""
    
    def test_describe_self(self):
        """Test test metadata structure"""
        metadata = TestTypeGmail.describe_self()
        
        assert "Gmail" in metadata.description
        assert "user_email" in metadata.required_fields
        assert "service_account_file" in metadata.optional_fields
        assert "folder_label" in metadata.optional_fields
        assert "GMAIL_SERVICE_ACCOUNT_JSON" in metadata.confidential_fields
    
    def test_prerequisite_tests(self):
        """Test that DNS prerequisite is included"""
        prerequisites = TestTypeGmail.prerequisite_tests()
        
        assert len(prerequisites) == 1
        from totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve
        assert prerequisites[0] == TestTypeDnsResolve
    
    def test_get_configs_with_defaults(self):
        """Test get_configs applies defaults"""
        test = TestTypeGmail({"user_email": "test@example.com"})
        configs = test.get_configs()
        
        assert configs["user_email"] == "test@example.com"
        assert configs["folder_label"] == DEFAULT_FOLDER_LABEL
        assert configs["max_emails"] == DEFAULT_MAX_EMAILS_TO_FETCH
        assert configs["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS
    
    def test_get_configs_hides_credentials(self, tmp_path):
        """Test get_configs masks credentials"""
        json_file = tmp_path / "creds.json"
        json_file.write_text('{"type":"service_account"}')
        
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file)
        })
        configs = test.get_configs()
        
        assert "<file:" in configs["service_account_file"]
        assert str(json_file) in configs["service_account_file"]
    
    def test_run_test_missing_user_email(self):
        """Test run_test fails without user_email"""
        test = TestTypeGmail({})
        
        with pytest.raises(ConfigurationError) as exc_info:
            test.run_test()
        
        assert "user_email is required" in str(exc_info.value)
    
    def test_run_test_invalid_email_format(self):
        """Test run_test rejects invalid email format"""
        test = TestTypeGmail({"user_email": "not-an-email"})
        result = test.run_test()
        
        assert result.success is False
        assert result.error_type == "config_invalid"
        assert "invalid user_email format" in result.error_message.lower()
    
    def test_run_test_no_credentials(self):
        """Test run_test fails when no credentials provided"""
        test = TestTypeGmail({"user_email": "test@example.com"})
        
        with patch.dict(os.environ, {}, clear=True):
            result = test.run_test()
        
        assert result.success is False
        assert result.error_type == "config_invalid"
        assert "GMAIL_SERVICE_ACCOUNT_JSON" in result.error_message or "No service account credentials" in result.error_message
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._test_gmail_api_call')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._get_oauth_token')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_success_metadata_only(self, mock_dns, mock_oauth, mock_api_call, 
                                           valid_service_account_json, mock_gmail_messages_response,
                                           mock_gmail_message_metadata, tmp_path):
        """Test successful run with metadata-only retrieval"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS success
        mock_dns.return_value = TestResult(success=True)
        
        # Mock OAuth success
        mock_oauth.return_value = (True, "mock_token", None, None)
        
        # Mock API calls
        def api_side_effect(endpoint, token, timeout):
            if "messages?" in endpoint:
                return (True, mock_gmail_messages_response, None, None)
            elif "format=metadata" in endpoint:
                return (True, mock_gmail_message_metadata, None, None)
            else:
                return (False, None, "api_error", "Unknown endpoint")
        
        mock_api_call.side_effect = api_side_effect
        
        # Run test
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file),
            "test_metadata_only": "yes"
        })
        result = test.run_test()
        
        # Verify success
        assert result.success is True
        assert "all_tests_passed" in result.extra_detail.get("summary", {})
        assert result.extra_detail["summary"]["all_tests_passed"] is True
        
        # Verify mailbox access
        assert "mailbox_access" in result.extra_detail
        assert result.extra_detail["mailbox_access"]["ok"] is True
        assert result.extra_detail["mailbox_access"]["message_count"] == 2
        
        # Verify email metadata retrieval
        assert "email_metadata_retrieval" in result.extra_detail
        assert result.extra_detail["email_metadata_retrieval"]["ok"] is True
        assert "Test Email" in result.extra_detail["email_metadata_retrieval"]["subject"]
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._test_gmail_api_call')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._get_oauth_token')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_success_full_retrieval(self, mock_dns, mock_oauth, mock_api_call, 
                                             valid_service_account_json, mock_gmail_messages_response,
                                             mock_gmail_message_raw, tmp_path):
        """Test successful run with full email retrieval"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS success
        mock_dns.return_value = TestResult(success=True)
        
        # Mock OAuth success
        mock_oauth.return_value = (True, "mock_token", None, None)
        
        # Mock API calls
        def api_side_effect(endpoint, token, timeout):
            if "messages?" in endpoint:
                return (True, mock_gmail_messages_response, None, None)
            elif "format=raw" in endpoint:
                return (True, mock_gmail_message_raw, None, None)
            else:
                return (False, None, "api_error", "Unknown endpoint")
        
        mock_api_call.side_effect = api_side_effect
        
        # Run test
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file)
        })
        result = test.run_test()
        
        # Verify success
        assert result.success is True
        
        # Verify email body retrieval
        assert "email_body_retrieval" in result.extra_detail
        assert result.extra_detail["email_body_retrieval"]["ok"] is True
        assert result.extra_detail["email_body_retrieval"]["mime_size_bytes"] > 0
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._get_oauth_token')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_oauth_failure(self, mock_dns, mock_oauth, 
                                    valid_service_account_json, tmp_path):
        """Test OAuth token acquisition failure"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS success
        mock_dns.return_value = TestResult(success=True)
        
        # Mock OAuth failure
        mock_oauth.return_value = (False, None, "invalid_delegation", 
                                   "Domain-wide delegation not configured")
        
        # Run test
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file)
        })
        result = test.run_test()
        
        # Verify failure
        assert result.success is False
        assert result.error_type == "invalid_delegation"
        assert "domain-wide delegation" in result.error_message.lower()
        assert any("delegation" in advice.lower() for advice in result.advice)
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._test_gmail_api_call')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._get_oauth_token')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_api_not_enabled(self, mock_dns, mock_oauth, mock_api_call, 
                                      valid_service_account_json, tmp_path):
        """Test Gmail API not enabled error"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS success
        mock_dns.return_value = TestResult(success=True)
        
        # Mock OAuth success
        mock_oauth.return_value = (True, "mock_token", None, None)
        
        # Mock API not enabled error
        mock_api_call.return_value = (False, None, "api_not_enabled", 
                                      "Gmail API has not been used in project")
        
        # Run test
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file)
        })
        result = test.run_test()
        
        # Verify failure
        assert result.success is False
        assert result.error_type == "api_not_enabled"
        assert "gmail api" in result.error_message.lower()
        assert any("enable" in advice.lower() for advice in result.advice)
        assert any("google cloud console" in advice.lower() for advice in result.advice)
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._test_gmail_api_call')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._get_oauth_token')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_mailbox_not_found(self, mock_dns, mock_oauth, mock_api_call, 
                                        valid_service_account_json, tmp_path):
        """Test mailbox not found error"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS success
        mock_dns.return_value = TestResult(success=True)
        
        # Mock OAuth success
        mock_oauth.return_value = (True, "mock_token", None, None)
        
        # Mock mailbox not found
        mock_api_call.return_value = (False, None, "resource_not_found", 
                                      "Requested entity was not found")
        
        # Run test
        test = TestTypeGmail({
            "user_email": "nonexistent@example.com",
            "service_account_file": str(json_file)
        })
        result = test.run_test()
        
        # Verify failure
        assert result.success is False
        assert result.error_type == "resource_not_found"
        assert "mailbox" in result.error_message.lower() or "not found" in result.error_message.lower()
        assert any("verify" in advice.lower() or "email address" in advice.lower() 
                  for advice in result.advice)
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._test_gmail_api_call')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_gmail._get_oauth_token')
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_empty_mailbox(self, mock_dns, mock_oauth, mock_api_call, 
                                    valid_service_account_json, tmp_path):
        """Test handling of empty mailbox"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS success
        mock_dns.return_value = TestResult(success=True)
        
        # Mock OAuth success
        mock_oauth.return_value = (True, "mock_token", None, None)
        
        # Mock empty mailbox
        mock_api_call.return_value = (True, {"messages": []}, None, None)
        
        # Run test
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file)
        })
        result = test.run_test()
        
        # Verify success (empty mailbox is not an error)
        assert result.success is True
        assert result.extra_detail["mailbox_access"]["message_count"] == 0
        
        # Verify email retrieval was skipped
        assert "email_retrieval" in result.extra_detail
        assert result.extra_detail["email_retrieval"]["ok"] is False
        assert "no messages" in result.extra_detail["email_retrieval"]["error"].lower()
    
    @patch('totodev_pub.cli.conn_tester_support.test_plugins.conntest_dns.TestTypeDnsResolve.run_test')
    def test_run_test_dns_failure(self, mock_dns, valid_service_account_json, tmp_path):
        """Test DNS prerequisite failure"""
        # Create service account file
        json_file = tmp_path / "service_account.json"
        with open(json_file, 'w') as f:
            json.dump(valid_service_account_json, f)
        
        # Mock DNS failure
        mock_dns.return_value = TestResult(
            success=False,
            error_type="dns_resolve",
            error_message="DNS resolution failed",
            advice=["Check network connectivity", "Verify DNS server"]
        )
        
        # Run test
        test = TestTypeGmail({
            "user_email": "test@example.com",
            "service_account_file": str(json_file)
        })
        result = test.run_test()
        
        # Verify failure
        assert result.success is False
        assert result.error_type == "dns_resolve"
        assert "dns resolution failed" in result.error_message.lower()


# =============================================================================
# Integration Tests
# =============================================================================

class TestGmailIntegration:
    """Integration tests requiring actual API (skip by default)"""
    
    @pytest.mark.skip(reason="Requires real Gmail credentials")
    def test_real_gmail_connection(self):
        """Test with real Gmail credentials (manual testing only)"""
        # This test should be run manually with real credentials
        # export GMAIL_SERVICE_ACCOUNT_JSON='...'
        # pytest -k test_real_gmail_connection -s
        
        service_account_json = os.getenv('GMAIL_SERVICE_ACCOUNT_JSON')
        user_email = os.getenv('GMAIL_TEST_USER_EMAIL', 'test@example.com')
        
        if not service_account_json:
            pytest.skip("GMAIL_SERVICE_ACCOUNT_JSON not set")
        
        test = TestTypeGmail({
            "user_email": user_email,
            "folder_label": "INBOX",
            "max_emails": 1
        })
        
        result = test.run_test()
        
        # Print result for manual verification
        print("\n=== Gmail Test Result ===")
        print(f"Success: {result.success}")
        print(f"Error Type: {result.error_type}")
        print(f"Error Message: {result.error_message}")
        print(f"Advice: {result.advice}")
        print(f"Extra Detail: {result.extra_detail}")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

