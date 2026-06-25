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
        _fsm_states      = ["new", "open", "closed"]
        _fsm_transitions = [
            {"trigger": "open_ticket",  "source": "new",  "dest": "open"},
            {"trigger": "close_ticket", "source": "open", "dest": "closed"},
        ]
        _closed_states   = {"closed"}

    FolderBackedCase.register_case_type(TicketCase)

    case = TicketCase.create_in_folder(Path("/data/cases/t-001"), case_id="t-001")
    with case:
        await case.open_ticket()
        await case.close_ticket()
    # lease auto-released by context manager; folder is fully self-contained

See FolderBackedCase Model.md for the full design narrative.
"""

from __future__ import annotations

import datetime
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
)
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.case_event_log_reader import CaseEventLogReader
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets

__all__ = [
    "FolderBackedCase",
    "CaseRecord",
    "CaseEventLogReader",
    "CaseAssets",
    "CaseAlreadyOpenError",
    "OwnershipLostError",
    "ReleasedCaseError",
    "UnregisteredCaseTypeError",
    "RecordTypeMismatchError",
    "IncompatibleReclassError",
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

    Subclasses declare:
        _fsm_states       list of state names
        _fsm_transitions  list of transition dicts (trigger/source/dest + optional callbacks)
        _closed_states    set of terminal state names (pulse suppressed; close hook fires)
        _pipeline         optional ordered list of forward trigger names for advance()

    See FolderBackedCase Model.md for the full design narrative.
    """

    _fsm_states: list = []
    _fsm_transitions: list = []
    _fsm_initial_state: str = "new"
    _closed_states: set = {"closed", "failed", "abandoned"}

    # Linear-pipeline subclasses list their FORWARD trigger names, in order, so the
    # generic driver can advance one step at a time. Human-driven cases leave it empty.
    _pipeline: list = []

    # Record extensibility (§4b): point record_cls at a CaseRecord subclass to carry
    # extra TYPED fields. Subclasses MAY mutate those fields mid-life; keep the record
    # as non-volatile as reasonably possible — volatile/derived data belongs in the
    # event log — but infrequently-changed fields are fine here.
    record_cls: type[CaseRecord] = CaseRecord

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
    def register_case_type(
        cls,
        case_cls: "type[FolderBackedCase]",
        *,
        name: Optional[str] = None,
    ) -> None:
        """Explicit, opt-in registration (bare logical name → class). No auto-register."""
        cls._registry[name or case_cls.__name__] = case_cls

    @classmethod
    def register_case_types(cls, classes) -> None:
        for c in classes:
            cls.register_case_type(c)

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
        self._record: CaseRecord = self.record_cls.open(
            str(self._folder / RECORD_NAME), without_lock=True
        )
        self._events = CaseEventLogReader.for_folder(self._folder)
        self._assets = CaseAssets(self._folder)   # asset playground + retention manifest
        self._listeners: list = []        # fn(case, event_name, info); how a manager subscribes
        self._stepping = False            # in-memory: a step is executing right now (this process)
        # State is DERIVED from the event log on load; cached in self.state for transitions.
        self.state: str = self._derive_state() or self._fsm_initial_state
        # Event-log mtime is LOCAL naive (datetime.fromtimestamp); astimezone() reads a
        # naive value as local and converts to aware UTC. record.created is already
        # aware UTC (CaseRecord validator), so _last_activity is aware UTC either way.
        _ev_activity = self._events.last_activity
        self._last_activity: datetime.datetime = (
            _ev_activity.astimezone(datetime.timezone.utc)
            if _ev_activity is not None
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
        record = cls.record_cls(
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
        case._events.primitive.create_event("ENTER_STATE", cls._fsm_initial_state)
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
            record_cls = case_cls.record_cls if case_cls else CaseRecord
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
        return self.state not in self._closed_states

    @property
    def is_closed(self) -> bool:
        return self.state in self._closed_states

    def is_idle_for(self, seconds: float) -> bool:
        return (_utcnow() - self._last_activity).total_seconds() >= seconds

    # ---- activity / lifecycle introspection (§6e) ----

    @property
    def next_step(self) -> Optional[str]:
        """The forward _pipeline trigger ready from the current state, else None."""
        nxt = self._next_forward(self.state)
        return nxt[0] if nxt else None

    @property
    def is_runnable(self) -> bool:
        """Open AND a forward step is ready right now."""
        return self.is_open and self.next_step is not None

    @property
    def is_awaiting(self) -> bool:
        """Open but blocked on external input / a human."""
        return self.is_open and self.next_step is None

    @property
    def is_stepping(self) -> bool:
        """In-memory: a step is mid-execution in THIS process."""
        return self._stepping

    # ---- FSM attachment via transitions (async-first composition pattern) ----

    def _build_machine(self, initial_state: str) -> AsyncMachine:
        # send_event=True so the global after-hook receives EventData and can see
        # BOTH the source and dest of each transition (needed for the closing edge).
        return AsyncMachine(
            model=self,
            states=self._fsm_states,
            transitions=self._fsm_transitions,
            initial=initial_state,
            after_state_change="_on_state_changed",
            send_event=True,
        )

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
        closing = src not in self._closed_states and dest in self._closed_states
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

    def reload_record(self) -> None:
        """Opt-in re-read of the record from disk. The base fetches the record EXACTLY
        ONCE at construction and never silently re-reads. Call this explicitly if an
        external process may have updated the file."""
        self._record.reload_from_file()

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

    def _next_forward(self, state: str):
        """(trigger, dest) of the single forward _pipeline transition whose source is
        `state`, or None when terminal / nothing applies (e.g. awaiting input)."""
        for t in self._fsm_transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if t["trigger"] in self._pipeline and state in srcs:
                return t["trigger"], t["dest"]
        return None

    async def advance(self) -> bool:
        """Fire EXACTLY ONE forward transition from the current state. Returns False
        when terminal or no forward step applies. Each step persists before the next,
        so the loop is flat and fully resumable."""
        if self.is_closed:              # terminal short-circuits first (a closed case
            return False                # auto-releases its lease in phase 2, §5d)
        self._check_active()            # else refuse to drive a released husk
        nxt = self._next_forward(self.state)
        if nxt is None:
            return False
        self._stepping = True
        try:
            await getattr(self, nxt[0])()
        finally:
            self._stepping = False
        return True

    async def run_to_completion(self, *, stop_before: Optional[str] = None) -> None:
        """Drive forward until closed, or until the next step would ENTER `stop_before`
        (for staged inspection / testing). Delegates to advance() so _stepping is set
        correctly for each step."""
        while self.is_open:
            nxt = self._next_forward(self.state)
            if nxt is None or (stop_before and nxt[1] == stop_before):
                break
            await self.advance()

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
        if self.state not in new_cls._fsm_states:
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

    # ---- pulse (stall-detection; subclasses override to escalate) ----

    @property
    def pulse_interval_secs(self) -> float:
        return 300.0

    async def pulse(self) -> None:
        """Called periodically by an external scheduler (or CaseManager). Logs an idle
        marker event and beats the lease. Override to escalate on stall or fire FSM
        triggers on timeout."""
        idle = (_utcnow() - self._last_activity).total_seconds()
        self._events.primitive.create_event("PULSE", "idle", {"idle_secs": round(idle)})
        self.heartbeat()               # piggyback liveness on the pulse cadence (§5d)
