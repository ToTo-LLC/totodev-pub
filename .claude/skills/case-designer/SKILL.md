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
here and tends to confuse rather than help a first-time case author. (The lone
exception is the advanced reclassify pattern in Step 7's catalog, which names
the one docstring to read if that specific pattern is adopted.)

**Reference boundary.** Within this skill, only allude to (a) files inside the
skill tree itself, or (b) files under `src/`. Do not point at `notebooks/`,
personal notes, or other project paths — those change or disappear. If outside
material would help the skill, copy it into `assets/` or `references/` here
instead of linking out.

## Workflow

1. **Interview** the developer (below) to learn the business situation.
2. **Draft the lifecycle** as `fsm_state_chains`, confirm it back in plain
   English before writing code. (If the developer brings an existing Mermaid
   sketch, convert it via the chain-DSL CLI first — see Step 2–4.)
3. **Draft `asset_aliases`** for the data contracts named in the interview.
4. **Draft `fsm_trigger_chokes`** for the expensive steps named.
5. **Generate the skeleton class file**, following the shape in
   `assets/case_class_template.py` and the output examples in
   `references/generated_output_examples.md` (module/class docstrings,
   asset TODOs).
6. **Validate it binds** — actually import the generated module and fix
   whatever the framework rejects. Do not skip this.
7. **Review optional design patterns** — consult
   `references/case_design_patterns.md` and prompt the developer about whether
   any of those enhancements fit this case; fold in the ones they adopt and
   re-validate.
8. **Lint the FSM and check coverage** — run the chain-DSL CLI over the final
   declaration for advisory warnings, then the checklist, before handing it
   back.
9. **Offer a case briefing** — ask if the developer wants a rendered
   summary of the finished class (lifecycle diagram, states/triggers/guards/
   assertions/assets tables) to sanity-check the whole design at a glance.

Before drafting chains, read **FSM design principles** in
`references/dsl_and_hooks.md` (start of that file). Then use the same file for
DSL grammar, hook-naming rules, assertions, and `AssetSpec` fields when you
need exact syntax.

## Step 1 — Interview

Don't guess at the business situation — ask. Go one topic at a time rather than
dumping every question at once; follow up based on what comes back. Cover:

1. **Name the case.** "What's the recurring unit of work?" (one email thread,
   one purchase transaction, one catalog image...). If the developer can't say
   what "one" is, stop here — everything else hangs on it.
2. **Draw the lifecycle** (apply FSM design principles in
   `references/dsl_and_hooks.md`).
   - Restate the **primary happy path** in plain English first (initial →
     terminal success); confirm before adding failure/edge paths.
   - Prefer the **coarsest honest** model; only propose extra states/triggers
     for concrete reasons (granular retry, one choke per trigger, divergent
     error handling, CaseWorkbench isolation, pool fairness) — not "more
     states for documentation."
   - Check naming: triggers as present-tense verbs, states as
     adjectives/nouns for condition or step just completed, in the users'
     vocabulary; names must be valid Python identifier segments.
   - For each transition: automatic once conditions are met, or explicit
     human/UI action (`-- trigger -->` vs `== trigger ==>`)? Human gates
     (attach, approve, archive, …) are usually manual (`==`).
   - Same-state edges (`A -- t --> A` / `A == t ==> A`) are allowed. If
     proposing an **auto** self-loop (`A -- trigger --> A`), it **must**
     carry at least one named method guard (`trigger [still_needed]`);
     otherwise make it **manual** (`A == trigger ==> A`). Factual `@DWELL` /
     `@FAIL` alone do not satisfy the auto self-loop rule (unguarded auto
     self-loops can spin forever).
   - Where can a step fail, and should it retry or divert after N failures
     (`@FAIL`)?
   - Where can a case sit waiting on a person indefinitely — does it need a
     timed escape (`@DWELL`)?
   - Is there a global "cancel from anywhere" edge or similar wildcard?
   - **How do files enter the case?** Unless the developer specifically needs
     otherwise, prefer the inert-intake default in Step 2 (below) — do not
     silently bake file copy/import into `create_case_in_folder`.
