# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
FolderBackedCase: folder-anchored, file-first case lifecycle framework.

A case is a heavyweight, FSM-driven object whose entire state—record,
event log, and working files—lives in a single folder on disk. No
database required. The folder is self-describing and can be archived or
moved atomically. Objects descended from this class represent things like
a support trouble ticket, an inbound document for processing, a contract
bundle to be reviewed, etc.

Core pieces
-----------
CaseRecord              — skinny Pydantic identity card (case_record.yaml).
CaseEventLogReader      — read-oriented convention interpreter over PrimitiveEventLog.
CaseAssets              — working-file playground + retention manifest (_keep.txt).
FolderBackedCase        — ABC you subclass to define a case type.
FolderBackedCaseReader  — lock-free read-only folder view (no lease, no registry).
CaseReadView            — Protocol shared by live case and reader.
AdvanceResult           — outcome of case_advance() (non-throwing reporter).
CaseTypeSpec            — compiled class-behavior contract (FSM + assets).
FsmChainSpec            — compiled state-chains DSL, as returned by compile_fsm().

Terminology
-----------
See volatile/tmp/case-docs-glossary.md for standard terms (case, record, rehydrate,
bind/detach, journal vs reader, CaseManager).

The tightly-coupled supporting classes live in the folder_backed_case_support
package. This facade re-exports only the case's OWN surface — the types actually
returned by, or raised by, a FolderBackedCase's own public methods. Implementation
details that never surface through a public method (e.g. CaseJournal, HeartbeatLease,
StateChainParser, the individual folder-layout name constants) are internal and must
be imported from their own submodule under folder_backed_case_support if you really
need them. Layers that sit ABOVE the individual case are likewise NOT re-exported and
must be imported from their own modules: name-driven resolution lives in
CaseTypeRegistry / case_type_registry (folder_backed_case_support.case_type_registry),
and the case-driving/scheduling seam lives in CasePoolDriver
(folder_backed_case_support.case_pool_driver).

Quick start
-----------
    class TicketCase(FolderBackedCase):
        fsm_state_chains = ["^new --open_ticket--> open ==close_ticket-->closed^"
                            "*--@DWELL>14d#non_responsive-->auto_closed^", # all state timed-escape edge
                           ]

        asset_aliases = [
            {"path": "ticket.yaml", "loader": Callable, "states": {"new", "open", "closed"}}, # alias "ticket"
            {"path": "resolution-log/customer--convo.md", "loader": ChatLog, "states": {"open"}}, # alias "convo"
        ]
        fsm_trigger_chokes = {"open_ticket": {"cpu"}}  # pretend it takes lots of cpu

        async def perform_open_ticket(self, tctx) -> None:
            # called by the open_ticket() trigger
            # when this case is run in a pool, it may wait on other "cpu" choking triggers

        async def perform_close_ticket(self, tctx) -> None:
            # called by the close_ticket() trigger
            # note that the fsm_state_chains say this step isn't auto-triggered (by advance())

        async def on_enter_closed(self, tctx) -> None:
            # do something like notify the customer that their ticket has been closed

        # Every hook takes the trigger context `tctx` after `self`; see "Creating Hook
        # Methods" in the class docstring for what `tctx` is and how it is populated.
     

    from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
    case_type_registry.register_case_types(TicketCase)

    case = TicketCase.create_case_in_folder(Path("/data/cases/t-001"), case_id="t-001")
    try:
        await case.open_ticket()
        await case.close_ticket()
    finally:
        case.case_detach()   # release the lease when done; folder is self-contained on disk

    # Reopen later without knowing the concrete class:
    case = case_type_registry.rehydrate(Path("/data/cases/t-001"))
    try:
        ...
    finally:
        case.case_detach()
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import logging
import time
import weakref
from abc import ABC
from dataclasses import replace
from pathlib import Path
from typing import Any

from totodev_pub.folder_backed_case_support.constants import (
    RECORD_NAME, LEASE_NAME, LOGS_DIR_NAME, LOG_FILE_NAME,
    CASE_RESERVED_ARTIFACT_NAMES, CASE_BASE_EVENT_PREFIX,
    DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS,
    DEFAULT_LEASE_TTL_SECS, LEASE_HEARTBEAT_THROTTLE_SECS,
    EV_TERMINATED, EV_ALERTED, SIG_TERMINATING,
)
from totodev_pub.folder_backed_case_support.helpers import _utcnow, _new_time_slug
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError, OwnershipLostError, DetachedCaseError,
    CaseTypeMismatchError, RecordTypeMismatchError,
    IncompatibleReclassError, MissingFsmError, FsmChainParseError, FsmBindingError,
    AutoAdvanceBlocked, TriggerTimeout, MissingAssetSchemaError, MissingTriggerChokesError,
    CaseTransitionInFlightError,)
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.case_type_spec import CaseTypeSpec
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_journal import CaseJournal
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.heartbeat_lease import (
    HeartbeatLease, LeaseAlreadyHeldError, LeaseOwnershipLostError,)
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    StateChainParser, FsmChainSpec,)
from totodev_pub.folder_backed_case_support.case_machine_factory import _CaseMachineFactory
from totodev_pub.folder_backed_case_support.case_logging import (
    LogRetention, set_case_log_retention, get_case_log_retention,
    build_case_logger, write_attach_banner, purge_case_log,
)
from totodev_pub.folder_backed_case_support.case_read_view import CaseReadView
from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader


logger = logging.getLogger(__name__)

__all__ = [
    "FolderBackedCase", "FolderBackedCaseReader", "CaseReadView",
    "CaseRecord", "CaseEventLogReader", "CaseAssets", "AdvanceResult",
    "FsmChainSpec", "CaseTypeSpec", "CaseAlreadyOpenError", "OwnershipLostError",
    "DetachedCaseError", "CaseTypeMismatchError",
    "RecordTypeMismatchError", "IncompatibleReclassError", "MissingFsmError",
    "FsmChainParseError", "FsmBindingError", "AutoAdvanceBlocked", "TriggerTimeout",
    "MissingAssetSchemaError", "MissingTriggerChokesError",
    "CASE_RESERVED_ARTIFACT_NAMES", "CASE_BASE_EVENT_PREFIX",
    "LogRetention", "set_case_log_retention",
]


def _raises_when_detached(fn):
    """Documentary marker (no behavior change): tags a PUBLIC method that raises
    ``DetachedCaseError`` when called on a detached husk (see ``case_is_detached`` /
    ``_check_active()``). Purely metadata — the actual guard is the method's own
    ``self._check_active()`` call; this decorator does not add, remove, or reorder it.
    Only apply to public ``case_*`` methods; private/internal methods don't need it."""
    fn.__raises_when_detached__ = True
    return fn


# ---------------------------------------------------------------------------
# FolderBackedCase — the logic base class
# ---------------------------------------------------------------------------

