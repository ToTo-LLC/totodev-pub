# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
StateChainParser: a Mermaid-flavoured DSL for declaring a case's finite-state
machine as chain strings, plus the dumb data structure it renders into.

A declaration is a list of chain strings, or a single multiline string (one chain
per line, `%%` comments allowed). Each chain reads left-to-right as alternating
states and the connectors that join them:

    fsm_state_chains = \"\"\"
        %% ticket lifecycle
        [*] --> new -- open_ticket --> open == close_ticket ==> closed --> [*]
        open --> closed : mark_as_duplicate [is_duplicative]
        * --> auto_closed : non_responsive [@DWELL>14d]
    \"\"\"

States
------
* A bare name is an ordinary state. Names use `[A-Za-z0-9_]` only (no dashes), so
  identifier-based hook conventions like `on_enter_<state>`/`on_exit_<state>` map
  directly to DSL state names.
* `[*]` is the BOUNDARY pseudo-state (as in Mermaid stateDiagram-v2). A label-less
  `[*] --> state` hop marks `state` INITIAL (a valid entry/root); a label-less
  `state --> [*]` hop marks it TERMINAL (entering one fires the two-phase
  termination hook). Hops may sit inline at a chain's ends
  (`[*] --> a -- t --> b --> [*]`) or stand alone as their own line
  (`[*] --> a`). Initial states are also the reachability anchors (they are
  exempt from the "must have an incoming edge" rule), which is how an entry
  reached only via case_reclassify_to() is declared. Initial and terminal are
  independent flags and may compose (`[*] --> x --> [*]`).

Connectors
----------
Two matched-arrow connector forms join states, each carrying a label:

* `A -- label --> B` marks the edge AUTO-ADVANCE: advance() (looped by a driver) may
  fire it unattended.
* `A == label ==> B` marks the edge MANUAL: only a direct `await case.trigger()` (or a
  pinned `case_advance(trigger, ...)`) fires it. Auto is opt-in (fail-safe): a manual
  edge never auto-fires, so a case simply waits rather than silently running past a
  human/event gate. The connector form itself carries this "may auto-fire" policy.
* Same-state edges (`A -- label --> A` / `A == label ==> A`) are legal. An AUTO
  self-loop MUST carry at least one method guard — validated in
  `FsmChainSpec.validate()` — so authors cannot accidentally declare an unguarded edge
  that fires on every `case_advance()` and spins forever / starves sibling auto edges.
  Manual self-loops need no method guard; factual guards (`@DWELL` / `@FAIL`) alone do
  not satisfy the auto self-loop check.

Colon-labeled single edges (Mermaid stateDiagram-v2 style)
----------
A line may instead declare ONE edge with its label after a colon:

    A --> B : label            (auto)
    A ==> B : label            (manual)
    A --> B : == label         (also manual: the leading `==` label marker is how a
                                manual edge is spelled inside a plain `-->` line,
                                keeping the line valid Mermaid stateDiagram-v2)
    A --> B : "label"          (optional quotes — ignored by the parser; used so
                                Mermaid can render hostile characters like
                                `[@DWELL>14d]` in generated diagrams)

Both spellings of manual are equivalent; generated diagrams emit the label-marker
form with quotes around the whole label. The colon form is one edge per line —
use inline connectors for multi-hop chains. `[*]` boundary hops take no label and
therefore have no colon form.

Labels  `trigger[~<dur>] [guard, ...]`
----------
* The label's first token is the TRIGGER name.
* `trigger~<dur>` attaches a SOFT TIMEOUT to the trigger's work — glued to the trigger
  name (the `~` reads "approximately") because it bounds the trigger's EXECUTION
  rather than gating entry. The duration uses units s|m|h|d, float allowed, unit
  required: `assign~20s`, `fetch~1.5m`. It is the point past which the step is
  considered SLOW (a warning), NOT a hard kill; the hard-abort ceiling is derived by
  the case as a multiple of this value. Most triggers are expected to be fast and go
  un-annotated (they inherit the case's default); annotate the ones known to be slow.
  The budget is keyed by TRIGGER (it is a property of `perform_<trigger>`), so the
  same trigger may not be annotated with two different durations. (Stored in
  FsmChainSpec.trigger_timeouts; consumed by the case, which owns the warn/abort/log
  behavior.)
* A bracketed, comma-separated GUARD LIST may follow the trigger: `finish [funded]`,
  `retry~3m [@FAIL<3, @DWELL>30m]`. All guards must pass for the edge to fire
  (conjunction). Method and factual guards may INTERSPERSE and are stored — and
  later evaluated — LEFT-TO-RIGHT in declaration order in the transition's private
  `_guards` list (e.g. `go [one, @FAIL<2, two]` =>
  `_guards=["guard_one", {"name":"FAIL",...}, "guard_two"]`). Two kinds of item
  may appear:
    * A bare identifier names a METHOD GUARD: each token is mapped to a
      `guard_<token>` carrier method and stored as a string in `_guards`
      (e.g. `finish [funded]` => `_guards=["guard_funded"]`; the carrier defines
      `async def guard_funded`). The `guard_` prefix keeps guard methods in their own
      namespace, away from ordinary helpers and lifecycle hooks. Guards are what make
      multiple auto-advance edges from one state meaningful — advance() tries each
      auto candidate in declared order and fires the first whose guards permit. A
      method guard is also required on every auto self-loop (see above).
    * `@FACT<op>N` is a FACTUAL GUARD: an `@`-prefixed, system-computed fact compared
      against a constant with one of `< <= > >=` (equality `==`/`!=` is deliberately
      UNSUPPORTED — we cannot promise to evaluate at an exact instant/count, so an
      equality test would create false expectations). Two facts are recognized:
        * `@DWELL<op><dur>` — seconds spent in the SOURCE state (dwell since the
          latest CASE_STATE_ENTERED). The operand is a duration, units s|m|h|d, float
          allowed (`@DWELL>90s`, `@DWELL>=1.5h`, `@DWELL>0.5d`). `case_dwell_secs` on
          the case computes it. A `>`/`>=` dwell guard is SELF-RELAXING (it ripens
          with time) and is what gives a state a guaranteed TIMED ESCAPE (see
          classify()/AutoAdvanceBlocked).
        * `@FAIL<op>N` — count of `CASE_TRANSITION_FAILED` events logged since the
          current state was entered (failed pre-commit attempts to LEAVE this state).
          The operand is a bare integer, NO unit (`@FAIL<3`, `@FAIL>=3`).
          State-scoped: every failed attempt in this dwell counts regardless of which
          trigger raised. This is the retry knob — list a `[@FAIL<n]` retry edge
          first and an optional `[@FAIL>=n]` divert edge second. RETRY IS OPT-IN: an
          auto edge that declares no `@FAIL` gets an implicit `@FAIL<1` (one attempt,
          no retry) via apply_implicit_fail_cap(); a pure timed-escape edge is exempt
          and tolerates unlimited failures (logically `@FAIL>=0`).
      A factual guard is a pure FACT, NOT a promise to fire — something must still
      attempt the trigger. At most one guard PER FACT NAME per label; facts compose
      with each other and with method guards in declaration order
      (e.g. `retry~3m [funded, @FAIL<3]`).

Wildcard ("from any source") chains  `* -- label --> DEST`
----------
* A chain whose source is the bare `*` declares one edge whose source is ANY otherwise
  non-terminal state: `* == cancel ==> cancelled` means "from anywhere, `cancel` =>
  cancelled"; `* -- timeout --> expired` is the auto-advance variant (colon form works
  too: `* --> expired : timeout`). The label carries the usual guards and `~<dur>`
  soft-timeout like any other, and a trailing boundary hop is allowed
  (`* -- timeout --> expired --> [*]`).
* NOTE the distinction: bracketed `[*]` is the START/END boundary pseudo-state; bare
  `*` is the ANY-STATE wildcard source.
* Exactly one transition (one destination) per wildcard chain. The concrete per-source
  edges are deduced AFTER all chains parse and AFTER validate(): terminal states and the
  destination itself (no self-loop) are excluded, and an EXPLICIT edge for the same
  trigger from a state always overrules the wildcard there.
* Wildcards inject only AFTER the typo-catching validations run on the explicit graph, so
  they can never mask a forgotten exit or a misspelled state — a state whose ONLY outgoing
  edge would be a wildcard is still flagged; declare an explicit edge.

Multiline strings, comments, and pasted Mermaid
----------
* `fsm_state_chains` may be a single (typically triple-quoted) string: it is split on
  newlines, one chain per line. `%%` starts a comment (whole-line or trailing), as in
  Mermaid. Blank lines are skipped, and Mermaid boilerplate lines
  (`stateDiagram-v2`, `flowchart TD`, `direction LR`, ...) are ignored — so a sketch
  drawn in a Mermaid editor pastes in nearly verbatim. CAUTION when pasting: `-->`
  means AUTO-ADVANCE here, and a pure Mermaid sketch is all `-->` — mark the edges
  that must wait for a human/event as manual (`==>` or the `: ==` label marker)
  before driving the case.
* A list of chain strings works identically (each entry may itself contain newlines
  and comments).

Conventions
-----------
* Chains COLLECTIVELY must declare at least one initial and one terminal state.
* The DEFAULT initial state (used by create_case_in_folder) is the first
  initial-marked state encountered scanning chains in order — so an initial in the
  first chain wins.

CLI
---
A declaration can be checked or rendered from the shell (see `main()` / the
`state_chain_cli` launcher module):

    python -m totodev_pub.folder_backed_case_support.state_chain_cli "<chains>"
    pbpaste | python -m totodev_pub.folder_backed_case_support.state_chain_cli --render
    pbpaste | python -m totodev_pub.folder_backed_case_support.state_chain_cli --convert

The default mode parses + validates, prints a short summary (or the pointed
parse error, exit 1), and appends advisory lint warnings (`lint_spec`;
`--quiet` suppresses). `--render` emits Mermaid source instead. `--convert`
takes a raw (near-)Mermaid sketch — unlabeled edges, free-text labels, notes —
and emits a cleaned declaration with `%% TODO` markers (see `mermaid_intake`).

