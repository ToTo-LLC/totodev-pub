---
name: case-designer
description: Interview a developer about a business process and scaffold a new FolderBackedCase subclass for it (this repo's folder-backed, FSM-driven "case" work-item base class) — producing fsm_state_chains, asset_aliases, fsm_trigger_chokes, and skeleton hook/guard/assertion methods that log a warning and return a safe default (via a self-deleting _not_implemented() scaffolding helper) instead of working implementation logic, so the generated lifecycle can be driven/simulated before anything is filled in. Use when the user wants to model a new recurring unit of work as a case, design or discuss a case lifecycle/FSM, or scaffold a new case class/case type in totodev_pub. Triggers on requests like "help me build a case for X", "scaffold a new case type", "model X as a FolderBackedCase", "design the lifecycle for Y", or "add perform_/guard_ stubs for this case".
---

# Case Designer

## Overview

A "case" in this repo (`FolderBackedCase`, `src/totodev_pub/folder_backed_case.py`)
is a heavyweight FSM-driven work item whose entire state lives in one folder on
disk. This skill turns a developer's description of a business process into a
skeleton subclass: declarations filled in for real, every behavioral method
stubbed with a call to a small self-deleting `_not_implemented()` helper (logs
a WARNING and returns a safe default, so the lifecycle can be driven/simulated
before anything real exists) and a docstring naming its responsibility — never
the implementation itself. The developer fills in the bodies afterward.

Everything a basic subclass author needs is on `FolderBackedCaseInterface`
(`src/totodev_pub/folder_backed_case_support/folder_backed_case_interface.py`).
Do not read the fuller `FolderBackedCase` implementation file or its advanced
customization seams for this task — that surface is deliberately out of scope
here and tends to confuse rather than help a first-time case author.

## Workflow

1. **Interview** the developer (below) to learn the business situation.
2. **Draft the lifecycle** as `fsm_state_chains`, confirm it back in plain
   English before writing code.
3. **Draft `asset_aliases`** for the data contracts named in the interview.
4. **Draft `fsm_trigger_chokes`** for the expensive steps named.
5. **Generate the skeleton class file**, following the shape in
   `assets/case_class_template.py`.
6. **Validate it binds** — actually import the generated module and fix
   whatever the framework rejects. Do not skip this.
7. **Check coverage** against the checklist before handing it back.
8. **Offer a documentation report** — ask if the developer wants a rendered
   summary of the finished class (lifecycle diagram, states/triggers/guards/
   assertions/assets tables) to sanity-check the whole design at a glance.

For DSL grammar, hook-naming rules, the assertion convention, and `AssetSpec`
fields, see `references/dsl_and_hooks.md` — read it before step 2 if any of
those are unfamiliar or you need the exact syntax.

## Step 1 — Interview

Don't guess at the business situation — ask. Go one topic at a time rather than
dumping every question at once; follow up based on what comes back. Cover:

1. **Name the case.** "What's the recurring unit of work?" (one email thread,
   one purchase transaction, one catalog image...). If the developer can't say
   what "one" is, stop here — everything else hangs on it.
2. **Draw the lifecycle.**
   - What states does it pass through, start to finish?
   - For each transition: does it happen automatically once conditions are
     met, or does it require an explicit human/UI action (`--` vs `==`)?
   - Where can a step fail, and should it retry or divert after N failures
     (`@FAIL`)?
   - Where can a case sit waiting on a person indefinitely — does it need a
     timed escape (`@DWELL`)?
   - Is there a global "cancel from anywhere" edge or similar wildcard?
3. **Name the data contracts.** What files/objects does the case read or
   write (e.g. an analysis result, a set of attachments, a draft)? For each:
   what states is it trustworthy/complete in? Does it need to survive
   termination (`keep=True`)? Is it a single file or a glob of many
   (`many=True`)?
4. **Mark the expensive steps.** Which triggers call something rate-limited or
   costly (an LLM, an external API, heavy CPU work)? Only the resource *name*
   is needed here — capacity limits are a deployment decision, not part of the
   case type.

If the developer gives you a rough narrative instead of clean answers to the
above, restate the lifecycle back as a numbered list of states/transitions and
confirm it before moving on — errors here propagate into every generated stub.

## Step 2–4 — Translate into declarations

Using `references/dsl_and_hooks.md`:
- Turn the confirmed lifecycle into `fsm_state_chains` strings.
- Turn the confirmed data contracts into `AssetSpec` entries (plus one
  `FileMappedPydanticMixin`/pydantic placeholder class per structured asset,
  fields named but bodies empty — field declarations aren't "meat" to defer,
  they're the contract itself).
- Turn the confirmed expensive steps into `fsm_trigger_chokes`.

Show the developer the declarations before generating hook stubs — cheaper to
fix a wrong state name now than after 15 stubs reference it.

## Step 5 — Generate the skeleton

Follow `assets/case_class_template.py` as the shape to imitate: same file
layout, same stub style, same `@case_type_registry.register` decorator and
import set. Do NOT copy its domain (permits) — only its structure. Concretely,
for every name that appears in the confirmed `fsm_state_chains`:

- One `async def perform_<trigger>(self, tctx)` for every trigger that does
  real work, docstring stating what it must read/write/call — drawn from the
  interview, not invented.
- One `async def guard_<guard>(self, tctx) -> bool` for every `guard#trigger`
  name, docstring stating the condition it decides.
- `on_enter_<state>` / `on_exit_<state>` **only** for states the developer
  specifically described as needing entry/exit side effects — not every state
  needs one.
- One `def case_assert_<state>_<slug>(self, ltx) -> None | str` per state with
  an expected shape (most states have at least one worth asserting — see the
  assertion convention in the reference file for what "expected shape" means).
  A state with genuinely nothing to check can be skipped; say so explicitly
  rather than silently omitting it.
- `on_terminating` only if the developer named a runtime (not declare-time)
  retention decision; otherwise omit it and rely on `AssetSpec(keep=True)`.

**Stub bodies never raise or implement anything.** Every generated class also
gets a small `_not_implemented(self, retval)` scaffolding helper (see
`references/dsl_and_hooks.md` for its exact form) that logs a WARNING to
`self.log` — naming the caller via `sys._getframe(1).f_code.co_qualname`, so
stubs never hardcode their method name — then returns `retval`. Every stub's
entire body (after its docstring) is exactly one line —
`return self._not_implemented(<default>)` — passing whatever default its
signature requires (`None` for hooks, `True` for guards, `None` for
assertions). This is what lets the developer drive/simulate the whole
lifecycle before any real logic exists, and it makes de-stubbing mechanical:
delete that one line, write the real body. Mention in the generated file that
`_not_implemented` and its call sites are meant to be deleted once real
implementations land. Never write the actual `perform_`/`guard_` logic, even
if it looks obvious or short — that is the developer's implementation to
write, not this skill's job.

## Step 6 — Validate it binds

`FolderBackedCase` subclasses are validated at class-definition time: malformed
`fsm_state_chains` (misspelled/unreachable/dead-end states), orphan hook
methods that match no DSL name, and asset aliases that are trustworthy in a
terminal state without `keep=True` all raise immediately on import — they do
not wait for a test suite. So after writing the file, actually import it and
fix whatever it raises, rather than eyeballing the code and calling it done:

```bash
python -c "import <module path to the generated file>"
```

Run this from wherever the project's dependencies are installed (e.g. its
`.venv`), not bare `python3` — a `ModuleNotFoundError` for a project
dependency (not the generated class itself) usually means the wrong
interpreter, not a real problem with the file. Iterate until it imports
cleanly. This has caught real mistakes before (e.g. an asset alias
trustworthy in a terminal state but missing `keep=True`) — treat a clean
import as a required gate, not an optional nicety.

**Then, if `create_case_in_folder`'s signature is plain** — i.e. the generated
class didn't override construction or add required custom record fields
(an advanced seam outside this skill's scope; if present, skip this and say
so) — go one step further and actually instantiate a throwaway case in a temp
folder. This exercises record creation, lease acquisition, and the initial
journal writes, none of which the import check alone touches:

```python
import shutil, tempfile
from pathlib import Path
from <module path> import <GeneratedClass>

tmp = Path(tempfile.mkdtemp(prefix="case-designer-smoke-"))
try:
    case = <GeneratedClass>.create_case_in_folder(tmp / "smoke-case")
    print("instantiated OK:", case.case_id, case.case_state)
    case.case_detach()
finally:
    shutil.rmtree(tmp, ignore_errors=True)  # ALWAYS runs, even if construction raised
```

The `try`/`finally` (or an equivalent like `tempfile.TemporaryDirectory()`) is
not optional — never leave a smoke-test folder behind, success or failure.
Don't drive the case any further than this (no `case_advance()`, no trigger
calls) — the point is confirming it binds and constructs, not simulating the
lifecycle; that's the developer's job once real logic replaces the stubs.

