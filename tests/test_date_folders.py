# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import datetime
import os
from pathlib import Path
from totodev_pub.date_folders import _DateDirPart, _DateFolderPather, DateFolders


class TestDateDirPart:
    """Test the _DateDirPart class and its static methods."""
    
    def test_is_probably_well_ordered_chronological_order(self):
        """Test that patterns with %Y, %m, %d in chronological order return True."""
        # Create parts for "%Y/%m/%d" pattern
        parts = [
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is True
    
    def test_is_probably_well_ordered_reverse_order(self):
        """Test that patterns with %d, %m, %Y in reverse order return False."""
        # Create parts for "%d/%m/%Y" pattern
        parts = [
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m']),
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is False
    
    def test_is_probably_well_ordered_mixed_order(self):
        """Test that patterns with mixed order return False."""
        # Create parts for "%Y/%d/%m" pattern
        parts = [
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is False
    
    def test_is_probably_well_ordered_with_extra_parts(self):
        """Test that extra parts don't interfere with chronological order detection."""
        # Create parts for "Project/%Y/%m/%d" pattern
        parts = [
            _DateDirPart(regex_pattern=r'Project', original_text='Project', capture_directives=[]),
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is True
    
    def test_is_probably_well_ordered_missing_components(self):
        """Test that patterns missing required components return False."""
        # Missing %d component
        parts = [
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is False
    
    def test_is_probably_well_ordered_no_captures(self):
        """Test that parts with no capture directives are handled correctly."""
        parts = [
            _DateDirPart(regex_pattern=r'Project', original_text='Project', capture_directives=[]),
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m']),
            _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is True
    
    def test_is_probably_well_ordered_mixed_directives(self):
        """Test that patterns with date directives mixed with other characters are correctly identified."""
        # Test pattern like "Project/%Y-%m/%d-%a" which should be well-ordered
        parts = [
            _DateDirPart(regex_pattern=r'Project', original_text='Project', capture_directives=[]),
            _DateDirPart(regex_pattern=r'(\d{4})-(\d{1,2})', original_text='%Y-%m', capture_directives=['%Y', '%m']),
            _DateDirPart(regex_pattern=r'(\d{1,2})-\w+', original_text='%d-%a', capture_directives=['%d'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is True
        
        # Test pattern like "Data/%Y/%m-%b/%d-%a" which should also be well-ordered
        parts = [
            _DateDirPart(regex_pattern=r'Data', original_text='Data', capture_directives=[]),
            _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y']),
            _DateDirPart(regex_pattern=r'(\d{1,2})-\w+', original_text='%m-%b', capture_directives=['%m']),
            _DateDirPart(regex_pattern=r'(\d{1,2})-\w+', original_text='%d-%a', capture_directives=['%d'])
        ]
        
        assert _DateDirPart.is_probably_well_ordered(parts) is True
    
    def test_is_probably_well_ordered_empty_list(self):
        """Test that empty list returns False."""
        assert _DateDirPart.is_probably_well_ordered([]) is False

    def test_globify_no_parameters(self):
        """Test globify method with no parameters - should use glob patterns."""
        part = _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y'])
        result = part.globify()
        assert result == "[0-9][0-9][0-9][0-9]"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        result = part.globify()
        assert result == "[0-9][0-9]"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        result = part.globify()
        assert result == "[0-9][0-9]"

    def test_globify_with_year_only(self):
        """Test globify method with year parameter only."""
        part = _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y'])
        result = part.globify(year=2024)
        assert result == "2024"
        
        # Month and day should still use glob patterns
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        result = part.globify(year=2024)
        assert result == "[0-9][0-9]"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        result = part.globify(year=2024)
        assert result == "[0-9][0-9]"

    def test_globify_with_month_only(self):
        """Test globify method with month parameter only."""
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        result = part.globify(month=12)
        assert result == "12"
        
        # Year and day should still use glob patterns
        part = _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y'])
        result = part.globify(month=12)
        assert result == "[0-9][0-9][0-9][0-9]"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        result = part.globify(month=12)
        assert result == "[0-9][0-9]"

    def test_globify_with_day_only(self):
        """Test globify method with day parameter only."""
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        result = part.globify(day=25)
        assert result == "25"
        
        # Year and month should still use glob patterns
        part = _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y'])
        result = part.globify(day=25)
        assert result == "[0-9][0-9][0-9][0-9]"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        result = part.globify(day=25)
        assert result == "[0-9][0-9]"

    def test_globify_with_multiple_parameters(self):
        """Test globify method with multiple parameters."""
        part = _DateDirPart(regex_pattern=r'(\d{4})', original_text='%Y', capture_directives=['%Y'])
        result = part.globify(year=2024, month=12, day=25)
        assert result == "2024"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        result = part.globify(year=2024, month=12, day=25)
        assert result == "12"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        result = part.globify(year=2024, month=12, day=25)
        assert result == "25"

    def test_globify_with_other_strftime_directives(self):
        """Test globify method with non-date strftime directives."""
        # Test with weekday
        part = _DateDirPart(regex_pattern=r'(\d{1,2})-[A-Za-z]+', original_text='%d-%a', capture_directives=['%d'])
        result = part.globify(day=25)
        assert result == "25-*"
        
        result = part.globify()
        assert result == "[0-9][0-9]-*"
        
        # Test with month abbreviation
        part = _DateDirPart(regex_pattern=r'(\d{1,2})-[A-Za-z]+', original_text='%m-%b', capture_directives=['%m'])
        result = part.globify(month=12)
        assert result == "12-*"
        
        result = part.globify()
        assert result == "[0-9][0-9]-*"
        
        # Test with multiple non-date directives
        part = _DateDirPart(regex_pattern=r'(\d{1,2})-[A-Za-z]+-[A-Za-z]+', original_text='%d-%a-%b', capture_directives=['%d'])
        result = part.globify(day=25)
        assert result == "25-*-*"
        
        result = part.globify()
        assert result == "[0-9][0-9]-*-*"

    def test_globify_formatting(self):
        """Test that globify properly formats numeric values."""
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%m', capture_directives=['%m'])
        result = part.globify(month=1)
        assert result == "01"
        
        result = part.globify(month=12)
        assert result == "12"
        
        part = _DateDirPart(regex_pattern=r'(\d{1,2})', original_text='%d', capture_directives=['%d'])
        result = part.globify(day=5)
        assert result == "05"
        
        result = part.globify(day=25)
        assert result == "25"

    def test_globify_with_literal_text(self):
        """Test globify method with patterns containing literal text."""
        part = _DateDirPart(regex_pattern=r'Project', original_text='Project', capture_directives=[])
        result = part.globify()
        assert result == "Project"
        
        # Should remain unchanged regardless of parameters
        result = part.globify(year=2024, month=12, day=25)
        assert result == "Project"


class TestDateFolderPather:
    """Test the _DateFolderPather class."""
    
    def test_build_pattern_regex_parts_simple_pattern(self):
        """Test building regex parts from a simple pattern."""
        parts = _DateFolderPather.build_pattern_regex_parts("%Y/%m/%d")
        
        assert len(parts) == 3
        assert parts[0].original_text == "%Y"
        assert parts[0].regex_pattern == r'(\d{4})'
        assert parts[0].capture_directives == ["%Y"]
        
        assert parts[1].original_text == "%m"
        assert parts[1].regex_pattern == r'(\d{1,2})'
        assert parts[1].capture_directives == ["%m"]
        
        assert parts[2].original_text == "%d"
        assert parts[2].regex_pattern == r'(\d{1,2})'
        assert parts[2].capture_directives == ["%d"]
    
    def test_build_pattern_regex_parts_with_weekday(self):
        """Test building regex parts from a pattern with weekday."""
        parts = _DateFolderPather.build_pattern_regex_parts("%Y/%m/%d-%a")
        
        assert len(parts) == 3
        assert parts[0].original_text == "%Y"
        assert parts[0].capture_directives == ["%Y"]
        
        assert parts[1].original_text == "%m"
        assert parts[1].capture_directives == ["%m"]
        
        assert parts[2].original_text == "%d-%a"
        assert parts[2].capture_directives == ["%d"]
        # Should contain weekday pattern (regex for alphanumeric characters)
        assert "[A-Za-z]+" in parts[2].regex_pattern
    
    def test_build_pattern_regex_parts_with_prefix(self):
        """Test building regex parts from a pattern with prefix."""
        parts = _DateFolderPather.build_pattern_regex_parts("Project/%Y/%m/%d")
        
        assert len(parts) == 4
        assert parts[0].original_text == "Project"
        assert parts[0].capture_directives == []
        assert parts[0].regex_pattern == "Project"
        
        assert parts[1].original_text == "%Y"
        assert parts[1].capture_directives == ["%Y"]
    
    def test_validation_missing_components(self):
        """Test that patterns missing required components raise ValueError."""
        with pytest.raises(ValueError, match="must contain %Y, %m, %d"):
            _DateFolderPather("%Y/%m")
    
    def test_validation_time_directives(self):
        """Test that patterns with time directives raise ValueError."""
        with pytest.raises(ValueError, match="cannot contain time-centric directives"):
            _DateFolderPather("%Y/%m/%d/%H")
    
    def test_validation_leading_separator(self):
        """Test that patterns starting with separator raise ValueError."""
        with pytest.raises(ValueError, match="cannot start with a separator"):
            _DateFolderPather("/%Y/%m/%d")
    
    def test_validation_backslashes(self):
        """Test that patterns with backslashes raise ValueError."""
        with pytest.raises(ValueError, match="must use forward slashes"):
            _DateFolderPather("%Y\\%m\\%d")
    
    def test_construct_folder_path(self):
        """Test constructing folder paths from dates."""
        pather = _DateFolderPather("%Y/%m/%d")
        path = pather.construct_folder_path(2024, 1, 15)
        
        assert str(path) == "2024/01/15"
    
    def test_construct_folder_path_with_offset(self):
        """Test constructing folder paths with day offset."""
        pather = _DateFolderPather("%Y/%m/%d")
        path = pather.construct_folder_path(2024, 1, 1, offset_days=14)
        
        assert str(path) == "2024/01/15"
    
    def test_construct_folder_path_with_weekday(self):
        """Test constructing folder paths with weekday."""
        pather = _DateFolderPather("%Y/%m/%d-%a")
        path = pather.construct_folder_path(2024, 1, 15)
        
        # Should contain the weekday (e.g., "15-Mon")
        assert "15-" in str(path)
        assert len(str(path).split("/")) == 3
    
    def test_infer_date_from_folder_path(self):
        """Test inferring dates from folder paths."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Test simple path
        date = pather.infer_date_from_folder_path("2024/01/15")
        assert date == datetime.date(2024, 1, 15)
        
        # Test path with prefix
        date = pather.infer_date_from_folder_path("/some/root/2024/01/15")
        assert date == datetime.date(2024, 1, 15)
        
        # Test path with suffix
        date = pather.infer_date_from_folder_path("2024/01/15/file.txt")
        assert date == datetime.date(2024, 1, 15)
    
    def test_infer_date_from_folder_path_invalid(self):
        """Test that invalid paths return None."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # Invalid date
        date = pather.infer_date_from_folder_path("2024/13/32")
        assert date is None
        
        # Non-matching pattern
        date = pather.infer_date_from_folder_path("2024-01-15")
        assert date is None
    
    def test_directory_separator_handling(self):
        """Test that directory separators are handled correctly."""
        # Set custom separator
        _DateFolderPather.set_directory_separator("\\")
        
        pather = _DateFolderPather("%Y/%m/%d")
        path = pather.construct_folder_path(2024, 1, 15)
        
        # Should use backslash when custom separator is set
        assert "\\" in str(path)
        
        # Reset to default
        _DateFolderPather.reset_directory_separator()

    def test_globify_basic_pattern(self):
        """Test the globify method with basic date patterns."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        # No parameters - should return glob patterns for each level
        result = pather.globify()
        assert len(result) == 3
        assert str(result[0]) == "[0-9][0-9][0-9][0-9]"
        assert str(result[1]) == "[0-9][0-9]"
        assert str(result[2]) == "[0-9][0-9]"

    def test_globify_with_year_parameter(self):
        """Test globify method with year parameter."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        result = pather.globify(year=2024)
        assert len(result) == 2  # 2024 merged with [0-9][0-9], [0-9][0-9] separate
        assert str(result[0]) == "2024/[0-9][0-9]"
        assert str(result[1]) == "[0-9][0-9]"

    def test_globify_with_month_parameter(self):
        """Test globify method with month parameter."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        result = pather.globify(month=12)
        assert len(result) == 2  # [0-9][0-9][0-9][0-9] separate, 12 merged with [0-9][0-9]
        assert str(result[0]) == "[0-9][0-9][0-9][0-9]"
        assert str(result[1]) == "12/[0-9][0-9]"

    def test_globify_with_day_parameter(self):
        """Test globify method with day parameter."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        result = pather.globify(day=25)
        assert len(result) == 3
        assert str(result[0]) == "[0-9][0-9][0-9][0-9]"
        assert str(result[1]) == "[0-9][0-9]"
        assert str(result[2]) == "25"

    def test_globify_with_multiple_parameters(self):
        """Test globify method with multiple parameters."""
        pather = _DateFolderPather("%Y/%m/%d")
        
        result = pather.globify(year=2024, month=12, day=25)
        assert len(result) == 2  # 2024 merged with 12, 25 separate
        assert str(result[0]) == "2024/12"
        assert str(result[1]) == "25"

    def test_globify_complex_pattern(self):
        """Test globify method with complex patterns containing literal text."""
        pather = _DateFolderPather("xyz/%Y%a/%m-%d/abc")
        
        # No parameters
        result = pather.globify()
        assert len(result) == 3  # Should merge non-wildcard parts
        assert str(result[0]) == "xyz/[0-9][0-9][0-9][0-9]*"  # Merged xyz with %Y%a
        assert str(result[1]) == "[0-9][0-9]-[0-9][0-9]"      # %m-%d
        assert str(result[2]) == "abc"                          # Literal text

    def test_globify_with_parameters_complex_pattern(self):
        """Test globify method with complex patterns and parameters."""
        pather = _DateFolderPather("xyz/%Y%a/%m-%d/abc")
        
        # With year and day parameters
        result = pather.globify(year=2027, day=7)
        assert len(result) == 3
        assert str(result[0]) == "xyz/2027*"                    # Merged xyz with 2027*
        assert str(result[1]) == "[0-9][0-9]-07"               # %m-07
        assert str(result[2]) == "abc"                          # Literal text

    def test_globify_merging_behavior(self):
        """Test that non-wildcard parts are properly merged with wildcard parts."""
        pather = _DateFolderPather("Project/%Y/%m-%b/%d")
        
        # No parameters
        result = pather.globify()
        assert len(result) == 3  # Should merge Project with %Y
        assert str(result[0]) == "Project/[0-9][0-9][0-9][0-9]"  # Merged Project with %Y
        assert str(result[1]) == "[0-9][0-9]-*"                   # %m-%b
        assert str(result[2]) == "[0-9][0-9]"                     # %d

    def test_globify_with_weekday_patterns(self):
        """Test globify method with weekday patterns."""
        pather = _DateFolderPather("%Y/%m/%d-%a")
        
        result = pather.globify()
        assert len(result) == 3
        assert str(result[0]) == "[0-9][0-9][0-9][0-9]"
        assert str(result[1]) == "[0-9][0-9]"
        assert str(result[2]) == "[0-9][0-9]-*"  # %d-%a becomes [0-9][0-9]-*

    def test_globify_directory_separator_handling(self):
        """Test that globify uses the correct directory separator."""
        # Test with custom separator
        _DateFolderPather.set_directory_separator("\\")
        pather = _DateFolderPather("Project/%Y/%m/%d")
        
        result = pather.globify()
        assert len(result) == 3  # Project merged with %Y, %m and %d kept separate
        assert str(result[0]) == "Project\\[0-9][0-9][0-9][0-9]"  # Uses backslash
        assert str(result[1]) == "[0-9][0-9]"                      # %m kept separate
        assert str(result[2]) == "[0-9][0-9]"                      # %d kept separate
        
        # Reset to default
        _DateFolderPather.reset_directory_separator()

    def test_globify_edge_cases(self):
        """Test globify method with edge cases."""
        # Pattern with only date components
        pather = _DateFolderPather("%Y%m%d")
        result = pather.globify()
        assert len(result) == 1
        assert str(result[0]) == "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
        
        # Pattern with literal text at end
        pather = _DateFolderPather("%Y/%m/%d/backup")
        result = pather.globify()
        assert len(result) == 4  # No merging of wildcard parts
        assert str(result[0]) == "[0-9][0-9][0-9][0-9]"
        assert str(result[1]) == "[0-9][0-9]"
        assert str(result[2]) == "[0-9][0-9]"
        assert str(result[3]) == "backup"
        
        # Verify reset worked
        pather2 = _DateFolderPather("%Y/%m/%d")
        path2 = pather2.construct_folder_path(2024, 1, 15)
        assert "/" in str(path2)
    
    def test_pattern_well_ordered_detection(self):
        """Test that the pather can detect well-ordered patterns."""
        # Well-ordered pattern
        pather = _DateFolderPather("%Y/%m/%d")
        assert _DateDirPart.is_probably_well_ordered(pather._dir_parts) is True
        
        # Not well-ordered pattern
        pather = _DateFolderPather("%Y/%d/%m")
        assert _DateDirPart.is_probably_well_ordered(pather._dir_parts) is False
    
    def test_is_probably_well_ordered_instance_method(self):
        """Test the instance method is_probably_well_ordered()."""
        # Well-ordered pattern
        pather = _DateFolderPather("%Y/%m/%d")
        assert pather.is_probably_well_ordered() is True
        
        # Not well-ordered pattern
        pather = _DateFolderPather("%Y/%d/%m")
        assert pather.is_probably_well_ordered() is False
        
        # Pattern with prefix
        pather = _DateFolderPather("Project/%Y/%m/%d")
        assert pather.is_probably_well_ordered() is True
        
        # Pattern with mixed order
        pather = _DateFolderPather("%m/%Y/%d")
        assert pather.is_probably_well_ordered() is False


class TestDateFolders:
    """Test the DateFolders class."""
    
    def test_init_with_root_dir(self, tmp_path):
        """Test initialization with a root directory."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        assert df.root_dir == tmp_path
        assert df.folder_pattern == "%Y/%m/%d"
    
    def test_init_without_root_dir(self):
        """Test initialization without a root directory."""
        df = DateFolders("%Y/%m/%d")
        assert df.root_dir == Path.cwd()
    
    def test_init_auto_creates_final_directory_component(self, tmp_path):
        """Test that DateFolders auto-creates the final directory component when parent exists."""
        # Create a parent directory
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        
        # Target directory that doesn't exist yet
        target_dir = parent_dir / "target"
        
        # Verify target directory doesn't exist
        assert not target_dir.exists()
        
        # Initialize DateFolders with target directory as root
        df = DateFolders("%Y/%m/%d", root_dir=target_dir)
        
        # Verify target directory was auto-created
        assert target_dir.exists()
        assert df.root_dir == target_dir
    
    def test_init_auto_creates_final_directory_component_with_nested_path(self, tmp_path):
        """Test that DateFolders auto-creates the final directory component for nested paths."""
        # Create parent directories
        parent_dirs = tmp_path / "level1" / "level2" / "level3"
        parent_dirs.mkdir(parents=True)
        
        # Target directory that doesn't exist yet
        target_dir = parent_dirs / "final_level"
        
        # Verify target directory doesn't exist
        assert not target_dir.exists()
        
        # Initialize DateFolders with target directory as root
        df = DateFolders("%Y/%m/%d", root_dir=target_dir)
        
        # Verify target directory was auto-created
        assert target_dir.exists()
        assert df.root_dir == target_dir
    
    def test_init_raises_error_when_parent_doesnt_exist(self, tmp_path):
        """Test that DateFolders raises FileNotFoundError when parent directory doesn't exist."""
        # Create a non-existent nested path where parent doesn't exist
        target_dir = tmp_path / "nonexistent" / "parent" / "target"
        
        # Verify neither target nor parent exists
        assert not target_dir.exists()
        assert not target_dir.parent.exists()
        
        # Initialize DateFolders should raise FileNotFoundError
        with pytest.raises(FileNotFoundError, match="Parent directory .* does not exist"):
            DateFolders("%Y/%m/%d", root_dir=target_dir)
    
    def test_init_works_when_root_already_exists(self, tmp_path):
        """Test that DateFolders works normally when root directory already exists."""
        # Create target directory
        target_dir = tmp_path / "existing"
        target_dir.mkdir()
        
        # Verify target directory exists
        assert target_dir.exists()
        
        # Initialize DateFolders with existing directory as root
        df = DateFolders("%Y/%m/%d", root_dir=target_dir)
        
        # Verify everything works normally
        assert target_dir.exists()
        assert df.root_dir == target_dir
    
    def test_init_auto_creation_with_relative_path(self, tmp_path, monkeypatch):
        """Test auto-creation behavior with relative paths."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)
        
        # Create parent directory
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        
        # Target directory that doesn't exist yet (relative path)
        target_dir = parent_dir / "target"
        
        # Verify target directory doesn't exist
        assert not target_dir.exists()
        
        # Initialize DateFolders with relative path
        df = DateFolders("%Y/%m/%d", root_dir=target_dir)
        
        # Verify target directory was auto-created
        assert target_dir.exists()
        assert df.root_dir == target_dir
    
    def test_init_auto_creation_preserves_existing_directory(self, tmp_path):
        """Test that auto-creation doesn't interfere with existing directories."""
        # Create target directory with some content
        target_dir = tmp_path / "parent" / "target"
        target_dir.mkdir(parents=True)
        
        # Add some content to verify it's preserved
        test_file = target_dir / "test.txt"
        test_file.write_text("test content")
        
        # Verify target directory and content exist
        assert target_dir.exists()
        assert test_file.exists()
        
        # Initialize DateFolders with existing directory as root
        df = DateFolders("%Y/%m/%d", root_dir=target_dir)
        
        # Verify directory and content are preserved
        assert target_dir.exists()
        assert test_file.exists()
        assert test_file.read_text() == "test content"
        assert df.root_dir == target_dir
    
    def test_init_auto_creation_with_complex_folder_pattern(self, tmp_path):
        """Test auto-creation behavior with complex folder patterns."""
        # Create parent directory
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        
        # Target directory that doesn't exist yet
        target_dir = parent_dir / "complex_target"
        
        # Verify target directory doesn't exist
        assert not target_dir.exists()
        
        # Initialize DateFolders with complex pattern
        df = DateFolders("Project/%Y/%m-%b/%d-%a", root_dir=target_dir)
        
        # Verify target directory was auto-created
        assert target_dir.exists()
        assert df.root_dir == target_dir
        
        # Test that the folder creation still works
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        assert folder.exists()
        # Should create: target_dir/Project/2024/01-Jan/15-Mon
        assert "Project" in str(folder)
        assert "2024" in str(folder)
        assert "01-Jan" in str(folder)
        assert "15-Mon" in str(folder)
    
    def test_date_folder_creation(self, tmp_path):
        """Test creating date folders."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        assert folder.exists()
        # Check the full path structure
        assert folder.name == "15"
        assert folder.parent.name == "01"
        assert folder.parent.parent.name == "2024"
        # Verify the full path is correct
        expected_path = tmp_path / "2024" / "01" / "15"
        assert folder == expected_path
    
    def test_date_folder_with_offset(self, tmp_path):
        """Test creating date folders with integer offsets."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path, anchor_date=datetime.date(2024, 1, 15))
        
        # Today (offset 0)
        folder = df.folder(0, create=True)
        assert folder.name == "15"
        assert folder.parent.name == "01"
        assert folder.parent.parent.name == "2024"
        
        # Tomorrow (offset 1)
        folder = df.folder(1, create=True)
        assert folder.name == "16"
        assert folder.parent.name == "01"
        assert folder.parent.parent.name == "2024"
        
        # Yesterday (offset -1)
        folder = df.folder(-1, create=True)
        assert folder.name == "14"
        assert folder.parent.name == "01"
        assert folder.parent.parent.name == "2024"
    
    def test_infer_date(self, tmp_path):
        """Test inferring dates from folder paths."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create a folder structure
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        
        # Infer date from the folder path
        inferred_date = df.infer_date(folder)
        assert inferred_date == datetime.date(2024, 1, 15)
    
    def test_infer_date_invalid_path(self):
        """Test that invalid paths raise ValueError."""
        df = DateFolders("%Y/%m/%d")
        
        with pytest.raises(ValueError):
            df.infer_date("invalid/path")
    
    def test_folders_in_range(self, tmp_path):
        """Test iterating over folders in a date range."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create folders for a week
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 22)
        
        folders = list(df.folders(start_date, end_date, existing_only=False))
        assert len(folders) == 7
        
        # Check that folders are in chronological order
        dates = [df.infer_date(folder) for folder in folders]
        assert dates == [
            datetime.date(2024, 1, 15),
            datetime.date(2024, 1, 16),
            datetime.date(2024, 1, 17),
            datetime.date(2024, 1, 18),
            datetime.date(2024, 1, 19),
            datetime.date(2024, 1, 20),
            datetime.date(2024, 1, 21)
        ]
    
    def test_folders_in_range_reverse(self, tmp_path):
        """Test iterating over folders in reverse order."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 22)
        
        folders = list(df.folders(start_date, end_date, existing_only=False, reverse=True))
        
        # Check that folders are in reverse chronological order
        dates = [df.infer_date(folder) for folder in folders]
        assert dates == [
            datetime.date(2024, 1, 21),
            datetime.date(2024, 1, 20),
            datetime.date(2024, 1, 19),
            datetime.date(2024, 1, 18),
            datetime.date(2024, 1, 17),
            datetime.date(2024, 1, 16),
            datetime.date(2024, 1, 15)
        ]
    

    
    def test_pattern_caching(self):
        """Test that folder patterns are cached."""
        # Clear any existing cache
        DateFolders._folder_pattern_to_dir_parts.clear()
        
        # Create first instance
        df1 = DateFolders("%Y/%m/%d")
        assert "%Y/%m/%d" in DateFolders._folder_pattern_to_dir_parts
        
        # Create second instance with same pattern
        df2 = DateFolders("%Y/%m/%d")
        assert df1._pather is df2._pather  # Should be the same object
        
        # Create instance with different pattern
        df3 = DateFolders("%Y-%m-%d")
        assert df3._pather is not df1._pather  # Should be different object

    def test_existing_in_range_well_ordered_pattern(self, tmp_path):
        """Test existing_in_range with well-ordered folder pattern."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create some existing folders
        df.folder(datetime.date(2024, 1, 15), create=True)
        df.folder(datetime.date(2024, 1, 16), create=True)
        df.folder(datetime.date(2024, 1, 20), create=True)
        
        # Test finding existing folders in range
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 22)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 3
        
        # Check that all returned folders exist
        for folder in existing_folders:
            assert folder.exists()
        
        # Check that dates are in chronological order
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert dates == [
            datetime.date(2024, 1, 15),
            datetime.date(2024, 1, 16),
            datetime.date(2024, 1, 20)
        ]

    def test_existing_in_range_non_well_ordered_pattern(self):
        """Test that existing_in_range raises ValueError for non-well-ordered patterns."""
        df = DateFolders("%Y/%d/%m")  # Non-well-ordered pattern
        
        with pytest.raises(ValueError, match="is not well ordered"):
            list(df.existing(datetime.date(2024, 1, 15), datetime.date(2024, 1, 22)))

    def test_existing_in_range_reverse_order(self, tmp_path):
        """Test existing_in_range with reverse chronological order."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create some existing folders
        df.folder(datetime.date(2024, 1, 15), create=True)
        df.folder(datetime.date(2024, 1, 16), create=True)
        df.folder(datetime.date(2024, 1, 20), create=True)
        
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 22)
        
        existing_folders = list(df.existing(start_date, end_date, reverse=True))
        assert len(existing_folders) == 3
        
        # Check that dates are in reverse chronological order
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert dates == [
            datetime.date(2024, 1, 20),
            datetime.date(2024, 1, 16),
            datetime.date(2024, 1, 15)
        ]

    def test_existing_in_range_with_integer_offsets(self, tmp_path):
        """Test existing_in_range with integer date offsets."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path, anchor_date=datetime.date(2024, 1, 15))
        
        # Create some existing folders
        df.folder(0, create=True)   # 2024-01-15
        df.folder(1, create=True)   # 2024-01-16
        df.folder(5, create=True)   # 2024-01-20
        
        # Test with integer offsets
        existing_folders = list(df.existing(0, 7))  # 7 days from anchor
        assert len(existing_folders) == 3
        
        # Check that dates are correct
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert dates == [
            datetime.date(2024, 1, 15),
            datetime.date(2024, 1, 16),
            datetime.date(2024, 1, 20)
        ]

    def test_existing_in_range_with_anchor_date_parameter(self, tmp_path):
        """Test existing_in_range with anchor_date parameter override."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path, anchor_date=datetime.date(2024, 1, 15))
        
        # Create some existing folders for the dates we expect to find
        df.folder(datetime.date(2024, 1, 9), create=True)   # -1 from new anchor
        df.folder(datetime.date(2024, 1, 10), create=True)  # 0 from new anchor
        df.folder(datetime.date(2024, 1, 11), create=True)  # 1 from new anchor
        
        # Test with different anchor date
        new_anchor = datetime.date(2024, 1, 10)
        existing_folders = list(df.existing(-1, 3, anchor_date=new_anchor))
        assert len(existing_folders) == 3
        
        # Check that dates are correct (relative to new anchor)
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert dates == [
            datetime.date(2024, 1, 9),   # -1 from new anchor
            datetime.date(2024, 1, 10),  # 0 from new anchor
            datetime.date(2024, 1, 11)   # 1 from new anchor
        ]

    def test_existing_in_range_complex_pattern(self, tmp_path):
        """Test existing_in_range with complex folder patterns."""
        df = DateFolders("Project/%Y/%m-%b/%d", root_dir=tmp_path)
        
        # Create some existing folders with complex pattern
        df.folder(datetime.date(2024, 1, 15), create=True)
        df.folder(datetime.date(2024, 1, 16), create=True)
        
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 18)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 2
        
        # Check that all returned folders exist
        for folder in existing_folders:
            assert folder.exists()

    def test_existing_in_range_no_existing_folders(self, tmp_path):
        """Test existing_in_range when no folders exist in the range."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 22)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 0

    def test_existing_in_range_partial_range(self, tmp_path):
        """Test existing_in_range with partial range coverage."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create folders outside the range
        df.folder(datetime.date(2024, 1, 10), create=True)
        df.folder(datetime.date(2024, 1, 25), create=True)
        
        # Create one folder within the range
        df.folder(datetime.date(2024, 1, 18), create=True)
        
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 22)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 1
        
        # Check that only the folder within range is returned
        date = df.infer_date(existing_folders[0])
        assert date == datetime.date(2024, 1, 18)

    def test_existing_in_range_year_optimization(self, tmp_path):
        """Test that existing_in_range optimizes by passing year parameter when start and end are in same year."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create folders in different years
        df.folder(datetime.date(2023, 12, 31), create=True)
        df.folder(datetime.date(2024, 1, 15), create=True)
        df.folder(datetime.date(2024, 1, 16), create=True)
        df.folder(datetime.date(2025, 1, 1), create=True)
        
        # Test range within same year (should use year optimization)
        start_date = datetime.date(2024, 1, 1)
        end_date = datetime.date(2024, 12, 31)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 2
        
        # Check that only 2024 folders are returned
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert all(date.year == 2024 for date in dates)
        assert datetime.date(2024, 1, 15) in dates
        assert datetime.date(2024, 1, 16) in dates
        
        # Test range spanning different years (should not use year optimization)
        start_date = datetime.date(2023, 12, 1)
        end_date = datetime.date(2025, 1, 31)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 4  # All folders should be found
        
        # Test range within same year and month (should use both year and month optimization)
        start_date = datetime.date(2024, 1, 1)
        end_date = datetime.date(2024, 1, 31)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 2
        
        # Check that only 2024/01 folders are returned
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert all(date.year == 2024 and date.month == 1 for date in dates)
        assert datetime.date(2024, 1, 15) in dates
        assert datetime.date(2024, 1, 16) in dates
        
        # Test range within same year but different months (should use only year optimization)
        start_date = datetime.date(2024, 1, 1)
        end_date = datetime.date(2024, 3, 31)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 2  # Only 2024 folders exist
        
        # Check that only 2024 folders are returned (but from different months)
        dates = [df.infer_date(folder) for folder in existing_folders]
        assert all(date.year == 2024 for date in dates)
        assert datetime.date(2024, 1, 15) in dates
        assert datetime.date(2024, 1, 16) in dates

    def test_folders_in_range_delegation_optimization(self, tmp_path):
        """Test that folders_in_range delegates to existing_in_range when optimization is possible."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create some existing folders
        df.folder(datetime.date(2024, 1, 15), create=True)
        df.folder(datetime.date(2024, 1, 16), create=True)
        
        # Test that folders_in_range with existing_only=True delegates to existing_in_range
        # This should use the optimized traversal instead of sequential probing
        start_date = datetime.date(2024, 1, 1)
        end_date = datetime.date(2024, 1, 31)
        
        # This should trigger the delegation optimization
        folders = list(df.folders(start_date, end_date, existing_only=True))
        assert len(folders) == 2
        
        # Check that the correct folders are returned
        dates = [df.infer_date(folder) for folder in folders]
        assert datetime.date(2024, 1, 15) in dates
        assert datetime.date(2024, 1, 16) in dates
        
        # Test that folders_in_range with existing_only=False does NOT delegate
        # This should use the original sequential probing logic
        folders = list(df.folders(start_date, end_date, existing_only=False))
        assert len(folders) == 30  # Days 1-30 (exclusive of day 31)
        
        # Test with non-well-ordered pattern (should not delegate)
        df_non_ordered = DateFolders("%Y/%d/%m", root_dir=tmp_path)
        df_non_ordered.folder(datetime.date(2024, 1, 15), create=True)
        
        # This should NOT delegate due to non-well-ordered pattern
        folders = list(df_non_ordered.folders(start_date, end_date, existing_only=True))
        assert len(folders) == 1  # Only the existing folder, but using sequential probing

    def test_existing_in_range_edge_cases(self, tmp_path):
        """Test existing_in_range with edge cases."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Test with same start and end date (empty range)
        start_date = datetime.date(2024, 1, 15)
        end_date = datetime.date(2024, 1, 15)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 0
        
        # Test with single day range
        end_date = datetime.date(2024, 1, 16)
        df.folder(datetime.date(2024, 1, 15), create=True)
        
        existing_folders = list(df.existing(start_date, end_date))
        assert len(existing_folders) == 1


class TestPurgeYesterdayTolerance:
    def test_purge_skips_yesterday_within_tolerance(self, tmp_path, monkeypatch):
        import datetime as _dt
        from totodev_pub.date_folders import DateFolders
        
        # Reference anchor date (e.g., 2025-01-10)
        anchor = _dt.date(2025, 1, 10)
        yesterday = anchor - _dt.timedelta(days=1)  # 2025-01-09
        two_days_ago = anchor - _dt.timedelta(days=2)  # 2025-01-08
        
        # Create DateFolders with pattern and temp root
        df = DateFolders("%Y-%m/%d", root_dir=tmp_path)
        
        # Create folders for two days ago and yesterday
        folder_two_days_ago = df.folder(two_days_ago, create=True)
        folder_yesterday = df.folder(yesterday, create=True)
        
        # Sanity checks
        assert folder_two_days_ago.exists()
        assert folder_yesterday.exists()
        
        # Monkeypatch datetime.datetime.now() in module to simulate 10 minutes after midnight
        import totodev_pub.date_folders as df_mod
        class _FakeDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                # 00:10 on the anchor date
                return _dt.datetime(anchor.year, anchor.month, anchor.day, 0, 10, 0, tzinfo=tz)
        
        monkeypatch.setattr(df_mod.datetime, "datetime", _FakeDT)
        
        # Call purge for range [two_days_ago, anchor) which includes two_days_ago and yesterday
        # With tolerance 20 minutes, yesterday should be protected and not deleted
        df.purge(two_days_ago, anchor, anchor_date=anchor, yesterday_tolerance_minutes=20)
        
        # two_days_ago should be deleted
        assert not folder_two_days_ago.exists()
        # yesterday should remain
        assert folder_yesterday.exists()


class TestPurgeInRange:
    """Test the purge_in_range method of DateFolders."""
    
    def test_purge_in_range_basic_functionality(self, tmp_path):
        """Test basic purge functionality with simple date range."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folders
        folder1 = df.folder(datetime.date(2024, 1, 15), create=True)
        folder2 = df.folder(datetime.date(2024, 1, 16), create=True)
        folder3 = df.folder(datetime.date(2024, 1, 17), create=True)
        folder4 = df.folder(datetime.date(2024, 1, 18), create=True)
        
        # Create some files in the folders
        (folder1 / "file1.txt").write_text("test1")
        (folder2 / "file2.txt").write_text("test2")
        (folder3 / "file3.txt").write_text("test3")
        (folder4 / "file4.txt").write_text("test4")
        
        # Verify folders exist
        assert folder1.exists()
        assert folder2.exists()
        assert folder3.exists()
        assert folder4.exists()
        
        # Purge range [2024-01-15, 2024-01-17) - should delete 15 and 16, keep 17 and 18
        df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 17))
        
        # Check that target folders are deleted
        assert not folder1.exists()
        assert not folder2.exists()
        assert folder3.exists()  # Should still exist (end_date is exclusive)
        assert folder4.exists()  # Should still exist
        
        # Check that parent directories are cleaned up if empty
        month_dir = tmp_path / "2024" / "01"
        year_dir = tmp_path / "2024"
        
        # Month directory should still exist (has folders for 17 and 18)
        assert month_dir.exists()
        # Year directory should still exist
        assert year_dir.exists()
    
    def test_purge_in_range_with_empty_directory_cleanup(self, tmp_path):
        """Test that empty parent directories are properly cleaned up."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folders in different months
        folder1 = df.folder(datetime.date(2024, 1, 15), create=True)
        folder2 = df.folder(datetime.date(2024, 1, 16), create=True)
        folder3 = df.folder(datetime.date(2024, 2, 1), create=True)
        
        # Create some files
        (folder1 / "file1.txt").write_text("test1")
        (folder2 / "file2.txt").write_text("test2")
        (folder3 / "file3.txt").write_text("test3")
        
        # Verify structure exists
        month1_dir = tmp_path / "2024" / "01"
        month2_dir = tmp_path / "2024" / "02"
        year_dir = tmp_path / "2024"
        
        assert month1_dir.exists()
        assert month2_dir.exists()
        assert year_dir.exists()
        
        # Purge all of January 2024
        df.purge(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1))
        
        # January folders should be deleted
        assert not folder1.exists()
        assert not folder2.exists()
        assert folder3.exists()  # February folder should remain
        
        # January month directory should be deleted (now empty)
        assert not month1_dir.exists()
        # February month directory should remain
        assert month2_dir.exists()
        # Year directory should remain (still has February)
        assert year_dir.exists()
    
    def test_purge_in_range_with_complex_pattern(self, tmp_path):
        """Test purge functionality with complex folder patterns."""
        df = DateFolders("Project/%Y-%m/%d-%a", root_dir=tmp_path)
        
        # Create test folders
        folder1 = df.folder(datetime.date(2024, 1, 15), create=True)
        folder2 = df.folder(datetime.date(2024, 1, 16), create=True)
        folder3 = df.folder(datetime.date(2024, 2, 1), create=True)
        
        # Create some files
        (folder1 / "data.txt").write_text("test1")
        (folder2 / "data.txt").write_text("test2")
        (folder3 / "data.txt").write_text("test3")
        
        # Verify structure exists
        project_dir = tmp_path / "Project"
        month1_dir = tmp_path / "Project" / "2024-01"
        month2_dir = tmp_path / "Project" / "2024-02"
        
        assert project_dir.exists()
        assert month1_dir.exists()
        assert month2_dir.exists()
        
        # Purge all of January 2024
        df.purge(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1))
        
        # January folders should be deleted
        assert not folder1.exists()
        assert not folder2.exists()
        assert folder3.exists()  # February folder should remain
        
        # January month directory should be deleted (now empty)
        assert not month1_dir.exists()
        # February month directory should remain
        assert month2_dir.exists()
        # Project directory should remain (still has February)
        assert project_dir.exists()
    
    def test_purge_in_range_with_integer_offsets(self, tmp_path):
        """Test purge functionality using integer date offsets."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path, anchor_date=datetime.date(2024, 1, 20))
        
        # Create test folders
        folder1 = df.folder(datetime.date(2024, 1, 15), create=True)
        folder2 = df.folder(datetime.date(2024, 1, 16), create=True)
        folder3 = df.folder(datetime.date(2024, 1, 17), create=True)
        folder4 = df.folder(datetime.date(2024, 1, 18), create=True)
        
        # Create some files
        (folder1 / "file1.txt").write_text("test1")
        (folder2 / "file2.txt").write_text("test2")
        (folder3 / "file3.txt").write_text("test3")
        (folder4 / "file4.txt").write_text("test4")
        
        # Purge using integer offsets: -5 to -2 (relative to anchor date 2024-01-20)
        # This should delete folders for 2024-01-15, 2024-01-16, 2024-01-17, 2024-01-18
        df.purge(-5, -1)
        
        # Check that target folders are deleted
        assert not folder1.exists()
        assert not folder2.exists()
        assert not folder3.exists()
        assert not folder4.exists()
        
        # Check that parent directories are cleaned up
        month_dir = tmp_path / "2024" / "01"
        year_dir = tmp_path / "2024"
        
        # Month directory should be deleted (now empty)
        assert not month_dir.exists()
        # Year directory should be deleted (now empty)
        assert not year_dir.exists()
    
    def test_purge_in_range_with_custom_anchor_date(self, tmp_path):
        """Test purge functionality with custom anchor date parameter."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path, anchor_date=datetime.date(2024, 1, 20))
        
        # Create test folders
        folder1 = df.folder(datetime.date(2024, 1, 15), create=True)
        folder2 = df.folder(datetime.date(2024, 1, 16), create=True)
        folder3 = df.folder(datetime.date(2024, 1, 17), create=True)
        
        # Create some files
        (folder1 / "file1.txt").write_text("test1")
        (folder2 / "file2.txt").write_text("test2")
        (folder3 / "file3.txt").write_text("test3")
        
        # Use custom anchor date (2024-01-25) for offset calculations
        custom_anchor = datetime.date(2024, 1, 25)
        # Purge using integer offsets: -10 to -7 (relative to custom anchor)
        # This should delete folders for 2024-01-15, 2024-01-16, 2024-01-17, 2024-01-18
        df.purge(-10, -7, anchor_date=custom_anchor)
        
        # Check that target folders are deleted
        assert not folder1.exists()
        assert not folder2.exists()
        assert not folder3.exists()
    
    def test_purge_in_range_empty_range(self, tmp_path):
        """Test purge functionality with empty date range."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folder
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        (folder / "file.txt").write_text("test")
        
        # Purge with empty range (same start and end date)
        df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 15))
        
        # Folder should still exist (empty range means no dates to purge)
        assert folder.exists()
    
    def test_purge_in_range_no_folders_in_range(self, tmp_path):
        """Test purge functionality when no folders exist in the specified range."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folder outside the range
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        (folder / "file.txt").write_text("test")
        
        # Purge range that doesn't include the existing folder
        df.purge(datetime.date(2024, 1, 20), datetime.date(2024, 1, 25))
        
        # Folder should still exist
        assert folder.exists()
    
    def test_purge_in_range_preserves_root_directory(self, tmp_path):
        """Test that the root directory is never deleted during purge operations."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folder
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        (folder / "file.txt").write_text("test")
        
        # Verify structure exists
        year_dir = tmp_path / "2024"
        month_dir = tmp_path / "2024" / "01"
        
        assert year_dir.exists()
        assert month_dir.exists()
        
        # Purge the folder
        df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 16))
        
        # Folder should be deleted
        assert not folder.exists()
        # Month directory should be deleted (now empty)
        assert not month_dir.exists()
        # Year directory should be deleted (now empty)
        assert not year_dir.exists()
        # Root directory should still exist (never deleted)
        assert tmp_path.exists()
    
    def test_purge_in_range_with_subdirectories(self, tmp_path):
        """Test purge functionality when date folders contain subdirectories."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folder with subdirectories
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        subdir1 = folder / "data"
        subdir2 = folder / "logs"
        subdir1.mkdir()
        subdir2.mkdir()
        
        # Create files in subdirectories
        (subdir1 / "data.txt").write_text("data")
        (subdir2 / "log.txt").write_text("log")
        
        # Verify structure exists
        assert folder.exists()
        assert subdir1.exists()
        assert subdir2.exists()
        
        # Purge the folder
        df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 16))
        
        # Everything should be deleted
        assert not folder.exists()
        assert not subdir1.exists()
        assert not subdir2.exists()
    
    def test_purge_in_range_error_handling(self, tmp_path):
        """Test that purge operations handle errors gracefully."""
        df = DateFolders("%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folder
        folder = df.folder(datetime.date(2024, 1, 15), create=True)
        (folder / "file.txt").write_text("test")
        
        # Create a read-only file to simulate permission issues
        read_only_file = folder / "readonly.txt"
        read_only_file.write_text("readonly")
        
        # On Unix-like systems, we can make the file read-only
        # On Windows, this might not work the same way
        try:
            import os
            os.chmod(read_only_file, 0o444)  # Read-only for all users
            
            # Purge should continue even if some files can't be deleted
            df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 16))
            
            # The folder might still exist if the read-only file couldn't be deleted
            # This is expected behavior - the method should continue and not crash
            
        except (OSError, PermissionError):
            # On some systems, chmod might not work as expected
            # Just verify the method doesn't crash
            df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 16))
    
    def test_purge_in_range_with_non_well_ordered_pattern(self, tmp_path):
        """Test that purge operations fail gracefully with non-well-ordered patterns."""
        df = DateFolders("%Y/%d/%m", root_dir=tmp_path)  # Non-well-ordered pattern
        
        # This should raise a ValueError because existing_in_range requires well-ordered patterns
        with pytest.raises(ValueError, match="not well ordered"):
            df.purge(datetime.date(2024, 1, 15), datetime.date(2024, 1, 16))
    
    def test_purge_in_range_complex_directory_structure(self, tmp_path):
        """Test purge functionality with a complex multi-level directory structure."""
        df = DateFolders("Project/Data/%Y/%m/%d", root_dir=tmp_path)
        
        # Create test folders in different months and years
        folder1 = df.folder(datetime.date(2023, 12, 31), create=True)
        folder2 = df.folder(datetime.date(2024, 1, 15), create=True)
        folder3 = df.folder(datetime.date(2024, 1, 16), create=True)
        folder4 = df.folder(datetime.date(2024, 2, 1), create=True)
        folder5 = df.folder(datetime.date(2025, 1, 1), create=True)
        
        # Create some files
        (folder1 / "file1.txt").write_text("test1")
        (folder2 / "file2.txt").write_text("test2")
        (folder3 / "file3.txt").write_text("test3")
        (folder4 / "file4.txt").write_text("test4")
        (folder5 / "file5.txt").write_text("test5")
        
        # Verify structure exists
        project_dir = tmp_path / "Project"
        data_dir = tmp_path / "Project" / "Data"
        year2023_dir = tmp_path / "Project" / "Data" / "2023"
        year2024_dir = tmp_path / "Project" / "Data" / "2024"
        year2025_dir = tmp_path / "Project" / "Data" / "2025"
        month12_dir = tmp_path / "Project" / "Data" / "2023" / "12"
        month1_2024_dir = tmp_path / "Project" / "Data" / "2024" / "01"
        month2_2024_dir = tmp_path / "Project" / "Data" / "2024" / "02"
        month1_2025_dir = tmp_path / "Project" / "Data" / "2025" / "01"
        
        assert all(d.exists() for d in [project_dir, data_dir, year2023_dir, year2024_dir, year2025_dir,
                                       month12_dir, month1_2024_dir, month2_2024_dir, month1_2025_dir])
        
        # Purge all of January 2024
        df.purge(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1))
        
        # January 2024 folders should be deleted
        assert not folder2.exists()
        assert not folder3.exists()
        
        # Other folders should remain
        assert folder1.exists()  # December 2023
        assert folder4.exists()  # February 2024
        assert folder5.exists()  # January 2025
        
        # January 2024 month directory should be deleted (now empty)
        assert not month1_2024_dir.exists()
        
        # Year 2024 directory should remain (still has February)
        assert year2024_dir.exists()
        
        # Other directories should remain
        assert year2023_dir.exists()
        assert year2025_dir.exists()
        assert month12_dir.exists()
        assert month2_2024_dir.exists()
        assert month1_2025_dir.exists()


# Fixtures
@pytest.fixture
def tmp_path():
    """Create a temporary directory for testing."""
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmpdirname:
        yield Path(tmpdirname)