The parser is PURE and instance-unaware: `StateChainParser.parse(chains)` returns an
`FsmChainSpec` and binds to nothing. Whole-graph semantic checks live in the separate,
explicitly-invoked `FsmChainSpec.validate()` (the default FolderBackedCase.compile_fsm
calls it; a hand-built override decides for itself). The spec is a mutable dumb dataclass
on purpose — an override may parse-then-tweak it before it is cached as the per-class
FSM singleton.
"""

from __future__ import annotations

import inspect
import re
import warnings
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx

from totodev_pub.folder_backed_case_support.constants import (
    DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    FsmChainParseError,
    FsmBindingError,
)
from totodev_pub.optional_dependencies import raise_missing_dependency

# A state name: one or more [A-Za-z0-9_] characters (no dashes). This keeps state names
# directly representable as Python suffix identifiers for hook conventions.
_NAME_RE = re.compile(r"[A-Za-z0-9_]+")

# A SOFT-TIMEOUT suffix on the trigger: `~<dur>` (e.g. `assign~20s`). `~` reads
# "approximately", matching the soft semantics: it is the duration past which the step is
# considered SLOW, not a hard kill. The unit is accepted loosely here (optional) so a
# unit-less typo gets a pointed message from _parse_trigger_timeout rather than a confusing
# whole-connector parse failure — exactly how @DWELL handles its unit.
_TIMEOUT_SUFFIX = r"(?:\s*~\s*\d+(?:\.\d+)?\s*[smhd]?)?"

# A connector label: `trigger[~<dur>]` optionally followed by a bracketed guard list.
# The bracket CONTENTS are deliberately loose (`[^\][]*` — anything but nested brackets):
# guard items get their pointed diagnostics from _parse_label, not a whole-connector
# parse failure. The trigger head is tight (an identifier), which is what makes the
# closing arrow after the label unambiguous.
_LABEL_BODY = (
    r"[A-Za-z_]\w*" + _TIMEOUT_SUFFIX + r"(?:\s*\[[^\][]*\])?"
)

# The connector between two states, in two matched-arrow forms plus the bare boundary hop:
#   `-- label -->`  auto-advance edge
#   `== label ==>`  manual edge
#   `-->`           bare, label-less hop — legal ONLY adjacent to the `[*]` boundary
# A mismatched pair (`== label -->` / `-- label ==>`) is caught after the match and
# rejected with a pointed message. Alternation order matters: the labeled form is tried
# first so a bare `-->` can never split a labeled connector apart.
_CONNECTOR_RE = re.compile(
    r"(?P<open>--|==)\s*(?P<label>" + _LABEL_BODY + r")\s*(?P<close>-->|==>)"
    r"|(?P<bare>-->)"
)

# A `@NAME<op>NUMBER[unit]` factual-guard token (the four ordering comparators only).
_FACT_GUARD_RE = re.compile(
    r"^@\s*([A-Za-z][A-Za-z0-9_]*)\s*(<=|>=|<|>)\s*(\d+(?:\.\d+)?)\s*([smhd]?)$"
)
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# Recognized factual-guard names and the kind of operand each takes. TIME facts compare a
# duration (a unit is required); COUNT facts compare a bare integer (no unit). DWELL is the
# only time fact today (AGE/IDLE are reserved for future, distinct clocks); FAIL the only
# count fact. The `>`/`>=` form of a TIME fact is "self-relaxing" — it ripens with the mere
# passage of time, which is what classify() looks for to grant a state a TIMED ESCAPE.
_TIME_FACTS = {"DWELL"}
_COUNT_FACTS = {"FAIL"}
_RELAXING_OPS = {">", ">="}

_BOUNDARY = "[*]"          # the start/end pseudo-state (Mermaid stateDiagram-v2)
_WILDCARD_SOURCE = "*"     # a bare `*` in the source slot => "from any source"

# A colon-form line: `SRC --> DEST : label` / `SRC ==> DEST : label` (one edge per line).
# The label side is validated by _parse_label; a leading `==` label marker spells MANUAL
# inside a plain `-->` line (keeping it valid Mermaid stateDiagram-v2). SRC/DEST use a
# tight charset (names plus the `[*]`/`*`/`^` glyphs, which get their own pointed
# diagnostics) so a multi-hop chain with a colon fails THIS regex — and gets the
# one-edge-per-line message — rather than half-matching.
_COLON_FORM_RE = re.compile(
    r"^(?P<src>[\w^\[\]*]+)\s*(?P<arrow>-->|==>)\s*(?P<dest>[\w^\[\]*]+)\s*:\s*(?P<label>.*)$"
)

# An edge label, decomposed: `trigger[~<dur>] [guard, ...]`. The timeout's duration and
# the guard-list CONTENTS are loose here — their strict validation (with pointed
# messages) lives in _parse_trigger_timeout / _parse_label.
_LABEL_PARSE_RE = re.compile(
    r"^(?P<trigger>[A-Za-z_]\w*(?:\s*~\s*[^\s\][]+)?)\s*(?:\[(?P<guards>[^\][]*)\])?$"
)

# Mermaid boilerplate lines silently ignored in multiline declarations, so a diagram
# sketched in a Mermaid editor pastes in nearly verbatim.
_MERMAID_BOILERPLATE_RE = re.compile(
    r"^(?:stateDiagram(?:-v2)?|flowchart(?:\s+\w+)?|graph(?:\s+\w+)?|direction\s+\w+)\s*$",
    re.IGNORECASE,
)

# Transition-dict keys whose (string) values name a callable resolved against the carrier
# object — hand-built callbacks a carrier MUST provide. DSL method guards live in `_guards`
# (heterogeneous ordered list) and are yielded separately by `_referenced_callbacks`; factual
# @DWELL/@FAIL dicts in that list are compiled by the base class, not looked up on the carrier.
_CARRIER_CALLBACK_KEYS = ("unless", "before", "after", "prepare")

# The canonical method-name PREFIXES for the two conventions the parser/base bind by name.
# Both are kept as named constants (not bare literals) so the parser, the binding check, and
# the orphan scan can never disagree about a namespace:
#   * guard methods   `guard_<token>`   — a `trigger [<token>]` DSL guard resolves here.
#   * trigger actions `perform_<trigger>` — the side-effect hook wired to the edge's `before`.
# The trigger action hook intentionally carries NO leading underscore, matching the other
# implicit conventions (`on_enter_`, `on_exit_`, `before_`, `after_`); it is a discoverable
# extension point, not a private method.
_GUARD_METHOD_PREFIX = "guard_"
_PERFORM_METHOD_PREFIX = "perform_"
_PERFORM_HOOK_PATTERN = _PERFORM_METHOD_PREFIX + "{trigger}"

# The SINGLE source of truth for the implicit hook/guard method conventions: a method-name
# PREFIX -> the KIND of FSM name its suffix is expected to match (`state`, `guard`, or
# `trigger`). The prefix is the key because prefixes are unique; several may share a kind.
# Orphan detection (see _find_orphan_hook_methods) iterates this registry to flag a method
# that LOOKS like a hook (one of these prefixes) but whose suffix maps to no such name —
# almost always a typo. Add a new hook prefix here and the scan + its diagnostics pick it up
# automatically. The `kind` is also the word used verbatim in orphan messages ("...is not a
# known <kind>"). Prefixes are mutually non-overlapping, so match order is irrelevant.
_HOOK_METHOD_PREFIXES: dict[str, str] = {
    "on_enter_": "state",
    "on_exit_": "state",
    _GUARD_METHOD_PREFIX: "guard",
    _PERFORM_METHOD_PREFIX: "trigger",
    "before_": "trigger",
    "after_": "trigger",
}


def _is_fact_guard(item) -> bool:
    return isinstance(item, dict)


def _is_method_guard(item) -> bool:
    return isinstance(item, str)


def _method_guards(guards) -> list[str]:
    return [g for g in (guards or []) if _is_method_guard(g)]


def _fact_guards_of(guards) -> list[dict]:
    return [g for g in (guards or []) if _is_fact_guard(g)]


def _guards_identity(guards) -> tuple:
    """Stable identity for an ordered heterogeneous guard list (methods + facts)."""
    out = []
    for g in guards or []:
        if _is_fact_guard(g):
            out.append(("f", g["name"], g["op"], g["operand"]))
        else:
            out.append(("m", g))
    return tuple(out)


def _copy_guards(guards) -> list:
    """Deep-copy fact dicts; leave method-guard strings as-is."""
    return [dict(g) if _is_fact_guard(g) else g for g in (guards or [])]


def _is_async_callable(fn) -> bool:
    """True if calling `fn` yields a coroutine: a coroutine function (incl. a functools.partial
    wrapping one, which inspect unwraps on 3.8+), or a callable instance whose `__call__` is a
    coroutine function. The single fact the force_async binding check turns on."""
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


_TCTX_PROBE = object()


def _accepts_tctx(fn) -> bool:
    """True if `fn` can be invoked with a single positional argument — the trigger context
    `tctx` the machine hands to EVERY hook (the case runs with `send_event=True`). `fn` is a
    BOUND method here, so `self` is already applied; this asks whether the hook accepts that
    one `tctx` value. A `(self, tctx)` or `(self, *args)` hook qualifies; a bare `(self)`
    hook does not (it would raise TypeError the instant its edge fired). If the signature
    cannot be introspected (rare C-level callables), assume True rather than false-alarm."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    try:
        sig.bind(_TCTX_PROBE)
    except TypeError:
        return False
    return True


