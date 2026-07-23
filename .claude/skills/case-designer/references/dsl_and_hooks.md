# FolderBackedCase reference: FSM design, DSL, hooks, assertions, assets

Condensed for skeleton generation. Authoritative source (read it if anything here
seems ambiguous or out of date): `src/totodev_pub/folder_backed_case_support/folder_backed_case_interface.py`.
Worked skeleton shape: `assets/case_class_template.py` in this skill.

## Contents

- FSM design principles (read before drafting chains)
- FSM state-chain DSL grammar
- Hook naming conventions (what to stub)
- Trigger chokes (`fsm_trigger_chokes`)
- Assertion convention (what to stub)
- AssetSpec fields
- Minimal class skeleton shape

## FSM design principles

**Agent instructions.** Apply this section while interviewing and drafting
`fsm_state_chains` (`SKILL.md` Steps 1–2). The grammar below encodes a design;
it does not invent one. Prefer restating a coarse happy path in plain English
and confirming it before adding states “for documentation.” Optional post-bind
enhancements (retry catalogs, dwell escapes, feedback assets, …) live in
`case_design_patterns.md` — raise those after the base class binds, except
inert intake when files enter the case.

### Start simple; split on purpose

There is a natural tension between a simple model with a few coarse steps
(e.g. `new → open → closed`) and a complex model with granular substeps.
**Default to the coarsest honest model** that still matches how the business
talks about the work. Extra states are cheap to add later and expensive to
rename once `perform_` / `guard_` / `case_assert_` / `AssetSpec.states` all
hang off the names.

Only propose a split when you can name a concrete reason from the checklist
below — not because a narrative “feels like it should have more boxes.”

### Mirror the users’ mental model

FSMs are most useful when terminology matches vocabulary operators and
developers already share:

| Kind | Prefer | Examples | Describes |
|---|---|---|---|
| **Triggers** | present-tense verbs | `apply_ocr`, `transmit_file`, `finalize`, `approve` | the action or change if that trigger fires |
| **States** | adjectives or nouns | `ready`, `finished`, `retrieved`, `ocrd` | current condition, or the step just completed |

If renaming a state or trigger to match how people already talk about the work
improves clarity, do that over inventing framework jargon (`phase_2b`,
`proc_ok`).

### Names must be Python identifiers

Triggers, states, and guards map to method names (`perform_<trigger>`,
`guard_<guard>`, `on_enter_<state>`, `case_assert_<state>_<slug>`). Use only
characters legal in a Python identifier segment: letters, digits, underscore.
No hyphens, spaces, punctuation, or Unicode “pretty” dashes. Keep names short
enough that the generated methods stay readable.

### When to break a coarse step down

Good reasons to split:

1. **Granular retry / recovery.** If part of a multi-step `perform_` fails
   often, splitting lets `@FAIL` retry or divert that edge without redoing the
   whole chunk.
2. **One choke resource per trigger.** Chokes are semaphores. A trigger that
   needs `"cpu"` *and* `"llm"` (or any stack of resources) must acquire several
   locks and tends to stall under load. Split so each trigger lists about one
   resource — see Trigger chokes below.
3. **Divergent error handling.** If failure at point A needs a different divert
   path than failure at point B, put A and B in different states so the FSM
   can express both.
4. **Workbench “breakpoints.”** Save or copy a case parked in a specific state
   and use `CaseWorkbench` (`totodev_pub.case_testing`) to isolate and retest
   one trigger. Granular states act like built-in breakpoints in the lifecycle.
5. **Pool fairness.** Orchestrators like `CaseManager` typically give other
   cases a chance to proceed after a case reaches a new state. A single fat
   `perform_` that runs for a long time hoggs the turn; smaller steps share the
   pool better.
6. **Inbuilt documentation** — useful, easy to overdo. Do not split *only* to
   narrate internal function calls.

Weak or insufficient reasons on their own: “we might want metrics someday,”
“every function deserves a state,” “the slide deck had seven boxes.”

### Design from the happy path outward

1. Draw the primary success path from initial state to terminal success.
2. Confirm that path with the developer in plain English (numbered steps).
3. Only then layer: failure / `@FAIL` divert, `@DWELL` escapes, secondary
   success paths, human gates, and wildcard edges.

