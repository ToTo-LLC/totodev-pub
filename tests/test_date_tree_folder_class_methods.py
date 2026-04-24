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

def test_apply_retention_policy_class_method(root_dir):
    """Test that the class method apply_retention_policy works correctly"""
    folders = []
    try:
        # Use explicit dates instead of relative dates to ensure consistency
        today = date(2025, 2, 25)  # Use a fixed date for testing
        yesterday = date(2025, 2, 24)
        two_days_ago = date(2025, 2, 23)
        
        # Create folders for testing deletion
        for dte in [today, yesterday, two_days_ago]:
            folder = DateTreeFolder(
                category_name="retention_test",
                uniqueness_src=f"test_{dte.isoformat()}",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
        
        # Verify all folders exist before applying retention policy
        for folder in folders:
            assert folder.path.exists()
        
        # Print debug info
        print(f"Today: {today}, Yesterday: {yesterday}, Two days ago: {two_days_ago}")
        print(f"Cutoff date: {today - timedelta(days=1)}")
        
        # Apply retention policy to keep only today and yesterday
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=1,  # Only retain today and yesterday
            reference_date=today,
            force=True
        )
        
        # Check if the folder was deleted
        two_days_ago_exists = folders[2].path.exists()
        print(f"Two days ago folder still exists: {two_days_ago_exists}")
        if two_days_ago_exists:
            # List contents of the directory to debug
            day_path = folders[2].path.parent
            if day_path.exists():
                print(f"Contents of day path: {os.listdir(day_path)}")
        
        assert not folders[2].path.exists()  # two_days_ago should be gone
        assert folders[1].path.exists()      # yesterday should remain
        assert folders[0].path.exists()      # today should remain
        
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_retention_policy_runs_once_per_day(root_dir):
    """Test that apply_retention_policy only runs once per day unless forced"""
    folders = []
    try:
        # Use explicit dates instead of relative dates to ensure consistency
        today = date(2025, 2, 25)  # Use a fixed date for testing
        yesterday = date(2025, 2, 24)
        two_days_ago = date(2025, 2, 23)
        
        # Create folders for testing deletion
        for dte in [today, yesterday, two_days_ago]:
            folder = DateTreeFolder(
                category_name="retention_test",
                uniqueness_src=f"test_{dte.isoformat()}",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
        
        # Verify all folders exist before applying retention policy
        for folder in folders:
            assert folder.path.exists()
        
        # First run should delete the two_days_ago folder
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=1,  # Only retain today and yesterday
            reference_date=today,
            force=True
        )
        
        # Check if the folder was deleted
        assert not folders[2].path.exists()  # two_days_ago should be gone
        assert folders[1].path.exists()      # yesterday should remain
        assert folders[0].path.exists()      # today should remain
        
        # Create another folder for two days ago to test second run
        another_old_folder = DateTreeFolder(
            category_name="retention_test",
            uniqueness_src="another_old",
            ultimate_root=str(root_dir),
            dte=two_days_ago
        )
        folders.append(another_old_folder)
        
        # Verify the new folder exists
        assert another_old_folder.path.exists()
        
        # Second run on same day should do nothing
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=1,
            reference_date=today,
            force=False  # Not forced
        )
        assert another_old_folder.path.exists()  # Should still exist because policy wasn't reapplied
        
        # Force run should delete the folder even on same day
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=1,
            reference_date=today,
            force=True
        )
        assert not another_old_folder.path.exists()  # Should be gone after forced run
        
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_apply_retention_policy_different_periods(root_dir):
    """Test that apply_retention_policy correctly handles different retention periods"""
    folders = []
    try:
        # Use explicit dates for testing
        today = date(2025, 3, 15)
        dates = [
            today,                                # Today
            today - timedelta(days=1),            # Yesterday
            today - timedelta(days=2),            # 2 days ago
            today - timedelta(days=3),            # 3 days ago
            today - timedelta(days=7),            # 7 days ago
            today - timedelta(days=30),           # 30 days ago
        ]
        
        # Create folders for each date with the same category
        for i, dte in enumerate(dates):
            folder = DateTreeFolder(
                category_name="retention_period_test",
                uniqueness_src=f"test_{i}",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
        
        # Verify all folders exist
        for folder in folders:
            assert folder.path.exists()
        
        # Test with retain_days=0 (only keep today)
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=0,  # Only retain today
            reference_date=today,
            force=True
        )
        
        # Check that only today's folder remains
        assert folders[0].path.exists()      # Today should remain
        for i in range(1, 6):
            assert not folders[i].path.exists()  # All other days should be gone
        
        # Recreate folders for each date
        folders = folders[:1]  # Keep only today's folder
        for i, dte in enumerate(dates[1:], 1):
            folder = DateTreeFolder(
                category_name="retention_period_test",
                uniqueness_src=f"test_{i}_recreated",
                ultimate_root=str(root_dir),
                dte=dte
            )
            folders.append(folder)
        
        # Test with retain_days=2 (keep today and 2 days back)
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=2,  # Retain today and 2 days back
            reference_date=today,
            force=True
        )
        
        # Check that today, yesterday, and 2 days ago remain
        assert folders[0].path.exists()      # Today should remain
        assert folders[1].path.exists()      # Yesterday should remain
        assert folders[2].path.exists()      # 2 days ago should remain
        for i in range(3, 6):
            assert not folders[i].path.exists()  # Older days should be gone
        
        # Test with retain_days=None (no retention policy)
        # Create a new set of folders in a different category to test the None behavior
        none_test_folders = []
        old_date = today - timedelta(days=10)  # An old date
        
        # Create a folder with the old date
        old_folder = DateTreeFolder(
            category_name="none_retention_test",  # Different category
            uniqueness_src="old_test",
            ultimate_root=str(root_dir),
            dte=old_date
        )
        none_test_folders.append(old_folder)
        
        # Verify the old folder exists
        assert old_folder.path.exists()
        
        # Apply retention policy with None retain_days
        DateTreeFolder.apply_retention_policy(
            ultimate_root=str(root_dir),
            retain_days=None,  # No retention policy
            reference_date=today,
            force=True
        )
        
        # Check that the old folder still exists (no deletion should occur)
        assert old_folder.path.exists(), "Folder was unexpectedly deleted with retain_days=None"
        
        # Clean up the none_test_folders
        for folder in none_test_folders:
            if folder.path.exists():
                folder.path.rmdir()
            
    finally:
        # Clean up any remaining folders
        for folder in folders:
            if folder.path.exists():
                folder.path.rmdir()