@dataclass
class FsmChainSpec:
    """The compiled FSM: the dumb intermediate the parser renders and FolderBackedCase
    consumes. Mutable by design (see module docstring). Every structure the machine and
    the auto-advance driver need lives here, so they can never drift apart.

    Attributes:
        states         every state name, in first-seen order.
        transitions    `transitions`-library dicts: {"trigger","source","dest"}.
                       A dict MAY also carry the private key "_guards": an ordered list of
                       method-guard strings (`guard_<token>`) and/or factual-guard dicts
                       ({"name","op","operand"}, e.g. @DWELL/@FAIL). Methods and facts may
                       intersperse; evaluation is left-to-right in declaration order. The
                       factory compiles `_guards` into `conditions` callables before the
                       machine sees the graph.
        terminal_states  states marked terminal (a `state --> [*]` boundary hop).
        initial_states states marked initial (a `[*] --> state` boundary hop); also
                       reachability anchors.
        initial_state  the DEFAULT entry state (first initial-marked, in chain order).
        auto_edges     {(source, trigger)} edges eligible for advance() (`--`/`-->`
                       connector; manual `==`/`==>` edges are absent).
        pipeline       distinct auto-advance trigger names, in first-seen order (display).
        triggers       every distinct trigger name, in first-seen order.
        primary_chain  the raw first chain string, kept for DEBUG logging / diagrams.
        pending_wildcards  unresolved `* -- ... --> dest` edges; expanded by
                       expand_wildcards().
        wildcard_dests dests of pending wildcards; exempt from validate()'s reachability
                       check (they are reached only once the wildcards are injected).
        timed_escape_states  states that own at least one auto edge guarded SOLELY by a
                       self-relaxing time fact (`@DWELL` with `>`/`>=`) — such a state can
                       never be permanently auto-blocked, because the edge ripens with time.
                       Computed by classify() (run AFTER expand_wildcards). Consulted by the
                       case to decide whether a no-progress pass means AutoAdvanceBlocked.
        trigger_timeouts  {trigger: soft_secs} for triggers carrying a `~<dur>` SOFT-timeout
                       annotation (the duration past which the step is considered SLOW, not a
                       hard kill). Keyed by TRIGGER because the budget is a property of the
                       work (`perform_<trigger>`), not the edge; the parser rejects the same
                       trigger annotated with conflicting durations. Triggers absent here take
                       the case's default; the hard-abort ceiling is derived (a multiple of
                       the soft value) by the case, not stored here.
        trigger_chokes    {trigger: frozenset[str]} resource names whose concurrent use a
                       pool driver may throttle when this case runs inside that pool. Folded
                       from the class's `fsm_trigger_chokes` at compile time; triggers absent
                       here draw on no named constrained resources.
    """
    states: list[str] = field(default_factory=list)
    transitions: list[dict] = field(default_factory=list)
    terminal_states: set[str] = field(default_factory=set)
    initial_states: set[str] = field(default_factory=set)
    initial_state: Optional[str] = None
    auto_edges: set[tuple[str, str]] = field(default_factory=set)
    pipeline: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    primary_chain: Optional[str] = None
    pending_wildcards: list[dict] = field(default_factory=list)
    wildcard_dests: set[str] = field(default_factory=set)
    timed_escape_states: set[str] = field(default_factory=set)
    trigger_timeouts: dict[str, float] = field(default_factory=dict)
    trigger_chokes: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "FsmChainSpec":
        """An FSM with nothing declared — the base ABC's default before any subclass
        supplies `fsm_state_chains` (or overrides compile_fsm)."""
        return cls()

    def is_auto(self, source: str, trigger: str) -> bool:
        """Is the edge (source -> via trigger) eligible for unattended advance()?"""
        return (source, trigger) in self.auto_edges

    def auto_edges_from(self, state: str) -> list[tuple[str, str]]:
        """Auto-advance edges leaving ``state`` as ``(trigger, dest)`` in declared order."""
        out: list[tuple[str, str]] = []
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if state in srcs and self.is_auto(state, t["trigger"]):
                out.append((t["trigger"], t["dest"]))
        return out

    def auto_triggers_from(self, state: str) -> tuple[str, ...]:
        """Auto-advance trigger names leaving ``state``, in declared order."""
        return tuple(trigger for trigger, _ in self.auto_edges_from(state))

    def has_auto_exits(self, state: str) -> bool:
        """True when ``state`` has at least one auto-advanceable (``--``) exit."""
        return bool(self.auto_triggers_from(state))

    def pending_chokes_for(self, state: str) -> frozenset[str]:
        """Union of choke resource sets over all auto exits from ``state``."""
        needed: set[str] = set()
        for trigger in self.auto_triggers_from(state):
            needed.update(self.trigger_chokes.get(trigger, frozenset()))
        return frozenset(needed)

    def validate(self) -> "FsmChainSpec":
        """Whole-graph semantic checks, kept SEPARATE from parsing so it can be invoked
        deliberately (the default compile_fsm calls it; a hand-built override may skip or
        re-run it after its own tweaks). Returns self for chaining. Raises
        FsmChainParseError on the first violation. A completely empty spec (an abstract or
        not-yet-configured subclass) is a no-op.

        Checks:
          - at least one initial (`[*] --> name`) and one terminal (`name --> [*]`)
                state exist;
          - V1: a terminal state has NO outgoing edge (it is the end of the road);
          - V2: a non-terminal state HAS an outgoing edge (a dead-end that isn't `^` is
                almost always a forgotten exit or a missing `^` — the one legitimate
                exception, a state left only via case_reclassify_to(), is handled by guidance:
                declare its successor's entry as an initial state, or add a trivial edge);
          - reachability: every non-initial state has an incoming edge (catches the
                mistyped state name, whose orphan has no way in). A wildcard's declared
                dest is exempt — it is reached only once the wildcards are injected, which
                happens AFTER validate() so the typo checks see only the explicit graph;
          - every AUTO (`--`) self-loop (`source == dest`) carries at least one method
                guard in `_guards` (factual `@DWELL`/`@FAIL` alone is not enough) so an
                unguarded auto self-loop cannot spin forever on every case_advance().
        """
        if not self.states:
            return self
        if not self.initial_states:
            raise FsmChainParseError(
                "no initial state declared; mark at least one entry state with a "
                "'[*] --> state' boundary hop, e.g. '[*] --> new'"
            )
        if not self.terminal_states:
            raise FsmChainParseError(
                "no terminal state declared; mark at least one end state with a "
                "'state --> [*]' boundary hop, e.g. 'closed --> [*]'"
            )

        out_sources: set[str] = set()
        in_dests: set[str] = set()
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            out_sources.update(srcs)
            in_dests.add(t["dest"])
            dest = t["dest"]
            trigger = t["trigger"]
            for s in srcs:
                if s != dest:
                    continue
                if (s, trigger) not in self.auto_edges:
                    continue
                if _method_guards(t.get("_guards")):
                    continue
                raise FsmChainParseError(
                    f"auto self-loop '{s} -- {trigger} --> {dest}' has no method guard. "
                    "An unguarded auto self-loop can fire on every case_advance() and spin "
                    "forever (or starve sibling auto edges from the same state). Add a "
                    f"method guard that eventually declines, e.g. "
                    f"'{s} -- {trigger} [still_needed] --> {dest}', and implement "
                    f"`async def guard_still_needed(...)-> bool`. Factual guards "
                    "(@DWELL / @FAIL) alone do not satisfy this check."
                )

        for s in self.states:
            terminal = s in self.terminal_states
            if terminal and s in out_sources:
                raise FsmChainParseError(
                    f"state {s!r} is marked terminal ('{s} --> [*]') but has an outgoing "
                    "transition; terminal states cannot be left"
                )
            if not terminal and s not in out_sources:
                raise FsmChainParseError(
                    f"state {s!r} has no outgoing transition and is not marked terminal; add "
                    f"a '{s} --> [*]' hop if it is an end state, or give it a transition (a "
                    "state left only via case_reclassify_to() should declare its successor's "
                    "entry as initial, or use a trivial edge — see the reclassify docs)"
                )
            if (s not in self.initial_states and s not in in_dests
                    and s not in self.wildcard_dests):
                raise FsmChainParseError(
                    f"state {s!r} is unreachable: it has no incoming transition and is not "
                    f"marked initial ('[*] --> {s}'). If this is a deliberate entry point, "
                    "mark it initial; otherwise it is probably a misspelled state name"
                )
        return self

    def expand_wildcards(self) -> "FsmChainSpec":
        """Inject the concrete per-source edges for any `* -- ... --> dest` wildcard chains,
        then return self for chaining. Call AFTER validate() so the typo-catching checks
        run against only the explicit graph (the default compile_fsm does exactly this;
        a hand-built override decides for itself). A no-op when there are no wildcards.

        For each pending wildcard, an edge `s -> dest` is created for every state `s`
        that is NOT terminal (terminals cannot be left) and NOT the dest itself (no
        self-loop), UNLESS `s` already has an EXPLICIT edge for that trigger — an explicit
        edge always overrules the wildcard. The ordered `_guards` list and the
        auto-advance flag from the wildcard connector are carried onto each injected edge.
        """
        if not self.pending_wildcards:
            return self
        # Snapshot (trigger, source) of the EXPLICIT edges; explicit always overrules a
        # wildcard, and this also prevents a later wildcard from re-injecting the same edge.
        claimed: set[tuple[str, str]] = set()
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            for s in srcs:
                claimed.add((t["trigger"], s))

        for w in self.pending_wildcards:
            trigger, dest = w["trigger"], w["dest"]
            for s in self.states:
                if s in self.terminal_states or s == dest:
                    continue
                if (trigger, s) in claimed:
                    continue
                td: dict = {"trigger": trigger, "source": s, "dest": dest, "_wildcard": True}
                if w.get("_guards"):
                    td["_guards"] = _copy_guards(w["_guards"])
                self.transitions.append(td)
                claimed.add((trigger, s))
                if trigger not in self.triggers:
                    self.triggers.append(trigger)
                if w["auto"]:
                    self.auto_edges.add((s, trigger))
                    if trigger not in self.pipeline:
                        self.pipeline.append(trigger)
        return self

    def classify(self) -> "FsmChainSpec":
        """Compute `timed_escape_states`: states that can never be permanently auto-blocked
        because they own an auto edge guaranteed to become fireable by the mere passage of
        time. Run AFTER expand_wildcards() so a blanket
        `* -- timeout [@DWELL>=2d] --> expired` net is reflected. Returns self for chaining.

        A state qualifies if it has an auto edge whose guards are satisfiable BY WAITING
        ALONE — i.e. the edge carries at least one self-relaxing time fact (`@DWELL` with
        `>`/`>=`), every fact item in `_guards` is such a fact, and it has NO method guards
        (an opaque method guard might never relax, so a mixed edge gives no guarantee)."""
        self.timed_escape_states = set()
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            for s in srcs:
                if (s, t["trigger"]) in self.auto_edges and self._is_pure_timed_escape(t):
                    self.timed_escape_states.add(s)
        return self

    @staticmethod
    def _is_pure_timed_escape(t: dict) -> bool:
        """True iff transition `t` is fireable by waiting alone: no method guards in
        `_guards`, and at least one fact item with EVERY fact being a self-relaxing time
        fact (`@DWELL` `>`/`>=`). An unguarded auto edge would have fired already, so it is
        not a 'timed' escape; a `@FAIL`/`<`-style guard only tightens with time, so it
        disqualifies."""
        guards = t.get("_guards") or []
        if _method_guards(guards):
            return False
        fgs = _fact_guards_of(guards)
        if not fgs:
            return False
        for fg in fgs:
            if fg["name"] not in _TIME_FACTS or fg["op"] not in _RELAXING_OPS:
                return False
        return True

    def _inject_implicit_fail_if_eligible(self, edge: dict, *, is_auto: bool) -> None:
        """Append implicit ``@FAIL<1`` to ``edge["_guards"]`` when eligible.

        Skips manual edges, edges with an explicit ``@FAIL``, and pure timed escapes.
        Mutates ``edge`` in place; no-op when not eligible.
        """
        if not is_auto:
            return
        guards = list(edge.get("_guards") or [])
        if any(_is_fact_guard(g) and g["name"] in _COUNT_FACTS for g in guards):
            return                       # explicit @FAIL policy wins
        if self._is_pure_timed_escape(edge):
            return                       # timed escape => unlimited fail tolerance
        edge["_guards"] = guards + [
            {"name": "FAIL", "op": "<", "operand": 1, "implicit": True},
        ]

    def apply_implicit_fail_cap(self) -> "FsmChainSpec":
        """Inject a default `@FAIL<1` (one attempt, NO retry) onto every AUTO edge that does
        not already declare a `@FAIL` guard and is not a pure timed escape. Retry is thus
        OPT-IN — an un-guarded auto edge halts auto-progress after a single failure rather
        than being hammered forever — mirroring how auto-advance itself is opt-in (`--`).
        Returns self for chaining. Run AFTER classify() so the injected guard never perturbs
        timed-escape detection (which keys on the EXPLICIT graph).

        Two kinds of edge are EXEMPT (left uncapped):
          * an edge with an EXPLICIT `@FAIL` guard — the author already chose the policy;
          * a pure timed-escape edge (`@DWELL>`/`>=` only) — logically `@FAIL>=0`, it
            tolerates any number of failures, so a safety-net timeout is never disabled by a
            transient failure.
        Only AUTO edges are touched: manual/event-driven (`==`) triggers are never
        driven by advance(), so a retry cap on them would have no meaning.

        Abstract `pending_wildcards` rules are updated in lockstep with expanded auto
        edges so `to_networkx()` / case briefings show the same implied cap the runtime
        expanded transitions already carry (display consistency only — advance() fires the
        expanded edges).

        The injected guard dict carries an extra `"implicit": True` key (absent from every
        author-written fact guard) — the ONLY way to later tell "the DSL declared this" from
        "the compiler defaulted it in", since the two are otherwise bit-for-bit identical
        once compiled. `FsmChainSpec.to_networkx(include_implied_caps=False)` is what reads
        this marker to exclude implied caps from a rendering that wants to show only what the
        author actually wrote."""
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            is_auto = any((s, t["trigger"]) in self.auto_edges for s in srcs)
            self._inject_implicit_fail_if_eligible(t, is_auto=is_auto)
        for w in self.pending_wildcards:
            self._inject_implicit_fail_if_eligible(w, is_auto=bool(w.get("auto")))
        return self

    # ---- rendering (visualization / analysis) ----
    # A read-only VIEW of the compiled spec for tools that want a graph library's worth of
    # algorithms (layout, shortest-path, cycle detection, ...) rather than this dataclass's
    # bespoke accessors. Kept OFF the hot path: `networkx` is imported lazily inside the
    # method body (an optional dependency), so a case that never calls to_networkx() never
    # pays for it.

    def to_networkx(
        self, *, wildcard_pseudo_state: bool = False, include_implied_caps: bool = True,
    ) -> "nx.MultiDiGraph":
        """Render this spec as a `networkx.MultiDiGraph`, losslessly: every state, transition,
        guard, timeout, choke, and initial/terminal/timed-escape flag the DSL can express is
        reproduced as a node/edge/graph attribute. Safe to call at ANY point in the compile
        pipeline (right after parse(), or after validate()/expand_wildcards()/classify()) —
        it never mutates `self` and never requires a particular stage to have run.

        A `MultiDiGraph` (not a plain `DiGraph`) is used deliberately: two distinct edges may
        share the same (source, dest) pair — e.g. two differently-guarded transitions on the
        same trigger, or two different triggers between the same states — and collapsing them
        would silently drop a transition. Each entry in `self.transitions` becomes its own
        edge; NetworkX auto-assigns the multi-edge key.

        Nodes are every name in `states`, with attributes:
          * initial          -- state in `initial_states` (a '[*] --> name' hop)
          * default_initial  -- state == `initial_state` (the create_case_in_folder default)
          * terminal         -- state in `terminal_states` (a 'name --> [*]' hop)
          * timed_escape      -- state in `timed_escape_states` (set only if classify() has
                                 run; False otherwise -- this is the one field whose fidelity
                                 depends on compile stage, since it is itself computed by a
                                 pipeline step)

        Edges are every dict in `transitions`, with attributes:
          * trigger              -- the trigger name
          * auto / manual         -- whether (source, trigger) is in `auto_edges` (`--`) or not
                                     (`==`); mutually exclusive
          * guards                -- ordered heterogeneous list of method-guard names
                                     (`guard_<token>` strings) and factual-guard dicts
                                     ({"name","op","operand"}), in declaration order. `name` is
                                     `"DWELL"` (time, `operand` in seconds) or `"FAIL"` (count,
                                     `operand` a bare int). An entry `apply_implicit_fail_cap()`
                                     injected (rather than the author declaring it) carries an
                                     extra `"implicit": True` key — the one case where an
                                     author-written guard and a compiler-defaulted one are
                                     otherwise bit-for-bit identical. See `include_implied_caps`.
          * soft_timeout_secs     -- this trigger's effective soft-timeout in seconds: its
                                     `~<dur>` DSL annotation if explicit, else (subject to
                                     `include_implied_caps`) the same
                                     `DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS` fallback
                                     `FolderBackedCase.trigger_warn_secs()` uses. NOTE: that
                                     fallback is a global constant, NOT the live value of a
                                     per-case override of `trigger_warn_secs()` -- this spec is
                                     a per-CLASS, instance-independent artifact and has no
                                     visibility into an overridden instance method.
          * soft_timeout_is_explicit -- True iff this trigger carries an explicit `~<dur>`
                                     annotation (i.e. `soft_timeout_secs` is NOT a filled-in
                                     default); always accurate regardless of
                                     `include_implied_caps`
          * chokes                -- frozenset of choke resource names for this trigger
          * pure_timed_escape     -- True iff THIS edge alone is fireable by waiting alone
                                     (see `_is_pure_timed_escape`); finer-grained than the
                                     node-level `timed_escape` flag, which only says the STATE
                                     has such an edge somewhere
          * wildcard_expanded     -- True if this edge was injected by expand_wildcards()
          * declaration_index     -- int index of the producing entry in `self.transitions`,
                                     or None for edges that are rendering artifacts rather than
                                     one fireable transition (see below). Globally well-ordered
                                     but NOT promised contiguous for a given state's out-edges
                                     (indices are global; hub-mode gaps are expected). Contract:
                                     for any state S, take S's out-edges where auto=True and
                                     declaration_index is not None, sort ascending by that
                                     index, and the resulting (trigger, dest) sequence equals
                                     `auto_edges_from(S)` — the order advance() attempts them.
                                     Do NOT treat NetworkX's own `out_edges()` iteration order
                                     as declaration order: a MultiDiGraph groups adjacency by
                                     destination, which can scramble interleaved destinations
                                     even when edges were added in declaration order. Multi-
                                     source transition dicts fan out to one edge per source that
                                     SHARE the same index (harmless: attempt order is only ever
                                     compared among one state's out-edges, where indices stay
                                     unique). Concrete wildcard-expanded edges inherit their
                                     appended position in `transitions` (after every explicit
                                     edge), matching runtime. `None` is used for (a) the
                                     deduplicated `"*" -> dest` hub edges under
                                     `wildcard_pseudo_state=True` and (b) abstract
                                     `wildcard_pending` rule edges — neither is a one-to-one
                                     fireable transition.

        A chain's `*[--|==]...-->dest` wildcard rule is ALSO represented directly (not just
        via the per-source edges expand_wildcards() concretizes): `expand_wildcards()` never
        clears `pending_wildcards`, so the abstract rule stays declared on the spec forever,
        same as `primary_chain`. Each entry in `pending_wildcards` becomes an edge from a
        synthetic `"*"` node (the DSL forbids `*` in a real state name, so this can never
        collide) to its destination, tagged `wildcard_pending=True`. This is UNAFFECTED by
        `wildcard_pseudo_state` — there is exactly one such edge per declared wildcard chain
        regardless, so it never contributes to fan-out clutter.

        `wildcard_pseudo_state` (default False) controls how the CONCRETE, expand_wildcards()
        -injected edges (`wildcard_expanded=True`) are drawn — these are the ones that fan out
        from every eligible source state to the wildcard's destination, and can make a diagram
        with many states look like every node connects to `cancelled`/`expired`/etc:
          * False (default) -- each injected edge is drawn directly `source -> dest`, exactly
            like any other transition. This is the raw, general-purpose topology: every real
            edge the compiled machine would actually fire is a real edge in the graph.
          * True -- each injected edge is instead routed through the same `"*"` sentinel node
            used for the pending rule: `source -> "*"` (carrying the edge's full trigger/
            guard/timeout/choke data, plus `wildcard_dest` naming the true destination so
            nothing is lost) and one deduplicated `"*" -> dest` edge per distinct
            (trigger, dest, guards_identity) combination. Visually this turns an
            N-source fan-out into a small hub-and-spoke cluster hanging off `"*"` — the
            "little islands" that keep wildcard escapes from tangling the main flow.

        `include_implied_caps` (default True) controls whether values the COMPILER filled in
        (nothing the author typed in the DSL) appear in the rendering:
          * True (default) -- the full effective picture: an unguarded auto edge's implicit
            `@FAIL<1` retry cap (from `apply_implicit_fail_cap()`) is included in
            `guards`, and an un-annotated trigger's `soft_timeout_secs` is filled in with
            the `DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS` fallback.
          * False -- only what the DSL chains actually declared: implicit `@FAIL<1` entries
            (fact items in `guards` with `"implicit": True`) are dropped, and an un-annotated
            trigger's `soft_timeout_secs` is `None` rather than the default. Useful for a
            diagram meant to show the author's own guard/timeout policy, undiluted by
            framework defaults.
        Either way, `soft_timeout_is_explicit` tells you which case you are in for a given
        edge, and non-implicit fact items in `guards` (explicit `@FAIL`/`@DWELL`, or any guard
        not from `apply_implicit_fail_cap()`) are never affected by this flag.

        Graph-level attributes (`graph.graph[...]`) carry everything that isn't naturally a
        node/edge property: `initial_state`, `states_order`, `triggers`, `pipeline`,
        `trigger_timeouts`, `trigger_chokes`, `timed_escape_states`, `wildcard_dests`, and
        `primary_chain` (the raw first chain string, handy as a diagram title).

        Some edge/graph attributes (frozensets) are not directly serializable by NetworkX's
        text-format writers (GraphML/GEXF); this method targets in-memory use (layout,
        algorithms, custom rendering), not round-tripping through those formats.

        Raises ImportError, with a pointed message, if `networkx` is not installed.
        """
        try:
            import networkx as nx
        except ImportError:
            raise_missing_dependency(feature="FsmChainSpec.to_networkx()", packages=["networkx"])

        def resolve_guards(raw_guards) -> list:
            out = []
            for g in raw_guards or []:
                if _is_fact_guard(g):
                    if not include_implied_caps and g.get("implicit"):
                        continue
                    out.append(dict(g))
                else:
                    out.append(g)
            return out

        def resolve_soft_timeout(trigger: str) -> tuple[Optional[float], bool]:
            if trigger in self.trigger_timeouts:
                return self.trigger_timeouts[trigger], True
            if include_implied_caps:
                return DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS, False
            return None, False

        g = nx.MultiDiGraph()
        g.graph.update({
            "initial_state": self.initial_state,
            "initial_states": set(self.initial_states),
            "terminal_states": set(self.terminal_states),
            "states_order": list(self.states),
            "triggers": list(self.triggers),
            "pipeline": list(self.pipeline),
            "trigger_timeouts": dict(self.trigger_timeouts),
            "trigger_chokes": {k: frozenset(v) for k, v in self.trigger_chokes.items()},
            "timed_escape_states": set(self.timed_escape_states),
            "wildcard_dests": set(self.wildcard_dests),
            "primary_chain": self.primary_chain,
        })

        for state in self.states:
            g.add_node(
                state,
                initial=state in self.initial_states,
                default_initial=state == self.initial_state,
                terminal=state in self.terminal_states,
                timed_escape=state in self.timed_escape_states,
            )

        needs_wildcard_node = bool(self.pending_wildcards) or (
            wildcard_pseudo_state and any(t.get("_wildcard") for t in self.transitions)
        )
        if needs_wildcard_node:
            g.add_node(_WILDCARD_SOURCE, wildcard_source=True)

        hub_dest_seen: set[tuple] = set()  # (trigger, dest, guards_identity) already hubbed

        for declaration_index, t in enumerate(self.transitions):
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            trigger = t["trigger"]
            dest = t["dest"]
            guards = resolve_guards(t.get("_guards"))
            chokes = frozenset(self.trigger_chokes.get(trigger, frozenset()))
            soft_timeout_secs, soft_timeout_is_explicit = resolve_soft_timeout(trigger)
            pure_timed_escape = self._is_pure_timed_escape(t)
            wildcard_expanded = bool(t.get("_wildcard", False))
            as_hub = wildcard_pseudo_state and wildcard_expanded

            for s in srcs:
                auto = (s, trigger) in self.auto_edges
                edge_attrs = dict(
                    trigger=trigger,
                    auto=auto,
                    manual=not auto,
                    guards=guards,
                    chokes=chokes,
                    soft_timeout_secs=soft_timeout_secs,
                    soft_timeout_is_explicit=soft_timeout_is_explicit,
                    pure_timed_escape=pure_timed_escape,
                    wildcard_expanded=wildcard_expanded,
                    declaration_index=declaration_index,
                )
                if not as_hub:
                    g.add_edge(s, dest, **edge_attrs)
                    continue

                g.add_edge(s, _WILDCARD_SOURCE, wildcard_dest=dest, **edge_attrs)
                hub_key = (trigger, dest, _guards_identity(guards))
                if hub_key not in hub_dest_seen:
                    hub_dest_seen.add(hub_key)
                    # Aggregated rendering edge: not one fireable transition.
                    g.add_edge(
                        _WILDCARD_SOURCE, dest,
                        **{**edge_attrs, "declaration_index": None},
                    )

        for w in self.pending_wildcards:
            trigger = w["trigger"]
            soft_timeout_secs, soft_timeout_is_explicit = resolve_soft_timeout(trigger)
            g.add_edge(
                _WILDCARD_SOURCE, w["dest"],
                trigger=trigger,
                auto=w["auto"],
                manual=not w["auto"],
                guards=resolve_guards(w.get("_guards")),
                chokes=frozenset(self.trigger_chokes.get(trigger, frozenset())),
                soft_timeout_secs=soft_timeout_secs,
                soft_timeout_is_explicit=soft_timeout_is_explicit,
                wildcard_pending=True,
                declaration_index=None,
            )

        return g

    # ---- carrier-object compatibility (the binding check) ----
    # The spec is the ONLY thing that knows what callables its graph implies, so it is the
    # natural place to ask "is this object a suitable carrier for me?" — keeping the check
    # off FolderBackedCase. PURE except validate_object_compatibility(), which inspects the
    # object passed in but neither stores it nor mutates the spec.

    def _referenced_callbacks(self):
        """Yield (slot, item, trigger) for every explicitly-referenced transition callback in
        the compiled spec — method-guard strings from `_guards` (slot `"guards"`) plus any
        hand-built `unless`/`before`/`after`/`prepare` from a compile_fsm() override. `item`
        is a NAME (str, to be resolved on the carrier) or an already-resolved callable. For a
        DSL method guard the NAME is already the conventionized `guard_<token>` the parser
        stored (see _GUARD_METHOD_PREFIX), so existence/async checks resolve it directly.
        Factual-guard dicts in `_guards` are excluded on purpose (compiled by the base class).
        """
        for t in self.transitions:
            trigger = t.get("trigger")
            for g in t.get("_guards") or []:
                if _is_method_guard(g):
                    yield "guards", g, trigger
            for slot in _CARRIER_CALLBACK_KEYS:
                val = t.get(slot)
                if val is None:
                    continue
                items = val if isinstance(val, (list, tuple)) else [val]
                for item in items:
                    yield slot, item, trigger

    def implied_carrier_attributes(
        self, *, hook_patterns=(_PERFORM_HOOK_PATTERN,)
    ) -> tuple[set[str], set[str]]:
        """The carrier-method names this FSM implies, as (required, optional):
          * required — every method-guard / explicitly-named callback the spec references;
            these MUST exist on the carrier (a missing one is a typo, not a choice). DSL
            method guards appear here in their `guard_<token>` form (e.g. a `[funded]`
            guard implies a required `guard_funded`). Plus action hooks for triggers
            reachable from any auto edge (`--`), because unattended paths must be explicit.
          * optional — per-trigger ACTION methods derived from `hook_patterns` (default
            `perform_<trigger>`) for manual-only triggers (`==`); wired when present.
        PURE: reads the spec, binds to nothing. validate_object_compatibility() is the
        companion that checks a concrete object against these."""
        required = {
            item for slot, item, _ in self._referenced_callbacks() if isinstance(item, str)
        }
        auto_triggers = {trigger for _, trigger in self.auto_edges}
        required.update(
            pat.format(trigger=trigger)
            for trigger in auto_triggers
            for pat in hook_patterns
        )
        optional = {
            pat.format(trigger=trigger)
            for trigger in self.triggers
            if trigger not in auto_triggers
            for pat in hook_patterns
        }
        return required, optional

    def validate_object_compatibility(
        self,
        obj,
        *,
        force_async: bool = True,
        require_tctx: bool = True,
        hook_patterns=(_PERFORM_HOOK_PATTERN,),
        orphan_detection: str = "error",
        sealed_names: frozenset[str] = frozenset(),
        sealed_owner: Optional[type] = None,
    ) -> None:
        """Confirm `obj` is a suitable CARRIER for this compiled FSM, raising FsmBindingError
        (listing ALL gaps at once) if not. Designed to run ONCE per concrete carrier class —
        FolderBackedCase calls it at first instantiation, the earliest point a concrete class
        is guaranteed fully assembled (Python forbids instantiating a class with unimplemented
        abstractmethods, so leaf-supplied guards are present by then).

        Checks:
          * EXISTENCE — every method guard / explicitly-named callback the spec references
            must be defined on `obj`. A DSL method guard `trigger [<token>]` resolves to the
            carrier method `guard_<token>` (see _GUARD_METHOD_PREFIX), so that is the name
            that must exist. Plus `perform_<trigger>` for any trigger reachable from an auto
            edge (`--`) must exist, even if it is a no-op.
          * ASYNC (force_async, default True) — every referenced callable that IS present —
            required names, required auto-edge hooks, and any optional action method that
            happens to exist — must be a coroutine function. The case family is driven through
            async (advance(), the generated triggers); a stray `def` instead of `async def`
            would silently block the event loop, so it is rejected here. Set force_async=False
            for the rare carrier that deliberately mixes in synchronous callables.
          * ARITY (require_tctx, default True) — every RECOGNIZED hook method (a
            `perform_`/`before_`/`after_`/`on_enter_`/`on_exit_`/`guard_` whose suffix maps
            to a known state/trigger/guard) must accept the trigger context `tctx` as its
            one argument after `self`. The machine runs with send_event=True, so every hook
            is dispatched with a single `tctx`; a bare `(self)` hook would raise TypeError
            the instant its edge fired, so it is rejected here at build time instead. A
            `(self, *args)` hook passes. Set require_tctx=False to skip this scan.

        `perform_<trigger>` stays OPTIONAL for manual-only triggers (`==`) — but if present
        it is held to the async rule like everything else.

        orphan_detection controls scanning for hook-looking typo traps:
          * "off"  — skip orphan scan
          * "warn" — emit a warning for hook-like methods whose suffix does not match any
                     known state/trigger/guard
          * "error" — fail binding with FsmBindingError including orphan details (DEFAULT).
                     A method that LOOKS like a hook (one of the scanned prefixes) but maps
                     to nothing is overwhelmingly a typo, and the safety of catching it at
                     construction outweighs the rare need to rename a coincidental helper
                     (pass orphan_detection="off" for that deliberate case).
        Scanned prefixes: `on_enter_`, `on_exit_`, `guard_`, `perform_`, `before_`,
        `after_`. A `guard_<token>` method whose token is not referenced by any chain guard
        is flagged the same way a misspelled `on_enter_<state>` is.

        sealed_names / sealed_owner protect the OWNER class's own namespace from accidental
        shadowing: `sealed_names` is the set of member names `sealed_owner` reserves for its
        core machinery (e.g. FolderBackedCase's `case_state`, `case_advance`, ...). If any
        class BETWEEN `type(obj)` and `sealed_owner` (i.e. a subclass) redefines one of those
        names in its body, binding fails — a subclass that wrote `def case_state(self)` has
        silently broken the base. This catches only class-body definitions (methods,
        properties, class attributes); a purely runtime `self.case_state = ...` reassignment
        is out of scope. A no-op when either argument is empty/None.

        TRIGGER COLLISIONS — each DSL trigger name must be free on `obj`. The transitions
        library skip-binds (with a warning) when a same-named attribute already exists, so a
        hand-written `def ingest(...)` would silently steal the trigger. That is almost
        always a mistaken `perform_<trigger>`; we reject it here instead."""
        if orphan_detection not in {"off", "warn", "error"}:
            raise ValueError("orphan_detection must be one of {'off', 'warn', 'error'}")
        missing: dict[str, tuple[str, str, str]] = {}
        sync: dict[str, tuple[str, str, str]] = {}
        auto_triggers = {trigger for _, trigger in self.auto_edges}

        for slot, item, trigger in self._referenced_callbacks():
            if isinstance(item, str):
                fn = getattr(obj, item, None)
                if fn is None:
                    missing.setdefault(item, (item, slot, trigger))
                    continue
                name = item
            elif callable(item):
                fn, name = item, getattr(item, "__name__", repr(item))
            else:
                continue
            if force_async and not _is_async_callable(fn):
                sync.setdefault(name, (name, slot, trigger))

        for trigger in auto_triggers:
            for pat in hook_patterns:
                name = pat.format(trigger=trigger)
                fn = getattr(obj, name, None)
                if fn is None:
                    missing.setdefault(name, (name, "auto_hook", trigger))
                    continue
                if force_async and not _is_async_callable(fn):
                    sync.setdefault(name, (name, "auto_hook", trigger))

        if force_async:
            for trigger in self.triggers:
                if trigger in auto_triggers:
                    continue
                for pat in hook_patterns:
                    name = pat.format(trigger=trigger)
                    fn = getattr(obj, name, None)
                    if fn is not None and not _is_async_callable(fn):
                        sync.setdefault(name, (name, "hook", trigger))
        orphaned = (
            self._find_orphan_hook_methods(obj) if orphan_detection != "off" else []
        )
        bad_arity = self._find_bad_arity_hook_methods(obj) if require_tctx else []
        sealed = self._find_sealed_overrides(obj, sealed_names, sealed_owner)
        # Same predicate transitions uses in Machine._checked_assignment: any existing
        # attribute blocks bind of the convenience trigger method.
        trigger_collisions = [
            trigger for trigger in self.triggers
            if getattr(obj, trigger, None) is not None
        ]

        if (
            missing or sync or bad_arity or sealed or trigger_collisions
            or (orphan_detection == "error" and orphaned)
        ):
            raise FsmBindingError(
                type(obj).__name__,
                missing=list(missing.values()),
                sync=list(sync.values()),
                orphaned=orphaned if orphan_detection == "error" else [],
                bad_arity=bad_arity,
                sealed=sealed,
                trigger_collisions=trigger_collisions,
            )
        if orphan_detection == "warn" and orphaned:
            warnings.warn(
                self._format_orphan_hook_warning(type(obj).__name__, orphaned),
                UserWarning,
                stacklevel=2,
            )

    def _declared_guard_tokens(self) -> set[str]:
        """The bare guard tokens this FSM references — the part AFTER the `guard_` prefix of
        every `guard_<token>` string found in any transition's `_guards` (or hand-built
        `unless`). These are exactly the `guard_<token>` carrier methods the parser emits for
        a `trigger [<token>]` DSL guard, plus any `guard_`-prefixed name a hand-built
        compile_fsm() override put there. Used by orphan detection to tell a real guard method
        from a `guard_`-prefixed typo. Fact dicts, callables, and non-`guard_` strings are
        ignored."""
        tokens: set[str] = set()
        for t in self.transitions:
            for item in _method_guards(t.get("_guards")):
                if item.startswith(_GUARD_METHOD_PREFIX):
                    tokens.add(item[len(_GUARD_METHOD_PREFIX):])
            val = t.get("unless")
            if val is None:
                continue
            items = val if isinstance(val, (list, tuple)) else [val]
            for item in items:
                if isinstance(item, str) and item.startswith(_GUARD_METHOD_PREFIX):
                    tokens.add(item[len(_GUARD_METHOD_PREFIX):])
        return tokens

    def _orphan_expected_names_by_kind(self) -> dict[str, set[str]]:
        """The set of valid suffixes for each hook KIND in _HOOK_METHOD_PREFIXES: state
        names, declared guard tokens, and trigger names. The single place that maps a hook
        kind to its universe of legal names, shared by the orphan scan and its formatters."""
        return {
            "state": set(self.states),
            "guard": self._declared_guard_tokens(),
            "trigger": set(self.triggers),
        }

    def _find_orphan_hook_methods(self, obj) -> list[tuple[str, str, str]]:
        """Return hook-like methods whose suffix points at no known state/trigger/guard.
        Tuples are (method_name, kind, suffix) where kind is a value of _HOOK_METHOD_PREFIXES
        ('state', 'guard', or 'trigger'). Driven entirely by that registry, so a new hook
        prefix added there is scanned here with no further change."""
        expected_by_kind = self._orphan_expected_names_by_kind()
        orphans: list[tuple[str, str, str]] = []
        for name in dir(type(obj)):
            fn = getattr(obj, name, None)
            if not callable(fn):
                continue
            for prefix, kind in _HOOK_METHOD_PREFIXES.items():
                if not name.startswith(prefix):
                    continue
                suffix = name[len(prefix):]
                if suffix and suffix not in expected_by_kind[kind]:
                    orphans.append((name, kind, suffix))
                break
        return orphans

    def _find_bad_arity_hook_methods(self, obj) -> list[tuple[str, str, str]]:
        """Return RECOGNIZED hook methods (suffix maps to a known state/trigger/guard) that
        cannot accept the trigger context `tctx`. Tuples are (method_name, kind, suffix),
        same shape as _find_orphan_hook_methods. Every hook is dispatched with a single
        `tctx` argument (the machine runs with send_event=True), so a hook declared `(self)`
        would raise TypeError the moment its edge fires; this pulls that failure forward to
        construction. Inverts the orphan suffix test (known, not unknown) and gates on arity
        instead of name; explicit hand-built callables in compile_fsm() overrides are out of
        scope (only convention-named carrier methods are scanned)."""
        expected_by_kind = self._orphan_expected_names_by_kind()
        bad: list[tuple[str, str, str]] = []
        for name in dir(type(obj)):
            fn = getattr(obj, name, None)
            if not callable(fn):
                continue
            for prefix, kind in _HOOK_METHOD_PREFIXES.items():
                if not name.startswith(prefix):
                    continue
                suffix = name[len(prefix):]
                if suffix and suffix in expected_by_kind[kind] and not _accepts_tctx(fn):
                    bad.append((name, kind, suffix))
                break
        return bad

    @staticmethod
    def _find_sealed_overrides(obj, sealed_names, sealed_owner) -> list[tuple[str, str]]:
        """Return (name, defining_class_name) for every sealed member that a SUBCLASS of
        `sealed_owner` redefines in its class body. Walks `type(obj).__mro__` and inspects
        each class that is a strict subclass of `sealed_owner` (the leaf and any intermediate
        bases, but NOT `sealed_owner` itself, which legitimately defines these names). Only
        class-body definitions are visible in `__dict__`, so a runtime `self.<name> = ...`
        reassignment is deliberately out of scope. A no-op when sealing is not requested."""
        if not sealed_names or sealed_owner is None:
            return []
        found: list[tuple[str, str]] = []
        for cls in type(obj).__mro__:
            if cls is sealed_owner or not issubclass(cls, sealed_owner):
                continue
            for name in sealed_names & set(vars(cls)):
                found.append((name, cls.__name__))
        return found

    def _format_orphan_hook_warning(
        self, carrier_name: str, orphaned: list[tuple[str, str, str]]
    ) -> str:
        lines = [
            f"{carrier_name!r} has hook-like methods that do not map to known FSM "
            "states/triggers/guards:"
        ]
        expected_by_kind = self._orphan_expected_names_by_kind()
        for name, kind, suffix in orphaned:
            expected = sorted(expected_by_kind.get(kind, set()))
            lines.append(
                f"  - {name!r}: suffix {suffix!r} is not a known {kind}; expected one of {expected}"
            )
        lines.append("Rename/fix these methods, or disable with orphan_detection='off'.")
        return "\n".join(lines)


