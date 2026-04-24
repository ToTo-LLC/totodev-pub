# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
from datetime import date, datetime, timedelta
import os
from pathlib import Path
from totodev_pub.minor.date_tree_folder import DateTreeFolder

@pytest.fixture
def root_dir(tmp_path):
    """Create a temporary directory for testing"""
    yield tmp_path

@pytest.fixture
def sample_folders(root_dir):
    """Create some sample date tree folders"""
    folders = []
    test_date = date(2024, 2, 15)
    
    try:
        # Create folders for category1
        for i in range(3):
            folder = DateTreeFolder(category_name="category1", 
                                  uniqueness_src=f"test{i}",
                                  dte=test_date,
                                  ultimate_root=str(root_dir))
            folders.append(folder)
        
        # Create folders for category2
        for i in range(2):
            folder = DateTreeFolder(category_name="category2",
                                  uniqueness_src=f"other{i}",
                                  dte=test_date + timedelta(days=i),
                                  ultimate_root=str(root_dir))
            folders.append(folder)
        yield folders
    finally:
        # Clean up all created folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_init_new_folder(root_dir):
    folder = None
    try:
        folder = DateTreeFolder(category_name="test_category",
                              uniqueness_src="test1",
                              ultimate_root=str(root_dir))
        
        assert folder.path is not None
        assert folder.category_name == "test_category"
        assert folder.uniqueness_src == "test1"
        assert folder.ultimate_root == str(root_dir)
    finally:
        if folder and folder.path.exists():
            folder.path.rmdir()

def test_init_existing_folder(root_dir, sample_folders):
    """Test initializing with an existing folder path"""
    existing_path = str(sample_folders[0].path)
    folder = DateTreeFolder(existing_folder_path=existing_path)
    
    assert str(folder.path) == existing_path
    assert folder.category_name == "category1"
    assert folder.uniqueness_src == "test0"
    assert folder.date == date(2024, 2, 15)

def test_init_validation(root_dir):
    # Test absolute path validation
    with pytest.raises(ValueError, match="ultimate_root must be provided"):
        DateTreeFolder(category_name="test")

    # Test missing required arguments
    with pytest.raises(ValueError, match="Must provide either a category_name"):
        DateTreeFolder()

    # Test conflicting arguments
    with pytest.raises(ValueError, match="Cannot provide existing_folder_path"):
        DateTreeFolder(existing_folder_path="/some/path",
                      category_name="test")

    # Test non-existent folder
    with pytest.raises(ValueError, match="does not exist"):
        DateTreeFolder(existing_folder_path="/nonexistent/path")

    # Test absolute path in category_name
    with pytest.raises(ValueError, match="category_name cannot be an absolute path"):
        DateTreeFolder(category_name="/absolute/path",
                      ultimate_root=str(root_dir))

def test_folder_uniqueness(root_dir):
    """Test that folders with same parameters get unique names"""
    folders = []
    for _ in range(3):
        folder = DateTreeFolder(category_name="same_category",
                              uniqueness_src="same_source",
                              ultimate_root=str(root_dir))
        folders.append(folder)
    
    # Check that all paths are unique
    paths = [f.path for f in folders]
    assert len(set(paths)) == len(paths)

def test_types_on_date(root_dir, sample_folders):
    """Test getting category types for a specific date"""
    test_date = date(2024, 2, 15)
    types = DateTreeFolder.types_on_date(test_date, ultimate_root=str(root_dir))
    assert "category1" in types
    assert "category2" in types

def test_instances_on_date(root_dir, sample_folders):
    """Test getting instances for a specific date and category"""
    test_date = date(2024, 2, 15)
    instances = list(DateTreeFolder.instances_on_date(test_date, "category1", ultimate_root=str(root_dir)))
    assert len(instances) == 3
    assert "test0" in instances
    assert "test1" in instances
    assert "test2" in instances

def test_active_dates(root_dir, sample_folders):
    """Test getting active dates within a range"""
    start_date = date(2024, 2, 15)
    end_date = date(2024, 2, 16)
    
    # Get all dates with any category
    active_dates = list(DateTreeFolder.active_dates(start_date, ultimate_root=str(root_dir), end_date=end_date))
    assert len(active_dates) == 2
    assert start_date in active_dates
    assert end_date in active_dates
    
    # Get dates for specific category
    category1_dates = list(DateTreeFolder.active_dates(start_date, ultimate_root=str(root_dir),
                                                     end_date=end_date,
                                                     category_name="category1"))
    assert len(category1_dates) == 1
    assert category1_dates[0] == start_date

