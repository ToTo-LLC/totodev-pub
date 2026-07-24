# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the case briefing generator (case_doc mini-spec).

Purely class-level, like test_case_type_spec.py — no case folder is ever
created; `collect()`/`render_markdown()` only ever touch the class."""

from __future__ import annotations

import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support import case_doc
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_doc import (
    CaseDocOptions, collect, generate_case_docs, render_markdown, to_mermaid,
)


class SampleCase(FolderBackedCase):
    """One customer ticket, from intake to closure or expiry.

    Extra detail that should not appear in a first-paragraph render.
    """

    fsm_state_chains = """
        [*] --> new -- intake --> reviewing
        reviewing == approve [funded] ==> done --> [*]
        reviewing == fasttrack [funded] ==> done
        reviewing -- expire [@DWELL>=1h] --> expired --> [*]
        * == cancel ==> cancelled --> [*]
    """
    fsm_trigger_chokes = {"approve": {"finance-api"}}
    asset_aliases = {
    'ticket': AssetSpec(relative_path="ticket.yaml", loader=Path,
            states={"reviewing", "done"}, keep=True),
}

    async def perform_intake(self, tctx):
        """Pull the raw ticket payload into the case folder."""

    async def guard_funded(self, tctx) -> bool:
        """True once the linked invoice shows a cleared payment."""
        return True

    async def before_approve(self, tctx):
        """Snapshot the reviewer's decision before the transition commits."""

    async def perform_approve(self, tctx):
        """Stamp the ticket as approved and notify the customer."""

    async def perform_expire(self, tctx):
        """Archive the stale ticket once the review window lapses."""

    async def on_enter_reviewing(self, tctx):
        """Kick off the reviewer-assignment workflow."""

    def case_assert_done_totals_balance(self, ltx):
        """The closed ticket's recorded total matches the invoice."""
        return None

    def case_assert_reviewing_has_ticket(self, ltx):
        """The ticket asset exists before review starts."""
        return None

    def on_terminating(self):
        """Keep the ticket asset regardless of the declarative keep rule."""


class UndocumentedCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new -- go --> done --> [*]"]

    async def perform_go(self, tctx):
        pass


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------

def test_collect_is_class_level_only_no_folder_needed():
    # If this needed a live case, it would raise (no folder exists).
    doc = collect(SampleCase)
    assert doc.case_cls_name == "SampleCase"


def test_collect_class_doc_is_first_paragraph_by_default():
    doc = collect(SampleCase)
    assert doc.class_doc == "One customer ticket, from intake to closure or expiry."


def test_collect_class_doc_none_when_undocumented():
    doc = collect(UndocumentedCase)
    assert doc.class_doc is None


def test_collect_states_flags_and_hooks():
    doc = collect(SampleCase)
    by_name = {s.name: s for s in doc.states}
    assert by_name["new"].initial is True
    assert by_name["new"].default_initial is True
    assert by_name["reviewing"].terminal is False
    assert by_name["reviewing"].on_enter_doc == "Kick off the reviewer-assignment workflow."
    assert by_name["reviewing"].on_exit_doc is None
    # "reviewing" is the SOURCE of the pure-timed-escape edge (DWELL alone fires it);
    # the flag marks the state that can escape by waiting, not the destination.
    assert by_name["reviewing"].timed_escape is True
    assert by_name["done"].terminal is True
    assert by_name["expired"].terminal is True
    assert by_name["cancelled"].terminal is True


def test_collect_state_assertions_grouped_and_documented():
    doc = collect(SampleCase)
    by_name = {s.name: s for s in doc.states}
    assert by_name["done"].assertions == [
        ("totals_balance", "The closed ticket's recorded total matches the invoice."),
    ]
    assert by_name["reviewing"].assertions == [
        ("has_ticket", "The ticket asset exists before review starts."),
    ]
    assert by_name["new"].assertions == []


def test_collect_triggers_docs_and_chokes():
    doc = collect(SampleCase)
    by_name = {t.name: t for t in doc.triggers}

    intake = by_name["intake"]
    assert intake.perform_doc == "Pull the raw ticket payload into the case folder."
    assert {(e.source, e.dest, e.auto) for e in intake.edges} == {("new", "reviewing", True)}

    approve = by_name["approve"]
    assert approve.before_doc == "Snapshot the reviewer's decision before the transition commits."
    assert approve.perform_doc == "Stamp the ticket as approved and notify the customer."
    assert approve.chokes == frozenset({"finance-api"})
    assert approve.edges[0].guard_names == ["funded"]

    fasttrack = by_name["fasttrack"]
    assert fasttrack.perform_doc is None          # never defined -> None, not an error
    assert fasttrack.edges[0].guard_names == ["funded"]

    expire = by_name["expire"]
    assert expire.edges[0].fact_guards == [{"name": "DWELL", "op": ">=", "operand": 3600.0}]

    cancel = by_name["cancel"]
    assert any(e.source == "*" for e in cancel.edges) or any(
        e.wildcard_expanded for e in cancel.edges
    )


