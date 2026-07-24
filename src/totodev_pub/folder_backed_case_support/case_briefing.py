# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Case briefing generator: static-analysis Markdown for a FolderBackedCase
subclass.

A **case briefing** is the class-level design handoff for a case type —
lifecycle diagram, states, triggers, guards, assertions, asset aliases, and
overridden lifecycle hooks — assembled from the CLASS alone
(``case_type_spec()``, ``discover_class_assertions()``, and hook docstrings).
No case folder or live instance is ever required; this is a pure
static-analysis tool, not a status report (see the mini-spec's non-goals).

Spec: volatile/specs/2026-07-18-case-briefing-gen-mini-spec.md.

Two-stage design, mirroring ``FsmChainSpec.to_networkx()``'s own "graph now,
format later" split:

  * ``collect()``   — builds a ``CaseBriefingDoc`` (plain data, no formatting).
  * ``render_markdown()`` — turns a ``CaseBriefingDoc`` into a case briefing
    (Markdown string).
  * ``to_mermaid()`` — renders an ``FsmChainSpec.to_networkx()`` graph as a
    Mermaid diagram; used by ``render_markdown()`` but usable standalone.

Not wired into ``FolderBackedCase``/``FolderBackedCaseInterface`` — this is a
tool, not a runtime contract (mini-spec decision #7). Import it directly from
this module, or invoke it as a CLI (``python -m
totodev_pub.folder_backed_case_support.case_briefing <module>:<ClassName>``).
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Optional

from totodev_pub.folder_backed_case_support.case_assertions import (
    discover_class_assertions,
)
from totodev_pub.folder_backed_case_support.asset_schema import loader_name
from totodev_pub.folder_backed_case_support.perform_signature import format_perform_params
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    _copy_guards,
    _is_fact_guard,
    _is_method_guard,
)

if TYPE_CHECKING:
    import networkx as nx

    from totodev_pub.folder_backed_case import FolderBackedCase

__all__ = [
    "CaseBriefingOptions", "CaseBriefingDoc", "StateDoc", "TriggerDoc", "EdgeDoc",
    "GuardDoc", "AssetDoc", "HookDoc",
    "collect", "render_markdown", "to_mermaid", "generate_case_briefing",
]

# The DSL's method-guard prefix (see FsmChainSpec.to_networkx()'s own docstring:
# "method-guard names (guard_<token>)"). Duplicated here as a literal rather than
# importing the parser's private _GUARD_METHOD_PREFIX -- to_networkx()'s public
# contract is the source of truth this module consumes, not parser internals.
_GUARD_PREFIX = "guard_"

# The overridable lifecycle hooks a subclass may customize (SECTION 3 of
# FolderBackedCase). Checked by identity against the base class to decide
# whether a subclass actually overrode one.
_OVERRIDABLE_HOOKS = (
    "on_transition_exception", "on_terminating", "on_assertion_failed",
    "case_ext_status_info",
)

_WILDCARD_SENTINEL = "*"  # must match state_chain_parser._WILDCARD_SOURCE
_MERMAID_WILDCARD_ID = "ANY_STATE"

# Module path used in generated-doc stamps and CLI prog — keep in sync with
# ``python -m totodev_pub.folder_backed_case_support.case_briefing``.
_GENERATOR_TOOL = "totodev_pub.folder_backed_case_support.case_briefing"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseBriefingOptions:
    """Toggles for what ``collect()``/``render_markdown()`` produce. Mirrors
    ``to_networkx()``'s own kwarg style; ``wildcard_pseudo_state`` and
    ``include_implied_caps`` are passed straight through to it."""

    include_diagram: bool = True
    include_states_table: bool = True
    include_triggers_table: bool = True
    include_guards_table: bool = True
    include_assertions: bool = True
    include_assets: bool = True
    include_hooks: bool = True
    docstring_mode: Literal["none", "first_paragraph", "full"] = "first_paragraph"
    # flowchart: thick ``==>`` for manual edges, plain labels (docs default).
    # state: stateDiagram-v2 (no thick arrows — see ``to_mermaid``); kept for
    # DSL round-trip and ``--diagram-style state``.
    diagram_style: Literal["state", "flowchart"] = "flowchart"
    wildcard_pseudo_state: bool = False
    # Diagram + triggers-table clarity: ``to_networkx()`` always emits both the
    # abstract ``* -> dest`` pending rule AND the concrete per-state fan-out
    # (``wildcard_expanded``). Docs default to the abstract ``*`` row/edge only;
    # set True to show the concrete fan-out instead (diagram draws those edges
    # too; the table then lists real source states rather than ``*``).
    include_wildcard_expanded_edges: bool = False
    include_implied_caps: bool = True


# ---------------------------------------------------------------------------
# Data model (CaseBriefingDoc and friends) — pure data, no formatting opinions
# ---------------------------------------------------------------------------

@dataclass
class EdgeDoc:
    source: str
    dest: str
    auto: bool
    guards: list = field(default_factory=list)  # heterogeneous, declaration order
    wildcard_expanded: bool = False
    wildcard_pending: bool = False


@dataclass
class StateDoc:
    name: str
    initial: bool
    default_initial: bool
    terminal: bool
    timed_escape: bool
    on_enter_doc: Optional[str] = None
    on_exit_doc: Optional[str] = None
    assertions: list[tuple[str, Optional[str]]] = field(default_factory=list)


@dataclass
class TriggerDoc:
    name: str
    perform_doc: Optional[str] = None
    perform_params: list[str] = field(default_factory=list)
    before_doc: Optional[str] = None
    after_doc: Optional[str] = None
    edges: list[EdgeDoc] = field(default_factory=list)
    chokes: frozenset = frozenset()
    soft_timeout_secs: Optional[float] = None
    soft_timeout_is_explicit: bool = False


@dataclass
class GuardDoc:
    name: str
    doc: Optional[str] = None
    used_by_triggers: list[str] = field(default_factory=list)


@dataclass
class AssetDoc:
    alias: str
    relative_path: str
    loader_name: Optional[str]
    trust_states: Optional[frozenset]
    keep: bool
    many: bool


@dataclass
class HookDoc:
    name: str
    doc: Optional[str] = None


@dataclass
class CaseBriefingDoc:
    case_cls_name: str
    source: str  # ``module:ClassName`` — same shape the CLI target uses
    class_doc: Optional[str]
    fsm_graph: "nx.MultiDiGraph"
    states: list[StateDoc]
    triggers: list[TriggerDoc]
    guards: list[GuardDoc]
    assets: list[AssetDoc]
    overridden_hooks: list[HookDoc]


# ---------------------------------------------------------------------------
# collect() — class-level introspection only, never a case folder/instance
# ---------------------------------------------------------------------------

def _first_paragraph(doc: Optional[str]) -> Optional[str]:
    if not doc:
        return None
    first, _, _ = doc.strip().partition("\n\n")
    return first.strip() or None


def _hook_doc(case_cls: type, name: str, mode: str) -> Optional[str]:
    if mode == "none":
        return None
    fn = getattr(case_cls, name, None)
    if fn is None or not callable(fn):
        return None
    doc = inspect.getdoc(fn)
    if not doc:
        return None
    return doc if mode == "full" else _first_paragraph(doc)


def _bare_guard_name(condition: str) -> str:
    if condition.startswith(_GUARD_PREFIX):
        return condition[len(_GUARD_PREFIX):]
    return condition


def collect(case_cls: "type[FolderBackedCase]", *, options: CaseBriefingOptions = CaseBriefingOptions()) -> CaseBriefingDoc:
    """Build a ``CaseBriefingDoc`` for ``case_cls`` — class-level only, no case
    folder or instance is ever touched."""
    mode = options.docstring_mode
    spec = case_cls.case_type_spec()
    fsm = spec.fsm
    graph = fsm.to_networkx(
        wildcard_pseudo_state=options.wildcard_pseudo_state,
        include_implied_caps=options.include_implied_caps,
    )

    # case_cls.__doc__ (not inspect.getdoc()) deliberately: a subclass that omits its
    # own docstring should render with none, not silently inherit FolderBackedCase's
    # generic class blurb (inspect.getdoc() walks the MRO for exactly that fallback).
    own_doc = case_cls.__doc__
    class_doc = inspect.cleandoc(own_doc) if own_doc and mode != "none" else None
    if class_doc is not None and mode != "full":
        class_doc = _first_paragraph(class_doc)

    assertions_by_state = discover_class_assertions(case_cls, fsm.states)

    states: list[StateDoc] = []
    for name in fsm.states:
        node = graph.nodes[name]
        assertions = [
            (slug, _hook_doc(case_cls, method_name, mode))
            for slug, method_name in assertions_by_state.get(name, [])
        ]
        states.append(StateDoc(
            name=name,
            initial=node["initial"],
            default_initial=node["default_initial"],
            terminal=node["terminal"],
            timed_escape=node["timed_escape"],
            on_enter_doc=_hook_doc(case_cls, f"on_enter_{name}", mode),
            on_exit_doc=_hook_doc(case_cls, f"on_exit_{name}", mode),
            assertions=assertions,
        ))

    guard_users: dict[str, set[str]] = {}
    triggers: list[TriggerDoc] = []
    for trigger in fsm.triggers:
        edges: list[EdgeDoc] = []
        chokes = frozenset()
        soft_timeout_secs = None
        soft_timeout_is_explicit = False
        for u, v, data in graph.edges(data=True):
            if data.get("trigger") != trigger:
                continue
            guards = _copy_guards(data.get("guards"))
            for item in guards:
                if _is_method_guard(item):
                    guard_users.setdefault(_bare_guard_name(item), set()).add(trigger)
            edges.append(EdgeDoc(
                source=u,
                dest=v,
                auto=bool(data.get("auto", False)),
                guards=guards,
                wildcard_expanded=bool(data.get("wildcard_expanded", False)),
                wildcard_pending=bool(data.get("wildcard_pending", False)),
            ))
            chokes = data.get("chokes", frozenset())
            soft_timeout_secs = data.get("soft_timeout_secs")
            soft_timeout_is_explicit = bool(data.get("soft_timeout_is_explicit", False))
        perform_name = f"perform_{trigger}"
        perform_fn = getattr(case_cls, perform_name, None)
        triggers.append(TriggerDoc(
            name=trigger,
            perform_doc=_hook_doc(case_cls, perform_name, mode),
            perform_params=(
                format_perform_params(perform_fn) if callable(perform_fn) else []
            ),
            before_doc=_hook_doc(case_cls, f"before_{trigger}", mode),
            after_doc=_hook_doc(case_cls, f"after_{trigger}", mode),
            edges=edges,
            chokes=chokes,
            soft_timeout_secs=soft_timeout_secs,
            soft_timeout_is_explicit=soft_timeout_is_explicit,
        ))

    guards = [
        GuardDoc(
            name=bare,
            doc=_hook_doc(case_cls, f"{_GUARD_PREFIX}{bare}", mode),
            used_by_triggers=sorted(users),
        )
        for bare, users in sorted(guard_users.items())
    ]

    assets = [
        AssetDoc(
            alias=alias,
            relative_path=(a_spec := spec.assets.spec(alias)).relative_path,
            loader_name=loader_name(a_spec.loader),
            trust_states=a_spec.trust_states,
            keep=a_spec.keep,
            many=a_spec.many,
        )
        for alias in spec.assets.aliases()
    ]

    from totodev_pub.folder_backed_case import FolderBackedCase as _Base
    overridden_hooks = [
        HookDoc(name=name, doc=_hook_doc(case_cls, name, mode))
        for name in _OVERRIDABLE_HOOKS
        if getattr(case_cls, name) is not getattr(_Base, name)
    ]

    return CaseBriefingDoc(
        case_cls_name=case_cls.__name__,
        source=f"{case_cls.__module__}:{case_cls.__name__}",
        class_doc=class_doc,
        fsm_graph=graph,
        states=states,
        triggers=triggers,
        guards=guards,
        assets=assets,
        overridden_hooks=overridden_hooks,
    )


# ---------------------------------------------------------------------------
# Mermaid diagram rendering — consumes an already-built to_networkx() graph
# ---------------------------------------------------------------------------

_DAY_SECS = 86400.0
_HOUR_SECS = 3600.0
_MINUTE_SECS = 60.0
# Summary-level DWELL labels: promote to a coarser unit only when within this
# many seconds of an exact multiple (checked both above and below the multiple).
_DWELL_UNIT_TOLERANCE_SECS = 30.0


def _fmt_duration(secs: float) -> str:
    """Format seconds as the DSL's `<dur>` token, choosing the largest unit that
    divides cleanly (14d, 1.5m, 90s, ...) so the output parses back to the same
    value. Used for soft-timeout suffixes where round-trip matters."""
    for unit, size in (("d", _DAY_SECS), ("h", _HOUR_SECS), ("m", _MINUTE_SECS)):
        if secs >= size and secs % size == 0:
            return f"{secs / size:g}{unit}"
    return f"{secs:g}s"


def _near_unit_multiple(secs: float, unit_secs: float) -> bool:
    """True when ``secs`` is within tolerance of a whole number of ``unit_secs``."""
    rem = secs % unit_secs
    return min(rem, unit_secs - rem) < _DWELL_UNIT_TOLERANCE_SECS


def _fmt_dwell_duration(secs: float) -> str:
    """Format a dwell operand (always seconds) for summary diagrams / tables.

    Prefer days / hours / minutes when the value is near a whole unit; otherwise
    keep seconds. Thresholds are deliberately loose — these labels are for
    Mermaid/tables, not exact runtime math. Soft-timeout suffixes keep using
    :func:`_fmt_duration` (exact divisibility) so they round-trip through the
    DSL parser.
    """
    secs = abs(float(secs))

    if secs > 23.9 * _HOUR_SECS and _near_unit_multiple(secs, _DAY_SECS):
        return f"{round(secs / _DAY_SECS):g}d"
    if secs > 0.95 * _HOUR_SECS and _near_unit_multiple(secs, _HOUR_SECS):
        return f"{round(secs / _HOUR_SECS):g}h"
    if secs > 0.95 * _MINUTE_SECS:
        return f"{round(secs / _MINUTE_SECS):g}m"
    return f"{secs:g}s"


def _fmt_fact_guard(fg: dict) -> str:
    """Format a factual guard for diagram/table labels (`@DWELL>14d`, `@FAIL<3`).

    DWELL operands are summary-formatted via :func:`_fmt_dwell_duration`.
    """
    name, op, operand = fg["name"], fg["op"], fg["operand"]
    if name == "DWELL":
        return f"@DWELL{op}{_fmt_dwell_duration(operand)}"
    return f"@{name}{op}{operand}"


def _edge_label(data: dict) -> str:
    # The label is the edge's DSL spelling: `trigger[~<dur>] [guard, ...]` — the same
    # grammar StateChainParser accepts, which is what makes a rendered state-style
    # diagram round-trip back through the parser. Compiler-injected implicit @FAIL
    # caps are excluded (they are framework defaults, not the author's declaration;
    # the Markdown triggers table still shows them); the `~<dur>` soft-timeout is
    # included only when the author annotated it.
    #
    # Wildcard fan-out: by default ``to_mermaid(..., include_wildcard_expanded=False)``
    # omits ``wildcard_expanded`` edges entirely (keeps the abstract pending
    # ``* -> dest`` rule). When fan-out is included, expanded and ordinary edges
    # share the same label style; the triggers table's Wildcard column is the
    # detailed inventory.
    trigger = data.get("trigger", "")
    if data.get("soft_timeout_is_explicit") and data.get("soft_timeout_secs"):
        trigger = f"{trigger}~{_fmt_duration(data['soft_timeout_secs'])}"
    annotations = []
    for item in data.get("guards") or []:
        if _is_fact_guard(item):
            if item.get("implicit"):
                continue
            annotations.append(_fmt_fact_guard(item))
        else:
            annotations.append(_bare_guard_name(item))
    return f"{trigger} [{', '.join(annotations)}]" if annotations else trigger


def _mermaid_id(node: str) -> str:
    return _MERMAID_WILDCARD_ID if node == _WILDCARD_SENTINEL else node


def _mermaid_state_label(label: str) -> str:
    """Quote a stateDiagram-v2 transition label so Mermaid treats it as opaque text.

    Characters like ``[``, ``]``, and ``>`` (common in ``@DWELL>14d`` guard lists)
    break bare ``: label`` syntax. Always quoting keeps diagrams renderable while
    preserving the same DSL text inside the quotes for round-trip through
    :class:`StateChainParser` (which strips optional surrounding quotes).
    """
    escaped = label.replace('"', "#quot;")
    return f'"{escaped}"'


def to_mermaid(
    graph: "nx.MultiDiGraph",
    *,
    style: Literal["state", "flowchart"] = "state",
    include_wildcard_expanded: bool = False,
) -> str:
    """Render a ``FsmChainSpec.to_networkx()`` graph as Mermaid source. Pure
    renderer over the graph — never recomputes FSM structure.

    Styles share the same label text (``trigger~<dur> [guard, ...]``) but differ
    in how they show auto vs manual:

    * **flowchart** (documentation default via ``CaseBriefingOptions``): auto edges
      use plain ``-->``; manual edges use Mermaid's thick ``==>``. Labels never
      carry a leading ``==`` — thickness is the signal.
    * **state** (``stateDiagram-v2``): Mermaid has only one transition arrow and
      no ``linkStyle`` for thickness, so manual edges prefix the label with the
      DSL's ``==`` marker (``: "== approve [funded]"``). Prefer flowchart for
      human-facing docs; keep state when you need the DSL round-trip below.

    Wildcard edges: ``to_networkx()`` emits both the abstract pending rule
    (``* -> dest``, ``wildcard_pending``) and every concrete fan-out edge
    (``wildcard_expanded``). ``include_wildcard_expanded`` defaults to False so
    diagrams keep a single ``*`` hub transition; set True to also draw the
    per-state fan-out (or hub-and-spoke spokes when the graph was built with
    ``wildcard_pseudo_state=True``).

    State-style transition labels are always double-quoted so Mermaid-hostile
    characters (``[]``, ``>``, …) render correctly. Flowchart style already
    quotes labels via ``|"…"|.``

    ROUND-TRIP: the ``state`` style's output is itself a valid
    ``fsm_state_chains`` declaration — ``StateChainParser.parse()`` strips the
    optional quotes on colon labels and reproduces the spec it was rendered
    from, provided the graph was built with ``include_implied_caps=False`` (so
    no compiler-injected defaults masquerade as declarations) and the spec
    declares no wildcard chains (a wildcard renders via a synthetic
    ``ANY_STATE`` node, which parses as an ordinary state)."""
    if style == "flowchart":
        return _to_mermaid_flowchart(
            graph, include_wildcard_expanded=include_wildcard_expanded,
        )
    return _to_mermaid_state(
        graph, include_wildcard_expanded=include_wildcard_expanded,
    )


def _iter_mermaid_edges(graph: "nx.MultiDiGraph", *, include_wildcard_expanded: bool):
    """Yield ``(u, v, data)`` for edges that should appear in a Mermaid diagram."""
    for u, v, data in graph.edges(data=True):
        if data.get("wildcard_expanded") and not include_wildcard_expanded:
            continue
        yield u, v, data


def _to_mermaid_state(
    graph: "nx.MultiDiGraph", *, include_wildcard_expanded: bool = False,
) -> str:
    """Render as ``stateDiagram-v2`` with quoted colon labels (Mermaid-safe)."""
    lines = ["stateDiagram-v2"]
    primary_chain = graph.graph.get("primary_chain")
    if primary_chain:
        lines.append(f"    %% primary chain: {primary_chain}")
    if _WILDCARD_SENTINEL in graph.nodes:
        lines.append(f'    state "*" as {_MERMAID_WILDCARD_ID}')
    for node, data in graph.nodes(data=True):
        if data.get("initial"):
            lines.append(f"    [*] --> {_mermaid_id(node)}")
    for node, data in graph.nodes(data=True):
        if data.get("terminal"):
            lines.append(f"    {_mermaid_id(node)} --> [*]")
    for u, v, data in _iter_mermaid_edges(
        graph, include_wildcard_expanded=include_wildcard_expanded,
    ):
        # Manual edges carry the DSL's `==` label marker — stateDiagram-v2 has no
        # thick arrow, so the marker rides inside the label, keeping the line both
        # valid Mermaid AND valid chain DSL (see to_mermaid round-trip note).
        label = _edge_label(data)
        if not data.get("auto"):
            label = f"== {label}"
        lines.append(
            f"    {_mermaid_id(u)} --> {_mermaid_id(v)} : {_mermaid_state_label(label)}"
        )
    return "\n".join(lines)


def _to_mermaid_flowchart(
    graph: "nx.MultiDiGraph", *, include_wildcard_expanded: bool = False,
) -> str:
    # POTENTIAL FUTURE ENHANCEMENT: unlike _to_mermaid_state, this renderer doesn't
    # emit a `%% primary chain: ...` title comment from graph.graph["primary_chain"].
    # Left out for v1 for the same reason as timed-escape styling — not yet clear
    # it's worth the asymmetry with the state-style renderer.
    lines = [
        "flowchart TD",
        "    classDef initialState fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;",
        "    classDef terminalState fill:#fce4ec,stroke:#ad1457,stroke-width:2px;",
    ]
    initials, terminals = [], []
    for node, data in graph.nodes(data=True):
        node_id = _mermaid_id(node)
        if node == _WILDCARD_SENTINEL:
            lines.append(f'    {node_id}{{{{"*"}}}}')
        else:
            lines.append(f'    {node_id}["{node}"]')
        if data.get("initial"):
            initials.append(node_id)
        if data.get("terminal"):
            terminals.append(node_id)
    for u, v, data in _iter_mermaid_edges(
        graph, include_wildcard_expanded=include_wildcard_expanded,
    ):
        # Same glyphs as the chain DSL: `-->` auto, thick `==>` manual. Labels are
        # quoted because DSL guard brackets are Mermaid shape syntax when bare.
        arrow = "-->" if data.get("auto") else "==>"
        lines.append(f'    {_mermaid_id(u)} {arrow}|"{_edge_label(data)}"| {_mermaid_id(v)}')
    if initials:
        lines.append(f"    class {','.join(initials)} initialState")
    if terminals:
        lines.append(f"    class {','.join(terminals)} terminalState")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt_names(names: list[str]) -> str:
    return ", ".join(names) if names else "—"


def _fmt_trust_states(trust_states: Optional[frozenset]) -> str:
    if trust_states is None:
        return "(any)"
    return ", ".join(f"`{s}`" for s in sorted(trust_states))


def _fmt_can_go_to(dests: list[tuple[str, bool]]) -> str:
    """Can-go-to cell: dest names sorted; bold when reachable via a manual edge
    (parallel to thick ``==>`` in the flowchart diagram). Blank when none."""
    parts: list[str] = []
    for name, via_manual in dests:
        parts.append(f"**{name}**" if via_manual else name)
    return ", ".join(parts)


def _has_non_wildcard_trigger(graph, state: str, trigger: str) -> bool:
    """True if ``state`` has an explicit (non-fan-out) edge for ``trigger``."""
    for _, _, data in graph.out_edges(state, data=True):
        if data.get("trigger") == trigger and not data.get("wildcard_expanded"):
            return True
    return False


def _can_go_to_states(
    graph, state: str, *, include_wildcard_expanded: bool,
) -> list[tuple[str, bool]]:
    """Distinct 1-hop destinations from ``state``, including self-loops.

    Each item is ``(dest, via_manual)`` where ``via_manual`` is True if at least
    one edge to that dest is manual (``==`` / thick ``==>``). Sorted by dest name.

    Wildcard policy matches the triggers table / diagram: by default omit
    ``wildcard_expanded`` fan-out and instead apply abstract ``wildcard_pending``
    rules to eligible states; with fan-out on, use the concrete edges only.
    """
    # dest → True if reachable via any manual edge considered for this cell
    via_manual: dict[str, bool] = {}

    def _note(dest: str, manual: bool) -> None:
        if dest == _WILDCARD_SENTINEL:
            return
        via_manual[dest] = via_manual.get(dest, False) or manual

    for _, v, data in graph.out_edges(state, data=True):
        if data.get("wildcard_expanded") and not include_wildcard_expanded:
            continue
        if data.get("wildcard_pending"):
            continue
        dest = data.get("wildcard_dest") or v
        _note(dest, not bool(data.get("auto", False)))

    if not include_wildcard_expanded and _WILDCARD_SENTINEL in graph:
        node = graph.nodes.get(state) or {}
        if not node.get("terminal", False):
            for _, dest, data in graph.out_edges(_WILDCARD_SENTINEL, data=True):
                if not data.get("wildcard_pending"):
                    continue
                if dest == state:
                    continue
                trigger = data.get("trigger")
                if trigger and _has_non_wildcard_trigger(graph, state, trigger):
                    continue
                _note(dest, not bool(data.get("auto", False)))

    return sorted(via_manual.items(), key=lambda item: item[0])


def _fmt_doc(doc: Optional[str]) -> str:
    return doc.replace("\n", " ") if doc else "—"


def _fmt_method(name: str) -> str:
    """Format a bound hook/guard/assertion as a callable for programmer eyes."""
    return f"`{name}()`"


def _fmt_perform_method(name: str, params: list[str]) -> str:
    """Format ``perform_<trigger>`` with its kwargs contract for programmer eyes.

    ``...`` stands for omitted ``self`` / ``tctx`` (not shown for clarity); the
    listed names are the trigger kwargs callers pass through ``case.<trigger>``.
    """
    if not params:
        return f"`{name}(...)`"
    return f"`{name}(..., {', '.join(params)})`"


def _fmt_guard_ref(name: str) -> str:
    """DSL guard identity mapped to the bound ``guard_<name>()`` method."""
    return f"`{name}` → {_fmt_method(f'{_GUARD_PREFIX}{name}')}"


def _fmt_sparse_bool(v: bool) -> str:
    return "True" if v else ""


def _fmt_initial_terminal(s: StateDoc) -> str:
    """Compact Initial / Terminal role cell; blank when neither applies.

    Bold **Initial** marks the default initial state. ``Both`` covers the rare
    case where a state is both initial and terminal.
    """
    if s.initial and s.terminal:
        return "Both"
    if s.initial:
        return "**Initial**" if s.default_initial else "Initial"
    if s.terminal:
        return "Terminal"
    return ""


def _fmt_guard_item(item) -> str:
    if _is_fact_guard(item):
        text = _fmt_fact_guard(item)
        # Implied @FAIL<1> (compiler default) is italic, not code — so it reads as
        # framework policy rather than an author-declared guard. Explicit facts stay
        # in backticks like method guards.
        if item.get("implicit"):
            return f"*{text}*"
        return f"`{text}`"
    return _fmt_method(item)


def _fmt_guards_cell(guards: list) -> str:
    if not guards:
        return ""
    return ", ".join(_fmt_guard_item(g) for g in guards)


def _fmt_asset_path(relative_path: str, *, many: bool) -> str:
    """Path cell: code span; italicize the whole cell when ``many=True`` (glob)."""
    cell = f"`{relative_path}`"
    return f"*{cell}*" if many else cell


def _fmt_asset_flags(*, keep: bool) -> str:
    """Sparse Flags cell — currently only ``keep``; blank when nothing to flag."""
    return "keep" if keep else ""


def _fmt_state_hook_cell(kind: str, state: str, doc: Optional[str]) -> str:
    """On Enter / On Exit flag-table cell: method() only when present, else —."""
    if doc is None:
        return "—"
    return _fmt_method(f"{kind}_{state}")


def render_markdown(doc: CaseBriefingDoc, *, options: CaseBriefingOptions = CaseBriefingOptions()) -> str:
    stamp = (
        f"<!-- Case briefing generated by {_GENERATOR_TOOL} from {doc.source} "
        f"on {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->"
    )
    parts: list[str] = [stamp, f"# {doc.case_cls_name}"]
    if doc.class_doc:
        parts.append(doc.class_doc)

    if options.include_diagram:
        mermaid = to_mermaid(
            doc.fsm_graph,
            style=options.diagram_style,
            include_wildcard_expanded=options.include_wildcard_expanded_edges,
        )
        parts.append(f"## Lifecycle\n\n```mermaid\n{mermaid}\n```")

    if options.include_states_table and doc.states:
        rows = [
            "| State | Initial / Terminal | Has Timed Escape | On Enter | On Exit | Can go to |",
            "|---|---|---|---|---|---|",
        ]
        for s in doc.states:
            can_go = _fmt_can_go_to(
                _can_go_to_states(
                    doc.fsm_graph,
                    s.name,
                    include_wildcard_expanded=options.include_wildcard_expanded_edges,
                )
            )
            rows.append(
                f"| {s.name} | {_fmt_initial_terminal(s)} | {_fmt_sparse_bool(s.timed_escape)} | "
                f"{_fmt_state_hook_cell('on_enter', s.name, s.on_enter_doc)} | "
                f"{_fmt_state_hook_cell('on_exit', s.name, s.on_exit_doc)} | {can_go} |"
            )
        states_block = "## States\n\n" + "\n".join(rows)
        # Docstrings for enter/exit hooks live outside the flags table — the
        # On Enter / On Exit columns only name the bound methods.
        hook_sections = []
        for s in doc.states:
            items = []
            if s.on_enter_doc:
                items.append(
                    f"- **On enter** {_fmt_method(f'on_enter_{s.name}')}: {_fmt_doc(s.on_enter_doc)}"
                )
            if s.on_exit_doc:
                items.append(
                    f"- **On exit** {_fmt_method(f'on_exit_{s.name}')}: {_fmt_doc(s.on_exit_doc)}"
                )
            if items:
                hook_sections.append(f"### {s.name}\n\n" + "\n".join(items))
        if hook_sections:
            states_block += "\n\n" + "\n\n".join(hook_sections)
        parts.append(states_block)

    if options.include_triggers_table and doc.triggers:
        sections = []
        for t in doc.triggers:
            # Guard-area arrow style: DSL name → bound perform_*(kwargs contract)
            lines = [
                f"### `{t.name}` → {_fmt_perform_method(f'perform_{t.name}', t.perform_params)}"
            ]
            if t.perform_doc:
                lines.append(t.perform_doc)
            if t.before_doc:
                lines.append(f"**Before:** {_fmt_method(f'before_{t.name}')} — {t.before_doc}")
            if t.after_doc:
                lines.append(f"**After:** {_fmt_method(f'after_{t.name}')} — {t.after_doc}")
            timeout_cell = ""
            if t.soft_timeout_secs is not None:
                timeout_cell = (
                    f"{t.soft_timeout_secs:g}s "
                    f"({'explicit' if t.soft_timeout_is_explicit else 'default'})"
                )
            chokes_cell = ", ".join(sorted(t.chokes)) if t.chokes else ""
            # Match diagram fan-out policy: default keeps abstract ``*`` rows;
            # ``include_wildcard_expanded_edges`` swaps those for concrete sources.
            if options.include_wildcard_expanded_edges:
                edges = [e for e in t.edges if not e.wildcard_pending]
            else:
                edges = [e for e in t.edges if not e.wildcard_expanded]
            edge_rows = [
                "| Source | Dest | Auto | Guards | Chokes | Soft timeout |",
                "|---|---|---|---|---|---|",
            ]
            for e in edges:
                edge_rows.append(
                    f"| {e.source} | {e.dest} | {_fmt_sparse_bool(e.auto)} | "
                    f"{_fmt_guards_cell(e.guards)} | {chokes_cell} | {timeout_cell} |"
                )
            lines.append("\n".join(edge_rows))
            sections.append("\n\n".join(lines))
        parts.append("## Triggers\n\n" + "\n\n".join(sections))

    if options.include_guards_table and doc.guards:
        # List form (like Assertions): arrow heading + used-by + doc — avoids a
        # wide three-column table with prose jammed into a Doc cell.
        sections = []
        for g in doc.guards:
            lines = [f"### {_fmt_guard_ref(g.name)}"]
            lines.append(f"**Used by:** {_fmt_names(g.used_by_triggers)}")
            if g.doc:
                lines.append(_fmt_doc(g.doc))
            sections.append("\n\n".join(lines))
        parts.append("## Guards\n\n" + "\n\n".join(sections))

    if options.include_assertions and any(s.assertions for s in doc.states):
        # Flat table: method names already encode the state (case_assert_<state>_…).
        rows = ["| Method | Description |", "|---|---|"]
        for s in doc.states:
            for slug, d in s.assertions:
                desc = d.replace("\n", " ") if d else ""
                rows.append(
                    f"| {_fmt_method(f'case_assert_{s.name}_{slug}')} | {desc} |"
                )
        footnote = (
            "Cases of this type may also carry per-case file assertions "
            "(`assertions/*.py`) not shown here — those are runtime, per-folder, "
            "and invisible to static class inspection."
        )
        parts.append("## Assertions\n\n" + "\n".join(rows) + f"\n\n*{footnote}*")

    if options.include_assets and doc.assets:
        rows = [
            "| Alias | Path | Loader | Trust states | Flags |",
            "|---|---|---|---|---|",
        ]
        for a in doc.assets:
            rows.append(
                f"| {a.alias} | {_fmt_asset_path(a.relative_path, many=a.many)} | "
                f"{a.loader_name or '—'} | {_fmt_trust_states(a.trust_states)} | "
                f"{_fmt_asset_flags(keep=a.keep)} |"
            )
        parts.append("## Asset Aliases\n\n" + "\n".join(rows))

    if options.include_hooks and doc.overridden_hooks:
        sections = [
            f"### {_fmt_method(h.name)}\n\n{_fmt_doc(h.doc)}" for h in doc.overridden_hooks
        ]
        parts.append("## Overridden Lifecycle Hooks\n\n" + "\n\n".join(sections))

    return "\n\n".join(parts) + "\n"


def generate_case_briefing(case_cls: "type[FolderBackedCase]", *, options: CaseBriefingOptions = CaseBriefingOptions()) -> str:
    """Build a case briefing for ``case_cls`` — ``collect()`` + ``render_markdown()``."""
    return render_markdown(collect(case_cls, options=options), options=options)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m totodev_pub.folder_backed_case_support.case_briefing",
        description=(
            "Render a case briefing (class-level design handoff) for a "
            "FolderBackedCase subclass. Section flags below omit parts of "
            "the briefing; they do not change its meaning."
        ),
    )
    parser.add_argument("target", help="dotted module path and class name, e.g. mypackage.cases:MyCase")
    parser.add_argument(
        "--no-diagram", dest="include_diagram", action="store_false", default=True,
        help="Omit the lifecycle diagram from the case briefing.",
    )
    parser.add_argument(
        "--no-states", dest="include_states_table", action="store_false", default=True,
        help="Omit the states table from the case briefing.",
    )
    parser.add_argument(
        "--no-triggers", dest="include_triggers_table", action="store_false", default=True,
        help="Omit the triggers section from the case briefing.",
    )
    parser.add_argument(
        "--no-guards", dest="include_guards_table", action="store_false", default=True,
        help="Omit the guards section from the case briefing.",
    )
    parser.add_argument(
        "--no-assertions", dest="include_assertions", action="store_false", default=True,
        help="Omit assertions from the case briefing.",
    )
    parser.add_argument(
        "--no-assets", dest="include_assets", action="store_false", default=True,
        help="Omit asset aliases from the case briefing.",
    )
    parser.add_argument(
        "--no-hooks", dest="include_hooks", action="store_false", default=True,
        help="Omit overridden lifecycle hooks from the case briefing.",
    )
    parser.add_argument(
        "--docstrings", choices=["none", "first_paragraph", "full"], default="first_paragraph",
    )
    parser.add_argument(
        "--diagram-style", choices=["state", "flowchart"], default="flowchart",
        help="Mermaid style for the lifecycle diagram (default: flowchart — "
             "thick ==> for manual edges). Use 'state' for stateDiagram-v2 / "
             "DSL round-trip (manual edges marked with '==' in the label).",
    )
    parser.add_argument("--wildcard-pseudo-state", action="store_true", default=False)
    parser.add_argument(
        "--wildcard-fanout",
        dest="include_wildcard_expanded_edges",
        action="store_true",
        default=False,
        help="Show concrete per-state wildcard fan-out in the Mermaid diagram "
             "and triggers table instead of the abstract '* -> dest' rule "
             "(default: abstract '*' only).",
    )
    parser.add_argument(
        "--no-implied-caps", dest="include_implied_caps", action="store_false", default=True,
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if ":" not in args.target:
        parser.error("target must be '<module>:<ClassName>' (e.g. mypackage.cases:MyCase)")
    module_name, _, class_name = args.target.partition(":")
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
        parser.error(f"{args.target} is not a FolderBackedCase subclass")
        return 2

    options = CaseBriefingOptions(
        include_diagram=args.include_diagram,
        include_states_table=args.include_states_table,
        include_triggers_table=args.include_triggers_table,
        include_guards_table=args.include_guards_table,
        include_assertions=args.include_assertions,
        include_assets=args.include_assets,
        include_hooks=args.include_hooks,
        docstring_mode=args.docstrings,
        diagram_style=args.diagram_style,
        wildcard_pseudo_state=args.wildcard_pseudo_state,
        include_wildcard_expanded_edges=args.include_wildcard_expanded_edges,
        include_implied_caps=args.include_implied_caps,
    )
    print(generate_case_briefing(case_cls, options=options))
    return 0


if __name__ == "__main__":
    sys.exit(main())