Showing a tidy happy-path chain first is cheaper than debating a fully loaded
graph the developer cannot yet visualize.

### Manual vs automated edges

Human intervention — attach a document, approve an action, archive a record,
confirm extraction — is usually a **manual** edge (`== trigger ==>`). Automated
pipeline steps use `-- trigger -->` so `case_advance()` can fire them
unattended.

If unsure whether a step should run without a person, use `==`. Auto-advance
is opt-in (see grammar defaults below).

**Same-state edges** (`A == trigger ==> A` / `A -- trigger --> A`) are allowed.
`AdvanceResult.progressed` is true whenever a trigger commits successfully,
including when the state name does not change. An **auto** self-loop must
declare at least one **method** guard (e.g. `ready -- tick [still_needed] --> ready`)
so it cannot accidentally fire on every `case_advance()` and spin forever;
factual `@DWELL` / `@FAIL` alone does not satisfy that check. Manual (`==`)
self-loops need no method guard.

### Wildcard-source triggers

Use `*` when the same action is valid from (almost) any live state — cancel,
abort, admin escape:

```text
* == abort ==> aborted --> [*]
```

Keep wildcards for cross-cutting control actions. Do **not** wildcard the
normal domain flow; that hides which states are real and makes unreachable /
dead-end validation harder to reason about.

### Chokes, splitting, and pool behavior

- Declare `fsm_trigger_chokes` only for triggers that contend for a shared,
  capacity-constrained resource.
- Prefer **at most one resource name per trigger**; split work rather than
  stacking semaphores.
- Remember pool fairness: after a state change, other cases often get a turn —
  another reason not to cram an entire pipeline into one trigger.

### Anti-patterns

- **State-per-statement** — one state for every line of `perform_` logic.
- **Multi-choke triggers** — `{"cpu", "api", "llm"}` on a single edge.
- **Alien names** — jargon the business would not use on a whiteboard.
- **Heavy work in `create_case_in_folder`** — prefer inert intake + a manual
  attach/import trigger (see `case_design_patterns.md` pattern 0 / `SKILL.md`
  Step 2).
- **Failure states with no story** — a divert into `failed` with no retry,
  human gate, or terminal path.
- **Happy-path wildcards** — `* -- process --> …` instead of naming real sources.
- **Splitting “for docs” only** — narrate in docstrings and assertions; keep
  the graph honest and lean.

### Worked sketches: coarse vs split

**Coarse (good starting point)** — one processing step, one choke, simple
failure sits until someone intervenes:

```text
[*] --> new == add_attachments ==> attachments_added -- begin --> submitted -- process --> done --> [*]
* == abort ==> aborted --> [*]
```

**Split on purpose** — OCR is flaky and CPU-bound; extraction uses an LLM;
you want retries on OCR only, separate chokes, and a Workbench park point
after OCR:

```text
[*] --> new == add_attachments ==> attachments_added -- begin --> submitted
submitted -- apply_ocr [@FAIL<3] --> ocrd -- extract_text --> extracted -- finalize --> done --> [*]
submitted -- give_up [@FAIL>=3] --> needs_review == resolve ==> closed --> [*]
* == abort ==> aborted --> [*]
```

with chokes like `{"apply_ocr": {"cpu"}, "extract_text": {"llm"}}`.

Same business outcome as the coarse model, but retry, resource acquisition,
error divert, and “retest `extract_text` from an `ocrd` fixture” are expressible
in the graph. Do not start here — arrive here when the interview surfaces those
needs.

## FSM state-chain DSL (`fsm_state_chains`)

A Mermaid-flavoured chain notation. Each chain reads left-to-right as
alternating states and labeled connectors — `A -- t1 --> B -- t2 --> C` — and
the parser merges all chains into one graph. `fsm_state_chains` may be a list
of chain strings **or a single triple-quoted multiline string** (one chain per
line); **prefer the multiline-string form once a declaration has three or more
chains** — it reads like the diagram it is and takes `%%` comments (see
"Multiline declarations" below).

