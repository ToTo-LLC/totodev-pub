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
CaseAssets          — working-file playground + retention manifest (_keep_assets.txt).
FolderBackedCase    — ABC you subclass to define a case type.

The tightly-coupled supporting classes live in the folder_backed_case_support
package and are re-exported here for convenience and backward compatibility.

Quick start
-----------
    class TicketCase(FolderBackedCase):
        # Mermaid-flavoured chains: A--trigger-->B; leading `^` = initial state,
        # trailing `^` = terminal state, leading `*` on a connector = auto-advance.
        fsm_state_chains = ["^new--open_ticket-->open--*close_ticket-->closed^"]

    FolderBackedCase.register_case_types(TicketCase)

    case = TicketCase.create_in_folder(Path("/data/cases/t-001"), case_id="t-001")
    with case:
        await case.open_ticket()
        await case.close_ticket()
    # lease auto-released by context manager; folder is fully self-contained

See FolderBackedCase Model.md for the full design narrative.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from abc import ABC
from pathlib import Path
from typing import Optional

from transitions.extensions.asyncio import AsyncMachine

from totodev_pub.folder_backed_case_support.constants import (
    RECORD_NAME,
    LEASE_NAME,
    ASSETS_DIR_NAME,
    KEEP_LIST_NAME,
)
from totodev_pub.folder_backed_case_support.helpers import _utcnow, _new_time_slug
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError,
    OwnershipLostError,
    ReleasedCaseError,
    UnregisteredCaseTypeError,
    RecordTypeMismatchError,
    IncompatibleReclassError,
    FsmChainParseError,
)
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    StateChainParser,
    FsmChainSpec,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FolderBackedCase",
    "CaseRecord",
    "CaseEventLogReader",
    "CaseAssets",
    "StateChainParser",
    "FsmChainSpec",
    "CaseAlreadyOpenError",
    "OwnershipLostError",
    "ReleasedCaseError",
    "UnregisteredCaseTypeError",
    "RecordTypeMismatchError",
    "IncompatibleReclassError",
    "FsmChainParseError",
    "RECORD_NAME",
    "LEASE_NAME",
    "ASSETS_DIR_NAME",
    "KEEP_LIST_NAME",
]


# ---------------------------------------------------------------------------
# FolderBackedCase — the logic base class
# ---------------------------------------------------------------------------