class StateChainParser:
    """Stateless renderer from chain declarations to an FsmChainSpec. Use the classmethod
    `parse`; there is nothing to instantiate. `parse` raises on SYNTACTIC/structural
    problems; whole-graph semantic rules live in FsmChainSpec.validate()."""

    @classmethod
    def normalize_chain_lines(cls, chains: list[str] | str | None) -> list[str]:
        """Flatten a chain declaration — a list of chain strings or a single (typically
        triple-quoted, multiline) string — into the substantive chain lines the parser
        consumes: split on newlines, strip `%%` comments (whole-line or trailing), drop
        blank lines and Mermaid boilerplate lines (`stateDiagram-v2`, `flowchart TD`,
        `direction LR`, ...). Empty/None -> []. This is also the canonical form the
        case record persists. Raises FsmChainParseError on a non-string entry or a
        '#'-style comment line."""
        if not chains:
            return []
        entries = [chains] if isinstance(chains, str) else list(chains)
        lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, str):
                raise FsmChainParseError(
                    f"chain entries must be strings, got {type(entry).__name__}"
                )
            for raw_line in entry.splitlines():
                line = raw_line.split("%%", 1)[0].strip()
                if not line:
                    continue
                if line.startswith("#"):
                    raise FsmChainParseError(
                        "comments in fsm_state_chains use '%%' (as in Mermaid)",
                        chain=raw_line.strip(),
                    )
                if _MERMAID_BOILERPLATE_RE.match(line):
                    continue
                lines.append(line)
        return lines

    @classmethod
    def parse(cls, chains: list[str] | str | None) -> FsmChainSpec:
        """Render a chain declaration into an FsmChainSpec. Accepts a list of chain
        strings or a single multiline string; see `normalize_chain_lines` for the
        comment/boilerplate handling. Empty/None -> empty spec. Raises
        FsmChainParseError (with the offending chain + index) on malformed input. Does
        NOT run the whole-graph checks (call FsmChainSpec.validate()) and does NOT
        expand `* -- ... --> dest` wildcard chains (call
        FsmChainSpec.expand_wildcards(), AFTER validate)."""
        spec = FsmChainSpec.empty()
        lines = cls.normalize_chain_lines(chains)
        if not lines:
            return spec

        # (trigger, source, guards_identity) -> dest, to catch genuinely
        # nondeterministic duplicates (identical guard, different dest) while allowing
        # guarded branching.
        seen_edges: dict[tuple, str] = {}

        for idx, line in enumerate(lines):
            cls._parse_chain(line, idx, spec, seen_edges, primary=(idx == 0))
        return spec

    # ---- per-chain ----

    @classmethod
    def _parse_chain(
        cls,
        chain: str,
        idx: int,
        spec: FsmChainSpec,
        seen_edges: dict[tuple, str],
        *,
        primary: bool,
    ) -> None:
        if primary:
            spec.primary_chain = chain

        # '#' has no legal use inside a chain (comments are '%%'; guards are
        # bracketed) — catch it here so it always gets the guard-bracket hint
        # rather than a shape-dependent tokenizer message.
        if "#" in chain:
            raise FsmChainParseError(
                "'#' has no meaning in a chain; guards are written in brackets after "
                "the trigger ('trigger [guard1, @DWELL>30m]') and comments use '%%'",
                chain=chain, index=idx,
            )

        # A colon anywhere marks the Mermaid-stateDiagram-style single-edge form; the
        # colon has no other meaning in the grammar.
        if ":" in chain:
            cls._parse_colon_form(chain, idx, spec, seen_edges)
            return

        tokens, connectors = cls._tokenize_chain(chain, idx)

        # Boundary hops first: a leading `[*] --> S` marks S initial, a trailing
        # `S --> [*]` marks S terminal. Stripping them leaves pure states/edges.
        initial_first = terminal_last = False
        if tokens[0] == _BOUNDARY:
            if len(tokens) < 2 or tokens[1] == _BOUNDARY:
                raise FsmChainParseError(
                    "a '[*] -->' boundary hop must lead to a state, e.g. '[*] --> new'",
                    chain=chain, index=idx,
                )
            cls._require_bare_boundary_hop(connectors[0], chain, idx)
            initial_first = True
            tokens, connectors = tokens[1:], connectors[1:]
        if len(tokens) > 1 and tokens[-1] == _BOUNDARY:
            cls._require_bare_boundary_hop(connectors[-1], chain, idx)
            terminal_last = True
            tokens, connectors = tokens[:-1], connectors[:-1]
        if _BOUNDARY in tokens:
            raise FsmChainParseError(
                "'[*]' (the start/end boundary) may only appear at a chain's ends: "
                "'[*] --> first' and/or 'last --> [*]'",
                chain=chain, index=idx,
            )
        for c in connectors:
            if c["label"] is None:
                raise FsmChainParseError(
                    "connector without a trigger label; only '[*]' boundary hops may "
                    "omit it — write '-- trigger -->' (auto) or '== trigger ==>' (manual)",
                    chain=chain, index=idx,
                )

        # A chain whose first state slot is a bare `*` is a "from any source" wildcard:
        # its concrete edges are deduced later by FsmChainSpec.expand_wildcards().
        if tokens[0] == _WILDCARD_SOURCE:
            if initial_first:
                raise FsmChainParseError(
                    "the any-state wildcard '*' cannot be marked initial ('[*] --> *')",
                    chain=chain, index=idx,
                )
            cls._parse_wildcard_chain(tokens, connectors, terminal_last, chain, idx, spec)
            return

        names = [cls._parse_state_token(t, chain, idx) for t in tokens]
        for pos, name in enumerate(names):
            cls._add_state(
                spec, name,
                initial=(initial_first and pos == 0),
                terminal=(terminal_last and pos == len(names) - 1),
            )
        for i, c in enumerate(connectors):
            guards, trigger, soft_secs = cls._parse_label(
                c["label"], chain, idx,
            )
            cls._add_transition(
                spec, trigger, names[i], names[i + 1],
                guards=guards, auto=not c["manual"],
                soft_secs=soft_secs, raw=chain, idx=idx, seen_edges=seen_edges,
            )

    @classmethod
    def _tokenize_chain(cls, chain: str, idx: int) -> tuple[list[str], list[dict]]:
        """Split a chain into its state tokens and connectors, alternating. Each
        connector is a dict {"manual": bool | None, "label": str | None}; a bare `-->`
        boundary hop has both None. Mismatched arrow pairs (`== ... -->`) are rejected
        here with a pointed message."""
        tokens: list[str] = []
        connectors: list[dict] = []
        pos = 0
        for m in _CONNECTOR_RE.finditer(chain):
            tokens.append(chain[pos:m.start()].strip())
            if m.group("bare"):
                connectors.append({"manual": None, "label": None})
            else:
                opener, closer = m.group("open"), m.group("close")
                if (opener == "--") != (closer == "-->"):
                    raise FsmChainParseError(
                        f"mismatched connector arrows in {m.group(0).strip()!r}; auto "
                        "edges are written '-- trigger -->' and manual edges "
                        "'== trigger ==>' (matched pairs)",
                        chain=chain, index=idx,
                    )
                connectors.append({"manual": opener == "==", "label": m.group("label")})
            pos = m.end()
        tokens.append(chain[pos:].strip())
        return tokens, connectors

    @staticmethod
    def _require_bare_boundary_hop(connector: dict, chain: str, idx: int) -> None:
        if connector["label"] is not None:
            raise FsmChainParseError(
                "a '[*]' boundary hop takes no trigger label — it is an initial/terminal "
                "marker, not a transition; write a bare '[*] --> state' or "
                "'state --> [*]'",
                chain=chain, index=idx,
            )

    @classmethod
    def _parse_colon_form(
        cls, chain: str, idx: int, spec: FsmChainSpec, seen_edges: dict[tuple, str],
    ) -> None:
        """Parse a Mermaid-stateDiagram-style single edge: `SRC --> DEST : label` (auto)
        or `SRC ==> DEST : label` / `SRC --> DEST : == label` (manual — the leading `==`
        label marker keeps the line valid Mermaid stateDiagram-v2). One edge per line;
        multi-hop chains use inline `-- label -->` connectors instead."""
        m = _COLON_FORM_RE.match(chain)
        if m is None:
            raise FsmChainParseError(
                "could not parse colon-labeled edge; write 'SRC --> DEST : trigger' "
                "(auto) or 'SRC ==> DEST : trigger' / 'SRC --> DEST : == trigger' "
                "(manual) — ONE edge per line (multi-hop chains use inline "
                "'-- trigger -->' labels and no colon)",
                chain=chain, index=idx,
            )
        src_tok, dest_tok = m.group("src"), m.group("dest")
        label = m.group("label").strip()
        # Optional Mermaid quotes around the label (generated diagrams always
        # quote so characters like `[` / `>` in guard lists render). Strip one
        # layer before interpreting the `==` manual marker.
        if len(label) >= 2 and label[0] == '"' and label[-1] == '"':
            label = label[1:-1]
        manual = m.group("arrow") == "==>"
        if label.startswith("=="):
            manual = True
            label = label[2:].strip()
        if not label:
            raise FsmChainParseError(
                "colon-labeled edge is missing its trigger (e.g. 'a --> b : finish')",
                chain=chain, index=idx,
            )
        if _BOUNDARY in (src_tok, dest_tok):
            raise FsmChainParseError(
                "a '[*]' boundary hop takes no label — write a bare '[*] --> state' or "
                "'state --> [*]' (its own line, or inline at a chain's ends)",
                chain=chain, index=idx,
            )
        guards, trigger, soft_secs = cls._parse_label(label, chain, idx)

        if src_tok == _WILDCARD_SOURCE:
            dest = cls._parse_state_token(dest_tok, chain, idx)
            cls._add_state(spec, dest, initial=False, terminal=False)
            cls._record_trigger_timeout(spec, trigger, soft_secs, chain, idx)
            spec.pending_wildcards.append({
                "trigger": trigger, "dest": dest, "_guards": guards,
                "auto": not manual,
            })
            spec.wildcard_dests.add(dest)
            return

        src = cls._parse_state_token(src_tok, chain, idx)
        dest = cls._parse_state_token(dest_tok, chain, idx)
        cls._add_state(spec, src, initial=False, terminal=False)
        cls._add_state(spec, dest, initial=False, terminal=False)
        cls._add_transition(
            spec, trigger, src, dest,
            guards=guards, auto=not manual,
            soft_secs=soft_secs, raw=chain, idx=idx, seen_edges=seen_edges,
        )

    @classmethod
    def _parse_wildcard_chain(
        cls,
        tokens: list[str],
        connectors: list[dict],
        terminal_last: bool,
        chain: str,
        idx: int,
        spec: FsmChainSpec,
    ) -> None:
        """Register a single `* -- trigger --> DEST` / `* == trigger ==> DEST` wildcard
        edge. Must contain exactly ONE transition to a single destination (a trailing
        `--> [*]` terminal hop is allowed); the concrete per-source edges are deduced
        by FsmChainSpec.expand_wildcards() once every state is known."""
        if len(tokens) != 2 or len(connectors) != 1:
            raise FsmChainParseError(
                "a wildcard chain must contain exactly ONE transition to a single "
                "destination, e.g. '* == cancel ==> cancelled'",
                chain=chain, index=idx,
            )
        dest = cls._parse_state_token(tokens[1], chain, idx)
        cls._add_state(spec, dest, initial=False, terminal=terminal_last)
        c = connectors[0]
        guards, trigger, soft_secs = cls._parse_label(
            c["label"], chain, idx,
        )
        cls._record_trigger_timeout(spec, trigger, soft_secs, chain, idx)
        spec.pending_wildcards.append({
            "trigger": trigger, "dest": dest, "_guards": guards,
            "auto": not c["manual"],
        })
        spec.wildcard_dests.add(dest)

    @classmethod
    def _parse_state_token(cls, token: str, raw: str, idx: int) -> str:
        """Validate a state token and return its name. States are bare
        `[A-Za-z0-9_]` identifiers; `[*]` (boundary) and `*` (wildcard source) are
        recognized by the callers before this runs, so their appearance here is a
        placement error and gets a pointed message."""
        tok = token.strip()
        if not tok:
            raise FsmChainParseError(
                "empty state name (check for stray or malformed arrows)",
                chain=raw, index=idx,
            )
        if tok == _WILDCARD_SOURCE:
            raise FsmChainParseError(
                "the any-state wildcard '*' may only appear as a chain's SOURCE, "
                "e.g. '* == cancel ==> cancelled'",
                chain=raw, index=idx,
            )
        if "^" in tok:
            raise FsmChainParseError(
                f"unknown marker '^' in {tok!r}; an initial state is declared with a "
                "'[*] --> state' hop and a terminal state with 'state --> [*]'",
                chain=raw, index=idx,
            )
        if any(ch in tok for ch in "-=>"):
            raise FsmChainParseError(
                f"could not parse {tok!r}; transitions are written "
                "'A -- trigger --> B' (auto) or 'A == trigger ==> B' (manual), and "
                "'[*]' boundary hops use a bare '-->'",
                chain=raw, index=idx,
            )
        if _NAME_RE.fullmatch(tok) is None:
            raise FsmChainParseError(
                f"invalid state name {tok!r}; names use letters/digits/underscores only",
                chain=raw, index=idx,
            )
        return tok

    @classmethod
    def _parse_label(
        cls, label: str, raw: str, idx: int
    ) -> tuple[list, str, Optional[float]]:
        """Split an edge label into (guards, trigger, soft_secs).
        Grammar: `trigger[~<dur>] [guard, ...]` — the trigger (optionally carrying a
        `~<dur>` SOFT-timeout suffix, split off here; see _parse_trigger_timeout)
        followed by an optional bracketed, comma-separated guard list. Guards are
        evaluated LEFT-TO-RIGHT in declaration order; method and `@FACT` items may
        intersperse in one list. Each bare guard token `tok` is mapped to the
        carrier-method name `guard_<tok>` (see _GUARD_METHOD_PREFIX), so
        `finish [funded]` yields guards=["guard_funded"] — the carrier must define
        `async def guard_funded`. `@`-prefixed items are factual guards (see
        _parse_fact_guard); at most one per FACT NAME. `soft_secs` is the annotated
        soft-timeout in seconds, or None when un-annotated."""
        text = label.strip()
        if "#" in text:
            raise FsmChainParseError(
                f"'#' in edge label {text!r}; guards are written in brackets after the "
                "trigger: 'trigger [guard1, @DWELL>30m]'",
                chain=raw, index=idx,
            )
        m = _LABEL_PARSE_RE.match(text)
        if m is None:
            raise FsmChainParseError(
                f"malformed edge label {text!r}; write 'trigger', 'trigger~<dur>', or "
                "'trigger [guard, ...]' (guards comma-separated in one bracket group "
                "after the trigger)",
                chain=raw, index=idx,
            )
        trigger = m.group("trigger")
        soft_secs: Optional[float] = None
        if "~" in trigger:                       # split the `~<dur>` soft-timeout off the trigger
            trigger, soft_secs = cls._parse_trigger_timeout(trigger, raw, idx)

        guards: list = []
        seen_facts: set[str] = set()
        guards_text = m.group("guards")
        if guards_text is not None:
            if not guards_text.strip():
                raise FsmChainParseError(
                    f"empty guard list in {text!r}; drop the brackets or name at least "
                    "one guard",
                    chain=raw, index=idx,
                )
            for tok in guards_text.split(","):
                tok = tok.strip()
                if not tok:
                    raise FsmChainParseError(
                        f"empty guard item in {text!r} (check for a stray comma)",
                        chain=raw, index=idx,
                    )
                if tok.startswith("@"):
                    fg = cls._parse_fact_guard(tok, raw, idx)
                    if fg["name"] in seen_facts:
                        raise FsmChainParseError(
                            f"at most one @{fg['name']} guard is allowed per edge",
                            chain=raw, index=idx,
                        )
                    seen_facts.add(fg["name"])
                    guards.append(fg)
                elif _NAME_RE.fullmatch(tok) and not tok[0].isdigit():
                    guards.append(f"{_GUARD_METHOD_PREFIX}{tok}")
                else:
                    raise FsmChainParseError(
                        f"invalid guard {tok!r} in {text!r}; a guard is a bare "
                        "method-guard name (resolved as guard_<name>) or a factual "
                        "guard like '@FAIL<3' / '@DWELL>=30m'",
                        chain=raw, index=idx,
                    )
        return guards, trigger, soft_secs

    @staticmethod
    def _parse_trigger_timeout(token: str, raw: str, idx: int) -> tuple[str, float]:
        """Split a `trigger~<dur>` token into (trigger_name, soft_secs). The duration uses
        the same units as @DWELL (s|m|h|d, float allowed) and yields SECONDS. A unit is
        REQUIRED — a unit-less duration is the most likely typo, so it gets a pointed message
        rather than silently meaning something else."""
        name, _, dur = token.partition("~")
        name, dur = name.strip(), dur.strip()
        m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])$", dur)
        if m is None:
            raise FsmChainParseError(
                f"invalid trigger soft-timeout in {token.strip()!r}; write 'trigger~<dur>' "
                "with a unit s|m|h|d (e.g. 'assign~20s', 'fetch~1.5m')",
                chain=raw, index=idx,
            )
        secs = float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        if secs <= 0:
            raise FsmChainParseError(
                f"trigger soft-timeout must be positive (got {token.strip()!r})",
                chain=raw, index=idx,
            )
        return name, secs

    @staticmethod
    def _record_trigger_timeout(
        spec: FsmChainSpec, trigger: str, soft_secs: Optional[float], raw: str, idx: int
    ) -> None:
        """Record a trigger's `~<dur>` soft-timeout on the spec, keyed by TRIGGER (the budget
        is a property of `perform_<trigger>`, not the edge). Annotating + not-annotating the
        same trigger is fine (the annotation wins); annotating it with two DIFFERENT durations
        is a contradiction and is rejected."""
        if soft_secs is None:
            return
        existing = spec.trigger_timeouts.get(trigger)
        if existing is not None and existing != soft_secs:
            raise FsmChainParseError(
                f"trigger {trigger!r} is annotated with conflicting soft-timeouts "
                f"({existing:g}s vs {soft_secs:g}s); a trigger's timeout is a property of its "
                "work — annotate it once, or identically on every edge",
                chain=raw, index=idx,
            )
        spec.trigger_timeouts[trigger] = soft_secs

    @staticmethod
    def _parse_fact_guard(token: str, raw: str, idx: int) -> dict:
        """Parse a `@NAME<op>NUMBER[unit]` factual guard into {"name","op","operand"}.
        TIME facts (@DWELL) require a duration unit (s|m|h|d) and yield operand in SECONDS;
        COUNT facts (@FAIL) take a bare integer (no unit). Only `< <= > >=` are accepted —
        equality is rejected with a pointed message so the intent is clear."""
        m = _FACT_GUARD_RE.match(token)
        if m is None:
            if "==" in token or "!=" in token:
                raise FsmChainParseError(
                    f"equality comparators are not supported in factual guards ({token!r}); "
                    "use one of < <= > >= (we cannot promise to evaluate at an exact "
                    "instant/count, so '==' would be misleading)",
                    chain=raw, index=idx,
                )
            raise FsmChainParseError(
                f"invalid factual guard {token!r}; use '@NAME<op>N' with op in < <= > >= "
                "(e.g. '@FAIL<3', '@DWELL>=90s', '@DWELL>1.5h')",
                chain=raw, index=idx,
            )
        name, op, number, unit = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        if name in _TIME_FACTS:
            if not unit:
                raise FsmChainParseError(
                    f"time fact @{name} needs a duration unit s|m|h|d (e.g. '@{name}>30m')",
                    chain=raw, index=idx,
                )
            operand: float = float(number) * _UNIT_SECONDS[unit]
        elif name in _COUNT_FACTS:
            if unit:
                raise FsmChainParseError(
                    f"count fact @{name} is a bare integer and takes NO unit (e.g. "
                    f"'@{name}<3'); got unit {unit!r}",
                    chain=raw, index=idx,
                )
            if "." in number:
                raise FsmChainParseError(
                    f"count fact @{name} must be a whole number (e.g. '@{name}<3'); "
                    f"got {number!r}",
                    chain=raw, index=idx,
                )
            operand = int(number)
        else:
            raise FsmChainParseError(
                f"unknown factual guard @{name}; supported: "
                f"@DWELL (time, needs unit) and @FAIL (count, bare integer)",
                chain=raw, index=idx,
            )
        return {"name": name, "op": op, "operand": operand}

    # ---- accumulation helpers ----

    @staticmethod
    def _add_state(spec: FsmChainSpec, name: str, *, initial: bool, terminal: bool) -> None:
        if name not in spec.states:
            spec.states.append(name)
        if terminal:
            spec.terminal_states.add(name)
        if initial:
            spec.initial_states.add(name)
            if spec.initial_state is None:        # first initial-marked state wins as default
                spec.initial_state = name

    @classmethod
    def _add_transition(
        cls,
        spec: FsmChainSpec,
        trigger: str,
        source: str,
        dest: str,
        *,
        guards: list,
        auto: bool,
        soft_secs: Optional[float],
        raw: str,
        idx: int,
        seen_edges: dict[tuple, str],
    ) -> None:
        # A trigger's soft-timeout is recorded regardless of edge-dedup below: it is keyed by
        # trigger (a property of its work), and the recorder rejects conflicting annotations.
        cls._record_trigger_timeout(spec, trigger, soft_secs, raw, idx)
        # Edge identity includes the ordered guards (methods + facts): identical
        # trigger+source+guards but different dest is genuinely ambiguous; differing
        # guards is legitimate branching.
        key = (trigger, source, _guards_identity(guards))
        if key in seen_edges:
            if seen_edges[key] != dest:
                raise FsmChainParseError(
                    f"trigger {trigger!r} from state {source!r} with the same guard(s) is "
                    f"nondeterministic: it goes to both {seen_edges[key]!r} and {dest!r}",
                    chain=raw, index=idx,
                )
            # exact duplicate edge -> harmless, dedupe silently (but still honour `auto`).
        else:
            seen_edges[key] = dest
            td: dict = {"trigger": trigger, "source": source, "dest": dest}
            if guards:
                td["_guards"] = _copy_guards(guards)
            spec.transitions.append(td)
            if trigger not in spec.triggers:
                spec.triggers.append(trigger)
        if auto:
            spec.auto_edges.add((source, trigger))
            if trigger not in spec.pipeline:
                spec.pipeline.append(trigger)


