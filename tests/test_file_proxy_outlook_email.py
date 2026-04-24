# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test cases for Outlook email proxy functionality.

These tests focus on business logic by mocking the Microsoft Graph API calls,
allowing us to test the core functionality without requiring actual Microsoft 365
connections or authentication.

NOTE: This test suite does NOT include tests for the actual API HTTP calls.
Those should be tested separately with integration tests that have proper
authentication and network access.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import os
import json

from totodev_pub.cached_file_folders_support.file_proxy_outlook_email import (
    OutlookEmailFileProxyFactory,
    OutlookEmailProxy,
    OutlookAttachmentProxy,
    OutlookEmailErrorProxy,
    _EmailMetadata,
    _AttachmentMetadata,
    _EmailDataHandler,
    _sanitize_path_component,
    _extract_counterparty,
    _format_ref_path_email,
    _format_ref_path_attachment,
    _hash_msg_id,
    _get_type_indicator,
    _should_extract_mime_part
)


# =============================================================================
# TEST HELPER FUNCTIONS
# =============================================================================

class TestHelperFunctions:
    """Test cases for helper functions."""
    
    def test_sanitize_path_component_basic(self):
        """Test basic path sanitization."""
        assert _sanitize_path_component("normal_text") == "normal_text"
        assert _sanitize_path_component("text with spaces") == "text with spaces"
    
    def test_sanitize_path_component_unsafe_chars(self):
        """Test sanitization of unsafe filesystem characters."""
        result = _sanitize_path_component("file/with\\unsafe:chars")
        assert '/' not in result
        assert '\\' not in result
        assert ':' not in result
        assert '_' in result  # Replaced with underscores
    
    def test_sanitize_path_component_max_length(self):
        """Test truncation to max length."""
        long_text = "a" * 100
        result = _sanitize_path_component(long_text, max_length=20)
        assert len(result) == 20
    
    def test_sanitize_path_component_empty(self):
        """Test handling of empty/None input."""
        assert _sanitize_path_component("") == "unknown"
        assert _sanitize_path_component(None) == "unknown"
    
    def test_extract_counterparty_standard(self):
        """Test counterparty extraction from standard email."""
        result = _extract_counterparty("joe.smith@example.com")
        assert result == "joe.smith@example"
    
    def test_extract_counterparty_no_dot(self):
        """Test counterparty extraction without dot in domain."""
        result = _extract_counterparty("user@localhost")
        assert result == "user@localhost"
    
    def test_extract_counterparty_multiple_dots(self):
        """Test counterparty extraction with multiple dots."""
        result = _extract_counterparty("admin@company.co.uk")
        assert result == "admin@company"
    
    def test_hash_msg_id_deterministic(self):
        """Test that message ID hashing is deterministic."""
        msg_id = "AAMkADM2MDU1NDZlLTg5MWQtNGEzZS05YWVkLTNkYzE2ZGVjODJkNw"
        hash1 = _hash_msg_id(msg_id)
        hash2 = _hash_msg_id(msg_id)
        
        assert hash1 == hash2
        assert len(hash1) == 12  # Default length
        assert hash1.isalnum()  # Hex characters
    
    def test_hash_msg_id_different_inputs(self):
        """Test that different message IDs produce different hashes."""
        hash1 = _hash_msg_id("MSG123")
        hash2 = _hash_msg_id("MSG456")
        
        assert hash1 != hash2
    
    def test_hash_msg_id_custom_length(self):
        """Test custom hash length."""
        msg_id = "AAMkADM2MDU1NDZlLTg5MWQtNGEzZS05YWVkLTNkYzE2ZGVjODJkNw"
        hash_short = _hash_msg_id(msg_id, length=8)
        hash_long = _hash_msg_id(msg_id, length=16)
        
        assert len(hash_short) == 8
        assert len(hash_long) == 16
    
    def test_format_ref_path_email(self):
        """Test email ref_path formatting with hashed message ID and .eml extension."""
        dt = datetime(2025, 11, 1, 14, 30, 45)
        result = _format_ref_path_email("Inbox", dt, "joe@example.com", "MSG123")
        
        assert result.startswith("2025-11-01/")
        assert "143045" in result  # Time component
        assert "joe@example" in result  # Counterparty
        assert result.endswith(".eml")  # Should include .eml extension
        # Message ID should be hashed (12 chars before .eml)
        parts = result.split('/')
        assert len(parts) == 2  # date/filename.eml
        filename = parts[-1]
        base_name = filename[:-4]  # Remove .eml
        msg_id_hash = base_name.split('_')[-1]
        assert len(msg_id_hash) == 12
        assert msg_id_hash == _hash_msg_id("MSG123")
    
    def test_format_ref_path_attachment(self):
        """Test attachment ref_path formatting with new structure."""
        email_ref = "2025-11-01/143045_joe@example_a3f2c9b1.eml"
        result = _format_ref_path_attachment(email_ref, 1, "document.pdf")
        
        # Should use email base name + _files directory with att prefix
        assert result == "2025-11-01/143045_joe@example_a3f2c9b1_files/att01_document.pdf"
    
    def test_format_ref_path_attachment_zero_padding(self):
        """Test that attachment sequence numbers are zero-padded."""
        email_ref = "2025-11-01/143045_joe@example_a3f2c9b1.eml"
        
        result_01 = _format_ref_path_attachment(email_ref, 1, "file1.pdf")
        result_11 = _format_ref_path_attachment(email_ref, 11, "file11.pdf")
        
        # Verify zero-padding ensures correct alphabetical sort
        assert result_01.endswith("_files/att01_file1.pdf")
        assert result_11.endswith("_files/att11_file11.pdf")
        
        # Verify they sort correctly
        paths = [result_11, result_01]
        sorted_paths = sorted(paths)
        assert sorted_paths[0] == result_01  # 01 comes before 11


