# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Alignment and smoke tests for FolderBackedCaseInterface."""

import inspect

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.folder_backed_case_interface import (
    FolderBackedCaseInterface,
)


def test_interface_alignment():
    FolderBackedCase._assert_interface_alignment()


def test_interface_is_an_ancestor():
    assert FolderBackedCaseInterface in FolderBackedCase.__mro__


def test_docstring_inherits_from_interface():
    doc = inspect.getdoc(FolderBackedCase.case_advance)
    assert doc
    assert doc == inspect.getdoc(FolderBackedCaseInterface.case_advance)
