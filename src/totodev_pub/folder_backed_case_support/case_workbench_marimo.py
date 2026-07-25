# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Workbench marimo skeleton generator: a ready-to-drive notebook for a case.

The interactive twin of the case briefing generator. From class-level static
analysis alone (via ``case_analysis.analyze()``), it emits a **marimo** app that
imports the case, builds it in a ``CaseWorkbench``, fires its manual triggers,
runs its auto edges, points at its custom-assertion folder, freeze-dries, and
detaches — pre-filled with the case's real triggers, states, and
``perform_<trigger>`` kwargs contracts.

Never touches a case folder or a live instance: generation is pure static
analysis; the *generated notebook* is what creates and drives a live case.

Design mirrors ``case_briefing``: a class-only ``render()`` + one-call
``generate_workbench_notebook()``, plus a CLI
(``python -m totodev_pub.folder_backed_case_support.case_workbench_marimo
<module>:<ClassName>``).

Spec: volatile/specs/2026-07-24-case-workbench-marimo-generator-mini-spec.md.

Future enhancement (not built): a Jupyter ``.ipynb`` renderer over the same
analysis + notebook model.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from totodev_pub.folder_backed_case_support.case_analysis import (
    CaseAnalysis,
    analyze,
)
from totodev_pub.folder_backed_case_support.case_briefing import (
    generate_case_briefing,
    to_mermaid,
)

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase
    from totodev_pub.folder_backed_case_support.case_analysis import (
        PerformParam,
        TriggerFacts,
    )

__all__ = [
    "MarimoSkeletonOptions",
    "CaseWorkbenchMarimoGenerator",
    "generate_workbench_notebook",
]

# Module path used in generated-doc stamps and CLI prog — keep in sync with
# ``python -m totodev_pub.folder_backed_case_support.case_workbench_marimo``.
_GENERATOR_TOOL = "totodev_pub.folder_backed_case_support.case_workbench_marimo"

_DOCS_REF = "docs/case-workbench-notebooks.md"


@dataclass(frozen=True)
class MarimoSkeletonOptions:
    """Toggles for what the generated notebook contains."""

    include_briefing: bool = False  # full briefing embed vs. lifecycle diagram only
    include_manual_triggers: bool = True
    include_run_loops: bool = True
    include_assertions: bool = True
    include_freeze_dry: bool = True
    run_button_gating: bool = True
    wildcard_pseudo_state: bool = False
    include_implied_caps: bool = True


# ---------------------------------------------------------------------------
# Cell builders — each returns one complete ``@app.cell`` block as a string.
# ---------------------------------------------------------------------------

def _md_cell(params: str, markdown: str) -> str:
    """A display cell wrapping raw markdown. The triple-quoted content is placed
    flush-left so it carries no accidental leading indentation."""
    return (
        "@app.cell\n"
        f"def _({params}):\n"
        '    mo.md(\n'
        '        r"""\n'
        f"{markdown.rstrip()}\n"
        '"""\n'
        "    )\n"
        "    return\n"
    )


def _kwarg_comment_lines(params: "list[PerformParam]") -> list[str]:
    if not params:
        return ["    #     # (no perform_ params declared; kwargs are unchecked)"]
    lines: list[str] = []
    for p in params:
        if p.required:
            lines.append(f"    #     {p.name}=...,  # required: {p.annotation_display}")
        else:
            lines.append(
                f"    #     {p.name}={p.default!r},  # optional: {p.annotation_display}"
            )
    return lines


def _manual_trigger_cell(t: "TriggerFacts") -> str:
    """One commented, run-button-gated example that fires the TRIGGER (never the
    perform method), with every declared kwarg listed (easier to delete than to
    add). ``async def`` so uncommenting the ``await`` just works."""
    lines = [
        "@app.cell",
        "async def _(mo, wb):",
        f'    mo.md(r"""**Manual trigger `{t.name}`** — uncomment below to fire '
        '(run-button-gated).""")',
        f'    # {t.name}_btn = mo.ui.run_button(label="Fire: {t.name}")',
        f"    # {t.name}_btn",
        f"    # mo.stop(not {t.name}_btn.value)",
        "    # await wb.trigger(",
        f'    #     "{t.name}",',
        *_kwarg_comment_lines(t.perform_params),
        "    # )",
        "    return",
        "",
    ]
    return "\n".join(lines)