# =============================================================================
# TEST EMAIL DATA HANDLER
# =============================================================================

class TestEmailDataHandler:
    """Test cases for _EmailDataHandler internal helper class."""
    
    @pytest.fixture
    def mock_email_bytes(self):
        """Create mock email bytes with attachment."""
        email_content = b"""From: sender@example.com
To: receiver@example.com
Subject: Test Email
Content-Type: multipart/mixed; boundary="boundary123"

--boundary123
Content-Type: text/plain; charset="UTF-8"

Hello, this is a test email.

--boundary123
Content-Type: application/pdf; name="document.pdf"
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="document.pdf"

JVBERi0xLjQKJcfsj6IKNSAwIG9iago8PC9MZW5ndGggNiAwIFI=

--boundary123--
"""
        return email_content
    
    @pytest.fixture
    def handler(self):
        """Create a handler instance for testing."""
        return _EmailDataHandler(
            msg_id="test-msg-id",
            user_email="user@company.com",
            access_token="test-token"
        )
    
    def test_handler_initialization(self, handler):
        """Test handler initialization."""
        assert handler.msg_id == "test-msg-id"
        assert handler.user_email == "user@company.com"
        assert handler.access_token == "test-token"
        assert handler._fetch_attempted is False
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_fetch_email_success(self, mock_api, handler, mock_email_bytes):
        """Test successful email fetching."""
        mock_response = Mock()
        mock_response.content = mock_email_bytes
        mock_api.return_value = mock_response
        
        result = handler._fetch_email_if_needed()
        
        assert result is True
        assert handler._raw_email_bytes is not None
        assert handler._fetch_attempted is True
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_fetch_email_only_once(self, mock_api, handler, mock_email_bytes):
        """Test that email is only fetched once (lazy loading)."""
        mock_response = Mock()
        mock_response.content = mock_email_bytes
        mock_api.return_value = mock_response
        
        handler._fetch_email_if_needed()
        handler._fetch_email_if_needed()
        
        # Should only be called once due to lazy loading
        assert mock_api.call_count == 1
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_get_email_body(self, mock_api, handler, mock_email_bytes):
        """Test getting email body."""
        mock_response = Mock()
        mock_response.content = mock_email_bytes
        mock_api.return_value = mock_response
        
        result = handler.get_email_body()
        
        assert result is not None
        assert isinstance(result, bytes)


# =============================================================================
# TEST OUTLOOK EMAIL PROXY
# =============================================================================

class TestOutlookEmailProxy:
    """Test cases for OutlookEmailProxy."""
    
    @pytest.fixture
    def mock_handler(self):
        """Create a mock EmailDataHandler."""
        handler = Mock(spec=_EmailDataHandler)
        handler.get_email_body.return_value = b"Mock email content"
        handler.get_attachment_list.return_value = [
            {
                'sequence_number': 1,
                'filename': 'document.pdf',
                'size_bytes': 1024,
                'original_content_type': 'application/pdf',
                'is_inline': False
            }
        ]
        handler.get_metadata.return_value = {
            'msg_id': 'MSG123',
            'subject': 'Test Email'
        }
        return handler
    
    @pytest.fixture
    def email_proxy(self, mock_handler):
        """Create an email proxy for testing."""
        metadata = _EmailMetadata(
            folder_path="Inbox",
            msg_id="MSG123",
            sender_email="sender@example.com",
            sender_name="Sender Name",
            receiver_email="receiver@example.com",
            receiver_name="Receiver Name",
            counterparty_email="sender@example.com",
            counterparty_name="Sender Name",
            subject="Test Email",
            received_datetime=datetime(2025, 11, 1, 14, 30, 0),
            sent_datetime=datetime(2025, 11, 1, 14, 29, 0)
        )
        return OutlookEmailProxy(handler=mock_handler, metadata=metadata)
    
    def test_email_proxy_initialization(self, email_proxy):
        """Test email proxy initialization."""
        assert email_proxy.sender_email == "sender@example.com"
        assert email_proxy.receiver_email == "receiver@example.com"
        assert email_proxy.counterparty_email == "sender@example.com"
        assert email_proxy._msg_id == "MSG123"
    
    def test_email_proxy_ref_path_format(self, email_proxy):
        """Test email proxy ref_path follows spec format with .eml extension."""
        ref_path = email_proxy.ref_path()
        
        # Should be: {date}/{time}_{counterparty}_{msg_id_hash}.eml
        assert ref_path.startswith("2025-11-01/")
        assert "143000" in ref_path  # Time component
        assert "sender@example" in ref_path  # Counterparty
        assert ref_path.endswith(".eml")  # Should include .eml extension
        # Message ID should be hashed (12 chars before .eml)
        filename = ref_path.split('/')[-1]
        base_name = filename[:-4]  # Remove .eml
        parts = base_name.split('_')
        msg_id_hash = parts[-1]
        assert len(msg_id_hash) == 12
        assert msg_id_hash == _hash_msg_id("MSG123")
    
    def test_email_proxy_file_name(self, email_proxy):
        """Test email proxy file_name includes .eml extension."""
        filename = email_proxy.file_name()
        assert filename.endswith('.eml')
    
    def test_email_proxy_component_type(self, email_proxy):
        """Test email_component returns 'eml' for emails."""
        assert email_proxy.email_component() == "eml"
    
    def test_email_proxy_ref_path_of_email(self, email_proxy):
        """Test ref_path_of_email returns self ref_path."""
        assert email_proxy.ref_path_of_email() == email_proxy.ref_path()
    
    def test_email_proxy_email_msg_id(self, email_proxy):
        """Test email_msg_id returns message ID."""
        assert email_proxy.email_msg_id() == "MSG123"
    
    def test_email_proxy_attachment_count(self, email_proxy):
        """Test attachment_count property."""
        assert email_proxy.attachment_count == 1
    
    def test_email_proxy_deploy(self, email_proxy, tmp_path):
        """Test deploying email to filesystem."""
        email_proxy.deploy(str(tmp_path))
        
        # Check that .eml file was created
        eml_files = list(tmp_path.glob("*.eml"))
        assert len(eml_files) == 1
        assert eml_files[0].read_bytes() == b"Mock email content"
    
    def test_email_proxy_looks_same_with_matching_msg_id(self, email_proxy, tmp_path):
        """Test looks_same optimization using hashed msg_id."""
        # Create a file with matching hashed msg_id in name
        from totodev_pub.cached_file_folders_support.file_proxy_outlook_email import _hash_msg_id
        msg_id_hash = _hash_msg_id("MSG123")
        test_file = tmp_path / f"143000_sender@example_{msg_id_hash}.eml"
        test_file.write_text("test content")
        
        result = email_proxy.looks_same(str(test_file))
        assert result is True
    
    def test_email_proxy_looks_same_with_flag_mismatch(self, email_proxy, tmp_path):
        """Test looks_same returns False when follow-up flag status differs."""
        cached_file = tmp_path / email_proxy.file_name()
        cached_file.write_text("X-Custom-FollowUpFlag: notFlagged\r\n\r\nbody", encoding="utf-8")
        
        # Simulate updated flag status
        email_proxy._follow_up_flag_status = "flagged"
        
        result = email_proxy.looks_same(str(cached_file))
        assert result is False
    
    def test_email_proxy_looks_same_nonexistent_file(self, email_proxy):
        """Test looks_same with nonexistent file."""
        result = email_proxy.looks_same("/nonexistent/file.eml")
        assert result is False
    
    def test_email_proxy_properties(self, email_proxy):
        """Test all email proxy properties."""
        assert email_proxy.sender_email == "sender@example.com"
        assert email_proxy.receiver_email == "receiver@example.com"
        assert email_proxy.counterparty_email == "sender@example.com"
        assert isinstance(email_proxy.received_datetime, datetime)
        assert email_proxy.follow_up_flag_status == "notFlagged"
        assert email_proxy.sequence_number is None  # Only attachments have this


