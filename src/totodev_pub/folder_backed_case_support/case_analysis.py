# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Shared class-level static analysis for FolderBackedCase subclasses.

``analyze(case_cls)`` introspects a case CLASS — never a folder or a live
instance — into a render-agnostic ``CaseAnalysis``: states, triggers (auto vs
manual, with structured ``perform_<trigger>`` kwargs contracts), guards (with a
trigger cross-reference), asset aliases, class assertions, and overridden
lifecycle hooks, plus the ``to_networkx()`` FSM graph.

This is the common front-end for the tools that document or drive a case type —
the case briefing generator (``case_briefing``) and the workbench marimo
skeleton generator (``case_workbench_marimo``) — so a trigger renamed in the
DSL, a guard added, or a ``perform_`` param changed is reflected everywhere
without each tool re-deriving FSM structure.

Docstrings are carried **raw/untrimmed** here; presentation choices (first
paragraph vs full, mermaid styling, table formatting) belong to the renderers.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from totodev_pub.folder_backed_case_support.asset_schema import loader_name
from totodev_pub.folder_backed_case_support.case_assertions import (
    discover_class_assertions,
)
from totodev_pub.folder_backed_case_support.perform_signature import (
    PerformParam,
    describe_perform_params,
)
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    _copy_guards,
    _is_method_guard,
)

if TYPE_CHECKING:
    import networkx as nx

    from totodev_pub.folder_backed_case import FolderBackedCase

__all__ = [
    "CaseAnalysis", "StateFacts", "TriggerFacts", "EdgeFacts", "GuardFacts",
    "AssetFacts", "HookFacts", "analyze", "FolderBackedCaseAnalyzer",
]

# The DSL's method-guard prefix and the overridable lifecycle hooks, duplicated
# as literals here (same rationale as case_briefing): to_networkx()'s public
# contract is the source of truth, not parser internals.
_GUARD_PREFIX = "guard_"
_OVERRIDABLE_HOOKS = (
    "on_transition_exception", "on_terminating", "on_assertion_failed",
    "case_ext_status_info",
)


def _bare_guard_name(condition: str) -> str:
    if condition.startswith(_GUARD_PREFIX):
        return condition[len(_GUARD_PREFIX):]
    return condition


def _raw_doc(case_cls: type, name: str) -> Optional[str]:
    """Raw ``inspect.getdoc`` for a hook by name, or None if absent/uncallable.
    Untrimmed — renderers apply their own docstring-mode policy."""
    fn = getattr(case_cls, name, None)
    if fn is None or not callable(fn):
        return None
    return inspect.getdoc(fn)


# ---------------------------------------------------------------------------
# Data model — frozen facts, raw docstrings, no formatting opinions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeFacts:
    source: str
    dest: str
    auto: bool
    guards: list = field(default_factory=list)  # heterogeneous, declaration order
    wildcard_expanded: bool = False
    wildcard_pending: bool = False


@dataclass(frozen=True)
class StateFacts:
    name: str
    initial: bool
    default_initial: bool
    terminal: bool
    timed_escape: bool
    on_enter_doc_raw: Optional[str] = None
    on_exit_doc_raw: Optional[str] = None
    # (slug, method_name, raw doc) — the CLASS assertion channel only.
    class_assertions: list[tuple[str, str, Optional[str]]] = field(default_factory=list)


@dataclass(frozen=True)
class TriggerFacts:
    name: str
    is_manual: bool
    perform_params: list[PerformParam] = field(default_factory=list)
    perform_doc_raw: Optional[str] = None
    before_doc_raw: Optional[str] = None
    after_doc_raw: Optional[str] = None
    edges: list[EdgeFacts] = field(default_factory=list)
    chokes: frozenset = frozenset()
    soft_timeout_secs: Optional[float] = None
    soft_timeout_is_explicit: bool = False


@dataclass(frozen=True)
class GuardFacts:
    name: str  # bare token, e.g. "funded" for guard_funded
    doc_raw: Optional[str] = None
    used_by_triggers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssetFacts:
    alias: str
    relative_path: str
    loader_name: Optional[str]
    trust_states: Optional[frozenset]
    keep: bool
    many: bool


@dataclass(frozen=True)
class HookFacts:
    name: str
    doc_raw: Optional[str] = None


@dataclass(frozen=True)
class CaseAnalysis:
    case_cls_name: str
    source: str  # ``module:ClassName`` — same shape the CLI target uses
    class_doc_raw: Optional[str]  # cleandoc'd own __doc__ (no MRO fallback), untrimmed
    fsm_graph: "nx.MultiDiGraph"
    states: list[StateFacts]
    triggers: list[TriggerFacts]
    guards: list[GuardFacts]
    assets: list[AssetFacts]
    overridden_hooks: list[HookFacts]
    initial_state: Optional[str]
    terminal_states: frozenset

    @property
    def manual_triggers(self) -> list[TriggerFacts]:
        """Triggers with at least one manual (``==``) edge — the ones a driver
        must fire explicitly (``await case.<trigger>(...)``)."""
        return [t for t in self.triggers if t.is_manual]

    @property
    def auto_triggers(self) -> list[TriggerFacts]:
        """Triggers whose every edge is automatic (fired by ``case_advance()``)."""
        return [t for t in self.triggers if not t.is_manual]

    def state(self, name: str) -> Optional[StateFacts]:
        return next((s for s in self.states if s.name == name), None)


