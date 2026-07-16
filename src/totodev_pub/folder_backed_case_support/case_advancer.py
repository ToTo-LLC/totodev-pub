# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Private one-step advance orchestrator for FolderBackedCase.

Internal by convention (leading underscore on the class): not part of any public
surface and not re-exported from the support package. The public façade remains
``FolderBackedCase.case_advance()`` / ``case_advanceable``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.constants import EV_ALERTED
from totodev_pub.folder_backed_case_support.exceptions import (
    AutoAdvanceBlocked,
    CaseTransitionInFlightError,
    OwnershipLostError,
)

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase


class _CaseAdvancer:
    """Implements one reported case-advance attempt for a live FolderBackedCase.

    Ephemeral: construct per call (or per property read). Holds no per-attempt state
    of its own — alert collection and the initial-state snapshot live on the stack
    of ``advance()``. Shared FSM lifecycle callbacks (prepare/finalize/state-changed/
    exception) remain on the case so direct ``await case.<trigger>()`` shares them.
    """

    def __init__(self, case: "FolderBackedCase") -> None:
        self._case = case

    @property
    def advanceable(self) -> bool:
        """True when the CURRENT state has at least one auto-advanceable (`--`) exit, i.e.
        an unattended `case_advance()` could fire here (subject to guards). False for a
        terminal state or a state left only by MANUAL (`==`) edges.

        STRUCTURAL, not runtime: this reports whether an auto exit EXISTS, not whether a
        guard would currently permit it. A scheduler uses it to tell a genuinely manual-only
        state (no auto exits — accelerate demotion) apart from a state whose guards merely
        declined this pass (auto exits exist — normal cadence). See AutoAdvanceBlocked for
        the runtime "declined now and can't ripen" signal."""
        return self._case._fsm.has_auto_exits(self._case.case_state)

    async def advance(
        self,
        trigger: str | None = None,
        trigger_kwargs: dict | None = None,
    ) -> AdvanceResult:
        """Fire ONE forward step from the current state and report the outcome as an
        AdvanceResult (a NON-throwing reporter — see that class).

        Quick use:
          Call with NO arguments (the common case) and a driver loops it across cases to
          drive AUTO (`--`) edges: it tries each auto candidate in declared order, firing
          the first whose guard permits. Transition failures are REPORTED, not raised:
          inspect `result.progressed` and `result.exceptions` rather than wrapping in
          try/except.

          Pass `trigger=...` to pin ONE specific edge — and uniquely, this is how you fire
          a MANUAL (`==`) edge through the reporter instead of via `await case.<trigger>()`
          (which DOES raise). A pinned manual edge needs `trigger_kwargs` (see Args); the
          same AdvanceResult / non-throwing contract then covers both auto and manual.

          There is deliberately NO run_to_completion()/drive loop on the case itself: this
          method knows how to take ONE step; deciding WHICH case to drive, in what order,
          with what fairness/concurrency/backpressure, is a SCHEDULER concern that belongs
          to a driver layer (see CasePoolDriver), not to the domain object. A built-in
          single-case loop would quietly endorse a one-case-at-a-time fleet model, which is
          the opposite of the intended round-robin-over-many-cases deployment. Tests that
          genuinely want to run a single case to its end use the `drive_to_completion()`
          helper in the test utilities.

        Args:
            trigger: optional name of a single edge (auto OR manual) leaving the current
              state. When given, ONLY that trigger is attempted — every other edge is
              skipped, and the BLOCKED detection below is suppressed (pinning one edge can
              never prove the whole state is walled off). If the name is not a trigger on
              this FSM at all, that is misuse and RAISES ValueError. If it IS a real
              trigger but simply has no edge out of the current state, it is a plain
              non-advance (nothing fires; no exception).
            trigger_kwargs: the keyword arguments bundled into `tctx.kwargs` for the fired
              trigger's hooks. REQUIRED (even as `{}`) when `trigger` names a MANUAL (`==`)
              edge — firing one without it is misuse and RAISES ValueError; the explicit
              bag is the deliberate "yes, fire this manual edge" acknowledgement. OPTIONAL
              for AUTO edges and for the no-argument sweep (an auto edge may still accept a
              bag if you choose to pass one; omitted means an empty `tctx.kwargs`). Passing
              it with `trigger=None` is meaningless and RAISES ValueError.

        Returns:
            AdvanceResult carrying the FLOW outcome as data — progressed, a failed attempt
            (the exception CAUGHT and folded into `exceptions`, the case left in its source
            state to retry), nothing to do, or BLOCKED. See AdvanceResult for the full
            taxonomy and its `progressed`/`failed`/`blocked` properties. Two wrinkles:
              * A pinned MANUAL edge routed through here also folds its failure as data
                (a direct `await case.<trigger>()` still raises — the other channel).
              * BLOCKED (a synthetic AutoAdvanceBlocked in `exceptions`; see that class) is
                detected only on the NO-ARGUMENT sweep — pinning one edge can never prove
                the whole state is walled off.

        Raises:
            Misuse guards and fatal invariants only — never flow outcomes:
            DetachedCaseError: mutating a detached husk.
            ValueError: unknown trigger name; a MANUAL edge without trigger_kwargs;
              trigger_kwargs without a trigger (see Args).
            OwnershipLostError: a lease beat (the pre-step one below, or the in-flight
              keepalive) found the folder reclaimed by another owner — a fatal invariant
              breach, NOT folded into the result.
            CaseTransitionInFlightError: another trigger call is ALREADY in flight on this
              same object; also NOT folded. This method is deliberately NON-REENTRANT — at
              most one FSM trigger invocation per live case, enforced fail-fast via
              _on_prepare_fsm_event (a direct `await case.<trigger>()` is guarded the same
              way). See that exception's docstring for who typically hits this and how
              CasePoolDriver.fire() serializes around it.

        Stall handling has NO self-pulse (a case cannot watchdog its own case_advance()
        from inside a single suspended coroutine): a stalled external job rides a
        `@DWELL>...` timed-escape edge that ripens and fires here; a genuinely stuck state
        surfaces as BLOCKED; a hung case_advance() call itself is the DRIVER's concern
        (e.g. asyncio.wait_for). Fuller rationale: notebooks/DEVDAVE/case_manager_classes/
        _backlog/finishing_watchdog.md.

        Hooks must be well-behaved async (yield the loop; offload blocking work) or the
        lease keepalive cannot protect them — see "Creating Hook Methods" in the class
        docstring for the contract."""
        case = self._case
        if trigger is None and trigger_kwargs is not None:
            raise ValueError(
                "case_advance(): trigger_kwargs was supplied without a trigger; kwargs have "
                "no edge to flow into. Name the trigger to fire, or drop trigger_kwargs."
            )
        initial = case.case_state
        if case.case_is_terminal:
            return AdvanceResult(initial, case.case_state)
        # Peek _transition_in_flight before _advance_collecting_alerts installs its alert
        # override — a rejected overlapping call would restore in finally and clobber the
        # in-flight call's override. The flag is set only in _on_prepare_fsm_event.
        if case._transition_in_flight:
            raise CaseTransitionInFlightError(
                case.case_id, case._folder, case._active_trigger_name
            )
        case._check_active()
        case.case_heartbeat()  # pre-step beat for long-dwelling no-op polls; see docstring
        return await self._advance_collecting_alerts(initial, trigger, trigger_kwargs)

    async def _advance_collecting_alerts(
        self, initial: str, trigger: str | None, trigger_kwargs: dict | None,
    ) -> AdvanceResult:
        """Run the chosen advance helper while harvesting CASE_ALERTED events into the result.

        Temporarily overrides the instance's case_log_alert so each call both records the
        message locally AND performs its normal on-disk logging, then restores the class
        method (by deleting the instance attribute) and folds the collected messages into
        the returned AdvanceResult's `alerts`. See advance() / AdvanceResult.alerts."""
        case = self._case
        collected: list[str] = []
        underlying = case.case_log_alert

        def _collecting_log_alert(short_msg: str = "", *, where: str | None = None) -> None:
            collected.append(short_msg)
            underlying(short_msg, where=where)

        case.case_log_alert = _collecting_log_alert  # type: ignore[method-assign]
        try:
            if trigger is not None:
                result = await self._advance_pinned(initial, trigger, trigger_kwargs)
            else:
                result = await self._advance_auto_sweep(initial)
        finally:
            del case.case_log_alert  # drop the instance override; class method shines through
        if collected:
            result = replace(result, alerts=tuple(collected))
        return result

    async def _advance_pinned(
        self, initial: str, trigger: str, trigger_kwargs: dict | None,
    ) -> AdvanceResult:
        """Pinned-trigger path of advance(). See advance()."""
        case = self._case
        if trigger not in case._fsm.triggers:   # not a trigger at all -> misuse, raise
            known = ", ".join(case._fsm.triggers) or "(none)"
            raise ValueError(
                f"case_advance(trigger={trigger!r}): {type(case).__name__!r} has no such "
                f"trigger on its FSM. Known triggers: {known}."
            )
        if not self._has_edge_from(case.case_state, trigger):
            # A real trigger, but no edge leaves the CURRENT state by it: a plain non-advance
            # (NOT 'blocked' — only the unrestricted sweep can prove that).
            return AdvanceResult(initial, case.case_state)
        if trigger_kwargs is None and not case._fsm.is_auto(case.case_state, trigger):
            raise ValueError(
                f"case_advance(trigger={trigger!r}): {trigger!r} is a MANUAL ('==') edge "
                f"from state {case.case_state!r}; pass trigger_kwargs (even {{}}) to fire it "
                "through the reporter. (Auto '--' edges may omit it.)"
            )
        result = await self._attempt_one_trigger(initial, trigger, trigger_kwargs or {})
        # progressed/failed -> report it; guard declined (None) -> a plain non-advance.
        return result if result is not None else AdvanceResult(initial, case.case_state)

    async def _advance_auto_sweep(self, initial: str) -> AdvanceResult:
        """No-argument path of advance(). See advance()."""
        case = self._case
        candidates = self._forward_candidates(case.case_state)
        for trig, _dest in candidates:
            result = await self._attempt_one_trigger(initial, trig, {})
            if result is not None:      # progressed or failed-and-folded -> done this pass
                return result
        exceptions: tuple = ()
        if case.case_state not in case._fsm.timed_escape_states:
            exceptions = (self._make_blocked(candidates),)
        return AdvanceResult(initial, case.case_state, exceptions=exceptions)

    async def _attempt_one_trigger(
        self, initial: str, trigger: str, kwargs: dict,
    ) -> AdvanceResult | None:
        """Fire one selected trigger. See advance()."""
        case = self._case
        try:
            if await getattr(case, trigger)(**kwargs):
                return AdvanceResult(initial, case.case_state, trigger=trigger)
            return None
        except OwnershipLostError:      # FATAL: surfaced exactly like the pre-step beat does.
            raise
        except CaseTransitionInFlightError:   # misuse, not a transition outcome: raise, don't fold.
            raise
        except Exception as err:        # absorbed: reported as data, not raised at the driver
            return AdvanceResult(initial, case.case_state, trigger=trigger,
                                 exceptions=(err,))

    def _forward_candidates(self, state: str):
        """The auto-advance edges leaving `state`, as (trigger, dest) in declared order.
        Empty when terminal / nothing auto-advances from here (e.g. awaiting input). With
        guards, more than one candidate may be eligible; advance() tries them in order."""
        return self._case._fsm.auto_edges_from(state)

    def _has_edge_from(self, state: str, trigger: str) -> bool:
        """Is `trigger` an edge (auto OR manual) leaving `state`? Unlike _forward_candidates
        (auto only), this also sees MANUAL (`==`) edges. The pinned-trigger path uses it to
        tell 'real trigger but not available from here' (a plain non-advance) apart from
        'not a trigger at all' (misuse that raises) — the latter checked against
        case._fsm.triggers before this."""
        for t in self._case._fsm.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if state in srcs and t["trigger"] == trigger:
                return True
        return False

    def _make_blocked(self, candidates) -> AutoAdvanceBlocked:
        """Build the AutoAdvanceBlocked marker for the current (provably stuck) state,
        logging a SINGLE CASE_ALERTED the first time we detect the block in this dwell so it
        is visible on disk without spamming the low-volume log on every case_advance() call."""
        case = self._case
        if not case._journal.has_event_since_enter(EV_ALERTED):
            case.case_log_alert(
                f"auto-advance blocked in {case.case_state!r}", where=case.case_state
            )
        return AutoAdvanceBlocked(
            case.case_id, case.case_state, candidates=[t for t, _ in candidates]
        )
