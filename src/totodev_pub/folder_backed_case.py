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
package and are re-exported here for convenience and backward compatibility.

Quick start
-----------
    class TicketCase(FolderBackedCase):
        # State-chains DSL: 
        #     State_A ==trigger--> State_B (manual)    a human-driven transition
        #     State_A --trigger--> State_B (auto)      an auto-advance transition
        #     leading  `^` = initial state, 
        #     trailing `^` = terminal state.
        fsm_state_chains = ["^new--open_ticket-->open==close_ticket-->closed^"
                            "*--@DWELL>14d#non_responsive-->auto_closed^"]  # timed-escape edge

    from totodev_pub.folder_backed_case import case_type_registry
    case_type_registry.register_case_types(TicketCase)

    case = TicketCase.create_in_folder(Path("/data/cases/t-001"), case_id="t-001")
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
    RECORD_NAME,
    LEASE_NAME,
    EVENTS_DIR_NAME,
    ASSETS_DIR_NAME,
    KEEP_LIST_NAME,
    CASE_RESERVED_ARTIFACT_NAMES,
    CASE_BASE_EVENT_PREFIX,
    DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS,
    EV_CLOSED,
    EV_ALERT,
    SIG_CLOSING,
)
from totodev_pub.folder_backed_case_support.helpers import _utcnow, _new_time_slug
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError,
    OwnershipLostError,
    DetachedCaseError,
    UnregisteredCaseTypeError,
    CaseTypeMismatchError,
    RecordTypeMismatchError,
    IncompatibleReclassError,
    MissingFsmError,
    FsmChainParseError,
    FsmBindingError,
    AutoAdvanceBlocked,
    TriggerTimeout,
)
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_journal import CaseJournal
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry,
    case_type_registry,
)
from totodev_pub.folder_backed_case_support.heartbeat_lease import (
    HeartbeatLease,
    LeaseAlreadyHeldError,
    LeaseOwnershipLostError,
)
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    StateChainParser,
    FsmChainSpec,
)
from totodev_pub.folder_backed_case_support.case_machine_factory import CaseMachineFactory

logger = logging.getLogger(__name__)

__all__ = [
    "FolderBackedCase",
    "CaseTypeRegistry",
    "case_type_registry",
    "HeartbeatLease",
    "CaseRecord",
    "CaseEventLogReader",
    "CaseJournal",
    "CaseAssets",
    "AdvanceResult",
    "StateChainParser",
    "FsmChainSpec",
    "CaseAlreadyOpenError",
    "OwnershipLostError",
    "DetachedCaseError",
    "UnregisteredCaseTypeError",
    "CaseTypeMismatchError",
    "RecordTypeMismatchError",
    "IncompatibleReclassError",
    "MissingFsmError",
    "FsmChainParseError",
    "FsmBindingError",
    "AutoAdvanceBlocked",
    "TriggerTimeout",
    "RECORD_NAME",
    "LEASE_NAME",
    "EVENTS_DIR_NAME",
    "ASSETS_DIR_NAME",
    "KEEP_LIST_NAME",
    "CASE_RESERVED_ARTIFACT_NAMES",
    "CASE_BASE_EVENT_PREFIX",
]


# ---------------------------------------------------------------------------
# FolderBackedCase — the logic base class
# ---------------------------------------------------------------------------