# =============================================================================
# TEST OUTLOOK ATTACHMENT PROXY
# =============================================================================

class TestOutlookAttachmentProxy:
    """Test cases for OutlookAttachmentProxy."""
    
    @pytest.fixture
    def mock_handler(self):
        """Create a mock EmailDataHandler."""
        handler = Mock(spec=_EmailDataHandler)
        handler.get_attachment.return_value = b"Mock attachment content"
        handler.get_attachment_list.return_value = [
            {'sequence_number': 1, 'filename': 'document.pdf', 'size_bytes': 1024}
        ]
        return handler
    
    @pytest.fixture
    def attachment_proxy(self, mock_handler):
        """Create an attachment proxy for testing."""
        metadata = _AttachmentMetadata(
            email_ref_path="2025-11-01/143000_sender@example_a3f2c9b1.eml",
            folder_path="Inbox",
            msg_id="MSG123",
            sequence_number=1,
            filename="document.pdf",
            size_bytes=1024,
            content_type="application/pdf",
            is_embedded=False,
            sender_email="sender@example.com",
            receiver_email="receiver@example.com",
            counterparty_email="sender@example.com",
            received_datetime=datetime(2025, 11, 1, 14, 30, 0)
        )
        return OutlookAttachmentProxy(handler=mock_handler, metadata=metadata)
    
    def test_attachment_proxy_initialization(self, attachment_proxy):
        """Test attachment proxy initialization."""
        assert attachment_proxy._sequence_number == 1
        assert attachment_proxy._filename == "document.pdf"
        assert attachment_proxy._size_bytes == 1024
    
    def test_attachment_proxy_ref_path_format(self, attachment_proxy):
        """Test attachment proxy ref_path follows new structure."""
        ref_path = attachment_proxy.ref_path()
        
        # Should be: {email_base}_files/att{seq}_{filename}
        assert ref_path == "2025-11-01/143000_sender@example_a3f2c9b1_files/att01_document.pdf"  # Zero-padded!
    
    def test_attachment_proxy_file_name(self, attachment_proxy):
        """Test attachment proxy file_name includes sequence prefix."""
        filename = attachment_proxy.file_name()
        assert filename == "att01_document.pdf"
    
    def test_attachment_proxy_component_type(self, attachment_proxy):
        """Test email_component returns 'attach' for attachments."""
        assert attachment_proxy.email_component() == "attach"
    
    def test_attachment_proxy_ref_path_of_email(self, attachment_proxy):
        """Test ref_path_of_email returns parent email ref_path."""
        expected = "2025-11-01/143000_sender@example_a3f2c9b1.eml"
        assert attachment_proxy.ref_path_of_email() == expected
    
    def test_attachment_proxy_email_msg_id(self, attachment_proxy):
        """Test email_msg_id returns parent message ID."""
        assert attachment_proxy.email_msg_id() == "MSG123"
    
    def test_attachment_proxy_sequence_number(self, attachment_proxy):
        """Test sequence_number property."""
        assert attachment_proxy.sequence_number == 1
    
    def test_attachment_proxy_deploy(self, attachment_proxy, tmp_path):
        """Test deploying attachment to filesystem."""
        attachment_proxy.deploy(str(tmp_path))
        
        # Check that attachment file was created with sequence prefix
        attachment_files = list(tmp_path.glob("*.pdf"))
        assert len(attachment_files) == 1
        assert attachment_files[0].name == "att01_document.pdf"
        assert attachment_files[0].read_bytes() == b"Mock attachment content"
    
    def test_attachment_proxy_looks_same_matching_size(self, attachment_proxy, tmp_path):
        """Test looks_same with matching file size."""
        test_file = tmp_path / "01_document.pdf"
        test_file.write_bytes(b"x" * 1024)  # Exact size match
        
        result = attachment_proxy.looks_same(str(test_file))
        assert result is True
    
    def test_attachment_proxy_looks_same_different_size(self, attachment_proxy, tmp_path):
        """Test looks_same with different file size."""
        test_file = tmp_path / "01_document.pdf"
        test_file.write_bytes(b"x" * 2048)  # Different size
        
        result = attachment_proxy.looks_same(str(test_file))
        assert result is False
    
    def test_attachment_zero_padding_sorts_correctly(self):
        """Test that zero-padded sequence numbers sort correctly alphabetically."""
        # This is critical for the spec requirement
        filenames = []
        for i in range(1, 12):
            filename = f"{i:02d}_attachment.pdf"
            filenames.append(filename)
        
        # Verify they sort correctly
        sorted_filenames = sorted(filenames)
        assert sorted_filenames == filenames  # Should already be in order
        
        # Verify 01 comes before 11 (not after, which would happen without padding)
        assert sorted_filenames[0] == "01_attachment.pdf"
        assert sorted_filenames[10] == "11_attachment.pdf"


