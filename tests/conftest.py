# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Pytest configuration and shared hooks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable, Iterable

# Allow `import totodev_pub` with src/ layout from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = str(_REPO_ROOT / "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)


# Data-driven mapping of optional-feature test areas. Each rule pairs a feature
# marker (matching the markers declared in pytest.ini and the extras in
# pyproject.toml) with a path predicate and the importable modules that the
# matching tests need at import time. When any required module is missing, the
# test is skipped at collection so a core-only install does not hard-fail with
# ModuleNotFoundError. The same rules drive automatic marker application so the
# core/full lanes can select with, e.g., `-m "not pipes and not connectors"`.
_OPTIONAL_TEST_RULES: tuple[tuple[str, Callable[[Path], bool], tuple[str, ...]], ...] = (
    ("pipes", lambda p: "pipes" in p.parts and p.name != "conftest.py", ("luigi",)),
    ("llm", lambda p: "llm" in p.parts and p.name != "conftest.py", ("langchain",)),
    (
        "connectors",
        lambda p: p.name in ("test_file_proxy_sharepoint.py", "test_file_proxy_refactoring.py"),
        ("aiohttp", "aiofiles", "requests"),
    ),
    ("connectors", lambda p: p.name == "test_file_proxy_outlook_email.py", ("requests",)),
    ("lucidspark", lambda p: p.name == "test_lucidspark_parser.py", ("networkx", "treelib")),
    ("git", lambda p: p.name == "test_cached_folders_versioner.py", ("git",)),
)


def _missing_modules(modules: Iterable[str]) -> list[str]:
    """Return the subset of importable module names that are not installed."""
    return [m for m in modules if importlib.util.find_spec(m) is None]


def pytest_ignore_collect(collection_path: Path, config) -> bool:
    """Skip optional-integration test modules when their dependencies are not installed."""
    p = Path(collection_path)
    if not (p.is_file() and p.suffix == ".py"):
        return False
    for _marker, predicate, required in _OPTIONAL_TEST_RULES:
        if predicate(p) and _missing_modules(required):
            return True
    return False


def pytest_collection_modifyitems(config, items) -> None:
    """Tag optional-feature tests with their feature marker for lane selection."""
    for item in items:
        p = Path(str(item.fspath))
        for marker, predicate, _required in _OPTIONAL_TEST_RULES:
            if predicate(p):
                item.add_marker(marker)
