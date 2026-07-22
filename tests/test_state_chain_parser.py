import pytest

from totodev_pub.folder_backed_case_support.constants import DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS
from totodev_pub.folder_backed_case_support.exceptions import FsmBindingError
from totodev_pub.folder_backed_case_support.exceptions import FsmChainParseError
from totodev_pub.folder_backed_case_support.state_chain_parser import StateChainParser


def test_auto_edge_uses_double_dash_connector():
    spec = StateChainParser.parse(["^new--begin-->open==finish-->done^"])

    assert ("new", "begin") in spec.auto_edges
    assert spec.pipeline == ["begin"]
    assert spec.transitions[0]["trigger"] == "begin"
    assert spec.transitions[0]["source"] == "new"
    assert spec.transitions[0]["dest"] == "open"


def test_legacy_star_auto_edge_syntax_is_rejected():
    with pytest.raises(FsmChainParseError):
        StateChainParser.parse(["^new--*begin-->open--finish-->done^"])


def test_missing_hook_for_auto_trigger_is_rejected():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        pass

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "perform_assign" in msg
    assert "auto-advance" in msg


def test_missing_hook_for_manual_trigger_is_allowed():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        pass

    spec.validate_object_compatibility(Carrier())


def test_required_auto_hook_must_be_async():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        def perform_assign(self, tctx):
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "perform_assign" in msg
    assert "synchronous" in msg


def test_implied_carrier_attributes_require_auto_hooks_only():
    spec = StateChainParser.parse(
        ["^new--assign-->assigned==notify-->done^"]
    )

    required, optional = spec.implied_carrier_attributes()
    assert "perform_assign" in required
    assert "perform_notify" in optional


def test_hyphenated_state_name_is_rejected():
    with pytest.raises(FsmChainParseError):
        StateChainParser.parse(["^new-item--begin-->open^"])


def test_orphan_detection_error_mode_rejects_unknown_hook_methods():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, tctx):
            return None

        async def on_enter_assgined(self, tctx):  # typo: assigned
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier(), orphan_detection="error")
    msg = str(excinfo.value)
    assert "orphan" in msg
    assert "on_enter_assgined" in msg


def test_orphan_detection_warn_mode_emits_warning():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, tctx):
            return None

        async def before_assgin(self, tctx):  # typo: assign
            return None

    with pytest.warns(UserWarning):
        spec.validate_object_compatibility(Carrier(), orphan_detection="warn")


def test_orphan_detection_off_mode_suppresses_orphan_checks():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, tctx):
            return None

        async def after_assgin(self, tctx):  # typo: assign
            return None

    spec.validate_object_compatibility(Carrier(), orphan_detection="off")


def test_orphan_detection_requires_valid_mode():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, tctx):
            return None

    with pytest.raises(ValueError):
        spec.validate_object_compatibility(Carrier(), orphan_detection="maybe")


# ---------------------------------------------------------------------------
# Method-guard `guard_<token>` convention
# ---------------------------------------------------------------------------

def test_method_guard_token_maps_to_guard_prefix():
    spec = StateChainParser.parse(["^new==funded#approved#finish-->done^"])

    conds = spec.transitions[-1]["conditions"]
    assert conds == ["guard_funded", "guard_approved"]


def test_missing_guard_method_is_rejected_with_prefixed_name():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def funded(self, tctx):  # bare name: not the guard_ convention
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "guard_funded" in msg


def test_async_guard_method_binds_cleanly():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, tctx):
            return True

    spec.validate_object_compatibility(Carrier())


def test_sync_guard_method_is_rejected():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        def guard_funded(self, tctx):  # sync: must be async
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "guard_funded" in msg
    assert "synchronous" in msg


def test_orphan_detection_error_mode_rejects_unknown_guard_method():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, tctx):
            return True

        async def guard_fundded(self, tctx):  # typo: funded
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier(), orphan_detection="error")
    msg = str(excinfo.value)
    assert "orphan" in msg
    assert "guard_fundded" in msg
    assert "guard" in msg


def test_orphan_detection_warn_mode_flags_unknown_guard_method():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, tctx):
            return True

        async def guard_fundded(self, tctx):  # typo: funded
            return True

    with pytest.warns(UserWarning):
        spec.validate_object_compatibility(Carrier(), orphan_detection="warn")


