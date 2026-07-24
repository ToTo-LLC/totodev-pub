# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Pytest helpers for workbench scenario scripts (lazy pytest import)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator


def iter_scenario_paths(
    root: Path | str | None = None,
    *,
    pattern: str = "scenario_*.py",
) -> Iterator[Path]:
    """Yield scenario script paths under ``tests/case-scenarios/**`` (or *root*)."""
    if root is None:
        # Best-effort: walk up for pyproject, else cwd-relative default
        from totodev_pub.case_testing.class_index import find_project_root

        proj = find_project_root()
        base = (proj / "tests" / "case-scenarios") if proj else Path("tests/case-scenarios")
    else:
        base = Path(root)
    if not base.is_dir():
        return
        yield  # pragma: no cover — makes this a generator
    for path in sorted(base.rglob(pattern)):
        if path.is_file():
            yield path


def make_scenario_parametrization(
    root: Path | str | None = None,
    *,
    pattern: str = "scenario_*.py",
    ids: bool = True,
):
    """Return ``pytest.mark.parametrize`` args for scenario files.

    Usage in a test module::

        import pytest
        from totodev_pub.case_testing.pytest_bridge import make_scenario_parametrization

        @pytest.mark.parametrize(*make_scenario_parametrization())
        def test_scenario(scenario_path, project_root):
            ...
    """
    import pytest  # lazy — not a hard runtime dep of totodev_pub

    paths = list(iter_scenario_paths(root, pattern=pattern))
    if ids:
        return ("scenario_path", paths, [p.stem for p in paths])
    return ("scenario_path", paths)


def run_scenario_file(
    path: Path | str,
    *,
    cwd: Path | str | None = None,
    timeout: float = 120.0,
    pythonpath_src: bool = True,
) -> None:
    """Run one scenario file as a subprocess; raise on non-zero exit.

    Imports pytest only indirectly via the caller's test; this helper uses
    subprocess so the scenario can be plain Python (including marimo scripts).
    """
    import os
    import subprocess
    import sys

    path = Path(path).resolve()
    work = Path(cwd).resolve() if cwd else path.parent
    env = os.environ.copy()
    if pythonpath_src:
        from totodev_pub.case_testing.class_index import find_project_root

        proj = find_project_root(work)
        if proj is not None:
            src = str(proj / "src")
            prev = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = src if not prev else f"{src}{os.pathsep}{prev}"
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Scenario {path} exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
