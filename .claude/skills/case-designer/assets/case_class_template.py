# WORKED EXAMPLE — not a real case type.
#
# This is a fictitious "PermitApplicationCase" showing the SHAPE a generated
# skeleton should have: every stub calls self._not_implemented(...) (logs a
# WARNING instead of raising, so the lifecycle can be driven/simulated before
# anything is filled in), every stub's docstring states its RESPONSIBILITY
# (what to read/write/decide) rather than implementing it, and every
# state/trigger/guard named in fsm_state_chains gets exactly the hooks it
# needs — no more, no less. Copy this shape; do not copy the domain (permits).
# Swap in the real case name, states, triggers, assets, and responsibility
# text gathered from the developer's interview answers.
#
# Lifecycle drawn (see references/dsl_and_hooks.md for the DSL grammar):
#
#   ^submitted --@FAIL<3#validate_documents~2m--> validated
#   submitted --@FAIL>=3#flag_incomplete--> needs_attention
#   validated --check_eligibility~1m--> screened
#   screened --eligible#route_to_reviewer--> awaiting_review
#   awaiting_review ==approve--> approved --issue_permit~30s--> issued^
#   awaiting_review ==deny--> denied^
#   awaiting_review --@DWELL>5d#escalate--> needs_attention
#   needs_attention ==reassign--> awaiting_review
#   needs_attention ==abandon--> abandoned^
#   *==cancel--> cancelled^

from __future__ import annotations

from pathlib import Path

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

    fsm_state_chains = [
        "^submitted--@FAIL<3#validate_documents~2m-->validated",
        "submitted--@FAIL>=3#flag_incomplete-->needs_attention",
        "validated--check_eligibility~1m-->screened",
        "screened--eligible#route_to_reviewer-->awaiting_review",
        "awaiting_review==approve-->approved--issue_permit~30s-->issued^",
        "awaiting_review==deny-->denied^",
        "awaiting_review--@DWELL>5d#escalate-->needs_attention",
        "needs_attention==reassign-->awaiting_review",
        "needs_attention==abandon-->abandoned^",
        "*==cancel-->cancelled^",
    ]

    asset_aliases = [
        AssetSpec(alias="application", relative_path="application.yaml",
                  loader=ApplicationData, states=NEEDS_APPLICATION, keep=True),
        AssetSpec(alias="eligibility", relative_path="eligibility.yaml",
                  loader=EligibilityAssessment, states=NEEDS_ELIGIBILITY, keep=True),
        AssetSpec(alias="supporting_docs", relative_path="documents/*",
                  loader=Path, states={"validated", "screened", "awaiting_review"},
                  many=True),
    ]

    fsm_trigger_chokes = {
        # TODO: name a resource only for triggers that actually draw on a
        # capacity-constrained dependency; leave the others unlisted.
        "validate_documents": {"cpu"},
        "check_eligibility": {"llm"},
        "issue_permit": {"external_api"},
    }

    # =======================================================================
    # Skeleton scaffolding — DELETE this method once every stub below has a
    # real implementation and no longer calls it. It exists only so the
    # lifecycle can be driven/simulated (case_advance() in a test, say)
    # before any hook is filled in: instead of a hard NotImplementedError
    # crash, it logs a WARNING to this case's own log and lets execution
    # continue with the caller's stubbed-in default return value.
    # =======================================================================

    def _not_implemented(self, method_name: str) -> None:
        self.log.warning("STUB not implemented: %s.%s", type(self).__name__, method_name)

    # =======================================================================
    # perform_<trigger> — the work each automated/manual edge does.
    # =======================================================================

    async def perform_validate_documents(self, tctx) -> None:
        """TODO(responsibility): validate the uploaded documents named in
        `supporting_docs`; write findings the retry/divert edges above key on.
        Must raise on a validation failure to count toward @FAIL.
        """
        self._not_implemented("perform_validate_documents")

    async def perform_flag_incomplete(self, tctx) -> None:
        """TODO(responsibility): after 3 failed validation attempts, record
        why (e.g. `case_emit_alert_event`) so a human knows what's missing.
        """
        self._not_implemented("perform_flag_incomplete")

    async def perform_check_eligibility(self, tctx) -> None:
        """TODO(responsibility): compute eligibility from `application` and
        write the result to the `eligibility` asset.
        """
        self._not_implemented("perform_check_eligibility")

    async def perform_route_to_reviewer(self, tctx) -> None:
        """TODO(responsibility): whatever bookkeeping/notification is needed
        when handing an eligible application to a human reviewer.
        """
        self._not_implemented("perform_route_to_reviewer")

    async def perform_issue_permit(self, tctx) -> None:
        """TODO(responsibility): call the external permit-issuing system;
        write its confirmation/identifier into an asset before returning.
        """
        self._not_implemented("perform_issue_permit")

    async def perform_escalate(self, tctx) -> None:
        """TODO(responsibility): the review sat for 5+ days — alert whoever
        owns the reviewer queue (e.g. `case_emit_alert_event`).
        """
        self._not_implemented("perform_escalate")

    # =======================================================================
    # guard_<guard> — fast, idempotent, side-effect-free data conditions.
    # =======================================================================

    async def guard_eligible(self, tctx) -> bool:
        """TODO(responsibility): return True iff `eligibility.is_eligible`.
        Must not mutate anything; may be polled repeatedly.

        Stubbed default is True so a simulated run can walk past this edge;
        flip to False if this guard should block advancement until real
        logic is in place.
        """
        self._not_implemented("guard_eligible")
        return True

    # =======================================================================
    # on_enter_/on_exit_<state> — only for states with real entry/exit work.
    # =======================================================================

    async def on_enter_awaiting_review(self, tctx) -> None:
        """TODO(responsibility): notify the reviewer queue that a new
        application is ready for a decision.
        """
        self._not_implemented("on_enter_awaiting_review")

    # =======================================================================
    # case_assert_<state>_<slug> — one per state with an expected shape.
    # See references/dsl_and_hooks.md: falsy = pass, message string = fail.
    # =======================================================================

    def case_assert_submitted_has_application(self, ltx) -> None | str:
        """TODO(responsibility): the `application` asset must exist and be
        loadable the moment a case enters `submitted`.

        Stubbed default is None (pass) rather than a failure message, so a
        known-unimplemented check doesn't spam CASE_ASSERT_FAILED events —
        the WARNING in the case log is the visible signal instead.
        """
        self._not_implemented("case_assert_submitted_has_application")
        return None

    def case_assert_validated_docs_present(self, ltx) -> None | str:
        """TODO(responsibility): at least one file must match `supporting_docs`."""
        self._not_implemented("case_assert_validated_docs_present")
        return None

    def case_assert_screened_has_eligibility(self, ltx) -> None | str:
        """TODO(responsibility): the `eligibility` asset must exist and be
        internally consistent (e.g. a reason is set when not eligible)."""
        self._not_implemented("case_assert_screened_has_eligibility")
        return None

    def case_assert_awaiting_review_is_eligible(self, ltx) -> None | str:
        """TODO(responsibility): only eligible applications should ever reach
        this state — assert `eligibility.is_eligible` is True."""
        self._not_implemented("case_assert_awaiting_review_is_eligible")
        return None

    def case_assert_issued_has_permit_id(self, ltx) -> None | str:
        """TODO(responsibility): a permit identifier must be recorded before
        the case is allowed to terminate in `issued`."""
        self._not_implemented("case_assert_issued_has_permit_id")
        return None

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
        self._not_implemented("on_terminating")
