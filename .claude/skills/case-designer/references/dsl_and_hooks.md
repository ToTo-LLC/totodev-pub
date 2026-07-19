# FolderBackedCase reference: DSL, hook naming, assertions, assets

Condensed for skeleton generation. Authoritative source (read it if anything here
seems ambiguous or out of date): `src/totodev_pub/folder_backed_case_support/folder_backed_case_interface.py`.
Worked example: `notebooks/DEVDAVE/case_manager_classes/Tutorial 1 - Building Case-Centric Systems with FolderBackedCase.md`.

## Contents
- FSM state-chain DSL grammar
- Hook naming conventions (what to stub)
- Trigger chokes (`fsm_trigger_chokes`)
- Assertion convention (what to stub)
- AssetSpec fields
- Minimal class skeleton shape

## FSM state-chain DSL (`fsm_state_chains`)

A list of chain strings, each `stateA--trigger-->stateB`, optionally chained
(`A--t1-->B--t2-->C`). The parser merges all chains into one graph.

| Syntax | Meaning |
|---|---|
| `^state` | leading `^` = an initial state |
| `state^` | trailing `^` = a terminal state (auto-purges assets not kept, soon after entry) |
| `A--trigger-->B` | **automated** edge — `case_advance()` may fire it unattended |
| `A==trigger-->B` | **manual** edge — only an explicit `await case.trigger()` fires it; never auto-fires |
| `guard#trigger` | binds `guard_<guard>(self, tctx) -> bool`; edge fires only when it returns truthy |
| `@DWELL(>|>=|<|<=)<dur>#trigger` | factual guard: time in current state vs `1s/2m/3h/4d`. Use for **timed escapes** so a state can't rot forever (pair with a `==` human gate or an automated pipeline step). |
| `@FAIL(>|>=|<|<=)n#trigger` | factual guard: failed-transition-attempt count since entering current state. Use for **retry-then-divert**: e.g. `@FAIL<3#retry` + `@FAIL>=3#give_up`. Default (no `@FAIL` given) is an implied `@FAIL<1` — one try, then stop. |
| `trigger~<dur>` | soft timeout on the trigger's `perform_` work (flags slow; hard-aborts at a multiple) |
| `*--trigger-->X` / `*==trigger-->X` | wildcard source — this edge exists from every live state |

Two defaults to keep in mind while drafting chains with the user:
- **Auto-advance is opt-in.** If unsure whether a step should run unattended, use `==` — the case waits rather than blowing past a human gate.
- **Retry is opt-in.** No `@FAIL` guard means one attempt, then the failure just sits there (visible, not hammered).

Validated at class-definition time: misspelled states, unreachable states, dead-end
non-terminal states, and orphan hook methods (below) all fail at import.

## Hook naming (what gets a stub)

Every name below is wired purely by matching the trigger/state/guard names used
in `fsm_state_chains`. A method whose suffix matches nothing in the DSL fails at
bind time — so only stub what the chains actually name.

| Pattern | Signature | When it fires | Stub for |
|---|---|---|---|
| `perform_<trigger>` | `async def (self, tctx)` | the trigger's main work; auto-wired as `before_<trigger>` if no explicit `before_<trigger>` exists | every trigger named in the chains that does real work |
| `before_<trigger>` | `async def (self, tctx)` | before the transition, only if you need this *and* a separate `perform_` | only if the developer distinguishes "before" from "perform" |
| `after_<trigger>` | `async def (self, tctx)` | after the transition commits | only if the developer names post-transition work |
| `guard_<guard>` | `async def (self, tctx) -> bool` | polled (possibly many times); must be fast, idempotent, side-effect free | every `guard#trigger` name used in the chains |
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
