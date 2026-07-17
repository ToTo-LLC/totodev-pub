# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
FolderBackedCaseInterface — basic-usage contract for ``FolderBackedCase``.

OVERVIEW
If you want to understand FolderBackedCase, START HERE!  This class defines
the way basic users should interact and use cases.

GOVERNANCE
----------
This class is contract only. It carries no policy, no implementation, and never
will. Contract documentation (purpose, arguments, guarantees, usage) lives HERE
and governs. Implementation commentary lives in ``folder_backed_case.py``.
Do not add executable behavior (validators, descriptors, computed defaults) to
this class; that is policy and belongs in ``FolderBackedCase``.

Derive from ``FolderBackedCase`` (not this interface). Read this class to learn
the everyday subclassing surface; open ``FolderBackedCase`` only when you need
customization seams or internal mechanics.
"""

from __future__ import annotations

import datetime
import logging
from abc import ABC
from pathlib import Path
from typing import TYPE_CHECKING, Self

from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.case_id_generation import CaseIDGenerator
from totodev_pub.folder_backed_case_support.case_journal import CaseEventJournalView
from totodev_pub.folder_backed_case_support.case_record import CaseRecord

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader


def _raises_when_detached(fn):
    """Decorator that marks a public method whose contract forbids operation
    on a detached case.

    Detachment relinquishes the case's authority to alter its state on disk, so
    marked operations are conceptually incapable of proceeding and must raise
    ``DetachedCaseError``. This is documentary metadata only; concrete
    implementations remain responsible for enforcing the contract.

    Scope note: this only governs mutating operations. Read-only snapshot
    members are deliberately NOT marked — a detached husk keeps answering
    those with its last-known in-memory values (see ``case_is_detached``).
    For guaranteed-current status without a lease, use
    ``get_case_reader(case_folder)`` rather than reaching for this decorator.
    """
    fn.__raises_when_detached__ = True
    return fn


class FolderBackedCaseInterface(ABC):
    """Basic-usage authoring surface for folder-backed case types.

    A case is a heavyweight, FSM-driven work item whose entire state — record,
    event log, and working files — lives in a single folder on disk. No database
    required. The folder is self-describing and can be archived or moved
    atomically. Typical uses: a support ticket, an inbound document for
    processing, a contract bundle to review.

    Key concepts
    ------------
    - The FSM language consists of linear ``stateA--trigger-->stateB`` segments
      (see ``fsm_state_chains``). Triggers may be preceded by gates.
    - States are Live or Terminal. Automatic cleanup runs soon after entering a
      terminal state.
    - Triggers are automated or manual. Automated triggers can be triggered by
      ``case_advance()`` (no arguments). Manual triggers are called directly and
      may take arguments.
    - Keep ALL state in the assets folder. Files purge unless added to a keep
      list — prefer declaring ``AssetSpec(keep=True)`` (declarative, seeded
      automatically); for a runtime decision use ``case_keep_files()``. Prefer
      FileMappedPydanticMixin-derived classes for structured assets
      (``case_load_asset()``).
    - Use ``self.log`` (not ``logging.getLogger(__name__)``) for anything
      case-scoped — it tees into this case's ``logs/case.log``. Like assets,
      that file purges on termination unless you opt in to keep it (see
      "Logging" below).
    - The journal is the authoritative source of truth about state. Change state
      ONLY through triggers — never mutate state directly.
    - Rehydrate from disk via ``case_type_registry.rehydrate(folder)``.
    - The case record is a skinny identity card; put your data in assets.
    - Don't instantiate ``FolderBackedCase``. Define a
      subclass.
    - An in-memory case holds a heartbeat lease on its filesystem folder. 
      Simple programs with no fear of concurrent access can ignore the lease.
      Other progrems should make sure the lease is fresh.

    Quick start
    -----------
        class TroubleTicketCase(FolderBackedCase):

            ##### CLASS CONFIG FOR CASE CLASSES #####
            fsm_state_chains = [
                "^new --open_ticket-->open ==close_ticket-->closed^",
                "open --is_duplicative#mark_as_duplicate-->closed",
                "*--@DWELL>14d#non_responsive-->auto_closed^",
                "open --@FAIL>0#failure-->terminal^",
            ]

            asset_aliases = [
                AssetSpec(alias="ticket", relative_path="ticket_info.yaml",
                          loader=TicketInfo, states={"new", "open", "closed"},
                          keep=True),
                AssetSpec(relative_path="resolution-log/customer--convo.md",
                          loader=ChatLog, states={"open"}),
            ]
            fsm_trigger_chokes = {"open_ticket": {"cpu"}}
            ##### END CLASS CONFIG #####

            async def perform_open_ticket(self, tctx) -> None:
                self.log.info("opening ticket %s", self.case_id)
                ...

            async def perform_close_ticket(self, tctx) -> None:
                ...

            async def guard_is_duplicative(self, tctx) -> bool:
                return False

            async def on_enter_closed(self, tctx) -> None:
                ...

        from totodev_pub.folder_backed_case_support.case_type_registry import (
            case_type_registry,
        )
        case_type_registry.register_case_types(TroubleTicketCase)

        case = TroubleTicketCase.create_case_in_folder(
            Path("/data/cases/t-001"), case_id="t-001",
        )
        try:
            await case.open_ticket()
            await case.case_advance()
            if case.case_is_live:
                await case.close_ticket()
        finally:
            case.case_detach()

        case = case_type_registry.rehydrate(Path("/data/cases/t-001"))
        try:
            ...
        finally:
            case.case_detach()

    Hooks and guards
    ----------------
    Names in ``fsm_state_chains`` wire optional subclass methods:

      * ``async def on_enter_<state>(self, tctx)``
      * ``async def on_exit_<state>(self, tctx)``
      * ``async def perform_<trigger>(self, tctx)`` — auto-wired as
        ``before_<trigger>`` when no explicit ``before_<trigger>`` exists
      * ``async def before_<trigger>(self, tctx)``
      * ``async def after_<trigger>(self, tctx)``
      * ``async def guard_<guard>(self, tctx)`` — boolean gate from
        ``guard#trigger`` DSL

    Raising in a guard or ``before_`` hook aborts the transition and counts as a
    transition fail. Guards should be fast, idempotent, and side-effect free
    (they may be polled many times). Built-in factual guards:

      * ``@FAIL(>|>=|<|<=)n#`` — fail count since entering current state
      * ``@DWELL(>|>=|<|<=)dur#`` — seconds in current state (units s/m/h/d)

    By default, transitions carry an implied ``@FAIL<1#`` guard unless overridden.
    Wildcard source ``*--guard#trigger-->X`` applies from any state.

    Creating hook methods — arguments to triggers
    ---------------------------------------------
    Every hook receives one trigger-context argument, conventionally
    ``tctx: EventData`` (the ``transitions`` EventData object — not the event log).

      * Direct ``await case.<trigger>(**kwargs)`` bundles kwargs into ``tctx.kwargs``.
      * No-argument ``case_advance()`` sweeps leave ``tctx.kwargs`` empty.
      * Pinned ``case_advance(trigger, trigger_kwargs={...})`` passes kwargs
        through; ``trigger_kwargs`` is REQUIRED for MANUAL (``==``) edges via the
        reporter.

    Contract — hooks must be well-behaved async. The lease keepalive depends on
    a trigger's work actually yielding the event loop: await at reasonable
    intervals and offload blocking/CPU-bound work via ``case_run_blocking()``
    (an advanced member on ``FolderBackedCase``) or your own executor. A hook
    that monopolizes the loop starves other cases and its own heartbeat. The
    keepalive protects only the trigger's work slot (``perform_``/``before``);
    guards and ``on_enter``/``on_exit``/``after`` are expected to be light.

    Logging — use ``self.log``, not ``getLogger(__name__)``
    --------------------------------------------------------
    ``self.log`` (see the ``log`` attribute below) is an ordinary logger that ALSO
    mirrors every record into this case's own ``logs/case.log`` — the easy-to-find,
    time-sorted story of just this one case, even with hundreds of others running
    concurrently. Use it for anything case-scoped: routine progress
    (``self.log.info(...)``), a caught-and-handled problem worth a full traceback
    (``self.log.exception(...)`` / ``exc_info=``), warnings, whatever. A module-level
    ``logging.getLogger(__name__)`` call still reaches the main log as always, but
    NEVER the per-case file — so anything you want attributable to a specific case
    belongs on ``self.log`` instead. Passing work off to a helper? ``self.log.getChild
    ("some_name")`` gives it a namespaced logger that behaves normally and stays
    tee'd. You do not need to log a trigger/guard/hook exception yourself for it to
    reach ``logs/case.log`` — the base class already logs the full traceback there
    (in addition to a terse fact in the event log) before re-raising; see
    ``on_transition_exception`` if you want to react to the failure, not just see it.

    ``logs/case.log`` is not a framework keep-rule: a purge deletes it like any other
    unmatched file (privacy default). To keep one, call ``case_keep_files("logs/case.log")``
    — typically from ``on_terminating()``, judged per case — or set the process-global
    ``LogRetention.RETAIN`` knob so bind-time seeding adds that keep rule for you.
    """

    # =======================================================================
    # Declaration attributes — set class attributes on your subclass
    # =======================================================================

    fsm_state_chains: list[str] | None = None
    """The ONE declarative FSM input: the default ``compile_fsm()`` parses this.
    PRIMARY extension point — set this on your subclass to define the whole
    lifecycle.

    DSL cheatsheet:
      ``^state``           leading ``^`` = initial state (first declared one is default)
      ``state^``           trailing ``^`` = terminal state
      ``A==trigger-->B``   ``==`` connector = MANUAL edge (``await case.trigger()``)
      ``A--trigger-->B``   ``--`` connector = AUTO edge (fired by ``case_advance()``)
      ``guard#trigger``    binds method ``guard_<guard>`` as the edge's guard
      ``@DWELL>14d``       factual time guard: true once dwell exceeds 14 days
      ``@FAIL>=n``         factual guard: true once n failures accrued this dwell
      ``~<dur>``           soft (warning) timeout for the trigger's work
      ``*--...-->X``       wildcard source: an edge leaving every state

    See ``StateChainParser`` for the authoritative, complete grammar.
    ``None`` means "not yet declared" — a concrete subclass MUST set this to a
    non-empty list (unless it overrides ``compile_fsm()`` to build the
    ``FsmChainSpec`` by hand). ``None`` and "declared []" both mean: no FSM.
    """

    fsm_trigger_chokes: dict[str, set[str]] | None = None
    """Required declaration of which capacity-constrained resources each
    trigger's work may draw on when this case runs inside a pool that throttles
    such resources. Map trigger name -> set of resource name strings. An empty
    dict means none. ``None`` means "not yet declared" — a concrete subclass
    MUST set this (even to ``{}``). External readers of compiled behavior:
    ``case_type_spec()`` (class method on ``FolderBackedCase``).
    """

    asset_aliases: list[AssetSpec] | None = None
    """Required declaration of the case's on-disk data objects (see
    ``aliased_asset_specs``). ``None`` means "not yet declared" — a concrete
    subclass MUST set this (even to ``[]``). External readers of compiled
    behavior: ``case_type_spec()`` (class method on ``FolderBackedCase``).
    """

    flexible_asset_alias_loading: bool = False
    """When False (default), every declared alias must specify loader and states.
    When True, informal declarations are allowed; omitted states/loader make the
    guard a no-op for that alias.
    """

    log: logging.Logger
    """Per-case folder-logging tee, assigned at bind time. An ordinary ``logging.Logger``
    that ALSO mirrors every record into this case's own ``logs/case.log`` — use it (not
    a module-level ``getLogger(__name__)``) for anything attributable to THIS case.

    Full contract — routing vs. main log, ``getChild``, automatic exception teeing,
    and the purge-by-default retention policy — lives in the class docstring's
    "Logging" section above; this is just the quick-reference version.
    """

    # =======================================================================
    # Creation / lifecycle
    # =======================================================================

    @classmethod
    def create_case_in_folder(
        cls,
        case_folder: Path,
        *,
        case_id: str | CaseIDGenerator | None = None,
        external_key: str | None = None,
        nickname: str | None = None,
        **fields,
    ) -> Self:
        """This is how you create a new case... in the filesystem

        This is the only built-in way to create a new case; the class's
        constructor loads an existing case from disk.

        Creates a fresh case folder, writes the record, binds a live lease-held
        instance, and logs CASE_CREATED + initial CASE_STATE_ENTERED. For
        reopening an existing folder use ``MyCase(folder)`` or
        ``case_type_registry.rehydrate(folder)``. Call ``case_detach()`` on the
        returned instance when you are done with it.

        ``case_id`` may be a literal id string, a ``CaseIDGenerator`` to mint one
        from, or omitted to use ``cls.case_id_generator``.

        Raises:
            FileNotFoundError: parent folder does not exist.
            FileExistsError: folder already contains case artifacts.
        """
        ...

    def case_detach(self) -> None:
        """Unbind this object from its folder: release the lease and mark detached.

        Call this when you are done acting on a live case (scripts, tests, handoff
        to ``CaseManager``, after harvesting a terminated case). After detach,
        mutating use raises ``DetachedCaseError``. Does not move or archive the
        folder.

        If you forget, the lease self-expires after a crash-recovery window;
        explicit detach is still preferred so other owners need not wait.
        Note that many informational properties of this object will respond
        with their last-known in-memory values after detach. For guaranteed-current
        status without holding the lease, use ``get_case_reader(case_folder)``
        instead.

        Also writes a closing banner to ``self.log`` and disables its per-case file
        tee (records keep propagating to the main log; the case folder is simply no
        longer written to, since we no longer legitimately own it). This is always
        written, even on an already-terminal case: the banner marks the end of THIS
        in-memory instance's session, not a claim that the log file itself is done —
        nothing prevents a terminal case from being reopened later (diagnostics, an
        audit tool, etc.), and that reopen gets its own attach banner just like any
        other case, appended right after whatever the termination purge left behind.
        Idempotent — harmless to call more than once on an already-detached object.
        """
        ...

    @_raises_when_detached
    async def case_advance(
        self, trigger: str | None = None, trigger_kwargs: dict | None = None,
    ) -> AdvanceResult:
        """Advance one reported step of the case FSM.

        With no arguments, attempts the next auto-edge from the current state.
        With ``trigger`` (and optional ``trigger_kwargs``), attempts that named
        trigger. Returns an ``AdvanceResult`` describing outcome without
        necessarily raising; see ``AdvanceResult`` for the full reporter contract.
        """
        ...

    # =======================================================================
    # Identity & status (read-only snapshots)
    # =======================================================================

    @property
    def case_id(self) -> str:
        """Stable case identifier from the record."""
        ...

    @property
    def case_external_key(self) -> str | None:
        """Optional business key from the record."""
        ...

    @property
    def case_folder(self) -> Path:
        """On-disk folder this case is bound to."""
        ...

    @property
    def case_state(self) -> str:
        """Current FSM state name.

        Read-only. Do not update directly — drive state changes through
        ``case_advance()`` or a named trigger instead.
        """
        ...

    @property
    def case_terminal_states(self) -> frozenset[str]:
        """FSM states marked terminal for this case type (trailing ``^`` in the DSL).

        Class-level fact — same for every instance.
        """
        ...

    @property
    def case_is_live(self) -> bool:
        """True when current FSM state is not terminal."""
        ...

    @property
    def case_is_terminal(self) -> bool:
        """True when current FSM state is terminal."""
        ...

    @property
    def case_is_detached(self) -> bool:
        """True when this object is no longer bound to its folder: the lease was
        released via ``case_detach()`` or has expired.

        A detached object is a husk — any mutating use (``case_advance()``,
        manual triggers) raises ``DetachedCaseError``. Re-open via
        ``case_type_registry.rehydrate(case_folder)``.

        Read-only snapshot members (``case_state``, ``case_id``, etc.) keep
        answering on a husk, but with whatever was last known in memory — they
        do not re-read disk and will not reflect changes made by another owner
        since detach. For guaranteed-current, lock-free status without holding
        the lease, use ``get_case_reader(case_folder)`` instead.
        """
        ...

    @property
    def case_advanceable(self) -> bool:
        """Whether the current state has an auto exit that ``case_advance()``
        could attempt.
        """
        ...

    @property
    def case_was_blocked(self) -> bool:
        """Whether the last unrestricted ``case_advance()`` on this live object
        proved auto-advance blocked (``AutoAdvanceBlocked``).

        Process-lifetime only: ``False`` after create/open; not journal-backed.
        Orthogonal to ``case_advanceable`` (structural). Cleared when a restricted
        advance, a direct trigger call, or an unrestricted progress/fail supersedes
        the observation; left unchanged on a plain unrestricted no-op.

        A case might be blocked for any combination of reasons:
           1) The case is in a state with no auto-exit.
           2) All auto-exits are gated by guards that are returning False.

        Note that this value is "historical", reflecting the last attempt to advance
        and may not indicate the current ability of the state to advance.
        An exception during trigger execution will also clear this flag.
        """
        ...

    @property
    def case_transition_fail_count(self) -> int:
        """The value the ``@FAIL`` guard compares against: the count of failed
        transition attempts since the case entered its current state.
        """
        ...

    @property
    def case_dwell_secs(self) -> float:
        """Seconds the case has spent in its CURRENT state — the value the
        ``@DWELL`` guard compares against (the sibling of
        ``case_transition_fail_count``). Measured from the latest
        CASE_STATE_ENTERED, or creation for a brand-new case.
        """
        ...

    @property
    def case_last_event_at(self) -> datetime.datetime | None:
        """Latest event-log activity, or record creation if none."""
        ...

    @property
    def case_events(self) -> CaseEventJournalView:
        """Read-only view of this case's event log."""
        ...

    # =======================================================================
    # Assets & record
    # =======================================================================

    @property
    def case_assets(self) -> CaseAssets:
        """The case's CaseAssets: the file playground under ``assets/``, plus a
        READ-ONLY view onto the keep manifest.

        Your working files live here. Use ``case.case_assets.folder``,
        ``.asset_path(...)``, ``.relative_path(...)``, ``.write(...)``,
        ``.list_assets()``, ``.keep_list()``, ``.is_kept(...)``, etc. Retention is
        NOT decided here — declare ``AssetSpec(keep=True)`` (preferred) or call
        ``case_keep_files()`` on the case object. Anything not matched by the
        manifest is purged when the case terminates.
        """
        ...

    def case_keep_files(self, *patterns: str | Path) -> None:
        """Register case files to survive the post-termination purge.

        Closing a case deletes everything under its folder except paths listed in
        ``_keep.txt``. Call this to add retention patterns — case-relative exact
        paths or globs (e.g. ``exports/summary.pdf``, ``reports/*.csv``), including
        under ``assets/`` (e.g. ``"assets/reply_draft.md"``) — this method is
        case-root scoped, not assets-scoped, so an asset path needs its ``assets/``
        prefix spelled out.

        Typical use: a RUNTIME keep decision that can't be made declaratively —
        override ``on_terminating()`` and name the deliverables to preserve after
        the case winds down, judged case-by-case. For an asset you already know at
        class-definition time you'll want kept, prefer declaring
        ``AssetSpec(keep=True)`` instead (seeded automatically, no procedural call
        needed). Patterns are append-only and idempotent; duplicates are ignored.
        Absolute paths inside the case folder are normalized to case-relative form.
        To keep the per-case log across purge, pass ``"logs/case.log"`` (or a
        covering glob) here.
        """
        ...

    def case_load_asset(self, alias: str) -> object:
        """Load a declared asset alias after checking it is trustworthy in the
        current FSM state. Raises ``AssetNotTrustedInStateError`` before any disk
        I/O when the alias is constrained and the current state is not listed.

        Declaring ``asset_aliases`` is convenience sugar, not a requirement — a
        subclass is free to leave it ``[]`` and manage its files by hand. Without
        a declared alias (or for unguarded access even with one), reach
        ``case_assets`` directly: ``case_assets.asset_path(relative_path)`` for
        the filepath, or ``case_assets.read(relative_path)`` / your own parsing
        for the in-memory object.
        """
        ...

    def case_record(self, *, force: bool = False) -> CaseRecord:
        """Public read accessor for the identity record.

        Returns a detached deep-copy snapshot. Pass ``force=True`` to re-read
        from disk first when another process may have changed the file.
        """
        ...

    # =======================================================================
    # Operator alert channel
    # =======================================================================

    def case_log_alert(self, short_msg: str = "", *, where: str | None = None) -> None:
        """Add a CASE_ALERTED entry to the event log: the case family's single
        type-agnostic "this case needs a human to look at it" marker.

        Call to flag a case for human attention. Because it reads the same for
        every case type, an observer can surface flagged cases without knowing
        any internals. Use SPARINGLY on the low-volume audit log: raise one for
        an integrity risk or a substantial deviation from norms, NOT for routine,
        recoverable defects the flow absorbs. Orthogonal to the FSM (does not
        change state or terminate the case).

        Args:
            short_msg: a brief human-readable reason (terse phrase, not a stack
                trace).
            where: locus of concern; defaults to the current state.
        """
        ...

    # =======================================================================
    # Lock-free folder peeks (no live instance)
    # =======================================================================

    @staticmethod
    def peek_case_record(
        folder: Path,
        *,
        record_cls: type[CaseRecord] | None = None,
        case_cls: type | None = None,
    ) -> CaseRecord:
        """Read the identity record from disk — lock-free, no live case, no
        registry.

        Inspect a case's record without taking the lease or building an object
        (safe even while another owner holds the case). YOU supply the typed
        record shape, or accept the base:

          - ``record_cls=...`` → use that CaseRecord subclass directly (wins if
            both given).
          - ``case_cls=...`` → use that case's ``_record_cls``.
          - neither → base CaseRecord (common identity fields only;
            subclass-specific fields not present on the base are silently
            dropped).

        When you want the type deduced from the on-disk record itself, use
        ``case_type_registry.peek_class(folder, return_class_object=True)`` first
        and pass the result as ``case_cls``.
        """
        ...

    @staticmethod
    def peek_case_events(folder: Path) -> CaseEventJournalView:
        """A CaseEventJournalView over the folder's event log — lock-free, no
        live case, no registry. Uniform across every case type (the log format is
        not subclassed). Exposes ``current_state``, ``is_terminal``,
        ``last_activity_at``, and ``.primitive`` for the raw log.
        """
        ...

    @staticmethod
    def peek_case_assets(
        folder: Path, *, resolve_asset_types: bool = False,
    ) -> CaseAssets:
        """A CaseAssets over the folder — lock-free, no live case, no registry.
        Uniform across every case type. Exposes ``list_assets()``,
        ``keep_list()``, ``asset_path()``, etc. The peek analog of a live case's
        ``.case_assets`` property.

        Loads via LazyLoadedFileData by default (no case class needed). Pass
        ``resolve_asset_types=True`` to type each alias whose persisted loader
        name resolves through the asset-dataclass registry (others fall back to
        lazy).
        """
        ...

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> bool | None:
        """Lock-free lease staleness read for a case folder (recovery sweeps).

        Return-value semantics: see ``HeartbeatLease.is_expired``.
        """
        ...

    @staticmethod
    def peek_lease_secs_left(folder: Path) -> float | None:
        """Lock-free lease-time read for a case folder (no acquire).

        Return-value semantics: see ``HeartbeatLease.secs_left``.
        """
        ...

    @staticmethod
    def get_case_reader(folder: Path) -> FolderBackedCaseReader:
        """Return a lock-free read-only view of a case folder — no lease, no
        registry.

        The case reader retrieves data about the case from disk rather than
        memory but typically provides no direct means of modifying the case.
        """
        ...