| Syntax | Meaning |
|---|---|
| `[*] --> state` | boundary hop marking `state` an **initial** state (entry/root) |
| `state --> [*]` | boundary hop marking `state` **terminal** (auto-purges assets not kept, soon after entry) |
| `A -- trigger --> B` | **automated** edge — `case_advance()` may fire it unattended |
| `A == trigger ==> B` | **manual** edge (matched `==`…`==>` arrows) — only an explicit `await case.trigger()` fires it; never auto-fires |
| `A --> B : trigger` | colon-labeled single edge (Mermaid stateDiagram-v2 style), **auto**; one edge per line |
| `A ==> B : trigger` / `A --> B : == trigger` | colon-labeled **manual** edge — both spellings are official; generated diagrams emit the `: ==` label-marker form |
| `A -- trigger [guard] --> A` | **auto self-loop** — same-state auto edge; **requires** ≥1 method guard (framework rejects unguarded `A -- trigger --> A`) |
| `A == trigger ==> A` | **manual self-loop** — same-state manual edge; no method guard required |
| `trigger [guard]` | method guard in trailing brackets: binds `guard_<guard>(self, tctx) -> bool`; edge fires only when it returns truthy |
| `trigger [guard1, guard2]` | **multiple guards** on one edge — all must pass (`conditions` = `guard_guard1`, `guard_guard2`, …). One bracket group per edge, comma-separated, trigger first. |
| `trigger [@DWELL(>\|>=\|<\|<=)<dur>]` | factual guard: time in current state vs `1s/2m/3h/4d`. Use for **timed escapes** so a state can't rot forever (pair with a `==` human gate or an automated pipeline step). |
| `trigger [@FAIL(>\|>=\|<\|<=)n]` | factual guard: failed-transition-attempt count since entering current state. Use for **retry-then-divert**: e.g. `retry [@FAIL<3]` + `give_up [@FAIL>=3]` as **separate edges**. Default (no `@FAIL` given) is an implied `@FAIL<1` — one try, then stop. |
| `trigger~<dur>` | soft timeout on the trigger's `perform_` work (flags slow; hard-aborts at a multiple) — glued to the trigger name, before any bracket group |
| `* -- trigger --> X` / `* == trigger ==> X` | wildcard source (bare `*`) — this edge exists from every live state |

An edge label is always the trigger name first, then an optional glued `~<dur>`
soft timeout, then at most **one** bracketed guard list. Method guards and
factual guards mix freely in the same brackets, comma-separated
(`finish [funded, @FAIL<3]`, `retry~3m [@FAIL<3, @DWELL>30m]`). At most one
guard per fact name (`@FAIL` / `@DWELL`) on a given edge.

**Bracketed `[*]` vs bare `*`:** `[*]` is the start/end **boundary**
pseudo-state (as in Mermaid stateDiagram-v2); bare `*` is the **any-state
wildcard source**. Boundary hops are label-less (no trigger, no colon form)
and may sit inline at either end of a chain
(`[*] --> a -- t --> b --> [*]`) or stand alone as their own line (`[*] --> a`).

### Multiline declarations and pasted Mermaid sketches

A multiline `fsm_state_chains` string is split on newlines, one chain per
line. `%%` starts a comment (whole-line or trailing) — **not** `#` — and
Mermaid boilerplate lines (`stateDiagram-v2`, `direction LR`, …) are ignored,
so a sketch drawn in a Mermaid editor pastes in nearly verbatim.

**Paste caution:** in this DSL `-->` means AUTO-ADVANCE, and a pure Mermaid
sketch is all `-->` — i.e. all-auto. After pasting, mark every human/event
gate manual (`==>` arrows, or the `: ==` label marker on a colon-form line)
before driving the case.

**CLI intake help:** the chain-DSL CLI can check a declaration and clean a raw
sketch — use it on the user's pasted Mermaid before scaffolding:

```bash
# validate + advisory lint (warns on all-auto, shadowed auto siblings, stall risk)
pbpaste | python -m totodev_pub.folder_backed_case_support.state_chain_cli

# convert a raw sketch: slugs free-text labels, scaffolds trigger names for
# unlabeled edges (`%% TODO` markers), comments out notes/aliases/choice states
pbpaste | python -m totodev_pub.folder_backed_case_support.state_chain_cli --convert
```

