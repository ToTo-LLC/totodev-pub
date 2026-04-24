# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Pytest configuration and shared hooks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Allow `import totodev_pub` with src/ layout from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = str(_REPO_ROOT / "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)


def pytest_ignore_collect(collection_path: Path, config) -> bool:
    """Skip optional-integration test modules when their dependencies are not installed."""
    p = Path(collection_path)
    if not (p.is_file() and p.suffix == ".py"):
        return False
    if "llm" in p.parts and p.name != "conftest.py":
        if importlib.util.find_spec("langchain") is None:
            return True
    if p.name in (
        "test_file_proxy_outlook_email.py",
        "test_file_proxy_sharepoint.py",
        "test_file_proxy_refactoring.py",
    ):
        if importlib.util.find_spec("requests") is None:
            return True
    if p.name == "test_cached_folders_versioner.py":
        if importlib.util.find_spec("git") is None:
            return True
    return False
