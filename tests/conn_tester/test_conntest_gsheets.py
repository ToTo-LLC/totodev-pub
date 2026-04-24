#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test suite for Google Sheets connection test plugin.

Tests the Google Sheets API connectivity testing functionality including:
- Configuration validation
- Service account credential loading
- OAuth token acquisition
- Spreadsheet metadata access
- Sheet selection
- Cell reading
- Error handling and advice generation
"""

import os
from unittest.mock import patch, MagicMock
from typing import Dict, Any

import pytest

# Import core testing infrastructure
from totodev_pub.cli.conn_tester_support.core import TestTypeBase
from totodev_pub.cli.conn_tester_support.models import TestResult, ConfigurationError

# Load the Google Sheets plugin using the dynamic plugin loader
# This avoids import path issues with relative imports
TestTypeGoogleSheets = TestTypeBase.load_test_plugin('gsheets')

# Get access to the plugin module to test helper functions
import sys
gsheets_module = sys.modules[TestTypeGoogleSheets.__module__]

# Import helper functions and constants from the loaded module
_build_service_account_info = gsheets_module._build_service_account_info
_hash_service_account = gsheets_module._hash_service_account
_extract_spreadsheet_id = gsheets_module._extract_spreadsheet_id
_get_oauth_token = gsheets_module._get_oauth_token
_test_sheets_api_call = gsheets_module._test_sheets_api_call
DEFAULT_TIMEOUT_SECONDS = gsheets_module.DEFAULT_TIMEOUT_SECONDS


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def valid_service_account_info():
    """Return a valid service account info structure"""
    return {
        "type": "service_account",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC...\n-----END PRIVATE KEY-----\n",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token"
    }


@pytest.fixture
def mock_spreadsheet_metadata():
    """Return a mock Google Sheets API spreadsheet metadata response"""
    return {
        "spreadsheetId": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M",
        "properties": {
            "title": "Test Spreadsheet"
        },
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "Sheet1",
                    "index": 0
                }
            },
            {
                "properties": {
                    "sheetId": 1,
                    "title": "Sheet2",
                    "index": 1
                }
            },
            {
                "properties": {
                    "sheetId": 2,
                    "title": "Config",
                    "index": 2
                }
            }
        ]
    }


@pytest.fixture
def mock_cell_value_response():
    """Return a mock Google Sheets API values response"""
    return {
        "range": "Sheet1!A1",
        "majorDimension": "ROWS",
        "values": [
            ["Hello World"]
        ]
    }


@pytest.fixture
def mock_empty_cell_response():
    """Return a mock Google Sheets API response for empty cell"""
    return {
        "range": "Sheet1!A1",
        "majorDimension": "ROWS",
        "values": []
    }


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestHelperFunctions:
    """Test helper functions"""
    
    def test_hash_service_account(self, valid_service_account_info):
        """Test service account hashing for logging"""
        hashed = _hash_service_account(valid_service_account_info)
        
        assert "test@test-project.iam.gserviceaccount.com" in hashed
        assert "key_hash:" in hashed
        assert len(hashed) > 50  # Has meaningful content
    
    def test_build_service_account_info(self):
        """Test building service account info from components"""
        email = "test@project.iam.gserviceaccount.com"
        key = "-----BEGIN PRIVATE KEY-----\\nMIIEvQ...\\n-----END PRIVATE KEY-----"
        
        result = _build_service_account_info(email, key)
        
        assert result["type"] == "service_account"
        assert result["client_email"] == email
        assert result["token_uri"] == "https://oauth2.googleapis.com/token"
        # Should have normalized newlines
        assert "\\n" not in result["private_key"]
        assert "\n" in result["private_key"]
    
    def test_extract_spreadsheet_id_from_direct_id(self):
        """Test extracting ID when given a direct ID"""
        test_id = "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
        
        success, extracted_id, error = _extract_spreadsheet_id(test_id)
        
        assert success is True
        assert extracted_id == test_id
        assert error is None
    
    def test_extract_spreadsheet_id_from_full_url(self):
        """Test extracting ID from full Google Sheets URL"""
        test_url = "https://docs.google.com/spreadsheets/d/1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M/edit#gid=0"
        
        success, extracted_id, error = _extract_spreadsheet_id(test_url)
        
        assert success is True
        assert extracted_id == "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
        assert error is None
    
    def test_extract_spreadsheet_id_from_short_url(self):
        """Test extracting ID from short Google Sheets URL"""
        test_url = "https://docs.google.com/spreadsheets/d/1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
        
        success, extracted_id, error = _extract_spreadsheet_id(test_url)
        
        assert success is True
        assert extracted_id == "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
        assert error is None
    
    def test_extract_spreadsheet_id_invalid_url(self):
        """Test extracting ID from malformed URL"""
        test_url = "https://docs.google.com/document/d/something"
        
        success, extracted_id, error = _extract_spreadsheet_id(test_url)
        
        assert success is False
        assert extracted_id is None
        assert "Could not extract" in error
    
    def test_extract_spreadsheet_id_invalid_characters(self):
        """Test extracting ID with invalid characters"""
        test_id = "invalid@id#with$special%chars"
        
        success, extracted_id, error = _extract_spreadsheet_id(test_id)
        
        assert success is False
        assert extracted_id is None
        assert "Invalid spreadsheet ID format" in error
    
    def test_extract_spreadsheet_id_empty(self):
        """Test extracting ID from empty string"""
        success, extracted_id, error = _extract_spreadsheet_id("")
        
        assert success is False
        assert extracted_id is None
        assert "empty" in error


# =============================================================================
# TestTypeGoogleSheets Class Tests
# =============================================================================

class TestGoogleSheetsTestType:
    """Test TestTypeGoogleSheets class"""
    
    def test_describe_self(self):
        """Test test metadata structure"""
        metadata = TestTypeGoogleSheets.describe_self()
        
        assert "Google Sheets" in metadata.description
        assert "service_account_email" in metadata.required_fields
        assert "spreadsheet" in metadata.required_fields
        assert "sheet_name" in metadata.optional_fields
        assert "GOOGLE_PRIVATE_KEY" in metadata.confidential_fields
    
    def test_prerequisite_tests(self):
        """Test that DNS prerequisite is included"""
        prerequisites = TestTypeGoogleSheets.prerequisite_tests()
        
        assert len(prerequisites) == 1
        # Check by class name since import paths may differ
        assert prerequisites[0].__name__ == 'TestTypeDnsResolve'
    
    def test_get_configs_with_defaults(self):
        """Test get_configs applies defaults"""
        test = TestTypeGoogleSheets({
            "service_account_email": "test@example.com",
            "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
        })
        configs = test.get_configs()
        
        assert configs["service_account_email"] == "test@example.com"
        assert configs["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS
    
    def test_run_test_missing_service_account_email(self):
        """Test run_test fails without service_account_email"""
        test = TestTypeGoogleSheets({"spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"})
        
        # ConfigurationError from the plugin module, not the test import
        with pytest.raises(Exception) as exc_info:
            test.run_test()
        
        assert "service_account_email is required" in str(exc_info.value)
        assert exc_info.type.__name__ == "ConfigurationError"
    
    def test_run_test_invalid_email_format(self):
        """Test run_test rejects invalid email format"""
        test = TestTypeGoogleSheets({
            "service_account_email": "not-an-email",
            "spreadsheet": "123"
        })
        
        with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': 'fake_key'}):
            result = test.run_test()
        
        assert result.success is False
        assert result.error_type == "config_invalid"
        assert "invalid service_account_email format" in result.error_message.lower()
    
    def test_run_test_missing_spreadsheet_params(self):
        """Test run_test fails when spreadsheet parameter is missing"""
        test = TestTypeGoogleSheets({
            "service_account_email": "test@example.com"
        })
        
        with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': 'fake_key'}):
            # ConfigurationError from the plugin module
            with pytest.raises(Exception) as exc_info:
                test.run_test()
            
            assert "spreadsheet is required" in str(exc_info.value)
            assert exc_info.type.__name__ == "ConfigurationError"
    
    def test_run_test_no_private_key(self):
        """Test run_test fails when GOOGLE_PRIVATE_KEY not set"""
        test = TestTypeGoogleSheets({
            "service_account_email": "test@example.com",
            "spreadsheet": "123"
        })
        
        with patch.dict(os.environ, {}, clear=True):
            result = test.run_test()
        
        assert result.success is False
        assert result.error_type == "config_invalid"
        assert "GOOGLE_PRIVATE_KEY" in result.error_message
    
    def test_run_test_success_with_id(self, mock_spreadsheet_metadata, mock_cell_value_response):
        """Test successful run with direct spreadsheet ID"""
        # Patch at the module where the plugin was loaded
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            # Mock OAuth success
            mock_oauth.return_value = (True, "mock_token", None, None)
            
            # Mock API calls
            def api_side_effect(endpoint, token, timeout):
                if "?fields=" in endpoint:
                    return (True, mock_spreadsheet_metadata, None, None)
                elif "/values/" in endpoint:
                    return (True, mock_cell_value_response, None, None)
                else:
                    return (False, None, "api_error", "Unknown endpoint")
            
            mock_api_call.side_effect = api_side_effect
            
            # Run test
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            # Verify success
            assert result.success is True
            assert "all_tests_passed" in result.extra_detail.get("summary", {})
            assert result.extra_detail["summary"]["all_tests_passed"] is True
            
            # Verify spreadsheet metadata
            assert "spreadsheet_metadata" in result.extra_detail
            assert result.extra_detail["spreadsheet_metadata"]["ok"] is True
            assert result.extra_detail["spreadsheet_metadata"]["spreadsheet_title"] == "Test Spreadsheet"
            assert result.extra_detail["spreadsheet_metadata"]["sheet_count"] == 3
            assert "Sheet1" in result.extra_detail["spreadsheet_metadata"]["sheet_names"]
            
            # Verify cell read
            assert "cell_read_test" in result.extra_detail
            assert result.extra_detail["cell_read_test"]["ok"] is True
            assert result.extra_detail["cell_read_test"]["cell_value"] == "Hello World"
            assert result.extra_detail["cell_read_test"]["cell_is_blank"] is False
    
    def test_run_test_success_with_url(self, mock_spreadsheet_metadata, mock_cell_value_response):
        """Test successful run with spreadsheet URL (unified parameter)"""
        # Patch at the module where the plugin was loaded
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            # Mock OAuth success
            mock_oauth.return_value = (True, "mock_token", None, None)
            
            # Mock API calls
            def api_side_effect(endpoint, token, timeout):
                if "?fields=" in endpoint:
                    return (True, mock_spreadsheet_metadata, None, None)
                elif "/values/" in endpoint:
                    return (True, mock_cell_value_response, None, None)
                else:
                    return (False, None, "api_error", "Unknown endpoint")
            
            mock_api_call.side_effect = api_side_effect
            
            # Run test with URL - same parameter as ID, just different format
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "https://docs.google.com/spreadsheets/d/1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M/edit"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            # Verify success
            assert result.success is True
    
    def test_run_test_success_specific_sheet(self, mock_spreadsheet_metadata, mock_cell_value_response):
        """Test successful run with specific sheet name"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            mock_oauth.return_value = (True, "mock_token", None, None)
            
            def api_side_effect(endpoint, token, timeout):
                if "?fields=" in endpoint:
                    return (True, mock_spreadsheet_metadata, None, None)
                elif "/values/" in endpoint:
                    return (True, mock_cell_value_response, None, None)
                else:
                    return (False, None, "api_error", "Unknown endpoint")
            
            mock_api_call.side_effect = api_side_effect
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M",
                "sheet_name": "Sheet2"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is True
            assert result.extra_detail["cell_read_test"]["sheet_name"] == "Sheet2"
    
    def test_run_test_success_empty_cell(self, mock_spreadsheet_metadata, mock_empty_cell_response):
        """Test successful run when cell A1 is blank"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            mock_oauth.return_value = (True, "mock_token", None, None)
            
            def api_side_effect(endpoint, token, timeout):
                if "?fields=" in endpoint:
                    return (True, mock_spreadsheet_metadata, None, None)
                elif "/values/" in endpoint:
                    return (True, mock_empty_cell_response, None, None)
                else:
                    return (False, None, "api_error", "Unknown endpoint")
            
            mock_api_call.side_effect = api_side_effect
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is True
            assert result.extra_detail["cell_read_test"]["ok"] is True
            assert result.extra_detail["cell_read_test"]["cell_value"] == ""
            assert result.extra_detail["cell_read_test"]["cell_is_blank"] is True
    
    def test_run_test_oauth_failure(self):
        """Test OAuth token acquisition failure"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth:
            mock_oauth.return_value = (False, None, "invalid_credentials", 
                                       "Service account credentials are invalid")
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is False
            assert result.error_type == "invalid_credentials"
            assert "oauth token acquisition failed" in result.error_message.lower()
            assert any("credentials are invalid" in advice.lower() for advice in result.advice)
    
    def test_run_test_api_not_enabled(self):
        """Test Google Sheets API not enabled error"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            mock_oauth.return_value = (True, "mock_token", None, None)
            mock_api_call.return_value = (False, None, "api_not_enabled", 
                                          "Google Sheets API has not been used in project")
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is False
            assert result.error_type == "api_not_enabled"
            assert "google sheets api" in result.error_message.lower()
            assert any("enable" in advice.lower() for advice in result.advice)
            assert any("google cloud console" in advice.lower() for advice in result.advice)
    
    def test_run_test_spreadsheet_not_found(self):
        """Test spreadsheet not found error"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            mock_oauth.return_value = (True, "mock_token", None, None)
            mock_api_call.return_value = (False, None, "resource_not_found", 
                                          "Requested entity was not found")
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "invalid_id"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is False
            assert result.error_type == "resource_not_found"
            assert "spreadsheet not found" in result.error_message.lower() or "not found" in result.error_message.lower()
            assert any("verify" in advice.lower() or "spreadsheet id" in advice.lower() 
                      for advice in result.advice)
    
    def test_run_test_permission_denied(self):
        """Test permission denied error"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            mock_oauth.return_value = (True, "mock_token", None, None)
            mock_api_call.return_value = (False, None, "permission_denied", 
                                          "The caller does not have permission")
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is False
            assert result.error_type == "permission_denied"
            assert any("share" in advice.lower() or "permission" in advice.lower() 
                      for advice in result.advice)
    
    def test_run_test_invalid_sheet_name(self, mock_spreadsheet_metadata):
        """Test invalid sheet name error with helpful advice"""
        with patch.object(gsheets_module, '_get_oauth_token') as mock_oauth, \
             patch.object(gsheets_module, '_test_sheets_api_call') as mock_api_call:
            
            mock_oauth.return_value = (True, "mock_token", None, None)
            mock_api_call.return_value = (True, mock_spreadsheet_metadata, None, None)
            
            test = TestTypeGoogleSheets({
                "service_account_email": "test@example.com",
                "spreadsheet": "1h9qlLloYwKD2cKKNmetp0qdV04TZyDT-wzmQ3OblE-M",
                "sheet_name": "NonExistentSheet"
            })
            
            with patch.dict(os.environ, {'GOOGLE_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----'}):
                result = test.run_test()
            
            assert result.success is False
            assert result.error_type == "invalid_sheet_name"
            assert "NonExistentSheet" in result.error_message
            advice_str = " ".join(result.advice)
            assert "Sheet1" in advice_str
            assert "Sheet2" in advice_str
            assert "Config" in advice_str


# =============================================================================
# Integration Tests
# =============================================================================

class TestGoogleSheetsIntegration:
    """Integration tests requiring actual API (skip by default)"""
    
    @pytest.mark.skip(reason="Requires real Google Sheets credentials")
    def test_real_gsheets_connection(self):
        """Test with real Google Sheets credentials (manual testing only)"""
        # This test should be run manually with real credentials
        # export GOOGLE_PRIVATE_KEY='...'
        # pytest -k test_real_gsheets_connection -s
        
        private_key = os.getenv('GOOGLE_PRIVATE_KEY')
        service_account_email = os.getenv('GOOGLE_SERVICE_ACCOUNT_EMAIL', 'test@example.com')
        spreadsheet_id = os.getenv('GOOGLE_SPREADSHEET_ID', '1h9qlLlo...')
        
        if not private_key:
            pytest.skip("GOOGLE_PRIVATE_KEY not set")
        
        test = TestTypeGoogleSheets({
            "service_account_email": service_account_email,
            "spreadsheet": spreadsheet_id
        })
        
        result = test.run_test()
        
        # Print result for manual verification
        print("\n=== Google Sheets Test Result ===")
        print(f"Success: {result.success}")
        print(f"Error Type: {result.error_type}")
        print(f"Error Message: {result.error_message}")
        print(f"Advice: {result.advice}")
        print(f"Extra Detail: {result.extra_detail}")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