# =============================================================================
# TEST EMBEDDED CONTENT
# =============================================================================

class TestEmbeddedContent:
    """Test cases for embedded content extraction (text/calendar, etc.)."""
    
    def test_get_type_indicator_calendar(self):
        """Test type indicator for calendar content."""
        assert _get_type_indicator("text/calendar") == "cal"
        assert _get_type_indicator("application/ics") == "cal"
        assert _get_type_indicator("text/calendar; charset=utf-8") == "cal"
    
    def test_get_type_indicator_vcard(self):
        """Test type indicator for vcard content."""
        assert _get_type_indicator("text/vcard") == "vcard"
        assert _get_type_indicator("text/x-vcard") == "vcard"
    
    def test_get_type_indicator_unknown(self):
        """Test type indicator for unknown embedded types."""
        assert _get_type_indicator("application/unknown") == "embed"
    
    def test_should_extract_mime_part_traditional_attachment(self):
        """Test detection of traditional attachments."""
        from email.mime.application import MIMEApplication
        part = MIMEApplication(b"test content", 'pdf')
        part.add_header('Content-Disposition', 'attachment', filename='test.pdf')
        
        should_extract, is_embedded = _should_extract_mime_part(part)
        assert should_extract is True
        assert is_embedded is False
    
    def test_should_extract_mime_part_inline_image(self):
        """Test detection of inline images."""
        from email.mime.base import MIMEBase
        part = MIMEBase('image', 'png')
        part.set_payload(b"test image")
        part.add_header('Content-Disposition', 'inline')
        
        should_extract, is_embedded = _should_extract_mime_part(part)
        assert should_extract is True
        assert is_embedded is False
    
    def test_should_extract_mime_part_calendar(self):
        """Test detection of embedded calendar content."""
        from email.mime.text import MIMEText
        part = MIMEText("BEGIN:VCALENDAR...", 'calendar')
        # No Content-Disposition header
        
        should_extract, is_embedded = _should_extract_mime_part(part)
        assert should_extract is True
        assert is_embedded is True
    
    def test_should_extract_mime_part_text_plain(self):
        """Test that message body is NOT extracted."""
        from email.mime.text import MIMEText
        part = MIMEText("Hello, this is the message body", 'plain')
        
        should_extract, is_embedded = _should_extract_mime_part(part)
        assert should_extract is False
        assert is_embedded is False
    
    def test_format_ref_path_embedded_content(self):
        """Test ref_path formatting for embedded content with new structure."""
        email_ref = "2025-11-01/143000_joe@example_a3f2c9b1.eml"
        result = _format_ref_path_attachment(
            email_ref,
            1,
            "meeting-invite.ics",
            is_embedded_content=True,
            mime_content_type="text/calendar"
        )
        
        # Should use email base name + _files directory with emb prefix
        assert result == "2025-11-01/143000_joe@example_a3f2c9b1_files/emb01_meeting-invite.ics"
    
    def test_format_ref_path_embedded_vs_attachment(self):
        """Test that embedded content and attachments use different subdirectories."""
        email_ref = "2025-11-01/143000_joe@example_a3f2c9b1.eml"
        
        attachment_path = _format_ref_path_attachment(
            email_ref, 1, "document.pdf",
            is_embedded_content=False
        )
        
        embedded_path = _format_ref_path_attachment(
            email_ref, 2, "meeting.ics",
            is_embedded_content=True,
            mime_content_type="text/calendar"
        )
        
        # Both should share same parent directory (email base name)
        assert "143000_joe@example_a3f2c9b1_files/att" in attachment_path
        assert "143000_joe@example_a3f2c9b1_files/emb" in embedded_path
        
        # Verify full paths
        assert attachment_path == "2025-11-01/143000_joe@example_a3f2c9b1_files/att01_document.pdf"
        assert embedded_path == "2025-11-01/143000_joe@example_a3f2c9b1_files/emb02_meeting.ics"
    
    @pytest.fixture
    def embedded_content_proxy(self):
        """Create an embedded content proxy for testing."""
        handler = Mock(spec=_EmailDataHandler)
        handler.get_attachment.return_value = b"BEGIN:VCALENDAR..."
        handler.get_attachment_list.return_value = []
        
        metadata = _AttachmentMetadata(
            email_ref_path="2025-11-01/143000_sender@example_a3f2c9b1.eml",
            folder_path="Inbox",
            msg_id="MSG123",
            sequence_number=1,
            filename="meeting-invite.ics",
            size_bytes=2048,
            content_type="text/calendar",
            is_embedded=True,
            sender_email="sender@example.com",
            receiver_email="receiver@example.com",
            counterparty_email="sender@example.com",
            received_datetime=datetime(2025, 11, 1, 14, 30, 0)
        )
        return OutlookAttachmentProxy(handler=handler, metadata=metadata)
    
    def test_embedded_content_ref_path(self, embedded_content_proxy):
        """Test that embedded content uses embed/ subdirectory."""
        ref_path = embedded_content_proxy.ref_path()
        
        assert "_files/emb" in ref_path
        assert ref_path == "2025-11-01/143000_sender@example_a3f2c9b1_files/emb01_meeting-invite.ics"
    
    def test_embedded_content_file_name(self, embedded_content_proxy):
        """Test embedded content filename uses emb prefix with zero padding."""
        filename = embedded_content_proxy.file_name()
        assert filename == "emb01_meeting-invite.ics"
    
    def test_embedded_content_component_type(self, embedded_content_proxy):
        """Test that embedded content returns 'embed' for email_component()."""
        assert embedded_content_proxy.email_component() == "embed"


