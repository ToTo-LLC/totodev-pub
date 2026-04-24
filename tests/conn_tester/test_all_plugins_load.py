# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Sanity test: ensure all public conn_tester plugins can be discovered and described.

This helps catch missing dependencies or import-path issues early, by exercising
the same discovery and describe_self() path that the webapp uses via jsonlist.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def test_all_public_plugins_load_and_describe() -> None:
    """
    Exercise the same jsonlist path the webapp uses to ensure all plugins load.

    Running the CLI entrypoint avoids import-path issues inside individual
    plugin modules (they expect to be imported with conn_tester_support on sys.path).

    Requires connector stack (``requests``, etc.); skip in a core-only venv.
    """
    if importlib.util.find_spec("requests") is None:
        pytest.skip("conn_tester jsonlist needs requests (install totodev-pub[connectors])")

    import json
    from subprocess import run, PIPE

    # Ensure we are invoking the project-local conn_tester CLI.
    # This file: <repo>/tests/conn_tester/test_all_plugins_load.py -> repo root is parents[2].
    root = Path(__file__).resolve().parents[2]
    cli_path = root / "src" / "totodev_pub" / "cli" / "conn_tester.py"

    result = run([sys.executable, str(cli_path), "jsonlist"], cwd=root, stdout=PIPE, stderr=PIPE, text=True)
    assert result.returncode == 0, f"jsonlist failed: {result.stderr}"

    data = json.loads(result.stdout)
    assert data, "jsonlist returned no tests"

    for shortname, info in data.items():
        # For now, just assert that we don't have internal error markers.
        assert "status" not in info or info["status"] != "internal_error", f"{shortname} has internal_error status: {info}"


