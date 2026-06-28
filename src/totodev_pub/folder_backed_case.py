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
CaseRecord          — skinny Pydantic identity card (case_record.yaml).
CaseEventLogReader  — read-oriented convention interpreter over PrimitiveEventLog.
CaseJournal         — domain-aware event-log read/write facade (the case's one log surface).
CaseAssets          — working-file playground + retention manifest (_keep_assets.txt).
FolderBackedCase    — ABC you subclass to define a case type.
StateChainParser    — authoritative source for the state-chains DSL: parser/compiler and
                      validation logic used by FolderBackedCase.compile_fsm().

The tightly-coupled supporting classes live in the folder_backed_case_support
package and are re-exported here for convenience. This facade exposes only the
case's OWN surface — what a case is composed of, returns, and raises. Layers that
sit ABOVE the individual case are NOT re-exported and must be imported from their
own modules: name-driven resolution lives in CaseTypeRegistry / case_type_registry
(folder_backed_case_support.case_type_registry), and the case-driving/scheduling
seam lives in CasePoolDriver (folder_backed_case_support.case_pool_driver).

Quick start
-----------
    class TicketCase(FolderBackedCase):
        fsm_state_chains = ["^new--open_ticket-->open==close_ticket-->closed^"
                            "*--@DWELL>14d#non_responsive-->auto_closed^"]  # timed-escape edge

        async def perform_open_ticket(self, tctx) -> None:
            # do something like log the ticket, contact external systems, etc.
            # is called by the open_ticket() trigger

        async def perform_close_ticket(self, tctx) -> None:
            # do something like log the ticket, contact external systems, etc.
            # is called by the close_ticket() trigger

        async def on_enter_closed(self, tctx) -> None:
            # do something like notify the customer that their ticket has been closed

        # Every hook takes the trigger context `tctx` after `self`; see "Creating Hook
        # Functions" in the class docstring for what `tctx` is and how it is populated.
     

    from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
    case_type_registry.register_case_types(TicketCase)

    case = TicketCase.create_case_in_folder(Path("/data/cases/t-001"), case_id="t-001")
    with case:
        await case.open_ticket()
        await case.close_ticket()
    # detached by the context manager on exit (lease cleared); folder is self-contained

    # Reopen later without knowing the concrete class:
    with case_type_registry.rehydrate(Path("/data/cases/t-001")) as case:
        ...
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import logging
import time
from abc import ABC
from pathlib import Path

from totodev_pub.folder_backed_case_support.constants import (
    RECORD_NAME, LEASE_NAME, EVENTS_DIR_NAME, ASSETS_DIR_NAME, KEEP_LIST_NAME,
    CASE_RESERVED_ARTIFACT_NAMES, CASE_BASE_EVENT_PREFIX,
    DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS, LEASE_PULSE_FRACTION_DIVISOR,
    EV_CLOSED, EV_ALERT, SIG_CLOSING,
)
from totodev_pub.folder_backed_case_support.helpers import _utcnow, _new_time_slug
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError, OwnershipLostError, DetachedCaseError,
    CaseTypeMismatchError, RecordTypeMismatchError,
    IncompatibleReclassError, MissingFsmError, FsmChainParseError, FsmBindingError,
    AutoAdvanceBlocked, TriggerTimeout,)
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_journal import CaseJournal
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.heartbeat_lease import (
    HeartbeatLease, LeaseAlreadyHeldError, LeaseOwnershipLostError,)
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    StateChainParser, FsmChainSpec,)
from totodev_pub.folder_backed_case_support.case_machine_factory import _CaseMachineFactory


logger = logging.getLogger(__name__)

__all__ = [
    "FolderBackedCase", "HeartbeatLease",
    "CaseRecord", "CaseEventLogReader", "CaseJournal", "CaseAssets", "AdvanceResult",
    "StateChainParser", "FsmChainSpec", "CaseAlreadyOpenError", "OwnershipLostError",
    "DetachedCaseError", "CaseTypeMismatchError",
    "RecordTypeMismatchError", "IncompatibleReclassError", "MissingFsmError",
    "FsmChainParseError", "FsmBindingError", "AutoAdvanceBlocked", "TriggerTimeout",
    "RECORD_NAME", "LEASE_NAME", "EVENTS_DIR_NAME", "ASSETS_DIR_NAME", "KEEP_LIST_NAME",
    "CASE_RESERVED_ARTIFACT_NAMES", "CASE_BASE_EVENT_PREFIX",
]


# ---------------------------------------------------------------------------
# FolderBackedCase — the logic base class
# ---------------------------------------------------------------------------