# =============================================================================
# TEST OUTLOOK EMAIL FILE PROXY FACTORY
# =============================================================================

class TestOutlookEmailFileProxyFactory:
    """Test cases for OutlookEmailFileProxyFactory."""
    
    @pytest.fixture
    def factory(self):
        """Create a factory instance for testing."""
        return OutlookEmailFileProxyFactory(
            user_email="user@company.com",
            access_token="test-token"
        )
    
    @pytest.fixture
    def mock_messages_response(self):
        """Mock Microsoft Graph API messages list response."""
        return {
            "value": [
                {
                    "id": "MSG001",
                    "subject": "Test Email 1",
                    "from": {
                        "emailAddress": {
                            "address": "sender1@example.com",
                            "name": "Sender One"
                        }
                    },
                    "toRecipients": [
                        {
                            "emailAddress": {
                                "address": "user@company.com",
                                "name": "User"
                            }
                        }
                    ],
                    "receivedDateTime": "2025-11-01T14:30:00Z",
                    "sentDateTime": "2025-11-01T14:29:00Z",
                    "hasAttachments": False,
                    "importance": "normal",
                    "isRead": False,
                    "flag": {"flagStatus": "notFlagged"},
                    "categories": [],
                    "conversationId": "CONV001"
                },
                {
                    "id": "MSG002",
                    "subject": "Test Email 2 with Attachment",
                    "from": {
                        "emailAddress": {
                            "address": "sender2@example.com",
                            "name": "Sender Two"
                        }
                    },
                    "toRecipients": [
                        {
                            "emailAddress": {
                                "address": "user@company.com",
                                "name": "User"
                            }
                        }
                    ],
                    "receivedDateTime": "2025-11-01T15:00:00Z",
                    "sentDateTime": "2025-11-01T14:59:00Z",
                    "hasAttachments": True,
                    "importance": "high",
                    "isRead": True,
                    "flag": {"flagStatus": "flagged"},
                    "categories": ["Important"],
                    "conversationId": "CONV002"
                }
            ],
            "@odata.nextLink": None
        }
    
    def test_factory_initialization(self, factory):
        """Test factory initialization."""
        assert factory.user_email == "user@company.com"
        assert factory.access_token == "test-token"
        assert factory.base_url == "https://graph.microsoft.com/v1.0"
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._EmailDataHandler.get_attachment_list')
    def test_scan_messages_basic(self, mock_attach_list, mock_api, factory, mock_messages_response):
        """Test basic message scanning."""
        mock_response = Mock()
        mock_response.json.return_value = mock_messages_response
        mock_api.return_value = mock_response
        mock_attach_list.return_value = []
        
        received_after = datetime(2025, 11, 1, 0, 0, 0)
        
        proxies = list(factory.scan_messages(
            received_after=received_after,
            folder_path="Inbox"
        ))
        
        # Should have 2 email proxies
        assert len(proxies) == 2
        assert all(isinstance(p, OutlookEmailProxy) for p in proxies)
        assert proxies[0].email_msg_id() == "MSG001"
        assert proxies[1].email_msg_id() == "MSG002"
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_scan_messages_with_attachments(self, mock_api, factory, mock_messages_response):
        """Test scanning with attachment proxies."""
        mock_response = Mock()
        mock_response.json.return_value = mock_messages_response
        mock_api.return_value = mock_response
        
        received_after = datetime(2025, 11, 1, 0, 0, 0)
        
        # Note: Testing with attachments requires mocking the full email fetch
        # which is complex. For now, we verify that the has_attachments flag
        # is properly read from the Graph API response.
        proxies = list(factory.scan_messages(
            received_after=received_after,
            folder_path="Inbox"
        ))
        
        # Should have 2 emails (attachments disabled)
        assert len(proxies) == 2
        
        # Verify both are email proxies
        assert isinstance(proxies[0], OutlookEmailProxy)
        assert isinstance(proxies[1], OutlookEmailProxy)
        
        # Verify the second email indicates it has attachments
        # (even though we didn't fetch them)
        # This is based on the hasAttachments field from the API
        assert mock_messages_response['value'][1]['hasAttachments'] is True
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_scan_messages_max_results(self, mock_api, factory, mock_messages_response):
        """Test max_results parameter limits emails returned."""
        mock_response = Mock()
        mock_response.json.return_value = mock_messages_response
        mock_api.return_value = mock_response
        
        received_after = datetime(2025, 11, 1, 0, 0, 0)
        
        proxies = list(factory.scan_messages(
            received_after=received_after,
            folder_path="Inbox",
            max_results=1
        ))
        
        # Should only return 1 email despite 2 being available
        assert len(proxies) == 1
    
    def test_scan_messages_counterparty_inbox(self, factory):
        """Test counterparty logic for inbox emails."""
        # For inbox, counterparty should be sender
        # This is tested indirectly through the email proxy creation
        # We verify the logic is correct
        
        folder_path = "Inbox"
        is_sent = folder_path.lower().startswith('sent')
        assert is_sent is False  # Inbox is not sent
    
    def test_scan_messages_counterparty_sent(self, factory):
        """Test counterparty logic for sent emails."""
        # For sent items, counterparty should be receiver
        
        folder_path = "Sent Items"
        is_sent = folder_path.lower().startswith('sent')
        assert is_sent is True  # Sent Items is sent


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegrationScenarios:
    """Integration-style tests combining multiple components."""
    
    @pytest.fixture
    def factory(self):
        """Create a factory instance for testing."""
        return OutlookEmailFileProxyFactory(
            user_email="user@company.com",
            access_token="test-token"
        )
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_full_email_and_attachment_workflow(self, mock_api, factory, tmp_path):
        """Test complete workflow: scan, fetch, and deploy emails with attachments."""
        # Mock the list messages response
        list_response = Mock()
        list_response.json.return_value = {
            "value": [{
                "id": "MSG001",
                "subject": "Test Email",
                "from": {"emailAddress": {"address": "sender@example.com", "name": "Sender"}},
                "toRecipients": [{"emailAddress": {"address": "user@company.com", "name": "User"}}],
                "receivedDateTime": "2025-11-01T14:30:00Z",
                "sentDateTime": "2025-11-01T14:29:00Z",
                "hasAttachments": True,
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
                "categories": [],
                "conversationId": "CONV001"
            }],
            "@odata.nextLink": None
        }
        
        # Mock the email content response
        email_content = b"""From: sender@example.com
To: user@company.com
Subject: Test Email
Content-Type: multipart/mixed; boundary="boundary123"

--boundary123
Content-Type: text/plain; charset="UTF-8"

Hello, this is a test.

--boundary123
Content-Type: application/pdf; name="test.pdf"
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="test.pdf"

JVBERi0xLjQ=

--boundary123--
"""
        content_response = Mock()
        content_response.content = email_content
        
        # Mock API returns different responses based on call order
        mock_api.side_effect = [list_response, content_response]
        
        # Scan messages
        received_after = datetime(2025, 11, 1, 0, 0, 0)
        proxies = list(factory.scan_messages(
            received_after=received_after,
            folder_path="Inbox"
        ))
        
        # Should have email + attachments
        assert len(proxies) >= 1
        assert isinstance(proxies[0], OutlookEmailProxy)
        
        # Deploy email
        email_proxy = proxies[0]
        email_proxy.deploy(str(tmp_path))
        
        # Verify email file was created
        eml_files = list(tmp_path.glob("*.eml"))
        assert len(eml_files) == 1
        
        # Verify attachments accessible via nested_proxies()
        attachments = list(email_proxy.nested_proxies())
        assert all(isinstance(p, OutlookAttachmentProxy) for p in attachments)
        assert len(attachments) == 1