# ---------------------------------------------------------------------------
# Lint — advisory design-smell warnings (CLI layer, not the library contract)
# ---------------------------------------------------------------------------

def lint_spec(spec: FsmChainSpec) -> list[str]:
    """Advisory warnings for a parsed spec — things that are LEGAL but usually mean
    the declaration isn't finished (e.g. a freshly pasted Mermaid sketch). Returns
    human-readable warning strings; empty when nothing looks off.

    Deliberately NOT part of parse()/validate(): a program compiling a class
    declaration should not receive opinions; a human at a terminal should. Run it
    AFTER the compile pipeline (validate/expand_wildcards/classify — so wildcard
    edges are concrete and timed-escape data is present) and BEFORE
    apply_implicit_fail_cap (so guardedness reflects what the author declared, not
    compiler defaults).

    Checks:
      * NO MANUAL EDGES — every edge auto-fires on case_advance(). A pure Mermaid
        sketch is all `-->` (auto), so this is the signature of a paste that has
        not yet had its human/event gates marked manual.
      * SHADOWED AUTO SIBLINGS — a state whose UNGUARDED auto edge is declared
        before other auto edges: advance() fires the first permitting edge, so the
        later ones can fire only after the first FAILS. Legal (it is the
        retry/divert idiom when @FAIL guards are involved) but with no guards at
        all it usually means a forgotten guard on an intended branch.
      * PERMANENT AUTO-BLOCK RISK — a non-terminal state whose every exit is an
        auto edge gated by a method guard: no manual exit, no timed escape. If the
        guards never ripen, the case stalls there forever (see AutoAdvanceBlocked).
    """
    found: list[str] = []
    if not spec.transitions:
        return found

    def _sources(t: dict) -> list[str]:
        src = t["source"]
        return list(src) if isinstance(src, (list, tuple)) else [src]

    any_manual = any(
        (s, t["trigger"]) not in spec.auto_edges
        for t in spec.transitions for s in _sources(t)
    ) or any(not w["auto"] for w in spec.pending_wildcards)
    if not any_manual and len(spec.transitions) >= 2:
        found.append(
            "no manual edges anywhere: every edge auto-fires on case_advance(). A "
            "pasted Mermaid sketch is all '-->' (auto) — mark the human/event gates "
            "manual ('== trigger ==>' or ': == trigger') before driving a case"
        )

    for state in spec.states:
        auto_entries: list[tuple[str, bool]] = []   # (trigger, guarded) in declared order
        auto_ts: list[dict] = []
        manual_exit = False
        for t in spec.transitions:
            if state not in _sources(t):
                continue
            if (state, t["trigger"]) in spec.auto_edges:
                guarded = bool(t.get("_guards"))
                auto_entries.append((t["trigger"], guarded))
                auto_ts.append(t)
            else:
                manual_exit = True

        for i, (trig, guarded) in enumerate(auto_entries):
            if not guarded and i < len(auto_entries) - 1:
                shadowed = ", ".join(f"'{tr}'" for tr, _ in auto_entries[i + 1:])
                found.append(
                    f"state '{state}': auto edge '{trig}' is unguarded and declared "
                    f"first, so sibling auto edge(s) {shadowed} can fire only after "
                    f"'{trig}' FAILS; add a guard to '{trig}' if branching is intended"
                )
                break

        if (auto_ts and not manual_exit
                and state not in spec.terminal_states
                and state not in spec.timed_escape_states
                and all(_method_guards(t.get("_guards")) for t in auto_ts)):
            found.append(
                f"state '{state}': every exit is an auto edge gated by a method "
                "guard — no manual exit, no timed escape. If the guards never "
                "ripen, the case auto-blocks here forever; consider a "
                "'[@DWELL>...]' escape net or a manual edge"
            )
    return found