def _first_manual_gate_state(analysis: CaseAnalysis) -> str:
    for t in analysis.manual_triggers:
        for e in t.edges:
            if e.source != "*":
                return e.source
    return analysis.initial_state or "<state>"


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class CaseWorkbenchMarimoGenerator:
    """Renders a marimo skeleton notebook for a ``FolderBackedCase`` subclass."""

    def __init__(self, *, options: MarimoSkeletonOptions = MarimoSkeletonOptions()) -> None:
        self._options = options

    def render(
        self, case_cls: "type[FolderBackedCase]", *, module_path: str | None = None,
    ) -> str:
        opts = self._options
        analysis = analyze(
            case_cls,
            wildcard_pseudo_state=opts.wildcard_pseudo_state,
            include_implied_caps=opts.include_implied_caps,
        )
        module_path = module_path or case_cls.__module__
        cls_name = analysis.case_cls_name
        source = f"{module_path}:{cls_name}"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cells: list[str] = []

        # 1. imports marimo
        cells.append(
            "@app.cell\n"
            "def _():\n"
            "    import marimo as mo\n"
            "    return (mo,)\n"
        )

        # 2. header / stamp / tutorial
        header_md = (
            f"# {cls_name} — CaseWorkbench notebook\n\n"
            f"<!-- generated by {_GENERATOR_TOOL} from {source} on {stamp} -->\n\n"
            f"**Generated:** {stamp} · **Source:** `{source}`\n\n"
            f"> ⚠️ **Staleness:** this notebook reflects `{cls_name}`'s FSM *as of "
            "generation time*. If the class changes (new/renamed triggers or "
            "states, changed `perform_*` params), regenerate it via "
            "`CaseWorkbench.marimo_skeleton(...)`.\n\n"
            "Driving is **async** — `await` works at cell top level in marimo. "
            "Side-effecting cells are gated behind a **run-button** so editing "
            "another cell never silently re-fires them.\n\n"
            f"See **{_DOCS_REF}** for the full guide."
        )
        cells.append(_md_cell("mo", header_md))

        # 3. lifecycle (full briefing embed, or just the diagram)
        if opts.include_briefing:
            briefing = generate_case_briefing(case_cls)
            cells.append(_md_cell("mo", briefing))
        else:
            diagram = to_mermaid(analysis.fsm_graph, style="flowchart")
            cells.append(
                "@app.cell\n"
                "def _(mo):\n"
                '    mo.md(r"""### Lifecycle""")\n'
                "    return\n"
            )
            cells.append(
                "@app.cell\n"
                "def _(mo):\n"
                '    mo.mermaid(\n'
                '        r"""\n'
                f"{diagram.rstrip()}\n"
                '"""\n'
                "    )\n"
                "    return\n"
            )

        # 4. imports: the case + the workbench
        cells.append(
            "@app.cell\n"
            "def _():\n"
            "    from totodev_pub.case_testing import CaseWorkbench\n"
            f"    from {module_path} import {cls_name}\n"
            f"    return CaseWorkbench, {cls_name}\n"
        )

        # 5. build the workbench + a fresh case (clone alternative commented)
        cells.append(
            "@app.cell\n"
            f"def _(CaseWorkbench, {cls_name}):\n"
            "    wb = CaseWorkbench.for_project()   # finds pyproject.toml; prints doctor()\n"
            f'    wb.create({cls_name}, nickname="demo")\n'
            "    # Or clone a freeze-dried example instead of create():\n"
            f'    #   wb.clone("{cls_name}/some_group/sample")\n'
            "    return (wb,)\n"
        )

        # 6. inspect: status / probe / history
        for method, note in (
            ("status", "state, dwell, terminal/advanceable, problem counts"),
            ("probe", "edges available from the current state, with guard verdicts"),
            ("history", "the transition table with per-step durations"),
        ):
            cells.append(
                "@app.cell\n"
                "def _(wb):\n"
                f"    wb.{method}()   # {note}\n"
                "    return\n"
            )

        # 7. manual triggers (commented, all kwargs listed)
        if opts.include_manual_triggers and analysis.manual_triggers:
            cells.append(
                _md_cell(
                    "mo",
                    "### Manual triggers\n\nEach fires `await wb.trigger(\"<name>\", "
                    "...)` — the **trigger**, not `perform_*`. Manual edges require "
                    "their kwargs.",
                )
            )
            for t in analysis.manual_triggers:
                cells.append(_manual_trigger_cell(t))
        elif opts.include_manual_triggers:
            cells.append(
                _md_cell(
                    "mo",
                    "### Manual triggers\n\n_This case has no manual triggers; every "
                    "edge advances automatically (see the run loop below)._",
                )
            )

        # 8. run loops
        if opts.include_run_loops:
            gate = _first_manual_gate_state(analysis)
            cells.append(_md_cell("mo", "### Run loops"))
            if opts.run_button_gating:
                cells.append(
                    "@app.cell\n"
                    "def _(mo):\n"
                    '    run_btn = mo.ui.run_button(label="Run: drive AUTO edges to completion")\n'
                    "    run_btn\n"
                    "    return (run_btn,)\n"
                )
                cells.append(
                    "@app.cell\n"
                    "async def _(mo, run_btn, wb):\n"
                    "    mo.stop(not run_btn.value)\n"
                    "    run_result = await wb.run()   # drives auto edges (max_steps=50)\n"
                    "    run_result\n"
                    "    return (run_result,)\n"
                )
            else:
                cells.append(
                    "@app.cell\n"
                    "async def _(wb):\n"
                    "    run_result = await wb.run()   # drives auto edges (max_steps=50)\n"
                    "    run_result\n"
                    "    return (run_result,)\n"
                )
            cells.append(
                "@app.cell\n"
                "async def _(wb):\n"
                "    # Other run-loop flavors (uncomment one):\n"
                "    #\n"
                "    # Stop just before a state (e.g. a manual gate):\n"
                f'    # await wb.run(stop_before="{gate}")\n'
                "    #\n"
                "    # Halt on the first new problem:\n"
                "    # await wb.run(stop_on_problems=True)\n"
                "    #\n"
                "    # Explicit long-hand (there is deliberately no run loop on the\n"
                "    # case itself; wb.run() is the sanctioned helper). One AUTO step\n"
                "    # at a time until the state stops changing:\n"
                "    # while wb.case.case_is_advanceable:\n"
                "    #     print(await wb.advance())\n"
                "    return\n"
            )

        # 9. custom assertions
        if opts.include_assertions:
            assert_state = analysis.initial_state or (
                analysis.states[0].name if analysis.states else "<state>"
            )
            cells.append(
                "@app.cell\n"
                "def _(wb):\n"
                "    print(wb.custom_assertions())   # list assertions/*.py in the case folder\n"
                "    wb.custom_assertions_dir()       # the folder to drop them in\n"
                "    return\n"
            )
            cells.append(
                _md_cell(
                    "mo",
                    "### Custom assertions\n\n"
                    "Drop a file in the case's `assertions/` folder (path above) "
                    "defining a function:\n\n"
                    "```python\n"
                    f"def case_assert_{assert_state}_my_check(case_reader, ltx):\n"
                    '    # return a message string to FAIL, or None/"" to pass\n'
                    "    return None\n"
                    "```\n\n"
                    f"It runs automatically on entry to `{assert_state}`. Failures "
                    "surface via `wb.problems()` / `wb.status()`. For untrusted case "
                    "folders, restrict with "
                    "`set_case_assertion_mode(AssertionMode.CLASS_ONLY)`.",
                )
            )

        # 10. freeze-dry + detach/cleanup (commented)
        if opts.include_freeze_dry:
            cells.append(
                "@app.cell\n"
                "def _(wb):\n"
                "    # Freeze-dry the current case onto the fixtures shelf for reuse:\n"
                '    # wb.freeze_dry(nickname="group/sample", description="...")\n'
                "    return\n"
            )
        cells.append(
            "@app.cell\n"
            "def _(wb):\n"
            "    # Detach the live case(s) and clear the scratch pool (scratch is\n"
            "    # durable by default, so this is optional):\n"
            "    # wb.cleanup()\n"
            "    return\n"
        )

        body = "\n\n\n".join(c.rstrip() for c in cells)
        return (
            "import marimo\n\n"
            'app = marimo.App()\n\n\n'
            f"{body}\n\n\n"
            'if __name__ == "__main__":\n'
            "    app.run()\n"
        )