## Step 7 — Coverage checklist

Before handing the file back, confirm:

- [ ] Every trigger named in `fsm_state_chains` has a `perform_<trigger>` (or
      an explicit note on why not).
- [ ] Every `guard#trigger` name has a matching `guard_<guard>`.
- [ ] Every state has at least one `case_assert_<state>_*`, or an explicit
      note on why that state has nothing to assert.
- [ ] `asset_aliases` states/loader/keep/many are all set (or
      `flexible_asset_alias_loading = True` was deliberately chosen instead).
- [ ] The class carries `@case_type_registry.register`.
- [ ] No stub contains real logic — only a responsibility docstring and a
      single `return self._not_implemented(<default>)` line.

## Step 8 — Offer a documentation report

Once the class binds cleanly (Step 6) and the coverage checklist (Step 7) is
satisfied, **ask** the developer whether they'd like a rendered documentation
report for the class — don't generate it unprompted; it's a few extra seconds
of output some developers won't want.

If they say yes, use the case documentation generator
(`totodev_pub.folder_backed_case_support.case_doc`):

```python
from totodev_pub.folder_backed_case_support.case_doc import generate_case_docs
print(generate_case_docs(<GeneratedClass>))
```

or, equivalently, from the CLI:

```bash
python -m totodev_pub.folder_backed_case_support.case_doc <module path>:<GeneratedClass>
```

This is class-level only, like Step 6's import check — no case folder is
created. It renders the lifecycle diagram plus states/triggers/guards/
assertions/asset-alias tables, pulling the first paragraph of every docstring
this skill just wrote. It doubles as a last sanity check in its own right: a
trigger, guard, or state whose docstring came out thin or missing stands out
immediately in the rendered tables — worth fixing before handing the file
back if so.

Show the rendered Markdown to the developer directly (or save it alongside
the generated class if they'd like a copy) — don't just report that it
succeeded.
