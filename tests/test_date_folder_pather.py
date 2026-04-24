# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import datetime
import os
import pytest
from pathlib import Path

from totodev_pub.date_folders import _DateFolderPather


class TestDateFolderPather:
    """Test cases for the _DateFolderPather utility class."""

    def test_init_valid_patterns(self):
        """Test initialization with valid folder patterns."""
        # Basic patterns
        pather = _DateFolderPather("%Y/%m/%d")
        assert pather.folder_pattern == "%Y/%m/%d"
        
        pather = _DateFolderPather("%Y-%m-%d")
        assert pather.folder_pattern == "%Y-%m-%d"
        
        # Complex patterns
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        assert pather.folder_pattern == "Project/%Y/%m-%b/%d"
        
        pather = _DateFolderPather("%Y/%m/%d-%a")
        assert pather.folder_pattern == "%Y/%m/%d-%a"

    def test_init_missing_required_components(self):
        """Test initialization with missing required date components."""
        # Missing day
        with pytest.raises(ValueError, match="Missing: %d"):
            _DateFolderPather("%Y/%m")
        
        # Missing month
        with pytest.raises(ValueError, match="Missing: %m"):
            _DateFolderPather("%Y/%d")
        
        # Missing year
        with pytest.raises(ValueError, match="Missing: %Y"):
            _DateFolderPather("%m/%d")
        
        # Missing multiple components
        with pytest.raises(ValueError, match="Missing: %m, %d"):
            _DateFolderPather("%Y")

    def test_init_time_directives_forbidden(self):
        """Test that time-centric directives are rejected."""
        time_directives = ["%H", "%I", "%M", "%S", "%p"]
        
        for directive in time_directives:
            pattern = f"%Y/%m/%d/{directive}"
            with pytest.raises(ValueError, match="time-centric directives"):
                _DateFolderPather(pattern)

    def test_init_starts_with_slash(self):
        """Test that patterns starting with forward slash are rejected."""
        with pytest.raises(ValueError, match="start with a separator"):
            _DateFolderPather("/%Y/%m/%d")

    def test_init_strips_trailing_slash(self):
        """Test that trailing slashes are stripped."""
        pather = _DateFolderPather("%Y/%m/%d/")
        assert pather.folder_pattern == "%Y/%m/%d"

    def test_build_pattern_regex_parts(self):
        """Test the static method that builds regex parts."""
        parts = _DateFolderPather.build_pattern_regex_parts("%Y/%m/%d")
        assert len(parts) == 3
        
        # Check year part
        assert parts[0].regex_pattern == r"(\d{4})"
        assert parts[0].capture_directives == ["%Y"]
        assert parts[0].has_captures is True
        
        # Check month part
        assert parts[1].regex_pattern == r"(\d{1,2})"
        assert parts[1].capture_directives == ["%m"]
        assert parts[1].has_captures is True
        
        # Check day part
        assert parts[2].regex_pattern == r"(\d{1,2})"
        assert parts[2].capture_directives == ["%d"]
        assert parts[2].has_captures is True

    def test_build_pattern_regex_parts_with_literal_text(self):
        """Test regex parts with literal text and mixed content."""
        parts = _DateFolderPather.build_pattern_regex_parts("Project/%Y/%m-%b/%d")
        assert len(parts) == 4
        
        # Project (literal)
        assert parts[0].regex_pattern == "Project"
        assert parts[0].capture_directives == []
        assert parts[0].has_captures is False
        
        # Year
        assert parts[1].regex_pattern == r"(\d{4})"
        assert parts[1].capture_directives == ["%Y"]
        
        # Month with abbreviation
        assert parts[2].regex_pattern == r"(\d{1,2})-[A-Za-z]+"
        assert parts[2].capture_directives == ["%m"]
        
        # Day
        assert parts[3].regex_pattern == r"(\d{1,2})"
        assert parts[3].capture_directives == ["%d"]

    def test_build_pattern_regex_parts_with_other_directives(self):
        """Test regex parts with non-captured strftime directives."""
        parts = _DateFolderPather.build_pattern_regex_parts("%Y/%m/%d-%a")
        assert len(parts) == 3
        
        # Year
        assert parts[0].regex_pattern == r"(\d{4})"
        assert parts[0].capture_directives == ["%Y"]
        
        # Month
        assert parts[1].regex_pattern == r"(\d{1,2})"
        assert parts[1].capture_directives == ["%m"]
        
        # Day with weekday
        assert parts[2].regex_pattern == r"(\d{1,2})-[A-Za-z]+"
        assert parts[2].capture_directives == ["%d"]

    def test_capture_positions_tracking(self):
        """Test that capture group positions are correctly tracked."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Group 1: outer wrapper (^|/)
        # Group 2: entire pattern
        # Group 3: year
        # Group 4: month  
        # Group 5: day
        assert pather._date_capture_positions["%Y"] == 3
        assert pather._date_capture_positions["%m"] == 4
        assert pather._date_capture_positions["%d"] == 5

    def test_capture_positions_complex_pattern(self):
        """Test capture positions with complex pattern."""
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        # Group 1: outer wrapper (^|/)
        # Group 2: entire pattern
        # Group 3: year
        # Group 4: month
        # Group 5: day
        assert pather._date_capture_positions["%Y"] == 3
        assert pather._date_capture_positions["%m"] == 4
        assert pather._date_capture_positions["%d"] == 5

    def test_regex_compilation(self):
        """Test that the regex is compiled correctly."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Should match exact pattern
        assert pather._regexp.search("2024/01/01")
        
        # Should match within larger paths
        assert pather._regexp.search("/var/www/data/2024/01/01/file.txt")
        
        # Test with different separator patterns
        pather_dash = _DateFolderPather("%Y-%m-%d")
        assert pather_dash._regexp.search("2024-12-31")
        
        # Test Windows-style paths with the appropriate pattern
        if os.sep == "\\":
            # On Windows, test with backslashes
            assert pather._regexp.search("C:\\Users\\data\\2024\\01\\01\\backup")
        else:
            # On Unix, test with forward slashes
            assert pather._regexp.search("/var/www/data/2024/01/01/file.txt")

    def test_regex_with_os_separator(self):
        """Test that regex works with different OS separators."""
        # Test with forward slash (Unix-like)
        pather = _DateFolderPather("%Y/%m/%d")
        assert pather._regexp.search("2024/01/01")
        
        # Test with backslash (Windows)
        if os.sep == "\\":
            assert pather._regexp.search("2024\\01\\01")
        else:
            assert pather._regexp.search("2024/01/01")

    def test_construct_folder_path_basic(self):
        """Test basic folder path construction."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        path = pather.construct_folder_path(2024, 1, 1)
        expected = Path("2024/01/01")
        assert path == expected
        
        path = pather.construct_folder_path(2024, 12, 31)
        expected = Path("2024/12/31")
        assert path == expected

    def test_construct_folder_path_with_offset(self):
        """Test folder path construction with day offset."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Forward offset
        path = pather.construct_folder_path(2024, 1, 1, offset_days=1)
        expected = Path("2024/01/02")
        assert path == expected
        
        # Backward offset
        path = pather.construct_folder_path(2024, 1, 1, offset_days=-1)
        expected = Path("2023/12/31")
        assert path == expected
        
        # Large offset (2024 is a leap year, so 365 days = Dec 31, 2024)
        path = pather.construct_folder_path(2024, 1, 1, offset_days=365)
        expected = Path("2024/12/31")
        assert path == expected
        
        # Test with 366 days to get to next year
        path = pather.construct_folder_path(2024, 1, 1, offset_days=366)
        expected = Path("2025/01/01")
        assert path == expected

    def test_construct_folder_path_complex_pattern(self):
        """Test folder path construction with complex patterns."""
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        path = pather.construct_folder_path(2024, 1, 1)
        expected = Path("Project/2024/01-Jan/01")
        assert path == expected
        
        pather = _DateFolderPather("%Y/%m/%d-%a")
        path = pather.construct_folder_path(2024, 1, 1)
        expected = Path("2024/01/01-Mon")
        assert path == expected

    def test_construct_folder_path_os_separator(self):
        """Test that constructed paths use OS-specific separators."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        path = pather.construct_folder_path(2024, 1, 1)
        # Should use current OS separator
        assert str(path) == f"2024{os.sep}01{os.sep}01"
        
        # Test with overridden separator
        _DateFolderPather.set_directory_separator("/")
        pather_unix = _DateFolderPather("%Y/%m/%d")
        path = pather_unix.construct_folder_path(2024, 1, 1)
        assert str(path) == "2024/01/01"
        
        # Reset
        _DateFolderPather.reset_directory_separator()

    def test_infer_date_from_folder_path_exact_match(self):
        """Test date inference from exact pattern matches."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        date = pather.infer_date_from_folder_path("2024/01/01")
        assert date == datetime.date(2024, 1, 1)
        
        # Test with different separator patterns
        pather_dash = _DateFolderPather("%Y-%m-%d")
        date = pather_dash.infer_date_from_folder_path("2024-12-31")
        assert date == datetime.date(2024, 12, 31)

    def test_infer_date_from_folder_path_within_larger_path(self):
        """Test date inference from paths containing the pattern."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Unix-style path
        date = pather.infer_date_from_folder_path("/var/www/data/2024/01/01/file.txt")
        assert date == datetime.date(2024, 1, 1)
        
        # Windows-style path
        if os.sep == "\\":
            date = pather.infer_date_from_folder_path("C:\\Users\\data\\2024\\01\\01\\backup")
            assert date == datetime.date(2024, 1, 1)
        
        # Mixed separators
        date = pather.infer_date_from_folder_path("data/2024/01/01/subfolder")
        assert date == datetime.date(2024, 1, 1)

    def test_infer_date_from_folder_path_complex_pattern(self):
        """Test date inference with complex patterns."""
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        date = pather.infer_date_from_folder_path("/var/Project/2024/01-Jan/01/logs")
        assert date == datetime.date(2024, 1, 1)
        
        pather = _DateFolderPather("%Y-%m/%d-%a")
        date = pather.infer_date_from_folder_path("data/2024-01/01-Mon/backup")
        assert date == datetime.date(2024, 1, 1)

    def test_infer_date_from_folder_path_no_match(self):
        """Test date inference when no match is found."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Invalid date components
        assert pather.infer_date_from_folder_path("2024/01/32") is None
        assert pather.infer_date_from_folder_path("2024/13/01") is None
        
        # No date components
        assert pather.infer_date_from_folder_path("abc/def/ghi") is None
        
        # Pattern not bounded by separators
        assert pather.infer_date_from_folder_path("2024/01/01abc") is None
        assert pather.infer_date_from_folder_path("abc2024/01/01") is None

    def test_infer_date_from_folder_path_edge_cases(self):
        """Test date inference edge cases."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Start of string
        date = pather.infer_date_from_folder_path("2024/01/01")
        assert date == datetime.date(2024, 1, 1)
        
        # End of string
        date = pather.infer_date_from_folder_path("data/2024/01/01")
        assert date == datetime.date(2024, 1, 1)
        
        # Middle of path
        date = pather.infer_date_from_folder_path("folder/2024/01/01/subfolder")
        assert date == datetime.date(2024, 1, 1)

    def test_infer_date_from_folder_path_different_orders(self):
        """Test date inference with different component orders."""
        # YYYY/MM/DD
        pather = _DateFolderPather("%Y/%m/%d")
        date = pather.infer_date_from_folder_path("2024/01/01")
        assert date == datetime.date(2024, 1, 1)
        
        # MM/DD/YYYY
        pather = _DateFolderPather("%m/%d/%Y")
        date = pather.infer_date_from_folder_path("01/01/2024")
        assert date == datetime.date(2024, 1, 1)
        
        # DD/MM/YYYY
        pather = _DateFolderPather("%d/%m/%Y")
        date = pather.infer_date_from_folder_path("01/01/2024")
        assert date == datetime.date(2024, 1, 1)

    def test_regex_capture_groups(self):
        """Test that regex capture groups work correctly."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        match = pather._regexp.search("2024/01/01")
        assert match is not None
        
        # Group 1: outer wrapper
        assert match.group(1) in ["", "/"]
        
        # Group 2: entire pattern
        assert match.group(2) == "2024/01/01"
        
        # Group 3: year
        assert match.group(3) == "2024"
        
        # Group 4: month
        assert match.group(4) == "01"
        
        # Group 5: day
        assert match.group(5) == "01"

    def test_regex_with_literal_text(self):
        """Test regex with patterns containing literal text."""
        pather = _DateFolderPather("Project/%Y/%m/%d")
        
        match = pather._regexp.search("Project/2024/01/01")
        assert match is not None
        
        # Should match the entire pattern
        assert "Project/2024/01/01" in match.group(0)

    def test_regex_with_weekday_and_month_names(self):
        """Test regex with weekday and month name directives."""
        pather = _DateFolderPather("%Y/%m-%b/%d-%a")
        
        match = pather._regexp.search("2024/01-Jan/01-Mon")
        assert match is not None
        
        # Should match the entire pattern
        assert "2024/01-Jan/01-Mon" in match.group(0)

    def test_os_separator_handling(self):
        """Test that OS separators are handled correctly."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Should work with current OS separator
        if os.sep == "\\":
            # Windows
            assert pather._regexp.search("2024\\01\\01")
        else:
            # Unix-like
            assert pather._regexp.search("2024/01/01")

    def test_cross_platform_directory_separator(self):
        """Test that directory separator can be overridden for cross-platform testing."""
        # Test Unix-style separator
        _DateFolderPather.set_directory_separator("/")
        pather_unix = _DateFolderPather("%Y/%m/%d")
        
        # Should match Unix-style paths
        assert pather_unix._regexp.search("2024/01/01")
        assert pather_unix._regexp.search("/var/www/data/2024/01/01/file.txt")
        
        # Should construct Unix-style paths
        path = pather_unix.construct_folder_path(2024, 1, 1)
        assert str(path) == "2024/01/01"
        
        # Test Windows-style separator
        _DateFolderPather.set_directory_separator("\\")
        pather_windows = _DateFolderPather("%Y/%m/%d")
        
        # Should match Windows-style paths
        assert pather_windows._regexp.search("2024\\01\\01")
        assert pather_windows._regexp.search("C:\\Users\\data\\2024\\01\\01\\backup")
        
        # Should construct Windows-style paths
        path = pather_windows.construct_folder_path(2024, 1, 1)
        assert str(path) == "2024\\01\\01"
        
        # Reset to OS default
        _DateFolderPather.reset_directory_separator()
        
        # Verify reset worked
        pather_default = _DateFolderPather("%Y/%m/%d")
        assert pather_default._dir_sep == os.sep

    def test_directory_separator_isolation(self):
        """Test that changing directory separator doesn't affect existing instances."""
        # Create instance with default separator
        pather1 = _DateFolderPather("%Y/%m/%d")
        original_sep = pather1._dir_sep
        
        # Change separator
        _DateFolderPather.set_directory_separator("\\")
        
        # Create new instance
        pather2 = _DateFolderPather("%Y/%m/%d")
        
        # Old instance should still have original separator
        assert pather1._dir_sep == original_sep
        
        # New instance should have new separator
        assert pather2._dir_sep == "\\"
        
        # Reset
        _DateFolderPather.reset_directory_separator()

    def test_pattern_validation_edge_cases(self):
        """Test pattern validation edge cases."""
        # Empty string (should fail validation)
        with pytest.raises(ValueError):
            _DateFolderPather("")
        
        # Just separators (should fail validation)
        with pytest.raises(ValueError):
            _DateFolderPather("///")
        
        # Pattern with only literal text (should fail validation)
        with pytest.raises(ValueError):
            _DateFolderPather("Project/Data")

    def test_class_variable_functionality(self):
        """Test that the class variable approach works correctly for all functionality."""
        # Test with Unix separator
        _DateFolderPather.set_directory_separator("/")
        pather_unix = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        # Test regex matching
        assert pather_unix._regexp.search("/var/Project/2024/01-Jan/01/logs")
        
        # Test path construction
        path = pather_unix.construct_folder_path(2024, 1, 1)
        assert str(path) == "Project/2024/01-Jan/01"
        
        # Test date inference
        date = pather_unix.infer_date_from_folder_path("/var/Project/2024/01-Jan/01/logs")
        assert date == datetime.date(2024, 1, 1)
        
        # Test with Windows separator
        _DateFolderPather.set_directory_separator("\\")
        pather_windows = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        # Test regex matching
        assert pather_windows._regexp.search("C:\\var\\Project\\2024\\01-Jan\\01\\logs")
        
        # Test path construction
        path = pather_windows.construct_folder_path(2024, 1, 1)
        assert str(path) == "Project\\2024\\01-Jan\\01"
        
        # Test date inference
        date = pather_windows.infer_date_from_folder_path("C:\\var\\Project\\2024\\01-Jan\\01\\logs")
        assert date == datetime.date(2024, 1, 1)
        
        # Reset to default
        _DateFolderPather.reset_directory_separator()

    def test_duplicate_directives(self):
        """Test patterns with duplicate date directives."""
        # Should work fine
        pather = _DateFolderPather("%Y/%Y/%m/%d")
        assert pather.folder_pattern == "%Y/%Y/%m/%d"
        
        # Should still capture all required components
        assert "%Y" in pather._date_capture_positions
        assert "%m" in pather._date_capture_positions
        assert "%d" in pather._date_capture_positions

    def test_mixed_separators_in_pattern(self):
        """Test patterns with mixed separators (though this might not be recommended)."""
        pather = _DateFolderPather("%Y-%m/%d")
        assert pather.folder_pattern == "%Y-%m/%d"
        
        # Should still work for construction
        path = pather.construct_folder_path(2024, 1, 1)
        expected = Path("2024-01/01")
        assert path == expected

    def test_pattern_length(self):
        """Test the pattern_length method returns correct number of directory parts."""
        # Basic pattern: %Y/%m/%d -> 3 parts
        pather = _DateFolderPather("%Y/%m/%d")
        assert len(pather._dir_parts) == 3
        
        # Pattern with literal text: Project/%Y/%m-%b/%d -> 4 parts
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        assert len(pather._dir_parts) == 4
        
        # Pattern with mixed separators: %Y/%m-%d -> 2 parts (forward slash + dash within part)
        pather = _DateFolderPather("%Y/%m-%d")
        assert len(pather._dir_parts) == 2
        
        # Pattern with weekday: %Y/%m/%d-%a -> 3 parts
        pather = _DateFolderPather("%Y/%m/%d-%a")
        assert len(pather._dir_parts) == 3
        
        # Complex pattern: Archive/%Y/%m/%d/backup -> 5 parts
        pather = _DateFolderPather("Archive/%Y/%m/%d/backup")
        assert len(pather._dir_parts) == 5

    def test_backslash_patterns_rejected(self):
        """Test that folder patterns with backslashes are properly rejected."""
        # Test backslash-only pattern
        with pytest.raises(ValueError, match="Folder pattern must use forward slashes"):
            _DateFolderPather("%Y\\%m\\%d")
        
        # Test mixed separators
        with pytest.raises(ValueError, match="Folder pattern must use forward slashes"):
            _DateFolderPather("%Y/%m\\%d")
        
        # Test backslash with literal text
        with pytest.raises(ValueError, match="Folder pattern must use forward slashes"):
            _DateFolderPather("Project\\%Y\\%m\\%d")

    def test_globify_basic_patterns(self):
        """Test the globify method with basic date patterns."""
        # Test basic %Y/%m/%d pattern
        pather = _DateFolderPather("%Y/%m/%d")
        
        # No values provided - should use glob patterns
        result = pather._dir_parts[0].globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        result = pather._dir_parts[1].globify()
        assert result == "[0-9][0-9]"
        
        result = pather._dir_parts[2].globify()
        assert result == "[0-9][0-9]"
        
        # With specific values provided
        result = pather._dir_parts[0].globify(year=2024)
        assert result == "2024"
        
        result = pather._dir_parts[1].globify(month=12)
        assert result == "12"
        
        result = pather._dir_parts[2].globify(day=25)
        assert result == "25"
        
        # Mixed values
        result = pather._dir_parts[0].globify(year=2024, month=12)
        assert result == "2024"
        
        result = pather._dir_parts[1].globify(year=2024, month=12)
        assert result == "12"

    def test_globify_with_other_strftime_directives(self):
        """Test globify method with non-date strftime directives."""
        # Test pattern with weekday and month abbreviations
        pather = _DateFolderPather("%Y/%m-%b/%d-%a")
        
        # Year part - should handle %Y correctly
        result = pather._dir_parts[0].globify(year=2024)
        assert result == "2024"
        
        result = pather._dir_parts[0].globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        # Month part with abbreviation - should handle %m and replace %b with *
        result = pather._dir_parts[1].globify(month=12)
        assert result == "12-*"
        
        result = pather._dir_parts[1].globify()
        assert result == "[0-9][0-9]-*"
        
        # Day part with weekday - should handle %d and replace %a with *
        result = pather._dir_parts[2].globify(day=25)
        assert result == "25-*"
        
        result = pather._dir_parts[2].globify()
        assert result == "[0-9][0-9]-*"

    def test_globify_with_literal_text(self):
        """Test globify method with patterns containing literal text."""
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        # Literal text should remain unchanged
        result = pather._dir_parts[0].globify()
        assert result == "Project"
        
        # Year part
        result = pather._dir_parts[1].globify(year=2024)
        assert result == "2024"
        
        result = pather._dir_parts[1].globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        # Month part with abbreviation
        result = pather._dir_parts[2].globify(month=12)
        assert result == "12-*"
        
        result = pather._dir_parts[2].globify()
        assert result == "[0-9][0-9]-*"
        
        # Day part
        result = pather._dir_parts[3].globify(day=25)
        assert result == "25"
        
        result = pather._dir_parts[3].globify()
        assert result == "[0-9][0-9]"

    def test_globify_edge_cases(self):
        """Test globify method with edge cases and unusual patterns."""
        # Test with duplicate directives
        pather = _DateFolderPather("%Y/%Y/%m/%d")
        
        # First year part
        result = pather._dir_parts[0].globify(year=2024)
        assert result == "2024"
        
        result = pather._dir_parts[0].globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        # Second year part
        result = pather._dir_parts[1].globify(year=2024)
        assert result == "2024"
        
        result = pather._dir_parts[1].globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        # Test with mixed separators in single part
        pather = _DateFolderPather("%Y-%m/%d")
        
        # Year-month part
        result = pather._dir_parts[0].globify(year=2024, month=12)
        assert result == "2024-12"
        
        result = pather._dir_parts[0].globify(year=2024)
        assert result == "2024-[0-9][0-9]"
        
        result = pather._dir_parts[0].globify(month=12)
        assert result == "[0-9][0-9][0-9][0-9]-12"
        
        result = pather._dir_parts[0].globify()
        assert result == "[0-9][0-9][0-9][0-9]-[0-9][0-9]"

    def test_globify_all_strftime_directives(self):
        """Test that all strftime directives are properly handled by globify."""
        # Test pattern with various strftime directives
        pather = _DateFolderPather("%Y/%m/%d-%a-%b-%j")
        
        # Year part
        result = pather._dir_parts[0].globify(year=2024)
        assert result == "2024"
        
        result = pather._dir_parts[0].globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        # Month part
        result = pather._dir_parts[1].globify(month=12)
        assert result == "12"
        
        result = pather._dir_parts[1].globify()
        assert result == "[0-9][0-9]"
        
        # Day part with multiple non-date directives
        result = pather._dir_parts[2].globify(day=25)
        assert result == "25-*-*-*"
        
        result = pather._dir_parts[2].globify()
        assert result == "[0-9][0-9]-*-*-*"

    def test_globify_formatting(self):
        """Test that globify properly formats numeric values."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Test single-digit month and day formatting
        result = pather._dir_parts[1].globify(month=1)
        assert result == "01"
        
        result = pather._dir_parts[2].globify(day=5)
        assert result == "05"
        
        # Test double-digit values
        result = pather._dir_parts[1].globify(month=12)
        assert result == "12"
        
        result = pather._dir_parts[2].globify(day=25)
        assert result == "25"
        
        # Test edge case values
        result = pather._dir_parts[1].globify(month=0)  # Should still format as 00
        assert result == "00"
        
        result = pather._dir_parts[2].globify(day=99)  # Should still format as 99
        assert result == "99"