# ---------------------------------------------------------------------------
# analyze() — the sole introspection pass (class-level only)
# ---------------------------------------------------------------------------

def analyze(
    case_cls: "type[FolderBackedCase]",
    *,
    wildcard_pseudo_state: bool = False,
    include_implied_caps: bool = True,
) -> CaseAnalysis:
    """Introspect ``case_cls`` into a ``CaseAnalysis`` — class-level only, no
    case folder or instance is ever touched. Docstrings are raw/untrimmed; the
    caller decides how to present them."""
    spec = case_cls.case_type_spec()
    fsm = spec.fsm
    graph = fsm.to_networkx(
        wildcard_pseudo_state=wildcard_pseudo_state,
        include_implied_caps=include_implied_caps,
    )

    # case_cls.__doc__ (not inspect.getdoc()) deliberately: a subclass that omits
    # its own docstring should carry None, not silently inherit FolderBackedCase's
    # generic blurb (inspect.getdoc() walks the MRO for exactly that fallback).
    own_doc = case_cls.__doc__
    class_doc_raw = inspect.cleandoc(own_doc) if own_doc else None

    assertions_by_state = discover_class_assertions(case_cls, fsm.states)

    states: list[StateFacts] = []
    for name in fsm.states:
        node = graph.nodes[name]
        class_assertions = [
            (slug, method_name, _raw_doc(case_cls, method_name))
            for slug, method_name in assertions_by_state.get(name, [])
        ]
        states.append(StateFacts(
            name=name,
            initial=node["initial"],
            default_initial=node["default_initial"],
            terminal=node["terminal"],
            timed_escape=node["timed_escape"],
            on_enter_doc_raw=_raw_doc(case_cls, f"on_enter_{name}"),
            on_exit_doc_raw=_raw_doc(case_cls, f"on_exit_{name}"),
            class_assertions=class_assertions,
        ))

    guard_users: dict[str, set[str]] = {}
    triggers: list[TriggerFacts] = []
    for trigger in fsm.triggers:
        edges: list[EdgeFacts] = []
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
            edges.append(EdgeFacts(
                source=u,
                dest=v,
                auto=bool(data.get("auto", False)),
                guards=guards,
                wildcard_expanded=bool(data.get("wildcard_expanded", False)),
                wildcard_pending=bool(data.get("wildcard_pending", False)),
            ))
            # last-edge-wins (matches case_briefing.collect(); preserved verbatim)
            chokes = data.get("chokes", frozenset())
            soft_timeout_secs = data.get("soft_timeout_secs")
            soft_timeout_is_explicit = bool(data.get("soft_timeout_is_explicit", False))
        perform_name = f"perform_{trigger}"
        perform_fn = getattr(case_cls, perform_name, None)
        triggers.append(TriggerFacts(
            name=trigger,
            is_manual=any(not e.auto for e in edges),
            perform_params=(
                describe_perform_params(perform_fn) if callable(perform_fn) else []
            ),
            perform_doc_raw=_raw_doc(case_cls, perform_name),
            before_doc_raw=_raw_doc(case_cls, f"before_{trigger}"),
            after_doc_raw=_raw_doc(case_cls, f"after_{trigger}"),
            edges=edges,
            chokes=chokes,
            soft_timeout_secs=soft_timeout_secs,
            soft_timeout_is_explicit=soft_timeout_is_explicit,
        ))

    guards = [
        GuardFacts(
            name=bare,
            doc_raw=_raw_doc(case_cls, f"{_GUARD_PREFIX}{bare}"),
            used_by_triggers=sorted(users),
        )
        for bare, users in sorted(guard_users.items())
    ]

    assets = [
        AssetFacts(
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
        HookFacts(name=name, doc_raw=_raw_doc(case_cls, name))
        for name in _OVERRIDABLE_HOOKS
        if getattr(case_cls, name) is not getattr(_Base, name)
    ]

    initial_state = (
        next((s.name for s in states if s.default_initial), None)
        or next((s.name for s in states if s.initial), None)
    )
    terminal_states = frozenset(s.name for s in states if s.terminal)

    return CaseAnalysis(
        case_cls_name=case_cls.__name__,
        source=f"{case_cls.__module__}:{case_cls.__name__}",
        class_doc_raw=class_doc_raw,
        fsm_graph=graph,
        states=states,
        triggers=triggers,
        guards=guards,
        assets=assets,
        overridden_hooks=overridden_hooks,
        initial_state=initial_state,
        terminal_states=terminal_states,
    )


class FolderBackedCaseAnalyzer:
    """Thin OO handle over :func:`analyze` (spec §3). Stateless; use it when an
    object is more convenient than the free function."""

    def __init__(
        self, *, wildcard_pseudo_state: bool = False, include_implied_caps: bool = True,
    ) -> None:
        self._wildcard_pseudo_state = wildcard_pseudo_state
        self._include_implied_caps = include_implied_caps

    def analyze(self, case_cls: "type[FolderBackedCase]") -> CaseAnalysis:
        return analyze(
            case_cls,
            wildcard_pseudo_state=self._wildcard_pseudo_state,
            include_implied_caps=self._include_implied_caps,
        )
