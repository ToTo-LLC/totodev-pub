# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
FolderBackedCaseInterface — basic-usage contract for ``FolderBackedCase``.

OVERVIEW
If you want to understand FolderBackedCase, START HERE!  This class defines
the way basic users should interact and use cases.

GOVERNANCE
----------
This class is contract only. It carries no policy, no implementation,
and never will. Contract documentation (purpose, arguments,
guarantees, usage) lives HERE and governs. Implementation
commentary and advanced features live in
``folder_backed_case.py``. Do not add executable behavior
(validators, descriptors, computed defaults) to this class;
that is policy and belongs in ``FolderBackedCase``.

Derive from ``FolderBackedCase`` (not this interface). Read this class to learn
the everyday subclassing surface; open ``FolderBackedCase`` only when you need
customization seams or internal mechanics.
"""

from __future__ import annotations

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
from totodev_pub.folder_backed_case_support.constants import (
    LEASE_HEARTBEAT_THROTTLE_SECS,
)

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
    event journal, and working files — lives in a single folder on disk. No database
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
      Normal operation keeps it fresh automatically; only a held-but-idle
      case needs a manual ``case_heartbeat()`` (see that method).

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
            await case.case_advance() # might trigger mark_as_duplicate()
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
    ``tctx: EventData`` (the ``transitions`` EventData object — not the event journal).

      * Direct ``await case.<trigger>(**kwargs)`` bundles kwargs into ``tctx.kwargs``.
      * No-argument ``case_advance()`` sweeps leave ``tctx.kwargs`` empty.
      * Pinned ``case_advance(trigger, trigger_kwargs={...})`` passes kwargs
        through; ``trigger_kwargs`` is REQUIRED for MANUAL (``==``) edges via the
        reporter.

    Contract — hooks must be well-behaved async. The lease keepalive depends on
    a trigger's work actually yielding the event loop: await at reasonable
    intervals and offload blocking/CPU-bound work via ``case_invoke_threaded()``
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
    (in addition to a terse fact in the event journal) before re-raising; see
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
        """This is how you create a new case... in the filesystem.

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
        """Unbinds this object from its folder: release the lease and mark detached.

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
        """Attempts to advance one reported step of the case FSM.
        This is an alternative to calling triggers directly.

        With no arguments, attempts the next auto-edge from the current state.
        With ``trigger`` (and optional ``trigger_kwargs``), attempts that named
        trigger. Returns an ``AdvanceResult`` describing outcome without
        necessarily raising; see ``AdvanceResult`` for the full reporter contract.
        """
        ...

    @_raises_when_detached
    def case_heartbeat(
        self,
        *,
        min_update_secs: float = LEASE_HEARTBEAT_THROTTLE_SECS,
        validate_ownership: bool = True,
    ) -> None:
        """Refresh this case's heartbeat lease — the keepalive for a held-but-idle
        case.  For active cases, this is done automatically.

        How the lease works (the short version): a case objevt in-memory owns its
        folder by holding a lease — a small file whose timestamp encodes "spoken
        for until then". The window is short (a fixed crash-recovery TTL, ~30s);
        staying the owner means re-stamping periodically before it lapses. Normal
        operation does this for you: ``case_advance()`` beats before each step
        and at every transition boundary, and while a trigger's work runs a
        background pulse keeps beating on its behalf. A case that is actively
        advancing never needs manual attention.

        Cases that aren't concerned with concurrency issues can ignore this method.

        The one gap is a case you HOLD without advancing — parked in memory
        between steps, waiting on external input, sitting in a custom dwell
        loop. Left alone past the TTL, the lease lapses and another owner may
        legitimately reclaim the folder. An idle holder should either call this
        periodically or ``case_detach()`` and rehydrate later.

        Call it as often as you like: refreshes are throttled
        (``min_update_secs``, default ~10s), so a redundant call while the lease
        is still fresh is a free no-op. It doubles as an ownership check —
        raises ``OwnershipLostError`` if another owner has displaced this
        instance (pass ``validate_ownership=False`` to skip that check;
        ``min_update_secs=0`` forces an immediate re-stamp). There is no knob
        for a LONGER lease — keeping an idle case alive is done by beating,
        never by extending the window.
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
    def case_is_advanceable(self) -> bool:
        """Whether the current state has an auto exit that ``case_advance()``
        could attempt.
        """
        ...

    @property
    def case_was_blocked(self) -> bool:
        """Whether the last unrestricted ``case_advance()`` on this live object
        proved auto-advance blocked (``AutoAdvanceBlocked``).

        Process-lifetime only: ``False`` after create/open; not journal-backed.
        Orthogonal to ``case_is_advanceable`` (structural). Cleared when a restricted
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
    def case_event_journal(self) -> CaseEventJournalView:
        """Read-only view of this case's event journal."""
        ...

    # =======================================================================
    # Assets & record
    # =======================================================================

    @property
    def case_assets(self) -> CaseAssets:
        """The case's CaseAssets: the file playground under ``assets/``.

        This object provides access to files in that directory and facilitates
        loading of structured data files. Declaring ``asset_aliases`` (see
        ``case_load_asset()``) is convenience sugar, not a requirement — a
        subclass is free to leave it ``[]`` and manage its files by hand.
        Without a declared alias (or for unguarded access even with one), use
        this directly: ``case_assets.asset_path(relative_path)`` for the
        filepath, or ``case_assets.read(relative_path)`` / your own parsing
        for the in-memory object.
        """
        ...

    def case_keep_files(self, *patterns: str | Path) -> None:
        """Register glob patterns or paths to NOT-purge when case folder 
        contents are being purged at terminal status.

        Patterns are relative to the root of the case folder,
        If passed an absolute path, it will be normalized to a case-relative 
        path.

        Common patterns::

            logs/*.log                  # all log files under logs/
            assets/my_data_thing.yaml   # one specific asset file
            assets/granular_data/**     # an entire asset subtree

       Prefer declaring ``AssetSpec(keep=True)`` within your derived class's
        ``asset_aliases`` at class-definition time for items you always want
        kept. Call this for a runtime keep decision—e.g. from
        ``on_terminating()``, naming deliverables to preserve as judged
        case-by-case.
   
        NOTE: Case level log files are purged by default for security reasons. Using
        "keep" on them is typically done only for debugging purposes and is 
        generally not done in production environments.
   
        """
        ...

    def case_load_asset(self, alias: str) -> object:
        """Load the asset declared under ``alias`` in ``asset_aliases``, returning
        the object its ``loader`` produces.  You must declare the asset in 
        ``asset_aliases`` in order to use this method.

        Before touching disk, checks that the current FSM state is one where
        ``alias`` is trustworthy (per the spec's ``states``), raising
        ``AssetNotTrustedInStateError`` if not.

        For assets not declared in ``asset_aliases`` use the ``case_assets``
        method to find/load manually.
        """
        ...

    def case_record(self, *, force: bool = False) -> CaseRecord:
        """Public read accessor for the case's identity record: a deliberately
        skinny, stable set of facts about the case — what type it is, its id(s),
        its declared asset/FSM shape, and (once reached) its terminal facts. It is
        NOT where detailed, evolving instance data lives; that belongs in assets
        or the event journal. Correspondingly, a driver class will rarely have reason
        to write to this record directly.

        Returns a detached deep-copy snapshot, so mutating the result has no
        effect on the case. Prefer this over ``peek_case_record`` when you
        already hold a live case: the record is already typed to this class, and
        the default path uses the in-memory copy (``force=True`` only reloads
        disk into that cache first — useful if you distrust it, not as a
        lock-free concurrent-read path). Without a live instance, use
        ``peek_case_record`` or ``get_case_reader``.
        """
        ...

    # =======================================================================
    # Operator alert channel
    # =======================================================================

    def case_emit_alert_event(self, short_msg: str = "", *, where: str | None = None) -> None:
        """Append a CASE_ALERTED entry to the event journal — the "this case needs a
        human to look at it" marker. Purely conventional: nothing enforces a
        response, and it does not touch FSM state (no transition, no
        termination). Because CASE_ALERTED reads the same for every case type, a
        type-agnostic observer (dashboard, fleet scan) can surface flagged cases
        without knowing any of this case's internals.

        Use SPARINGLY on this low-volume audit journal — raise one for an integrity
        risk or a substantial deviation from norms, NOT for a routine,
        recoverable defect the flow already absorbs (that belongs on
        ``self.log`` instead).

        Prefer calling this from a hook that runs once per attempt
        (``perform_``/``before_``/``after_``/``on_enter_``/``on_exit_``) rather
        than from a ``guard_``, which can be polled repeatedly and would
        duplicate the alert. Called during ``case_advance()``, the message
        also surfaces on the returned ``AdvanceResult.alerts``.

        Args:
            short_msg: a brief human-readable reason (terse phrase, not a stack
                trace). Leaving it empty is legal but rarely useful — the
                message is the whole point.
            where: locus of concern, stored as the event's value (searchable /
                glob-scannable); defaults to the current state name, but any
                short free-text label is fine if it better names what to look
                at. Becomes literal filename text on disk — keep it short and
                free of ``/ \\ : * ? " < > |``. It is NOT sanitized for you;
                an illegal character (especially ``/``) or an overlong value
                raises ``OSError`` out of the write.
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
        registry. For a live, lease-holding case, prefer ``case_record()``
        instead — it returns a typed snapshot of the owned in-memory record
        without you supplying ``record_cls`` / ``case_cls``.

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
    def peek_case_event_journal(folder: Path) -> CaseEventJournalView:
        """A CaseEventJournalView over the folder's event journal — lock-free, no
        live case, no registry. Uniform across every case type (the journal format
        is not subclassed). Exposes ``current_state``, ``is_terminal``,
        ``last_activity_at``, and ``.primitive`` for the raw journal.
        """
        ...

    @staticmethod
    def peek_case_assets(
        folder: Path, *, resolve_asset_types: bool = False,
    ) -> CaseAssets:
        """A CaseAssets over the folder — lock-free, no live case, no registry.
        Uniform across every case type. Exposes ``list_assets()``,
        ``asset_path()``, etc. The peek analog of a live case's
        ``.case_assets`` property.

        Loads via LazyLoadedFileData by default (no case class needed). Pass
        ``resolve_asset_types=True`` to type each alias whose persisted loader
        name resolves through the asset-dataclass registry (others fall back to
        lazy).
        """
        ...

    @staticmethod
    def get_case_reader(folder: Path) -> FolderBackedCaseReader:
        """Return a lock-free, read-only view of a case folder — no lease, no
        registry, no live case object required.

        This is the primary way to read a case's data without acquiring its
        lease. It's especially useful when you have no live instance at all
        (nothing to detach), or when a live instance HAS been detached and its
        read-only snapshot properties (``case_state``, ``case_dwell_secs``,
        etc.) are frozen at their last-known in-memory values. The reader has
        no such staleness: every property re-reads disk on access, so it stays
        current even if another process or thread advances the case afterward.
        That also makes it safe to hand across process/thread boundaries —
        unlike a live case object, which is bound to one owner's lease.

        The returned ``FolderBackedCaseReader`` mirrors the read-only surface
        above (``case_id``, ``case_state``, ``case_is_terminal``,
        ``case_dwell_secs``, ``case_assets``, ``case_load_asset()``,
        ``case_event_journal``, plus lease-aware extras like
        ``case_lease_secs_left`` and ``case_active_trigger``) as thin wrappers
        over the same ``peek_*`` static methods on this class. Each access
        re-reads its source of truth (record, event journal, or filesystem) rather
        than caching, so expect more I/O cost per read than the equivalent
        in-memory property on a live case — a reasonable trade for
        correctness when you can't or don't want to hold the lease.

        Cannot trigger transitions or otherwise mutate the case — for that you
        need a live, lease-holding instance (see ``create_case_in_folder()`` /
        ``case_type_registry.rehydrate()``).
        """
        ...