3. **Name the data contracts.** What files/objects does the case read or
   write (e.g. an analysis result, a set of attachments, a draft)? For each:
   what states is it trustworthy/complete in? Does it need to survive
   termination (`keep=True`)? Is it a single file or a glob of many
   (`many=True`)?
4. **Mark the expensive steps** (chokes). Which triggers' `perform_` work draws
   on a rate-limited or costly *shared* resource — local OCR / vector embedding
   (`"cpu"`), a bandwidth-limited external API (`"api"`, even when the ceiling
   is fairly high, e.g. ~20 parallel calls), an LLM (`"llm"`)? Only the resource
   *name* is needed here — capacity limits are a deployment decision, not part
   of the case type. Be sparing: many cases have no chokes at all, and when in
   doubt leave a step unlisted and let real resource contention emerge under
   test rather than pre-declaring chokes you may not need.

If the developer gives you a rough narrative instead of clean answers to the
above, restate the lifecycle back as a numbered list of states/transitions and
confirm it before moving on — errors here propagate into every generated stub.

If the developer already has a lifecycle sketched in another tool (Mermaid
Live, a README diagram, an export), don't transcribe or re-interview it from
scratch — convert it (see "Starting from a pre-existing Mermaid sketch" in
Step 2–4) and review the result together, treating the converted text as the
draft to confirm.

## Step 2–4 — Translate into declarations

Using `references/dsl_and_hooks.md` (design principles first, then grammar):

- Turn the confirmed lifecycle into `fsm_state_chains` — encode the happy
  path first, then layer `@FAIL` / `@DWELL` / wildcards / secondary paths.
  Use the principles section's split checklist and anti-patterns when
  tempted to grow the graph. Once the declaration has three or more chains,
  prefer a single triple-quoted multiline string (one chain per line, `%%`
  comments) over a list of strings.
- When drafting any same-state edge: use `==` **or** a method-guarded `--`
  (`ready -- tick [still_needed] --> ready`). Never emit an unguarded auto
  self-loop — `FsmChainSpec.validate()` rejects it at import.