def test_collect_guards_cross_referenced_across_triggers():
    doc = collect(SampleCase)
    [funded] = [g for g in doc.guards if g.name == "funded"]
    assert funded.doc == "True once the linked invoice shows a cleared payment."
    assert funded.used_by_triggers == ["approve", "fasttrack"]


def test_collect_assets():
    doc = collect(SampleCase)
    [ticket] = doc.assets
    assert ticket.alias == "ticket"
    assert ticket.relative_path == "ticket.yaml"
    assert ticket.states == frozenset({"reviewing", "done"})
    assert ticket.keep is True
    assert ticket.many is False


def test_collect_overridden_hooks_only_lists_actual_overrides():
    doc = collect(SampleCase)
    names = {h.name for h in doc.overridden_hooks}
    assert names == {"on_terminating"}
    [hook] = [h for h in doc.overridden_hooks if h.name == "on_terminating"]
    assert hook.doc == "Keep the ticket asset regardless of the declarative keep rule."


def test_collect_overridden_hooks_empty_when_none_overridden():
    doc = collect(UndocumentedCase)
    assert doc.overridden_hooks == []


def test_docstring_mode_none_drops_all_docs():
    doc = collect(SampleCase, options=CaseDocOptions(docstring_mode="none"))
    assert doc.class_doc is None
    assert all(s.on_enter_doc is None for s in doc.states)
    assert all(t.perform_doc is None for t in doc.triggers)
    assert all(g.doc is None for g in doc.guards)


# ---------------------------------------------------------------------------
# to_mermaid()
# ---------------------------------------------------------------------------

def test_to_mermaid_state_style_default():
    """Standalone to_mermaid() still defaults to stateDiagram-v2 (DSL round-trip)."""
    doc = collect(SampleCase)
    mermaid = to_mermaid(doc.fsm_graph)
    assert mermaid.startswith("stateDiagram-v2")
    assert "[*] --> new" in mermaid
    assert "done --> [*]" in mermaid
    assert "approve [funded]" in mermaid


def test_to_mermaid_flowchart_style_with_wildcard_hub():
    doc = collect(SampleCase, options=CaseDocOptions(wildcard_pseudo_state=True))
    mermaid = to_mermaid(doc.fsm_graph, style="flowchart")
    assert mermaid.startswith("flowchart TD")
    assert case_doc._MERMAID_WILDCARD_ID in mermaid
    assert "classDef initialState" in mermaid


def test_to_mermaid_flowchart_distinguishes_auto_vs_manual_edges():
    """Same glyphs as the chain DSL: auto edges are plain '-->'; manual edges are the
    double-thick '==>'. Labels are the edge's DSL text, quoted (guard brackets are
    Mermaid shape syntax when bare); compiler-injected implicit @FAIL caps are not
    part of the author's declaration and stay out of the label. No leading '=='
    in the label — thickness carries that signal."""
    doc = collect(SampleCase)
    mermaid = to_mermaid(doc.fsm_graph, style="flowchart")
    assert 'new -->|"intake"| reviewing' in mermaid
    assert 'reviewing ==>|"approve [funded]"| done' in mermaid
    assert 'reviewing -->|"expire [@DWELL>=1h]"| expired' in mermaid
    assert "== approve" not in mermaid


def test_to_mermaid_omits_wildcard_fanout_by_default():
    """Abstract '* -> dest' stays; concrete per-state fan-out is hidden for clarity."""
    doc = collect(SampleCase)
    mermaid = to_mermaid(doc.fsm_graph, style="flowchart")
    assert case_doc._MERMAID_WILDCARD_ID in mermaid
    assert f'{case_doc._MERMAID_WILDCARD_ID} ==>|"cancel"| cancelled' in mermaid
    assert 'new ==>|"cancel"| cancelled' not in mermaid
    assert 'reviewing ==>|"cancel"| cancelled' not in mermaid


def test_to_mermaid_can_include_wildcard_fanout():
    doc = collect(SampleCase)
    mermaid = to_mermaid(
        doc.fsm_graph, style="flowchart", include_wildcard_expanded=True,
    )
    assert 'new ==>|"cancel"| cancelled' in mermaid
    assert f'{case_doc._MERMAID_WILDCARD_ID} ==>|"cancel"| cancelled' in mermaid


def test_render_markdown_default_diagram_hides_wildcard_fanout():
    text = generate_case_docs(SampleCase)
    assert f'{case_doc._MERMAID_WILDCARD_ID} ==>|"cancel"| cancelled' in text
    assert 'new ==>|"cancel"| cancelled' not in text
    # Triggers table still inventories the expanded edges.
    assert "| expanded |" in text or "|expanded|" in text.replace(" ", "")