def test_active_dates_empty_directory(tmp_path):
    """Test active_dates behavior with empty directories and None begin_date"""
    # Test with completely empty directory
    empty_dates = list(DateTreeFolder.active_dates(None, ultimate_root=str(tmp_path)))
    assert len(empty_dates) == 0

    # Create a directory structure but with no valid YYYY-MM folders
    os.makedirs(os.path.join(tmp_path, "invalid-folder"))
    os.makedirs(os.path.join(tmp_path, "2024-XX"))  # Invalid month format
    empty_dates = list(DateTreeFolder.active_dates(None, ultimate_root=str(tmp_path)))
    assert len(empty_dates) == 0

def test_active_dates_none_begin_date(root_dir, sample_folders):
    """Test active_dates behavior when begin_date is None"""
    # Get all dates with None begin_date
    active_dates = list(DateTreeFolder.active_dates(None, ultimate_root=str(root_dir)))
    
    # Should find existing dates in the directory structure
    assert len(active_dates) > 0
    
    # The test data only has entries on Feb 15 and Feb 16
    # The earliest date should be Feb 15, not Feb 1
    earliest_date = min(active_dates)
    assert earliest_date.year == 2024
    assert earliest_date.month == 2
    # The test fixture only creates folders for Feb 15 and 16, not for Feb 1
    assert earliest_date.day in (15, 16)  # Either 15 or 16 depending on which is found first

    # Test with category filter
    category1_dates = list(DateTreeFolder.active_dates(None, 
                                                     ultimate_root=str(root_dir),
                                                     category_name="category1"))
    assert len(category1_dates) > 0
    # category1 only exists on Feb 15 in our test fixture
    assert all(d.day == 15 and d.month == 2 and d.year == 2024 for d in category1_dates)

