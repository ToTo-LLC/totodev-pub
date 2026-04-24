# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Guardrail: this project must remain a single Git repository."""

from pathlib import Path


def test_no_nested_git_directories_under_repo() -> None:
    """Fail if any nested .git directories exist in tracked source/test trees."""
    repo_root = Path(__file__).resolve().parent.parent
    scan_roots = [repo_root / "src", repo_root / "tests"]
    nested = []
    for root in scan_roots:
        if root.exists():
            nested.extend(root.glob("**/.git"))
    assert not nested, f"Nested .git directories are not allowed: {nested}"
