# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Illustrative ``FolderBackedCase`` subclasses for design review and docs.

These modules are not imported by ``folder_backed_case_support`` itself — import
a specific example when you need it (e.g. for a case briefing or workbench
session).
"""

from totodev_pub.folder_backed_case_support.case_examples.indexable_file_case import (
    IndexableFileCase,
)

__all__ = ["IndexableFileCase"]
