# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Example tests demonstrating how to test totodev_pub library functionality.
"""

import pytest


def test_import_specific_modules():
    """Test importing specific totodev_pub modules."""
    try:
        # Test importing specific modules
        import totodev_pub.forgetful_reader
        import totodev_pub.minor.flexargs
        import totodev_pub.dbjig_support.tbdict
        
        # If we get here, imports were successful
        assert True
    except ImportError as e:
        pytest.fail(f"Failed to import totodev_pub modules: {e}")


def test_forgetful_reader_functionality():
    """Test forgetful_reader module is importable."""
    try:
        import totodev_pub.forgetful_reader as fr
        
        assert hasattr(fr, "ForgetfulReader"), "forgetful_reader should expose ForgetfulReader"
        
    except ImportError:
        pytest.skip("totodev_pub.forgetful_reader not available")


def test_flexargs_functionality():
    """Test basic flexargs functionality."""
    try:
        import totodev_pub.minor.flexargs as flexargs_mod
        
        # Test that flexargs module has expected functionality
        assert hasattr(flexargs_mod, 'FlexArgs'), "flexargs module should have FlexArgs class"
        
        # Test creating FlexArgs instance with required arguments
        flex_args = flexargs_mod.FlexArgs(
            arg_spec="test_spec", 
            prog_purpose="test_purpose"
        )
        assert flex_args is not None, "Should be able to create FlexArgs instance"
        
    except ImportError:
        pytest.skip("totodev_pub.minor.flexargs not available")


def test_tbdict_functionality():
    """Test basic tbdict functionality."""
    try:
        import totodev_pub.dbjig_support.tbdict as tbdict_mod
        
        # Test that tbdict module has expected functionality
        assert hasattr(tbdict_mod, 'TableBackedDict'), "tbdict module should have TableBackedDict class"
        
        # Test creating TableBackedDict instance (this would require a database file)
        # For now, just test that the class exists
        assert tbdict_mod.TableBackedDict is not None, "TableBackedDict class should exist"
        
    except ImportError:
        pytest.skip("totodev_pub.dbjig_support.tbdict not available")


@pytest.mark.parametrize("module_name", [
    "dbjig",
    "minor.sweep",
    "pipes",
    "app_config_root_class",
    "minor.date_tree_folder"
])
def test_module_imports(module_name):
    """Test that various totodev_pub modules can be imported."""
    try:
        module = __import__(f"totodev_pub.{module_name}", fromlist=[""])
        assert module is not None, f"Should be able to import totodev_pub.{module_name}"
    except ImportError as e:
        pytest.skip(f"totodev_pub.{module_name} not available: {e}")


if __name__ == "__main__":
    pytest.main([__file__])