- Show the developer the happy-path chains before the fully loaded graph.
- Turn the confirmed data contracts into `AssetSpec` entries (plus one
  `FileMappedPydanticMixin`/pydantic placeholder class per structured asset,
  fields named but bodies empty — field declarations aren't "meat" to defer,
  they're the contract itself).
- Turn the confirmed expensive steps into `fsm_trigger_chokes` (prefer a single
  resource per trigger — see the chokes section in `references/dsl_and_hooks.md`
  for why).

### Starting from a pre-existing Mermaid sketch

When the developer supplies a lifecycle already drawn elsewhere, feed it to the
chain-DSL CLI's intake mode instead of hand-transcribing it:

```bash
# from the project venv; SOURCE may be a file, stdin (pipe/pbpaste), or a literal
python -m totodev_pub.folder_backed_case_support.state_chain_cli --convert sketch.mmd
```

It emits a cleaned declaration: free-text labels slugged into trigger
identifiers (originals kept in `%% was "..."` comments), unlabeled edges given
placeholder triggers flagged `%% TODO: name this trigger`, and
notes/aliases/choice states commented out rather than dropped. Review the
output with the developer like any other draft — in particular, a pasted
sketch is all `-->` (all-auto; the CLI warns about this): resolve every
`%% TODO` and mark each human/event gate manual (`==>` or `: ==`) before
moving on.

Before that review, read `references/mermaid_and_the_dsl.md` — it maps the
two notations, names the semantic gaps a diagram cannot express (auto vs
manual above all — those become interview questions), and shows how to
re-express constructs the DSL lacks (choice diamonds → guarded auto branching,
fork/join → coarse states or separate case types, composites → flattened
names).

### Default: inert initial state + trigger-based intake

Unless the developer specifically needs otherwise, draft the lifecycle so the
**initial state is inert and empty**, with a name clearly outside the domain
(e.g. `new`, `initial`). Do **not** make file import part of
`create_case_in_folder` (or a subclass override of it). The portable way to add
files is a **manual** trigger whose kwargs carry a filepath or filepaths —
e.g. `add_attachments` — into a very temporary state like `attachments_added`,
then either loop back to `new` or advance into the first real-flow state:

```text
[*] --> new == add_attachments ==> attachments_added -- begin --> submitted -- ...
```

**Why:** real processing (OCR, translate, parse, …) becomes a trigger operation
with semantics, event tracking, timing, and the rest of the case machinery.
Pre-creation's main virtue is speed; putting heavy init there makes testing and
tracking harder. Heavy work *inside* `create_case_in_folder` is not wrong — it
just costs those facilities. If the developer insists on create-time import,
honor that explicitly and note the tradeoff; otherwise propose the trigger
shape even when they ask to "keep it simple" or "skip extra states."

Full notes and variants: pattern "Inert intake" in
`references/case_design_patterns.md` (raise this during base lifecycle design
when the case ingests files — do not wait for Step 7).

Show the developer the declarations before generating hook stubs — cheaper to
fix a wrong state name now than after 15 stubs reference it.

## Step 5 — Generate the skeleton

Follow `assets/case_class_template.py` as the shape to imitate: same file
layout, same stub style, same `@case_type_registry.register` decorator and
import set. Do NOT copy its domain (permits) — only its structure. For
docstring voice, ClassVar trust map, and anti-patterns, also read
`references/generated_output_examples.md` (short excerpts of good output).

### Documentation and declaration principles

- **Module docstring** — narrative of the intended business purpose: why this
  case exists and what it models at a high level, drawn from the interview.
  Close with an **IMPORTANT** paragraph stating this is a rough first draft
  that needs substantial follow-on work (data structures, method bodies,
  tuning `fsm_state_chains`, assertions, asset contracts). Point readers at
  `fsm_state_chains` for the lifecycle — do not paste a second ASCII diagram
  of the chains into the module header.
- **Do not** mention who/when/how the file was constructed, or this skill.
  **Do not** lecture in the module docstring about `_not_implemented` / “delete
  this one line” — the call sites already show that.
- **Class docstring** — short identity of “one X” (feeds case briefings). Put
  the long story in the module docstring, not here.
- **Asset models** (`FileMappedPydanticMixin` / nested pydantic) — docstring
  states purpose (including *why* the data exists, e.g. enough identity to
  delete an index row later). Include an explicit **TODO** to replace the
  placeholder attribute layout.
- **Trustworthy states** — put each alias's trustworthy FSM states directly on
  its `AssetSpec` via `trust_states={...}` (or `frozenset({...})`). Do **not**
  emit a parallel `asset_trust_states` ClassVar or a pile of module-level
  `NEEDS_*` constants.

### Hooks to emit

For every name that appears in the confirmed `fsm_state_chains`:

- One `async def perform_<trigger>(self, tctx: EventData)` for every trigger
  that does real work, docstring stating what it must read/write/call — drawn
  from the interview, not invented. Import `EventData` from
  `transitions.core` (the transitions trigger-context object — not the case
  event journal). Annotate `tctx: EventData` on every hook that takes it
  (`perform_` / `before_` / `after_` / `guard_` / `on_enter_` / `on_exit_`).
- One `async def guard_<guard>(self, tctx: EventData) -> bool` for every
  method guard named in a bracket group (`trigger [guard]`), docstring
  stating the condition it decides.
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
assertions). Give `_not_implemented` a **one-line** docstring only (remove
with the stubs that call it); do not expand that lecture into the module
header. Never write the actual `perform_`/`guard_` logic, even if it looks
obvious or short — that is the developer's implementation to write, not this
skill's job.

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

## Step 7 — Review optional design patterns

The base class now binds, but a case that runs unattended usually wants one or
two recurring enhancements the interview may not have surfaced — surviving data
past the terminal purge, capturing user feedback, retrying fallible steps,
escaping a stall, and so on.

Read `references/case_design_patterns.md` and follow its "how to use this list"
guidance: judge each pattern against *this* case, raise only the ones that
plausibly fit (one topic at a time, as a question), and adopt only on the
developer's explicit confirmation. Most cases warrant two or three, not all and
not none.