# ---------------------------------------------------------------------------
# render_markdown() / generate_case_docs()
# ---------------------------------------------------------------------------

def test_render_markdown_starts_with_generation_stamp(monkeypatch):
    """Case briefings open with an HTML comment naming the generator, source, and stamp."""
    fixed = datetime(2026, 7, 22, 20, 16, 30, tzinfo=timezone.utc)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    # raising=False: attribute is added by the implementation under test.
    monkeypatch.setattr(case_doc, "datetime", _FakeDateTime, raising=False)

    text = generate_case_docs(SampleCase)
    first_line = text.splitlines()[0]
    assert first_line.startswith("<!-- Case briefing generated by ")
    assert first_line.endswith(" -->")
    assert "totodev_pub.folder_backed_case_support.case_doc" in first_line
    assert f"from {SampleCase.__module__}:{SampleCase.__name__}" in first_line
    assert "2026-07-22T20:16:30+00:00" in first_line
    assert any(line.startswith("# SampleCase") for line in text.splitlines()[1:])


def test_render_markdown_surfaces_bound_method_names():
    """DSL names stay as identity; bound methods appear with trailing ()."""
    text = generate_case_docs(SampleCase)

    # Triggers: DSL heading + perform_/before_ callables
    assert "### `approve`" in text
    assert "**Method:** `perform_approve()`" in text
    assert "**Before:** `before_approve()`" in text

    # Guards: DSL → method as a heading; docs stay out of a cramped table cell
    assert "### `funded` → `guard_funded()`" in text
    assert "**Used by:**" in text

    # Assertions: full case_assert_*() name
    assert "`case_assert_done_totals_balance()`" in text
    assert "`case_assert_reviewing_has_ticket()`" in text

    # State enter: method-only in the flags table; docstring in the hooks list
    assert "| reviewing |" in text
    assert "`on_enter_reviewing()`" in text
    assert "Kick off the reviewer-assignment workflow." in text
    # Not jammed into the On Enter cell as "method — docstring"
    assert "`on_enter_reviewing()` — Kick off" not in text

    # Overridden lifecycle hooks
    assert "### `on_terminating()`" in text


def test_render_markdown_asset_states_are_individually_backticked():
    text = generate_case_docs(SampleCase)
    # Wide CSV of bare names is harder to scan; each state is a code span.
    assert "`reviewing`, `done`" in text or "`done`, `reviewing`" in text


def test_render_markdown_contains_all_default_sections():
    text = generate_case_docs(SampleCase)
    assert "# SampleCase" in text
    assert text.lstrip().startswith("<!-- ")
    assert "```mermaid" in text
    assert "## States" in text
    assert "## Triggers" in text
    assert "## Guards" in text
    assert "## Assertions" in text
    assert "## Asset Aliases" in text
    assert "## Overridden Lifecycle Hooks" in text
    assert "per-case file assertions" in text        # the §6 footnote


def test_render_markdown_default_diagram_uses_flowchart_thick_manual_edges():
    """Docs default to flowchart so manual edges render thick (==>) without a
    leading '==' in the label (stateDiagram-v2 cannot draw thick transitions)."""
    text = generate_case_docs(SampleCase)
    assert "flowchart TD" in text
    assert 'reviewing ==>|"approve [funded]"| done' in text
    assert 'new -->|"intake"| reviewing' in text
    assert "== approve" not in text
    assert ':"== ' not in text



def test_render_markdown_sections_are_individually_toggleable():
    options = CaseDocOptions(
        include_diagram=False, include_states_table=False,
        include_triggers_table=False, include_guards_table=False,
        include_assertions=False, include_assets=False, include_hooks=False,
    )
    text = generate_case_docs(SampleCase, options=options)
    assert "```mermaid" not in text
    assert "## States" not in text
    assert "## Triggers" not in text
    assert "## Guards" not in text
    assert "## Assertions" not in text
    assert "## Asset Aliases" not in text
    assert "## Overridden Lifecycle Hooks" not in text
    assert "# SampleCase" in text                      # header always present