class FolderBackedCase(ABC):
    """Base class for all folder-backed case types.

    Subclass this ABC, set ``fsm_state_chains``, and declare the hook methods
    the chains name. Provides: case folder (record + event log + assets), async
    FSM via ``transitions``, one-step ``case_advance()`` driving, ephemeral-file
    retention, the two-phase termination hook, and a single-owner heartbeat lease.

    How this class is organized
    ---------------------------
    The class body is laid out in four labeled sections:

      * SECTION 1 — START HERE: ``fsm_state_chains`` and hook/guard naming.
      * SECTION 2 — Runtime API: create/open, ``case_advance()``, properties,
        ``case_assets``, lock-free ``peek_*`` inspectors.
      * SECTION 3 — Customization seams: ``_record_cls``, ``compile_fsm()``,
        recovery hooks, ``case_reclassify_to()``, etc.
      * SECTION 4 — Internal mechanics (maintainers only).

    Hooks and guards
    ----------------
    Names in ``fsm_state_chains`` wire optional subclass methods:

      * ``async def on_enter_<state>(self, tctx)``
      * ``async def on_exit_<state>(self, tctx)``
      * ``async def perform_<trigger>(self, tctx)`` — auto-wired as ``before_<trigger>``
        when no explicit ``before_<trigger>`` exists
      * ``async def before_<trigger>(self, tctx)``
      * ``async def after_<trigger>(self, tctx)``
      * ``async def guard_<guard>(self, tctx)`` — boolean gate from ``guard#trigger`` DSL

    Built on the ``transitions`` library; raising in a guard or ``before_`` hook
    aborts the transition and counts as a transition fail.

    Guards should be fast, idempotent, and side-effect free (they may be polled
    many times). Library-provided factual guards:

      * ``@FAIL(>|>=|<|<=)n#`` — transition-fail count since entering current state
      * ``@DWELL(>|>=|<|<=)dur#`` — seconds in current state (units s/m/h/d)

    By default, transitions carry an implied ``@FAIL<1#`` guard unless overridden.

    Wildcard source ``*--guard#trigger-->X`` applies from any state (often with
    ``@FAIL`` or ``@DWELL`` for error/aging flow control).

    Creating Hook Methods — passing arguments to triggers
    -----------------------------------------------------
    Every hook receives one trigger-context argument, conventionally ``tctx``
    (the ``transitions`` EventData object — not to be confused with the event log).

      * Direct ``await case.<trigger>(**kwargs)`` bundles kwargs into ``tctx.kwargs``.
      * No-argument ``case_advance()`` sweeps leave ``tctx.kwargs`` empty.
      * Pinned ``case_advance(trigger, trigger_kwargs={...})`` passes kwargs through;
        ``trigger_kwargs`` is REQUIRED for MANUAL (``==``) edges via the reporter.

    Advanced ``transitions`` customization is possible but beyond this docstring.
  """

    # =======================================================================
    # SECTION 1 — START HERE: define your case type (ALL audiences)
    # -----------------------------------------------------------------------
    # The first thing every subclass does: declare its FSM in `fsm_state_chains` and
    # write the hook methods it names. Most case types need ONLY this section — set
    # `fsm_state_chains`, add a few `perform_<trigger>` / `guard_<guard>` methods, done.
    # (Rarer define-time seams — `_record_cls`, `compile_fsm()` — live in SECTION 3.)
    # =======================================================================

    # The ONE declarative FSM input (World A): the default compile_fsm() parses this.
    # PRIMARY extension point — set this on your subclass to define the whole lifecycle.
    #
    # DSL cheatsheet:
    #   ^state           leading  `^` = initial state
    #   state^           trailing `^` = terminal state
    #   A==trigger-->B   `==` connector = MANUAL edge (fired by `await case.trigger()`)
    #   A--trigger-->B   `--` connector = AUTO edge (fired by case_advance(); a driver loops it)
    #   guard#trigger    binds method `guard_<guard>` as the edge's guard
    #   @DWELL>14d       factual time guard: true once dwell in this state exceeds 14 days
    #   @FAIL>=n         factual guard: true once n failures accrued this dwell
    #   ~<dur>           soft (warning) timeout for the trigger's work
    #   *--...-->X       wildcard source: an edge leaving every state
    # See StateChainParser for the authoritative, complete grammar.
    fsm_state_chains: list[str] = []

    # Required declaration of which capacity-constrained resources each trigger's work
    # may draw on when this case runs inside a pool that throttles such resources.
    # Map trigger name -> set of resource name strings. An empty dict means none.
    # Pool drivers (not the case itself) supply the integer permit counts.
    # External readers of compiled behavior: ``case_type_spec()`` (class method).
    _TRIGGER_CHOKES_NOT_DECLARED = object()
    fsm_trigger_chokes = _TRIGGER_CHOKES_NOT_DECLARED

    # Required declaration of the case's on-disk data objects (see aliased_asset_specs).
    # A concrete subclass MUST set this (even to []). The sentinel lets abstract
    # intermediates stay undeclared until something tries to instantiate them.
    # External readers of compiled behavior: ``case_type_spec()`` (class method).
    _ASSET_ALIASES_NOT_DECLARED = object()
    asset_aliases = _ASSET_ALIASES_NOT_DECLARED
    _asset_book: AliasedAssetSpecs | None = None

    # When False (default), every declared alias must specify loader and states.
    # When True, informal declarations are allowed; omitted states/loader make the
    # guard a no-op for that alias.
    flexible_asset_alias_loading: bool = False

    # ---- Hook & guard naming (see class docstring for full rules) ----
    #
    # After `fsm_state_chains`, behavior is attached by METHOD NAME. Suffixes must
    # match parsed state/trigger/guard names exactly. Orphan hook methods (no matching
    # DSL name) fail at bind via validate_object_compatibility(orphan_detection="error").
    #
    # SIGNATURE: every hook takes `tctx` after `self`. Hooks must yield the event loop;
    # offload blocking work via case_run_blocking(). See case_advance() for lease keepalive.

    # =======================================================================
    # SECTION 2 — Quick-start runtime API (mainstream "quick & dirty" users)
    # -----------------------------------------------------------------------
    # The everyday surface: create/open a case, drive it forward, read its
    # status, reach its files, and peek at folders without binding. Most users
    # need nothing beyond Sections 1 and 2.
    # =======================================================================

    @classmethod
    def create_case_in_folder(
        cls,
        case_folder: Path,
        *,
        case_id: str | None = None,
        external_key: str | None = None,
        nickname: str | None = None,
        **fields,
    ) -> FolderBackedCase:
        """First-time inception of a brand-new case.

        Creates a fresh case folder, writes the record, binds a live lease-held
        instance, and logs CASE_CREATED + initial CASE_STATE_ENTERED. For reopening an
        existing folder use ``MyCase(folder)`` or ``case_type_registry.rehydrate(folder)``.
        Call ``case_detach()`` on the returned instance when you are done with it.

        Planned ``CaseManager`` (draft: notebooks/DEVDAVE/case_manager_classes/CaseManager
        Model.md) will also call this for fleet inception.

        Raises:
            FileNotFoundError: parent folder does not exist.
            FileExistsError: folder already contains case artifacts.
        """
        case_folder = Path(case_folder)
        book = cls._resolve_asset_book()
        asset_aliases = book.to_record()
        parent = case_folder.parent
        if not parent.exists():
            raise FileNotFoundError(
                f"Parent folder {parent} does not exist. "
                "Create/confirm the parent folder first, then retry create_case_in_folder()."
            )
        if not parent.is_dir():
            raise NotADirectoryError(
                f"Parent path {parent} exists but is not a directory."
            )
        if case_folder.exists():
            if not case_folder.is_dir():
                raise NotADirectoryError(
                    f"Target path {case_folder} exists but is not a directory."
                )
        else:
            case_folder.mkdir(parents=False, exist_ok=False)
        reserved_paths = {
            name: case_folder / name for name in CASE_RESERVED_ARTIFACT_NAMES
        }
        found = [name for name, path in reserved_paths.items() if path.exists()]
        if found:
            found_list = ", ".join(sorted(found))
            raise FileExistsError(
                f"Cannot create a new case in {case_folder}: existing case artifacts "
                f"found ({found_list}). Choose a clean case folder."
            )
        record = cls._record_cls(
            case_object_type=cls.__name__,
            case_id=case_id or cls.generate_case_id(),
            external_key=external_key,
            nickname=nickname,
            created=_utcnow(),
            asset_aliases=asset_aliases,
            fsm_state_chains=list(cls.fsm_state_chains),
            **fields,
        )
        # Direct save (no instance yet) — SAFE BY CONSTRUCTION: case_object_type is set to
        # cls.__name__ here, so it satisfies the _flush_record type-name guard.
        record.save(str(case_folder / RECORD_NAME))
        case = cls(case_folder)
        keep_paths = [
            spec.relative_path
            for spec in book.spec_map().values()
            if spec.keep
        ]
        if keep_paths:
            case.case_assets.add_keep_rules(*keep_paths)
        case._journal.log_created(
            cls.__name__,
            case_id=record.case_id,
            external_key=record.external_key,
        )
        case._journal.log_state_entered(cls._fsm.initial_state)
        return case

    # Re-opening an existing case by its concrete class is the constructor `cls(folder)`;
    # re-opening WITHOUT knowing the class is case_type_registry.rehydrate(folder). Both
    # are documented in SECTION 4 (__init__) and the CaseTypeRegistry, respectively.

    def case_detach(self) -> None:
        """Unbind this object from its folder: release the lease and mark detached.

        Call this when you are done acting on a live case (scripts, tests, handoff to
        ``CaseManager``, after harvesting a terminated case). After detach, mutating use
        raises ``DetachedCaseError``. Does not move or archive the folder — a planned
        ``CaseManager`` (draft: notebooks/DEVDAVE/case_manager_classes/CaseManager
        Model.md) owns lifecycle actions once the lease is cleared.

        If you forget, the lease self-expires after ``DEFAULT_LEASE_TTL_SECS`` (crash-
        recovery window); explicit detach is still preferred so other owners need not wait.
        """
        if self._lease is not None:
            self._lease.release()

    @_raises_when_detached
    async def case_advance(
        self, trigger: str | None = None, trigger_kwargs: dict | None = None,
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

        Maintainer notes:
          Outcomes (all returned, never raised — except the misuse guards below):
            * progressed — a transition fired; result.trigger names it, final_state advanced.
            * a failed attempt — the work raised; the exception is CAUGHT and carried in
              result.exceptions (decorated + logged + hooked by _on_fsm_exception), the case
              stayed in its source state to be retried next pass. This now ALSO covers a
              pinned MANUAL edge: routed through here, its failure is folded as data rather
              than raised (a direct `await case.<trigger>()` still raises — the other channel).
            * nothing to do — terminal, a guard declined, or the pinned trigger has no edge
              from here, all with no progress.
            * BLOCKED — only on the NO-ARGUMENT sweep: when nothing fired/raised AND the
              state has no timed escape, a synthetic AutoAdvanceBlocked is carried in
              result.exceptions (one CASE_ALERTED logged on first detection per dwell).
              Deterministic. NOT synthesized for a pinned `trigger=...` call.
          Stall handling has NO self-pulse: a case cannot watchdog its own case_advance()
          from inside a single suspended coroutine, so stalls are split by failure mode
          instead: a stalled external job rides a `@DWELL>...` timed-escape edge that
          ripens and case_advance() fires it (in-band, declarative, self-healing); a
          genuinely stuck state surfaces as the BLOCKED outcome above; and a hung
          case_advance() call itself is an out-of-band concern for the driver (e.g.
          wrapping the call in asyncio.wait_for) — the case cannot observe that itself.
          Misuse guards that RAISE (programming errors, not flow conditions): DetachedCaseError
          (acting on a detached husk); ValueError (unknown trigger name, a manual edge fired
          without trigger_kwargs, or trigger_kwargs given with no trigger). Also RAISES
          OwnershipLostError (NOT folded into the result) if a beat — the pre-step one below
          or the in-flight keepalive — finds the folder reclaimed by another owner: a fatal
          invariant breach, not a step outcome. Also RAISES CaseTransitionInFlightError (also
          NOT folded into the result) if another trigger call is ALREADY in flight on this
          same object — this method is deliberately NON-REENTRANT: at most one FSM trigger
          invocation may be executing on a given live case at a time, checked and enforced
          fail-fast (not queued) via _on_prepare_fsm_event. A direct `await case.<trigger>()`
          call is guarded the same way. A driver's fire() does not hit this for its own
          beats — it detects an in-flight slot and awaits the existing task's result instead
          of calling fire() again; this exception is for callers that bypass that coalescing
          (e.g. a caller that obtained a live reference via CaseManager.get() and calls a
          trigger directly while a beat is already advancing the same case).

        Contract (well-behaved async hooks):
          The lease keepalive — and cooperative scheduling generally — depends on a trigger's
          work actually YIELDING the event loop. Hooks MUST be well-behaved async: await at
          reasonable intervals and offload blocking/CPU-bound work via case_run_blocking()
          (or their own executor/thread). A hook that monopolizes the loop starves every other
          case sharing it AND its own heartbeat, so the lease can lapse despite the keepalive.
          The keepalive protects only the trigger's WORK slot (perform_/before); guards and
          on_enter/on_exit/after are expected to be light (see the hook conventions)."""
        if trigger is None and trigger_kwargs is not None:
            raise ValueError(
                "case_advance(): trigger_kwargs was supplied without a trigger; kwargs have "
                "no edge to flow into. Name the trigger to fire, or drop trigger_kwargs."
            )
        initial = self.case_state
        if self.case_is_terminal:
            return AdvanceResult(initial, self.case_state)
        # Peek _transition_in_flight before _advance_collecting_alerts installs its alert
        # override — a rejected overlapping call would restore in finally and clobber the
        # in-flight call's override. The flag is set only in _on_prepare_fsm_event.
        if self._transition_in_flight:
            raise CaseTransitionInFlightError(self.case_id, self._folder, self._active_trigger_name)
        self._check_active()
        self.case_heartbeat()  # pre-step beat for long-dwelling no-op polls; see docstring
        return await self._advance_collecting_alerts(initial, trigger, trigger_kwargs)

    async def _advance_collecting_alerts(
        self, initial: str, trigger: str | None, trigger_kwargs: dict | None,
    ) -> AdvanceResult:
        """Run the chosen advance helper while harvesting CASE_ALERTED events into the result.

        Temporarily overrides the instance's case_log_alert so each call both records the
        message locally AND performs its normal on-disk logging, then restores the class
        method (by deleting the instance attribute) and folds the collected messages into
        the returned AdvanceResult's `alerts`. See case_advance() / AdvanceResult.alerts."""
        collected: list[str] = []
        underlying = self.case_log_alert

        def _collecting_log_alert(short_msg: str = "", *, where: str | None = None) -> None:
            collected.append(short_msg)
            underlying(short_msg, where=where)

        self.case_log_alert = _collecting_log_alert  # type: ignore[method-assign]
        try:
            if trigger is not None:
                result = await self._advance_pinned(initial, trigger, trigger_kwargs)
            else:
                result = await self._advance_auto_sweep(initial)
        finally:
            del self.case_log_alert  # drop the instance override; class method shines through
        if collected:
            result = replace(result, alerts=tuple(collected))
        return result

    async def _advance_pinned(
        self, initial: str, trigger: str, trigger_kwargs: dict | None,
    ) -> AdvanceResult:
        """Pinned-trigger path of case_advance(). See case_advance()."""
        if trigger not in self._fsm.triggers:   # not a trigger at all -> misuse, raise
            known = ", ".join(self._fsm.triggers) or "(none)"
            raise ValueError(
                f"case_advance(trigger={trigger!r}): {type(self).__name__!r} has no such "
                f"trigger on its FSM. Known triggers: {known}."
            )
        if not self._has_edge_from(self.case_state, trigger):
            # A real trigger, but no edge leaves the CURRENT state by it: a plain non-advance
            # (NOT 'blocked' — only the unrestricted sweep can prove that).
            return AdvanceResult(initial, self.case_state)
        if trigger_kwargs is None and not self._fsm.is_auto(self.case_state, trigger):
            raise ValueError(
                f"case_advance(trigger={trigger!r}): {trigger!r} is a MANUAL ('==') edge "
                f"from state {self.case_state!r}; pass trigger_kwargs (even {{}}) to fire it "
                "through the reporter. (Auto '--' edges may omit it.)"
            )
        result = await self._attempt_one_trigger(initial, trigger, trigger_kwargs or {})
        # progressed/failed -> report it; guard declined (None) -> a plain non-advance.
        return result if result is not None else AdvanceResult(initial, self.case_state)

    async def _advance_auto_sweep(self, initial: str) -> AdvanceResult:
        """No-argument path of case_advance(). See case_advance()."""
        candidates = self._forward_candidates(self.case_state)
        for trig, _dest in candidates:
            result = await self._attempt_one_trigger(initial, trig, {})
            if result is not None:      # progressed or failed-and-folded -> done this pass
                return result
        exceptions: tuple = ()
        if self.case_state not in self._fsm.timed_escape_states:
            exceptions = (self._make_blocked(candidates),)
        return AdvanceResult(initial, self.case_state, exceptions=exceptions)

    async def _attempt_one_trigger(
        self, initial: str, trigger: str, kwargs: dict,
    ) -> AdvanceResult | None:
        """Fire one selected trigger. See case_advance()."""
        try:
            if await getattr(self, trigger)(**kwargs):
                return AdvanceResult(initial, self.case_state, trigger=trigger)
            return None
        except OwnershipLostError:      # FATAL: surfaced exactly like the pre-step beat does.
            raise
        except CaseTransitionInFlightError:   # misuse, not a transition outcome: raise, don't fold.
            raise
        except Exception as err:        # absorbed: reported as data, not raised at the driver
            return AdvanceResult(initial, self.case_state, trigger=trigger,
                                 exceptions=(err,))

    # Why no run_to_completion()/drive loop lives here: see case_advance()'s docstring.

    # ---- Identity & status (read-only snapshots; case_state is a plain attribute) ----

    @property
    def case_id(self) -> str:
        """Stable case identifier from the record."""
        return self._record.case_id

    @property
    def case_external_key(self) -> str | None:
        """Optional business key from the record."""
        return self._record.external_key

    @property
    def case_nickname(self) -> str | None:
        """Optional display label from the record."""
        return self._record.nickname

    @property
    def case_object_type(self) -> str:
        """Registered case class name stamped on the record."""
        return self._record.case_object_type

    @property
    def case_created(self) -> datetime.datetime:
        """Record creation timestamp (aware UTC)."""
        return self._record.created

    @property
    def case_terminal_at(self) -> datetime.datetime | None:
        """Termination timestamp when terminal; None while live."""
        return self._record.terminal

    @property
    def case_folder(self) -> Path:
        """On-disk folder this case is bound to."""
        return self._folder

    @property
    def case_is_live(self) -> bool:
        """True when current FSM state is not terminal."""
        return self.case_state not in self._fsm.terminal_states

    @property
    def case_is_terminal(self) -> bool:
        """True when current FSM state is terminal."""
        return self.case_state in self._fsm.terminal_states

    @property
    def case_is_detached(self) -> bool:
        """True when this object is no longer bound to its folder: the lease was
        released via ``case_detach()`` or has expired.

        A detached object is a husk — any mutating use (`case_advance()`,
        `case_heartbeat()`, manual triggers) raises `DetachedCaseError`. Re-open via
        `case_type_registry.rehydrate(case_folder)`."""
        return self._lease is None or not self._lease.is_active()

    @property
    def case_advanceable(self) -> bool:
        """True when the CURRENT state has at least one auto-advanceable (`--`) exit, i.e.
        an unattended `case_advance()` could fire here (subject to guards). False for a
        terminal state or a state left only by MANUAL (`==`) edges.

        STRUCTURAL, not runtime: this reports whether an auto exit EXISTS, not whether a
        guard would currently permit it. A scheduler uses it to tell a genuinely manual-only
        state (no auto exits — accelerate demotion) apart from a state whose guards merely
        declined this pass (auto exits exist — normal cadence). See AutoAdvanceBlocked for
        the runtime "declined now and can't ripen" signal."""
        return self._fsm.has_auto_exits(self.case_state)

    @property
    def case_transition_fail_count(self) -> int:
        """The value the `@FAIL` guard compares against: the count of failed transition
        attempts since the case entered its current state. See
        `CaseJournal.count_fails_this_dwell` for exactly what counts as a failure and how it
        is derived."""
        return self._journal.count_fails_this_dwell()

    @property
    def case_dwell_secs(self) -> float:
        """Seconds the case has spent in its CURRENT state — the value the `@DWELL` guard
        compares against (the sibling of `case_transition_fail_count`). Measured from
        `self._state_entered_at` (the latest CASE_STATE_ENTERED, or creation for a brand-new
        case).

        It is ALSO an override SEAM: a subclass may override this property (e.g. to fake the
        clock in tests), and the _CaseMachineFactory reads it back for the `@DWELL` guard."""
        return (_utcnow() - self._state_entered_at).total_seconds()

    @property
    def case_last_activity(self) -> datetime.datetime | None:
        """Latest event-log activity, or record creation if none."""
        return self._as_utc(self._journal.last_activity) or self._record.created

    @property
    def case_events(self) -> CaseEventLogReader:
        """Read-only view of this case's event log (writes go through CaseJournal)."""
        return self._journal.reader

    # ---- assets (playground + retention), grouped on CaseAssets ----

    @property
    def case_assets(self) -> CaseAssets:
        """The case's CaseAssets: file playground under assets/ plus the keep manifest.

        Quick use:
          Your working files live here. Use case.case_assets.folder, .asset_path(...),
          .relative_path(...), .write(...), .add_keep_rules(...), .list_assets(), etc.
          Asset keep rules are stored in ``_keep.txt`` with an ``assets/`` prefix.
          Anything not matched by the manifest is purged when the case terminates.

        Maintainer notes:
          Kept off this class's own namespace so asset concerns stay grouped in one place.
          For non-asset files, use ``case_add_keep_rules()`` instead."""
        return self._assets

    def case_add_keep_rules(self, *rules: str | Path) -> None:
        """Append case-relative keep rules to ``_keep.txt`` (exact path or glob).

        Use this from subclass hooks (especially ``on_terminating()``) to retain files
        outside ``assets/``. For assets, prefer ``case_assets.add_keep_rules()`` or
        ``case_assets.write(..., keep=True)``."""
        self._keep_manifest.add_rules(*rules)

    def case_load_asset(self, alias: str) -> object:
        """Load a declared asset alias after checking it is trustworthy in the current
        FSM state. Raises AssetNotTrustedInStateError before any disk I/O when the
        alias is constrained and the current state is not listed.

        Declaring asset_aliases is convenience sugar, not a requirement — a subclass
        is free to leave it ``[]`` and manage its files by hand. Without a declared
        alias (or for unguarded access even with one), reach case_assets directly:
        ``case_assets.asset_path(relative_path)`` for the filepath, or
        ``case_assets.read(relative_path)``/your own parsing for the in-memory object.
        It's just a couple more steps than this one-liner."""
        type(self)._resolve_asset_book().assert_trusted(alias, self.case_state)
        return self.case_assets.load_dataclass(alias)

    # ---- record read accessor ----

    def case_fetch_record(self, *, force: bool = False) -> CaseRecord:
        """Public read accessor for the identity record.

        Returns a detached deep-copy snapshot. Pass ``force=True`` to re-read from disk
        first when another process may have changed the file.
        """
        self._record.reload_from_file(force=force)
        return self._record.detached_copy()

    # ---- operator alert channel (type-agnostic escalation marker) ----

    def case_log_alert(self, short_msg: str = "", *, where: str | None = None) -> None:
        """Add a CASE_ALERTED entry to the event log: the case family's single type-agnostic "this case needs a
        human to look at it" marker.

        Quick use:
          Call to flag a case for human attention. Because it reads the same for every
          case type, an observer can surface flagged cases without knowing any internals.
          Use SPARINGLY on the low-volume audit log: raise one for an integrity risk or a
          substantial deviation from norms, NOT for routine, recoverable defects the flow
          absorbs. Orthogonal to the FSM (does not change state or terminate the case).

        Args:
            short_msg: a brief human-readable reason (terse phrase, not a stack trace).
            where: locus of concern; defaults to the current state.
        """
        self._journal.log_alerted(
            where or self.case_state, msg=short_msg
        )

    # ---- folder peek: read a case folder without constructing a live instance ----

    @staticmethod
    def peek_case_record(
        folder: Path,
        *,
        record_cls: type[CaseRecord] | None = None,
        case_cls: type[FolderBackedCase] | None = None,
    ) -> CaseRecord:
        """Read the identity record from disk — lock-free, no live case, no registry.

        Quick use:
          Inspect a case's record without taking the lease or building an object (safe
          even while another owner holds the case). YOU supply the typed record shape,
          or accept the base:
            - record_cls=...  → use that CaseRecord subclass directly (wins if both given).
            - case_cls=...    → use that case's _record_cls.
            - neither         → base CaseRecord (common identity fields only; subclass-specific
                                fields not present on the base are silently dropped).

        Advanced:
          When you want the type deduced from the on-disk record itself rather than supplying
          it here, use case_type_registry.peek_class(folder, return_class_object=True) first
          and pass the result as case_cls."""
        if record_cls is None:
            record_cls = case_cls._record_cls if case_cls is not None else CaseRecord
        # Peek is explicitly lock-free; the case lease is not ours to take here.
        return record_cls.open(str(Path(folder) / RECORD_NAME), without_lock=True)

    @staticmethod
    def peek_case_events(folder: Path) -> CaseEventLogReader:
        """A CaseEventLogReader over the folder's event log — lock-free, no live case,
        no registry. Uniform across every case type (the log format is not subclassed).
        Exposes current_state, is_terminal, last_activity, and .primitive for the raw log."""
        return CaseEventLogReader.for_folder(Path(folder))

    @staticmethod
    def peek_case_assets(folder: Path, *, resolve_asset_types: bool = False) -> CaseAssets:
        """A CaseAssets over the folder — lock-free, no live case, no registry. Uniform
        across every case type. Exposes list_assets(), keep_list(), asset_path(), etc.
        The peek analog of a live case's .case_assets property.

        Loads via LazyLoadedFileData by default (no case class needed). Pass
        resolve_asset_types=True to type each alias whose persisted loader name
        resolves through the asset-dataclass registry (others fall back to lazy)."""
        record = CaseRecord.open(str(Path(folder) / RECORD_NAME), without_lock=True)
        specs = AliasedAssetSpecs.from_record(
            record.asset_aliases, resolve_types=resolve_asset_types
        ).spec_map()
        return CaseAssets(
            Path(folder), asset_specs=specs, flexible_asset_alias_loading=True,
        )

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> bool | None:
        """Lock-free lease staleness read for a case folder (recovery sweeps).

        Return-value semantics: see `HeartbeatLease.is_expired` (on ``folder / LEASE_NAME``)."""
        return HeartbeatLease.is_expired(Path(folder) / LEASE_NAME)

    @staticmethod
    def peek_lease_secs_left(folder: Path) -> float | None:
        """Lock-free lease-time read for a case folder (no acquire).

        Return-value semantics: see `HeartbeatLease.secs_left` (on ``folder / LEASE_NAME``)."""
        return HeartbeatLease.secs_left(Path(folder) / LEASE_NAME)

    @staticmethod
    def get_case_reader(folder: Path) -> "FolderBackedCaseReader":
        """Return a lock-free read-only view of a case folder — no lease, no registry."""
        from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
        return FolderBackedCaseReader(Path(folder))

    # =======================================================================
    # SECTION 3 — Customization seams (highly-custom developers)
    # -----------------------------------------------------------------------
    # Overridable hooks and policy knobs for the peculiar use case. Everything
    # here ships with a sensible default; override only what you need. None of
    # this is required for the mainstream path in Section 2.
    # =======================================================================

    # ---- Define-time seams (rarely needed; most case types use the defaults) ----

    # The record-type seam: defaults to CaseRecord. Override with a CaseRecord subclass
    # when extra fields are needed. Read back via case_fetch_record().
    _record_cls: type[CaseRecord] = CaseRecord

    @classmethod
    def case_type_spec(cls) -> CaseTypeSpec:
        """Compiled class-behavior contract: FSM (including chokes) + asset aliases.

        Class-level only — the same for every instance of this type. Pool drivers,
        validators, and tooling should use this rather than private ``_fsm`` /
        ``_asset_book``."""
        return CaseTypeSpec(fsm=cls._fsm, assets=cls._resolve_asset_book())

    @classmethod
    def _resolve_asset_book(cls) -> AliasedAssetSpecs:
        """The class's declared alias book. Raises MissingAssetSchemaError if the
        concrete subclass never declared `asset_aliases`."""
        if cls.asset_aliases is FolderBackedCase._ASSET_ALIASES_NOT_DECLARED:
            raise MissingAssetSchemaError(cls.__name__)
        if cls._asset_book is None:
            cls._asset_book = AliasedAssetSpecs.from_declaration(
                cls.asset_aliases, flexible=cls.flexible_asset_alias_loading,
            )
        return cls._asset_book

    @classmethod
    def _seed_keep_rules(cls, assets: CaseAssets) -> None:
        """Append keep rules for every declaration with keep=True (idempotent)."""
        paths = [
            spec.relative_path
            for spec in cls._resolve_asset_book().spec_map().values()
            if spec.keep
        ]
        if paths:
            assets.add_keep_rules(*paths)

    @classmethod
    def _require_fsm_trigger_chokes_declared(cls) -> None:
        """Every subclass must set `fsm_trigger_chokes` explicitly ({} is valid)."""
        if cls.fsm_trigger_chokes is FolderBackedCase._TRIGGER_CHOKES_NOT_DECLARED:
            raise MissingTriggerChokesError(cls.__name__)

    @classmethod
    def _fold_trigger_chokes(cls, spec: FsmChainSpec) -> FsmChainSpec:
        """Merge this class's `fsm_trigger_chokes` into a compiled spec. Unknown trigger
        keys are a build-time error. Override authors should call this after building
        their hand-crafted spec unless they populate `trigger_chokes` themselves."""
        raw = cls.fsm_trigger_chokes
        if not raw:
            return spec
        known = set(spec.triggers)
        folded: dict[str, frozenset[str]] = {}
        for trigger, resources in raw.items():
            if trigger not in known:
                raise FsmChainParseError(
                    f"fsm_trigger_chokes names trigger {trigger!r}, which is not among "
                    f"this class's FSM triggers ({sorted(known)!r})"
                )
            if not isinstance(resources, (set, frozenset, list, tuple)):
                raise FsmChainParseError(
                    f"fsm_trigger_chokes[{trigger!r}] must be a set of resource name "
                    f"strings, not {type(resources).__name__}"
                )
            names = frozenset(str(name) for name in resources)
            if not names:
                continue
            if trigger in folded:
                raise FsmChainParseError(
                    f"fsm_trigger_chokes declares trigger {trigger!r} more than once"
                )
            folded[trigger] = names
        spec.trigger_chokes = folded
        return spec

    @classmethod
    def compile_fsm(cls) -> FsmChainSpec:
        """Render this class's declared FSM into an FsmChainSpec.

        Quick use:
          You almost never call or override this. The default simply parses
          `fsm_state_chains` for you; just declare your chains and move on.

        Advanced:
          This is the single, unambiguous manual escape hatch. OVERRIDE it to build
          or extend the spec by hand for the rare cases the chain DSL can't express
          (arbitrary callbacks, `unless`, state objects) — optionally by parsing the
          chains first and then tweaking the result. An override OWNS whether and when
          to call validate() / expand_wildcards() / classify() / apply_implicit_fail_cap().

        Maintainer notes:
          PURE: touches no class state and is called exactly once per subclass (by
          __init_subclass__), whose job is to cache the result as the `_fsm` singleton.
          The default parses `fsm_state_chains`, runs the whole-graph
          FsmChainSpec.validate(), then injects any `*--...-->` wildcard edges via
          expand_wildcards() (in that order, so the typo checks see only the explicit
          graph)."""
        return cls._fold_trigger_chokes(
            StateChainParser.parse(cls.fsm_state_chains)
            .validate()
            .expand_wildcards()
            .classify()
            .apply_implicit_fail_cap()
        )

    # ---- Recovery / lifecycle hooks ----

    def on_transition_exception(self, begin_state, trigger, final_state, exc) -> None:
        """Overridable recovery hook, fired (before the exception re-raises) whenever a
        transition's dispatch raised. Default: no-op.

        Advanced:
          `begin_state == final_state` ⇒ a PRE-commit failure (the work raised, the case
          never left its state — the retryable "no progress" kind). `begin_state !=
          final_state` ⇒ a POST-commit failure (the state DID change, then an entry/after
          hook raised; the case is in `final_state` carrying the baggage of a failed
          side-effect).

          Use it to compensate from inside the case (which, unlike a generic driver, knows
          its own data): mark a record field, schedule a fix-up, set a flag a later guard
          reads.

          DO NOT fire a transition from within this hook — re-entering the machine
          mid-dispatch is unsupported. To route to a fault state, prefer the declarative
          `@FAIL>=n` divert edge, or record intent here and let the next case_advance() carry
          it out."""

    def on_terminating(self) -> None:
        """Overridable hook fired in phase 1 (pre-finalization): assets still exist,
        record not yet stamped. Override to retain/extract final artifacts before the
        ephemeral purge — call ``case_add_keep_rules()`` for non-asset paths or
        ``case_assets.add_keep_rules()`` for assets. Default: no-op. Heavy async work
        belongs in an async ``before_`` hook on the terminating transition; this hook is
        synchronous."""

    def case_ext_status_info(self) -> dict[str, Any]:
        """Overridable hook: extra fields this case wants attached to its fleet-board
        row's "ext". Default: no-op (empty dict).

        Quick use:
          Override to surface transient, in-memory progress from inside a running step —
          e.g. read an attribute a perform_* trigger updates as it works, and return it
          here. Typically only meaningful while the case is sitting in one particular
          state; return {} the rest of the time.

          Must return a JSON-serializable dict[str, Any]. Any exception, non-dict
          return, or unserializable value is logged (throttled per case) and treated
          as {} — the board only degrades, it never fails or stalls the beat because
          of this hook.

        Advanced:
          Keep this FAST and CHEAP — it may be called on every row build (as often as
          once per maintenance tick), so never touch disk, never block, never await.
          It may also be called WHILE a perform_* trigger for this case is actively
          running (the fleet writer runs outside the trigger's own execution), so
          reading a value mid-update is expected and fine: this hook has no
          consistency guarantee relative to an in-flight trigger, and generally
          shouldn't need one for a best-effort progress signal like this."""
        return {}

    @classmethod
    def generate_case_id(cls) -> str:
        """Auto-ID factory used by create_case_in_folder() when no explicit case_id is
        supplied.

        Advanced:
          The SOLE public slug seam: an overridable extension point — a subclass may
          return a UUID, a domain-prefixed id, a sequential counter, etc., and can COMPOSE
          with the default via super().generate_case_id() (e.g.
          f"INV-{super().generate_case_id()}"). The default is a short, sortable, base-36
          millisecond time slug.

        Maintainer notes:
          For in-process collision resistance, generation is monotonic per class:
          if two calls land in the same millisecond, the latter is bumped to
          (previous + 1ms) before encoding. This does not guarantee uniqueness
          across multiple processes or machines.

          (Must be a classmethod: the id is minted before the instance exists, so
          there is no `self` to hang an instance method on.)"""
        now_ms = int(time.time() * 1000)
        mint_ms = now_ms if now_ms > cls._last_generated_case_id_ms else cls._last_generated_case_id_ms + 1
        cls._last_generated_case_id_ms = mint_ms
        return _new_time_slug(mint_ms)

    # Lease timing (TTL, beat throttle, in-flight pulse cadence) is a single FIXED policy in
    # constants.py (DEFAULT_LEASE_TTL_SECS et al.), deliberately not a per-case/per-state seam.
    # See "single-owner protection" in SECTION 4 for the crash-recovery-window rationale and
    # the idle-ownership contract.

    @_raises_when_detached
    def case_heartbeat(
        self,
        *,
        min_update_secs: float = LEASE_HEARTBEAT_THROTTLE_SECS,
        validate_ownership: bool = True,
    ) -> None:
        """Extend our lease. Raises `OwnershipLostError` when ownership is displaced.

        Quick use:
          The mainstream driver does NOT need to call this — case_advance() beats the lease
          for you. Call it directly only when you HOLD a bound case without advancing it (a
          custom dwell loop, or an idle holder choosing to keep the folder spoken-for rather
          than detaching — see the idle-ownership contract in SECTION 4)."""
        self._check_active()
        try:
            self._lease.heartbeat(
                min_update_secs=min_update_secs,
                validate_ownership=validate_ownership,
            )
        except LeaseOwnershipLostError as e:
            raise OwnershipLostError(self._folder) from e

    def trigger_warn_secs(self, trigger: str) -> float:
        """The SOFT timeout (seconds) for a trigger's work: its `~<dur>` DSL annotation if
        present, else DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS. The override seam for a per-case
        or dynamic budget — keep it cheap, it is consulted on every step that has work."""
        return self._fsm.trigger_timeouts.get(trigger, DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS)


    def archive_grouping_label(self) -> str:
        """Destination archive grouping when this case closes. Default: close month
        (``YYYY-MM``). Override to key on creation date, fiscal period, tenant, etc."""
        return _utcnow().strftime("%Y-%m")

    async def case_run_blocking(self, fn, /, *args, **kwargs):
        """OPT-IN escape hatch for a SYNCHRONOUS/blocking call inside a `perform_`. Runs `fn`
        on the default thread-pool executor and awaits it, so the call does NOT freeze the
        event loop (which would stall every other case sharing it). Use ONLY when a library
        gives you no async API:

            async def perform_fetch(self, tctx):
                resp = await self.case_run_blocking(requests.get, url)

        SECOND-CLASS by design, with two caveats vs. an async-native client:
          * Concurrency is bounded by the executor's thread pool (not the ~unbounded
            concurrency of real async I/O), so blocking calls do not scale the same way.
          * The hard-abort (TriggerTimeout) cancels the AWAIT, but a running thread cannot be
            killed — the worker keeps going until `fn` returns on its own. So a true hang here
            frees the case but leaks the thread. Prefer an async client for anything that can
            hang."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    # ---- reclassify ("call an audible" to a different subclass) ----

    def case_reclassify_to(
        self, new_cls: type[FolderBackedCase]
    ) -> FolderBackedCase:
        """Rebind this case to a different FolderBackedCase subclass via a two-phase
        COMMIT, logging a CASE_RECLASSIFIED event. The CALLER owns compatibility.

        Advanced:
          Use when a case must change its TYPE mid-life (e.g. a generic intake becomes a
          specialized workflow) while keeping its folder, id, and history. The current
          state must be a valid state of `new_cls` or IncompatibleReclassError is raised.

        Maintainer notes:
          Two-phase commit (crash-atomic):
            Phase 1 — snapshot old repr in the OLD class's schema; detach (free the lease).
            Phase 2 — NEW class acquires the now-free lease, CONSCIOUSLY stamps its
                      own name, force-commits in its own schema.
          A crash between phases reopens cleanly as the OLD class (name not switched)."""
        if self.case_state not in new_cls._fsm.states:
            raise IncompatibleReclassError(self.case_state, new_cls.__name__)
        from_name = self._record.case_object_type
        self._flush_record(force=True)           # phase 1: snapshot old repr (old name)
        self.case_detach()                       # serialize-then-DETACH, then re-acquire
        # Bind without the type gate: the old name is still on disk until phase 2 stamps the
        # new one. __new__ + _bind_existing_case_dir bypasses __init__'s gated public path.
        fresh = new_cls.__new__(new_cls)
        fresh._bind_existing_case_dir(self._folder, check_type=False)
        fresh._journal.log_reclassified(
            new_cls.__name__, from_type=from_name, at_state=fresh.case_state
        )
        fresh._record.case_object_type = new_cls.__name__   # CONSCIOUS stamp
        fresh._record.asset_aliases = type(fresh)._resolve_asset_book().to_record()
        fresh._record.fsm_state_chains = list(type(fresh).fsm_state_chains)
        fresh._flush_record(force=True)                      # phase 2: commit new name + schema
        type(fresh)._seed_keep_rules(fresh.case_assets)
        for fn in self._listeners:
            fresh.case_add_transition_listener(fn)
        return fresh

    # ---- lifecycle-signal subscription ----

    def case_add_transition_listener(self, fn) -> None:
        """Subscribe to post-transition notifications: ``fn(case, event_name, info)``.

        Planned ``CaseManager`` (draft: notebooks/DEVDAVE/case_manager_classes/CaseManager
        Model.md) uses this for archival without the case knowing the manager.
        """
        self._listeners.append(fn)

    # =======================================================================
    # SECTION 4 — Internal mechanics (maintainers)
    # -----------------------------------------------------------------------
    # Construction/binding, the FSM state-change and exception choke points,
    # record flush, the pipeline candidate finder, and other private machinery.
    # Read this to MAINTAIN the class; you should not need it to USE it.
    # =======================================================================

    # ---- FSM compilation trigger (parse-once-at-class-definition) ----

    # Compiled per-class FSM (private): populated in __init_subclass__ via compile_fsm()
    # (the overridable default lives in SECTION 3). Empty on the base until a subclass
    # supplies chains.
    _fsm: FsmChainSpec = FsmChainSpec.empty()

    # The base's RESERVED instance call-surface: names that back core machinery and must
    # never be shadowed by a subclass. The binding check (validate_object_compatibility,
    # passed these via _bind_existing_case_dir) fails fast if a subclass redefines one in
    # its body, so descendants are free to use short names everywhere else. Deliberately
    # excludes the override SEAMS (compile_fsm, generate_case_id, on_terminating, case_dwell_secs,
    # ...) — those are MEANT to be overridden — and the hook-name conventions
    # (perform_/before_/after_/on_enter_/on_exit_/guard_), which belong to the subclass.
    _SEALED_MEMBER_NAMES: frozenset[str] = frozenset({
        "case_state", "case_folder", "case_assets", "case_nickname", "case_external_key",
        "case_is_live", "case_is_terminal", "case_transition_fail_count", "case_id",
        "case_advance", "case_detach", "case_heartbeat", "case_fetch_record",
        "case_log_alert", "case_run_blocking", "case_reclassify_to",
        "case_add_transition_listener",
    })

    def __init_subclass__(cls, **kwargs) -> None:
        # Parse + validate at class-definition time: fail-fast (a malformed chain blows
        # up at import, not first instantiation) and performant (compiled once, not per
        # instance). The result is the shared per-class FSM singleton.
        super().__init_subclass__(**kwargs)
        cls._require_fsm_trigger_chokes_declared()
        cls._fsm = cls.compile_fsm()
        if cls.asset_aliases is not FolderBackedCase._ASSET_ALIASES_NOT_DECLARED:
            cls._asset_book = AliasedAssetSpecs.from_declaration(
                cls.asset_aliases, flexible=cls.flexible_asset_alias_loading,
            )
            if not cls._asset_book.aliases():
                logger.warning(
                    "%r declares asset_aliases but the alias set is empty — no "
                    "protocol-elevated data objects are registered for cross-process trust.",
                    cls.__name__,
                )
        if cls._fsm.primary_chain is not None:
            logger.debug(
                "FSM for %s: chains compiled (primary=%r, initial=%r, initial_states=%s, "
                "terminal=%s, auto-advance=%s)",
                cls.__name__, cls._fsm.primary_chain, cls._fsm.initial_state,
                sorted(cls._fsm.initial_states), sorted(cls._fsm.terminal_states),
                cls._fsm.pipeline,
            )

    # ---- ID generation state ----

    # Monotonic clock for in-process case_id minting (see generate_case_id in SECTION 3).
    _last_generated_case_id_ms: int = -1

    # ---- construction / binding ----

    def __init__(self, case_folder: Path):
        """Bind a live case object to an EXISTING on-disk case folder.

        This constructor is intentionally the load/bind path, not inception:
        it expects `case_record.yaml` (and any existing event-log history) to
        already exist on disk, then loads them, acquires the lease, and builds
        the in-memory FSM carrier. Call ``case_detach()`` when you are done with
        this live object. Two ways it fails fast and points elsewhere:
          * The folder is not an initialized case (no record) -> FileNotFoundError
            naming ``create_case_in_folder()`` and ``case_type_registry.rehydrate(folder)``.
          * The record names a DIFFERENT case type than this class -> the
            CaseTypeMismatchError gate.

        Brand-new cases are created via `create_case_in_folder(...)`, which first
        materializes the folder + record (minting `case_id` via
        `generate_case_id()` when needed), then immediately calls this
        constructor to attach the live object.

        Retention at close is manifest-driven: ``_keep.txt`` at the case root lists
        every file that survives purge. Framework artifacts are seeded automatically;
        subclasses must call ``case_add_keep_rules()`` (or ``case_assets.add_keep_rules()``
        for assets) for any custom files they want retained."""
        self._bind_existing_case_dir(case_folder, check_type=True)

    def _bind_existing_case_dir(
        self, case_folder: Path, *, check_type: bool = True
    ) -> None:
        """Load an existing case folder and bind this live object to it.

        Shared by ``__init__`` (``check_type=True``) and ``case_reclassify_to``
        (``check_type=False`` during the two-phase type switch).
        """
        # Run config guards BEFORE any disk/lease I/O so misconfigured classes fail cleanly.
        cls = type(self)
        # 1) Empty FSM is legal on base/abstract classes, so this check belongs here.
        if not cls._fsm.states:
            raise MissingFsmError(cls.__name__)
        # 2) One-time carrier binding check, keyed on cls.__dict__ so subclasses don't
        # inherit a parent's "already checked" sentinel. Uses the method's default
        # orphan_detection="error": a hook/guard-looking method that maps to no known
        # state/trigger/guard is treated as a typo and fails the build.
        if "_fsm_binding_checked" not in cls.__dict__:
            cls._fsm.validate_object_compatibility(
                self,
                sealed_names=FolderBackedCase._SEALED_MEMBER_NAMES,
                sealed_owner=FolderBackedCase,
            )
            cls._resolve_asset_book().validate_against_fsm(
                cls._fsm, flexible=cls.flexible_asset_alias_loading,
            )
            cls._fsm_binding_checked = True
        self._folder = Path(case_folder)
        # Uninitialized-folder gate: missing record means create_case_in_folder() or
        # case_type_registry.rehydrate(folder) was intended instead.
        record_path = self._folder / RECORD_NAME
        if not record_path.exists():
            raise FileNotFoundError(
                f"No case record at {record_path}: this folder is not an initialized case. "
                "Use create_case_in_folder() to incept a new case, or "
                "case_type_registry.rehydrate(folder) to open an existing one by type."
            )
        self._lease: HeartbeatLease | None = None
        # without_lock=True: the case lease is our single-owner mechanism; the mixin's file
        # lock is redundant for reads and would block re-opens. save() still acquires its
        # own short-lived lock per write.
        self._record: CaseRecord = self._record_cls.open(
            str(record_path), without_lock=True
        )
        # Wrong-type gate (before the lease, so a reject claims nothing): this class is not
        # the one the record names. case_reclassify_to passes check_type=False to build over the
        # old name on purpose; every other caller gets the gate.
        if check_type and self._record.case_object_type != cls.__name__:
            raise CaseTypeMismatchError(
                on_disk=self._record.case_object_type, loading_class=cls.__name__
            )
        self._journal = CaseJournal.for_folder(self._folder)
        self._keep_manifest = CaseKeepManifest(self._folder)
        self._keep_manifest.ensure_framework_rules()
        self._assets = CaseAssets(
            self._folder,
            asset_specs=type(self)._resolve_asset_book().spec_map(),
            flexible_asset_alias_loading=cls.flexible_asset_alias_loading,
            keep_manifest=self._keep_manifest,
        )
        self._listeners: list = []        # fn(case, event_name, info)
        # State is derived from the event log on load; transitions then cache on
        # self.case_state (the machine's model_attribute).
        self.case_state: str = self._derive_state() or self._fsm.initial_state
        # Event-log mtimes are LOCAL naive (datetime.fromtimestamp); _as_utc() converts them
        # to aware UTC. record.created is already aware UTC (CaseRecord validator).
        self._last_activity: datetime.datetime = (
            self._as_utc(self._journal.last_activity) or self._record.created
        )
        # When the CURRENT state was entered — dwell anchor for @DWELL guards,
        # from the latest CASE_STATE_ENTERED; a brand-new case has none yet, so fall back.
        self._state_entered_at: datetime.datetime = (
            self._as_utc(self._journal.last_state_entered_mtime()) or self._record.created
        )
        # The lease TTL is a single fixed crash-recovery window (see constants.py); the
        # provider is a constant function, not a per-state policy.
        self._lease = HeartbeatLease(
            self._folder / LEASE_NAME,
            ttl_provider=lambda: DEFAULT_LEASE_TTL_SECS,
        )
        try:
            self._lease.acquire()
        except LeaseAlreadyHeldError as e:
            raise CaseAlreadyOpenError(self._folder, expires_in=e.expires_in) from e
        # Per-case folder-logging tee (always on). Set up only AFTER the lease is held, so we
        # never write into a folder owned by another process. The state provider is
        # weakref-based so the per-instance logger never pins its owning case, and the
        # close-after-write handler holds no persistent fd (thousands of live cases cost ~0
        # open descriptors at rest).
        _case_ref = weakref.ref(self)
        self.log = build_case_logger(
            self._record.case_id,
            self._folder / LOGS_DIR_NAME / LOG_FILE_NAME,
            case_object_type=type(self).__name__,
            state_provider=lambda: (c := _case_ref()) and c.case_state,
        )
        write_attach_banner(self.log)
        # Non-reentrancy guard (see _on_prepare_fsm_event / CaseTransitionInFlightError):
        # at most one FSM trigger invocation may be in flight on this object at a time.
        # Reset here so a freshly (re)bound instance — including case_reclassify_to's
        # fresh object — never inherits a stale flag.
        self._transition_in_flight: bool = False
        self._active_trigger_name: str | None = None
        # Instance-time machine binding is delegated to _CaseMachineFactory.
        self._machine = _CaseMachineFactory(self, self._fsm, self._journal).build(self.case_state)

    @staticmethod
    def _as_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
        """Read a naive (local) event-log mtime as aware UTC; pass None through."""
        return dt.astimezone(datetime.timezone.utc) if dt is not None else None

    def _derive_state(self) -> str | None:
        """Current state = the most recent CASE_STATE_ENTERED entry. Delegates to the
        journal (over the same CaseEventLogReader the peek path uses) — no-drift
        guarantee is structural."""
        return self._journal.current_state

    def _notify(self, event_name: str, **info) -> None:
        for fn in self._listeners:
            fn(self, event_name, info)

    # ---- FSM attachment via transitions (async-first composition pattern) ----
    # Machine construction + callback wiring live in _CaseMachineFactory; the case keeps
    # the override seams the factory reads back (trigger_warn_secs, case_dwell_secs) and the
    # named lifecycle callbacks the machine binds (_on_state_changed, _on_fsm_exception,
    # _on_prepare_fsm_event, _on_finalize_fsm_event).

    def _on_prepare_fsm_event(self, event_data) -> None:
        """Machine `prepare_event` hook — the single chokepoint EVERY trigger call passes
        through before any guard/condition runs (covers case_advance() and a direct
        `await case.<trigger>()` alike). Enforces the non-reentrancy invariant: at most
        one FSM event may be in flight on this object at a time.

        DELIBERATELY SYNCHRONOUS (no `await` before the check-and-set): the `transitions`
        library runs this to completion without yielding the event loop, so the
        check-and-set below is atomic with respect to any OTHER concurrent trigger call
        on this same object — whichever call's prepare hook actually executes first
        always wins, regardless of scheduling order. See CaseTransitionInFlightError.

        Marks `event_data` (not just `self`) as "mine to clear" via `_case_guard_owner`:
        `finalize_event` below ALWAYS fires exactly once per trigger() call — even for a
        call rejected before prepare_event ever ran (e.g. the trigger has no edge from
        the CURRENT state) — so finalize must only clear the flag for the call that
        actually set it, or it could wrongly release a flag held by a genuinely in-flight
        transition."""
        if self._transition_in_flight:
            raise CaseTransitionInFlightError(
                self.case_id, self._folder, self._active_trigger_name
            )
        self._transition_in_flight = True
        self._active_trigger_name = (
            event_data.event.name if event_data.event is not None else None
        )
        event_data._case_guard_owner = True

    def _on_finalize_fsm_event(self, event_data) -> None:
        """Pairs with `_on_prepare_fsm_event` — see that method for why the ownership
        check (rather than an unconditional clear) matters."""
        if getattr(event_data, "_case_guard_owner", False):
            self._transition_in_flight = False
            self._active_trigger_name = None

    def _on_state_changed(self, event) -> None:
        """Runs after EVERY transition (event is a transitions EventData). Records the
        state-change entry, then on non-terminating transitions throttled-flushes the record
        and beats the lease. On the non-terminal → terminal EDGE, runs the two-phase termination.

        Two-phase termination (CASE_TERMINATING / CASE_TERMINATED distinction):
          Phase 1 — PRE-FINALIZATION (assets still exist):
            1. Log CASE_TERMINATED event
            2. on_terminating() — subclass retains/extracts final artifacts
            3. _notify("CASE_TERMINATING") — pre-purge observers (audit, test harness)
          Phase 2 — POST-FINALIZATION (immutable, still BOUND):
            4. _keep_manifest.purge() — drop everything not matched in _keep.txt
            5. _record.terminal stamped + FORCE-flushed (authoritative seal)
            6. heartbeat(force) — keep the lock fresh; termination does NOT detach (the
               object stays bound so owners can harvest before calling case_detach())
            7. _notify("CASE_TERMINATED") — finalized-but-still-bound; the "safe to move"
               signal is case_detach(), not this. Standalone: no-op.
        """
        src, dest = event.transition.source, event.transition.dest
        trigger = event.event.name if event.event is not None else None
        self._journal.log_state_entered(dest, trigger=trigger, from_state=src)
        self._last_activity = _utcnow()
        self._state_entered_at = self._last_activity   # reset the time-guard dwell anchor
        terminating = src not in self._fsm.terminal_states and dest in self._fsm.terminal_states
        if not terminating:
            # Throttled flush + lease beat at the boundary. Skipped on the terminating edge:
            # the forced phase-2 seal supersedes the flush, and phase 2 beats explicitly.
            self._flush_record()
            self.case_heartbeat()
        else:
            # --- phase 1: pre-finalization --- assets still present ---
            self._journal.log_terminated(dest, from_state=src)
            self.on_terminating()
            self._notify(SIG_TERMINATING, src=src, dest=dest)
            # --- phase 2: post-finalization --- assets gone, record sealed ---
            self._keep_manifest.purge()
            # The case logically ends here; apply the termination log-retention policy alongside
            # the asset purge. PURGE rewrites logs/case.log with a single sentinel line.
            if get_case_log_retention() is LogRetention.PURGE:
                purge_case_log(self._folder / LOGS_DIR_NAME / LOG_FILE_NAME)
            self._record.terminal = self._last_activity
            self._flush_record(force=True)
            # Termination keeps the lock; it does NOT detach. Force a fresh beat so the now-idle
            # (un-advanced) terminal case holds a full-TTL grace window for owners to harvest
            # before they call case_detach(). A crash still lapses the lock via the TTL.
            self.case_heartbeat(min_update_secs=0)
            self._notify(EV_TERMINATED, src=src, dest=dest)

    async def _on_fsm_exception(self, event) -> None:
        """Machine-level `on_exception` hook (wired by the machine factory): the SINGLE chokepoint
        every trigger dispatch funnels through, so it covers both case_advance() and a direct
        `await case.<trigger>()`. Fires when ANY callback raises — a guard, a
        `perform_<trigger>`, on_exit/on_enter, or an `after`.

        It distinguishes the COMMIT BOUNDARY without needing to know which callback slot
        raised: `transitions` sets `self.case_state` to the dest during the state change, BEFORE
        on_enter/after run, so `self.case_state == dest` means we are POST-commit.

        Steps, in order:
          1. NO-DRIFT REMEDY (post-commit only): if we advanced in memory but the durable
             CASE_STATE_ENTERED write never ran (the after_state_change writer was skipped by
             the raise), write it now so the on-disk log can never lag in-memory state.
          2. DECORATE the exception with structured `case_context` (case_id, trigger,
             source/dest, commit phase) so a type-agnostic driver can branch without parsing
             messages — attached HERE so it travels regardless of how the trigger was fired.
          3. LOG a terse, COUNTABLE failure fact (NOT a CASE_ALERTED): CASE_TRANSITION_FAILED for
             a pre-commit failure (the kind `@FAIL` counts and retries re-attempt), or
             CASE_ENTRY_EXCEPTION for a post-commit entry-hook raise (it DID enter; logged
             and hooked, but NOT counted by @FAIL). A pre-commit TriggerTimeout is logged as
             CASE_TRIGGER_TIMED_OUT instead — visually distinct, still @FAIL-counted.
          4. Call on_transition_exception(...) so the case may compensate.
          5. RE-RAISE the original exception. case_advance() catches it and folds it into an
             AdvanceResult; a direct caller gets the raise (fail-fast preserved)."""
        err = event.error
        # Lost the folder to another owner mid-step (raised by the keepalive pulse): a fatal
        # invariant breach, not a transition failure. Surface it WITHOUT logging a fail or
        # counting it toward @FAIL — the displaced owner must simply stop operating.
        if isinstance(err, OwnershipLostError):
            raise err
        # Reentrancy rejection from _on_prepare_fsm_event: raised BEFORE any guard/condition
        # ran for THIS call, so there is nothing here to decorate or log — no transition was
        # even attempted. Surface it exactly like OwnershipLostError: a misuse/invariant
        # signal, not a transition outcome (never folded into AdvanceResult; see
        # CaseTransitionInFlightError and _attempt_one_trigger's matching re-raise).
        if isinstance(err, CaseTransitionInFlightError):
            raise err
        trigger = event.event.name if event.event is not None else None
        trans = event.transition
        src = trans.source if trans is not None else self.case_state
        dest = trans.dest if trans is not None else None
        post_commit = dest is not None and self.case_state == dest

        # 1. No-drift remedy. NOTE (sharp edge): if dest is a TERMINAL state, the two-phase
        # termination in _on_state_changed was also skipped here; we reconcile the
        # CASE_STATE_ENTERED but do NOT attempt termination from inside the exception
        # handler. Terminal-entry failures still need the termination path made
        # idempotent/re-runnable.
        if post_commit and self._journal.current_state != dest:
            self._journal.log_state_entered(dest, trigger=trigger, from_state=src)
            self._last_activity = _utcnow()
            self._state_entered_at = self._last_activity

        # 2. Decorate.
        context = {
            "case_id": self.case_id,
            "trigger": trigger,
            "source": src,
            "dest": dest,
            "phase": "post_commit" if post_commit else "pre_commit",
        }
        try:
            err.case_context = context        # best-effort; some exceptions forbid attrs
        except (AttributeError, TypeError):
            pass

        # 3. Log the countable failure fact. A TriggerTimeout is pre-commit by construction
        # (the work runs in `before`), but gets its OWN distinct label so a timeout never
        # reads like an ordinary transition failure — while count_fails_this_dwell still
        # counts it.
        detail = {"trigger": trigger, "source": src, "dest": dest,
                  "error": type(err).__name__, "msg": str(err)[:200]}
        if isinstance(err, TriggerTimeout):
            self._journal.log_trigger_timed_out(trigger or src, detail)
        elif post_commit:
            self._journal.log_entry_exception(dest or src, detail)
        else:
            self._journal.log_transition_failed(trigger or src, detail)

        # 4. Let the case react. final_state reflects where we actually ended up.
        final_state = dest if post_commit else src
        try:
            self.on_transition_exception(src, trigger, final_state, err)
        except Exception:                     # a misbehaving hook must not mask the real error
            self.log.exception("on_transition_exception hook raised for case %s", self.case_id)

        # 5. Re-raise so case_advance() can fold it in (and direct callers stay fail-fast).
        raise err

    # ---- record flush protocol (single guarded chokepoint) ----
    # Reads use without_lock=True (open() would otherwise hold a lock for the
    # instance lifetime, which conflicts with our single-owner lease model).
    # Writes use the mixin's save() as normal — it acquires a brief transient
    # lock only for the duration of the write and releases immediately after.
    # The public read companion, case_fetch_record(), lives in SECTION 2.

    def _flush_record(self, *, force: bool = False) -> None:
        """Persist the owned record. Default is THROTTLED — writes only when
        the in-memory record differs from its last-saved snapshot; force=True
        writes unconditionally (the authoritative seal at create/close/reclassify).
        A class may only ever write its OWN name: case_object_type must equal
        type(self).__name__ or RecordTypeMismatchError is raised.
        ALL live-instance record writes MUST funnel through here for the guard."""
        expected = type(self).__name__
        if self._record.case_object_type != expected:
            raise RecordTypeMismatchError(self._record.case_object_type, expected)
        if force or self._record.is_modified():
            self._record.save()

    # Lease TTL is a fixed crash-recovery window (constants.py). Idle holders must
    # detach, heartbeat, or delegate to a planned CaseManager (draft: notebooks/DEVDAVE/
    # case_manager_classes/CaseManager Model.md) for fleet-wide keepalive.

    def _check_active(self) -> None:
        if self.case_is_detached:
            raise DetachedCaseError(self._folder)

    def __del__(self):
        try:
            self.case_detach()
        except Exception:
            pass

    # ---- flat pipeline driver (internals behind case_advance()) ----

    def _forward_candidates(self, state: str):
        """The auto-advance edges leaving `state`, as (trigger, dest) in declared order.
        Empty when terminal / nothing auto-advances from here (e.g. awaiting input). With
        guards, more than one candidate may be eligible; case_advance() tries them in order."""
        return self._fsm.auto_edges_from(state)

    def _has_edge_from(self, state: str, trigger: str) -> bool:
        """Is `trigger` an edge (auto OR manual) leaving `state`? Unlike _forward_candidates
        (auto only), this also sees MANUAL (`==`) edges. case_advance()'s pinned-trigger path
        uses it to tell 'real trigger but not available from here' (a plain non-advance) apart
        from 'not a trigger at all' (misuse that raises) — the latter checked against
        self._fsm.triggers before this."""
        for t in self._fsm.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if state in srcs and t["trigger"] == trigger:
                return True
        return False

    def _make_blocked(self, candidates) -> AutoAdvanceBlocked:
        """Build the AutoAdvanceBlocked marker for the current (provably stuck) state,
        logging a SINGLE CASE_ALERTED the first time we detect the block in this dwell so it
        is visible on disk without spamming the low-volume log on every case_advance() call."""
        if not self._journal.has_event_since_enter(EV_ALERTED):
            self.case_log_alert(f"auto-advance blocked in {self.case_state!r}", where=self.case_state)
        return AutoAdvanceBlocked(
            self.case_id, self.case_state, candidates=[t for t, _ in candidates]
        )

    # ---- stall handling (why there's no self-pulse: see case_advance()'s docstring) ----
