# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
StateChainParser: a tiny, Mermaid-flavoured DSL for declaring a case's finite-state
machine as a list of chain strings, plus the dumb data structure it renders into.

A chain reads left-to-right as alternating states and the connectors that join them:

    "^new--assign-->assigned--*begin-->in_process--*funded#finish-->closed^"

States
------
* A bare name is an ordinary state. Names use `[A-Za-z0-9_]` with SINGLE internal
  dashes (no leading/trailing/double dash) — that constraint is what keeps the
  decorator glyphs unambiguous.
* A LEADING `^` marks an INITIAL state (a valid entry/root): `^new`. Initial states
  are also the reachability anchors (they are exempt from the "must have an incoming
  edge" rule), which is how an entry reached only via reclassify_to() is declared.
* A TRAILING `^` marks a TERMINAL/closed state: `closed^`. Entering one fires the
  two-phase close hook and suppresses the pulse.
* Initial and terminal are independent flags and may compose (`^x^`). A run of trailing
  markers is tolerated; an error-state marker glyph is intentionally DEFERRED, so any
  other trailing glyph is rejected to keep the namespace open.

Connectors  `--[*][cond#...]trigger-->`
----------
* `A--trigger-->B` is one transition (trigger `trigger`, source `A`, dest `B`).
* A leading `*` marks the edge AUTO-ADVANCE: advance()/run_to_completion() may fire it
  unattended. `*` is opt-in (fail-safe): an un-starred edge never auto-fires, so a case
  simply waits rather than silently running past a human/event gate. The `*` sits to the
  LEFT because it governs whether the edge is taken at all.
* `cond#trigger` attaches a GUARD: the leading `#`-separated identifiers become the
  transition's `conditions` (e.g. `funded#finish` => conditions=["funded"], trigger
  "finish"). Multiple guards chain: `a#b#trigger`. Guards are what make multiple
  auto-advance edges from one state meaningful — advance() tries each auto candidate in
  declared order and fires the first whose guard permits.

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

import re
from dataclasses import dataclass, field
from typing import Optional

from totodev_pub.folder_backed_case_support.exceptions import FsmChainParseError

# A state name: one or more [A-Za-z0-9_] groups joined by SINGLE dashes. Forbids
# leading/trailing/double dashes, so a connector's `--` can never be read as part of a
# name and a trailing marker glyph can never be read as a name character.
_NAME_RE = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")

# The connector between two states: `--[*][cond#...]trigger-->`.
#   group 1: the optional auto-advance star.
#   group 2: the label — a `#`-separated run of identifiers (conditions..., then trigger).
# Each identifier is a valid Python identifier because `transitions` turns the trigger
# into a method and resolves condition names against the model.
_CONNECTOR_RE = re.compile(
    r"--\s*(\*)?\s*"
    r"([A-Za-z_]\w*(?:\s*#\s*[A-Za-z_]\w*)*)"
    r"\s*-->"
)

_INITIAL_MARKER = "^"      # LEADING on a state name
_TERMINAL_MARKER = "^"     # TRAILING on a state name
# Markers reserved for the future are recognised here so they fail LOUDLY rather than
# being silently swallowed. The error-state marker is deferred, so this stays empty for
# now; adding one later is a single entry.
_RESERVED_MARKERS: dict[str, str] = {}