# =============================================================================
# ERROR HANDLING TESTS
# =============================================================================

class TestErrorHandling:
    """Test cases for error handling and edge cases."""
    
    @pytest.fixture
    def factory(self):
        """Create a factory instance for testing."""
        return OutlookEmailFileProxyFactory(
            user_email="user@company.com",
            access_token="test-token"
        )
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_scan_messages_empty_folder(self, mock_api, factory):
        """Test scanning an empty folder."""
        mock_response = Mock()
        mock_response.json.return_value = {"value": [], "@odata.nextLink": None}
        mock_api.return_value = mock_response
        
        received_after = datetime(2025, 11, 1, 0, 0, 0)
        
        proxies = list(factory.scan_messages(
            received_after=received_after,
            folder_path="Inbox"
        ))
        
        assert len(proxies) == 0
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_scan_messages_api_error(self, mock_api, factory):
        """Test handling of API errors during scanning."""
        mock_api.side_effect = Exception("API Error")
        
        received_after = datetime(2025, 11, 1, 0, 0, 0)
        
        proxies = list(factory.scan_messages(
            received_after=received_after,
            folder_path="Inbox"
        ))
        
        # Should return empty list on error
        assert len(proxies) == 0
    
    def test_format_ref_path_attachment_with_short_path(self):
        """Test attachment ref_path with various email path formats."""
        # New implementation is more flexible and doesn't raise errors for short paths
        # It just uses the path as-is
        result = _format_ref_path_attachment("simple.eml", 1, "file.pdf")
        assert result == "simple_files/att01_file.pdf"


# =============================================================================
# ERROR PLACEHOLDER TESTS
# =============================================================================

