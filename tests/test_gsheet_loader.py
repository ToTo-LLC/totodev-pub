# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for Google Sheets to DbJig Loader Module.

Note: The concrete Google Sheets loader implementation lives in the
`totodev_pub.gsheet_loader` module. In environments where that optional
integration is not installed, this entire test module is skipped.
"""

import pytest
import tempfile
import os
from unittest.mock import Mock, patch, MagicMock
from io import StringIO

gs_module = pytest.importorskip(
    "totodev_pub.gsheet_loader",
    reason="Google Sheets loader module (totodev_pub.gsheet_loader) is not installed in this project."
)

from totodev_pub.gsheet_loader import (  # type: ignore[attr-defined]
    GoogleSheetLoader,
    GoogleSheetTableSpec,
    GoogleSheetLoaderException,
    SheetNotFoundException,
    ColumnsNotFoundException,
    AuthenticationException,
)
from totodev_pub.dbjig import DbJig


class TestGoogleSheetTableSpec:
    """Tests for GoogleSheetTableSpec class."""
    
    def test_table_spec_initialization(self):
        """Test that GoogleSheetTableSpec initializes correctly."""
        spec = GoogleSheetTableSpec(
            spreadsheet_id="test_id_123",
            expected_columns=["Name", "Email", "Phone"],
            table_name="contacts",
            sheet_name="Sheet1",
            header_row=1
        )
        
        assert spec.spreadsheet_id == "test_id_123"
        assert spec.expected_columns == ["Name", "Email", "Phone"]
        assert spec.table_name == "contacts"
        assert spec.sheet_name == "Sheet1"
        assert spec.header_row == 1
    
    def test_table_spec_defaults(self):
        """Test that optional fields have correct defaults."""
        spec = GoogleSheetTableSpec(
            spreadsheet_id="test_id_123",
            expected_columns=["Name"]
        )
        
        assert spec.table_name is None
        assert spec.sheet_name is None
        assert spec.header_row == 1
    
    def test_table_spec_validation_no_spreadsheet_id(self):
        """Test that empty spreadsheet_id raises ValueError."""
        with pytest.raises(ValueError, match="spreadsheet_id is required"):
            GoogleSheetTableSpec(
                spreadsheet_id="",
                expected_columns=["Name"]
            )
    
    def test_table_spec_validation_no_columns(self):
        """Test that empty expected_columns raises ValueError."""
        with pytest.raises(ValueError, match="expected_columns is required"):
            GoogleSheetTableSpec(
                spreadsheet_id="test_id",
                expected_columns=[]
            )
    
    def test_table_spec_validation_invalid_header_row(self):
        """Test that invalid header_row raises ValueError."""
        with pytest.raises(ValueError, match="header_row must be >= 1"):
            GoogleSheetTableSpec(
                spreadsheet_id="test_id",
                expected_columns=["Name"],
                header_row=0
            )


class TestGoogleSheetLoader:
    """Tests for GoogleSheetLoader class."""
    
    @pytest.fixture
    def mock_credentials_path(self, tmp_path):
        """Create a temporary credentials file path."""
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"type": "service_account", "project_id": "test"}')
        return str(creds_file)
    
    @pytest.fixture
    def loader(self, mock_credentials_path):
        """Create a GoogleSheetLoader instance."""
        return GoogleSheetLoader(mock_credentials_path)
    
    @pytest.fixture
    def sample_spec(self):
        """Create a sample table spec."""
        return GoogleSheetTableSpec(
            spreadsheet_id="test_spreadsheet_123",
            expected_columns=["Name", "Email", "Phone"],
            table_name="contacts"
        )

    @pytest.fixture
    def dbjig_schema_sources(self):
        """Provide minimal DbJig seed files so new databases can be created."""
        return {
            '<<unit_test>>/1000-01-01_contacts.sql': (
                "CREATE TABLE contacts (\n"
                "    Name TEXT,\n"
                "    Email TEXT\n"
                ");"
            )
        }
    
    def test_loader_initialization(self, mock_credentials_path):
        """Test loader initializes correctly."""
        loader = GoogleSheetLoader(mock_credentials_path)
        assert loader.credentials_path == mock_credentials_path
        assert loader._service is None
    
    @patch('my_test_file.service_account.Credentials.from_service_account_file')
    @patch('my_test_file.build')
    def test_authenticate(self, mock_build, mock_creds, loader):
        """Test authentication with Google Sheets API."""
        mock_creds_obj = Mock()
        mock_creds.return_value = mock_creds_obj
        mock_service = Mock()
        mock_build.return_value = mock_service
        
        service = loader._authenticate()
        
        assert service == mock_service
        mock_creds.assert_called_once_with(
            loader.credentials_path,
            scopes=GoogleSheetLoader.SCOPES
        )
        mock_build.assert_called_once_with('sheets', 'v4', credentials=mock_creds_obj)
    
    @patch('my_test_file.service_account.Credentials.from_service_account_file')
    def test_authenticate_failure(self, mock_creds, loader):
        """Test authentication failure raises proper exception."""
        mock_creds.side_effect = Exception("Auth failed")
        
        with pytest.raises(AuthenticationException, match="Failed to authenticate"):
            loader._authenticate()
    
    def test_find_header_row_exact_match(self, loader):
        """Test finding header row with exact column matches."""
        sheet_data = [
            ["ID", "Name", "Email"],
            ["1", "John", "john@example.com"],
            ["2", "Jane", "jane@example.com"]
        ]
        expected_columns = ["ID", "Name", "Email"]
        
        header_idx = loader._find_header_row(sheet_data, expected_columns)
        
        assert header_idx == 0
    
    def test_find_header_row_not_in_first_row(self, loader):
        """Test finding header row when it's not the first row."""
        sheet_data = [
            ["Title", "Document Info"],
            ["", ""],
            ["ID", "Name", "Email"],
            ["1", "John", "john@example.com"]
        ]
        expected_columns = ["ID", "Name", "Email"]
        
        header_idx = loader._find_header_row(sheet_data, expected_columns)
        
        assert header_idx == 2
    
    def test_find_header_row_not_found(self, loader):
        """Test when header row is not found."""
        sheet_data = [
            ["ID", "Name"],
            ["1", "John"]
        ]
        expected_columns = ["ID", "Name", "Email"]  # Email not present
        
        header_idx = loader._find_header_row(sheet_data, expected_columns)
        
        assert header_idx is None
    
    @patch.object(GoogleSheetLoader, '_authenticate')
    @patch.object(GoogleSheetLoader, '_get_spreadsheet_metadata')
    def test_find_table_with_mock(self, mock_metadata, mock_auth, loader, sample_spec):
        """Test finding table with mocked Google API."""
        # Mock the service
        mock_service = Mock()
        mock_auth.return_value = mock_service
        
        # Mock spreadsheet metadata
        mock_metadata.return_value = {
            'sheets': [
                {'properties': {'title': 'Sheet1'}},
                {'properties': {'title': 'Contacts'}}
            ]
        }
        
        # Mock the values().get() call
        mock_values = Mock()
        mock_service.spreadsheets().values().get.return_value = mock_values
        mock_values.execute.return_value = {
            'values': [
                ['Name', 'Email', 'Phone'],
                ['John Doe', 'john@example.com', '555-1234'],
                ['Jane Smith', 'jane@example.com', '555-5678']
            ]
        }
        
        result = loader.find_table(sample_spec)
        
        assert result['sheet_name'] == 'Sheet1'
        assert result['columns'] == ['Name', 'Email', 'Phone']
        assert result['row_count'] == 2
        assert result['header_row'] == 1
    
    @patch.object(GoogleSheetLoader, '_authenticate')
    @patch.object(GoogleSheetLoader, '_get_spreadsheet_metadata')
    def test_find_table_sheet_not_found(self, mock_metadata, mock_auth, loader):
        """Test that SheetNotFoundException is raised when sheet doesn't exist."""
        mock_auth.return_value = Mock()
        mock_metadata.return_value = {
            'sheets': [
                {'properties': {'title': 'Sheet1'}}
            ]
        }
        
        spec = GoogleSheetTableSpec(
            spreadsheet_id="test_id",
            expected_columns=["Name"],
            sheet_name="NonExistentSheet"
        )
        
        with pytest.raises(SheetNotFoundException, match="Sheet 'NonExistentSheet' not found"):
            loader.find_table(spec)
    
    @patch.object(GoogleSheetLoader, 'find_table')
    @patch.object(GoogleSheetLoader, '_authenticate')
    def test_extract_table_as_dict(self, mock_auth, mock_find_table, loader, sample_spec):
        """Test extracting table as dictionary."""
        # Mock find_table result
        mock_find_table.return_value = {
            'sheet_name': 'Contacts',
            'range': 'Contacts!A1:C3',
            'columns': ['Name', 'Email', 'Phone'],
            'row_count': 2,
            'header_row': 1
        }
        
        # Mock the service
        mock_service = Mock()
        mock_auth.return_value = mock_service
        mock_values = Mock()
        mock_service.spreadsheets().values().get.return_value = mock_values
        mock_values.execute.return_value = {
            'values': [
                ['Name', 'Email', 'Phone'],
                ['John', 'john@example.com', '555-1234']
            ]
        }
        
        result = loader.extract_table_as_dict(sample_spec, batch_label="2025-11-04")
        
        assert len(result) == 1
        key = list(result.keys())[0]
        assert key.startswith('<<gsheet>>/2025-11-04_contacts.csv')
        assert 'Name,Email,Phone' in result[key]
        assert 'John,john@example.com,555-1234' in result[key]
    
    @patch.object(GoogleSheetLoader, 'extract_table_as_dict')
    def test_export_to_file(self, mock_extract, loader, sample_spec, tmp_path):
        """Test exporting table to CSV file."""
        # Mock the extracted data
        mock_extract.return_value = {
            '<<gsheet>>/2025-11-04_contacts.csv': 'Name,Email\nJohn,john@example.com\n'
        }
        
        output_path = str(tmp_path / "test_output.csv")
        
        result_path = loader.export_table_to_file(
            sample_spec,
            output_path,
            include_batch_label=True,
            overwrite=False
        )
        
        assert os.path.exists(result_path)
        with open(result_path, 'r') as f:
            content = f.read()
            assert 'Name,Email' in content
            assert 'John,john@example.com' in content
    
    @patch.object(GoogleSheetLoader, 'extract_table_as_dict')
    def test_export_to_directory(self, mock_extract, loader, sample_spec, tmp_path):
        """Test exporting table to a directory (not a file path)."""
        mock_extract.return_value = {
            '<<gsheet>>/2025-11-04_contacts.csv': 'Name,Email\nJohn,john@example.com\n'
        }
        
        output_dir = str(tmp_path / "exports")
        os.makedirs(output_dir, exist_ok=True)
        
        result_path = loader.export_table_to_file(
            sample_spec,
            output_dir,
            include_batch_label=True
        )
        
        assert os.path.exists(result_path)
        assert result_path.startswith(output_dir)
        assert '2025-11-04_contacts.csv' in result_path
    
    @patch.object(GoogleSheetLoader, 'extract_table_as_dict')
    def test_load_into_dbjig(self, mock_extract, loader, sample_spec, tmp_path, dbjig_schema_sources):
        """Test loading data into DbJig database."""
        # Mock extracted data
        mock_extract.return_value = {
            '<<gsheet>>/2025-11-04_contacts.csv': 'Name,Email\nJohn,john@example.com\nJane,jane@example.com\n'
        }
        
        # Create a temporary database
        db_path = str(tmp_path / "test.db")
        dbjig = DbJig(db_path, loadsources=dbjig_schema_sources)
        
        # Load the data
        result = loader.load_into_dbjig(dbjig, sample_spec, batch_label="2025-11-04")
        
        # Verify data was loaded
        rows = dbjig.query("SELECT * FROM contacts")
        assert len(rows) == 2
        assert rows[0]['Name'] == 'John'
        assert rows[0]['Email'] == 'john@example.com'
        assert rows[1]['Name'] == 'Jane'
        
        # Check that batch was logged
        loaded_labels = dbjig.loaded_labels()
        assert '2025-11-04' in loaded_labels
    
    @patch.object(GoogleSheetLoader, 'load_into_dbjig')
    def test_quick_load(self, mock_load, mock_credentials_path, tmp_path, dbjig_schema_sources):
        """Test the quick_load convenience method."""
        db_path = str(tmp_path / "test.db")
        dbjig = DbJig(db_path, loadsources=dbjig_schema_sources)
        
        mock_load.return_value = {'2025-11-04': []}
        
        result = GoogleSheetLoader.quick_load(
            credentials_path=mock_credentials_path,
            dbjig=dbjig,
            spreadsheet_id="test_id",
            expected_columns=["Name", "Email"],
            table_name="contacts",
            sheet_name="Sheet1",
            batch_label="2025-11-04"
        )
        
        # Verify load_into_dbjig was called
        mock_load.assert_called_once()
        call_args = mock_load.call_args
        assert call_args[0][0] == dbjig  # First positional arg is dbjig
        spec = call_args[0][1]  # Second positional arg is spec
        assert spec.spreadsheet_id == "test_id"
        assert spec.expected_columns == ["Name", "Email"]
        assert spec.table_name == "contacts"
        assert spec.sheet_name == "Sheet1"