# ---------------------------------------------------------------------------
# CLI — validate a chain declaration, or render it as a Mermaid diagram
# ---------------------------------------------------------------------------
# Mirrors case_briefing's module-as-CLI pattern. Validation needs nothing beyond this
# module; --render goes through to_networkx() -> case_briefing.to_mermaid(), both
# imported/raised lazily so the parser keeps its zero-dependency core.

def main(argv: Optional[list[str]] = None) -> int:
    """Implementation behind ``python -m totodev_pub.folder_backed_case_support.state_chain_cli``
    (that shim module is the runnable target; running THIS module with ``-m``
    would re-execute it under runpy, since the package __init__ already imports it).

    SOURCE is a file path, ``-`` for stdin (the default), or a literal chain
    string (recognized by containing a ``-->``/``==>`` arrow) — so a Mermaid
    sketch can be piped straight in and checked, or a single chain tried
    inline. Default mode parses and runs the whole-graph checks, printing a
    short summary; ``--render`` emits Mermaid source on stdout instead (the
    ``state`` style's output is itself a valid declaration — see
    ``case_briefing.to_mermaid``). Exit codes: 0 success, 1 parse/validation
    failure, 2 usage error.
    """
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="python -m totodev_pub.folder_backed_case_support.state_chain_cli",
        description=(
            "Validate an fsm_state_chains declaration, or render it as a Mermaid "
            "diagram. SOURCE is a file path, '-' for stdin (default), or a literal "
            "chain string."
        ),
    )
    ap.add_argument(
        "source", nargs="?", default="-",
        help="file path, '-' for stdin (default), or a literal chain string",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--render", action="store_true",
        help="emit a Mermaid diagram on stdout instead of a validation summary",
    )
    mode.add_argument(
        "--convert", action="store_true",
        help="treat the input as a (near-)Mermaid sketch: emit a cleaned, valid "
             "chain declaration on stdout, with %%%%-TODO markers wherever a human "
             "must decide (see mermaid_intake)",
    )
    ap.add_argument(
        "--style", choices=("state", "flowchart"), default="state",
        help="Mermaid dialect for --render (default: state, whose output is "
             "itself valid chain DSL)",
    )
    ap.add_argument(
        "--no-validate", action="store_true",
        help="parse only, skipping the whole-graph checks (for deliberately "
             "partial snippets that lack an initial/terminal state)",
    )
    ap.add_argument(
        "--quiet", action="store_true",
        help="suppress advisory lint warnings (see lint_spec)",
    )
    args = ap.parse_args(argv)

    if args.source == "-":
        text = sys.stdin.read()
    elif "-->" in args.source or "==>" in args.source:
        text = args.source                       # literal chain(s) on the command line
    else:
        path = Path(args.source)
        if not path.is_file():
            print(
                f"error: {args.source!r} is not a file and does not look like a "
                "chain (contains no '-->'/'==>' arrow)",
                file=sys.stderr,
            )
            return 2
        text = path.read_text(encoding="utf-8")

    if args.convert:
        from totodev_pub.folder_backed_case_support.mermaid_intake import (
            convert_mermaid_text,
        )
        converted, notes = convert_mermaid_text(text)
        print(converted)
        for note in notes:
            print(f"note: {note}", file=sys.stderr)
        try:
            spec = StateChainParser.parse(converted)
        except FsmChainParseError as e:
            # Should not happen — conversion promises parseable output.
            print(f"error: converted output does not parse: {e}", file=sys.stderr)
            return 1
        if not args.no_validate:
            try:
                spec.validate().expand_wildcards().classify()
                print("ok: converted output parses and validates", file=sys.stderr)
            except FsmChainParseError as e:
                # Expected mid-workflow (e.g. a partial sketch): report, don't fail.
                print(f"warning: converted output parses but does not validate "
                      f"yet: {e}", file=sys.stderr)
                return 0
        if not args.quiet:
            for w in lint_spec(spec):
                print(f"warning: {w}", file=sys.stderr)
        return 0

    try:
        spec = StateChainParser.parse(text)
        if not args.no_validate:
            # The default compile_fsm() order: whole-graph checks on the explicit
            # graph, then wildcard injection, then timed-escape classification.
            spec.validate().expand_wildcards().classify()
    except FsmChainParseError as e:
        print(f"error: {e}", file=sys.stderr)
        if "without a trigger label" in str(e):
            print("hint: pasting a Mermaid sketch? --convert scaffolds trigger "
                  "names and comments out what the DSL cannot express",
                  file=sys.stderr)
        return 1

    if args.render:
        try:
            graph = spec.to_networkx(include_implied_caps=False)
        except ImportError as e:                 # networkx is an optional dependency
            print(f"error: {e}", file=sys.stderr)
            return 1
        from totodev_pub.folder_backed_case_support.case_briefing import to_mermaid
        print(to_mermaid(graph, style=args.style))
        return 0

    auto_triggers = {trigger for _, trigger in spec.auto_edges}
    manual_triggers = [t for t in spec.triggers if t not in auto_triggers]
    wildcard_note = (
        f", {len(spec.pending_wildcards)} wildcard rule(s)"
        if spec.pending_wildcards else ""
    )
    print(f"OK: {len(spec.states)} states, {len(spec.transitions)} transitions"
          f"{wildcard_note}")
    if spec.initial_state is not None:
        others = sorted(spec.initial_states - {spec.initial_state})
        also = f" (also initial: {', '.join(others)})" if others else ""
        print(f"  initial: {spec.initial_state}{also}")
    if spec.terminal_states:
        print(f"  terminal: {', '.join(sorted(spec.terminal_states))}")
    print(f"  triggers: {len(spec.triggers)} "
          f"({len(auto_triggers)} auto, {len(manual_triggers)} manual)")
    if spec.timed_escape_states:
        print(f"  timed escapes: {', '.join(sorted(spec.timed_escape_states))}")
    if not args.quiet:
        for w in lint_spec(spec):
            print(f"warning: {w}", file=sys.stderr)
    return 0