def test_make_folder_factory(root_dir):
    """Test that the make_folder_factory method creates a working factory function."""
    try:
        # Create a factory for test folders with a 1-day retention policy
        test_factory = DateTreeFolder.make_folder_factory(
            ultimate_root=str(root_dir),
            category_name="factory_test",
            retain_days=1
        )
        
        # Create folders using the factory
        folder1 = test_factory()  # Default uniqueness
        folder2 = test_factory(uniqueness_src="custom_unique")
        
        # Verify the folders were created correctly
        assert folder1.path.exists()
        assert folder2.path.exists()
        assert folder1.category_name == "factory_test"
        assert folder2.category_name == "factory_test"
        assert folder1.ultimate_root == str(root_dir)
        assert folder2.ultimate_root == str(root_dir)
        assert folder2.uniqueness_src == "custom_unique"
        
        # Verify the folders have today's date
        today = date.today()
        assert folder1.date == today
        assert folder2.date == today
        
        # Test retention policy application
        # Create a folder with yesterday's date that should be removed by retention policy
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)
        
        old_folder1 = DateTreeFolder(
            category_name="factory_test",
            uniqueness_src="old_test1",
            ultimate_root=str(root_dir),
            dte=yesterday
        )
        
        old_folder2 = DateTreeFolder(
            category_name="factory_test",
            uniqueness_src="old_test2",
            ultimate_root=str(root_dir),
            dte=two_days_ago
        )
        
        # Verify old folders exist
        assert old_folder1.path.exists()
        assert old_folder2.path.exists()
        
        # Reset the last retention run date to force a new run
        DateTreeFolder._last_retention_run_dates = {}
        
        # Create another folder using the factory, which should trigger retention policy
        folder3 = test_factory(uniqueness_src="trigger_retention")
        
        # Verify the new folder exists
        assert folder3.path.exists()
        
        # Verify retention policy was applied - yesterday's folder should still exist,
        # but two_days_ago folder should be deleted
        assert old_folder1.path.exists(), "Yesterday's folder should not be deleted with retain_days=1"
        assert not old_folder2.path.exists(), "Two days ago folder should be deleted with retain_days=1"
        
        # Test factory with no retention policy
        no_retention_factory = DateTreeFolder.make_folder_factory(
            ultimate_root=str(root_dir),
            category_name="no_retention_test",
            retain_days=None
        )
        
        # Create a folder with the no-retention factory
        no_retention_folder = no_retention_factory()
        
        # Verify the folder exists
        assert no_retention_folder.path.exists()
        
        # Clean up
        for folder in [folder1, folder2, folder3, old_folder1, no_retention_folder]:
            if folder.path.exists():
                folder.path.rmdir()
                
    except Exception as e:
        # Clean up even if test fails
        for path in Path(root_dir).glob("**/*"):
            if path.is_dir() and "factory_test" in str(path) or "no_retention_test" in str(path):
                try:
                    path.rmdir()
                except:
                    pass
        raise e 

def test_factory_with_retention(root_dir):
    """Test the factory with retention policy."""
    # Create folders for different dates
    old_folder = DateTreeFolder(
        category_name="retention_test",
        uniqueness_src="old",
        ultimate_root=str(root_dir),
        dte=date.today() - timedelta(days=10)
    )
    
    # Create a factory with retention policy
    factory = DateTreeFolder.make_folder_factory(
        ultimate_root=str(root_dir),
        category_name="retention_test",
        retain_days=7  # Keep only 7 days
    )
    
    # Create a new folder using the factory
    # This should trigger the retention policy
    new_folder = factory()
    
    # Verify the old folder was deleted
    assert not old_folder.path.exists()
    
    # Verify the new folder was created
    assert new_folder.path.exists()
    
    # Clean up
    if new_folder.path.exists():
        new_folder.path.rmdir() 