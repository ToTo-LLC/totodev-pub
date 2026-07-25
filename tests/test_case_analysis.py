# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the shared FolderBackedCase analyzer (case_analysis).

Purely class-level, like test_case_briefing.py — no case folder is ever
created; analyze() only ever touches the class."""

from __future__ import annotations

from pathlib import Path

import pytest

# networkx is an optional dependency; analyze() goes through to_networkx(), so
# the module skips at collection time on a core-only install (same pattern as
# test_case_briefing.py).
pytest.importorskip("networkx")

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_analysis import (
    FolderBackedCaseAnalyzer,
    analyze,
)
from totodev_pub.folder_backed_case_support.perform_signature import (
    format_perform_param,
    format_perform_params,
)


class SampleCase(FolderBackedCase):
    """One customer ticket, from intake to closure or expiry.

    Extra detail that should not appear in a first-paragraph render.
    """

    fsm_state_chains = """
        [*] --> new -- intake --> reviewing
        reviewing == approve [funded] ==> done --> [*]
        reviewing == fasttrack [funded] ==> done
        reviewing -- recheck [funded] --> reviewing
        reviewing -- expire [@DWELL>=1h] --> expired --> [*]
        * == cancel ==> cancelled --> [*]
    """
    fsm_trigger_chokes = {"approve": {"finance-api"}}
    asset_aliases = {
        "ticket": AssetSpec(
            relative_path="ticket.yaml", loader=Path,
            trust_states={"reviewing", "done"}, keep=True,
        ),
    }

    async def perform_intake(self, tctx, *, source: str):
        """Pull the raw ticket payload into the case folder."""

    async def guard_funded(self, tctx) -> bool:
        """True once the linked invoice shows a cleared payment."""
        return True

    async def perform_approve(self, tctx, *, notify: bool = True):
        """Stamp the ticket as approved and notify the customer."""

    async def perform_recheck(self, tctx):
        """Re-evaluate funding before another auto advance attempt."""

    async def perform_expire(self, tctx):
        """Archive the stale ticket once the review window lapses."""


def _analysis():
    return analyze(SampleCase)


def test_states_and_initial_terminal():
    a = _analysis()
    names = {s.name for s in a.states}
    assert {"new", "reviewing", "done", "expired", "cancelled"} <= names
    assert a.initial_state == "new"
    assert a.terminal_states == frozenset({"done", "expired", "cancelled"})
    assert a.state("new").initial is True
    assert a.state("done").terminal is True


def test_manual_vs_auto_triggers():
    a = _analysis()
    manual = {t.name for t in a.manual_triggers}
    auto = {t.name for t in a.auto_triggers}
    assert manual == {"approve", "fasttrack", "cancel"}
    assert {"intake", "recheck", "expire"} <= auto
    # every trigger is classified into exactly one bucket
    assert manual.isdisjoint(auto)
    assert manual | auto == {t.name for t in a.triggers}


def test_structured_perform_params():
    a = _analysis()
    by_name = {t.name: t for t in a.triggers}

    # required, no default
    (source,) = by_name["intake"].perform_params
    assert source.name == "source"
    assert source.required is True
    assert source.has_default is False
    assert source.annotation_display == "str"

    # optional, with default
    (notify,) = by_name["approve"].perform_params
    assert notify.name == "notify"
    assert notify.required is False
    assert notify.has_default is True
    assert notify.default is True
    assert notify.annotation_display == "bool"

    # no perform_ params declared
    assert by_name["recheck"].perform_params == []


def test_guard_cross_reference():
    a = _analysis()
    funded = next(g for g in a.guards if g.name == "funded")
    assert set(funded.used_by_triggers) == {"approve", "fasttrack", "recheck"}
    assert funded.doc_raw and "cleared payment" in funded.doc_raw


def test_assets():
    a = _analysis()
    (ticket,) = a.assets
    assert ticket.alias == "ticket"
    assert ticket.relative_path == "ticket.yaml"
    assert ticket.keep is True
    assert ticket.trust_states == frozenset({"reviewing", "done"})


def test_raw_docstrings_untrimmed():
    # The analyzer carries the FULL docstring (multi-paragraph); renderers trim.
    a = _analysis()
    assert a.class_doc_raw is not None
    assert "Extra detail" in a.class_doc_raw  # second paragraph survives


def test_no_overridden_hooks_by_default():
    a = _analysis()
    assert a.overridden_hooks == []


def test_format_perform_params_matches_structured():
    # format_perform_params(fn) must equal formatting each structured param.
    fn = SampleCase.perform_approve
    from totodev_pub.folder_backed_case_support.perform_signature import (
        describe_perform_params,
    )
    assert format_perform_params(fn) == [
        format_perform_param(p) for p in describe_perform_params(fn)
    ]
    assert format_perform_params(fn) == ["notify: bool = True"]


def test_analyzer_class_wrapper_matches_free_function():
    a1 = analyze(SampleCase)
    a2 = FolderBackedCaseAnalyzer().analyze(SampleCase)
    assert {s.name for s in a1.states} == {s.name for s in a2.states}
    assert {t.name for t in a1.manual_triggers} == {t.name for t in a2.manual_triggers}