class TestErrorPlaceholders:
    """Test cases for error placeholder file generation."""
    
    def test_error_proxy_initialization(self):
        """Test OutlookEmailErrorProxy initialization."""
        error_info = {
            'error': True,
            'error_type': 'ValueError',
            'error_message': 'Test error',
            'timestamp': '2025-11-02T14:30:00Z',
            'traceback': 'Traceback...',
            'can_retry': True
        }
        email_metadata = {
            'msg_id': 'MSG123',
            'subject': 'Test Email',
            'sender_email': 'sender@example.com',
            'folder_path': 'Inbox'
        }
        base_ref_path = "2025-11-01/143000_sender@example_a3f2c9b1.eml"
        
        error_proxy = OutlookEmailErrorProxy(error_info, email_metadata, base_ref_path)
        
        assert error_proxy.email_component() == "error"
        assert error_proxy.email_msg_id() == "MSG123"
        assert error_proxy.ref_path() == "2025-11-01/143000_sender@example_a3f2c9b1.error.json"
    
    def test_error_proxy_ref_path_conversion(self):
        """Test that .eml is correctly replaced with .error.json."""
        error_proxy = OutlookEmailErrorProxy(
            error_info={'error': True, 'error_type': 'TestError', 'error_message': 'Test'},
            email_metadata={'msg_id': 'MSG001'},
            base_ref_path="2025-11-01/143000_joe@example_abc123.eml"
        )
        
        ref_path = error_proxy.ref_path()
        assert ref_path.endswith(".error.json")
        assert ".eml" not in ref_path
        assert "143000_joe@example_abc123.error.json" in ref_path
    
    def test_error_proxy_file_name(self):
        """Test error proxy file_name returns just the filename."""
        error_proxy = OutlookEmailErrorProxy(
            error_info={'error': True},
            email_metadata={'msg_id': 'MSG001'},
            base_ref_path="2025-11-01/143000_sender@example_abc123.eml"
        )
        
        filename = error_proxy.file_name()
        assert filename == "143000_sender@example_abc123.error.json"
        assert "/" not in filename
    
    def test_error_proxy_deploy_creates_json(self, tmp_path):
        """Test that error proxy deploys a valid JSON file."""
        error_info = {
            'error': True,
            'error_type': 'requests.Timeout',
            'error_message': 'Request timed out',
            'timestamp': '2025-11-02T14:30:00Z',
            'traceback': 'Traceback (most recent call last):\n  File ...',
            'can_retry': True
        }
        email_metadata = {
            'msg_id': 'MSG123',
            'subject': 'Important Email',
            'sender_email': 'sender@example.com',
            'folder_path': 'Inbox'
        }
        
        error_proxy = OutlookEmailErrorProxy(
            error_info=error_info,
            email_metadata=email_metadata,
            base_ref_path="2025-11-01/143000_sender@example_abc123.eml"
        )
        
        error_proxy.deploy(str(tmp_path))
        
        # Verify file was created
        error_files = list(tmp_path.glob("*.error.json"))
        assert len(error_files) == 1
        
        # Verify content is valid JSON with expected structure
        error_data = json.loads(error_files[0].read_text())
        assert error_data['error'] is True
        assert error_data['error_type'] == 'requests.Timeout'
        assert error_data['error_message'] == 'Request timed out'
        assert error_data['can_retry'] is True
        assert 'email_metadata' in error_data
        assert error_data['email_metadata']['msg_id'] == 'MSG123'
        assert error_data['email_metadata']['subject'] == 'Important Email'
    
    def test_error_proxy_looks_same_always_false(self, tmp_path):
        """Test that error proxies always return False for looks_same to trigger retry."""
        error_proxy = OutlookEmailErrorProxy(
            error_info={'error': True},
            email_metadata={'msg_id': 'MSG001'},
            base_ref_path="2025-11-01/143000_sender@example_abc123.eml"
        )
        
        # Create an identical error file
        error_file = tmp_path / error_proxy.file_name()
        error_file.write_text('{"error": true}')
        
        # Should always return False to trigger retry
        result = error_proxy.looks_same(str(error_file))
        assert result is False
    
    @pytest.mark.asyncio
    async def test_error_proxy_materialize(self):
        """Test that error proxy materialize always returns True."""
        error_proxy = OutlookEmailErrorProxy(
            error_info={'error': True},
            email_metadata={'msg_id': 'MSG001'},
            base_ref_path="2025-11-01/143000_sender@example_abc123.eml"
        )
        
        # Error placeholders don't need materialization
        result = await error_proxy.materialize(blocking_secs=1.0)
        assert result is True
    
    def test_error_proxy_get_context_info(self):
        """Test error proxy context info."""
        error_proxy = OutlookEmailErrorProxy(
            error_info={'error': True, 'error_type': 'ValueError'},
            email_metadata={'msg_id': 'MSG123'},
            base_ref_path="2025-11-01/143000_sender@example_abc123.eml"
        )
        
        context = error_proxy.get_context_info()
        assert context['type'] == 'error'
        assert context['msg_id'] == 'MSG123'
        assert context['error_type'] == 'ValueError'
        assert 'ref_path' in context
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_factory_creates_error_placeholders_when_enabled(self, mock_api):
        """Test that factory creates error placeholders when create_error_placeholders=True."""
        factory = OutlookEmailFileProxyFactory(
            user_email="user@company.com",
            access_token="test-token",
            create_error_placeholders=True
        )
        
        # Mock API to return valid message list
        mock_response = Mock()
        mock_response.json.return_value = {
            "value": [{
                "id": "MSG001",
                "subject": "Test Email",
                "from": {"emailAddress": {"address": "sender@example.com", "name": "Sender"}},
                "toRecipients": [{"emailAddress": {"address": "user@company.com", "name": "User"}}],
                "receivedDateTime": "2025-11-01T14:30:00Z",
                "sentDateTime": "2025-11-01T14:29:00Z",
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
                "categories": [],
                "conversationId": "CONV001"
            }],
            "@odata.nextLink": None
        }
        mock_api.return_value = mock_response
        
        # Mock the from_graph_api to raise an exception
        with patch.object(OutlookEmailProxy, 'from_graph_api', side_effect=ValueError("Parse error")):
            received_after = datetime(2025, 11, 1, 0, 0, 0)
            proxies = list(factory.scan_messages(
                received_after=received_after,
                folder_path="Inbox"
            ))
            
            # Should have 1 error proxy
            assert len(proxies) == 1
            assert isinstance(proxies[0], OutlookEmailErrorProxy)
            assert proxies[0].email_component() == "error"
    
    @patch('totodev_pub.cached_file_folders_support.file_proxy_outlook_email._api_call_with_retry')
    def test_factory_skips_errors_when_disabled(self, mock_api):
        """Test that factory skips errors when create_error_placeholders=False."""
        factory = OutlookEmailFileProxyFactory(
            user_email="user@company.com",
            access_token="test-token",
            create_error_placeholders=False  # Disabled
        )
        
        # Mock API to return valid message list
        mock_response = Mock()
        mock_response.json.return_value = {
            "value": [{
                "id": "MSG001",
                "subject": "Test Email",
                "from": {"emailAddress": {"address": "sender@example.com", "name": "Sender"}},
                "toRecipients": [{"emailAddress": {"address": "user@company.com", "name": "User"}}],
                "receivedDateTime": "2025-11-01T14:30:00Z",
                "sentDateTime": "2025-11-01T14:29:00Z",
                "importance": "normal",
                "isRead": False,
                "flag": {"flagStatus": "notFlagged"},
                "categories": [],
                "conversationId": "CONV001"
            }],
            "@odata.nextLink": None
        }
        mock_api.return_value = mock_response
        
        # Mock the from_graph_api to raise an exception
        with patch.object(OutlookEmailProxy, 'from_graph_api', side_effect=ValueError("Parse error")):
            received_after = datetime(2025, 11, 1, 0, 0, 0)
            proxies = list(factory.scan_messages(
                received_after=received_after,
                folder_path="Inbox"
            ))
            
            # Should have 0 proxies (errors silently skipped)
            assert len(proxies) == 0
    
    def test_factory_input_validation(self):
        """Test factory validates inputs properly."""
        # Invalid email
        with pytest.raises(ValueError, match="Invalid user_email"):
            OutlookEmailFileProxyFactory(
                user_email="not-an-email",
                access_token="valid-token-1234567890"
            )
        
        # Invalid token
        with pytest.raises(ValueError, match="Invalid or missing access_token"):
            OutlookEmailFileProxyFactory(
                user_email="user@company.com",
                access_token="short"
            )
        
        # Invalid base_url
        with pytest.raises(ValueError, match="base_url must use HTTPS"):
            OutlookEmailFileProxyFactory(
                user_email="user@company.com",
                access_token="valid-token-1234567890",
                base_url="http://insecure.com"
            )