def test_purge_by_date_specific_range(root_dir):
    """Test purging folders within a specific date range"""
    folders = []
    try:
        # Create test folders for different dates
        dates = [
            date(2023, 12, 31),
            date(2024, 1, 1),
            date(2024, 1, 15),
            date(2024, 2, 1),
        ]
        
        for dte in dates:
            folder = DateTreeFolder(
                category_name="test_category",
                uniqueness_src="test1",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
            
        # Purge folders for January 2024
        begin_date = date(2024, 1, 1)
        end_date = date(2024, 1, 31)
        DateTreeFolder.purge_by_date(begin_date, str(root_dir), end_date)
        
        # Check that only January folders are deleted
        assert folders[0].path.exists()      # 2023-12-31 should remain
        assert not folders[1].path.exists()  # 2024-01-01 should be gone
        assert not folders[2].path.exists()  # 2024-01-15 should be gone
        assert folders[3].path.exists()      # 2024-02-01 should remain
        
        # Check that January month directory is removed
        jan_dir = os.path.join(str(root_dir), "2024-01")
        assert not os.path.exists(jan_dir)
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_purge_by_date_none_params(root_dir):
    """Test purging folders with None parameters"""
    folders = []
    try:
        # Create test folders for different dates
        dates = [
            date(2023, 12, 31),
            date(2024, 1, 1),
            date(2024, 1, 15),
        ]
        
        for dte in dates:
            folder = DateTreeFolder(
                category_name="test_category",
                uniqueness_src="test1",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
            
        # Test with None begin_date (should use earliest date)
        DateTreeFolder.purge_by_date(None, str(root_dir), date(2023, 12, 31))
        
        # Only 2023-12-31 should be deleted
        assert not folders[0].path.exists()  # 2023-12-31 should be gone
        assert folders[1].path.exists()      # 2024-01-01 should remain
        assert folders[2].path.exists()      # 2024-01-15 should remain
        
        # Test with None end_date (should use today)
        today = date.today()
        # Only run this part of the test if today is after our test dates
        if today > date(2024, 1, 15):
            DateTreeFolder.purge_by_date(date(2024, 1, 1), str(root_dir), None)
            
            # All remaining folders should be gone
            assert not folders[1].path.exists()  # 2024-01-01 should be gone
            assert not folders[2].path.exists()  # 2024-01-15 should be gone
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_purge_by_date_empty_directory(tmp_path):
    """Test purging an empty directory"""
    # Should not raise any errors
    DateTreeFolder.purge_by_date(None, str(tmp_path), None)
    DateTreeFolder.purge_by_date(date(2024, 1, 1), str(tmp_path), date(2024, 1, 31))

def test_purge_by_date_affected_directories(root_dir):
    """Test that purge_by_date only checks year-month directories that had folders deleted"""
    folders = []
    try:
        # Create test folders for different dates
        dates = [
            date(2024, 1, 15),  # Will be purged
            date(2024, 2, 15),  # Will remain
        ]
        
        for dte in dates:
            folder = DateTreeFolder(
                category_name="test_category",
                uniqueness_src="test1",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
            
        # Create an empty month directory that should not be checked
        empty_month_dir = os.path.join(str(root_dir), "2024-03")
        os.makedirs(empty_month_dir, exist_ok=True)
        
        # Purge January only
        begin_date = date(2024, 1, 1)
        end_date = date(2024, 1, 31)
        
        # Instead of mocking os.rmdir, we'll check which directories exist before and after
        jan_dir = os.path.join(str(root_dir), "2024-01")
        feb_dir = os.path.join(str(root_dir), "2024-02")
        mar_dir = os.path.join(str(root_dir), "2024-03")
        
        # Verify all directories exist before purging
        assert os.path.exists(jan_dir)
        assert os.path.exists(feb_dir)
        assert os.path.exists(mar_dir)
        
        # Perform the purge
        DateTreeFolder.purge_by_date(begin_date, str(root_dir), end_date)
        
        # January directory should be gone (it was empty after purging day folders)
        assert not os.path.exists(jan_dir)
        
        # February and March directories should still exist
        assert os.path.exists(feb_dir)
        assert os.path.exists(mar_dir)
        
        # Clean up the remaining directories
        if os.path.exists(empty_month_dir):
            os.rmdir(empty_month_dir)
            
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_path_properties(root_dir):
    """Test path-related properties"""
    folder = DateTreeFolder(category_name="test_category",
                          uniqueness_src="test1",
                          ultimate_root=str(root_dir))
    
    assert isinstance(folder.ultimate_root, str)
    assert isinstance(folder.path, Path)
    assert isinstance(folder.abspath, str)
    assert Path(folder.abspath).is_absolute()
    assert folder.path.is_absolute() == Path(folder.abspath).is_absolute()

def test_delete_folders(root_dir):
    """Test deleting folders based on date criteria"""
    folders = []
    try:
        # Create test folders for different dates
        dates = [
            date(2023, 12, 31),
            date(2024, 1, 1),
            date(2024, 1, 2),
            date(2024, 2, 1),
        ]
        
        for dte in dates:
            folder = DateTreeFolder(
                category_name="test_category",
                uniqueness_src="test1",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
            
        # Test deleting specific day
        DateTreeFolder.delete_folders(str(root_dir), year=2024, month=1, day=1)
        assert not folders[1].path.exists()  # 2024-01-01 should be gone
        assert folders[0].path.exists()      # 2023-12-31 should remain
        assert folders[2].path.exists()      # 2024-01-02 should remain
        assert folders[3].path.exists()      # 2024-02-01 should remain
        
        # Test deleting entire month
        DateTreeFolder.delete_folders(str(root_dir), year=2024, month=1)
        assert not folders[2].path.exists()  # 2024-01-02 should now be gone
        assert folders[0].path.exists()      # 2023-12-31 should still remain
        assert folders[3].path.exists()      # 2024-02-01 should still remain
        
        # Test deleting entire year
        DateTreeFolder.delete_folders(str(root_dir), year=2024)
        assert not folders[3].path.exists()  # 2024-02-01 should now be gone
        assert folders[0].path.exists()      # 2023-12-31 should still remain
        
        # Test validation
        with pytest.raises(ValueError, match="Month must be specified if day is specified"):
            DateTreeFolder.delete_folders(str(root_dir), year=2024, day=1)
            
        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            DateTreeFolder.delete_folders(str(root_dir), year=2024, month=13)
            
        with pytest.raises(ValueError, match="Day must be between 1 and 31"):
            DateTreeFolder.delete_folders(str(root_dir), year=2024, month=1, day=32)
        
        # Test deleting non-existent folders (should not raise error)
        DateTreeFolder.delete_folders(str(root_dir), year=2025)
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