class FolderBackedCase(ABC):
    """
    Base class for all folder-backed case types.

    Provides: case folder (record + event log + assets), async FSM via
    `transitions`, a flat pipeline driver, pulse hook, ephemeral-file retention,
    the two-phase closing-edge hook, and a single-owner heartbeat lease.

    Subclasses declare ONE thing for the FSM:
        fsm_state_chains  list of Mermaid-flavoured chain strings, e.g.
                          ["^new--assign-->assigned--*begin-->in_process--*funded#finish-->closed^"].
                            * `A--trigger-->B` is a transition.
                            * a LEADING `^` marks an INITIAL/entry state (`^new`); a
                              TRAILING `^` marks a TERMINAL/closed state (`closed^`,
                              pulse suppressed + close hook). Chains must collectively
                              declare ≥1 of each; the default entry is the first
                              initial-marked state, so a first-chain initial wins.
                            * a leading `*` on a connector marks the edge AUTO-ADVANCE
                              (opt-in; advance()/run_to_completion may fire it). Un-starred
                              edges never auto-fire — fail-safe against skipping a gate.
                            * `guard#trigger` attaches `conditions`; advance() tries the
                              auto candidates from a state in order, firing the first whose
                              guard permits.
                            * `@<dur>#trigger` adds a TIME GUARD (`*@60m#expire`): a factual
                              condition true once the case has dwelled ≥ that long in the
                              source state (units s|m|h|d; it gates, it does not drive).
                            * a chain starting with `*--` is a "from any source" wildcard
                              (`*--cancel-->cancelled^`): one edge fired from any non-terminal
                              state, deduced after validation (explicit edges overrule it).
                          Side-effecting work for a trigger goes in a `_perform_<trigger>`
                          method, auto-wired as the transition's `before` (a raise aborts the
                          step). The parser renders chains into the per-class singleton `_fsm`.

    Need the full power of `transitions` (callbacks, `unless`, `*`/multi-source, state
    objects)? Override compile_fsm() and return an FsmChainSpec you build yourself
    (optionally by parsing the chains first and then tweaking the result). There is no
    second declarative attribute to keep in sync — compile_fsm() is the single seam.

    See FolderBackedCase Model.md for the full design narrative.
    """

    # The ONE declarative FSM input (World A): the default compile_fsm() parses this.
    fsm_state_chains: list[str] = []

    # The compiled FSM — a PER-CLASS singleton, populated in __init_subclass__ and shared
    # by every instance. Reconfiguring it on a live machine is unsupported (don't). The
    # base ABC itself carries an empty spec; concrete subclasses overwrite it.
    _fsm: FsmChainSpec = FsmChainSpec.empty()

    # Record extensibility (§4b): point _record_cls at a CaseRecord subclass to carry
    # extra TYPED fields. Subclasses MAY mutate those fields mid-life; keep the record
    # as non-volatile as reasonably possible — volatile/derived data belongs in the
    # event log — but infrequently-changed fields are fine here. PRIVATE on purpose:
    # the only public way to read a record is fetch_record().
    _record_cls: type[CaseRecord] = CaseRecord

    # ---- FSM compilation (parse-once-at-class-definition, §FSM) ----

    @classmethod
    def compile_fsm(cls) -> FsmChainSpec:
        """Render this class's declared FSM into an FsmChainSpec. PURE: it touches no
        class state and is called exactly once per subclass (by __init_subclass__), whose
        job is to cache the result as the `_fsm` singleton. The default parses
        `fsm_state_chains`, runs the whole-graph FsmChainSpec.validate(), then injects any
        `*--...-->` wildcard edges via expand_wildcards() (in that order, so the typo checks
        see only the explicit graph); OVERRIDE this to build/extend the spec by hand for the
        rare cases the chain DSL can't express (arbitrary callbacks, `unless`, state objects)
        — an override owns whether and when to call validate()/expand_wildcards(). Overriding
        is the single, unambiguous manual escape hatch."""
        return StateChainParser.parse(cls.fsm_state_chains).validate().expand_wildcards()

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

    # Flush policy (§5b): by default the base throttled-flushes the record (only if
    # dirty) at every transition boundary. Set False — or shadow as a property — to
    # suppress that intermediate flush; the FORCED lifecycle flushes (create / close /
    # reclassify) STILL happen regardless.
    autoflush_record_on_transition: bool = True

    # Case-type registry (§4a): a GLOBAL SINGLETON owned here, not by a manager, so
    # type resolution (rehydrate / smart peek_record) works manager-free.
    _registry: dict[str, "type[FolderBackedCase]"] = {}

    # ---- registry management ----

    @classmethod
    def register_case_types(cls, *case_classes: "type[FolderBackedCase]") -> None:
        """Explicit, opt-in registration of one or more case types (no auto-register).
        Each class is keyed by its bare __name__ — the EXACT value stamped into every
        record's case_object_type (see create_in_folder / reclassify_to) and enforced by
        _flush_record's guard — so the class name in code is guaranteed to match the name
        on disk. Single or many: register_case_types(A) or register_case_types(A, B, C)."""
        for case_cls in case_classes:
            cls._registry[case_cls.__name__] = case_cls

    @classmethod
    def register(cls, case_cls: "type[FolderBackedCase]") -> "type[FolderBackedCase]":
        """Class-decorator sugar for one-line self-registration. Returns the class
        unchanged so it composes cleanly:

            @FolderBackedCase.register
            class TicketCase(FolderBackedCase):
                ...

        Equivalent to register_case_types(TicketCase) after the definition. Runs at
        class-definition time, when the class object already exists in full."""
        cls.register_case_types(case_cls)
        return case_cls

    @classmethod
    def resolve_case_type(
        cls,
        type_name: Optional[str],
        *,
        registry=None,
    ) -> "type[FolderBackedCase] | None":
        """Look up a class by its stored bare name. `registry` overrides the singleton
        (tests / isolation); None means use the global singleton."""
        if type_name is None:
            return None
        return (registry if registry is not None else cls._registry).get(type_name)

    # ---- ID generation ----

    @classmethod
    def new_standard_time_slug(cls) -> str:
        """The canonical default case_id scheme: a short, sortable, base-36
        millisecond time slug. Public so a subclass with a custom
        generate_case_id() can still fall back to (or compose) the standard."""
        return _new_time_slug()

    @classmethod
    def generate_case_id(cls) -> str:
        """Auto-ID factory used by create_in_folder() when no explicit case_id is
        supplied. PUBLIC, overridable extension point — a subclass may return a
        UUID, a domain-prefixed id, a sequential counter, etc. The default is the
        standard time slug. (Must be a classmethod: the id is minted before the
        instance exists, so there is no `self` to hang an instance method on.)"""
        return cls.new_standard_time_slug()

    # ---- construction ----

    def __init__(self, case_folder: Path):
        # ALWAYS folder-anchored (same in standalone and manager modes) and
        # MANAGER-AGNOSTIC. Load-only: brand-new cases come via create_in_folder().
        self._folder = Path(case_folder)
        self._holds_lease = False         # set True only on a successful _acquire_lease()
        self._released = False
        # without_lock=True: the case lease (§5d) is our single-owner mechanism;
        # the mixin's file lock is redundant for reads and would block re-opens.
        # save() still acquires its own short-lived lock per write.
        self._record: CaseRecord = self._record_cls.open(
            str(self._folder / RECORD_NAME), without_lock=True
        )
        self._events = CaseEventLogReader.for_folder(self._folder)
        self._assets = CaseAssets(self._folder)   # asset playground + retention manifest
        self._listeners: list = []        # fn(case, event_name, info); how a manager subscribes
        self._stepping = False            # in-memory: a step is executing right now (this process)
        # State is DERIVED from the event log on load; cached in self.state for transitions.
        self.state: str = self._derive_state() or self._fsm.initial_state
        # Event-log mtime is LOCAL naive (datetime.fromtimestamp); astimezone() reads a
        # naive value as local and converts to aware UTC. record.created is already
        # aware UTC (CaseRecord validator), so _last_activity is aware UTC either way.
        _ev_activity = self._events.last_activity
        self._last_activity: datetime.datetime = (
            _ev_activity.astimezone(datetime.timezone.utc)
            if _ev_activity is not None
            else self._record.created
        )
        # When the CURRENT state was entered — the dwell anchor for time guards (@<dur>).
        # Derived from the latest ENTER_STATE mtime (LOCAL naive; astimezone reads it as
        # local -> aware UTC); a brand-new case has none yet, so fall back to created.
        _enter = next(self._events.primitive.events(label_glob="ENTER_STATE"), None)
        self._state_entered_at: datetime.datetime = (
            _enter.mtime.astimezone(datetime.timezone.utc)
            if _enter is not None
            else self._record.created
        )
        # Acquire single ownership of the folder (§5d). AFTER state is known because the
        # lease TTL is state-aware. Raises CaseAlreadyOpenError if another owner holds it.
        self._my_mtime: Optional[float] = None
        self._last_beat_local: float = 0.0
        self._acquire_lease()
        self._machine = self._build_machine(self.state)

    @classmethod
    def create_in_folder(
        cls,
        case_folder: Path,
        *,
        case_id: Optional[str] = None,
        external_key: Optional[str] = None,
        nickname: Optional[str] = None,
        **fields,
    ) -> "FolderBackedCase":
        """First-time inception, shared by CaseManager.create_case() and standalone (§6c).
        Writes the skinny record, constructs (load-only), and logs the lifecycle bookend
        CASE_NEW plus the initial ENTER_STATE."""
        case_folder = Path(case_folder)
        case_folder.mkdir(parents=True, exist_ok=True)
        record = cls._record_cls(
            case_object_type=cls.__name__,
            case_id=case_id or cls.generate_case_id(),
            external_key=external_key,
            nickname=nickname,
            created=_utcnow(),
            **fields,
        )
        # Direct save (no instance yet) — SAFE BY CONSTRUCTION: case_object_type is
        # set to cls.__name__ here, so it satisfies the §5b type-name guard.
        record.save(str(case_folder / RECORD_NAME))
        case = cls(case_folder)
        case._events.primitive.create_event(
            "CASE_NEW", cls.__name__,
            {"case_id": record.case_id, "external_key": record.external_key},
        )
        case._events.primitive.create_event("ENTER_STATE", cls._fsm.initial_state)
        return case

    # ---- type resolution (shared by rehydrate + peek_record) ----

    @staticmethod
    def _sniff_case_type(folder: Path) -> Optional[str]:
        """Cheaply read the record's case_object_type WITHOUT full Pydantic validation.
        case_object_type is the FIRST CaseRecord field and YAML is emitted in definition
        order (sort_keys=False), so it reliably appears at the top of the file."""
        record_path = Path(folder) / RECORD_NAME
        try:
            text = record_path.read_text()
        except FileNotFoundError:
            return None
        m = re.search(r'^case_object_type:\s*["\']?([^"\'\s]+)', text, re.M)
        return m.group(1) if m else None

    @classmethod
    def rehydrate(cls, folder: Path, *, registry=None) -> "FolderBackedCase":
        """Open the folder as the CORRECT FolderBackedCase subclass (registry lookup on
        the sniffed type). The live-object analog of peek_record(); both share the same
        resolution. RAISES UnregisteredCaseTypeError for an unknown type — you cannot
        build behavior without the class (contrast peek_record, which degrades)."""
        tname = cls._sniff_case_type(folder)
        case_cls = cls.resolve_case_type(tname, registry=registry)
        if case_cls is None:
            raise UnregisteredCaseTypeError(tname)
        return case_cls(folder)

    # ---- peek accessors: read a case without constructing a live instance (§5c) ----

    @classmethod
    def peek_record(
        cls,
        folder: Path,
        *,
        record_cls: Optional[type[CaseRecord]] = None,
        registry=None,
    ) -> CaseRecord:
        """The identity record, read straight from disk with the CORRECT typed record
        class when resolvable:
          - record_cls given     → use it directly (no sniff, no registry).
          - else                 → sniff case_object_type, resolve via registry.
          - unregistered/unknown → fall back to base CaseRecord (graceful, §4a)."""
        if record_cls is None:
            case_cls = cls.resolve_case_type(cls._sniff_case_type(folder), registry=registry)
            record_cls = case_cls._record_cls if case_cls else CaseRecord
        # Peek is explicitly lock-free; the case lease is not ours to take here.
        return record_cls.open(str(Path(folder) / RECORD_NAME), without_lock=True)

    @staticmethod
    def peek_events(folder: Path) -> CaseEventLogReader:
        """A CaseEventLogReader over the folder's event log (§4c): current_state,
        status/is_closed, last_activity, and .primitive for the raw log — all without
        constructing or owning a live case."""
        return CaseEventLogReader.for_folder(Path(folder))

    @staticmethod
    def peek_assets(folder: Path) -> CaseAssets:
        """A CaseAssets over the folder — list_assets(), keep_list(), etc. — without
        constructing or owning a live case. The peek analog of case.assets."""
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

    def _derive_state(self) -> Optional[str]:
        """Current state = the most recent ENTER_STATE entry. Delegates to the same
        CaseEventLogReader the peek path uses — no-drift guarantee is structural."""
        return self._events.current_state

    # ---- identity / status properties ----

    @property
    def case_id(self) -> str:
        return self._record.case_id

    @property
    def external_key(self) -> Optional[str]:
        return self._record.external_key

    @property
    def nickname(self) -> Optional[str]:
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

    def is_idle_for(self, seconds: float) -> bool:
        return (_utcnow() - self._last_activity).total_seconds() >= seconds

    # ---- activity / lifecycle introspection (§6e) ----

    @property
    def next_step(self) -> Optional[str]:
        """The first auto-advance trigger from the current state, else None. With guards,
        this names the highest-priority CANDIDATE; whether it actually fires depends on its
        guard at advance()-time."""
        candidates = self._forward_candidates(self.state)
        return candidates[0][0] if candidates else None

    @property
    def is_runnable(self) -> bool:
        """Open AND at least one auto-advance candidate is DEFINED from the current state.
        Note: with guards, a runnable case can still no-op if every guard declines —
        runnable means 'there is something to try', not 'a step is guaranteed'."""
        return self.is_open and self.next_step is not None

    @property
    def is_awaiting(self) -> bool:
        """Open but with no auto-advance candidate defined — blocked on external input / a
        human / an event-driven (un-starred) transition."""
        return self.is_open and self.next_step is None

    @property
    def is_stepping(self) -> bool:
        """In-memory: a step is mid-execution in THIS process."""
        return self._stepping

    # ---- FSM attachment via transitions (async-first composition pattern) ----

    def _build_machine(self, initial_state: str) -> AsyncMachine:
        # send_event=True so the global after-hook receives EventData and can see
        # BOTH the source and dest of each transition (needed for the closing edge).
        # on_exception funnels EVERY callback failure (guard / _perform_ / on_enter /
        # on_exit / after) through one handler regardless of how the trigger was fired.
        return AsyncMachine(
            model=self,
            states=self._fsm.states,
            transitions=self._prepare_machine_transitions(self._fsm.transitions),
            initial=initial_state,
            after_state_change="_on_state_changed",
            on_exception="_on_fsm_exception",
            send_event=True,
        )

    def _prepare_machine_transitions(self, transitions: list[dict]) -> list[dict]:
        """Return machine-ready transition dicts from the per-class `_fsm` spec, applying
        the two conventions the parser leaves for the live instance to bind, and stripping
        the spec's private `_`-prefixed keys (e.g. `_min_dwell_secs`, `_wildcard`) that the
        `transitions` library would reject. The shared `_fsm` spec is never mutated — each
        edge is rebuilt as a fresh dict.

        1. TIME GUARD — a `_min_dwell_secs` (from a `@<dur>` token) is compiled into an
           extra `conditions` callable that is true once the case has dwelled at least that
           long in its current state (measured from `self._state_entered_at`). It is a pure
           factual guard; it does not drive anything.
        2. `_perform_<trigger>` -> `before` — the work a trigger implies belongs in
           `before`: if it raises, the transition ABORTS and the case stays in its source
           state (no ENTER_STATE logged), so "retry" is just "fire the trigger again" and a
           crash mid-work resumes cleanly (§6a). Opt-in and non-clobbering: wired ONLY when
           the method exists AND no `before` was already declared (a compile_fsm() override
           keeps full control). A declarative FSM with no `_perform_` methods stays
           callback-free."""
        prepared: list[dict] = []
        for td in transitions:
            trigger = td["trigger"]
            clean = {k: v for k, v in td.items() if not k.startswith("_")}
            min_dwell = td.get("_min_dwell_secs")
            if min_dwell is not None:
                conds = list(clean.get("conditions", []))
                conds.append(self._make_dwell_condition(min_dwell))
                clean["conditions"] = conds
            if "before" not in clean:
                method = f"_perform_{trigger}"
                if callable(getattr(type(self), method, None)):
                    clean["before"] = method
            prepared.append(clean)
        return prepared

    def _make_dwell_condition(self, min_secs: float):
        """Build a `transitions` condition callable for a `@<dur>` time guard: true once at
        least `min_secs` have elapsed since the current state was entered. Bound to this
        instance so each model gets its own check (send_event=True => receives EventData)."""
        def _dwell_elapsed(event) -> bool:
            return (_utcnow() - self._state_entered_at).total_seconds() >= min_secs
        return _dwell_elapsed

    def _on_state_changed(self, event) -> None:
        """Runs after EVERY transition (event is a transitions EventData). Records the
        state-change entry, then on non-closing transitions throttled-flushes the record
        and beats the lease. On the non-closed → closed EDGE, runs the two-phase close.

        Two-phase closing (CASE_CLOSING / CASE_CLOSED distinction):
          Phase 1 — PRE-FINALIZATION (assets still exist):
            1. Log CASE_CLOSED event
            2. on_closing() — subclass retains/extracts final artifacts
            3. _notify("CASE_CLOSING") — pre-purge observers (audit, test harness)
          Phase 2 — POST-FINALIZATION (immutable, safe to move):
            4. assets.purge_ephemeral() — drop everything not in the keep manifest
            5. _record.closed stamped + FORCE-flushed (authoritative seal)
            6. release() — drop the lease BEFORE any archival folder move
            7. _notify("CASE_CLOSED") — manager archives; standalone: no-op
        """
        src, dest = event.transition.source, event.transition.dest
        self._events.primitive.create_event("ENTER_STATE", dest)
        self._last_activity = _utcnow()
        self._state_entered_at = self._last_activity   # reset the time-guard dwell anchor
        closing = src not in self._fsm.closed_states and dest in self._fsm.closed_states
        if not closing:
            # Throttled flush + lease beat at the boundary. Skipped on the closing edge:
            # the forced phase-2 seal supersedes the flush, and phase 2 releases the lease.
            if self.autoflush_record_on_transition:
                self._flush_record()
            self.heartbeat()
        if closing:
            # --- phase 1: pre-finalization --- assets still present ---
            self._events.primitive.create_event("CASE_CLOSED", dest, {"from": src})
            self.on_closing()
            self._notify("CASE_CLOSING", src=src, dest=dest)
            # --- phase 2: post-finalization --- assets gone, record sealed ---
            self._assets.purge_ephemeral()
            self._record.closed = self._last_activity
            self._flush_record(force=True)
            self.release()                     # drop lease BEFORE any archival move (§5d)
            self._notify("CASE_CLOSED", src=src, dest=dest)

    async def _on_fsm_exception(self, event) -> None:
        """Machine-level `on_exception` hook (wired in _build_machine): fires when ANY
        callback in a trigger's dispatch raises — a guard, a `_perform_<trigger>`, an
        on_enter/on_exit, or an `after`. `transitions` has already aborted the transition
        (the model stayed in its source state) by the time we get here.

        We do three things, in order:
          1. DECORATE the exception with structured FSM context under `case_context`
             (case_id + the trigger and intended source/dest). A driver such as a
             CaseManager — which by design does NOT understand any case's internals —
             can branch on this to take its crude actions (kill / monitor / retry /
             escalate) without parsing the message or knowing the case type. Decoration
             happens HERE rather than in advance() so the context is attached however the
             trigger was fired (direct `await case.x()` as well as via advance()).
          2. Log a CASE_ALERT (type-agnostic escalation marker) capturing the same
             context in the low-volume event log, so the failure is visible on disk.
          3. RE-RAISE the original exception (preserving its type and traceback) so the
             abort stays fail-fast and the caller still sees the real error.

        FUTURE ENHANCEMENT (deferred): EventData does not reveal WHICH callback slot
        raised, so we cannot yet distinguish e.g. an operational `_perform_` failure
        from a (likely-defect) guard exception. That phase attribution can be added by
        wrapping the callbacks we ourselves wire; not implemented until a need appears."""
        err = event.error
        trigger = event.event.name if event.event is not None else None
        trans = event.transition
        src = trans.source if trans is not None else self.state
        dest = trans.dest if trans is not None else None
        context = {
            "case_id": self.case_id,
            "trigger": trigger,
            "source": src,
            "dest": dest,
        }
        try:
            err.case_context = context        # best-effort; some exceptions forbid attrs
        except (AttributeError, TypeError):
            pass
        self.log_alert(
            f"unhandled {type(err).__name__} in {trigger} ({src} -> {dest})",
            where=src,
        )
        raise err

    def on_closing(self) -> None:
        """Overridable hook fired in phase 1 (pre-finalization): assets still exist,
        record not yet stamped. Override to retain/extract final artifacts before the
        ephemeral purge. Default: no-op. Heavy async finalization belongs in a `before`
        callback on the closing transition instead — this hook is sync cleanup."""

    # ---- record flush / fetch protocol (single guarded chokepoint, §5b) ----
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

    # ---- single-owner protection: the heartbeat lease (§5d) ----
    # A content-free `.case.lease` file whose mtime is a "valid-until" expiry. A FUTURE
    # mtime => still held; past/absent => free. The owner re-beats to push the expiry
    # forward; the exact mtime it last wrote also serves as its claim token.

    def _lease_path(self) -> Path:
        return self._folder / LEASE_NAME

    def _on_disk_mtime(self) -> Optional[float]:
        try:
            return self._lease_path().stat().st_mtime
        except FileNotFoundError:
            return None

    def lease_ttl_for(self, state: str) -> float:
        """Seconds the lease stays valid after a beat, for `state`. Override per state
        for long-idle windows. MUST comfortably exceed the gap between heartbeat() calls
        (≈ pulse_interval_secs + slack), or a live-but-quiet owner can be reclaimed."""
        return 300.0

    def _beat(self) -> None:
        expiry = time.time() + self.lease_ttl_for(self.state)
        self._lease_path().touch(exist_ok=True)
        os.utime(self._lease_path(), (expiry, expiry))
        self._my_mtime = self._on_disk_mtime()   # remember EXACTLY what we wrote
        self._last_beat_local = time.monotonic()  # throttle clock (jump-immune)
        self._holds_lease = True
        self._released = False

    def _acquire_lease(self) -> None:
        m = self._on_disk_mtime()
        if m is not None and m > time.time():    # future expiry => still held
            raise CaseAlreadyOpenError(self._folder, expires_in=m - time.time())
        self._beat()                             # absent/expired => claim (or reclaim)

    def heartbeat(
        self,
        *,
        min_update_secs: float = 15.0,
        validate_ownership: bool = True,
    ) -> None:
        """Extend our lease (mtime = now + lease_ttl_for(state)). Throttled: a no-op if
        < min_update_secs since our last beat, so tight loops may call freely; pass
        min_update_secs=0 to FORCE a check+beat now. With validate_ownership (default),
        raises OwnershipLostError (FATAL) if the on-disk mtime no longer matches what we
        last wrote — another owner reclaimed us past our TTL, or the lease vanished."""
        self._check_active()
        if time.monotonic() - self._last_beat_local < min_update_secs:
            return
        if validate_ownership and self._on_disk_mtime() != self._my_mtime:
            raise OwnershipLostError(self._folder)
        self._beat()

    def release(self) -> None:
        """Relinquish the claim: delete the lease and mark this instance detached.
        Idempotent. This is NOT an FSM close and does NOTHING to the case's assets —
        it only drops ownership of the folder. Any later mutating use raises
        ReleasedCaseError. Wired to __exit__ (context manager) and best-effort __del__;
        on a crash the lease simply expires via its TTL instead."""
        if self._released or not self._holds_lease:
            return                               # never acquired => never delete
        self._lease_path().unlink(missing_ok=True)
        self._released = True
        self._holds_lease = False

    def _check_active(self) -> None:
        if self._released:
            raise ReleasedCaseError(self._folder)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> Optional[bool]:
        """Lock-free staleness read for a manager's recovery sweep: True if the lease
        has expired (reclaimable), False if still held, None if unheld. Policy-free —
        the expiry is baked into the mtime, so no state/TTL lookup is needed here."""
        try:
            return (Path(folder) / LEASE_NAME).stat().st_mtime <= time.time()
        except FileNotFoundError:
            return None

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

    async def advance(self) -> bool:
        """Fire ONE forward step from the current state and return True if a transition
        occurred. Tries each auto-advance candidate in declared order, firing the first
        whose guard permits; returns False when terminal, when nothing auto-advances, or
        when every candidate's guard declines. Each fired step persists before the next,
        so the loop is flat and fully resumable."""
        if self.is_closed:              # terminal short-circuits first (a closed case
            return False                # auto-releases its lease in phase 2, §5d)
        self._check_active()            # else refuse to drive a released husk
        candidates = self._forward_candidates(self.state)
        if not candidates:
            return False
        self._stepping = True
        try:
            for trigger, _dest in candidates:
                # A guard-blocked transition returns falsy WITHOUT changing state (and
                # without firing the after-hook), so we simply fall through to the next.
                if await getattr(self, trigger)():
                    return True
            return False
        finally:
            self._stepping = False

    async def run_to_completion(self, *, stop_before: Optional[str] = None) -> None:
        """Drive forward until closed, until nothing auto-advances, or until an auto step
        from the current state could ENTER `stop_before` (for staged inspection / testing).
        Delegates to advance() so _stepping is set correctly for each step."""
        while self.is_open:
            candidates = self._forward_candidates(self.state)
            if not candidates:
                break
            if stop_before and any(dest == stop_before for _, dest in candidates):
                break
            if not await self.advance():     # all guards declined -> nothing left to do
                break

    # ---- reclassify ("call an audible" to a different subclass) ----

    def reclassify_to(
        self, new_cls: "type[FolderBackedCase]"
    ) -> "FolderBackedCase":
        """Rebind this case to a different FolderBackedCase subclass via a two-phase
        COMMIT, logging a RECLASSIFY event. The CALLER owns compatibility.

        Two-phase commit (crash-atomic):
          Phase 1 — snapshot old repr in the OLD class's schema; release lease.
          Phase 2 — NEW class acquires the now-free lease, CONSCIOUSLY stamps its
                    own name, force-commits in its own schema.
        A crash between phases reopens cleanly as the OLD class (name not switched)."""
        if self.state not in new_cls._fsm.states:
            raise IncompatibleReclassError(self.state, new_cls.__name__)
        from_name = self._record.case_object_type
        self._flush_record(force=True)           # phase 1: snapshot old repr (old name)
        self.release()                           # serialize-then-RELEASE, then re-acquire
        fresh = new_cls(self._folder)            # acquires the now-free lease; old repr on disk
        fresh._events.primitive.create_event(
            "RECLASSIFY", new_cls.__name__,
            {"from": from_name, "at_state": fresh.state},
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
        Use case.assets.write(...), .keep_asset(...), .list_assets(), etc. Kept off this
        class's own namespace so asset concerns stay grouped in one place."""
        return self._assets

    # ---- archive label hook ----

    def archive_grouping_label(self) -> str:
        """Destination archive grouping when this case closes. Default: close month.
        Override to key on creation date, fiscal period, tenant, etc."""
        return _utcnow().strftime("%Y-%m-archive")

    # ---- operator alert channel (type-agnostic escalation marker) ----

    def log_alert(self, short_msg: str = "", *, where: Optional[str] = None) -> None:
        """Record a CASE_ALERT: a deliberate, type-agnostic "this case needs a human
        or administrator to look at it" marker in the event log.

        This is the case family's single generic escalation channel. Because it reads
        the same for every case type, an observer (a CaseManager sweep, a dashboard, a
        person grepping folders) can surface cases needing attention WITHOUT
        understanding any particular case type's internal state vocabulary.

        Orthogonal to the FSM: it does NOT change state, close the case, or imply a
        terminal/failure state. A case may log zero, one, or several over its lifetime.

        Use SPARINGLY — the event log is a low-volume audit trail (tens of entries, not
        thousands), so every alert should be worth a reader's time. Raise one when the
        case has deviated in a way that warrants out-of-band examination, e.g.:

          * Integrity risk — outputs may be inconsistent or violate an agreed protocol
            (e.g. a non-recoverable write failed partway through).
          * Substantial deviation from norms — a behavioral/SLA envelope was breached
            (e.g. a step expected to take 5 minutes took 30).

        Do NOT use it for routine, recoverable defects or normal exception handling the
        flow is designed to absorb (retries, expected guard declines) — that is ordinary
        control flow, not an alert.

        Args:
            short_msg: a brief human-readable reason. The event log holds only short
                text — keep it to a terse phrase, not a stack trace.
            where: optional label for the locus of concern; defaults to the current
                state, usually the most useful starting point for examination.
        """
        self._events.primitive.create_event(
            "CASE_ALERT", where or self.state, {"msg": short_msg}
        )

    # ---- pulse (stall-detection; subclasses override to escalate) ----

    @property
    def pulse_interval_secs(self) -> float:
        return 300.0

    async def pulse(self) -> None:
        """Called periodically by an external scheduler (or CaseManager). Logs an idle
        marker event and beats the lease. Override to escalate on stall or fire FSM
        triggers on timeout.

        PLANNED (next design pass — not yet implemented): pulse()/advance() will also
        re-evaluate TIME-GUARDED auto edges (the `@<dur>` guard, e.g. `*@60m#expire`),
        firing one once its minimum dwell in the current state has elapsed. The dwell is
        measured from the latest ENTER_STATE timestamp; the time guard is a floor ("at
        least N elapsed"), never a promise to fire exactly at N. Interrupting an
        in-progress transition is explicitly OUT of scope for that work."""
        idle = (_utcnow() - self._last_activity).total_seconds()
        self._events.primitive.create_event("PULSE", "idle", {"idle_secs": round(idle)})
        self.heartbeat()               # piggyback liveness on the pulse cadence (§5d)