def test_render_markdown_omits_hooks_section_when_none_overridden():
    text = generate_case_docs(UndocumentedCase)
    assert "## Overridden Lifecycle Hooks" not in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_main_smoke(monkeypatch, capsys):
    fake_module = types.SimpleNamespace(SampleCase=SampleCase)
    monkeypatch.setattr(case_doc.importlib, "import_module", lambda name: fake_module)

    rc = case_doc.main(["fake_module:SampleCase", "--no-diagram"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "# SampleCase" in captured.out
    assert "```mermaid" not in captured.out


def test_cli_main_rejects_bad_target_syntax(capsys):
    with pytest.raises(SystemExit) as exc_info:
        case_doc.main(["no-colon-here"])
    assert exc_info.value.code == 2
    assert "must be" in capsys.readouterr().err


def test_cli_main_rejects_non_case_class(monkeypatch, capsys):
    fake_module = types.SimpleNamespace(NotACase=object)
    monkeypatch.setattr(case_doc.importlib, "import_module", lambda name: fake_module)

    with pytest.raises(SystemExit) as exc_info:
        case_doc.main(["fake_module:NotACase"])
    assert exc_info.value.code == 2
    assert "not a FolderBackedCase subclass" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Round-trip: the state-style diagram is itself valid fsm_state_chains DSL
# ---------------------------------------------------------------------------

def test_to_mermaid_state_marks_manual_edges_with_label_marker():
    """stateDiagram-v2 has no thick arrow, so manual edges carry the DSL's '=='
    label marker — keeping the line valid Mermaid AND valid chain DSL.
    Labels are always quoted so Mermaid-hostile guard characters render.
    Prefer flowchart for human-facing docs (see CaseDocOptions.diagram_style)."""
    doc = collect(SampleCase)
    mermaid = to_mermaid(doc.fsm_graph, style="state")
    assert 'reviewing --> done : "== approve [funded]"' in mermaid
    assert 'new --> reviewing : "intake"' in mermaid          # auto: plain label, quoted
    assert 'reviewing --> expired : "expire [@DWELL>=1h]"' in mermaid


def test_state_diagram_round_trips_through_the_parser():
    """to_mermaid(state) output parses back to the spec it rendered — the contract
    that keeps the diagram grammar and the chain grammar from drifting apart.
    Scope (see to_mermaid docstring): graph built with include_implied_caps=False,
    spec without wildcard chains."""
    from totodev_pub.folder_backed_case_support.state_chain_parser import (
        StateChainParser,
    )

    decl = """
        [*] --> new -- intake --> reviewing
        reviewing == approve [funded] ==> done --> [*]
        reviewing == fasttrack [funded] ==> done
        reviewing -- expire~90s [@DWELL>=1.5h] --> expired --> [*]
        reviewing --> parked : shelve [@FAIL<3, triaged]
        parked --> [*]
    """
    spec = StateChainParser.parse(decl).validate()
    mermaid = to_mermaid(spec.to_networkx(include_implied_caps=False), style="state")
    rt = StateChainParser.parse(mermaid).validate()

    def canon(s):
        def edge(t):
            facts = tuple(sorted(
                (fg["name"], fg["op"], fg["operand"]) for fg in t.get("_fact_guards", [])
            ))
            return (t["trigger"], t["source"], t["dest"],
                    tuple(t.get("conditions", [])), facts)
        return {
            "states": sorted(s.states),
            "transitions": sorted(edge(t) for t in s.transitions),
            "initial_states": sorted(s.initial_states),
            "initial_state": s.initial_state,
            "terminal_states": sorted(s.terminal_states),
            "auto_edges": sorted(s.auto_edges),
            "trigger_timeouts": s.trigger_timeouts,
        }

    assert canon(rt) == canon(spec)


# ---------------------------------------------------------------------------
# DWELL duration summary formatting (_fmt_dwell_duration)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secs, expected",
    [
        (14 * 86400, "14d"),
        (14 * 86400 + 20, "14d"),          # slightly over → still days
        (14 * 86400 - 20, "14d"),          # slightly under (bidirectional)
        (25 * 3600, "25h"),
        (90 * 60, "90m"),                  # not near a whole hour
        (1.5 * 86400, "36h"),              # half-day → hours, not days
        (59, "1m"),                        # > 0.95 min → nearest minute
        (56, "56s"),
        (3600, "1h"),
        (86400, "1d"),
    ],
)
def test_fmt_dwell_duration_summary_units(secs, expected):
    assert case_doc._fmt_dwell_duration(secs) == expected


def test_fmt_fact_guard_dwell_uses_summary_units():
    assert case_doc._fmt_fact_guard(
        {"name": "DWELL", "op": ">", "operand": 14 * 86400.0}
    ) == "@DWELL>14d"
    assert case_doc._fmt_fact_guard(
        {"name": "DWELL", "op": ">=", "operand": 3600.0}
    ) == "@DWELL>=1h"


def test_fmt_duration_soft_timeout_stays_exact():
    """Soft-timeout labels keep exact divisibility (round-trip), not summary rounding."""
    assert case_doc._fmt_duration(120) == "2m"
    assert case_doc._fmt_duration(90) == "90s"  # 90 does not divide cleanly into minutes
    assert case_doc._fmt_duration(14 * 86400 - 20) == f"{14 * 86400 - 20:g}s"
