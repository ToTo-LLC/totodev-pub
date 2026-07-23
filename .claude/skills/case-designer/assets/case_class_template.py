# WORKED EXAMPLE — not a real case type.
#
# This is a fictitious "PermitApplicationCase" showing the SHAPE a generated
# skeleton should have: every stub's body is one deletable line —
# `return self._not_implemented(<default>)` — which logs a WARNING (naming
# the caller via sys._getframe) instead of raising and returns the stubbed-in
# default, so the lifecycle can be driven/simulated before anything is filled
# in. Replacing a stub with a real implementation means deleting that one
# line and writing the body. Every stub's docstring states its RESPONSIBILITY
# (what to read/write/decide) rather than implementing it, and every
# state/trigger/guard named in fsm_state_chains gets exactly the hooks it
# needs — no more, no less. Copy this shape; do not copy the domain (permits).
# Swap in the real case name, states, triggers, assets, and responsibility
# text gathered from the developer's interview answers.
#
# Lifecycle: see the fsm_state_chains declaration below — with three or more
# chains, prefer the triple-quoted multiline form (one chain per line, `%%`
# comments), which reads as its own diagram. Grammar is in
# references/dsl_and_hooks.md. Default intake shape (SKILL.md Step 2 /
# pattern 0): inert `new` entered from the [*] boundary, plus a manual
# add_attachments trigger carrying filepath(s) in kwargs — not file copy
# inside create_case_in_folder.

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry

# ---------------------------------------------------------------------------
# Data contracts — one FileMappedPydanticMixin per asset_aliases entry below.
# Fields are placeholders: declare the shape the developer described, nothing
# more. These are declarations, not logic, so they get no _not_implemented()
# call — there is no behavior here to defer.
# ---------------------------------------------------------------------------

class ApplicationData(BaseModel, FileMappedPydanticMixin):
    """TODO: fields describing the incoming application itself."""
    applicant_name: str = ""
    # TODO: remaining fields from the developer's data-contract answers


class EligibilityAssessment(BaseModel, FileMappedPydanticMixin):
    """TODO: fields describing the outcome of check_eligibility."""
    is_eligible: bool = False
    # TODO: remaining fields


NEEDS_APPLICATION = {
    "submitted", "validated", "screened", "awaiting_review",
    "approved", "issued", "denied",
}
NEEDS_ELIGIBILITY = {"screened", "awaiting_review", "approved", "issued"}
# supporting_docs become trustworthy once intake has run (not in inert `new`)
NEEDS_SUPPORTING_DOCS = {
    "attachments_added", "submitted", "validated", "screened", "awaiting_review",
}


