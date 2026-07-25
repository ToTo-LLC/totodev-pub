# CaseWorkbench marimo notebooks

> **Status: stub.** This is the reference the generated marimo skeletons link to.
> The section skeleton below is intentionally brief for now — flesh out the
> prose as the workflow settles.

A **workbench notebook** is a marimo app that drives a `FolderBackedCase`
subclass through a `CaseWorkbench`. Generate one with
`CaseWorkbench.marimo_skeleton(MyCase)` or the CLI:

```
python -m totodev_pub.folder_backed_case_support.case_workbench_marimo <module>:<ClassName>
```

It is the interactive twin of the [case briefing](../volatile/specs/2026-07-18-case-briefing-gen-mini-spec.md):
the briefing documents a case type; the notebook drives one.

## The workbench mental model

- **Fixtures shelf** (`tests/case-fixtures`) — durable, freeze-dried example
  cases.
- **Scratch pool** (`volatile/case-workbench/scratch`) — working case folders.
- **Focus** (`wb.case`) — the live case most commands act on.

_TODO: expand._

## Manual vs. auto triggers

- **Auto** edges (`A -- trigger --> B`) fire during `await wb.run()` /
  `await wb.advance()`.
- **Manual** edges (`A == trigger ==> B`) must be fired explicitly:
  `await wb.trigger("<trigger>", **kwargs)` — which fires the *trigger*, not the
  `perform_*` method. Manual edges require their kwargs; the generated notebook
  pre-fills every parameter from the `perform_<trigger>` signature.

_TODO: expand._

## Run loops

`await wb.run()` drives auto edges until terminal / no progress / `max_steps`.
Variants: `stop_before="<state>"`, `stop_on_problems=True`. There is
deliberately no run loop on the case itself — `wb.run()` is the sanctioned
helper.

_TODO: expand._

## Assertions: class vs. custom

- **Class assertions** — `case_assert_<state>_<slug>(self, ltx)` methods on the
  subclass.
- **Custom assertions** — per-case `assertions/*.py` files inside a case folder
  (`case_assert_<state>_<slug>(case_reader, ltx)`). List them with
  `wb.custom_assertions()`; find the folder with `wb.custom_assertions_dir()`.
  Because custom assertions are "code that arrived as data," restrict untrusted
  folders with `set_case_assertion_mode(AssertionMode.CLASS_ONLY)`.

_TODO: expand._

## Freeze-dry and detach

`wb.freeze_dry(nickname="group/sample", description="...")` copies the focus
case onto the fixtures shelf. `wb.cleanup()` detaches live cases and clears the
scratch pool (scratch is durable by default).

_TODO: expand._

## Marimo notes

- Driving is async — `await` works at cell top level in marimo.
- Side-effecting cells are gated behind `mo.ui.run_button()` so marimo's
  reactive re-execution never silently re-fires a trigger.

_TODO: expand._
