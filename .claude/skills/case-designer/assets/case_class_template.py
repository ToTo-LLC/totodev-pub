# WORKED EXAMPLE — fictitious "permits" domain. Imitate this file's SHAPE only
# (docstring roles, ClassVar trust map, stub style). Do not copy the permit
# story into a real case. Real module docstrings are written from the interview.
# Stub convention: references/dsl_and_hooks.md. Output examples:
# references/generated_output_examples.md.

"""Permit application lifecycle for a municipal licensing desk.

A resident or business submits materials for a permit; staff validate the
packet, check eligibility rules, and either issue the permit or deny /
abandon the request. This case type is the recurring unit of work for one
application from intake through a terminal outcome.

IMPORTANT: This module is a rough first draft. Expect substantial follow-on work
before production use — refining data structures, implementing trigger/guard/
assertion bodies, tuning ``fsm_state_chains``, and hardening asset contracts.
See ``fsm_state_chains`` on the case class for the lifecycle declaration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel
from transitions.core import EventData

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry

# ---------------------------------------------------------------------------
# Data contracts — one FileMappedPydanticMixin (or Path) per asset alias.
# ---------------------------------------------------------------------------


class ApplicationData(BaseModel, FileMappedPydanticMixin):
    """Incoming application packet for this permit request.

    TODO: replace this placeholder attribute layout with the real application
    fields gathered in the interview.
    """

    applicant_name: str = ""


class EligibilityAssessment(BaseModel, FileMappedPydanticMixin):
    """Outcome of the eligibility check for this application.

    TODO: replace this placeholder attribute layout with the real assessment
    fields the downstream edges and assertions need.
    """

    is_eligible: bool = False


@case_type_registry.register
class PermitApplicationCase(FolderBackedCase):
    """One permit application, from submission to issue/denial/abandonment."""

    # =======================================================================
    # Declarations
    # =======================================================================

    # States in which each asset alias is considered trustworthy / complete.
    asset_trust_states: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType({
        "application": frozenset({
            "submitted", "validated", "screened", "awaiting_review",
            "approved", "issued", "denied",
        }),
        "eligibility": frozenset({
            "screened", "awaiting_review", "approved", "issued",
        }),
        "supporting_docs": frozenset({
            "attachments_added", "submitted", "validated", "screened",
            "awaiting_review",
        }),
    })

    fsm_state_chains = """
        %% Intake: inert initial state, manual attach (kwargs paths), then the flow.
        [*] --> new == add_attachments ==> attachments_added -- begin --> submitted
        %% Validation with retry-then-divert (a @FAIL pair on separate edges).
        submitted -- validate_documents~2m [@FAIL<3] --> validated
        submitted -- flag_incomplete [@FAIL>=3] --> needs_attention
        validated -- check_eligibility~1m --> screened
        screened -- route_to_reviewer [eligible] --> awaiting_review
        %% Human decision gates are manual (==); a dwell escape guards the wait.
        awaiting_review == approve ==> approved -- issue_permit~30s --> issued --> [*]
        awaiting_review == deny ==> denied --> [*]
        awaiting_review -- escalate [@DWELL>5d] --> needs_attention
        needs_attention == reassign ==> awaiting_review
        needs_attention == abandon ==> abandoned --> [*]
        %% Cross-cutting escape: bare * = wildcard source ("from any live state").
        * == cancel ==> cancelled --> [*]
    """

    asset_aliases = {
        "application": AssetSpec(
            relative_path="application.yaml",
            loader=ApplicationData,
            states=asset_trust_states["application"],
            keep=True,
        ),
        "eligibility": AssetSpec(
            relative_path="eligibility.yaml",
            loader=EligibilityAssessment,
            states=asset_trust_states["eligibility"],
            keep=True,
        ),
        "supporting_docs": AssetSpec(
            relative_path="documents/*",
            loader=Path,
            states=asset_trust_states["supporting_docs"],
            many=True,
        ),
    }

    fsm_trigger_chokes = {
        # Name a resource only for triggers whose perform_ contends for a
        # capacity-constrained shared dependency; leave the rest unlisted.
        "validate_documents": {"cpu"},
        "check_eligibility": {"llm"},
        "issue_permit": {"api"},
    }

    def _not_implemented(self, retval: Any = None) -> Any:
        """Scaffolding helper for stub hooks; remove with the stubs that call it."""
        caller = sys._getframe(1).f_code.co_qualname
        self.log.warning("STUB not implemented: %s", caller)
        return retval

    # =======================================================================
    # perform_<trigger>
    # =======================================================================

    async def perform_add_attachments(self, tctx: EventData) -> None:
        """TODO(responsibility): copy/link filepath(s) from tctx.kwargs into
        the `supporting_docs` asset location. Intake only — no OCR/parse here.
        """
        return self._not_implemented(None)

    async def perform_begin(self, tctx: EventData) -> None:
        """TODO(responsibility): any bookkeeping needed when leaving the
        temporary intake state and entering the real flow at `submitted`.
        """
        return self._not_implemented(None)

    async def perform_validate_documents(self, tctx: EventData) -> None:
        """TODO(responsibility): validate the uploaded documents named in
        `supporting_docs`; write findings the retry/divert edges above key on.
        Must raise on a validation failure to count toward @FAIL.
        """
        return self._not_implemented(None)

    async def perform_flag_incomplete(self, tctx: EventData) -> None:
        """TODO(responsibility): after 3 failed validation attempts, record
        why (e.g. `case_emit_alert_event`) so a human knows what's missing.
        """
        return self._not_implemented(None)

    async def perform_check_eligibility(self, tctx: EventData) -> None:
        """TODO(responsibility): compute eligibility from `application` and
        write the result to the `eligibility` asset.
        """
        return self._not_implemented(None)

    async def perform_route_to_reviewer(self, tctx: EventData) -> None:
        """TODO(responsibility): whatever bookkeeping/notification is needed
        when handing an eligible application to a human reviewer.
        """
        return self._not_implemented(None)

    async def perform_issue_permit(self, tctx: EventData) -> None:
        """TODO(responsibility): call the external permit-issuing system;
        write its confirmation/identifier into an asset before returning.
        """
        return self._not_implemented(None)

    async def perform_escalate(self, tctx: EventData) -> None:
        """TODO(responsibility): the review sat for 5+ days — alert whoever
        owns the reviewer queue (e.g. `case_emit_alert_event`).
        """
        return self._not_implemented(None)

    # =======================================================================
    # guard_<guard>
    # =======================================================================

    async def guard_eligible(self, tctx: EventData) -> bool:
        """TODO(responsibility): return True iff `eligibility.is_eligible`.
        Must not mutate anything; may be polled repeatedly.

        Stubbed default is True so a simulated run can walk past this edge;
        pass False instead if this guard should block advancement until real
        logic is in place.
        """
        return self._not_implemented(True)

    # =======================================================================
    # on_enter_/on_exit_<state> — only for states with real entry/exit work.
    # =======================================================================

    async def on_enter_awaiting_review(self, tctx: EventData) -> None:
        """TODO(responsibility): notify the reviewer queue that a new
        application is ready for a decision.
        """
        return self._not_implemented(None)

    # =======================================================================
    # case_assert_<state>_<slug>
    # =======================================================================

    def case_assert_new_is_empty(self, ltx) -> None | str:
        """TODO(responsibility): inert parking state — no supporting_docs yet."""
        return self._not_implemented(None)

    def case_assert_attachments_added_has_docs(self, ltx) -> None | str:
        """TODO(responsibility): at least one file must match `supporting_docs`
        after `add_attachments`."""
        return self._not_implemented(None)

    def case_assert_submitted_has_application(self, ltx) -> None | str:
        """TODO(responsibility): the `application` asset must exist and be
        loadable the moment a case enters `submitted`."""
        return self._not_implemented(None)

    def case_assert_validated_docs_present(self, ltx) -> None | str:
        """TODO(responsibility): at least one file must match `supporting_docs`."""
        return self._not_implemented(None)

    def case_assert_screened_has_eligibility(self, ltx) -> None | str:
        """TODO(responsibility): the `eligibility` asset must exist and be
        internally consistent (e.g. a reason is set when not eligible)."""
        return self._not_implemented(None)

    def case_assert_awaiting_review_is_eligible(self, ltx) -> None | str:
        """TODO(responsibility): only eligible applications should ever reach
        this state — assert `eligibility.is_eligible` is True."""
        return self._not_implemented(None)

    def case_assert_issued_has_permit_id(self, ltx) -> None | str:
        """TODO(responsibility): a permit identifier must be recorded before
        the case is allowed to terminate in `issued`."""
        return self._not_implemented(None)

    # TODO: add case_assert_* for denied / needs_attention / abandoned /
    # cancelled if those states have an expected shape worth checking; a
    # state with nothing to assert can legitimately have none.

    def on_terminating(self) -> None:
        """TODO(responsibility): name any deliverables worth keeping past
        purge that weren't already declared `keep=True` above, via
        `self.case_keep_files(...)`. Delete this method if declared
        AssetSpec.keep flags already cover everything.
        """
        return self._not_implemented(None)