def generate_workbench_notebook(
    case_cls: "type[FolderBackedCase]",
    *,
    module_path: str | None = None,
    options: MarimoSkeletonOptions = MarimoSkeletonOptions(),
) -> str:
    """Render a marimo skeleton notebook for ``case_cls`` — the library entry
    point (a ``CaseWorkbenchMarimoGenerator`` in one call)."""
    return CaseWorkbenchMarimoGenerator(options=options).render(
        case_cls, module_path=module_path,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m " + _GENERATOR_TOOL,
        description="Generate a marimo workbench notebook for a FolderBackedCase subclass.",
    )
    parser.add_argument(
        "target", help="the case class as <module>:<ClassName>",
    )
    parser.add_argument(
        "--briefing", dest="include_briefing", action="store_true",
        help="embed the full case briefing (default: lifecycle diagram only)",
    )
    parser.add_argument(
        "--no-manual-triggers", dest="include_manual_triggers",
        action="store_false", default=True,
    )
    parser.add_argument(
        "--no-run-loops", dest="include_run_loops", action="store_false", default=True,
    )
    parser.add_argument(
        "--no-assertions", dest="include_assertions", action="store_false", default=True,
    )
    parser.add_argument(
        "--no-freeze-dry", dest="include_freeze_dry", action="store_false", default=True,
    )
    parser.add_argument(
        "--no-run-buttons", dest="run_button_gating", action="store_false", default=True,
        help="do not gate side-effecting cells behind run-buttons (they auto-run)",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="write to PATH instead of stdout",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    target = args.target
    if ":" not in target:
        parser.error("target must be <module>:<ClassName>")
    module_name, _, class_name = target.partition(":")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        parser.error(f"could not import module {module_name!r}: {exc}")
        return 2
    case_cls = getattr(module, class_name, None)
    if case_cls is None:
        parser.error(f"module {module_name!r} has no attribute {class_name!r}")
        return 2

    from totodev_pub.folder_backed_case import FolderBackedCase as _Base
    if not (isinstance(case_cls, type) and issubclass(case_cls, _Base)):
        parser.error(f"{target} is not a FolderBackedCase subclass")
        return 2

    options = MarimoSkeletonOptions(
        include_briefing=args.include_briefing,
        include_manual_triggers=args.include_manual_triggers,
        include_run_loops=args.include_run_loops,
        include_assertions=args.include_assertions,
        include_freeze_dry=args.include_freeze_dry,
        run_button_gating=args.run_button_gating,
    )
    text = generate_workbench_notebook(
        case_cls, module_path=module_name, options=options,
    )
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
