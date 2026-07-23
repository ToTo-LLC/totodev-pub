# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Case documentation generator: static-analysis Markdown docs for a
FolderBackedCase subclass.

Spec: volatile/specs/2026-07-18-case-doc-gen-mini-spec.md. Renders lifecycle
diagram, states, triggers, guards, assertions, asset aliases, and overridden
lifecycle hooks — all pulled from the CLASS alone (``case_type_spec()``,
``discover_class_assertions()``, and hook docstrings). No case folder or live
instance is ever required; this is a pure static-analysis tool, not a status
report (see the mini-spec's non-goals).

Two-stage design, mirroring ``FsmChainSpec.to_networkx()``'s own "graph now,
format later" split:

  * ``collect()``   — builds a ``CaseTypeDoc`` (plain data, no formatting).
  * ``render_markdown()`` — turns a ``CaseTypeDoc`` into a Markdown string.
  * ``to_mermaid()`` — renders an ``FsmChainSpec.to_networkx()`` graph as a
    Mermaid diagram; used by ``render_markdown()`` but usable standalone.

Not wired into ``FolderBackedCase``/``FolderBackedCaseInterface`` — this is a
tool, not a runtime contract (mini-spec decision #7). Import it directly from
this module, or invoke it as a CLI (``python -m
totodev_pub.folder_backed_case_support.case_doc <module>:<ClassName>``).
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

from totodev_pub.folder_backed_case_support.case_assertions import (
    discover_class_assertions,
)
from totodev_pub.folder_backed_case_support.asset_schema import loader_name

if TYPE_CHECKING:
    import networkx as nx

    from totodev_pub.folder_backed_case import FolderBackedCase

__all__ = [
    "CaseDocOptions", "CaseTypeDoc", "StateDoc", "TriggerDoc", "EdgeDoc",
    "GuardDoc", "AssetDoc", "HookDoc",
    "collect", "render_markdown", "to_mermaid", "generate_case_docs",
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


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseDocOptions:
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
    diagram_style: Literal["state", "flowchart"] = "state"
    wildcard_pseudo_state: bool = False
    include_implied_caps: bool = True


# ---------------------------------------------------------------------------
# Data model (CaseTypeDoc and friends) — pure data, no formatting opinions
# ---------------------------------------------------------------------------

@dataclass
class EdgeDoc:
    source: str
    dest: str
    auto: bool
    guard_names: list[str] = field(default_factory=list)
    fact_guards: list[dict] = field(default_factory=list)
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
    states: Optional[frozenset]
    keep: bool
    many: bool


@dataclass
class HookDoc:
    name: str
    doc: Optional[str] = None


@dataclass
class CaseTypeDoc:
    case_cls_name: str
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


def collect(case_cls: "type[FolderBackedCase]", *, options: CaseDocOptions = CaseDocOptions()) -> CaseTypeDoc:
    """Build a ``CaseTypeDoc`` for ``case_cls`` — class-level only, no case
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
            guard_names = [_bare_guard_name(c) for c in data.get("conditions", [])]
            for g in guard_names:
                guard_users.setdefault(g, set()).add(trigger)
            edges.append(EdgeDoc(
                source=u,
                dest=v,
                auto=bool(data.get("auto", False)),
                guard_names=guard_names,
                fact_guards=list(data.get("fact_guards", [])),
                wildcard_expanded=bool(data.get("wildcard_expanded", False)),
                wildcard_pending=bool(data.get("wildcard_pending", False)),
            ))
            chokes = data.get("chokes", frozenset())
            soft_timeout_secs = data.get("soft_timeout_secs")
            soft_timeout_is_explicit = bool(data.get("soft_timeout_is_explicit", False))
        triggers.append(TriggerDoc(
            name=trigger,
            perform_doc=_hook_doc(case_cls, f"perform_{trigger}", mode),
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
            states=a_spec.states,
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

    return CaseTypeDoc(
        case_cls_name=case_cls.__name__,
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
    # POTENTIAL FUTURE ENHANCEMENT: this label ignores `wildcard_expanded` and
    # `pure_timed_escape` -- a synthetic wildcard fan-out edge and a normal edge
    # render identically here (the mini-spec's §5 suggested a distinct line style,
    # e.g. dashed, for wildcard_expanded=True). Not done for v1: unclear it earns
    # its complexity yet -- the Markdown triggers table already surfaces both via
    # its Wildcard column, so the diagram's plain label may be enough on its own.
    trigger = data.get("trigger", "")
    if data.get("soft_timeout_is_explicit") and data.get("soft_timeout_secs"):
        trigger = f"{trigger}~{_fmt_duration(data['soft_timeout_secs'])}"
    guard_names = [_bare_guard_name(c) for c in data.get("conditions", [])]
    facts = [
        _fmt_fact_guard(fg)
        for fg in data.get("fact_guards", [])
        if not fg.get("implicit")
    ]
    annotations = guard_names + facts
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


def to_mermaid(graph: "nx.MultiDiGraph", *, style: Literal["state", "flowchart"] = "state") -> str:
    """Render a ``FsmChainSpec.to_networkx()`` graph as Mermaid source. Pure
    renderer over the graph — never recomputes FSM structure.

    Both styles use the SAME visual vocabulary as the chain DSL itself: an auto
    edge is a plain arrow, a manual edge is the "double-thick" spelling (the
    flowchart's thick ``== label ==>`` arrow; the state diagram's ``: == label``
    label marker, since stateDiagram-v2 has only one arrow), and edge labels are
    the edge's exact DSL text (``trigger~<dur> [guard, ...]``).

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
        return _to_mermaid_flowchart(graph)
    return _to_mermaid_state(graph)


def _to_mermaid_state(graph: "nx.MultiDiGraph") -> str:
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
    for u, v, data in graph.edges(data=True):
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


def _to_mermaid_flowchart(graph: "nx.MultiDiGraph") -> str:
    # POTENTIAL FUTURE ENHANCEMENT: unlike _to_mermaid_state, this renderer doesn't
    # emit a `%% primary chain: ...` title comment from graph.graph["primary_chain"].
    # Left out for v1 for the same reason as the wildcard/timed-escape styling
    # above -- not yet clear it's worth the asymmetry with the state-style renderer.
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
    for u, v, data in graph.edges(data=True):
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


def _fmt_states(states: Optional[frozenset]) -> str:
    return ", ".join(sorted(states)) if states is not None else "(any)"


def _fmt_doc(doc: Optional[str]) -> str:
    return doc.replace("\n", " ") if doc else "—"


def render_markdown(doc: CaseTypeDoc, *, options: CaseDocOptions = CaseDocOptions()) -> str:
    parts: list[str] = [f"# {doc.case_cls_name}"]
    if doc.class_doc:
        parts.append(doc.class_doc)

    if options.include_diagram:
        mermaid = to_mermaid(doc.fsm_graph, style=options.diagram_style)
        parts.append(f"## Lifecycle\n\n```mermaid\n{mermaid}\n```")

    if options.include_states_table and doc.states:
        rows = ["| State | Initial | Default Initial | Terminal | Timed Escape | On Enter | On Exit |",
                "|---|---|---|---|---|---|---|"]
        for s in doc.states:
            rows.append(
                f"| {s.name} | {s.initial} | {s.default_initial} | {s.terminal} | "
                f"{s.timed_escape} | {_fmt_doc(s.on_enter_doc)} | {_fmt_doc(s.on_exit_doc)} |"
            )
        parts.append("## States\n\n" + "\n".join(rows))

    if options.include_triggers_table and doc.triggers:
        sections = []
        for t in doc.triggers:
            lines = [f"### `{t.name}`"]
            if t.perform_doc:
                lines.append(f"**Perform:** {t.perform_doc}")
            if t.before_doc:
                lines.append(f"**Before:** {t.before_doc}")
            if t.after_doc:
                lines.append(f"**After:** {t.after_doc}")
            edge_rows = ["| Source | Dest | Auto | Guards | Fact Guards | Wildcard |",
                         "|---|---|---|---|---|---|"]
            for e in t.edges:
                fact_str = ", ".join(_fmt_fact_guard(fg) for fg in e.fact_guards) or "—"
                wildcard = "pending" if e.wildcard_pending else ("expanded" if e.wildcard_expanded else "—")
                edge_rows.append(
                    f"| {e.source} | {e.dest} | {e.auto} | {_fmt_names(e.guard_names)} | "
                    f"{fact_str} | {wildcard} |"
                )
            lines.append("\n".join(edge_rows))
            timeout = (
                f"{t.soft_timeout_secs:g}s ({'explicit' if t.soft_timeout_is_explicit else 'default'})"
                if t.soft_timeout_secs is not None else "—"
            )
            lines.append(f"Chokes: {_fmt_names(sorted(t.chokes))} · Soft timeout: {timeout}")
            sections.append("\n\n".join(lines))
        parts.append("## Triggers\n\n" + "\n\n".join(sections))

    if options.include_guards_table and doc.guards:
        rows = ["| Guard | Used By | Doc |", "|---|---|---|"]
        for g in doc.guards:
            rows.append(f"| `{g.name}` | {_fmt_names(g.used_by_triggers)} | {_fmt_doc(g.doc)} |")
        parts.append("## Guards\n\n" + "\n".join(rows))

    if options.include_assertions and any(s.assertions for s in doc.states):
        sections = []
        for s in doc.states:
            if not s.assertions:
                continue
            items = "\n".join(f"- **{slug}**: {_fmt_doc(d)}" for slug, d in s.assertions)
            sections.append(f"### {s.name}\n\n{items}")
        footnote = (
            "Cases of this type may also carry per-case file assertions "
            "(`assertions/*.py`) not shown here — those are runtime, per-folder, "
            "and invisible to static class inspection."
        )
        parts.append("## Assertions\n\n" + "\n\n".join(sections) + f"\n\n*{footnote}*")

    if options.include_assets and doc.assets:
        rows = ["| Alias | Path | Loader | States | Keep | Many |", "|---|---|---|---|---|---|"]
        for a in doc.assets:
            rows.append(
                f"| {a.alias} | `{a.relative_path}` | {a.loader_name or '—'} | "
                f"{_fmt_states(a.states)} | {a.keep} | {a.many} |"
            )
        parts.append("## Asset Aliases\n\n" + "\n".join(rows))

    if options.include_hooks and doc.overridden_hooks:
        sections = [f"### `{h.name}`\n\n{_fmt_doc(h.doc)}" for h in doc.overridden_hooks]
        parts.append("## Overridden Lifecycle Hooks\n\n" + "\n\n".join(sections))

    return "\n\n".join(parts) + "\n"


def generate_case_docs(case_cls: "type[FolderBackedCase]", *, options: CaseDocOptions = CaseDocOptions()) -> str:
    """``collect()`` + ``render_markdown()`` in one call — the library entry point."""
    return render_markdown(collect(case_cls, options=options), options=options)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m totodev_pub.folder_backed_case_support.case_doc",
        description="Render Markdown documentation for a FolderBackedCase subclass.",
    )
    parser.add_argument("target", help="dotted module path and class name, e.g. mypackage.cases:MyCase")
    parser.add_argument("--no-diagram", dest="include_diagram", action="store_false", default=True)
    parser.add_argument("--no-states", dest="include_states_table", action="store_false", default=True)
    parser.add_argument("--no-triggers", dest="include_triggers_table", action="store_false", default=True)
    parser.add_argument("--no-guards", dest="include_guards_table", action="store_false", default=True)
    parser.add_argument("--no-assertions", dest="include_assertions", action="store_false", default=True)
    parser.add_argument("--no-assets", dest="include_assets", action="store_false", default=True)
    parser.add_argument("--no-hooks", dest="include_hooks", action="store_false", default=True)
    parser.add_argument(
        "--docstrings", choices=["none", "first_paragraph", "full"], default="first_paragraph",
    )
    parser.add_argument("--diagram-style", choices=["state", "flowchart"], default="state")
    parser.add_argument("--wildcard-pseudo-state", action="store_true", default=False)
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

    options = CaseDocOptions(
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
        include_implied_caps=args.include_implied_caps,
    )
    print(generate_case_docs(case_cls, options=options))
    return 0


if __name__ == "__main__":
    sys.exit(main())
