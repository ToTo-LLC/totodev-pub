# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import os
import tempfile
from pathlib import Path
import pytest
from totodev_pub.minor.sweep import scan_for_issues, ALLOWED_CAPITAL_FILENAMES, ALLOWED_SYSTEM_PATHS

def create_test_files(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Helper to create test files with known issues"""
    # Create a Python file with a breakpoint
    py_file = tmp_path / "test.py"
    py_file.write_text("""
def some_function():
    # This is fine
    breakpoint()  # This should be caught
    x = 1  # DEBUG: this should be caught
    # FIXME: this should be caught
    # # DIAG: this should not be caught
""")

    # Create a YAML file with debug comments
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("""
service:
  name: myapp
  # DEBUG: need to fix this
  port: 8080
""")
    
    # Create a file with class name issues
    class_file = tmp_path / "classes.py"
    class_file.write_text("""
class GoodClassName:
    pass

class badClassName:  # Should be caught
    pass

class Good_But_With_Underscores:  # Should be caught
    pass

class _PrivateClass:  # Should be fine
    pass

class Test_Class:  # Should be caught (not in a test file)
    pass
""")

    # Create a file with hardcoded paths
    paths_file = tmp_path / "paths.py"
    paths_file.write_text(f'''
# This is fine - in a constant
ROOT_PATH = "/usr/local/myapp"

def some_function():
    # This should be caught
    path = "/usr/local/myapp/data"
    
    # This should be fine - allowed system path
    path2 = "/usr/local/bin"
    
    return path
''')
    
    # Create a .gitignore file
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.log\n*.tmp\n")
    
    return py_file, yaml_file, class_file, paths_file, gitignore

def test_scan_for_issues_finds_all_issue_types(tmp_path: Path):
    """Test that scan_for_issues finds breakpoints and debug comments"""
    py_file, yaml_file, class_file, paths_file, _ = create_test_files(tmp_path)
    
    issues = list(scan_for_issues(str(tmp_path)))
    
    # Should find:
    # - 3 issues in Python file (breakpoint, DEBUG, FIXME)
    # - 1 issue in YAML file (DEBUG)
    # - 3 issues in class file (bad class names)
    # - 1 issue in paths file (hardcoded path)
    assert len(issues) == 8
    
    # Verify breakpoint detection
    breakpoint_issues = [i for i in issues if 'breakpoint()' in i]
    assert len(breakpoint_issues) == 1
    assert str(py_file) in breakpoint_issues[0]
    
    # Verify DEBUG comment detection
    debug_issues = [i for i in issues if 'DEBUG' in i]
    assert len(debug_issues) == 2
    
    # Verify FIXME comment detection
    fixme_issues = [i for i in issues if 'FIXME' in i]
    assert len(fixme_issues) == 1
    
    # Verify class name issues
    class_issues = [i for i in issues if 'class name' in i.lower()]
    assert len(class_issues) == 3
    assert any('badClassName' in i for i in class_issues)  # Not capitalized
    assert any('Good_But_With_Underscores' in i for i in class_issues)  # Has underscores
    assert any('Test_Class' in i for i in class_issues)  # Test_ prefix not in test file
    
    # Verify hardcoded path detection
    path_issues = [i for i in issues if 'hardcoded absolute filepath' in i.lower()]
    assert len(path_issues) == 1
    assert '/usr/local/myapp/data' in path_issues[0]  # Only the non-allowed path should be caught
    assert not any('/usr/local/bin' in i for i in path_issues)  # The allowed path should not be caught

def test_filename_case_checks(tmp_path: Path):
    """Test that scan_for_issues correctly handles filename case checks"""
    # Create files with different naming patterns
    (tmp_path / "lowercase.py").write_text("# This is fine")
    (tmp_path / "Mixed_Case.py").write_text("# This should be caught")
    (tmp_path / "README.md").write_text("# This should be allowed")
    
    issues = list(scan_for_issues(str(tmp_path)))
    
    # Should only find one issue with Mixed_Case.py
    case_issues = [i for i in issues if 'capital letters' in i.lower()]
    assert len(case_issues) == 1
    assert 'Mixed_Case.py' in case_issues[0]

def test_scan_for_issues_respects_gitignore(tmp_path: Path):
    """Test that scan_for_issues respects .gitignore patterns"""
    create_test_files(tmp_path)
    
    # Create an ignored file
    ignored_file = tmp_path / "test.log"
    ignored_file.write_text("# DEBUG: this should be ignored\n")
    
    issues = list(scan_for_issues(str(tmp_path)))
    
    # Verify no issues from ignored file are reported
    assert not any('test.log' in issue for issue in issues)

def test_scan_for_issues_handles_invalid_files(tmp_path: Path):
    """Test that scan_for_issues handles unreadable or invalid files gracefully"""
    # Create an unreadable file
    bad_file = tmp_path / "test.py"
    bad_file.write_text("# DEBUG: test\n")
    bad_file.chmod(0o000)  # Remove read permissions
    
    issues = list(scan_for_issues(str(tmp_path)))
    
    # Should get an error message about being unable to read the file
    assert any('Error reading file' in issue for issue in issues)
    
    # Cleanup
    bad_file.chmod(0o644)  # Restore permissions for cleanup

def test_scan_for_issues_with_empty_directory(tmp_path: Path):
    """Test that scan_for_issues handles empty directories correctly"""
    issues = list(scan_for_issues(str(tmp_path)))
    assert len(issues) == 0

def test_config_file_path_exceptions(tmp_path: Path):
    """Test that hardcoded paths in config files are allowed"""
    config_file = tmp_path / "app_config.py"
    config_file.write_text('''
# This should be allowed because it's in a config file
DEFAULT_PATH = "/usr/local/app/data"
''')
    
    issues = list(scan_for_issues(str(tmp_path)))
    assert len(issues) == 0

def test_private_class_names(tmp_path: Path):
    """Test that private class names are allowed to break convention"""
    test_file = tmp_path / "private.py"
    test_file.write_text('''
class _lower_case_private:
    pass

class _Mixed_Case_Private:
    pass

class public_class:  # This should be caught
    pass
''')
    
    issues = list(scan_for_issues(str(tmp_path)))
    
    # Should only catch the public class
    class_issues = [i for i in issues if 'class name' in i.lower()]
    assert len(class_issues) == 1
    assert 'public_class' in class_issues[0] 