@dataclass
class FsmChainSpec:
    """The compiled FSM: the dumb intermediate the parser renders and FolderBackedCase
    consumes. Mutable by design (see module docstring). Every structure the machine and
    the auto-advance driver need lives here, so they can never drift apart.

    Attributes:
        states         every state name, in first-seen order.
        transitions    `transitions`-library dicts: {"trigger","source","dest"[,"conditions"]}.
        closed_states  states marked terminal (trailing `^`).
        initial_states states marked initial (leading `^`); also reachability anchors.
        initial_state  the DEFAULT entry state (first initial-marked, in chain order).
        auto_edges     {(source, trigger)} edges eligible for advance() (leading `*`).
        pipeline       distinct auto-advance trigger names, in first-seen order (display).
        triggers       every distinct trigger name, in first-seen order.
        primary_chain  the raw first chain string, kept for DEBUG logging / diagrams.
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
                mistyped state name, whose orphan has no way in).
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
            if s not in self.initial_states and s not in in_dests:
                raise FsmChainParseError(
                    f"state {s!r} is unreachable: it has no incoming transition and is not "
                    "marked initial ('^name'). If this is a deliberate entry point, mark it "
                    "initial; otherwise it is probably a misspelled state name"
                )
        return self


class StateChainParser:
    """Stateless renderer from chain strings to an FsmChainSpec. Use the classmethod
    `parse`; there is nothing to instantiate. `parse` raises on SYNTACTIC/structural
    problems; whole-graph semantic rules live in FsmChainSpec.validate()."""

    @classmethod
    def parse(cls, chains: Optional[list[str]]) -> FsmChainSpec:
        """Render `chains` into an FsmChainSpec. Empty/None -> empty spec. Raises
        FsmChainParseError (with the offending chain + index) on malformed input. Does NOT
        run the whole-graph checks — call FsmChainSpec.validate() for those."""
        spec = FsmChainSpec.empty()
        if not chains:
            return spec
        if isinstance(chains, str):
            raise FsmChainParseError(
                "expected a list of chain strings, got a single string; "
                "wrap it in a list, e.g. ['^a--t-->b^']"
            )

        # (trigger, source, conditions) -> dest, to catch genuinely nondeterministic
        # duplicates (identical guard, different dest) while allowing guarded branching.
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

        parts = _CONNECTOR_RE.split(chain)   # [state, star, label, state, star, label, ...]
        state_tokens = parts[0::3]
        stars = parts[1::3]
        labels = parts[2::3]

        if primary:
            spec.primary_chain = chain

        node_names: list[str] = []
        for tok in state_tokens:
            name, initial, terminal = cls._parse_state_token(tok, raw, idx)
            node_names.append(name)
            cls._add_state(spec, name, initial=initial, terminal=terminal)

        for i, label in enumerate(labels):
            auto = stars[i] == "*"
            conditions, trigger = cls._parse_label(label, raw, idx)
            cls._add_transition(
                spec, trigger, node_names[i], node_names[i + 1],
                conditions=conditions, auto=auto, raw=raw, idx=idx, seen_edges=seen_edges,
            )

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
                "with single internal dashes (mark initial with a LEADING '^', terminal with "
                "a TRAILING '^')",
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
        is active; reserved glyphs fail with a 'reserved' message and anything else fails
        as unknown — both keep the marker namespace closed against silent typos."""
        terminal = False
        for ch in rest:
            if ch == _TERMINAL_MARKER:
                terminal = True
            elif ch.isspace():
                continue
            elif ch in _RESERVED_MARKERS:
                raise FsmChainParseError(
                    f"marker {ch!r} on state {name!r} is reserved for {_RESERVED_MARKERS[ch]} "
                    "and is not yet supported",
                    chain=raw, index=idx,
                )
            else:
                if "--" in token or "->" in token:
                    raise FsmChainParseError(
                        f"could not parse {token.strip()!r}; a transition must be written "
                        "`A--trigger-->B` (two dashes, then `-->`)",
                        chain=raw, index=idx,
                    )
                raise FsmChainParseError(
                    f"unknown marker {ch!r} on state {name!r}; only a trailing '^' (terminal) "
                    "is supported",
                    chain=raw, index=idx,
                )
        return terminal

    @staticmethod
    def _parse_label(label: str, raw: str, idx: int) -> tuple[list[str], str]:
        """Split a connector label into (conditions, trigger): the final `#`-delimited
        token is the trigger; any preceding tokens are guard/condition method names."""
        tokens = [p.strip() for p in label.split("#")]
        if any(not t for t in tokens):
            raise FsmChainParseError(
                f"malformed connector label {label.strip()!r}; use `trigger` or "
                "`guard#trigger` (no empty segments)",
                chain=raw, index=idx,
            )
        return tokens[:-1], tokens[-1]

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

    @staticmethod
    def _add_transition(
        spec: FsmChainSpec,
        trigger: str,
        source: str,
        dest: str,
        *,
        conditions: list[str],
        auto: bool,
        raw: str,
        idx: int,
        seen_edges: dict[tuple, str],
    ) -> None:
        # Edge identity includes conditions: identical trigger+source+guards but different
        # dest is genuinely ambiguous; differing guards is legitimate branching.
        key = (trigger, source, tuple(conditions))
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
            spec.transitions.append(td)
            if trigger not in spec.triggers:
                spec.triggers.append(trigger)
        if auto:
            spec.auto_edges.add((source, trigger))
            if trigger not in spec.pipeline:
                spec.pipeline.append(trigger)