def test_orphan_detection_off_mode_allows_unknown_guard_method():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, tctx):
            return True

        async def guard_fundded(self, tctx):  # typo: funded
            return True

    spec.validate_object_compatibility(Carrier(), orphan_detection="off")


def test_factual_guards_do_not_imply_guard_methods():
    """`@DWELL`/`@FAIL` are compiled by the base class; they live in `_fact_guards`, not
    `conditions`, so they impose no `guard_<name>` carrier method and contribute nothing
    to the declared-guard set used by orphan detection."""
    spec = StateChainParser.parse(["^new--@DWELL>30m#timeout-->done^"])

    assert spec._declared_guard_tokens() == set()
    assert "conditions" not in spec.transitions[-1]

    class Carrier:
        async def perform_timeout(self, tctx):  # timeout is auto (`--`)
            return None

    spec.validate_object_compatibility(Carrier())


# ---------------------------------------------------------------------------
# Hook arity: every recognized hook must accept the trigger context `tctx`
# ---------------------------------------------------------------------------

def test_hook_without_tctx_param_is_rejected():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self):  # missing the tctx parameter
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "perform_assign" in msg
    assert "tctx" in msg


def test_guard_without_tctx_param_is_rejected():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self):  # missing the tctx parameter
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "guard_funded" in msg
    assert "tctx" in msg


def test_hook_with_varargs_accepts_tctx():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, *args):  # *args can receive the single tctx
            return None

    spec.validate_object_compatibility(Carrier())


def test_require_tctx_false_skips_arity_check():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self):  # no tctx, but the scan is disabled
            return None

    spec.validate_object_compatibility(Carrier(), require_tctx=False)


# ---------------------------------------------------------------------------
# to_networkx(): lossless rendering into a networkx.MultiDiGraph
# ---------------------------------------------------------------------------
# networkx is an optional dependency (not required for core FSM usage), so each
# test below skips at collection time via importorskip rather than gating the
# whole module -- the rest of this file must keep running without it installed.

def test_to_networkx_missing_dependency_raises_import_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "networkx":
            raise ImportError("simulated missing networkx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    spec = StateChainParser.parse(["^new--go-->done^"])
    with pytest.raises(ImportError) as excinfo:
        spec.to_networkx()
    assert "networkx" in str(excinfo.value)


def test_to_networkx_maps_state_flags_to_node_attributes():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["^new--go-->open==finish-->done^"]).validate()

    g = spec.to_networkx()

    assert set(g.nodes) == {"new", "open", "done"}
    assert g.nodes["new"] == {
        "initial": True, "default_initial": True, "terminal": False, "timed_escape": False,
    }
    assert g.nodes["open"] == {
        "initial": False, "default_initial": False, "terminal": False, "timed_escape": False,
    }
    assert g.nodes["done"] == {
        "initial": False, "default_initial": False, "terminal": True, "timed_escape": False,
    }


def test_to_networkx_edge_attributes_capture_guards_auto_and_facts():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(
        ["^new--@FAIL<3#funded#finish-->done^"]
    ).validate().classify()

    g = spec.to_networkx()

    edges = list(g.edges(data=True))
    assert len(edges) == 1
    u, v, data = edges[0]
    assert (u, v) == ("new", "done")
    assert data["trigger"] == "finish"
    assert data["auto"] is True
    assert data["manual"] is False
    assert data["conditions"] == ["guard_funded"]
    assert data["fact_guards"] == [{"name": "FAIL", "op": "<", "operand": 3}]
    assert data["wildcard_expanded"] is False
    # a `<`-style @FAIL guard tightens rather than relaxes, so this is not a timed escape.
    assert data["pure_timed_escape"] is False
    assert g.nodes["new"]["timed_escape"] is False


def test_to_networkx_flags_pure_timed_escape_edges_and_states():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(
        ["^new--go-->waiting--@DWELL>=30m#timeout-->done^"]
    ).validate().classify()

    g = spec.to_networkx()

    _, _, data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "timeout")
    assert data["pure_timed_escape"] is True
    assert data["fact_guards"] == [{"name": "DWELL", "op": ">=", "operand": 1800.0}]
    assert g.nodes["waiting"]["timed_escape"] is True