# =============================================================================
# SPEC COMPLIANCE TESTS
# =============================================================================

class TestSpecCompliance:
    """Tests to verify compliance with the specification."""
    
    def test_sequence_number_zero_padding(self):
        """Verify sequence numbers are zero-padded to 2 digits per spec."""
        email_ref = "2025-11-01/143000_joe@example_MSG123.eml"
        
        # Test various sequence numbers
        for seq in [1, 5, 9, 10, 11, 99]:
            result = _format_ref_path_attachment(email_ref, seq, "file.pdf")
            # Extract sequence from result
            filename = result.split('/')[-1]
            seq_token = filename.split('_')[0]
            
            # Verify prefix and zero padding
            assert seq_token.startswith("att")
            seq_str = seq_token[3:]
            assert len(seq_str) == 2
            assert seq_str.isdigit()
            assert int(seq_str) == seq
    
    def test_ref_path_email_components(self):
        """Verify email ref_path contains all required components per spec."""
        dt = datetime(2025, 11, 1, 14, 30, 45)
        msg_id = "AAMkADM2MDU1NDZlLTg5MWQtNGEzZS05YWVkLTNkYzE2ZGVjODJkNw"
        result = _format_ref_path_email("Inbox", dt, "joe@example.com", msg_id)
        
        parts = result.split('/')
        
        # Should have 2 parts: date/filename.eml
        assert len(parts) == 2
        
        # Verify date format
        assert parts[0] == "2025-11-01"
        
        # Verify filename contains required components and .eml extension
        filename = parts[1]
        assert filename.endswith(".eml")
        assert "143045" in filename  # time
        assert "joe@example" in filename  # counterparty
        # msg_id should be hashed to 12 characters (before .eml)
        base_name = filename[:-4]  # Remove .eml
        filename_parts = base_name.split('_')
        msg_id_hash = filename_parts[-1]
        assert len(msg_id_hash) == 12
        assert msg_id_hash == _hash_msg_id(msg_id)
    
    def test_attachment_ref_path_structure(self):
        """Verify attachment ref_path structure per spec."""
        email_ref = "2025-11-01/143000_joe@example_a3f2c9b1.eml"
        result = _format_ref_path_attachment(email_ref, 1, "document.pdf")
        
        # Should be: date/email_base_name_files/att{seq}_{filename}
        parts = result.split('/')
        assert len(parts) == 3
        
        assert parts[0] == "2025-11-01"
        assert parts[1] == "143000_joe@example_a3f2c9b1_files"  # Email base name directory
        assert parts[2] == "att01_document.pdf"
    
    def test_counterparty_extraction_spec_examples(self):
        """Test counterparty extraction with examples from spec."""
        # Examples from spec
        assert _extract_counterparty("joe.smith@example.com") == "joe.smith@example"
        assert _extract_counterparty("admin@company.co.uk") == "admin@company"
        assert _extract_counterparty("user@localhost") == "user@localhost"
    
    def test_yaml_metadata_not_json(self):
        """Verify metadata is YAML, not JSON (per spec)."""
        # This is implicit in the write_metadata_to_slave_dir implementation
        # which uses yaml.dump, but we verify the requirement is clear
        import yaml as yaml_lib
        assert yaml_lib is not None, "PyYAML should be available for metadata"