class FolderBackedCase(ABC):
    """ions`.
    That's deliberately been made possible but beyond the scope of this documentation.

    How this class is organized
    ---------------------------
    The class body is laid out in four labeled sections so each kind
    of reader can jump straight to what they need:

      * SECTION 1 — START HERE: define your case type. The declarative
        FSM input (`fsm_state_chains`) and the hook/guard naming
        conventions. EVERY developer starts here, usually before writing
        any Python; the ~80% case needs nothing else to define a type.

    Base class for all folder-backed case types.

    Provides: case folder (record + event log + assets), async FSM via
    `transitions`, a flat pipeline driver, ephemeral-file retention,
    the two-phase closing-edge hook, and a single-owner heartbeat lease.

    Subclasses declare fsm_state_chains to explain the states,
    transitions, and guards that their "case" will obey. A trivial
    example:

        fsm_state_chains = ["^new--assign-->assigned==work-->closed^"]

    The names appearing in fsm_state_chains are used to automagically
    wire up "hook" methods that you may optionally declare in your
    subclass (see below). They also allow declaring a small amount of
    error and aging-based flow-control (eg. '@FAIL>3#', '@DWELL>7d#').
    Hook methods all have the same signature.

    Based on the names provided in your fsm_state_chains, instance
    initialization will search for corresponding methods in your
    subclass. If found, they are wired up.
       - `async def on_enter_<state>(self, tctx)` 
       - `async def on_exit_<state>(self, tctx)` 
       - `async def perform_<trigger>(self, tctx)` 
            - `async def before_<trigger>(self, tctx)` 
            - `async def after_<trigger>(self, tctx)` 
       - `async def guard_<guard>(self, tctx)` 

    Note that these are built almost directly on the behavior and logic of the `transitions` library.
    Examine their documentation for semantics and timing.  Note that raising an exception in the trigger, a guard, or the before_*
    will abort a tranition and counts as a "transition fail".

    Guards
    ------
    Guards are a way to gate the firing of a trigger.  They are declared
    in the fsm_state_chains using the `guard#trigger` DSL segment.  They
    are declared as a method in your subclass with the name `guard_<guard>`.
    The method must return a boolean value.  If the method returns False,
    the trigger will not fire.  Guards should be fast, idempotent, and side-effect free.
    Guards may be called many many times in automated loops awaiting transitions.

    One exception that is permitted is if the guard yields with an await,
    async sleep, or other async operation.  Implementers should be aware
    that this will block transitions on that case objevct until the async operation completes,
    but it technically permitted.  Use this with caution.

    Two special purpose guards are provided by the library:
      * @FAIL(>|>=|<|<=)n# - allows proceeding only if the number of transition fails
        in since arriving in the current state is greater/less/etc. than the given number.
      * @DWELL(>|>=|<|<=)dur# - a guard that returns True if the number of seconds
        since the current dwell started is greater/less/etc. the given duration.  Units
        are permitted to be s/m/h/d.  Float values are permitted.
    
    By default, all transitions have an implied guard of `@FAIL<1#` unless explicitly overridden.
    This means that when a trigger fails in a given state, no other trigger may exit the
    state except those who have an explicit guard permitting failures. 


    Any State Triggers
    ------------------

    The event Chains notation supports a wildcard source:
       `*--guard#trigger-->X` 
       
    which means from any source, the trigger may move it to state X.
    A common use of this is combining it with the @FAIL or @DWELL guards
    to create error or aging based flow control.
       `*--@FAIL>3#abort-->aborted`
       `*--@DWELL>10d#timeout-->expired`


    Creating Hook Methods - Passing Arguments to Triggers
    -----------------------------------------------------
    The machine hands EVERY hook a single trigger-context argument,
    conventionally named `tctx`.

      * Although the `transitions` library calls it the EventData object
        (a property bag of whatever was passed to the trigger method) we
        refer to it as the trigger context `tctx`, not `event`. By doing
        this we hope to avoid confusion with the event log which is part
        of the FolderBackedCase class itself.
      * When you call a trigger method directly, you may choose to pass
        arguments that are bundled up into this tctx object. These
        arguments are then available in the hook methods as
        `tctx.kwargs`.
      * Note that when triggers are fired by invocation of the
        case_advance() method, the tctx.kwargs is empty. This means that if
        your code uses AUTO edges ('--') then you won't be seeing any
        tctx.kwargs passed to your hook methods.

    Very very advanced users of this library may need the full power of
    `transitions` library.  That's deliberately been made possible but
    beyond the scope of this documentation.

      * SECTION 2 — Quick-start runtime API. The mainstream "do the work"
        surface: create/open, the context-manager + `case_detach()`,
        `case_advance()` (the one-step driving primitive), identity & status properties,
        `case_assets`, `case_fetch_record()`, `case_log_alert()`, and the lock-free
        `peek_*` inspectors. ~80% of users need only Sections 1 and 2.
      * SECTION 3 — Customization seams. Overridable hooks and policy
        knobs for the peculiar use case: the rarely-needed DEFINE-TIME
        seams (`_record_cls` for a custom record schema, `compile_fsm()`
        for full manual FSM control), recovery hooks, `generate_case_id()`,
        lease/heartbeat tuning, timeout budgets, archive grouping, the
        `case_run_blocking()` escape hatch, and `case_reclassify_to()`.
      * SECTION 4 — Internal mechanics. Construction/binding, the FSM
        state-change and exception choke points, record flush, the
        pipeline candidate finder, and other private machinery. Read
        this to MAINTAIN the class; you should not need it to USE it.
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
    #   state^           trailing `^` = terminal (closing) state
    #   A==trigger-->B   `==` connector = MANUAL edge (fired by `await case.trigger()`)
    #   A--trigger-->B   `--` connector = AUTO edge (fired by case_advance(); a driver loops it)
    #   guard#trigger    binds method `guard_<guard>` as the edge's guard
    #   @DWELL>14d       factual time guard: true once dwell in this state exceeds 14 days
    #   @FAIL>=n         factual guard: true once n failures accrued this dwell
    #   ~<dur>           soft (warning) timeout for the trigger's work
    #   *--...-->X       wildcard source: an edge leaving every state
    # See StateChainParser for the authoritative, complete grammar.
    fsm_state_chains: list[str] = []

    # ---- Hook & guard naming conventions (the rest of "defining a case type") ----
    #
    # After `fsm_state_chains`, behavior is attached purely by METHOD NAME. Suffixes are
    # strict exact matches to parsed state/trigger/guard names (typos fail the build via
    # orphan_detection="error"; opt out per class with orphan_detection="off"):
    #
    #   perform_<trigger>    side-effecting work for a trigger (auto-wired as `before`;
    #                        a raise aborts the step). This is the workhorse hook.
    #   before_<trigger>     extra pre-transition callback for <trigger>.
    #   after_<trigger>      post-transition callback for <trigger>.
    #   on_enter_<state>     fires when entering <state>.
    #   on_exit_<state>      fires when leaving <state>.
    #   guard_<guard>        the boolean gate named by a `guard#trigger` DSL segment.
    #
    # SIGNATURE: every hook/guard above takes the trigger context `tctx` after `self`
    #   (`async def perform_x(self, tctx)`); see "Creating Hook Functions" in the class
    #   docstring for what `tctx` is and how it is populated.
    #
    # WELL-BEHAVED ASYNC: hooks must yield the event loop at reasonable intervals and offload
    #   blocking/CPU-bound work via case_run_blocking() (or their own thread/executor). A hook
    #   that monopolizes the loop starves the other cases a driver is advancing AND the lease
    #   keepalive (see case_advance's Contract note). The keepalive beats the lease only for
    #   the duration of the trigger's WORK slot (perform_/before), which runs AFTER guards
    #   pass; guards and on_enter/on_exit/after are expected to be quick. If a guard must run
    #   pathologically long (e.g. it awaits something), it owns its own self.case_heartbeat().
    #
    # See the class docstring for the full rules. The remaining overridable hooks
    # (on_closing, on_transition_exception, etc.) live in SECTION 3.
    #
    # That is everything the ~80% case needs. Two define-time seams that most case types
    # NEVER touch — a custom record schema (`_record_cls`) and full manual FSM control
    # (`compile_fsm()` / FsmChainSpec) — also live among the customization seams in SECTION 3.

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
        """First-time inception of a brand-new case (shared by CaseManager.create_case()
        and standalone use).

        Quick use:
          Call this to MAKE a new case; use the constructor / rehydrate() to RE-OPEN an
          existing one. Pass a fresh, empty `case_folder`; extra keyword `**fields` flow
          straight onto your `_record_cls`. Returns a live, lease-held case object.

          Common failures:
            * parent folder missing -> FileNotFoundError (parents are not auto-created).
            * the folder already holds case artifacts -> FileExistsError.

        Maintainer notes:
          Path policy is deliberately typo-resistant:
            * If `case_folder` exists, reuse it.
            * If it does not exist, create ONLY that leaf directory.
            * Its parent MUST already exist (no recursive parent creation), so a typo
              cannot silently create a deep stray path.
          Safety guard: the target folder must not already contain any case-owned
          artifacts (`case_record.yaml`, `events/`, `assets/`, `_keep_assets.txt`,
          `.case.lease`). Unrelated files are allowed.
          Then writes the skinny record, constructs (load/bind), and logs the
          lifecycle bookend CASE_NEW plus the initial CASE_ENTER_STATE.
        """
        case_folder = Path(case_folder)
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
            **fields,
        )
        # Direct save (no instance yet) — SAFE BY CONSTRUCTION: case_object_type is set to
        # cls.__name__ here, so it satisfies the _flush_record type-name guard.
        record.save(str(case_folder / RECORD_NAME))
        case = cls(case_folder)
        case._journal.log_new(
            cls.__name__,
            case_id=record.case_id,
            external_key=record.external_key,
        )
        case._journal.log_enter_state(cls._fsm.initial_state)
        return case

    # Re-opening an existing case by its concrete class is the constructor `cls(folder)`;
    # re-opening WITHOUT knowing the class is case_type_registry.rehydrate(folder). Both
    # are documented in SECTION 4 (__init__) and the CaseTypeRegistry, respectively.

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.case_detach()

    def case_detach(self) -> None:
        """Unbind this object from its folder: delete the lease (clearing the conceptual
        lock) and mark this instance detached.

        Quick use:
          Prefer the context manager (`with case:`) which calls this on exit. Call it
          directly when you opened a case without a `with` block. After detach, any
          mutating use raises DetachedCaseError.

        Maintainer notes:
          Idempotent and DELIBERATE — it is NOT a side-effect of FSM closure and does
          NOTHING to the case's assets. This is the single point at which the folder
          becomes fair game for an external owner (a CaseManager) to move/archive: while
          ANY in-memory object remains bound, the lease asserts the folder is spoken-for.
          A closed case stays bound until an owner — who may first harvest non-ephemeral
          data from it — calls this. Wired to __exit__ (context manager) and best-effort
          __del__; on a crash the lease simply expires via its TTL."""
        if self._lease is not None:
            self._lease.release()

    async def case_advance(self) -> AdvanceResult:
        """Fire ONE auto (`--`) forward step from the current state and report the
        outcome as an AdvanceResult (a NON-throwing reporter — see that class).

        Quick use:
          Call repeatedly (a driver loops it across cases) to drive AUTO (`--`) edges. It
          tries each auto-advance candidate in declared order, firing the first whose
          guard permits. Transition failures are REPORTED, not raised: inspect
          `result.progressed` and `result.exceptions` rather than wrapping in try/except.
          (Manual `==` edges are fired directly via `await case.<trigger>()`, which DOES
          raise on failure — that is the other error channel.)

        Maintainer notes:
          Outcomes (all returned, never raised — except the misuse guards below):
            * progressed — a transition fired; result.trigger names it, final_state advanced.
            * a failed attempt — a candidate's work raised; the exception is CAUGHT and
              carried in result.exceptions (decorated + logged + hooked by _on_fsm_exception),
              the case stayed in its source state to be retried next pass.
            * nothing to do — terminal, or every guard declined, with no progress.
            * BLOCKED — when nothing fired/raised AND the state has no timed escape, a
              synthetic AutoAdvanceBlocked is carried in result.exceptions (one CASE_ALERT is
              logged on first detection per dwell). Deterministic: same state, same block.
          Still RAISES the misuse guard DetachedCaseError (acting on a detached husk is a
          programming error, not a flow condition). Also RAISES OwnershipLostError (NOT folded
          into the result) if a beat — the pre-step one below or the in-flight keepalive —
          finds the folder reclaimed by another owner: a fatal invariant breach, not a step
          outcome.

        Contract (well-behaved async hooks):
          The lease keepalive — and cooperative scheduling generally — depends on a trigger's
          work actually YIELDING the event loop. Hooks MUST be well-behaved async: await at
          reasonable intervals and offload blocking/CPU-bound work via case_run_blocking()
          (or their own executor/thread). A hook that monopolizes the loop starves every other
          case sharing it AND its own heartbeat, so the lease can lapse despite the keepalive.
          The keepalive protects only the trigger's WORK slot (perform_/before); guards and
          on_enter/on_exit/after are expected to be light (see the hook conventions)."""
        initial = self.case_state
        if self.case_is_closed:         # terminal short-circuits first (a closed case stays
            return AdvanceResult(initial, self.case_state)   # bound; lease cleared only by case_detach())
        self._check_active()            # else refuse to drive a detached husk
        # Every poll is proof of active ownership, so beat the lease here (throttled — a
        # no-op if < min_update_secs since our last beat). This covers the long-dwelling
        # case that keeps no-opping and never transitions, which _on_state_changed's
        # per-transition beat would otherwise leave to expire. Done BEFORE the step so the
        # lease is fresh going in; the in-flight keepalive (the _LeaseKeepalive pulse in the
        # perform wrapper) then keeps beating for the duration of a slow, awaited step.
        self.case_heartbeat()
        candidates = self._forward_candidates(self.case_state)
        exceptions: list[Exception] = []
        for trigger, _dest in candidates:
            try:
                # A guard-blocked transition returns falsy WITHOUT changing state (and
                # without firing the after-hook), so we fall through to the next.
                if await getattr(self, trigger)():
                    return AdvanceResult(initial, self.case_state, trigger=trigger)
            except OwnershipLostError:  # FATAL: the keepalive (or pre-step beat) found the
                raise                   # folder reclaimed mid-step — not a flow condition to
                                        # fold; surface it exactly like the line-446 beat does.
            except Exception as err:    # absorbed: reported as data, not raised at driver
                exceptions.append(err)
                return AdvanceResult(initial, self.case_state, trigger=trigger,
                                     exceptions=tuple(exceptions))
        # Nothing fired and nothing raised: no forward progress this pass. If the state has
        # no guaranteed timed escape, it is provably auto-advance blocked.
        if self.case_state not in self._fsm.timed_escape_states:
            exceptions.append(self._make_blocked(candidates))
        return AdvanceResult(initial, self.case_state, exceptions=tuple(exceptions))

    # Note: there is deliberately NO run_to_completion()/drive loop on the case itself.
    # A case knows how to take ONE step (case_advance()); deciding WHICH case to drive, in what
    # order, with what fairness/concurrency/backpressure is a SCHEDULER concern that belongs
    # to a driver layer (see CasePoolDriver), not to the domain object. A built-in single-case
    # loop would quietly endorse a one-case-at-a-time fleet model, which is the opposite of
    # the intended round-robin-over-many-cases deployment. Tests that genuinely want to run a
    # single case to its end use the drive_to_completion() helper in the test utilities.


    # ---- at-a-glance attributes (cheap, useful facts about the case) ----
    
    # The bucket of "things worth asking the case about itself at a glance": identity,
    # open/closed status, and the two live facts the built-in guards compare against —
    # `case_dwell_secs` (the `@DWELL` value) and `case_transition_fail_count` (the `@FAIL`
    # value). These are everyday status reads and belong here in the mainstream surface,
    # NOT down among the advanced customization seams where a casual reader never looks.
    #
    # One wrinkle, flagged so it isn't mistaken for an omission: case_state — the case's
    # CURRENT state, the single most at-a-glance fact there is — is a plain public instance
    # ATTRIBUTE, not a @property: the FSM owns it as its model_attribute and writes it
    # directly on every transition, so it can't be a read-only property. Read `case.case_state`.
    #
    # (case_dwell_secs is also an override seam, read back by the factory for the `@DWELL`
    # guard; being a seam is no reason to bury a common read, so it lives here. See its docstring.)

    @property
    def case_id(self) -> str:
        return self._record.case_id

    @property
    def case_external_key(self) -> str | None:
        return self._record.external_key

    @property
    def case_nickname(self) -> str | None:
        return self._record.nickname

    @property
    def case_folder(self) -> Path:
        return self._folder

    @property
    def case_is_open(self) -> bool:
        return self.case_state not in self._fsm.closed_states

    @property
    def case_is_closed(self) -> bool:
        return self.case_state in self._fsm.closed_states

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
        `self._state_entered_at` (the latest CASE_ENTER_STATE, or creation for a brand-new
        case).

        It is ALSO an override SEAM: a subclass may override this property (e.g. to fake the
        clock in tests), and the _CaseMachineFactory reads it back for the `@DWELL` guard."""
        return (_utcnow() - self._state_entered_at).total_seconds()

    # ---- assets (playground + retention), grouped on CaseAssets ----

    @property
    def case_assets(self) -> CaseAssets:
        """The case's CaseAssets: file playground under assets/ plus the keep manifest.

        Quick use:
          Your working files live here. Use case.case_assets.folder, .asset_path(...),
          .relative_path(...), .write(...), .keep_asset(...), .list_assets(), etc.
          Anything not kept via the manifest is purged when the case closes.

        Maintainer notes:
          Kept off this class's own namespace so asset concerns stay grouped in one place."""
        return self._assets

    # ---- record read accessor ----

    def case_fetch_record(self, *, force: bool = False) -> CaseRecord:
        """Public read accessor for the identity record — the read companion to
        _flush_record().

        Quick use:
          Returns a detached deep-copy SNAPSHOT of the record. FAST by default (no disk
          I/O). Mutating or even save()-ing the snapshot can NOT reach back into the case;
          pass force=True to re-read from disk first if another process may have changed it.

        Maintainer notes:
          force=True re-reads the on-disk record IN PLACE, refreshing the case's own copy —
          and any internal holder of that same object — then snapshots that fresh state.
          The returned snapshot is unmapped from any file; _flush_record() remains the one
          sanctioned write path. (The base fetches the record exactly once at construction
          and never silently re-reads — pass force=True if an external process may have
          updated the file.)"""
        if force:
            self._record.reload_from_file(force=True)
        return self._record.detached_copy()

    # ---- operator alert channel (type-agnostic escalation marker) ----

    def case_log_alert(self, short_msg: str = "", *, where: str | None = None) -> None:
        """Record a CASE_ALERT: the case family's single type-agnostic "this case needs a
        human to look at it" marker.

        Quick use:
          Call to flag a case for human attention. Because it reads the same for every
          case type, an observer can surface flagged cases without knowing any internals.
          Use SPARINGLY on the low-volume audit log: raise one for an integrity risk or a
          substantial deviation from norms, NOT for routine, recoverable defects the flow
          absorbs. Orthogonal to the FSM (does not change state or close the case).

        Args:
            short_msg: a brief human-readable reason (terse phrase, not a stack trace).
            where: locus of concern; defaults to the current state.
        """
        self._journal.log_alert(
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
        Exposes current_state, is_closed, last_activity, and .primitive for the raw log."""
        return CaseEventLogReader.for_folder(Path(folder))

    @staticmethod
    def peek_case_assets(folder: Path) -> CaseAssets:
        """A CaseAssets over the folder — lock-free, no live case, no registry. Uniform
        across every case type. Exposes list_assets(), keep_list(), asset_path(), etc.
        The peek analog of a live case's .case_assets property."""
        return CaseAssets(Path(folder))

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> bool | None:
        """Lock-free staleness read for a manager's recovery sweep: True if the lease
        has expired (reclaimable), False if still held, None if unheld. Policy-free —
        the expiry is baked into the mtime, so no state/TTL lookup is needed here."""
        return HeartbeatLease.is_expired(Path(folder) / LEASE_NAME)

    # =======================================================================
    # SECTION 3 — Customization seams (highly-custom developers)
    # -----------------------------------------------------------------------
    # Overridable hooks and policy knobs for the peculiar use case. Everything
    # here ships with a sensible default; override only what you need. None of
    # this is required for the mainstream path in Section 2.
    # =======================================================================

    # ---- Define-time seams (rarely needed; most case types use the defaults) ----

    # The record-type seam: defaults to CaseRecord, so the ~80% case never sets it. It is
    # NOT something you must declare — only override it when you need extra fields: subclass
    # CaseRecord and set `_record_cls = MyRecord`, and every case then carries that schema.
    # Read the live record back via case_fetch_record().
    _record_cls: type[CaseRecord] = CaseRecord

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
        return (
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

    def on_closing(self) -> None:
        """Overridable hook fired in phase 1 (pre-finalization): assets still exist,
        record not yet stamped. Override to retain/extract final artifacts before the
        ephemeral purge. Default: no-op. Heavy async finalization belongs in a `before`
        callback on the closing transition instead — this hook is sync cleanup."""

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

    def lease_ttl_for(self, state: str) -> float:
        """Seconds the lease stays valid after a beat, for `state`. Override per state
        for long-idle windows. MUST comfortably exceed the gap between case_heartbeat() calls
        (i.e. the driver's case_advance() poll cadence + slack), or a live-but-quiet owner can
        be reclaimed."""
        return 300.0

    def lease_pulse_interval_for(self, state: str) -> float:
        """Seconds between in-flight lease beats while a trigger's awaited work
        (`perform_`/`before`) runs, for `state`.

        Maintainer notes:
          The keepalive pulse (see _LeaseKeepalive) beats this often so a long, AWAITED step
          does not let the lease lapse out from under a live owner. The default is a third of
          `lease_ttl_for(state)` — two beats before expiry, so a single missed beat still
          leaves a margin. MUST stay comfortably BELOW `lease_ttl_for(state)`. Return 0 (or a
          non-positive value) to DISABLE the pulse for this state (the pre-step beat then
          remains the only protection, so the lease can lapse during a step longer than the
          TTL — opt out only when no step in this state can outlive the TTL)."""
        return self.lease_ttl_for(state) / LEASE_PULSE_FRACTION_DIVISOR

    def case_heartbeat(
        self,
        *,
        min_update_secs: float = 15.0,
        validate_ownership: bool = True,
    ) -> None:
        """Extend our lease. Raises `OwnershipLostError` when ownership is displaced.

        Quick use:
          The mainstream driver does NOT need to call this — case_advance() beats the lease
          for you. Call it directly only in a custom drive loop that dwells without advancing."""
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

    # The case's other factory-read override seam, case_dwell_secs, is an everyday status read
    # (the `@DWELL` value), so it lives up in SECTION 2's at-a-glance reads. Override it there.

    def archive_grouping_label(self) -> str:
        """Destination archive grouping when this case closes. Default: close month.
        Override to key on creation date, fiscal period, tenant, etc."""
        return _utcnow().strftime("%Y-%m-archive")

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
        COMMIT, logging a CASE_RECLASSIFY event. The CALLER owns compatibility.

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
        fresh._journal.log_reclassify(
            new_cls.__name__, from_type=from_name, at_state=fresh.case_state
        )
        fresh._record.case_object_type = new_cls.__name__   # CONSCIOUS stamp
        fresh._flush_record(force=True)                      # phase 2: commit new name + schema
        for fn in self._listeners:
            fresh.case_add_transition_listener(fn)
        return fresh

    # ---- lifecycle-signal subscription ----

    def case_add_transition_listener(self, fn) -> None:
        """Subscribe to post-transition notifications: fn(case, event_name, info).
        This is how a CaseManager attaches archival behavior without the case having
        any knowledge of the manager. Standalone cases have no listeners."""
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
    # excludes the override SEAMS (compile_fsm, generate_case_id, on_closing, lease_ttl_for,
    # case_dwell_secs, ...) — those are MEANT to be overridden — and the hook-name conventions
    # (perform_/before_/after_/on_enter_/on_exit_/guard_), which belong to the subclass.
    _SEALED_MEMBER_NAMES: frozenset[str] = frozenset({
        "case_state", "case_folder", "case_assets", "case_nickname", "case_external_key",
        "case_is_open", "case_is_closed", "case_transition_fail_count", "case_id",
        "case_advance", "case_detach", "case_heartbeat", "case_fetch_record",
        "case_log_alert", "case_run_blocking", "case_reclassify_to",
        "case_add_transition_listener",
    })

    def __init_subclass__(cls, **kwargs) -> None:
        # Parse + validate at class-definition time: fail-fast (a malformed chain blows
        # up at import, not first instantiation) and performant (compiled once, not per
        # instance). The result is the shared per-class FSM singleton.
        super().__init_subclass__(**kwargs)
        cls._fsm = cls.compile_fsm()
        if cls._fsm.primary_chain is not None:
            logger.debug(
                "FSM for %s: chains compiled (primary=%r, initial=%r, initial_states=%s, "
                "closed=%s, auto-advance=%s)",
                cls.__name__, cls._fsm.primary_chain, cls._fsm.initial_state,
                sorted(cls._fsm.initial_states), sorted(cls._fsm.closed_states),
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
        the in-memory FSM carrier. Two ways it fails fast and points elsewhere:
          * The folder is not an initialized case (no record) -> FileNotFoundError
            naming `create_case_in_folder()` (inception) and `rehydrate()`.
          * The record names a DIFFERENT case type than this class -> the
            CaseTypeMismatchError gate.

        Brand-new cases are created via `create_case_in_folder(...)`, which first
        materializes the folder + record (minting `case_id` via
        `generate_case_id()` when needed), then immediately calls this
        constructor to attach the live object.
        """
        self._bind_existing_case_dir(case_folder, check_type=True)

    def _bind_existing_case_dir(
        self, case_folder: Path, *, check_type: bool = True
    ) -> None:
        """Load an existing case folder and bind this live object to it: run the
        config guards, open the record, (optionally) enforce the case-type gate,
        derive state, acquire the lease, and build the FSM carrier.

        Shared worker behind two entry points:
          * `__init__` calls it with `check_type=True` (the normal public path).
          * `case_reclassify_to` calls it with `check_type=False` on a freshly
            `__new__`-ed instance, because phase 1 deliberately leaves the OLD
            type name on disk until phase 2 stamps the new one.

        Deliberately verbose: the base-class namespace is crowded and inherited
        by every subclass, so internal seams are named to be unmistakable rather
        than terse. Subclasses should never need to call this directly.
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
            cls._fsm_binding_checked = True
        self._folder = Path(case_folder)
        # Uninitialized-folder gate: this is the BIND path, not inception. A missing
        # record means the caller wanted create_case_in_folder() (new) or rehydrate() (by type).
        record_path = self._folder / RECORD_NAME
        if not record_path.exists():
            raise FileNotFoundError(
                f"No case record at {record_path}: this folder is not an initialized case. "
                "Use create_case_in_folder() to incept a new case, or "
                "case_type_registry.rehydrate(folder) to open an existing one by type."
            )
        # Initialized after state load so TTL can depend on current state.
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
        self._assets = CaseAssets(self._folder)   # asset playground + retention manifest
        self._listeners: list = []        # fn(case, event_name, info)
        # State is derived from the event log on load; transitions then cache on
        # self.case_state (the machine's model_attribute).
        self.case_state: str = self._derive_state() or self._fsm.initial_state
        # Event-log mtimes are LOCAL naive (datetime.fromtimestamp); _as_utc() converts them
        # to aware UTC. record.created is already aware UTC (CaseRecord validator).
        self._last_activity: datetime.datetime = (
            self._as_utc(self._journal.last_activity) or self._record.created
        )
        # When the CURRENT state was entered — the dwell anchor for time guards (@<dur>),
        # from the latest CASE_ENTER_STATE; a brand-new case has none yet, so fall back.
        self._state_entered_at: datetime.datetime = (
            self._as_utc(self._journal.last_enter_state_mtime()) or self._record.created
        )
        # Acquire after state is known because TTL can be state-dependent.
        self._lease = HeartbeatLease(
            self._folder / LEASE_NAME,
            ttl_provider=lambda: self.lease_ttl_for(self.case_state),
        )
        try:
            self._lease.acquire()
        except LeaseAlreadyHeldError as e:
            raise CaseAlreadyOpenError(self._folder, expires_in=e.expires_in) from e
        # Instance-time machine binding is delegated to _CaseMachineFactory.
        self._machine = _CaseMachineFactory(self, self._fsm, self._journal).build(self.case_state)

    @staticmethod
    def _as_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
        """Read a naive (local) event-log mtime as aware UTC; pass None through."""
        return dt.astimezone(datetime.timezone.utc) if dt is not None else None

    def _derive_state(self) -> str | None:
        """Current state = the most recent CASE_ENTER_STATE entry. Delegates to the
        journal (over the same CaseEventLogReader the peek path uses) — no-drift
        guarantee is structural."""
        return self._journal.current_state

    def _notify(self, event_name: str, **info) -> None:
        for fn in self._listeners:
            fn(self, event_name, info)

    # ---- FSM attachment via transitions (async-first composition pattern) ----
    # Machine construction + callback wiring live in _CaseMachineFactory; the case keeps
    # the override seams the factory reads back (trigger_warn_secs, case_dwell_secs) and the
    # named lifecycle callbacks the machine binds (_on_state_changed, _on_fsm_exception).

    def _on_state_changed(self, event) -> None:
        """Runs after EVERY transition (event is a transitions EventData). Records the
        state-change entry, then on non-closing transitions throttled-flushes the record
        and beats the lease. On the non-closed → closed EDGE, runs the two-phase close.

        Two-phase closing (CASE_CLOSING / CASE_CLOSED distinction):
          Phase 1 — PRE-FINALIZATION (assets still exist):
            1. Log CASE_CLOSED event
            2. on_closing() — subclass retains/extracts final artifacts
            3. _notify("CASE_CLOSING") — pre-purge observers (audit, test harness)
          Phase 2 — POST-FINALIZATION (immutable, still BOUND):
            4. assets.purge_ephemeral() — drop everything not in the keep manifest
            5. _record.closed stamped + FORCE-flushed (authoritative seal)
            6. heartbeat(force) — keep the lock fresh; closure does NOT detach (the
               object stays bound so owners can harvest before calling case_detach())
            7. _notify("CASE_CLOSED") — finalized-but-still-bound; the "safe to move"
               signal is case_detach(), not this. Standalone: no-op.
        """
        src, dest = event.transition.source, event.transition.dest
        self._journal.log_enter_state(dest)
        self._last_activity = _utcnow()
        self._state_entered_at = self._last_activity   # reset the time-guard dwell anchor
        closing = src not in self._fsm.closed_states and dest in self._fsm.closed_states
        if not closing:
            # Throttled flush + lease beat at the boundary. Skipped on the closing edge:
            # the forced phase-2 seal supersedes the flush, and phase 2 beats explicitly.
            self._flush_record()
            self.case_heartbeat()
        else:
            # --- phase 1: pre-finalization --- assets still present ---
            self._journal.log_closed(dest, from_state=src)
            self.on_closing()
            self._notify(SIG_CLOSING, src=src, dest=dest)
            # --- phase 2: post-finalization --- assets gone, record sealed ---
            self._assets.purge_ephemeral()
            self._record.closed = self._last_activity
            self._flush_record(force=True)
            # Closure keeps the lock; it does NOT detach. Force a fresh beat so the now-idle
            # (un-advanced) closed case holds a full-TTL grace window for owners to harvest
            # before they call case_detach(). A crash still lapses the lock via the TTL.
            self.case_heartbeat(min_update_secs=0)
            self._notify(EV_CLOSED, src=src, dest=dest)

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
             CASE_ENTER_STATE write never ran (the after_state_change writer was skipped by
             the raise), write it now so the on-disk log can never lag in-memory state.
          2. DECORATE the exception with structured `case_context` (case_id, trigger,
             source/dest, commit phase) so a type-agnostic driver can branch without parsing
             messages — attached HERE so it travels regardless of how the trigger was fired.
          3. LOG a terse, COUNTABLE failure fact (NOT a CASE_ALERT): CASE_FAIL_TRANSITION for
             a pre-commit failure (the kind `@FAIL` counts and retries re-attempt), or
             CASE_ENTRY_EXCEPTION for a post-commit entry-hook raise (it DID enter; logged
             and hooked, but NOT counted by @FAIL). A pre-commit TriggerTimeout is logged as
             CASE_TRIGGER_TIMEOUT instead — visually distinct, still @FAIL-counted.
          4. Call on_transition_exception(...) so the case may compensate.
          5. RE-RAISE the original exception. case_advance() catches it and folds it into an
             AdvanceResult; a direct caller gets the raise (fail-fast preserved)."""
        err = event.error
        # Lost the folder to another owner mid-step (raised by the keepalive pulse): a fatal
        # invariant breach, not a transition failure. Surface it WITHOUT logging a fail or
        # counting it toward @FAIL — the displaced owner must simply stop operating.
        if isinstance(err, OwnershipLostError):
            raise err
        trigger = event.event.name if event.event is not None else None
        trans = event.transition
        src = trans.source if trans is not None else self.case_state
        dest = trans.dest if trans is not None else None
        post_commit = dest is not None and self.case_state == dest

        # 1. No-drift remedy. NOTE (sharp edge): if dest is a TERMINAL state, the two-phase
        # close in _on_state_changed was also skipped here; we reconcile the CASE_ENTER_STATE
        # but do NOT attempt the close from inside the exception handler. Terminal-entry
        # failures still need the close path made idempotent/re-runnable.
        if post_commit and self._journal.current_state != dest:
            self._journal.log_enter_state(dest)
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
            self._journal.log_trigger_timeout(trigger or src, detail)
        elif post_commit:
            self._journal.log_entry_exception(dest or src, detail)
        else:
            self._journal.log_fail_transition(trigger or src, detail)

        # 4. Let the case react. final_state reflects where we actually ended up.
        final_state = dest if post_commit else src
        try:
            self.on_transition_exception(src, trigger, final_state, err)
        except Exception:                     # a misbehaving hook must not mask the real error
            logger.exception("on_transition_exception hook raised for case %s", self.case_id)

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

    # ---- single-owner protection: the heartbeat lease (mechanics) ----
    # Lease mechanics live in HeartbeatLease; the domain-flavored facade (heartbeat,
    # lease_ttl_for) is an override seam in SECTION 3, and case_detach() is in SECTION 2.

    def _check_active(self) -> None:
        if self._lease is None or not self._lease.is_active():
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
        out = []
        for t in self._fsm.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if state in srcs and self._fsm.is_auto(state, t["trigger"]):
                out.append((t["trigger"], t["dest"]))
        return out

    def _make_blocked(self, candidates) -> AutoAdvanceBlocked:
        """Build the AutoAdvanceBlocked marker for the current (provably stuck) state,
        logging a SINGLE CASE_ALERT the first time we detect the block in this dwell so it
        is visible on disk without spamming the low-volume log on every case_advance() call."""
        if not self._journal.has_event_since_enter(EV_ALERT):
            self.case_log_alert(f"auto-advance blocked in {self.case_state!r}", where=self.case_state)
        return AutoAdvanceBlocked(
            self.case_id, self.case_state, candidates=[t for t, _ in candidates]
        )

    # ---- stall handling ----
    # There is deliberately NO self-pulse: a case cannot watchdog its own case_advance() from
    # inside a single suspended coroutine. Stall handling is split by failure mode instead:
    #   * a stalled external job   -> a `@DWELL>...` timed-escape edge ripens and case_advance()
    #                                 fires it (in-band, declarative, self-healing);
    #   * a genuinely stuck state  -> AutoAdvanceBlocked, carried in AdvanceResult + one
    #                                 CASE_ALERT per dwell;
    #   * a hung case_advance() call -> an out-of-band concern for the driver (e.g. wrapping
    #                                 case_advance() in asyncio.wait_for); the case cannot observe
    #                                 it itself.