def test_to_networkx_carries_trigger_timeout_and_choke_metadata():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["^new--assign~20s-->done^"]).validate()
    spec.trigger_chokes = {"assign": frozenset({"db"})}

    g = spec.to_networkx()

    _, _, data = next(iter(g.edges(data=True)))
    assert data["soft_timeout_secs"] == 20.0
    assert data["chokes"] == frozenset({"db"})
    assert g.graph["trigger_timeouts"] == {"assign": 20.0}
    assert g.graph["trigger_chokes"] == {"assign": frozenset({"db"})}


def test_to_networkx_is_multigraph_and_keeps_parallel_edges_distinct():
    """Two auto edges leave the same source with different triggers/guards -- a plain
    DiGraph would collapse same-(source,dest) edges; MultiDiGraph must keep both."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^fork--gated#alpha-->done^",
        "fork--@DWELL>1h#beta-->done^",
    ]).validate()

    g = spec.to_networkx()

    import networkx as nx
    assert isinstance(g, nx.MultiDiGraph)
    triggers = sorted(d["trigger"] for _, _, d in g.edges(data=True))
    assert triggers == ["alpha", "beta"]
    assert g.number_of_edges("fork", "done") == 2


def test_to_networkx_pending_wildcard_renders_as_sentinel_node_and_edge():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["*==cancel-->cancelled^"])

    g = spec.to_networkx()

    assert g.nodes["*"] == {"wildcard_source": True}
    _, dest, data = next(iter(g.edges(data=True)))
    assert dest == "cancelled"
    assert data["trigger"] == "cancel"
    assert data["wildcard_pending"] is True
    assert data["auto"] is False and data["manual"] is True


def test_to_networkx_wildcard_expansion_adds_concrete_edges_alongside_sentinel():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^new--go-->open==finish-->done^",
        "*==cancel-->cancelled^",
    ]).validate().expand_wildcards()

    g = spec.to_networkx()

    # concrete, per-source edges injected by expand_wildcards()
    concrete = [
        (u, v) for u, v, d in g.edges(data=True)
        if d["trigger"] == "cancel" and d.get("wildcard_expanded")
    ]
    assert set(concrete) == {("new", "cancelled"), ("open", "cancelled")}
    # the abstract rule (expand_wildcards() never clears pending_wildcards) still renders too
    assert g.has_edge("*", "cancelled")


def test_to_networkx_fact_guards_include_dwell_and_explicit_fail():
    """The built-in @DWELL/@FAIL factual guards (compiled by the base class itself, not a
    carrier method) show up on the edge exactly as declared."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(
        ["^new--@FAIL<3#retry-->waiting--@DWELL>=2h#timeout-->done^"]
    ).validate().classify()

    g = spec.to_networkx()

    _, _, retry_data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "retry")
    assert retry_data["fact_guards"] == [{"name": "FAIL", "op": "<", "operand": 3}]

    _, _, timeout_data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "timeout")
    assert timeout_data["fact_guards"] == [{"name": "DWELL", "op": ">=", "operand": 7200.0}]
    assert timeout_data["pure_timed_escape"] is True


