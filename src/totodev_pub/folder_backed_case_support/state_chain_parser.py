# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
StateChainParser: a tiny, Mermaid-flavoured DSL for declaring a case's finite-state
machine as a list of chain strings, plus the dumb data structure it renders into.

A chain reads left-to-right as alternating states and the connectors that join them:

    "^new==assign-->assigned--begin-->in_process--funded#finish-->closed^"

States
------
* A bare name is an ordinary state. Names use `[A-Za-z0-9_]` only (no dashes), so
  identifier-based hook conventions like `on_enter_<state>`/`on_exit_<state>` map
  directly to DSL state names.
* A LEADING `^` marks an INITIAL state (a valid entry/root): `^new`. Initial states
  are also the reachability anchors (they are exempt from the "must have an incoming
  edge" rule), which is how an entry reached only via reclassify_to() is declared.
* A TRAILING `^` marks a TERMINAL/closed state: `closed^`. Entering one fires the
  two-phase close hook.
* Initial and terminal are independent flags and may compose (`^x^`). A run of trailing
  markers is tolerated; any trailing glyph other than `^` is rejected, keeping the marker
  namespace closed against silent typos.

Connectors  `[--|==][cond#...][@FACT<op>N#...]trigger[~<dur>]-->`
----------
* `A==trigger-->B` is one transition (trigger `trigger`, source `A`, dest `B`).
* `A--trigger-->B` marks the edge AUTO-ADVANCE: advance()/run_to_completion() may fire it
  unattended. `--` is opt-in (fail-safe): a `==` edge never auto-fires, so a case
  simply waits rather than silently running past a human/event gate. The connector form
  itself carries this "may auto-fire" policy.
* `cond#trigger` attaches a GUARD: the leading `#`-separated identifiers name method
  guards. Each guard token is mapped to a `guard_<token>` carrier method and stored in the
  transition's `conditions` (e.g. `funded#finish` => conditions=["guard_funded"], trigger
  "finish"; the carrier defines `async def guard_funded`). The `guard_` prefix keeps guard
  methods in their own namespace, away from ordinary helpers and lifecycle hooks. Multiple
  guards chain: `a#b#trigger` => conditions=["guard_a", "guard_b"]. Guards are what make
  multiple auto-advance edges from one state meaningful — advance() tries each auto
  candidate in declared order and fires the first whose guard permits.
* `@FACT<op>N#trigger` attaches a FACTUAL GUARD: an `@`-prefixed, system-computed fact
  compared against a constant with one of `< <= > >=` (equality `==`/`!=` is deliberately
  UNSUPPORTED — we cannot promise to evaluate at an exact instant/count, so an equality
  test would create false expectations). Two facts are recognized:
    * `@DWELL<op><dur>` — seconds spent in the SOURCE state (dwell since the latest
      CASE_ENTER_STATE). The operand is a duration, units s|m|h|d, float allowed
      (`@DWELL>90s`, `@DWELL>=1.5h`, `@DWELL>0.5d`). `dwell_secs()` on the case computes it.
      A `>`/`>=` dwell guard is SELF-RELAXING (it ripens with time) and is what gives a
      state a guaranteed TIMED ESCAPE (see classify()/AutoAdvanceBlocked).
    * `@FAIL<op>N` — count of `CASE_FAIL_TRANSITION` events logged since the current state
      was entered (failed pre-commit attempts to LEAVE this state). The operand is a bare
      integer, NO unit (`@FAIL<3`, `@FAIL>=3`). State-scoped: every failed attempt in this
      dwell counts regardless of which trigger raised. This is the retry knob — list a
      `@FAIL<n` retry edge first and an optional `@FAIL>=n` divert edge second. RETRY IS
      OPT-IN: an auto edge that declares no `@FAIL` gets an implicit `@FAIL<1` (one attempt,
      no retry) via apply_implicit_fail_cap(); a pure timed-escape edge is exempt and
      tolerates unlimited failures (logically `@FAIL>=0`).
  A factual guard is a pure FACT, NOT a promise to fire — something must still attempt the
  trigger. At most one guard PER FACT NAME per connector; facts compose with each other and
  with method guards (e.g. `@FAIL<3#@DWELL>30m#retry`).