For the notation-by-notation mapping, the semantic gaps conversion cannot
bridge, and how to re-express Mermaid constructs the DSL lacks (choice, fork,
composite states), see `mermaid_and_the_dsl.md` in this folder.

Two defaults to keep in mind while drafting chains with the user:

- **Auto-advance is opt-in.** If unsure whether a step should run unattended, use `==` — the case waits rather than blowing past a human gate.
- **Retry is opt-in.** No `@FAIL` guard means one attempt, then the failure just sits there (visible, not hammered).
- **Auto self-loops need a method guard.** Prefer an intermediate state when “loop back” is really a different step; if you keep `A -- t --> A` automated, name a method guard that eventually declines. `@DWELL` / `@FAIL` alone do not count.

Validated at class-definition time: misspelled states, unreachable states, dead-end
non-terminal states, unguarded auto self-loops, and orphan hook methods (below) all
fail at import.

## Hook naming (what gets a stub)

Every name below is wired purely by matching the trigger/state/guard names used
in `fsm_state_chains`. A method whose suffix matches nothing in the DSL fails at
bind time — so only stub what the chains actually name.

| Pattern | Signature | When it fires | Stub for |
|---|---|---|---|
| `perform_<trigger>` | `async def (self, tctx)` | the trigger's main work; auto-wired as `before_<trigger>` if no explicit `before_<trigger>` exists | every trigger named in the chains that does real work |
| `before_<trigger>` | `async def (self, tctx)` | before the transition, only if you need this *and* a separate `perform_` | only if the developer distinguishes "before" from "perform" |
| `after_<trigger>` | `async def (self, tctx)` | after the transition commits | only if the developer names post-transition work |
| `guard_<guard>` | `async def (self, tctx) -> bool` | polled (possibly many times); must be fast, idempotent, side-effect free | every method guard named in a bracket group (`trigger [guard]`) in the chains |
| `on_enter_<state>` | `async def (self, tctx)` | on entering `<state>` | only states the developer says need entry side effects |
| `on_exit_<state>` | `async def (self, tctx)` | on leaving `<state>` | only states the developer says need exit side effects |

`tctx` is the `transitions` `EventData` object (not the event journal); kwargs
passed to a direct trigger call land in `tctx.kwargs`. Keep those kwargs
JSON-serializable — parts of the `CaseManager` framework may persist or relay
them. Raising in a guard or `before_` hook aborts the transition and counts as a
failed attempt (feeds `@FAIL`).

Hooks must be well-behaved async — they share one event loop with every other
live case. Long/blocking work belongs behind `case_invoke_threaded()` (in-process
blocking call) or `case_invoke_process()` (external CLI/subprocess), not stubbed
inline — note the need for it in the docstring but do not implement it.

## Trigger chokes (`fsm_trigger_chokes`)

Maps a trigger name to a set of resource-name strings, e.g.
`{"check_eligibility": {"llm"}, "validate_documents": {"cpu"}}`. It is purely a
**case-level annotation** — the case itself does nothing with it. Orchestrators
like the `CaseManager` read it when managing a pool of running cases, primarily
to **limit how many cases run a given trigger's `perform_` work concurrently**
when they contend for the same scarce resource (a CPU/GPU, a bandwidth-limited
API, an LLM endpoint).

- Chokes gate **trigger steps** — the work in `perform_<trigger>` — not states,
  guards, or assets.
- **Prefer at most one resource per trigger.** Chokes are enforced with
  semaphores; a trigger listing several must acquire several semaphores, which
  tends to slow the whole case's lifecycle. Split the work across triggers or
  pick the dominant constraint rather than stacking resources onto one trigger.
- Resource *names* are free-form strings the deployment agrees on; the actual
  concurrency limit per name is configured where cases are run, not here.
- Many case types need no chokes. Only list a trigger that genuinely contends
  for a shared, capacity-constrained dependency.

## Assertions (`case_assert_<state>_<slug>`)

Plain **synchronous** method, one per state-shape invariant worth checking:

```python
def case_assert_<state>_<slug>(self, ltx) -> None | str:
    """Return a falsy value to pass, or a message string describing the failure."""
    return self._not_implemented(None)
```