def test_to_networkx_shows_implicit_fail_cap_after_apply_implicit_fail_cap():
    """An unguarded auto edge gets NO @FAIL guard until apply_implicit_fail_cap() runs; once
    it does, the injected cap is visible on the edge, marked `"implicit": True` so it can be
    told apart from an author-written `@FAIL` guard (which never carries that key)."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["^new--go-->done^"]).validate().classify()

    g_before = spec.to_networkx()
    _, _, before = next(iter(g_before.edges(data=True)))
    assert before["fact_guards"] == []

    spec.apply_implicit_fail_cap()
    g_after = spec.to_networkx()
    _, _, after = next(iter(g_after.edges(data=True)))
    assert after["fact_guards"] == [{"name": "FAIL", "op": "<", "operand": 1, "implicit": True}]


# ---------------------------------------------------------------------------
# to_networkx(include_implied_caps=False): hide compiler-filled-in defaults
# (the implicit @FAIL<1 retry cap, and the default soft-timeout for
# un-annotated triggers) so the rendering shows only what the DSL declared.
# ---------------------------------------------------------------------------

def test_to_networkx_excludes_implicit_fail_cap_when_disabled():
    pytest.importorskip("networkx")
    spec = (
        StateChainParser.parse(["^new--go-->done^"])
        .validate().classify().apply_implicit_fail_cap()
    )

    g_default = spec.to_networkx()
    _, _, with_caps = next(iter(g_default.edges(data=True)))
    assert with_caps["fact_guards"] == [{"name": "FAIL", "op": "<", "operand": 1, "implicit": True}]

    g_stripped = spec.to_networkx(include_implied_caps=False)
    _, _, without_caps = next(iter(g_stripped.edges(data=True)))
    assert without_caps["fact_guards"] == []


def test_to_networkx_keeps_explicit_fail_guard_regardless_of_flag():
    """An author-written `@FAIL<3` (no `implicit` key) is never touched by the flag, even
    after apply_implicit_fail_cap() runs (which exempts edges with an explicit @FAIL)."""
    pytest.importorskip("networkx")
    spec = (
        StateChainParser.parse(["^new--@FAIL<3#go-->done^"])
        .validate().classify().apply_implicit_fail_cap()
    )

    for include in (True, False):
        g = spec.to_networkx(include_implied_caps=include)
        _, _, data = next(iter(g.edges(data=True)))
        assert data["fact_guards"] == [{"name": "FAIL", "op": "<", "operand": 3}]


def test_to_networkx_fills_default_soft_timeout_when_enabled():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["^new--go-->waiting--assign~20s-->done^"]).validate()

    g = spec.to_networkx()

    _, _, go_data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "go")
    assert go_data["soft_timeout_secs"] == DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS
    assert go_data["soft_timeout_is_explicit"] is False

    _, _, assign_data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "assign")
    assert assign_data["soft_timeout_secs"] == 20.0
    assert assign_data["soft_timeout_is_explicit"] is True


def test_to_networkx_omits_default_soft_timeout_when_disabled():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["^new--go-->waiting--assign~20s-->done^"]).validate()

    g = spec.to_networkx(include_implied_caps=False)

    _, _, go_data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "go")
    assert go_data["soft_timeout_secs"] is None
    assert go_data["soft_timeout_is_explicit"] is False

    # an EXPLICIT annotation is not "implied" -- it is never hidden by the flag
    _, _, assign_data = next(e for e in g.edges(data=True) if e[2]["trigger"] == "assign")
    assert assign_data["soft_timeout_secs"] == 20.0
    assert assign_data["soft_timeout_is_explicit"] is True


def test_to_networkx_include_implied_caps_applies_to_pending_wildcard_edges_too():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["*--go-->done^"])

    g_default = spec.to_networkx()
    _, _, with_default = next(iter(g_default.edges(data=True)))
    assert with_default["soft_timeout_secs"] == DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS
    assert with_default["soft_timeout_is_explicit"] is False

    g_stripped = spec.to_networkx(include_implied_caps=False)
    _, _, without_default = next(iter(g_stripped.edges(data=True)))
    assert without_default["soft_timeout_secs"] is None


# ---------------------------------------------------------------------------
# to_networkx(wildcard_pseudo_state=True): route wildcard-expanded fan-out
# through the "*" hub instead of drawing direct source -> dest edges
# ---------------------------------------------------------------------------

def test_to_networkx_default_draws_wildcard_expansion_as_direct_edges():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^new--go-->open==finish-->done^",
        "*==cancel-->cancelled^",
    ]).validate().expand_wildcards()

    g = spec.to_networkx()  # wildcard_pseudo_state=False by default

    direct = {(u, v) for u, v, d in g.edges(data=True) if d["trigger"] == "cancel" and d.get("wildcard_expanded")}
    assert direct == {("new", "cancelled"), ("open", "cancelled")}
    assert not g.has_edge("new", "*")
    assert not g.has_edge("open", "*")


def test_to_networkx_pseudo_state_routes_expanded_wildcard_edges_through_hub():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^new--go-->open==finish-->done^",
        "*==cancel-->cancelled^",
    ]).validate().expand_wildcards()

    g = spec.to_networkx(wildcard_pseudo_state=True)

    # no direct source -> cancelled edges any more for the wildcard trigger
    assert not g.has_edge("new", "cancelled")
    assert not g.has_edge("open", "cancelled")

    # each eligible source instead points at the hub, with the true destination preserved
    for source in ("new", "open"):
        _, _, data = next(e for e in g.out_edges(source, data=True) if e[2]["trigger"] == "cancel")
        assert data["wildcard_dest"] == "cancelled"
        assert data["wildcard_expanded"] is True

    # non-wildcard edges are completely unaffected
    assert g.has_edge("new", "open")
    assert g.has_edge("open", "done")

    # the hub fans out to the real destination -- deduplicated, not once per source
    hub_to_cancelled = [
        d for _, v, d in g.out_edges("*", data=True)
        if v == "cancelled" and d.get("wildcard_expanded")
    ]
    assert len(hub_to_cancelled) == 1

    # the abstract pending-rule edge is untouched by the flag, and coexists with the hub edge
    pending_to_cancelled = [
        d for _, v, d in g.out_edges("*", data=True)
        if v == "cancelled" and d.get("wildcard_pending")
    ]
    assert len(pending_to_cancelled) == 1


def test_to_networkx_pseudo_state_dedupes_hub_edges_by_guard_not_just_trigger():
    """Two sources funneling into the hub with the SAME trigger+dest but DIFFERENT guards
    must still produce two distinct hub -> dest edges -- guards are part of edge identity."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^a--x-->done^",
        "b--gated#x-->done^",
        "*==escape-->done^",
    ])
    # hand-craft two differently-guarded concrete "wildcard-expanded" edges sharing a
    # trigger+dest, the way expand_wildcards() would if two distinct wildcard chains both
    # targeted the same destination under the same trigger name but different guards.
    spec.transitions.append({
        "trigger": "escape", "source": "a", "dest": "done", "_wildcard": True,
    })
    spec.transitions.append({
        "trigger": "escape", "source": "b", "dest": "done", "_wildcard": True,
        "conditions": ["guard_gated"],
    })
    spec.auto_edges.add(("a", "escape"))
    spec.auto_edges.add(("b", "escape"))

    g = spec.to_networkx(wildcard_pseudo_state=True)

    hub_to_done = [d for _, v, d in g.out_edges("*", data=True) if v == "done" and d.get("wildcard_expanded")]
    assert len(hub_to_done) == 2
    assert {tuple(d["conditions"]) for d in hub_to_done} == {(), ("guard_gated",)}