class FolderBackedCase(ABC):
    """
    Base class for all folder-backed case types.

    Provides: case folder (record + event log + assets), async FSM via
    `transitions`, a flat pipeline driver, ephemeral-file retention,
    the two-phase closing-edge hook, and a single-owner heartbeat lease.

    Subclasses declare ONE thing for the FSM: `fsm_state_chains`, a list of
    Mermaid-flavoured chain strings. A trivial example:

        fsm_state_chains = ["^new--assign-->assigned==work-->closed^"]

    reads left-to-right as states joined by `--trigger-->` connectors, where a leading
    `^` marks the initial state, a trailing `^` a terminal (closing) state, and `--`
    opts an edge into auto-advance (`==` stays manual). Connectors also carry guards, `@DWELL`/
    `@FAIL` factual guards, and `~<dur>` soft timeouts — see StateChainParser for the
    full grammar. Side-effecting work for a trigger goes in a `perform_<trigger>`
    method, auto-wired as the transition's `before` (a raise aborts the step); the
    parser renders the chains into the per-class singleton `_fsm`.

    Implicit hook conventions (derived from your declared states/triggers):
      * `on_enter_<state>`, `on_exit_<state>` fire only when `<state>` is an exact
        known FSM state name.
      * `perform_<trigger>`, `before_<trigger>`, `after_<trigger>` map only when
        `<trigger>` is an exact known FSM trigger name.
      * a method `<guard>` in a `guard#trigger` DSL segment is bound by the `guard_`
        convention: the token `<guard>` resolves to the method `guard_<guard>` (so
        `funded#finish` requires `async def guard_funded`). A hand-built transition dict
        may instead supply an explicit callable. The `guard_` prefix keeps guards in their
        own namespace, away from ordinary helpers and lifecycle hooks.
    In short: hook/guard suffixes are strict exact matches to parsed state/trigger/guard
    names. Typos are caught at first construction: compatibility validation runs with
    `orphan_detection="error"` by DEFAULT, so a method that looks like a hook/guard
    (`on_enter_`, `on_exit_`, `guard_`, `perform_`, `before_`, `after_`) but maps to no
    known name fails the build. A deliberately-named coincidental method can opt out via
    `orphan_detection="off"`.

    Need the full power of `transitions` (callbacks, `unless`, `*`/multi-source, state
    objects)? Override compile_fsm() and return an FsmChainSpec you build yourself
    (optionally by parsing the chains first and then tweaking the result). There is no
    second declarative attribute to keep in sync — compile_fsm() is the single seam.
    """

    # The ONE declarative FSM input (World A): the default compile_fsm() parses this.
    fsm_state_chains: list[str] = []

    # Compiled per-class FSM (private); really populated in __init_subclass__ via
    # compile_fsm(). Empty here on the base until a subclass supplies one.
    _fsm: FsmChainSpec = FsmChainSpec.empty()

    # The record-type seam (always required; defaults to CaseRecord): this attribute IS
    # the extension point — subclass CaseRecord and set `_record_cls = MyRecord` to add
    # fields. Read the live record back via fetch_record().
    _record_cls: type[CaseRecord] = CaseRecord

    # ---- FSM compilation (parse-once-at-class-definition) ----

    @classmethod
    def compile_fsm(cls) -> FsmChainSpec:
        """Render this class's declared FSM into an FsmChainSpec. PURE: it touches no
        class state and is called exactly once per subclass (by __init_subclass__), whose
        job is to cache the result as the `_fsm` singleton. The default parses
        `fsm_state_chains`, runs the whole-graph FsmChainSpec.validate(), then injects any
        `*--...-->` wildcard edges via expand_wildcards() (in that order, so the typo checks
        see only the explicit graph); OVERRIDE this to build/extend the spec by hand for the
        rare cases the chain DSL can't express (arbitrary callbacks, `unless`, state objects)
        — an override owns whether and when to call validate()/expand_wildcards()/classify()/
        apply_implicit_fail_cap(). Overriding is the single, unambiguous manual escape hatch."""
        return (
            StateChainParser.parse(cls.fsm_state_chains)
            .validate()
            .expand_wildcards()
            .classify()
            .apply_implicit_fail_cap()
        )

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

    # ---- ID generation ----

    # Monotonic clock for in-process case_id minting (see generate_case_id).
    _last_generated_case_id_ms: int = -1

    @classmethod
    def generate_case_id(cls) -> str:
        """Auto-ID factory used by create_in_folder() when no explicit case_id is
        supplied. The SOLE public slug seam: an overridable extension point — a
        subclass may return a UUID, a domain-prefixed id, a sequential counter,
        etc., and can COMPOSE with the default via super().generate_case_id()
        (e.g. f"INV-{super().generate_case_id()}"). The default is a short,
        sortable, base-36 millisecond time slug.

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

    # ---- construction ----

    def __init__(self, case_folder: Path):
        """Bind a live case object to an EXISTING on-disk case folder.

        This constructor is intentionally the load/bind path, not inception:
        it expects `case_record.yaml` (and any existing event-log history) to
        already exist on disk, then loads them, acquires the lease, and builds
        the in-memory FSM carrier. Two ways it fails fast and points elsewhere:
          * The folder is not an initialized case (no record) -> FileNotFoundError
            naming `create_in_folder()` (inception) and `rehydrate()`.
          * The record names a DIFFERENT case type than this class -> the
            CaseTypeMismatchError gate.

        Brand-new cases are created via `create_in_folder(...)`, which first
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
          * `reclassify_to` calls it with `check_type=False` on a freshly
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
            cls._fsm.validate_object_compatibility(self)
            cls._fsm_binding_checked = True
        self._folder = Path(case_folder)
        # Uninitialized-folder gate: this is the BIND path, not inception. A missing
        # record means the caller wanted create_in_folder() (new) or rehydrate() (by type).
        record_path = self._folder / RECORD_NAME
        if not record_path.exists():
            raise FileNotFoundError(
                f"No case record at {record_path}: this folder is not an initialized case. "
                "Use create_in_folder() to incept a new case, or "
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
        # the one the record names. reclassify_to passes check_type=False to build over the
        # old name on purpose; every other caller gets the gate.
        if check_type and self._record.case_object_type != cls.__name__:
            raise CaseTypeMismatchError(
                on_disk=self._record.case_object_type, loading_class=cls.__name__
            )
        self._journal = CaseJournal.for_folder(self._folder)
        self._assets = CaseAssets(self._folder)   # asset playground + retention manifest
        self._listeners: list = []        # fn(case, event_name, info)
        # State is derived from the event log on load; transitions then cache on self.state.
        self.state: str = self._derive_state() or self._fsm.initial_state
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
            ttl_provider=lambda: self.lease_ttl_for(self.state),
        )
        try:
            self._lease.acquire()
        except LeaseAlreadyHeldError as e:
            raise CaseAlreadyOpenError(self._folder, expires_in=e.expires_in) from e
        # Instance-time machine binding is delegated to CaseMachineFactory.
        self._machine = CaseMachineFactory(self, self._fsm, self._journal).build(self.state)

    @staticmethod
    def _as_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
        """Read a naive (local) event-log mtime as aware UTC; pass None through."""
        return dt.astimezone(datetime.timezone.utc) if dt is not None else None

    @classmethod
    def create_in_folder(
        cls,
        case_folder: Path,
        *,
        case_id: str | None = None,
        external_key: str | None = None,
        nickname: str | None = None,
        **fields,
    ) -> FolderBackedCase:
        """First-time inception, shared by CaseManager.create_case() and standalone.

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
                "Create/confirm the parent folder first, then retry create_in_folder()."
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

    # Type resolution and rehydrate live in CaseTypeRegistry (case_type_registry singleton).

    # ---- folder peek: read a case folder without constructing a live instance ----

    @staticmethod
    def peek_case_record(
        folder: Path,
        *,
        record_cls: type[CaseRecord] | None = None,
        case_cls: type[FolderBackedCase] | None = None,
    ) -> CaseRecord:
        """Read the identity record from disk — lock-free, no live case, no registry.

        YOU supply the typed record shape, or accept the base:
          - record_cls=...  → use that CaseRecord subclass directly (wins if both given).
          - case_cls=...    → use that case's _record_cls.
          - neither         → base CaseRecord (common identity fields only; subclass-specific
                              fields not present on the base are silently dropped).

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
        The peek analog of a live case's .assets property."""
        return CaseAssets(Path(folder))

    # ---- lifecycle-signal subscription ----

    def add_transition_listener(self, fn) -> None:
        """Subscribe to post-transition notifications: fn(case, event_name, info).
        This is how a CaseManager attaches archival behavior without the case having
        any knowledge of the manager. Standalone cases have no listeners."""
        self._listeners.append(fn)

    def _notify(self, event_name: str, **info) -> None:
        for fn in self._listeners:
            fn(self, event_name, info)

    def _derive_state(self) -> str | None:
        """Current state = the most recent CASE_ENTER_STATE entry. Delegates to the
        journal (over the same CaseEventLogReader the peek path uses) — no-drift
        guarantee is structural."""
        return self._journal.current_state

    # ---- identity / status properties ----

    @property
    def case_id(self) -> str:
        return self._record.case_id

    @property
    def external_key(self) -> str | None:
        return self._record.external_key

    @property
    def nickname(self) -> str | None:
        return self._record.nickname

    @property
    def folder(self) -> Path:
        return self._folder

    @property
    def is_open(self) -> bool:
        return self.state not in self._fsm.closed_states

    @property
    def is_closed(self) -> bool:
        return self.state in self._fsm.closed_states

    # ---- FSM attachment via transitions (async-first composition pattern) ----
    # Machine construction + callback wiring live in CaseMachineFactory; the case keeps
    # the override seams the factory reads back (trigger_warn_secs, dwell_secs) and the
    # named lifecycle callbacks the machine binds (_on_state_changed, _on_fsm_exception).

    def trigger_warn_secs(self, trigger: str) -> float:
        """The SOFT timeout (seconds) for a trigger's work: its `~<dur>` DSL annotation if
        present, else DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS. The override seam for a per-case
        or dynamic budget — keep it cheap, it is consulted on every step that has work."""
        return self._fsm.trigger_timeouts.get(trigger, DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS)

    async def run_blocking(self, fn, /, *args, **kwargs):
        """OPT-IN escape hatch for a SYNCHRONOUS/blocking call inside a `perform_`. Runs `fn`
        on the default thread-pool executor and awaits it, so the call does NOT freeze the
        event loop (which would stall every other case sharing it). Use ONLY when a library
        gives you no async API:

            async def perform_fetch(self, event):
                resp = await self.run_blocking(requests.get, url)

        SECOND-CLASS by design, with two caveats vs. an async-native client:
          * Concurrency is bounded by the executor's thread pool (not the ~unbounded
            concurrency of real async I/O), so blocking calls do not scale the same way.
          * The hard-abort (TriggerTimeout) cancels the AWAIT, but a running thread cannot be
            killed — the worker keeps going until `fn` returns on its own. So a true hang here
            frees the case but leaks the thread. Prefer an async client for anything that can
            hang."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    def dwell_secs(self) -> float:
        """Seconds the case has spent in its CURRENT state — the fact the `@DWELL` guard
        compares against. Measured from `self._state_entered_at` (the latest
        CASE_ENTER_STATE, or creation for a brand-new case)."""
        return (_utcnow() - self._state_entered_at).total_seconds()

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
               object stays bound so owners can harvest before calling detach())
            7. _notify("CASE_CLOSED") — finalized-but-still-bound; the "safe to move"
               signal is detach(), not this. Standalone: no-op.
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
            self.heartbeat()
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
            # before they call detach(). A crash still lapses the lock via the TTL.
            self.heartbeat(min_update_secs=0)
            self._notify(EV_CLOSED, src=src, dest=dest)

    async def _on_fsm_exception(self, event) -> None:
        """Machine-level `on_exception` hook (wired by the machine factory): the SINGLE chokepoint
        every trigger dispatch funnels through, so it covers both advance() and a direct
        `await case.<trigger>()`. Fires when ANY callback raises — a guard, a
        `perform_<trigger>`, on_exit/on_enter, or an `after`.

        It distinguishes the COMMIT BOUNDARY without needing to know which callback slot
        raised: `transitions` sets `self.state` to the dest during the state change, BEFORE
        on_enter/after run, so `self.state == dest` means we are POST-commit.

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
          5. RE-RAISE the original exception. advance() catches it and folds it into an
             AdvanceResult; a direct caller gets the raise (fail-fast preserved)."""
        err = event.error
        trigger = event.event.name if event.event is not None else None
        trans = event.transition
        src = trans.source if trans is not None else self.state
        dest = trans.dest if trans is not None else None
        post_commit = dest is not None and self.state == dest

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

        # 5. Re-raise so advance() can fold it in (and direct callers stay fail-fast).
        raise err

    def on_transition_exception(self, begin_state, trigger, final_state, exc) -> None:
        """Overridable recovery hook, fired (before the exception re-raises) whenever a
        transition's dispatch raised. Default: no-op.

        `begin_state == final_state` ⇒ a PRE-commit failure (the work raised, the case never
        left its state — the retryable "no progress" kind). `begin_state != final_state` ⇒
        a POST-commit failure (the state DID change, then an entry/after hook raised; the
        case is in `final_state` carrying the baggage of a failed side-effect).

        Use it to compensate from inside the case (which, unlike a generic driver, knows its
        own data): mark a record field, schedule a fix-up, set a flag a later guard reads.

        DO NOT fire a transition from within this hook — re-entering the machine mid-dispatch
        is unsupported. To route to a fault state, prefer the declarative `@FAIL>=n` divert
        edge, or record intent here and let the next advance() carry it out."""

    def on_closing(self) -> None:
        """Overridable hook fired in phase 1 (pre-finalization): assets still exist,
        record not yet stamped. Override to retain/extract final artifacts before the
        ephemeral purge. Default: no-op. Heavy async finalization belongs in a `before`
        callback on the closing transition instead — this hook is sync cleanup."""

    # ---- record flush / fetch protocol (single guarded chokepoint) ----
    # Reads use without_lock=True (open() would otherwise hold a lock for the
    # instance lifetime, which conflicts with our single-owner lease model).
    # Writes use the mixin's save() as normal — it acquires a brief transient
    # lock only for the duration of the write and releases immediately after.

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

    def fetch_record(self, *, force: bool = False) -> CaseRecord:
        """Public read accessor for the identity record — the read companion to
        _flush_record(). FAST by default: returns a detached deep-copy SNAPSHOT of the
        in-memory record (no disk I/O, no revalidation). force=True first re-reads the
        on-disk record IN PLACE, refreshing the case's own copy — and any internal
        holder of that same object — then snapshots that fresh state.

        The returned snapshot is unmapped from any file, so mutating or even save()-ing
        it cannot reach back into the case's state or the record file; _flush_record()
        remains the one sanctioned write path. (The base fetches the record exactly once
        at construction and never silently re-reads — pass force=True if an external
        process may have updated the file.)"""
        if force:
            self._record.reload_from_file(force=True)
        return self._record.detached_copy()

    # ---- single-owner protection: the heartbeat lease ----
    # Lease mechanics live in HeartbeatLease; this class keeps a domain-flavored facade.

    def lease_ttl_for(self, state: str) -> float:
        """Seconds the lease stays valid after a beat, for `state`. Override per state
        for long-idle windows. MUST comfortably exceed the gap between heartbeat() calls
        (i.e. the driver's advance() poll cadence + slack), or a live-but-quiet owner can
        be reclaimed."""
        return 300.0

    def heartbeat(
        self,
        *,
        min_update_secs: float = 15.0,
        validate_ownership: bool = True,
    ) -> None:
        """Extend our lease. Raises `OwnershipLostError` when ownership is displaced."""
        self._check_active()
        try:
            self._lease.heartbeat(
                min_update_secs=min_update_secs,
                validate_ownership=validate_ownership,
            )
        except LeaseOwnershipLostError as e:
            raise OwnershipLostError(self._folder) from e

    def detach(self) -> None:
        """Unbind this object from its folder: delete the lease (clearing the conceptual
        lock) and mark this instance detached. Idempotent and DELIBERATE — it is NOT a
        side-effect of FSM closure and does NOTHING to the case's assets.

        This is the single point at which the folder becomes fair game for an external
        owner (a CaseManager) to move/archive: while ANY in-memory object remains bound,
        the lease asserts the folder is spoken-for. A closed case stays bound until an
        owner — who may first harvest non-ephemeral data from it — calls this.

        Any later mutating use raises DetachedCaseError. Wired to __exit__ (context
        manager) and best-effort __del__; on a crash the lease simply expires via its TTL."""
        if self._lease is not None:
            self._lease.release()

    def _check_active(self) -> None:
        if self._lease is None or not self._lease.is_active():
            raise DetachedCaseError(self._folder)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.detach()

    def __del__(self):
        try:
            self.detach()
        except Exception:
            pass

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> bool | None:
        """Lock-free staleness read for a manager's recovery sweep: True if the lease
        has expired (reclaimable), False if still held, None if unheld. Policy-free —
        the expiry is baked into the mtime, so no state/TTL lookup is needed here."""
        return HeartbeatLease.is_expired(Path(folder) / LEASE_NAME)

    # ---- flat pipeline driver ----

    def _forward_candidates(self, state: str):
        """The auto-advance edges leaving `state`, as (trigger, dest) in declared order.
        Empty when terminal / nothing auto-advances from here (e.g. awaiting input). With
        guards, more than one candidate may be eligible; advance() tries them in order."""
        out = []
        for t in self._fsm.transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if state in srcs and self._fsm.is_auto(state, t["trigger"]):
                out.append((t["trigger"], t["dest"]))
        return out

    async def advance(self) -> AdvanceResult:
        """Fire ONE forward step from the current state and report the outcome as an
        AdvanceResult (a NON-throwing reporter — see that class). Tries each auto-advance
        candidate in declared order, firing the first whose guard permits.

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
        programming error, not a flow condition)."""
        initial = self.state
        if self.is_closed:              # terminal short-circuits first (a closed case stays
            return AdvanceResult(initial, self.state)   # bound; lease cleared only by detach())
        self._check_active()            # else refuse to drive a detached husk
        # Every poll is proof of active ownership, so beat the lease here (throttled — a
        # no-op if < min_update_secs since our last beat). This covers the long-dwelling
        # case that keeps no-opping and never transitions, which _on_state_changed's
        # per-transition beat would otherwise leave to expire. Done BEFORE the step so the
        # lease is fresh for the duration of a (possibly slow, awaited) transition.
        self.heartbeat()
        candidates = self._forward_candidates(self.state)
        exceptions: list[Exception] = []
        for trigger, _dest in candidates:
            try:
                # A guard-blocked transition returns falsy WITHOUT changing state (and
                # without firing the after-hook), so we fall through to the next.
                if await getattr(self, trigger)():
                    return AdvanceResult(initial, self.state, trigger=trigger)
            except Exception as err:    # absorbed: reported as data, not raised at driver
                exceptions.append(err)
                return AdvanceResult(initial, self.state, trigger=trigger,
                                     exceptions=tuple(exceptions))
        # Nothing fired and nothing raised: no forward progress this pass. If the state has
        # no guaranteed timed escape, it is provably auto-advance blocked.
        if self.state not in self._fsm.timed_escape_states:
            exceptions.append(self._make_blocked(candidates))
        return AdvanceResult(initial, self.state, exceptions=tuple(exceptions))

    def _make_blocked(self, candidates) -> AutoAdvanceBlocked:
        """Build the AutoAdvanceBlocked marker for the current (provably stuck) state,
        logging a SINGLE CASE_ALERT the first time we detect the block in this dwell so it
        is visible on disk without spamming the low-volume log on every advance() call."""
        if not self._journal.has_event_since_enter(EV_ALERT):
            self.log_alert(f"auto-advance blocked in {self.state!r}", where=self.state)
        return AutoAdvanceBlocked(
            self.case_id, self.state, candidates=[t for t, _ in candidates]
        )

    async def run_to_completion(
        self, *, stop_before: str | None = None
    ) -> AdvanceResult | None:
        """Drive forward until closed, until nothing auto-advances (no progress — including
        a failed attempt or a block), or until an auto step from the current state could
        ENTER `stop_before` (for staged inspection / testing). Returns the LAST AdvanceResult (so a
        caller can inspect why the drive stopped), or None if it never stepped."""
        last: AdvanceResult | None = None
        while self.is_open:
            candidates = self._forward_candidates(self.state)
            if not candidates:
                break
            if stop_before and any(dest == stop_before for _, dest in candidates):
                break
            last = await self.advance()
            if not last.progressed:          # failed / all guards declined / blocked
                break
        return last

    # ---- reclassify ("call an audible" to a different subclass) ----

    def reclassify_to(
        self, new_cls: type[FolderBackedCase]
    ) -> FolderBackedCase:
        """Rebind this case to a different FolderBackedCase subclass via a two-phase
        COMMIT, logging a CASE_RECLASSIFY event. The CALLER owns compatibility.

        Two-phase commit (crash-atomic):
          Phase 1 — snapshot old repr in the OLD class's schema; detach (free the lease).
          Phase 2 — NEW class acquires the now-free lease, CONSCIOUSLY stamps its
                    own name, force-commits in its own schema.
        A crash between phases reopens cleanly as the OLD class (name not switched)."""
        if self.state not in new_cls._fsm.states:
            raise IncompatibleReclassError(self.state, new_cls.__name__)
        from_name = self._record.case_object_type
        self._flush_record(force=True)           # phase 1: snapshot old repr (old name)
        self.detach()                            # serialize-then-DETACH, then re-acquire
        # Bind without the type gate: the old name is still on disk until phase 2 stamps the
        # new one. __new__ + _bind_existing_case_dir bypasses __init__'s gated public path.
        fresh = new_cls.__new__(new_cls)
        fresh._bind_existing_case_dir(self._folder, check_type=False)
        fresh._journal.log_reclassify(
            new_cls.__name__, from_type=from_name, at_state=fresh.state
        )
        fresh._record.case_object_type = new_cls.__name__   # CONSCIOUS stamp
        fresh._flush_record(force=True)                      # phase 2: commit new name + schema
        for fn in self._listeners:
            fresh.add_transition_listener(fn)
        return fresh

    # ---- assets (playground + retention), grouped on CaseAssets ----

    @property
    def assets(self) -> CaseAssets:
        """The case's CaseAssets: file playground under assets/ plus the keep manifest.
        Use case.assets.folder, .asset_path(...), .relative_path(...), .write(...),
        .keep_asset(...), .list_assets(), etc. Kept off this class's own namespace so
        asset concerns stay grouped in one place."""
        return self._assets

    # ---- archive label hook ----

    def archive_grouping_label(self) -> str:
        """Destination archive grouping when this case closes. Default: close month.
        Override to key on creation date, fiscal period, tenant, etc."""
        return _utcnow().strftime("%Y-%m-archive")

    # ---- operator alert channel (type-agnostic escalation marker) ----

    def log_alert(self, short_msg: str = "", *, where: str | None = None) -> None:
        """Record a CASE_ALERT: the case family's single type-agnostic "this case needs a
        human to look at it" marker. Because it reads the same for every case type, an
        observer can surface cases needing attention without knowing any type's internals.

        Orthogonal to the FSM (it does not change state or close the case). Use SPARINGLY
        on the low-volume audit log: raise one for an integrity risk or a substantial
        deviation from norms, NOT for routine, recoverable defects the flow absorbs.

        Args:
            short_msg: a brief human-readable reason (terse phrase, not a stack trace).
            where: locus of concern; defaults to the current state.
        """
        self._journal.log_alert(
            where or self.state, msg=short_msg
        )

    # ---- stall handling ----
    # There is deliberately NO self-pulse: a case cannot watchdog its own advance() from
    # inside a single suspended coroutine. Stall handling is split by failure mode instead:
    #   * a stalled external job   -> a `@DWELL>...` timed-escape edge ripens and advance()
    #                                 fires it (in-band, declarative, self-healing);
    #   * a genuinely stuck state  -> AutoAdvanceBlocked, carried in AdvanceResult + one
    #                                 CASE_ALERT per dwell;
    #   * a hung advance() call     -> an out-of-band concern for the driver (e.g. wrapping
    #                                 advance() in asyncio.wait_for); the case cannot observe
    #                                 it itself.
