# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Test code quality standards within the totodev_pub package."""

from pathlib import Path

import pytest

from totodev_pub.minor.sweep import scan_for_issues

_ALLOWLIST_PATHS = [
    Path("cli/conn_tester.py"),
    Path("cli/conn_tester_support/logfile_manager.py"),
]


def test_totodev_code_quality() -> None:
    """
    Scan the totodev_pub package for code quality issues.

    This test ensures that the totodev_pub package itself follows our code quality standards,
    checking for:
    - DEBUG/FIXME/DIAG comments
    - Naked breakpoint() calls
    - Python filenames with capital letters
    - Hardcoded absolute filepaths
    - Non-CamelCase class names
    """
    package_root = Path(__file__).resolve().parent.parent / "src" / "totodev_pub"

    issues = list(scan_for_issues(str(package_root)))

    filtered = [
        issue
        for issue in issues
        if _should_flag_issue(issue, package_root)
    ]

    if filtered:
        issue_details = "\n".join(f"- {issue}" for issue in filtered)
        pytest.fail(
            "Found code quality issues in totodev_pub package:\n"
            f"{issue_details}\n\n"
            "Please fix these issues to maintain code quality standards."
        )

    assert not filtered, "No code quality issues should remain after filtering."


def _should_flag_issue(issue: str, package_root: Path) -> bool:
    """Determine whether a reported issue should fail the test."""
    raw_path = Path(issue.split(":", 1)[0])

    try:
        rel_path = raw_path.resolve().relative_to(package_root.resolve())
    except ValueError:
        rel_path = raw_path

    if any(part.startswith("test_") for part in rel_path.parts):
        return False

    if rel_path.name == "__init__.py":
        return False

    if any(
        rel_path == allowed or rel_path.is_relative_to(allowed)
        for allowed in _ALLOWLIST_PATHS
    ):
        return False

    return True