def test_to_networkx_graph_level_metadata():
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(
        ["^new--go-->open==finish-->done^"]
    ).validate()

    g = spec.to_networkx()

    assert g.graph["initial_state"] == "new"
    assert g.graph["initial_states"] == {"new"}
    assert g.graph["terminal_states"] == {"done"}
    assert g.graph["states_order"] == ["new", "open", "done"]
    assert g.graph["triggers"] == ["go", "finish"]
    assert g.graph["pipeline"] == ["go"]
    assert g.graph["primary_chain"] == "^new--go-->open==finish-->done^"


# ---------------------------------------------------------------------------
# to_networkx() declaration_index: preserve advance() attempt order
# ---------------------------------------------------------------------------


def test_to_networkx_declaration_index_restores_auto_attempt_order():
    """MultiDiGraph groups out-edges by destination, so raw iteration can scramble
    declaration order when destinations interleave. Sorting by declaration_index must
    reproduce auto_edges_from() — the order advance() actually tries."""
    pytest.importorskip("networkx")
    # Destinations B, C, B: NetworkX adjacency yields t1,t3,t2 (grouped by dest),
    # while declaration / advance order is t1,t2,t3.
    spec = StateChainParser.parse([
        "^fork--t1-->B^",
        "fork--t2-->C^",
        "fork--t3-->B^",
    ]).validate()

    g = spec.to_networkx()

    raw = [(d["trigger"], v) for _, v, d in g.out_edges("fork", data=True) if d["auto"]]
    expected = spec.auto_edges_from("fork")
    assert raw != list(expected), (
        "precondition: NetworkX out_edges order must differ from declaration order "
        "for this interleaved-destination fixture"
    )

    restored = [
        (d["trigger"], v)
        for _, v, d in sorted(
            ((u, v, d) for u, v, d in g.out_edges("fork", data=True)
             if d["auto"] and d["declaration_index"] is not None),
            key=lambda e: e[2]["declaration_index"],
        )
    ]
    assert restored == list(expected)


