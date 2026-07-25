# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the workbench marimo skeleton generator (case_workbench_marimo).

Class-level only — the generator never creates a case folder. Correctness here
means: the emitted text is valid Python, is a well-formed marimo app (no
duplicate cell defs; loads in marimo when installed), and reflects the case's
real manual triggers and their perform kwargs."""

from __future__ import annotations

import ast

import pytest

pytest.importorskip("networkx")

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_workbench_marimo import (
    MarimoSkeletonOptions,
    generate_workbench_notebook,
)


class SampleCase(FolderBackedCase):
    """A ticket case with both manual and auto triggers."""

    fsm_state_chains = """
        [*] --> new -- intake --> reviewing
        reviewing == approve [funded] ==> done --> [*]
        reviewing == fasttrack [funded] ==> done
        reviewing -- recheck [funded] --> reviewing
        reviewing -- expire [@DWELL>=1h] --> expired --> [*]
        * == cancel ==> cancelled --> [*]
    """
    fsm_trigger_chokes = {}
    asset_aliases = {}

    async def perform_intake(self, tctx, *, source: str):
        """Pull the raw ticket payload into the case folder."""

    async def guard_funded(self, tctx) -> bool:
        return True

    async def perform_approve(self, tctx, *, reason: str, notify: bool = True):
        """Stamp the ticket as approved."""

    async def perform_recheck(self, tctx):
        ...

    async def perform_expire(self, tctx):
        ...


class AutoOnlyCase(FolderBackedCase):
    """Every edge advances automatically — no manual triggers."""

    fsm_state_chains = """
        [*] --> start -- go --> middle -- finish --> done --> [*]
    """
    fsm_trigger_chokes = {}
    asset_aliases = {}

    async def perform_go(self, tctx):
        ...

    async def perform_finish(self, tctx):
        ...


def _cell_return_names(source: str) -> list[str]:
    """All names returned (i.e. defined-for-downstream) by every @app.cell."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            (isinstance(d, ast.Attribute) and d.attr == "cell")
            for d in node.decorator_list
        ):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                val = stmt.value
                elts = val.elts if isinstance(val, ast.Tuple) else [val]
                names.extend(e.id for e in elts if isinstance(e, ast.Name))
    return names


def test_generates_valid_python():
    src = generate_workbench_notebook(SampleCase)
    ast.parse(src)  # raises SyntaxError on failure


def test_marimo_structural_markers():
    src = generate_workbench_notebook(SampleCase)
    assert src.startswith("import marimo")
    assert "app = marimo.App()" in src
    assert 'if __name__ == "__main__":' in src
    assert "app.run()" in src
    # side-effecting run cell gated by default
    assert "mo.ui.run_button" in src
    assert "mo.stop(" in src


def test_manual_trigger_cells_and_kwargs():
    src = generate_workbench_notebook(SampleCase)
    # manual triggers appear; the auto trigger 'intake' has no fire-cell
    assert 'wb.trigger(\n    #     "approve"' in src
    assert 'wb.trigger(\n    #     "fasttrack"' in src
    assert 'wb.trigger(\n    #     "cancel"' in src
    # fires the trigger, never perform_*
    assert "perform_approve(" not in src
    # required vs optional kwargs, from the manual trigger's perform signature
    assert "reason=...,  # required: str" in src
    assert "notify=True,  # optional: bool" in src


def test_no_duplicate_cell_defs():
    src = generate_workbench_notebook(SampleCase)
    names = _cell_return_names(src)
    assert len(names) == len(set(names)), f"duplicate cell defs: {names}"


def test_no_manual_triggers_note():
    src = generate_workbench_notebook(AutoOnlyCase)
    assert "no manual triggers" in src
    # no trigger fire-cell emitted
    assert "wb.trigger(" not in src


def test_run_button_gating_can_be_disabled():
    gated = generate_workbench_notebook(SampleCase)
    ungated = generate_workbench_notebook(
        SampleCase, options=MarimoSkeletonOptions(run_button_gating=False),
    )
    # the RUN-LOOP cell is gated by default (commented trigger examples always
    # show run_button, so target the run-loop's own button/stop specifically)
    assert 'run_btn = mo.ui.run_button(label="Run:' in gated
    assert "mo.stop(not run_btn.value)" in gated
    assert "run_btn" not in ungated
    assert "await wb.run()" in ungated  # still drives, just not gated


def test_include_briefing_embeds_full_briefing():
    src = generate_workbench_notebook(
        SampleCase, options=MarimoSkeletonOptions(include_briefing=True),
    )
    # the briefing carries its own generated-by stamp
    assert "Case briefing generated by" in src


def test_stamp_and_staleness_header():
    src = generate_workbench_notebook(SampleCase)
    assert "Staleness" in src
    assert "generated by totodev_pub.folder_backed_case_support.case_workbench_marimo" in src


def test_module_path_override_used_in_imports():
    src = generate_workbench_notebook(SampleCase, module_path="my.custom.module")
    assert "from my.custom.module import SampleCase" in src


def test_marimo_loads_the_app():
    pytest.importorskip("marimo")
    from marimo._ast.load import load_app

    src = generate_workbench_notebook(SampleCase)
    # write to a temp path marimo can read
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nb.py"
        p.write_text(src, encoding="utf-8")
        app = load_app(str(p))
    assert app is not None
