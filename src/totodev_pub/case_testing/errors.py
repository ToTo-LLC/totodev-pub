# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Pedagogical errors for CaseWorkbench — hard misuse, not structure deviations."""

from __future__ import annotations


class WorkbenchError(Exception):
    """Hard workbench misuse with remediation in the message.

    Structure / layout issues are reported via ``doctor()`` findings instead —
    those never raise. Use this for missing project context, ambiguous globs,
    missing paths, conflicting kwargs, and similar operator errors.
    """