Each adopted pattern is folded in the same way the base design was built — new
states get `case_assert_*`, new triggers get `perform_`/`guard_` stubs, new
assets get an `AssetSpec` plus a pydantic class — with bodies still stubbed via
`_not_implemented`, never implemented. Adding a state or trigger can break the
FSM, so **re-run the Step 6 bind check** after folding anything in.

## Step 8 — FSM lint + coverage checklist

Before the checklist, run the finished declaration through the chain-DSL CLI
once. The Step 6 import gate proves the FSM *parses and validates*; the CLI
adds advisory lint the import cannot — every edge auto-firing (a case that
sprints to terminal unattended), an unguarded auto edge shadowing its declared
siblings, and a state that can auto-block forever (all method-guarded auto
exits, no manual exit, no timed escape):

```bash
python -c "
from <module path> import <GeneratedClass> as C
c = C.fsm_state_chains
print(c if isinstance(c, str) else '\n'.join(c))" \
  | python -m totodev_pub.folder_backed_case_support.state_chain_cli
```

Resolve each warning with the developer, or note explicitly why it is intended
for this case (e.g. a deliberately fully-automated pipeline). Then confirm:

- [ ] Every trigger named in `fsm_state_chains` has a `perform_<trigger>` (or
      an explicit note on why not).
- [ ] Every method guard named in a bracket group (`trigger [guard]`) has a
      matching `guard_<guard>`.
- [ ] Every **auto** self-loop (`A -- … --> A`) has at least one named method
      guard; otherwise the edge is manual (`== … ==>`). Factual
      `@DWELL`/`@FAIL` alone is not enough for auto self-loops.
- [ ] Every state has at least one `case_assert_<state>_*`, or an explicit
      note on why that state has nothing to assert.
- [ ] `asset_aliases` trust_states/loader/keep/many are all set (or
      `flexible_asset_alias_loading = True` was deliberately chosen instead),
      with `trust_states={...}` inlined on each `AssetSpec` (not a parallel
      ClassVar map or orphan `NEEDS_*` module constants).
- [ ] The class carries `@case_type_registry.register`.
- [ ] Module docstring narrates business purpose and includes the IMPORTANT
      first-draft warning; it does **not** mention this skill, generation
      meta, or `_not_implemented` mechanics.
- [ ] Each asset pydantic model has a purpose docstring plus a TODO to replace
      the placeholder attribute layout.
- [ ] Transition hooks annotate `tctx: EventData` (`from transitions.core
      import EventData`); no bare `tctx` parameters.
- [ ] No stub contains real logic — only a responsibility docstring and a
      single `return self._not_implemented(<default>)` line.
- [ ] If the case ingests files: initial state is inert (e.g. `new` /
      `initial`) and intake is a kwargs-bearing trigger — **or** the
      developer explicitly chose create-time import and that tradeoff is
      noted. Do not silently put file copy/import in `create_case_in_folder`.
- [ ] The chain-DSL CLI (above) reports no unaddressed lint warnings — or each
      remaining one is acknowledged as intended for this case.

## Step 9 — Offer a case briefing

Once the class binds cleanly (Step 6) and the coverage checklist (Step 8) is
satisfied, **ask** the developer whether they'd like a **case briefing** for
the class — don't generate it unprompted; it's a few extra seconds of output
some developers won't want.

A case briefing is the class-level design handoff produced by
`totodev_pub.folder_backed_case_support.case_doc`: lifecycle diagram plus
states/triggers/guards/assertions/asset-alias tables. It is *not* a live
status report (no folder, no current state).

If they say yes, generate it:

```python
from totodev_pub.folder_backed_case_support.case_doc import generate_case_docs
print(generate_case_docs(<GeneratedClass>))
```

or, equivalently, from the CLI:

```bash
python -m totodev_pub.folder_backed_case_support.case_doc <module path>:<GeneratedClass>
```

Like Step 6's import check, this touches the class only — no case folder is
created. It pulls the first paragraph of every docstring this skill just
wrote, and doubles as a last sanity check: a trigger, guard, or state whose
docstring came out thin or missing stands out immediately in the rendered
tables — worth fixing before handing the file back if so.

Show the case briefing to the developer directly (or save it alongside the
generated class if they'd like a copy) — don't just report that it
succeeded.