@case_type_registry.register
class PermitApplicationCase(FolderBackedCase):
    """One permit application, from submission to issue/denial/abandonment.

    TODO: replace with the real one-paragraph description of the recurring
    unit of work this case type models (the answer to interview step 1).
    """

    # =======================================================================
    # Declarations — see references/dsl_and_hooks.md for the DSL grammar and
    # AssetSpec field meanings.
    # =======================================================================

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

    asset_aliases = [
        AssetSpec(alias="application", relative_path="application.yaml",
                  loader=ApplicationData, states=NEEDS_APPLICATION, keep=True),
        AssetSpec(alias="eligibility", relative_path="eligibility.yaml",
                  loader=EligibilityAssessment, states=NEEDS_ELIGIBILITY, keep=True),
        AssetSpec(alias="supporting_docs", relative_path="documents/*",
                  loader=Path, states=NEEDS_SUPPORTING_DOCS, many=True),
    ]

    fsm_trigger_chokes = {
        # TODO: name a resource only for triggers whose perform_ work actually
        # contends for a capacity-constrained shared dependency; leave the rest
        # unlisted (many cases need none). Prefer one resource per trigger --
        # chokes are semaphores, so stacking several onto one trigger slows the
        # lifecycle. See references/dsl_and_hooks.md on trigger chokes.
        "validate_documents": {"cpu"},   # local OCR / vector embedding
        "check_eligibility": {"llm"},
        "issue_permit": {"api"},         # bandwidth-limited external API
    }

    # =======================================================================
    # Skeleton scaffolding — DELETE this method once every stub below has a
    # real implementation and no longer calls it. It exists only so the
    # lifecycle can be driven/simulated (case_advance() in a test, say)
    # before any hook is filled in: instead of a hard NotImplementedError
    # crash, it logs a WARNING to this case's own log (naming the caller via
    # sys._getframe so stubs need not hardcode their method name) and returns
    # the caller's stubbed-in default (`retval`). Each stub's entire body is
    # one `return self._not_implemented(...)` line, so implementing a method
    # for real means deleting that single line and writing the body.
    # =======================================================================

    def _not_implemented(self, retval: Any = None) -> Any:
        caller = sys._getframe(1).f_code.co_qualname
        self.log.warning("STUB not implemented: %s", caller)
        return retval

    # =======================================================================
    # perform_<trigger> — the work each automated/manual edge does.
    # =======================================================================

    async def perform_add_attachments(self, tctx) -> None:
        """TODO(responsibility): copy/link filepath(s) from tctx.kwargs into
        the `supporting_docs` asset location. Intake only — no OCR/parse here.
        """
        return self._not_implemented(None)

    async def perform_begin(self, tctx) -> None:
        """TODO(responsibility): any bookkeeping needed when leaving the
        temporary intake state and entering the real flow at `submitted`.
        """
        return self._not_implemented(None)

    async def perform_validate_documents(self, tctx) -> None:
        """TODO(responsibility): validate the uploaded documents named in
        `supporting_docs`; write findings the retry/divert edges above key on.
        Must raise on a validation failure to count toward @FAIL.
        """
        return self._not_implemented(None)

    async def perform_flag_incomplete(self, tctx) -> None:
        """TODO(responsibility): after 3 failed validation attempts, record
        why (e.g. `case_emit_alert_event`) so a human knows what's missing.
        """
        return self._not_implemented(None)

    async def perform_check_eligibility(self, tctx) -> None:
        """TODO(responsibility): compute eligibility from `application` and
        write the result to the `eligibility` asset.
        """
        return self._not_implemented(None)

    async def perform_route_to_reviewer(self, tctx) -> None:
        """TODO(responsibility): whatever bookkeeping/notification is needed
        when handing an eligible application to a human reviewer.
        """
        return self._not_implemented(None)

    async def perform_issue_permit(self, tctx) -> None:
        """TODO(responsibility): call the external permit-issuing system;
        write its confirmation/identifier into an asset before returning.
        """
        return self._not_implemented(None)

    async def perform_escalate(self, tctx) -> None:
        """TODO(responsibility): the review sat for 5+ days — alert whoever
        owns the reviewer queue (e.g. `case_emit_alert_event`).
        """
        return self._not_implemented(None)

    # =======================================================================
    # guard_<guard> — fast, idempotent, side-effect-free data conditions.
    # =======================================================================

    async def guard_eligible(self, tctx) -> bool:
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

    async def on_enter_awaiting_review(self, tctx) -> None:
        """TODO(responsibility): notify the reviewer queue that a new
        application is ready for a decision.
        """
        return self._not_implemented(None)

    # =======================================================================
    # case_assert_<state>_<slug> — one per state with an expected shape.
    # See references/dsl_and_hooks.md: falsy = pass, message string = fail.
    # =======================================================================

    def case_assert_new_is_empty(self, ltx) -> None | str:
        """TODO(responsibility): inert parking state — no supporting_docs yet.
        Stubbed default is None (pass) rather than a failure message, so a
        known-unimplemented check doesn't spam CASE_ASSERT_FAILED events —
        the WARNING in the case log is the visible signal instead.
        """
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

    # =======================================================================
    # Termination — runtime-judged retention (declare always-keep assets via
    # AssetSpec(keep=True) instead; use this only for a case-by-case decision).
    # =======================================================================

    def on_terminating(self) -> None:
        """TODO(responsibility): name any deliverables worth keeping past
        purge that weren't already declared `keep=True` above, via
        `self.case_keep_files(...)`. Delete this method (and the call below)
        if declared AssetSpec.keep flags already cover everything.
        """
        return self._not_implemented(None)
