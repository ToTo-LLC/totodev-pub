# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Test plugins package for connection testing

This module provides functionality for discovering and loading connection test plugins.
"""

import importlib.util
from pathlib import Path
from typing import List, Type

def discover_available_tests(plugin_dir: Path) -> List[str]:
    """Scan for available connection test files and extract shortnames"""
    test_files = []
    for test_file in plugin_dir.glob("conntest_*.py"):
        if test_file.name != "__init__.py":
            # Extract shortname: conntest_https.py -> https
            shortname = test_file.stem[9:]  # Remove "conntest_" prefix
            test_files.append(shortname)
    return sorted(test_files)


def load_test_class(shortname: str) -> Type['TestTypeBase']:
    """
    Load a specific connection test class only when needed.

    This uses a flexible detection strategy so that plugins which import
    TestTypeBase via slightly different module paths still work. Rather than
    requiring identity equality with a particular TestTypeBase object, we
    look for classes whose MRO includes a base class named 'TestTypeBase'
    and which are defined in the target plugin module.
    """
    plugin_dir = Path(__file__).parent
    test_file = plugin_dir / f"conntest_{shortname}.py"

    if not test_file.exists():
        raise ValueError(f"Test '{shortname}' not found (looked for conntest_{shortname}.py)")

    # Dynamic import and class discovery
    spec = importlib.util.spec_from_file_location(f"conntest_{shortname}", test_file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None  # for type checkers
    spec.loader.exec_module(module)

    # Find a concrete TestTypeBase subclass defined in this module.
    # We intentionally detect by base-class NAME to avoid issues where
    # plugins import TestTypeBase via a different module path than the
    # one used here.
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if not isinstance(attr, type):
            continue

        # Skip the base class itself if it happens to be present
        if attr.__name__ == 'TestTypeBase':
            continue

        # Require that the class is defined in this module
        if attr.__module__ != module.__name__:
            continue

        # Check for a base class named 'TestTypeBase' anywhere in the MRO
        has_test_type_base = any(base.__name__ == 'TestTypeBase' for base in attr.__mro__)
        if not has_test_type_base:
            continue

        return attr

    raise ValueError(f"No TestTypeBase subclass found in conntest_{shortname}.py")


__all__ = ['discover_available_tests', 'load_test_class']