* `trigger~<dur>` attaches a SOFT TIMEOUT to the trigger's work — a SUFFIX (the `~` reads
  "approximately"), the only suffix decoration, because unlike the prefix guards it bounds
  the trigger's EXECUTION rather than gating entry. The duration uses the @DWELL units
  (s|m|h|d, float allowed, unit required): `assign~20s`, `fetch~1.5m`. It is the point past
  which the step is considered SLOW (a warning), NOT a hard kill; the hard-abort ceiling is
  derived by the case as a multiple of this value. Most triggers are expected to be fast and
  go un-annotated (they inherit the case's default); annotate the ones known to be slow. The
  budget is keyed by TRIGGER (it is a property of `perform_<trigger>`), so the same trigger
  may not be annotated with two different durations. Composes with everything:
  `@FAIL<3#funded#finish~3m`. (Stored in FsmChainSpec.trigger_timeouts; consumed by the
  case, which owns the warn/abort/log behavior.)

Wildcard ("from any source") chains  `*[--|==]...-->DEST`
----------
* A chain that BEGINS with `*--` or `*==` declares one edge whose source is ANY otherwise
  non-terminal state: `*==cancel-->cancelled^` means "from anywhere, `cancel` => cancelled";
  `*--timeout-->expired^` is the auto-advance variant. The connector carries the usual
  guards and `~<dur>` soft-timeout like any other.
* Exactly one transition (one destination) per wildcard chain. The concrete per-source
  edges are deduced AFTER all chains parse and AFTER validate(): terminal states and the
  destination itself (no self-loop) are excluded, and an EXPLICIT edge for the same
  trigger from a state always overrules the wildcard there.
* Wildcards inject only AFTER the typo-catching validations run on the explicit graph, so
  they can never mask a forgotten exit or a misspelled state — a state whose ONLY outgoing
  edge would be a wildcard is still flagged; declare an explicit edge.

Conventions
-----------
* Chains COLLECTIVELY must declare at least one initial and one terminal state.
* The DEFAULT initial state (used by create_in_folder) is the first initial-marked state
  encountered scanning chains in order — so an initial in the first chain wins.

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
from typing import Optional

from totodev_pub.folder_backed_case_support.exceptions import (
    FsmChainParseError,
    FsmBindingError,
)

# A state name: one or more [A-Za-z0-9_] characters (no dashes). This keeps state names
# directly representable as Python suffix identifiers for hook conventions.
_NAME_RE = re.compile(r"[A-Za-z0-9_]+")

# A FACTUAL-GUARD segment: `@NAME<op>NUMBER[unit]` with op in < <= > >= (NO ==/!=). The
# optional s|m|h|d unit belongs to time facts (@DWELL); count facts (@FAIL) take a bare int.
_FACT_GUARD_SEGMENT = r"@\s*[A-Za-z][A-Za-z0-9_]*\s*(?:<=|>=|<|>)\s*\d+(?:\.\d+)?\s*[smhd]?"

# One connector label segment: a guard/trigger identifier OR a `@FACT<op>N` factual guard.
_LABEL_SEGMENT = r"(?:" + _FACT_GUARD_SEGMENT + r"|[A-Za-z_]\w*)"

# A SOFT-TIMEOUT suffix on the trigger: `~<dur>` (e.g. `assign~20s`). `~` reads
# "approximately", matching the soft semantics: it is the duration past which the step is
# considered SLOW, not a hard kill. The unit is accepted loosely here (optional) so a
# unit-less typo gets a pointed message from _parse_trigger_timeout rather than a confusing
# whole-connector parse failure — exactly how @DWELL handles its unit.
_TIMEOUT_SUFFIX = r"(?:\s*~\s*\d+(?:\.\d+)?\s*[smhd]?)?"

# The connector between two states: `[--|==][cond#...][@FACT<op>N#...]trigger[~<dur>]-->`.
#   group 1: connector kind (`--` auto-advance, `==` manual).
#   group 2: the label — a `#`-separated run of segments (method guards / factual guards,
#            then the trigger), optionally followed by a `~<dur>` soft-timeout suffix on the
#            trigger. Identifier segments are valid Python identifiers because `transitions`
#            turns the trigger into a method and resolves condition names against the model;
#            `@...` segments are factual guards (see _parse_fact_guard); the `~<dur>` suffix
#            is split off the trigger in _parse_label (see _parse_trigger_timeout).
_CONNECTOR_RE = re.compile(
    r"(--|==)\s*"
    r"(" + _LABEL_SEGMENT + r"(?:\s*#\s*" + _LABEL_SEGMENT + r")*" + _TIMEOUT_SUFFIX + r")"
    r"\s*-->"
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

_INITIAL_MARKER = "^"      # LEADING on a state name
_TERMINAL_MARKER = "^"     # TRAILING on a state name
_WILDCARD_SOURCE = "*"     # a lone `*` in the first state slot => "from any source"

# Transition-dict keys whose (string) values name a callable resolved against the carrier
# object — the explicitly-referenced callbacks a carrier MUST provide. `_fact_guards` is
# deliberately ABSENT: factual guards (@DWELL/@FAIL) are compiled by the base class itself,
# not looked up on the carrier, so they impose no carrier-method requirement.
_CARRIER_CALLBACK_KEYS = ("conditions", "unless", "before", "after", "prepare")

# The canonical method-name PREFIXES for the two conventions the parser/base bind by name.
# Both are kept as named constants (not bare literals) so the parser, the binding check, and
# the orphan scan can never disagree about a namespace:
#   * guard methods   `guard_<token>`   — a `<token>#trigger` DSL guard resolves here.
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


def _is_async_callable(fn) -> bool:
    """True if calling `fn` yields a coroutine: a coroutine function (incl. a functools.partial
    wrapping one, which inspect unwraps on 3.8+), or a callable instance whose `__call__` is a
    coroutine function. The single fact the force_async binding check turns on."""
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


@dataclass
class FsmChainSpec:
    """The compiled FSM: the dumb intermediate the parser renders and FolderBackedCase
    consumes. Mutable by design (see module docstring). Every structure the machine and
    the auto-advance driver need lives here, so they can never drift apart.

    Attributes:
        states         every state name, in first-seen order.
        transitions    `transitions`-library dicts: {"trigger","source","dest"[,"conditions"]}.
                       A dict MAY also carry the private key "_fact_guards" (a list of
                       {"name","op","operand"} factual guards, e.g. @DWELL/@FAIL) — stripped
                       and compiled into `conditions` callables before reaching the machine.
        closed_states  states marked terminal (trailing `^`).
        initial_states states marked initial (leading `^`); also reachability anchors.
        initial_state  the DEFAULT entry state (first initial-marked, in chain order).
        auto_edges     {(source, trigger)} edges eligible for advance() (`--` connector).
        pipeline       distinct auto-advance trigger names, in first-seen order (display).
        triggers       every distinct trigger name, in first-seen order.
        primary_chain  the raw first chain string, kept for DEBUG logging / diagrams.
        pending_wildcards  unresolved `*--...-->dest` edges; expanded by expand_wildcards().
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
    """
    states: list[str] = field(default_factory=list)
    transitions: list[dict] = field(default_factory=list)
    closed_states: set[str] = field(default_factory=set)
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

    @classmethod
    def empty(cls) -> "FsmChainSpec":
        """An FSM with nothing declared — the base ABC's default before any subclass
        supplies `fsm_state_chains` (or overrides compile_fsm)."""
        return cls()

    def is_auto(self, source: str, trigger: str) -> bool:
        """Is the edge (source -> via trigger) eligible for unattended advance()?"""
        return (source, trigger) in self.auto_edges

    def validate(self) -> "FsmChainSpec":
        """Whole-graph semantic checks, kept SEPARATE from parsing so it can be invoked
        deliberately (the default compile_fsm calls it; a hand-built override may skip or
        re-run it after its own tweaks). Returns self for chaining. Raises
        FsmChainParseError on the first violation. A completely empty spec (an abstract or
        not-yet-configured subclass) is a no-op.

        Checks:
          - at least one initial (`^name`) and one terminal (`name^`) state exist;
          - V1: a terminal state has NO outgoing edge (it is the end of the road);
          - V2: a non-terminal state HAS an outgoing edge (a dead-end that isn't `^` is
                almost always a forgotten exit or a missing `^` — the one legitimate
                exception, a state left only via reclassify_to(), is handled by guidance:
                declare its successor's entry as an initial state, or add a trivial edge);
          - reachability: every non-initial state has an incoming edge (catches the
                mistyped state name, whose orphan has no way in). A wildcard's declared
                dest is exempt — it is reached only once the wildcards are injected, which
                happens AFTER validate() so the typo checks see only the explicit graph.
        """
        if not self.states:
            return self
        if not self.initial_states:
            raise FsmChainParseError(
                "no initial state declared; mark at least one entry state with a LEADING "
                "'^', e.g. '^new--...'"
            )
        if not self.closed_states:
            raise FsmChainParseError(
                "no terminal state declared; mark at least one end state with a TRAILING "
                "'^', e.g. '...-->closed^'"
            )

        out_sources: set[str] = set()
        in_dests: set[str] = set()
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            out_sources.update(srcs)
            in_dests.add(t["dest"])

        for s in self.states:
            terminal = s in self.closed_states
            if terminal and s in out_sources:
                raise FsmChainParseError(
                    f"state {s!r} is marked terminal ('^') but has an outgoing transition; "
                    "terminal states cannot be left"
                )
            if not terminal and s not in out_sources:
                raise FsmChainParseError(
                    f"state {s!r} has no outgoing transition and is not marked terminal; add "
                    "a trailing '^' if it is an end state, or give it a transition (a state "
                    "left only via reclassify_to() should declare its successor's entry as "
                    "initial, or use a trivial edge — see the reclassify docs)"
                )
            if (s not in self.initial_states and s not in in_dests
                    and s not in self.wildcard_dests):
                raise FsmChainParseError(
                    f"state {s!r} is unreachable: it has no incoming transition and is not "
                    "marked initial ('^name'). If this is a deliberate entry point, mark it "
                    "initial; otherwise it is probably a misspelled state name"
                )
        return self

    def expand_wildcards(self) -> "FsmChainSpec":
        """Inject the concrete per-source edges for any `*--...-->dest` wildcard chains,
        then return self for chaining. Call AFTER validate() so the typo-catching checks
        run against only the explicit graph (the default compile_fsm does exactly this;
        a hand-built override decides for itself). A no-op when there are no wildcards.

        For each pending wildcard, an edge `s -> dest` is created for every state `s`
        that is NOT terminal (terminals cannot be left) and NOT the dest itself (no
        self-loop), UNLESS `s` already has an EXPLICIT edge for that trigger — an explicit
        edge always overrules the wildcard. Conditions, the time guard, and the
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
                if s in self.closed_states or s == dest:
                    continue
                if (trigger, s) in claimed:
                    continue
                td: dict = {"trigger": trigger, "source": s, "dest": dest, "_wildcard": True}
                if w["conditions"]:
                    td["conditions"] = list(w["conditions"])
                if w["fact_guards"]:
                    td["_fact_guards"] = [dict(fg) for fg in w["fact_guards"]]
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
        time. Run AFTER expand_wildcards() so a blanket `*--@DWELL>=2d#timeout-->expired^`
        net is reflected. Returns self for chaining.

        A state qualifies if it has an auto edge whose guards are satisfiable BY WAITING
        ALONE — i.e. the edge carries at least one self-relaxing time fact (`@DWELL` with
        `>`/`>=`), every fact guard on it is such a fact, and it has NO method conditions
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
        """True iff transition `t` is fireable by waiting alone: no method conditions, and
        at least one fact guard with EVERY fact being a self-relaxing time fact (`@DWELL`
        `>`/`>=`). An unguarded auto edge would have fired already, so it is not a 'timed'
        escape; a `@FAIL`/`<`-style guard only tightens with time, so it disqualifies."""
        if t.get("conditions"):
            return False
        fgs = t.get("_fact_guards") or []
        if not fgs:
            return False
        for fg in fgs:
            if fg["name"] not in _TIME_FACTS or fg["op"] not in _RELAXING_OPS:
                return False
        return True

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
        driven by advance(), so a retry cap on them would have no meaning."""
        for t in self.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if not any((s, t["trigger"]) in self.auto_edges for s in srcs):
                continue
            fgs = t.get("_fact_guards") or []
            if any(fg["name"] in _COUNT_FACTS for fg in fgs):
                continue                       # explicit @FAIL policy wins
            if self._is_pure_timed_escape(t):
                continue                       # timed escape => unlimited fail tolerance
            t["_fact_guards"] = list(fgs) + [{"name": "FAIL", "op": "<", "operand": 1}]
        return self

    # ---- carrier-object compatibility (the binding check) ----
    # The spec is the ONLY thing that knows what callables its graph implies, so it is the
    # natural place to ask "is this object a suitable carrier for me?" — keeping the check
    # off FolderBackedCase. PURE except validate_object_compatibility(), which inspects the
    # object passed in but neither stores it nor mutates the spec.

    def _referenced_callbacks(self):
        """Yield (slot, item, trigger) for every explicitly-referenced transition callback in
        the compiled spec — method guards (`conditions`/`unless`) plus any hand-built
        `before`/`after`/`prepare` from a compile_fsm() override. `item` is a NAME (str, to be
        resolved on the carrier) or an already-resolved callable. For a DSL method guard the
        NAME is already the conventionized `guard_<token>` the parser stored (see
        _GUARD_METHOD_PREFIX), so existence/async checks resolve it directly. Factual guards
        are excluded on purpose (see _CARRIER_CALLBACK_KEYS)."""
        for t in self.transitions:
            trigger = t.get("trigger")
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
            method guards appear here in their `guard_<token>` form (e.g. a `funded#...`
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
        hook_patterns=(_PERFORM_HOOK_PATTERN,),
        orphan_detection: str = "error",
    ) -> None:
        """Confirm `obj` is a suitable CARRIER for this compiled FSM, raising FsmBindingError
        (listing ALL gaps at once) if not. Designed to run ONCE per concrete carrier class —
        FolderBackedCase calls it at first instantiation, the earliest point a concrete class
        is guaranteed fully assembled (Python forbids instantiating a class with unimplemented
        abstractmethods, so leaf-supplied guards are present by then).

        Checks:
          * EXISTENCE — every method guard / explicitly-named callback the spec references
            must be defined on `obj`. A DSL method guard `<token>#trigger` resolves to the
            carrier method `guard_<token>` (see _GUARD_METHOD_PREFIX), so that is the name
            that must exist. Plus `perform_<trigger>` for any trigger reachable from an auto
            edge (`==`) must exist, even if it is a no-op.
          * ASYNC (force_async, default True) — every referenced callable that IS present —
            required names, required auto-edge hooks, and any optional action method that
            happens to exist — must be a coroutine function. The case family is driven through
            async (advance(), the generated triggers); a stray `def` instead of `async def`
            would silently block the event loop, so it is rejected here. Set force_async=False
            for the rare carrier that deliberately mixes in synchronous callables.

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
        is flagged the same way a misspelled `on_enter_<state>` is."""
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

        if missing or sync or (orphan_detection == "error" and orphaned):
            raise FsmBindingError(
                type(obj).__name__,
                missing=list(missing.values()),
                sync=list(sync.values()),
                orphaned=orphaned if orphan_detection == "error" else [],
            )
        if orphan_detection == "warn" and orphaned:
            warnings.warn(
                self._format_orphan_hook_warning(type(obj).__name__, orphaned),
                UserWarning,
                stacklevel=2,
            )

    def _declared_guard_tokens(self) -> set[str]:
        """The bare guard tokens this FSM references — the part AFTER the `guard_` prefix of
        every `guard_<token>` name found in any transition's `conditions`/`unless`. These are
        exactly the `guard_<token>` carrier methods the parser emits for a `<token>#trigger`
        DSL segment, plus any `guard_`-prefixed name a hand-built compile_fsm() override put
        there. Used by orphan detection to tell a real guard method from a `guard_`-prefixed
        typo. Callables and non-`guard_` strings are ignored (the former are already resolved;
        the latter are not part of the guard convention)."""
        tokens: set[str] = set()
        for t in self.transitions:
            for slot in ("conditions", "unless"):
                val = t.get(slot)
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
    """Stateless renderer from chain strings to an FsmChainSpec. Use the classmethod
    `parse`; there is nothing to instantiate. `parse` raises on SYNTACTIC/structural
    problems; whole-graph semantic rules live in FsmChainSpec.validate()."""

    @classmethod
    def parse(cls, chains: Optional[list[str]]) -> FsmChainSpec:
        """Render `chains` into an FsmChainSpec. Empty/None -> empty spec. Raises
        FsmChainParseError (with the offending chain + index) on malformed input. Does NOT
        run the whole-graph checks (call FsmChainSpec.validate()) and does NOT expand
        `*[--|==]...-->` wildcard chains (call FsmChainSpec.expand_wildcards(),
        AFTER validate)."""
        spec = FsmChainSpec.empty()
        if not chains:
            return spec
        if isinstance(chains, str):
            raise FsmChainParseError(
                "expected a list of chain strings, got a single string; "
                "wrap it in a list, e.g. ['^a--t-->b^']"
            )

        # (trigger, source, conditions, min_dwell) -> dest, to catch genuinely
        # nondeterministic duplicates (identical guard, different dest) while allowing
        # guarded branching.
        seen_edges: dict[tuple, str] = {}

        for idx, raw in enumerate(chains):
            cls._parse_chain(raw, idx, spec, seen_edges, primary=(idx == 0))
        return spec

    # ---- per-chain ----

    @classmethod
    def _parse_chain(
        cls,
        raw: str,
        idx: int,
        spec: FsmChainSpec,
        seen_edges: dict[tuple, str],
        *,
        primary: bool,
    ) -> None:
        if not isinstance(raw, str):
            raise FsmChainParseError(
                f"chain entries must be strings, got {type(raw).__name__}", index=idx
            )
        chain = raw.strip()
        if not chain:
            raise FsmChainParseError("empty chain string", chain=raw, index=idx)

        parts = _CONNECTOR_RE.split(chain)   # [state, connector, label, state, connector, label, ...]
        state_tokens = parts[0::3]
        connectors = parts[1::3]
        labels = parts[2::3]

        if primary:
            spec.primary_chain = chain

        # A chain whose first state slot is a lone `*` is a "from any source" wildcard:
        # its concrete edges are deduced later by FsmChainSpec.expand_wildcards().
        if state_tokens and state_tokens[0].strip() == _WILDCARD_SOURCE:
            cls._parse_wildcard_chain(state_tokens, connectors, labels, raw, idx, spec)
            return

        node_names: list[str] = []
        for tok in state_tokens:
            name, initial, terminal = cls._parse_state_token(tok, raw, idx)
            node_names.append(name)
            cls._add_state(spec, name, initial=initial, terminal=terminal)

        for i, label in enumerate(labels):
            auto = connectors[i] == "--"
            conditions, fact_guards, trigger, soft_secs = cls._parse_label(label, raw, idx)
            cls._add_transition(
                spec, trigger, node_names[i], node_names[i + 1],
                conditions=conditions, fact_guards=fact_guards, auto=auto,
                soft_secs=soft_secs, raw=raw, idx=idx, seen_edges=seen_edges,
            )

    @classmethod
    def _parse_wildcard_chain(
        cls,
        state_tokens: list[str],
        connectors: list[str],
        labels: list[str],
        raw: str,
        idx: int,
        spec: FsmChainSpec,
    ) -> None:
        """Register a single `*[--|==][guards#]trigger[~<dur>]-->DEST` wildcard edge.
        Must contain exactly ONE transition to a single destination; the concrete
        per-source edges are deduced by FsmChainSpec.expand_wildcards() once every
        state is known."""
        if len(labels) != 1 or len(state_tokens) != 2:
            raise FsmChainParseError(
                "a wildcard '*--...-->' or '*==...-->' chain must contain exactly ONE transition to a "
                "single destination, e.g. '*==cancel-->cancelled^'",
                chain=raw, index=idx,
            )
        name, initial, terminal = cls._parse_state_token(state_tokens[1], raw, idx)
        cls._add_state(spec, name, initial=initial, terminal=terminal)
        auto = connectors[0] == "--"
        conditions, fact_guards, trigger, soft_secs = cls._parse_label(labels[0], raw, idx)
        cls._record_trigger_timeout(spec, trigger, soft_secs, raw, idx)
        spec.pending_wildcards.append({
            "trigger": trigger, "dest": name, "conditions": conditions,
            "fact_guards": fact_guards, "auto": auto,
        })
        spec.wildcard_dests.add(name)

    @classmethod
    def _parse_state_token(cls, token: str, raw: str, idx: int) -> tuple[str, bool, bool]:
        """Split a state token into (name, is_initial, is_terminal). Validates the name
        charset, the leading initial marker, and the trailing marker run."""
        tok = token.strip()
        if not tok:
            raise FsmChainParseError(
                "empty state name (check for stray or malformed `-->` arrows)",
                chain=raw, index=idx,
            )
        initial = False
        if tok[0] == _INITIAL_MARKER:
            initial = True
            tok = tok[1:].strip()
            if not tok:
                raise FsmChainParseError(
                    "leading '^' (initial marker) with no state name after it",
                    chain=raw, index=idx,
                )
        m = _NAME_RE.match(tok)
        if m is None or m.start() != 0:
            raise FsmChainParseError(
                f"invalid state name {token.strip()!r}; names use letters/digits/underscores "
                "only (mark initial with a LEADING '^', terminal with a TRAILING '^')",
                chain=raw, index=idx,
            )
        name = m.group(0)
        terminal = cls._parse_trailing_markers(tok[m.end():], name, token, raw, idx)
        return name, initial, terminal

    @classmethod
    def _parse_trailing_markers(
        cls, rest: str, name: str, token: str, raw: str, idx: int
    ) -> bool:
        """Read the run of trailing marker glyphs after a state name. Only `^` (terminal)
        is active; anything else fails as unknown, keeping the marker namespace closed
        against silent typos."""
        terminal = False
        for ch in rest:
            if ch == _TERMINAL_MARKER:
                terminal = True
            elif ch.isspace():
                continue
            else:
                if "--" in token or "->" in token:
                    raise FsmChainParseError(
                        f"could not parse {token.strip()!r}; a transition must be written "
                        "`A--trigger-->B` (auto) or `A==trigger-->B` (manual)",
                        chain=raw, index=idx,
                    )
                raise FsmChainParseError(
                    f"unknown marker {ch!r} on state {name!r}; only a trailing '^' (terminal) "
                    "is supported",
                    chain=raw, index=idx,
                )
        return terminal

    @classmethod
    def _parse_label(
        cls, label: str, raw: str, idx: int
    ) -> tuple[list[str], list[dict], str, Optional[float]]:
        """Split a connector label into (conditions, fact_guards, trigger, soft_secs). The
        final `#`-delimited token is the trigger (optionally carrying a `~<dur>` SOFT-timeout
        suffix, split off here); preceding tokens are either method-guard names or
        `@FACT<op>N` factual guards (parsed by _parse_fact_guard). The trigger itself may not
        be a factual guard, and at most one guard per FACT NAME is allowed. `soft_secs` is the
        annotated soft-timeout in seconds, or None when un-annotated.

        Each method-guard token `tok` is mapped to the carrier-method name `guard_<tok>` (see
        _GUARD_METHOD_PREFIX) before it enters `conditions`, so `funded#finish` yields
        conditions=["guard_funded"] — the carrier must define `async def guard_funded`."""
        tokens = [p.strip() for p in label.split("#")]
        if any(not t for t in tokens):
            raise FsmChainParseError(
                f"malformed connector label {label.strip()!r}; use `trigger` or "
                "`guard#trigger` (no empty segments)",
                chain=raw, index=idx,
            )
        trigger = tokens[-1]
        soft_secs: Optional[float] = None
        if "~" in trigger:                       # split the `~<dur>` soft-timeout off the trigger
            trigger, soft_secs = cls._parse_trigger_timeout(trigger, raw, idx)
        if trigger.startswith("@"):
            raise FsmChainParseError(
                f"a connector's trigger cannot be a factual guard ({trigger!r}); the final "
                "'#'-segment must be the trigger name (e.g. '@DWELL>60m#expire')",
                chain=raw, index=idx,
            )
        conditions: list[str] = []
        fact_guards: list[dict] = []
        seen_facts: set[str] = set()
        for tok in tokens[:-1]:
            if tok.startswith("@"):
                fg = cls._parse_fact_guard(tok, raw, idx)
                if fg["name"] in seen_facts:
                    raise FsmChainParseError(
                        f"at most one @{fg['name']} guard is allowed per connector",
                        chain=raw, index=idx,
                    )
                seen_facts.add(fg["name"])
                fact_guards.append(fg)
            else:
                conditions.append(f"{_GUARD_METHOD_PREFIX}{tok}")
        return conditions, fact_guards, trigger, soft_secs

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
            spec.closed_states.add(name)
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
        conditions: list[str],
        fact_guards: list[dict],
        auto: bool,
        soft_secs: Optional[float],
        raw: str,
        idx: int,
        seen_edges: dict[tuple, str],
    ) -> None:
        # A trigger's soft-timeout is recorded regardless of edge-dedup below: it is keyed by
        # trigger (a property of its work), and the recorder rejects conflicting annotations.
        cls._record_trigger_timeout(spec, trigger, soft_secs, raw, idx)
        # Edge identity includes the guards (method conditions + factual guards): identical
        # trigger+source+guards but different dest is genuinely ambiguous; differing
        # guards is legitimate branching.
        fact_key = tuple((fg["name"], fg["op"], fg["operand"]) for fg in fact_guards)
        key = (trigger, source, tuple(conditions), fact_key)
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
            if conditions:
                td["conditions"] = list(conditions)
            if fact_guards:
                td["_fact_guards"] = [dict(fg) for fg in fact_guards]
            spec.transitions.append(td)
            if trigger not in spec.triggers:
                spec.triggers.append(trigger)
        if auto:
            spec.auto_edges.add((source, trigger))
            if trigger not in spec.pipeline:
                spec.pipeline.append(trigger)