def test_to_networkx_wildcard_expanded_edges_sort_after_explicit():
    """expand_wildcards() appends concrete edges, so their declaration_index must be
    greater than every explicit transition's index — matching runtime attempt order."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^new--go-->open==finish-->done^",
        "*--timeout-->expired^",
    ]).validate().expand_wildcards()

    g = spec.to_networkx()

    explicit_idxs = [
        d["declaration_index"]
        for _, _, d in g.edges(data=True)
        if not d.get("wildcard_expanded") and not d.get("wildcard_pending")
    ]
    expanded_idxs = [
        d["declaration_index"]
        for _, _, d in g.edges(data=True)
        if d.get("wildcard_expanded")
    ]
    assert explicit_idxs and expanded_idxs
    assert all(isinstance(i, int) for i in explicit_idxs + expanded_idxs)
    assert min(expanded_idxs) > max(explicit_idxs)


def test_to_networkx_hub_spokes_keep_index_artifacts_are_none():
    """In hub mode, source->* spokes are the fireable edges and keep their index;
    deduplicated *->dest hub edges and abstract pending-rule edges get None."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse([
        "^new--go-->open==finish-->done^",
        "*--timeout-->expired^",
    ]).validate().expand_wildcards()

    g = spec.to_networkx(wildcard_pseudo_state=True)

    spokes = [
        d for u, v, d in g.edges(data=True)
        if v == "*" and d.get("wildcard_expanded")
    ]
    assert spokes
    assert all(isinstance(d["declaration_index"], int) for d in spokes)

    hub_to_dest = [
        d for u, v, d in g.out_edges("*", data=True)
        if d.get("wildcard_expanded") and not d.get("wildcard_pending")
    ]
    assert hub_to_dest
    assert all(d["declaration_index"] is None for d in hub_to_dest)

    pending = [d for _, _, d in g.edges(data=True) if d.get("wildcard_pending")]
    assert pending
    assert all(d["declaration_index"] is None for d in pending)


def test_to_networkx_multi_source_transition_shares_declaration_index():
    """A hand-built multi-source transition dict fans out to one edge per source that
    all share the same transitions-list index; within each source the index is unique."""
    pytest.importorskip("networkx")
    spec = StateChainParser.parse(["^a--x-->done^", "^b--y-->done^"]).validate()
    # Replace the two single-source edges with one multi-source dict at a known index.
    shared = {"trigger": "shared", "source": ["a", "b"], "dest": "done"}
    spec.transitions = [shared]
    spec.auto_edges = {("a", "shared"), ("b", "shared")}
    spec.triggers = ["shared"]
    spec.pipeline = ["shared"]

    g = spec.to_networkx()

    a_edge = next(d for _, _, d in g.out_edges("a", data=True) if d["trigger"] == "shared")
    b_edge = next(d for _, _, d in g.out_edges("b", data=True) if d["trigger"] == "shared")
    assert a_edge["declaration_index"] == b_edge["declaration_index"] == 0
    assert [d["declaration_index"] for _, _, d in g.out_edges("a", data=True)] == [0]
    assert [d["declaration_index"] for _, _, d in g.out_edges("b", data=True)] == [0]


# ---------------------------------------------------------------------------
# Same-state (self-loop) edges
# ---------------------------------------------------------------------------


def test_unguarded_auto_self_loop_is_rejected():
    with pytest.raises(FsmChainParseError) as excinfo:
        StateChainParser.parse(["^a--tick-->a--go-->done^"]).validate()
    msg = str(excinfo.value)
    assert "auto self-loop" in msg
    assert "tick" in msg
    assert "method guard" in msg
    assert "still_needed" in msg
    assert "@DWELL" in msg or "@FAIL" in msg


def test_method_guarded_auto_self_loop_is_accepted():
    spec = StateChainParser.parse(["^a--ok#tick-->a--go-->done^"]).validate()
    tick = next(t for t in spec.transitions if t["trigger"] == "tick")
    assert tick["source"] == "a" and tick["dest"] == "a"
    assert tick["conditions"] == ["guard_ok"]
    assert ("a", "tick") in spec.auto_edges


def test_unguarded_manual_self_loop_is_accepted():
    spec = StateChainParser.parse(["^a==tick-->a--go-->done^"]).validate()
    tick = next(t for t in spec.transitions if t["trigger"] == "tick")
    assert tick["source"] == "a" and tick["dest"] == "a"
    assert not tick.get("conditions")
    assert ("a", "tick") not in spec.auto_edges


def test_fact_only_auto_self_loop_is_rejected():
    with pytest.raises(FsmChainParseError) as excinfo:
        StateChainParser.parse(["^a--@DWELL>1s#tick-->a--go-->done^"]).validate()
    msg = str(excinfo.value)
    assert "auto self-loop" in msg
    assert "method guard" in msg
