# FolderBackedCase — Design Model

> **Status:** DRAFT — illustrative code is design-fidelity, **not** production
> code. No implementation until approved.
>
> **FSM library:** **`transitions`** (pytransitions).
>
> **Companion document:** `CaseManager Model.md` covers one complete
> implementation of a collection host built on top of this class family.
> It is unnecessary to understand CaseManager unless you are using it as your persistence/container.


> It is unnecessary to understand CaseManager unless you are using it as your persistence/container.---

## 1. Background

The class names (`FolderBackedCase`, concrete subclasses), many method names,
and the FSM-by-composition pattern come from an initial design sketch by an
external architect. This document grounds that sketch on the `totodev-pub`
building blocks (`FileMappedPydanticMixin`, `PrimitiveEventLog`) and incorporates
all subsequent design decisions. FSM implementation uses **`transitions`**
(pytransitions) throughout.

---

## 2. The two core pieces

1. **`CaseRecord(BaseModel, FileMappedPydanticMixin)`** — the deliberately
   **skinny**, near-immutable serialized record (`case_record.yaml`, always
   *inside* the case folder). An identity card only: it carries no volatile
   data — **status, activity time, and the retained-file set are all derived from
   the event log** (cached in memory while the case is live), never stored here.
   The Pydantic layer stays dumb; `FileMappedPydanticMixin` provides persistence,
   locking, change-tracking, the non-serialized bound filepath, and clean YAML
   diffs.

2. **`FolderBackedCase(ABC)`** — the logic base class. It OWNS one `CaseRecord`,
   hosts an **async-first** `transitions` FSM (`model=self`), owns the per-case
   file area (its case folder) and a file-based `PrimitiveEventLog`, and exposes
   `pulse()`, `write_file()`, `read_file()`, `retain_file()`, `is_open`,
   `is_closed`, `is_idle_for()`, the flat `advance()` / `run_to_completion()`
   pipeline driver, the `on_closing()` closing-edge hook, and
   `reclassify_case()`. Concrete subclasses (`TicketCase`, `InboundDocCase`, …)
   declare `_fsm_states` / `_fsm_transitions` / `_closed_states` (and, for
   linear pipelines, `_pipeline`) and override hooks.

For collection management — creating, listing, hydrating, and archiving cases at
scale — see **`CaseManager Model.md`**.

---

## 3. The case folder — one symmetric layout

The single most important inspectability rule: **the case folder has the same
internal shape in every mode**, and `case_record.yaml` always lives *inside* it.
A human or agent that opens any case folder — whether it was created standalone
or by a `CaseManager`, whether it's open or archived — sees an identical layout.

```text
<case_folder>/
  case_record.yaml          # the skinny record — ALWAYS here, by a fixed name
  .case.lease               # single-owner lease (§5d): content-free; mtime = "valid-until".
                            #   runtime-only — created on acquire, removed on release/close.
  events/                   # PrimitiveEventLog: SOURCE OF TRUTH for status + retention
    e001_ENTER_STATE@new.yaml ...
  assets/                   # write_file() targets
    source/  raw.pdf ...    #   ephemeral inputs (purged on close)
    derived/ text.txt ...   #   retained outputs (survive close, travel to archive)
```

This layout is **identical** whether the case is standalone or lives inside a
`CachedFileFolders` collection. In collection mode the case folder happens to be
the **slave dir** of a tracked entry; in standalone mode it is any directory you
hand the constructor. The record never moves within the folder.

In **standalone mode** (§6c) there is no outer collection at all — the case
folder is any directory you hand the constructor.

Why this layout:
- **Symmetry / inspectability** — the case folder is self-describing regardless
  of how it was created; the record never relocates within it.
- **The whole case folder travels** on archive/reopen, so record + assets +
  event log move atomically.
- **`CachedFileFolders` fit** — the folder is designed to sit as a slave dir of
  a tracked entry in a `CachedFileFolders` grouping. The tracked file becomes a
  thin listing card; the case folder is its slave dir. See `CaseManager Model.md`
  for the full collection layout.

**Design philosophy — tolerate foreign files.** A case must be a *good neighbor*
in its own folder: it owns `case_record.yaml`, `events/`, `assets/`, and its own
`.case.lease` (§5d), and it must **ignore** anything else it doesn't recognize rather than choking on or
deleting it. In particular, `CachedFileFolders` may drop its own bookkeeping into
the tree, and tools or humans may leave notes. Concretely: cleanup is scoped to
subtrees the case owns (`_purge_ephemeral_files()` only touches `assets/`), and
listing/iteration skips unknowns.

---

## 4. `CaseRecord` — the skinny, near-immutable identity card

```python
from datetime import datetime
from pydantic import BaseModel
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

class CaseRecord(BaseModel, FileMappedPydanticMixin):   # BaseModel FIRST (mixin rule)
    """Deliberately SKINNY. An identity card, not a state store. No volatile data:
    status, last-activity time, and the retained-file set are all DERIVED from the
    event log (and cached in memory while the case is live), never persisted here."""
    case_object_type: str             # bare class __name__; resolved via the registry at hydration
    case_id: str                      # natural internal id (default: time-based base36 slug)
    external_key: str | None = None   # caller-supplied id in an external system
    nickname: str | None = None       # optional human-friendly label for listings
    created: datetime                 # immutable
    closed: datetime | None = None    # stamped once on terminal entry; doubles as "is terminal?" hint
    # Intentionally NOT stored: status/state, last_activity, retained_files,
    # priority/assignee/tags. A denormalized listing field may be added LATER, but
    # ONLY if a measured listing bottleneck justifies it.
```

- Serialized as **YAML** via the mixin (its dumper uses `sort_keys=False`, so
  fields emit in definition order → clean, churn-free git diffs).
- The mixin gives us locking (`portalocker`-based lock files), change tracking,
  `persisted_file()` (the non-serialized bound path), `would_conflict()`, and
  `reload_from_file()` for safe concurrent edits.
- **Near-immutable:** the *base* record is written once at create and then touched
  only once more — at close, to stamp `closed`. That near-zero churn minimizes
  write contention and keeps diffs meaningful. Subclasses **may** add typed fields
  and mutate them mid-life under a relaxed policy (§4b), with an explicit promise
  about when those writes become durable (§5b); the guidance is to keep the record
  as low-churn as reasonably possible, not to forbid all mutation.
- **Status is not on the record.** Coarse open-vs-closed is reinforced by the
  presence of `closed`. The *fine-grained* current status is derived from the
  event log's state-change entries and cached in memory while the case is live
  (see §5). This removes any persisted cache that could drift out of sync.
- **The retained-file set is not on the record** either — it is reconstructed
  from `FILE` entries in the event log (write-with-retain / explicit retain).
- The full event history lives in the case folder's `events/` (`PrimitiveEventLog`),
  never on the record.

---

## 4a. Hydration, the case-type registry, and the constructor contract

**The record stores a bare class name.** `case_object_type` is exactly
`cls.__name__` (e.g. `"TicketCase"`) — a short *logical* name. We deliberately do
**not** store a Python import path and never reconstruct a class by importing a
string from disk (see the call-out below). Most deployments have only a handful
of case types — often just one — so a bare name is plenty.

**The registry lives on `FolderBackedCase`, not the manager.** It is a
class-level global singleton (`FolderBackedCase._registry`, a `dict[str, type]`
mapping the bare logical name → class), populated explicitly via
`FolderBackedCase.register_case_type(TicketCase)` / `.register_case_types([...])`.
This is the keystone that lets the case family resolve its own types **without a
manager** — so manager-agnostic facilities like `rehydrate()` and the smart
`peek_record()` (§5c) work standalone. A `CaseManager` keeps only thin convenience
wrappers that delegate down to the base registry; it does **not** own it.

**The registry is explicit and opt-in — never a hidden side effect.**

- Registration is an explicit call (a manager-bound `@manager.register` decorator
  is optional sugar that still delegates to the base). Creation does **not**
  auto-register.