- Runs once per entry into `<state>`, after `on_enter_`/`after_` hooks (and for a
  terminal state, before the asset purge).
- `ltx` is the last-transition snapshot (`from_state`, `trigger`, `to_state`).
- Failures are observational only (logged, do not affect `@FAIL` or block anything).
- A name that doesn't match a known state fails loudly at bind time.
- Write one for every state whose entry has an expected shape — code review looks
  for coverage here, so don't skip states just because they feel obvious.

## `AssetSpec` fields (`asset_aliases`)

```python
AssetSpec(
    alias="...",            # required, no path separators or glob chars
    relative_path="...",    # path under assets/, or a glob when many=True
    loader=SomeModel,       # a FileMappedPydanticMixin subclass, `Path` (identity), or a Callable[[Path], Any]
    states={"state_a", ...},# states in which this asset is trustworthy; omit only if flexible_asset_alias_loading=True
    keep=True,               # declare here (not in on_terminating) for assets ALWAYS worth keeping past termination
    many=False,              # True => relative_path is a glob; case_load_assets() returns a list
)
```

`flexible_asset_alias_loading = True` on the class relaxes the loader/states
requirement for informal aliases — leave it `False` (the default) unless the
developer explicitly wants that.

## Stub convention: warn-and-continue, not raise

Stub bodies do NOT `raise NotImplementedError`. Instead every generated class
gets one small scaffolding helper, and every stub calls it:

```python
def _not_implemented(self, retval: Any = None) -> Any:
    """Log that a stub ran (naming the caller), then return the stubbed-in default."""
    caller = sys._getframe(1).f_code.co_qualname  # e.g. "MyCase.guard_eligible"
    self.log.warning("STUB not implemented: %s", caller)
    return retval
```

This logs a WARNING to the case's own `self.log` (case.log tee) instead of
crashing, so the developer can drive/simulate the lifecycle (`case_advance()`
in a scratch script or test) before any hook has real logic. The helper reads
the caller's qualified name via `sys._getframe(1)` (`co_qualname` is fine —
this project requires Python >= 3.11), so stubs never hardcode their own
method name (and can't silently drift if a trigger gets renamed mid-design).
Each stub's entire body (after its docstring) is exactly one line —
`return self._not_implemented(<default>)` — so replacing a stub with a real
implementation means deleting that one line and writing the body. The
`<default>` is whatever the stub's signature requires:

| Stub kind | Return type | Stubbed body | Why |
|---|---|---|---|
| `perform_`/`before_`/`after_`/`on_enter_`/`on_exit_`/`on_terminating` | `None` | `return self._not_implemented(None)` | return value is ignored; `None` keeps it honest |
| `guard_<guard>` | `bool` | `return self._not_implemented(True)` | lets a simulated run walk past the edge; pass `False` if the guard should block until real logic lands |
| `case_assert_<state>_<slug>` | `None \| str` | `return self._not_implemented(None)` | falsy = pass, so a known-unimplemented check doesn't spam `CASE_ASSERT_FAILED` events — the log WARNING is the visible signal instead |

`_not_implemented` and every call to it are meant to be deleted once real
implementations replace the stubs — say so in its docstring/comment when
generating it.

## Minimal skeleton shape

```python
from typing import Any
import sys

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@case_type_registry.register
class MyCase(FolderBackedCase):
    """One <the recurring unit of work>, from <initial state> to <terminal state(s)>."""

    fsm_state_chains = [...]
    asset_aliases = [...]
    fsm_trigger_chokes = {...}

    def _not_implemented(self, retval: Any = None) -> Any:
        caller = sys._getframe(1).f_code.co_qualname
        self.log.warning("STUB not implemented: %s", caller)
        return retval

    async def perform_<trigger>(self, tctx):
        return self._not_implemented(None)

    async def guard_<guard>(self, tctx) -> bool:
        return self._not_implemented(True)

    def case_assert_<state>_<slug>(self, ltx) -> None | str:
        return self._not_implemented(None)

    def on_terminating(self):
        return self._not_implemented(None)  # or case_keep_files() for a real decision
```

See `assets/case_class_template.py` in this skill for a fully worked, fully
stubbed example built on a fictitious case type.