- For our scale a single global singleton is the simple, sensible default. For
  isolation (tests, independent registries) the resolution methods accept an
  optional `registry=` argument that falls back to the singleton — so injection is
  available without complicating the common path.
- Registration is required **only** for **rehydration** (turning a stored
  `case_object_type` string back into the right class — `rehydrate()`, §5c).
  Defining a case class, instantiating it, driving its FSM, writing its event log
  and working files all work **without** registering — because creation is handed
  the concrete class directly.
- **Rehydration of an unregistered type raises** a loud, explicit
  `UnregisteredCaseTypeError(case_object_type)`. Unknown types still **degrade
  gracefully for reads**: `peek_record()` falls back to the base `CaseRecord` so the
  skinny fields (`nickname`, `case_id`, `external_key`, dates) stay readable
  without the class.
- **Renames are not designed for.** If a class is ever renamed, a manual alias
  registration (old name → new class) is the escape hatch; we do not complicate
  the class family to handle this obscure case.

> **Why not store an import path / import-by-string?** Storing
> `"myapp.cases.tickets.TicketCase"` and reconstructing via
> `importlib.import_module(...) + getattr(...)` is a robustness footgun: it
> couples on-disk data to your code's package layout (refactors orphan good
> records), runs arbitrary import side effects, widens the trust boundary
> (hand-editable YAML naming code to load), and fails to degrade (a venv missing
> the module can't even *list* cases). The explicit registry stores a short
> logical name bound to a class at startup, and degrades gracefully.

---

## 4b. Extending the record — typed subclass fields (RELAXED)

`FolderBackedCase.record_cls` (default `CaseRecord`) lets a case type swap in a
**`CaseRecord` subclass** to carry extra **typed** fields, validated and
serialized by Pydantic exactly like the base. `create_in_folder()` builds the
record via `cls.record_cls(...)` and `__init__` hydrates via
`self.record_cls.load_from_file(...)`, so the subclass schema is used end-to-end.

We chose this over a freeform `payload: dict` blob **deliberately**. A typed
subclass keeps Pydantic validation, self-documenting fields, and clean
field-ordered YAML diffs, and it composes with the registry (the stored
`case_object_type` already names the class). A freeform dict has none of those and
would invite exactly the volatile/derived data the skinny-record design (§4) works
to keep *off* the record. (If you ever genuinely need per-instance freeform data,
the event log already *is* an extensible, append-only store.)

**The policy is RELAXED, not strict — subclass fields MAY be mutated mid-life.**
The record is a convenient, already-locked place to stash an
infrequently-changed value (e.g. a ticket's `resolution_summary`). The guidance is
a gradient, not a prohibition:

- **Prefer the event log** for anything volatile or frequently-changing (status,
  retained files, progress, in-flight job handles) — it is the source of truth and
  stays append-only.
- **The record is fine** for immutable identity/config (set at create) **and** for
  occasional, low-churn mutable fields. Still, keep it as non-volatile as
  reasonably possible: `FileMappedPydanticMixin` locking exists but is neither
  specially performant nor bulletproof, so every record write is a small
  contention point.

When the record *is* mutated, **when it becomes durable is an explicit promise** —
see the flush/fetch protocol in §5b.

**Listing/hydration degrades gracefully** (§4a): a reader without the subclass
loads the base `CaseRecord`, and Pydantic's default `extra='ignore'` drops the
unknown fields — the skinny identity fields stay readable. A corollary for
reclass targets: any field a `record_cls` *adds* must have a default (or be
optional), or loading an older record through the new schema will fail.

---

## 4c. `CaseEventLogReader` — the case-convention interpreter

`PrimitiveEventLog` is deliberately **domain-agnostic** (it just stores ordered,
keyed entries). The *meaning* the case framework layers on top — `CASE_NEW` /
`CASE_CLOSED` bookends, "the latest `ENTER_STATE` is the current state," "`FILE`
entries reconstruct the retained set" — is encapsulated **once** in a small
read-oriented wrapper, `CaseEventLogReader`. Both the live `FolderBackedCase` and
the lock-free peek path (§5c) use it, so an external observer and the case's own
owner can never disagree about how the log is interpreted — the no-drift guarantee
is structural, not a convention to maintain.

```python
class CaseEventLogReader:
    """Read-oriented wrapper over the domain-agnostic PrimitiveEventLog that owns
    the INTERPRETATION of case conventions in one place. Writes are not its job:
    callers that must append go through `.primitive` (the underlying log)."""

    def __init__(self, event_dir: Path):
        self._log = PrimitiveEventLog(event_dir=event_dir)

    @classmethod
    def for_folder(cls, folder: Path) -> "CaseEventLogReader":
        return cls(folder / "events")

    @property
    def primitive(self) -> PrimitiveEventLog:
        """The underlying log — the escape hatch for everything bespoke, INCLUDING
        writes (e.g. the live case does `reader.primitive.create_event(...)`)."""
        return self._log

    # ---- convention-aware reads ----
    @property
    def current_state(self) -> str | None:          # latest ENTER_STATE = fine-grained state
        last = self._log.latest("ENTER_STATE")
        return last.value if last else None
    @property
    def is_closed(self) -> bool:                     # the CASE_CLOSED bookend = done
        return self._log.latest("CASE_CLOSED") is not None
    @property
    def status(self) -> str:                         # coarse "open" / "closed"
        return "closed" if self.is_closed else "open"
    @property
    def last_activity(self):
        return self._log.latest_timestamp()
    def retained_files(self) -> set[str]:            # replay FILE entries (write-with-retain / retain)
        keep: set[str] = set()
        for ev in self._log.entries("FILE"):
            if ev.action == "retain" or (ev.action == "write" and ev.meta.get("retain")):
                keep.add(ev.meta["path"])
        return keep
```

`CaseEventLogReader` belongs to the **case-framework** layer (it knows case
conventions); `PrimitiveEventLog` stays generic and reusable. The live object
holds one as `self._events` and reads through it (`self._events.current_state`,
`self._events.retained_files()`) while appending through
`self._events.primitive.create_event(...)`.

---

## 5. `FolderBackedCase` — the logic object

`FolderBackedCase` owns its **case folder** (record + event log + assets), hosts
the `transitions` FSM, and exposes the public lifecycle API. **The case is
manager-agnostic** — it knows only its own folder and *emits* lifecycle signals;
interested parties (like a `CaseManager`) subscribe (§5a explains why).

**Constructor contract: folder-anchored, load-only.** A `FolderBackedCase`
*always* has filesystem surface area — event log and working-files directories —
so there is no meaningful in-memory-only instance.

- The sole constructor parameter is the **case folder** (identical layout in
  standalone and manager modes, §3). The record is read from
  `<case_folder>/case_record.yaml`; events and assets from `<case_folder>/events`
  and `<case_folder>/assets`. This single anchor makes standalone and manager
  modes interchangeable.
- `__init__` is **load-only** — no create-or-load magic. Brand-new cases come
  via `create_in_folder()` (used by both `CaseManager.create_case()` and
  standalone, §6c).

```python
import datetime, os, re, shutil, time
from abc import ABC
from pathlib import Path
from transitions.extensions.asyncio import AsyncMachine   # async-first: triggers are awaitable
from totodev_pub.primitive_event_log import PrimitiveEventLog
# CaseEventLogReader: the case-convention interpreter over PrimitiveEventLog (§4c)

RECORD_NAME = "case_record.yaml"
LEASE_NAME  = ".case.lease"   # single-owner lease (§5d): content-free; mtime = "valid-until"

class FolderBackedCase(ABC):
    """
    Base class for all case types. Provides: case folder (record + event log +
    assets), async FSM via `transitions`, a flat pipeline driver, pulse hook,
    ephemeral-file retention, and the two-phase closing-edge hook.
    Subclasses declare _fsm_states / _fsm_transitions / _closed_states and
    optionally _pipeline for linear pipelines.
    """

    _fsm_states: list = []
    _fsm_transitions: list = []
    _fsm_initial_state: str = "new"
    _closed_states: set = {"closed", "failed", "abandoned"}   # pulse suppressed here
    # Linear-pipeline subclasses list their FORWARD trigger names, in order, so the
    # generic driver can advance one step at a time. Human-driven cases leave it empty.
    _pipeline: list = []

    # Record extensibility (RELAXED, see §4b): point record_cls at a CaseRecord
    # subclass to carry extra TYPED fields. Subclasses MAY mutate those fields
    # mid-life; keep the record as non-volatile as reasonably possible (volatile /
    # derived data belongs in the event log) but infrequently-changed fields are OK.
    record_cls: type[CaseRecord] = CaseRecord

    # Flush policy (see §5b): by default the base throttled-flushes the record (only
    # if dirty) at every transition boundary. Set False — or override as a property —
    # to suppress that intermediate flush; the FORCED lifecycle flushes
    # (create / close / reclassify) STILL happen regardless.
    autoflush_record_on_transition: bool = True

    # The case-type registry (§4a): a GLOBAL SINGLETON owned here, not by a manager,
    # so type resolution (rehydrate / smart peek_record) works manager-free.
    _registry: dict[str, "type[FolderBackedCase]"] = {}

    @classmethod
    def register_case_type(cls, case_cls: "type[FolderBackedCase]", *, name: str | None = None) -> None:
        """Explicit, opt-in registration (bare logical name -> class). No auto-register."""
        cls._registry[name or case_cls.__name__] = case_cls

    @classmethod
    def register_case_types(cls, classes) -> None:
        for c in classes:
            cls.register_case_type(c)

    @classmethod
    def resolve_case_type(cls, type_name: str | None, *, registry=None) -> "type[FolderBackedCase] | None":
        """Look up a class by its stored bare name. `registry` overrides the singleton
        (tests / isolation); None means use the global singleton."""
        if type_name is None:
            return None
        return (registry if registry is not None else cls._registry).get(type_name)

    def __init__(self, case_folder: Path):
        # ALWAYS folder-anchored (same in standalone and manager modes) and
        # MANAGER-AGNOSTIC. Load-only: brand-new cases come via create_in_folder()
        # (used by both CaseManager.create_case and standalone). The record is
        # always <folder>/case_record.yaml.
        self._folder = case_folder
        self._holds_lease = False         # set True only on a successful _acquire_lease()
        self._released = False
        self._record = self.record_cls.load_from_file(case_folder / RECORD_NAME)
        self._events = CaseEventLogReader.for_folder(case_folder)   # convention interpreter (§4c)
        self._listeners: list = []        # fn(case, event_name, info); how a manager subscribes
        self._stepping = False            # in-memory: a step is executing right now (this process)
        # Status is DERIVED from the event log, then cached in memory while live.
        # `state` lives on the logic object so `transitions` can manage it.
        self.state = self._derive_state() or self._fsm_initial_state
        self._last_activity = self._events.last_activity or self._record.created
        # Claim single ownership of the folder (§5d). AFTER state is known, because the
        # lease TTL is state-aware. Raises CaseAlreadyOpenError if someone holds it.
        self._acquire_lease()
        self._machine = self._build_machine(self.state)

    @classmethod
    def create_in_folder(cls, case_folder: Path, *, case_id=None, external_key=None,
                         nickname=None, **fields) -> "FolderBackedCase":
        """First-time inception, shared by CaseManager.create_case() and standalone
        (§6c). Writes the skinny record, constructs (load-only), and logs the
        lifecycle bookend `CASE_NEW` plus the initial `ENTER_STATE`."""
        case_folder.mkdir(parents=True, exist_ok=True)
        record = cls.record_cls(
            case_object_type=cls.__name__, case_id=case_id or _new_time_slug(),
            external_key=external_key, nickname=nickname, created=_utcnow(), **fields,
        )
        # Direct write (no instance exists yet) — SAFE BY CONSTRUCTION: case_object_type
        # is set to cls.__name__ here, so it satisfies the §5b type-name guard.
        record.save_to_file(case_folder / RECORD_NAME)
        case = cls(case_folder)
        case._events.primitive.create_event(
            "CASE_NEW", cls.__name__,
            {"case_id": record.case_id, "external_key": record.external_key},
        )
        case._events.primitive.create_event("ENTER_STATE", cls._fsm_initial_state)
        return case

    # ---- type resolution (shared by rehydrate + peek_record) ----
    @staticmethod
    def _sniff_case_type(folder: Path) -> str | None:
        """Cheaply read the record's case_object_type WITHOUT full validation.
        `case_object_type` is the FIRST CaseRecord field and YAML is emitted in
        definition order (§4), so it reliably appears at the top of the file."""
        m = re.search(r'^case_object_type:\s*["\']?([^"\'\s]+)',
                      (folder / RECORD_NAME).read_text(), re.M)
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

    # ---- peek accessors: read a case from its folder WITHOUT constructing a live
    #      instance (§5c). Pure, non-blocking, file-only reads — no ownership taken. ----
    @classmethod
    def peek_record(cls, folder: Path, *, record_cls=None, registry=None) -> CaseRecord:
        """The identity record, read straight from disk with the CORRECT typed record
        class when resolvable:
          - record_cls given      -> use it directly (shortcut; no sniff, no registry).
          - else                  -> sniff case_object_type, resolve via the registry to
                                     the case class, use its record_cls.
          - unregistered/unknown  -> fall back to base CaseRecord (graceful, §4a): skinny
                                     identity fields stay readable; extra fields ignored."""
        if record_cls is None:
            case_cls = cls.resolve_case_type(cls._sniff_case_type(folder), registry=registry)
            record_cls = case_cls.record_cls if case_cls else CaseRecord
        return record_cls.load_from_file(folder / RECORD_NAME)

    @staticmethod
    def peek_events(folder: Path) -> CaseEventLogReader:
        """A CaseEventLogReader over the folder's event log (§4c): current_state,
        status/is_closed, retained_files(), last_activity, and `.primitive` for the
        raw log — all without constructing or owning a live case."""
        return CaseEventLogReader.for_folder(folder)

    # ---- lifecycle-signal subscription (the case never reaches "up" to a manager) ----
    def add_transition_listener(self, fn) -> None:
        """Subscribe to post-transition notifications: fn(case, event_name, info).
        This is how a CaseManager attaches archival behavior WITHOUT the case
        having any knowledge of the manager. Standalone cases have no listeners."""
        self._listeners.append(fn)

    def _notify(self, event_name: str, **info) -> None:
        for fn in self._listeners:
            fn(self, event_name, info)

    def _derive_state(self) -> str | None:
        """Current status = the most recent ENTER_STATE entry. Delegates to the same
        CaseEventLogReader (§4c) the peek path uses, so an external observer and this
        live object can never disagree."""
        return self._events.current_state

    # ---- identity / status ----
    @property
    def case_id(self) -> str: return self._record.case_id
    @property
    def external_key(self) -> str | None: return self._record.external_key
    @property
    def nickname(self) -> str | None: return self._record.nickname
    @property
    def is_open(self) -> bool: return self.state not in self._closed_states
    @property
    def is_closed(self) -> bool: return self.state in self._closed_states
    def is_idle_for(self, seconds: float) -> bool:
        return (datetime.datetime.utcnow() - self._last_activity).total_seconds() >= seconds

    # ---- activity / lifecycle introspection (standardized; see §6e) ----
    @property
    def next_step(self) -> str | None:
        """The forward `_pipeline` trigger ready from the current state, else None."""
        nxt = self._next_forward(self.state)
        return nxt[0] if nxt else None
    @property
    def is_runnable(self) -> bool:        # open AND a forward step is ready right now
        return self.is_open and self.next_step is not None
    @property
    def is_awaiting(self) -> bool:        # open but blocked on external input / a human
        return self.is_open and self.next_step is None
    @property
    def is_stepping(self) -> bool:        # in-memory: a step is mid-execution in THIS process
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
        """Runs after EVERY transition (`event` is a transitions EventData). Records
        the state-change event, then (unless the class opts out via
        `autoflush_record_on_transition`) throttled-flushes the record, and ONLY on
        the non-closed -> closed EDGE runs the two-phase closing sequence. See §5b
        for the full flush/fetch protocol.

        Two-phase closing (for the CASE_CLOSING / CASE_CLOSED distinction):
          Phase 1 — PRE-FINALIZATION (assets still exist):
            1. Log CASE_CLOSED to the event log (the durable history fact)
            2. on_closing() — subclass retains/extracts final artifacts
            3. _notify("CASE_CLOSING") — external PRE-PURGE observers (audit,
               test harness, downstream pipeline) inspect assets here.
               Subscribers MUST be fast; they are on the hot path.
          Phase 2 — POST-FINALIZATION (immutable, safe to move):
            4. _purge_ephemeral_files() — ephemerals gone
            5. _record.closed stamped + FORCE-flushed (authoritative seal: bypasses
               change detection, captures any on_closing() mutations)
            6. _notify("CASE_CLOSED") — manager archives, or any subscriber
               that needs the fully-finalized, immutable case.
        """
        src, dest = event.transition.source, event.transition.dest
        self._events.primitive.create_event("ENTER_STATE", dest)
        self._last_activity = datetime.datetime.utcnow()
        closing = src not in self._closed_states and dest in self._closed_states
        if not closing:
            # Throttled flush + lease beat at the boundary. Skipped on the closing edge:
            # the forced phase-2 seal supersedes the flush, and phase 2 RELEASES the lease.
            if self.autoflush_record_on_transition:
                self._flush_record()
            self.heartbeat()                           # extend the lease (throttled, §5d)
        if closing:                                    # the closing edge
            # --- phase 1: pre-finalization --- assets still present ---
            self._events.primitive.create_event("CASE_CLOSED", dest, {"from": src})
            self.on_closing()                          # subclass retains/extracts
            self._notify("CASE_CLOSING", src=src, dest=dest)   # external pre-purge observers
            # --- phase 2: post-finalization --- assets gone, record sealed ---
            self._purge_ephemeral_files()
            self._record.closed = self._last_activity
            self._flush_record(force=True)             # FORCED authoritative seal (skips the throttle)
            self.release()                             # drop the lease BEFORE any archival move (§5d)
            self._notify("CASE_CLOSED", src=src, dest=dest)    # manager archives; standalone: no-op

    def on_closing(self) -> None:
        """Overridable subclass hook fired in phase 1 (pre-finalization): assets
        still exist, record not yet stamped. Use to retain/extract final artifacts
        before the ephemeral purge. Runs BEFORE `CASE_CLOSING` is notified to
        external listeners, so the subclass always gets first access.
        Default: no-op. (Heavy *async* finalization belongs in a `before` callback
        on the closing transition instead — this hook is sync cleanup.)"""

    # ---- record flush / fetch protocol (the single guarded chokepoint; see §5b) ----
    def _flush_record(self, *, force: bool = False) -> None:
        """Persist the owned record. Default is THROTTLED (writes only if the
        in-memory record differs from disk → a clean record costs nothing);
        force=True writes UNCONDITIONALLY (the authoritative seal at create/close/
        reclassify). A class may only ever write its OWN name, so we assert
        case_object_type == type(self).__name__ first; a mismatch raises
        RecordTypeMismatchError. This is deliberate friction (it makes the reclassify
        stamp a conscious act and catches the wrong subclass opened on a folder).
        ALL live-instance record writes MUST funnel through here for the guard to hold."""
        expected = type(self).__name__
        if self._record.case_object_type != expected:
            raise RecordTypeMismatchError(self._record.case_object_type, expected)
        if force or self._record.is_dirty():
            self._record.save()

    def reload_record(self) -> None:
        """Opt-in re-read of the record from disk (the mixin handles locking/conflict).
        The base fetches the record EXACTLY ONCE at construction and never silently
        re-reads (status/retained-files derive from the event log, so a single live
        instance owns its record). Concurrency-aware subclasses expecting external
        writers call this explicitly."""
        self._record.reload_from_file()

    # ---- single-owner protection: the heartbeat lease (§5d) ----
    # A content-free `.case.lease` file whose mtime is a "valid-until" expiry. A FUTURE
    # mtime => still held; past/absent => free. The owner re-beats to push the expiry
    # forward; the exact mtime it last wrote also serves as its claim token.
    def _lease_path(self) -> Path:
        return self._folder / LEASE_NAME

    def _on_disk_mtime(self) -> float | None:
        try:
            return self._lease_path().stat().st_mtime
        except FileNotFoundError:
            return None

    def lease_ttl_for(self, state: str) -> float:
        """Seconds the lease stays valid after a beat, for `state`. Override per state
        for long-idle windows. MUST comfortably exceed the gap between heartbeat()
        calls (≈ pulse interval + slack), or a live-but-quiet owner can be reclaimed."""
        return 300.0

    def _beat(self) -> None:
        expiry = time.time() + self.lease_ttl_for(self.state)   # writer bakes in its own TTL
        self._lease_path().touch(exist_ok=True)
        os.utime(self._lease_path(), (expiry, expiry))
        self._my_mtime = self._on_disk_mtime()                  # remember EXACTLY what we wrote
        self._last_beat_local = time.monotonic()               # throttle clock (jump-immune)
        self._holds_lease, self._released = True, False

    def _acquire_lease(self) -> None:
        m = self._on_disk_mtime()
        if m is not None and m > time.time():                   # future expiry => still held
            raise CaseAlreadyOpenError(self._folder, expires_in=m - time.time())
        self._beat()                                            # absent/expired => claim (or reclaim)

    def heartbeat(self, *, min_update_secs: float = 15.0, validate_ownership: bool = True) -> None:
        """Extend our lease (mtime = now + lease_ttl_for(state)). Throttled: a no-op if
        < min_update_secs since our last beat, so tight loops may call freely; pass
        min_update_secs=0 to FORCE a check+beat now. With validate_ownership, raises
        OwnershipLostError (FATAL) if the on-disk mtime no longer matches what we last
        wrote — i.e. another owner reclaimed us past our TTL, or the lease vanished."""
        self._check_active()
        if time.monotonic() - self._last_beat_local < min_update_secs:
            return
        if validate_ownership and self._on_disk_mtime() != self._my_mtime:
            raise OwnershipLostError(self._folder)
        self._beat()

    def release(self) -> None:
        """Relinquish the claim: delete the lease and mark this husk detached. Idempotent.
        This is NOT an FSM close and does NOTHING to the case's assets — it only drops
        ownership of the folder. Any later mutating use raises ReleasedCaseError. Wired
        to __exit__ (use as a context manager) and best-effort __del__; on a crash the
        lease simply expires via its TTL instead."""
        if self._released or not self._holds_lease:
            return                                              # never acquired => never delete
        self._lease_path().unlink(missing_ok=True)
        self._released, self._holds_lease = True, False

    def _check_active(self) -> None:
        if self._released:
            raise ReleasedCaseError(self._folder)

    def __enter__(self): return self
    def __exit__(self, *exc): self.release()
    def __del__(self):
        try: self.release()
        except Exception: pass

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> bool | None:
        """Lock-free staleness read for a manager's recovery sweep: True if the lease
        has expired (reclaimable), False if still held, None if unheld. Policy-free —
        the expiry is baked into the mtime, so no state/TTL lookup is needed here."""
        try:
            return (folder / LEASE_NAME).stat().st_mtime <= time.time()
        except FileNotFoundError:
            return None

    # ---- flat pipeline driver (no cascade, no pulse-hacking) ----
    def _next_forward(self, state: str):
        """(trigger, dest) of the single forward `_pipeline` transition whose source
        is `state`, or None when terminal / nothing applies (e.g. awaiting input)."""
        for t in self._fsm_transitions:
            srcs = t["source"] if isinstance(t["source"], (list, tuple)) else [t["source"]]
            if t["trigger"] in self._pipeline and state in srcs:
                return t["trigger"], t["dest"]
        return None

    async def advance(self) -> bool:
        """Fire EXACTLY ONE forward transition from the current state. Returns False
        when terminal or no forward step applies. Each step persists before the next,
        so the loop is flat and fully resumable."""
        if self.is_closed:                       # terminal short-circuits first (a closed case
            return False                         #   auto-releases its lease in phase 2, §5d)
        self._check_active()                     # else refuse to drive a released husk
        nxt = self._next_forward(self.state)
        if nxt is None:
            return False
        self._stepping = True                    # observable via is_stepping while the await runs
        try:
            await getattr(self, nxt[0])()        # public verb-trigger drives FSM + persistence
        finally:
            self._stepping = False
        return True

    async def run_to_completion(self, *, stop_before: str | None = None) -> None:
        """Drive forward until closed, or until the next step would ENTER
        `stop_before` (for staged inspection / testing). Delegates to advance()
        so _stepping is set correctly for each step."""
        while self.is_open:
            nxt = self._next_forward(self.state)
            if nxt is None or (stop_before and nxt[1] == stop_before):
                break
            await self.advance()

    # ---- reclassify ("call an audible" to a different subclass) ----
    def reclassify_case(self, new_cls: "type[FolderBackedCase]") -> "FolderBackedCase":
        """Rebind this case to a different FolderBackedCase subclass via a two-phase
        COMMIT, logging a RECLASSIFY event. The CALLER owns compatibility.
        Precondition: the current state must be in `new_cls._fsm_states` (the rebuilt
        machine uses it as the initial state). Returns a FRESH instance of `new_cls`
        on the same folder; the old instance is now stale. Intended for deliberately
        designed class hierarchies.

        Two-phase commit (crash-atomic — the switch is authoritative only at phase 2):
          Phase 1 — SNAPSHOT the 'from' representation in the OLD class's schema, so
            the prior record is fully on disk while the new class loads (this also
            preserves the migration seam: a new record's `model_validator(mode=
            "before")` can read the old fields here — §5b).
          Phase 2 — the NEW class adopts the folder, CONSCIOUSLY stamps its own name
            (required — `_flush_record` asserts a class may only write its own name;
            see RecordTypeMismatchError), and force-commits in its own schema.
        A crash between phases reopens cleanly as the OLD class (name not yet switched)."""
        if self.state not in new_cls._fsm_states:
            raise IncompatibleReclassError(self.state, new_cls.__name__)
        from_name = self._record.case_object_type
        self._flush_record(force=True)                       # phase 1: snapshot old repr (old name)
        self.release()                                       # serialize-then-RELEASE, then re-acquire (§5d)
        fresh = new_cls(self._folder)                        # acquires the now-free lease; old repr on disk
        fresh._events.primitive.create_event(
            "RECLASSIFY", new_cls.__name__,
            {"from": from_name, "at_state": fresh.state},
        )
        fresh._record.case_object_type = new_cls.__name__    # CONSCIOUS stamp — guard fires without it
        fresh._flush_record(force=True)                      # phase 2: commit new name + schema
        for fn in self._listeners:               # carry over any subscribers (e.g. manager archival)
            fresh.add_transition_listener(fn)
        return fresh

    # ---- file area (rooted in the case folder) ----
    def _assets_dir(self) -> Path:
        d = self._folder / "assets"; d.mkdir(parents=True, exist_ok=True); return d

    def write_file(self, relative_path: str, data: bytes, retain: bool = False) -> Path:
        target = self._assets_dir() / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # Retention is recorded in the event log, NOT on the skinny record.
        self._events.primitive.create_event("FILE", "write", {"path": relative_path, "retain": retain})
        self._last_activity = datetime.datetime.utcnow()
        return target

    def retain_file(self, relative_path: str) -> None:
        self._events.primitive.create_event("FILE", "retain", {"path": relative_path})

    def read_file(self, relative_path: str) -> bytes:
        return (self._assets_dir() / relative_path).read_bytes()

    def _retained_files(self) -> set[str]:
        """Reconstruct the retained-file set from the event log (write-with-retain
        or an explicit retain). Delegates to the shared CaseEventLogReader (§4c) so
        the live object and the peek path interpret retention identically."""
        return self._events.retained_files()

    def _purge_ephemeral_files(self) -> None:
        keep = self._retained_files()
        assets = self._folder / "assets"
        if not assets.exists(): return
        for item in assets.iterdir():
            if item.name in keep:                          # keep retained
                continue
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
            self._events.primitive.create_event("FILE", "purged", {"path": item.name})

    # ---- archive label hook: implementer chooses the date attribute ----
    def archive_grouping_label(self) -> str:
        """Destination archive grouping when this case closes. Default: close month.
        Override to key on creation date, fiscal period, tenant, etc."""
        return datetime.datetime.utcnow().strftime("%Y-%m-archive")

    # ---- pulse (stall-detection; subclasses override to escalate) ----
    @property
    def pulse_interval_secs(self) -> float: return 300.0
    async def pulse(self) -> None:
        idle = (datetime.datetime.utcnow() - self._last_activity).total_seconds()
        self._events.primitive.create_event("PULSE", "idle", {"idle_secs": round(idle)})
        self.heartbeat()                          # piggyback liveness on the pulse cadence (§5d)
        # subclasses override to escalate / fire FSM triggers on timeout
```

Notes:
- **Plain class, not Pydantic** — so `transitions` can freely inject and manage
  `self.state`. All serialized data lives in the owned `self._record`; the
  logic class carries only in-memory state.
- **`send_event=True` on `AsyncMachine`** — gives the `after_state_change` hook
  an `EventData` object with both `source` and `dest`, which is required for the
  closing-edge detection in `_on_state_changed`.
- **Status and retained-files are event-derived, never persisted.** Every
  transition appends `ENTER_STATE`; `_derive_state()` replays it on load.
  `_retained_files()` replays `FILE` entries. No field on `CaseRecord` can drift
  out of sync with the event log.
- **One log interpreter, shared by live and peek paths.** Both `_derive_state()`
  and `_retained_files()` delegate to a `CaseEventLogReader` (§4c) — the single
  place the case conventions (bookends, `ENTER_STATE`, `FILE`) are interpreted —
  so the live object and the lock-free `peek_*` accessors (§5c) can never disagree.
- **Type resolution lives on the base.** The case-type registry is a class-level
  singleton (§4a); `rehydrate()` builds the right subclass for a folder and the
  smart `peek_record()` resolves the right `record_cls`, both manager-free.
- **The record is extensible (relaxed) with a precise flush/fetch promise.**
  Subclasses add typed fields via `record_cls` and may mutate them (§4b); the
  base fetches the record once and flushes through the single guarded
  `_flush_record()` — throttled at transition boundaries, forced at
  create/close/reclassify. Full protocol and the type-name guard in §5b.
- **One live owner per folder.** Construction acquires a self-expiring
  `.case.lease` (mtime = "valid-until"); a second live owner fails with
  `CaseAlreadyOpenError`, displacement is fatal (`OwnershipLostError`), and a crash
  is cleaned up by TTL expiry. `heartbeat()` refreshes it; `release()` drops it.
  Full model — including the lease-vs-flock rationale — in §5d.
- **Two-phase closing hook** — see the `_on_state_changed` docstring and §5a for
  the `CASE_CLOSING` / `CASE_CLOSED` distinction and who subscribes to each.
- **Manager-agnostic** — the case emits signals via `_notify`; managers and other
  observers subscribe via `add_transition_listener`. Details in §5a.
- For the public verb-trigger + `_run_*` convention, the flat pipeline driver,
  and `reclassify_case()` usage see §6a–6b; for lifecycle introspection
  (`is_runnable`, `is_awaiting`) see §6e.

---

## 5a. Why a case does **not** know its `CaseManager`

A case legitimately owns its **folder, record, event log, FSM, and assets** — all
intrinsic to a single case. It does **not** own **placement/archival policy**:
"which grouping should a closed case live in" is a *collection-level* concern that
belongs to the manager. Giving the case a `self._manager` backref would make a
`manager is not None` branch necessary everywhere and make closing behavior
depend on whether a manager exists.

So we **invert the dependency**: the case just *emits* lifecycle signals
(`_notify`), and whoever cares *subscribes*.

- The case's closing sequence is fully self-contained and identical everywhere
  (§5 two-phase detail): log `CASE_CLOSED` event → `on_closing()` →
  `_notify("CASE_CLOSING")` → purge → stamp `closed` →
  `_notify("CASE_CLOSED")`. Nothing about a manager appears.
- A `CaseManager` subscribes to **`CASE_CLOSED`** (post-purge) and archives the
  folder when it fires. An audit system or test harness that needs to inspect
  assets subscribes to **`CASE_CLOSING`** (pre-purge) instead. Both use the same
  `add_transition_listener` mechanism; they simply react to different event names.
  **Standalone** cases have no listeners — closing completes cleanly with no move.
- This also keeps the **risky folder move off the transition's critical path**:
  the listener requests it, and a manager **sweep** (`reap_closed()`) is the
  idempotent safety net that archives any case found `closed` but still in `open`
  (e.g. after a crash mid-move, or a notification missed by a non-subscribed
  loader). The `closed` stamp makes such cases trivially identifiable.

Net: the case is a clean, standalone-capable unit; the manager is a pure
*observer/coordinator* layered on top.

---

## 5b. The record flush / fetch protocol (an explicit promise)

Because subclass fields may be mutated (§4b), the base makes a precise promise
about *when* the record is read and written, so derived classes can rely on it.

**Fetch — once, eagerly, at construction.** `__init__` reads
`<folder>/case_record.yaml` exactly once and the base NEVER silently re-reads
(status and retained-files come from the event log, not the record, so a single
live instance owns its record). Concurrency-aware subclasses that expect external
writers call `reload_record()` explicitly.

**Flush — one method, two modes.** All live-instance writes funnel through the
single guarded chokepoint `_flush_record(*, force=False)`:

- **Throttled (default):** writes only when the in-memory record differs from disk
  (`is_dirty()`), so a clean record costs nothing. Used at every transition
  boundary, gated by `autoflush_record_on_transition` (default `True`; set `False`
  — or shadow it with a property for a dynamic decision — to opt out).
- **Forced (`force=True`):** writes unconditionally, bypassing change detection.
  Used at the non-negotiable lifecycle milestones — **creation**, **close**
  (phase-2 seal), and **reclassify** — where the write is authoritative,
  once-per-event (no perf concern), captures pending mutations (including opt-out
  classes and `on_closing()`), and deliberately *wins over a concurrent external
  writer* (the milestone instance is authoritative).

The promise derived classes build on: *what you write to the record is durable at
the next transition or at close, whichever comes first; need it sooner, call
`self._flush_record()`; want fewer writes, set
`autoflush_record_on_transition = False` and accept that mid-life mutations land
only at close.*

**Closing-edge optimization.** On the closing transition the intermediate
throttled flush is skipped; the forced phase-2 seal supersedes it (no double
write) and still captures any `on_closing()` mutations.

**Never touches the record.** `write_file()`, `retain_file()`, and `pulse()` write
only the event log / in-memory activity — the skinny-record invariant holds.

**Type-name guard (deliberate friction).** `_flush_record` asserts the record's
`case_object_type` equals the writing instance's own class name; a mismatch raises
`RecordTypeMismatchError`. A case may only ever write its OWN name. This is the
safety that makes the reclassify phase-2 stamp a *conscious* act — and, for free,
catches the wrong subclass opened on a folder. We deliberately do **not** add a
*load-time* check: it would false-fail during reclassify, which by design loads
the new class over a record still bearing the old name.

```python
class RecordTypeMismatchError(Exception):
    """Raised by _flush_record when the record's case_object_type doesn't match the
    class attempting the write. A FolderBackedCase may only ever write its OWN name.

    If you hit this inside reclassify_case(): the friction is DELIBERATE. Phase 2
    constructs the NEW class over a record that still carries the OLD name (so a
    crash mid-reclassify reopens cleanly as the old class). Before the committing
    flush you must CONSCIOUSLY stamp the new name (and migrate any new-schema
    fields, if the new record_cls added some):
        fresh._record.case_object_type = NewClass.__name__
        # ...initialize/migrate any new-schema fields here, if needed...
        fresh._flush_record(force=True)
    """
    def __init__(self, on_record: str, writing_class: str):
        super().__init__(
            f"record case_object_type={on_record!r} but {writing_class!r} is writing it. "
            f"A case may only write its own name. If reclassifying, set "
            f"_record.case_object_type to the new class name (and migrate any new "
            f"fields) before flushing — this guard is intentional."
        )
```

> **No reclassify migration hook (YAGNI).** Many reclassifies change nothing but
> the class name, so the base ships no migration API. If a subclass *does* need to
> transform old→new fields, the seam is already there: the old representation is
> fully on disk while `new_cls(folder)` loads in phase 1→2, so a Pydantic
> `model_validator(mode="before")` on the new record (or reading the raw file) can
> map it — without any base-class machinery.

---

## 5c. Peek accessors — read a case without opening it

Inspecting a case — listing it, rendering a dashboard, deciding whether to act on
it, or simply **deducing its current state** — should not require constructing a
live `FolderBackedCase`. The base therefore exposes **two** read accessors that
take a case folder and read straight from disk, constructing no live instance and
taking no ownership:

- `peek_record(folder, *, record_cls=None, registry=None)` — the identity record,
  loaded with the **correct typed `record_cls` when resolvable**. It sniffs
  `case_object_type` (a cheap top-of-file read — that field is first by definition
  order, §4) and resolves the class via the registry (§4a); pass `record_cls=` to
  skip the sniff entirely, or `registry=` to override the singleton. An
  **unregistered** type degrades gracefully to the base `CaseRecord` so the skinny
  fields stay readable.
- `peek_events(folder)` — a `CaseEventLogReader` (§4c) exposing `current_state`,
  `status` / `is_closed`, `retained_files()`, `last_activity`, and `.primitive`
  for the raw log. State/status come off this object rather than from separate
  methods, so there is exactly **one** interpreter of the log's conventions.

The live-object analog of the smart `peek_record` is **`rehydrate(folder)`**: same
type resolution, but it *constructs the right subclass* and **raises**
`UnregisteredCaseTypeError` for an unknown type (you can't build behavior without
the class — contrast `peek_record`, which degrades for read-only use).

Two properties make these trustworthy:

- **No drift between observer and owner.** `peek_events(...).current_state` and the
  live object's `_derive_state()` are the *same* `CaseEventLogReader` code (§4c), so
  an external process and the case's own owner can never disagree about its state.
- **Consistent reads on an append-only log.** Because the event log only ever
  *appends* (and `PrimitiveEventLog` writes each entry atomically, temp-then-rename),
  a reader always sees a complete prefix; the only race is a partially written
  *latest* entry, which atomic writes remove.

These accessors stand on their own regardless of how (or whether) we ultimately
coordinate multiple live instances on one folder — a reader never needs to
participate in that coordination.

---

## 5d. Single-owner protection — the heartbeat lease

A case is a **heavyweight, deep object passed by reference, not copied** (copy
semantics barely make sense). The failure we guard against is therefore *not* a
microsecond race — it's **two live owners on the same folder**, most likely the
resident web app and a cross-process batch job on the same host both grabbing the
same case. The deployment assumption is narrow on purpose: **single host, local
filesystem.** We do **not** target NFS or multi-host coordination.

### The model: an mtime-as-"valid-until" lease

Coordination rides on a single content-free file, **`.case.lease`**, living *inside*
the case folder (so it travels with the folder and vanishes on release). Its entire
signal is its **mtime**, which we deliberately write as a **future** timestamp —
`now + lease_ttl_for(state)` — so the mtime *is* the lease's expiry ("valid-until"):

- **A future mtime ⇒ still held.** Acquiring on a folder whose lease mtime is in the
  future raises `CaseAlreadyOpenError` (fatal — see below).
- **A past or absent mtime ⇒ free.** Acquire claims (or reclaims) it by beating.

The win of encoding *expiry* rather than *last-touch*: **staleness is self-describing,
so no reader needs the TTL policy.** A manager's recovery sweep just asks
`is_heartbeat_expired(folder)` — pure `mtime <= now` — with no per-case state read or
TTL lookup. The **state-aware TTL lives entirely on the writer**, who knows its own
state (`self.state`, no peek) and bakes its own patience into the file. States with
naturally long idle windows override `lease_ttl_for()` to write a farther-future
expiry.

### Beating, throttling, and ownership validation

`heartbeat(min_update_secs=15, validate_ownership=True)` extends the lease. It is the
**single** refresh entry point and is called from three places: explicitly at will,
at every non-closing state transition, and from `pulse()`. Two ergonomics matter:

- **Throttle** — calls within `min_update_secs` of our last beat are no-ops (a tight
  loop may call freely without a write storm); `min_update_secs=0` forces a beat now.
- **Ownership validation** — before writing, it compares the on-disk mtime to *the
  exact value we last wrote*. A mismatch means someone reclaimed us past our TTL (or
  the lease vanished) → **`OwnershipLostError`, which is FATAL.** We do not try to
  re-steal; a displaced owner stops. (A lone owner that merely went quiet past its TTL
  but was *not* stolen still matches its own remembered mtime, so it just re-beats —
  expiry alone never revokes ownership; only an actual competing write does.)

### The cadence-vs-TTL constraint

Because the encoded expiry can lapse while a live owner is merely *quiet*,
`lease_ttl_for(state)` **must comfortably exceed the maximum realistic gap between
`heartbeat()` calls** (≈ `pulse_interval_secs` + slack). Set TTLs with the beat
cadence in mind, per state.

### Lifecycle

- **Acquire** happens in `__init__`, *after* state is known (the TTL is state-aware),
  so every construction — including `create_in_folder()` and `rehydrate()` — claims
  ownership or fails loudly.
- **Release** (`release()`) deletes the lease and marks the instance **detached**;
  it is idempotent, is **not** an FSM close, and touches **no** case assets. It is
  wired to `__exit__` (use the case as a context manager) and best-effort `__del__`.
  Any later *mutating* use of a released husk raises `ReleasedCaseError` (via the
  `_check_active()` guard on `advance()`/`heartbeat()`/mutators); reads still work.
- **Close** releases the lease in phase 2 *before* any archival folder move, so a
  closed/archived case never carries a stale claim.
- **Reclassify** is just *serialize → release → re-acquire → deserialize*: the old
  instance flushes and `release()`s, then `new_cls(folder)` acquires the now-free
  lease. No special ownership-transfer machinery.
- **Crash** needs no cleanup: the lease simply **expires** via its TTL and the next
  owner reclaims it.

### Why a lease, not a flock

We chose this over `portalocker`/OS advisory locks deliberately: an OS lock dies with
the process holding it (no liveness signal survives a crash for a manager to reason
about), is awkward to inspect lock-free, and says nothing about *staleness*. The lease
gives us a crash-surviving, self-expiring, **inspectable** claim whose single artifact
(an mtime) doubles as both claim token and liveness/expiry signal — at the cost of one
cosmetic oddity: **the lease file's mtime is in the future**, which looks strange to
`ls`/`find -mtime`/backup tools. Nothing else relies on this file's mtime meaning
"last modified," so the abuse is contained; the name `.case.lease` (not `.heartbeat`)
signals the valid-until semantics.

### What lives here vs. in the manager

`FolderBackedCase` carries **no in-memory registry of live instances** — that was only
a same-process speed optimization and is non-essential, since the lease handles the
real (cross-process) conflict. **Recovery** — spotting expired leases via
`is_heartbeat_expired()`, waiting, and reloading a stolen/abandoned case from disk —
is the **`CaseManager`'s** job, not the base class's. The base provides only the
lock-free read (`is_heartbeat_expired`) it needs.

```python
class CaseAlreadyOpenError(Exception):
    """Raised on construction when a non-expired lease (future mtime) already exists on
    the folder — another live owner holds it. `expires_in` is seconds until it lapses."""
    def __init__(self, folder, *, expires_in: float):
        super().__init__(f"{folder} is already open (lease valid for ~{expires_in:.0f}s)")
        self.folder, self.expires_in = folder, expires_in

class OwnershipLostError(Exception):
    """FATAL: our heartbeat found the on-disk lease no longer matches what we wrote —
    another process reclaimed this folder past our TTL. The displaced owner must stop."""

class ReleasedCaseError(Exception):
    """A mutating operation was attempted on a case husk that has already release()d its
    lease (detached). Construct a fresh instance (rehydrate) to act on the folder again."""
```

---

## 6. Concrete case subclass — a human-driven case (`TicketCase`)

This case is *human-driven* (no `_pipeline`): a person fires the verbs as work
happens. All persistence is inherited from `FolderBackedCase`. Triggers are
awaitable because the machine is async.

```python
class TicketRecord(CaseRecord):
    # RELAXED extension (§4b): a typed, low-churn field, mutated mid-life (set when
    # the ticket is resolved). Optional/defaulted so older records still load.
    # Durable at the next transition via the default autoflush, force-sealed at close.
    resolution_summary: str | None = None

class TicketCase(FolderBackedCase):
    record_cls = TicketRecord           # swap in the extended typed record
    _fsm_states = ["new", "assigned", "in_progress", "waiting", "resolved", "closed"]
    _closed_states = {"closed"}
    _fsm_initial_state = "new"
    _fsm_transitions = [
        {"trigger": "assign",           "source": "new",                      "dest": "assigned"},
        {"trigger": "start_work",       "source": "assigned",                 "dest": "in_progress"},
        {"trigger": "wait_on_customer", "source": "in_progress",              "dest": "waiting"},
        {"trigger": "customer_replied", "source": "waiting",                  "dest": "in_progress"},
        {"trigger": "resolve",          "source": ["in_progress", "waiting"], "dest": "resolved",
         "conditions": ["_has_resolution"]},
        {"trigger": "close",            "source": "resolved",                 "dest": "closed"},
        {"trigger": "timeout",          "source": "waiting",                  "dest": "in_progress"},
    ]

    def _has_resolution(self, event) -> bool:    # send_event=True => callbacks receive EventData
        return bool(self._record.resolution_summary)   # typed field on TicketRecord (§4b)

    async def pulse(self) -> None:
        await super().pulse()
        if self.state == "waiting" and self.is_idle_for(86400):   # 24h
            await self.timeout()                                  # awaitable async trigger
```

---

## 6a. Convention: public verb-triggers + private `_run_*` workers

The base class nudges implementers toward this convention, which we adopt in the
pipeline example below:

- **The domain verb is a *trigger*** (`classify`, `textify`, …). `transitions`
  injects it as a public, awaitable method that *drives the transition* — running
  guards, the work, the state change, and the `after_state_change` persistence
  hook. So `await case.classify()` always does the bookkeeping.
- **The slow work is a private `before` callback** (`_run_classify`). Putting it
  in `before` means a failure aborts the transition and leaves you in the prior
  state — so "retry" is just "call the trigger again," and a crash mid-call
  resumes from the last *completed* stage (no `ENTER_STATE` was written).
- **Why not a public `_run_*`?** A public raw-work method invites callers to run
  the work *without* the transition — writing artifacts with no state change or
  event. Keeping it private preserves the "work only happens as part of a
  recorded transition" invariant. (A test may still call it directly, accepting
  that it bypasses bookkeeping.)

## 6b. A staged async pipeline (`InboundDocCase`)

A document flows through slow external services (classify → textify → summarize →
index). It declares `_pipeline` so the generic `advance()`/`run_to_completion()`
driver can step it forward; each public verb is a trigger whose private `_run_*`
callback awaits the external service.

```python
class InboundDocCase(FolderBackedCase):
    _fsm_states    = ["new", "classified", "textified", "summarized", "indexed", "closed"]
    _closed_states = {"closed"}
    _fsm_initial_state = "new"
    _pipeline = ["classify", "textify", "summarize", "index", "close"]   # forward order
    _fsm_transitions = [
        {"trigger": "classify",  "source": "new",        "dest": "classified", "before": "_run_classify"},
        {"trigger": "textify",   "source": "classified", "dest": "textified",  "before": "_run_textify"},
        {"trigger": "summarize", "source": "textified",  "dest": "summarized", "before": "_run_summarize"},
        {"trigger": "index",     "source": "summarized", "dest": "indexed",    "before": "_run_index"},
        {"trigger": "close",     "source": "indexed",    "dest": "closed"},
    ]

    # on receipt: tuck the raw source in, deliberately EPHEMERAL (purged on close)
    def ingest(self, raw: bytes, name: str = "raw") -> None:
        self.write_file(f"source/{name}", raw, retain=False)

    # private workers: quick local work + a slow awaited external call; derived
    # artifacts are written with retain=True so they survive close + archival.
    async def _run_classify(self, event):
        labels = await classify_service(self.read_file("source/raw"))
        self.write_file("derived/labels.json", labels, retain=True)

    async def _run_textify(self, event):
        text = await ocr_service(self.read_file("source/raw"))
        self.write_file("derived/text.txt", text, retain=True)

    async def _run_summarize(self, event):
        summary = await summarize_service(self.read_file("derived/text.txt"))
        self.write_file("derived/summary.md", summary, retain=True)

    async def _run_index(self, event):
        await index_service(self.read_file("derived/text.txt"), self.case_id)

    # final-artifact bookkeeping on the closing edge (raw source will be purged)
    def on_closing(self) -> None:
        self._events.primitive.create_event("NOTE", "archived derived artifacts; raw source purged")
```

Driving it is a **flat loop**, not a recursive cascade:

```python
await case.run_to_completion()        # classify -> textify -> summarize -> index -> close
# or one externally-scheduled step at a time:
while await case.advance():
    ...                               # persist/inspect/throttle between steps
```

> **Long / restart-spanning services.** When a stage can outlive the process or
> is webhook-based, split it into an explicit in-flight state pair
> (`…->submitting->awaiting_x->…`): `advance()` in `awaiting_x` *checks* for the
> result and returns `False` until it's ready, letting a scheduler revisit later.
> The "I'm waiting on job X" handle is an **event-log entry**, not a record field
> (skinny-record rule). The await-in-place form above is the simpler default.

**`reclassify_case()` — calling an audible.** After a generic stage completes,
hand the case off to a specialized subclass whose flow diverges from there. The
new class **must** include the current state in its `_fsm_states` (the rebuilt
machine resumes from it) — otherwise `IncompatibleReclassError` is raised. The
new class should also be **registered** for later hydration (§4a):

```python
generic = InboundDocCase(folder)
await generic.classify()
specialized = generic.reclassify_case(InvoiceDocCase)   # shares "classified" in its states
await specialized.run_to_completion()
```

---

## 6c. Standalone (manager-free) usage

Because a case is **folder-anchored**, it runs without any `CaseManager`. The
folder layout is identical to manager mode (§3); only the outer cache/listing
card and the archive *move* are absent — with no listeners subscribed, the
closing-edge `_notify()` calls are no-ops and the case simply stays put with
`closed` stamped.

```python
# inception via the SAME classmethod the manager uses (writes record + CASE_NEW)
case = InboundDocCase.create_in_folder(Path("/data/inbound/doc-12345"),
                                       case_id="doc-12345")
case.ingest(raw_bytes)
await case.run_to_completion()         # everything persists under the folder; no listeners => no archival

# later, in another process: just re-open the folder (load-only ctor).
# Single-owner protection (§5d) applies even standalone — use it as a context
# manager so the lease is released promptly when you're done with the folder.
with InboundDocCase(Path("/data/inbound/doc-12345")) as again:
    ...                                # raises CaseAlreadyOpenError if another owner is live
```

The only differences from manager mode are the absent outer listing card and the
absent archival listener (so a closed standalone case stays put, with `closed`
stamped). The folder's internal layout, the event-log bookends, and the
single-owner lease (§5d) are identical — standalone, recovery from an expired
lease is simply up to you instead of a manager.

## 6d. Testing harness & directory-of-files fixtures

The all-on-disk design makes tests pleasant:

- **Fixtures are folders.** A prepared `case_folder/` (record + `events/` +
  `assets/`) *is* a fixture. A test loads the case from it, asserts, and/or keeps
  driving. The deterministic-key YAML makes the `events/` log **golden-file
  friendly** — diffing the event directory asserts "what happened."
- **Stop-at-stage.** `run_to_completion(stop_before="indexed")` drives to a
  boundary and stops, so a test can snapshot the folder mid-pipeline. Each step
  persisted before the next means every boundary is a valid inspection point.
- **Sync facade, not sync methods.** Keep production async-first; give tests a
  thin wrapper (e.g. `asyncio.run(case.advance())`) rather than duplicating each
  method in sync form — one place bridges async↔sync.
- **Inject fakes for external services** (constructor-injected service objects or
  a test subclass overriding `_run_*`) so the pipeline is fully reproducible from
  files with no network.

---

## 6e. Activity & lifecycle introspection — "is it stepping or idle?"

There are three distinct questions, answered at three different scopes:

1. **"Where is it in its lifecycle?"** — read the event-log **bookends**:
   `CASE_NEW` present, no `CASE_CLOSED` ⇒ still running; `CASE_CLOSED` present ⇒
   done. This works for *any* subclass regardless of state names, and is readable
   from files by a human, an agent, or another process — programmatically via the
   the `peek_events(folder)` reader (`.status` / `.current_state`) and
   `peek_record(folder)` (§5c), which read this directly without constructing or
   owning a live case.

2. **"Does it have work it could do right now?"** — derived, standardized
   predicates on the live object:
   - `is_closed` — terminal.
   - `is_runnable` — open **and** a forward `_pipeline` step is ready
     (`next_step` is not `None`); a driver could call `advance()` now.
   - `is_awaiting` — open but **no** forward step applies: it's parked on external
     input or a human (e.g. `TicketCase` in `waiting`, or an in-flight
     `awaiting_x` state).
   - `next_step` — the name of that ready forward trigger, or `None`.

3. **"Is a step executing at this instant?"**
   - *In-process:* `is_stepping` is `True` while `advance()` is awaiting a stage.
   - *Cross-process / durable:* an in-memory flag can't be seen by another
     process, so for stages where that matters, model an explicit **in-flight
     state** (`…submitting → awaiting_x → …`, §6b). That state *is* the durable,
     file-visible "actively working on X" signal — and the work handle lives as an
     event-log entry, not on the record.

A subtlety worth stating: the event log can show that a step **started and hasn't
completed** (an in-flight state with no following `ENTER_STATE`), but it cannot by
itself tell you whether the worker is still *alive* or died mid-step — that's a
**liveness** question answered by `pulse()` / the `FileMappedPydanticMixin` lock,
not by the activity signal. Keep the two concerns separate.

---

## 7. Using with a collection manager

The case folder layout (§3) is deliberately designed to sit as the **slave
directory** of a tracked entry in a `CachedFileFolders` grouping. A collection
manager only needs three things from a case:

1. Call `FolderBackedCase.create_in_folder()` with an allocated folder.
2. Subscribe via `add_transition_listener()` to react to lifecycle signals.
3. On `CASE_CLOSED`, move the whole case folder to an archive grouping.

`CaseManager` is a complete, opinionated implementation of this pattern — backed
by `CachedFileFolders`, with open/date-archive groupings, thin listing cards, a
case-type registry for rehydration, `reap_closed()` for safety-net archival, and
a pulse loop. See **`CaseManager Model.md`** for the full design.

---

## 8. Dependencies

- **`transitions`** (pytransitions) — FSM engine. Lightweight, MIT, composition
  pattern (`model=self`). We use its **async** machine
  (`transitions.extensions.asyncio.AsyncMachine`), which ships in the same
  package. Packaging TBD: core dependency vs. an optional extra (e.g.
  `casequeue = ["transitions"]`) consistent with the library's lean-core + extras
  convention.
- Reuses existing core deps: `pydantic>=2`, `pyyaml`, `portalocker`.
