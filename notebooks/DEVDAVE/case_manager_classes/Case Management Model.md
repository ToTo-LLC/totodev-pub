# Case Management Model

> **Status:** DRAFT — illustrative code is design-fidelity, **not** production
> code. No implementation until approved.
>
> **FSM library:** **`transitions`** (pytransitions). `case_queue_design.md` has
> been updated to match.

---

## 1. Background

The class names (`CaseInstanceBase`, `CaseManager`, concrete subclasses), many
method names, and the FSM-by-composition pattern come from an initial design
sketch by an external architect. This document grounds that sketch on the
`totodev-pub` building blocks (`CachedFileFolders`, `FileMappedPydanticMixin`,
`PrimitiveEventLog`), adds the open/archive partitioning strategy, and
incorporates all subsequent design decisions. FSM implementation uses
**`transitions`** (pytransitions) throughout.

---

## 2. The three cooperating pieces

1. **`CaseRecord(BaseModel, FileMappedPydanticMixin)`** — the deliberately
   **skinny**, near-immutable serialized record (`case_record.yaml`, always
   *inside* the case folder). An identity card only: it carries no volatile
   data — **status, activity time, and the retained-file set are all derived from
   the event log** (cached in memory while the case is live), never stored here.
   The Pydantic layer stays dumb; `FileMappedPydanticMixin` provides persistence,
   locking, change-tracking, the non-serialized bound filepath, and clean YAML
   diffs.

2. **`CaseInstanceBase(ABC)`** — the logic base class. It OWNS one `CaseRecord`,
   hosts an **async-first**
   `transitions` FSM (`model=self`), owns the per-case file area (its case folder)
   and a file-based `PrimitiveEventLog`, and exposes `pulse()`, `write_file()`,
   `read_file()`, `retain_file()`, `is_open`, `is_closed`, `is_idle_for()`, the
   flat `advance()` / `run_to_completion()` pipeline driver, the `on_closing()`
   closing-edge hook, and `reclassify_case()`. Concrete subclasses (`TicketCase`,
   `InboundDocCase`, …) declare `_fsm_states` / `_fsm_transitions` /
   `_closed_states` (and, for linear pipelines, `_pipeline`) and override hooks.

3. **`CaseManager`** — host backed by a `CachedFileFolders` cache. It manages
   an **open** grouping (hot) plus **date-labeled archive groupings**
   (cold), creates cases, hydrates them via an **explicit, opt-in case-type
   registry** (§4a), looks them up by `case_id` / `external_key`, lists open and
   (date-bounded) closed cases via thin **listing cards**, performs the
   close→archive **move** (of the whole case folder), and drives the `pulse`
   cycle. A case can also run entirely **without** a manager (§6c).

---

## 3. Storage layout (totodev-pub grounded) — one **symmetric case folder**

The single most important inspectability rule: **the case folder has the same
internal shape in every mode**, and `case_record.yaml` always lives *inside* it.
A human or agent that opens any case folder — whether it was created standalone
or by a `CaseManager`, whether it's open or archived — sees an identical layout.
Nothing about the record "moves" relative to its folder.

```text
THE CASE FOLDER (identical everywhere):
<case_folder>/
  case_record.yaml          # the skinny record — ALWAYS here, by a fixed name
  events/                   # PrimitiveEventLog: SOURCE OF TRUTH for status + retention
    e001_ENTER_STATE@new.yaml ...
  assets/                   # write_file() targets
    source/  raw.pdf ...    #   ephemeral inputs (purged on close)
    derived/ text.txt ...   #   retained outputs (survive close, travel to archive)
```

In **manager mode**, each case folder is simply the **slave dir** of a thin
tracked entry in a `CachedFileFolders` grouping. The tracked file itself holds no
authoritative state — it is an (optionally empty) **listing card** the manager
uses to enumerate/sort the open set cheaply without descending into every folder:

```text
<cache_root>/
  open/                                       # the hot grouping (bounded ~500)
    Case/Case-<case_id>.yaml                  # manager listing card (regenerable; or empty touch-file)
    Case/Case-<case_id>.yaml._slave/          # == THE CASE FOLDER (layout above)
  2026-08-archive/                            # cold grouping, label from archive_grouping_label()
    Case/Case-<case_id>.yaml (+ ._slave/ == the case folder)
  ...
```

In **standalone mode** (§6c) there is no outer cache/listing card at all — the
case folder is any directory you hand the constructor, with the exact same
internal layout.

Why this layout:
- **Symmetry / inspectability** — the case folder is self-describing and
  identical regardless of how it was created; the record never relocates within
  it.
- **Open listing is naturally bounded** — `iter_open_cases()` reads the listing
  cards (or `case_record.yaml`s) in the `open` grouping (≤ ~500), never the
  (ever-growing) closed history.
- **Aging out** = dropping a whole archive grouping.
- **The whole case folder travels** on archive/reopen, so record + assets +
  event log move atomically.
- The listing card is a **regenerable convenience**, not a second source of
  truth — so the `_index.json` race risk never returns for the hot path. An
  optional cross-archive `external_key` manifest is still discussed in §9.

**Design philosophy — tolerate foreign files.** A case must be a *good neighbor*
in its own folder: it owns `case_record.yaml`, `events/`, and `assets/`, and it
must **ignore** anything else it doesn't recognize rather than choking on or
deleting it. In particular, `CachedFileFolders` may drop its own bookkeeping into
the tree (e.g. a metadata file alongside the tracked entry), and tools or humans
may leave notes. Concretely: cleanup is scoped to subtrees the case owns
(`_purge_ephemeral_files()` only touches `assets/`), and listing/iteration skips
unknowns. Derived classes are encouraged to honor this (without belaboring it).

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
    # ONLY if a measured listing bottleneck justifies it (same "no premature index"
    # stance as the open/archive partition).
```

- Serialized as **YAML** via the mixin (its dumper uses `sort_keys=False`, so
  fields emit in definition order → clean, churn-free git diffs).
- The mixin gives us locking (`portalocker`-based lock files), change tracking,
  `persisted_file()` (the non-serialized bound path), `would_conflict()`, and
  `reload_from_file()` for safe concurrent edits.
- **Near-immutable:** the record is written once at create and then touched only
  once more — at close, to stamp `closed`. That near-zero churn minimizes write
  contention and keeps diffs meaningful.
- **Status is not on the record.** Coarse open-vs-closed is encoded by *which
  grouping the record lives in* (`open` vs. a `*-archive`), reinforced by the
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

**The registry is explicit and opt-in — never a hidden side effect.**

- `manager.register_case_type(TicketCase)` (architect's form), plus a bulk
  `manager.register_case_types([...])`. A manager-bound decorator
  (`@manager.register`) is an optional convenience — still an explicit line of
  code, just co-located with the class. Creation does **not** auto-register.
- Registration is required **only** for manager-driven **rehydration** (turning a
  stored `case_object_type` string back into the right class). Defining a case
  class, instantiating it, driving its FSM, writing its event log and working
  files all work **without** registering — because creation is handed the
  concrete class directly.
- Hydrating an unregistered type raises a loud, explicit
  `UnregisteredCaseTypeError(case_object_type)`. Unknown types still **degrade
  gracefully for listing**: the skinny record fields (`nickname`, `case_id`,
  `external_key`, dates) are readable without the class.
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

## 5. `CaseInstanceBase` — the logic object

`CaseInstanceBase` owns its **case folder** (record + event log + assets), hosts
the `transitions` FSM, and exposes the public lifecycle API. **The case is
manager-agnostic** — it knows only its own folder and *emits* lifecycle signals;
interested parties (like a `CaseManager`) subscribe (§5a explains why).

**Constructor contract: folder-anchored, load-only.** A `CaseInstanceBase`
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
import datetime, shutil
from abc import ABC
from pathlib import Path
from transitions.extensions.asyncio import AsyncMachine   # async-first: triggers are awaitable
from totodev_pub.primitive_event_log import PrimitiveEventLog

RECORD_NAME = "case_record.yaml"

class CaseInstanceBase(ABC):
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

    # CaseRecord subclass to use for this case type (override in subclasses to add fields)
    record_cls: type[CaseRecord] = CaseRecord

    def __init__(self, case_folder: Path):
        # ALWAYS folder-anchored (same in standalone and manager modes) and
        # MANAGER-AGNOSTIC. Load-only: brand-new cases come via create_in_folder()
        # (used by both CaseManager.create_case and standalone). The record is
        # always <folder>/case_record.yaml.
        self._folder = case_folder
        self._record = self.record_cls.load_from_file(case_folder / RECORD_NAME)
        self._event_log = PrimitiveEventLog(event_dir=case_folder / "events")
        self._listeners: list = []        # fn(case, event_name, info); how a manager subscribes
        self._stepping = False            # in-memory: a step is executing right now (this process)
        # Status is DERIVED from the event log, then cached in memory while live.
        # `state` lives on the logic object so `transitions` can manage it.
        self.state = self._derive_state() or self._fsm_initial_state
        self._last_activity = self._event_log.latest_timestamp() or self._record.created
        self._machine = self._build_machine(self.state)

    @classmethod
    def create_in_folder(cls, case_folder: Path, *, case_id=None, external_key=None,
                         nickname=None, **fields) -> "CaseInstanceBase":
        """First-time inception, shared by CaseManager.create_case() and standalone
        (§6c). Writes the skinny record, constructs (load-only), and logs the
        lifecycle bookend `CASE_NEW` plus the initial `ENTER_STATE`."""
        case_folder.mkdir(parents=True, exist_ok=True)
        record = cls.record_cls(
            case_object_type=cls.__name__, case_id=case_id or _new_time_slug(),
            external_key=external_key, nickname=nickname, created=_utcnow(), **fields,
        )
        record.save_to_file(case_folder / RECORD_NAME)
        case = cls(case_folder)
        case._event_log.create_event(
            "CASE_NEW", cls.__name__,
            {"case_id": record.case_id, "external_key": record.external_key},
        )
        case._event_log.create_event("ENTER_STATE", cls._fsm_initial_state)
        return case

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
        """Current status = the most recent ENTER_STATE entry in the event log."""
        last = self._event_log.latest("ENTER_STATE")
        return last.value if last else None

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
        the state-change event and, ONLY on the non-closed -> closed EDGE, runs the
        two-phase closing sequence. The record is otherwise untouched after create.

        Two-phase closing (for the CASE_CLOSING / CASE_CLOSED distinction):
          Phase 1 — PRE-FINALIZATION (assets still exist):
            1. Log CASE_CLOSED to the event log (the durable history fact)
            2. on_closing() — subclass retains/extracts final artifacts
            3. _notify("CASE_CLOSING") — external PRE-PURGE observers (audit,
               test harness, downstream pipeline) inspect assets here.
               Subscribers MUST be fast; they are on the hot path.
          Phase 2 — POST-FINALIZATION (immutable, safe to move):
            4. _purge_ephemeral_files() — ephemerals gone
            5. _record.closed stamped + saved
            6. _notify("CASE_CLOSED") — manager archives, or any subscriber
               that needs the fully-finalized, immutable case.
        """
        src, dest = event.transition.source, event.transition.dest
        self._event_log.create_event("ENTER_STATE", dest)
        self._last_activity = datetime.datetime.utcnow()
        if src not in self._closed_states and dest in self._closed_states:   # the closing edge
            # --- phase 1: pre-finalization --- assets still present ---
            self._event_log.create_event("CASE_CLOSED", dest, {"from": src})
            self.on_closing()                          # subclass retains/extracts
            self._notify("CASE_CLOSING", src=src, dest=dest)   # external pre-purge observers
            # --- phase 2: post-finalization --- assets gone, record sealed ---
            self._purge_ephemeral_files()
            self._record.closed = self._last_activity
            self._record.save()
            self._notify("CASE_CLOSED", src=src, dest=dest)    # manager archives; standalone: no-op

    def on_closing(self) -> None:
        """Overridable subclass hook fired in phase 1 (pre-finalization): assets
        still exist, record not yet stamped. Use to retain/extract final artifacts
        before the ephemeral purge. Runs BEFORE `CASE_CLOSING` is notified to
        external listeners, so the subclass always gets first access.
        Default: no-op. (Heavy *async* finalization belongs in a `before` callback
        on the closing transition instead — this hook is sync cleanup.)"""

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
        if self.is_closed:
            return False
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
    def reclassify_case(self, new_cls: "type[CaseInstanceBase]") -> "CaseInstanceBase":
        """Rebind this case to a different CaseInstanceBase subclass, updating only
        `case_object_type` in the record and logging a RECLASSIFY event. The CALLER
        owns compatibility. Precondition: the current state must be in
        `new_cls._fsm_states` (the rebuilt machine uses it as the initial state).
        Returns a FRESH instance of `new_cls` on the same folder; the old instance
        is now stale. Intended for deliberately designed class hierarchies."""
        if self.state not in new_cls._fsm_states:
            raise IncompatibleReclassError(self.state, new_cls.__name__)
        self._event_log.create_event(
            "RECLASSIFY", new_cls.__name__,
            {"from": self._record.case_object_type, "at_state": self.state},
        )
        self._record.case_object_type = new_cls.__name__
        self._record.save()
        fresh = new_cls(self._folder)
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
        self._event_log.create_event("FILE", "write", {"path": relative_path, "retain": retain})
        self._last_activity = datetime.datetime.utcnow()
        return target

    def retain_file(self, relative_path: str) -> None:
        self._event_log.create_event("FILE", "retain", {"path": relative_path})

    def read_file(self, relative_path: str) -> bytes:
        return (self._assets_dir() / relative_path).read_bytes()

    def _retained_files(self) -> set[str]:
        """Reconstruct the retained-file set from the event log (write-with-retain
        or an explicit retain). No retention state is kept on the record."""
        keep: set[str] = set()
        for ev in self._event_log.entries("FILE"):
            if ev.action == "retain" or (ev.action == "write" and ev.meta.get("retain")):
                keep.add(ev.meta["path"])
        return keep

    def _purge_ephemeral_files(self) -> None:
        keep = self._retained_files()
        assets = self._folder / "assets"
        if not assets.exists(): return
        for item in assets.iterdir():
            if item.name in keep:                          # keep retained
                continue
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
            self._event_log.create_event("FILE", "purged", {"path": item.name})

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
        self._event_log.create_event("PULSE", "idle", {"idle_secs": round(idle)})
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
belongs to the manager. An earlier draft had the case hold a `self._manager`
backref and call `manager.on_case_closed(self)` on close. That's a layering smell
— the lower-level object reaching "up" to its coordinator — and it forced a
`manager is not None` branch and made closing behavior depend on whether a manager
exists.

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
*observer/coordinator* layered on top. (Open trade-off — prompt listener-driven
archival vs. purely lazy sweep-driven — is noted in §11.)

---

## 6. Concrete case subclass — a human-driven case (`TicketCase`)

This case is *human-driven* (no `_pipeline`): a person fires the verbs as work
happens. All persistence is inherited from `CaseInstanceBase`. Triggers are
awaitable because the machine is async.

```python
class TicketCase(CaseInstanceBase):
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
        return bool(getattr(self._record, "resolution_summary", None))

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
class InboundDocCase(CaseInstanceBase):
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
        self._event_log.create_event("NOTE", "archived derived artifacts; raw source purged")
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

# later, in another process: just re-open the folder (load-only ctor)
again = InboundDocCase(Path("/data/inbound/doc-12345"))
```

The only differences from manager mode are the absent outer listing card and the
absent archival listener (so a closed standalone case stays put, with `closed`
stamped). The folder's internal layout and the event-log bookends are identical.

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
   from files by a human, an agent, or another process.

2. **"Does it have work it could do right now?"** — derived, standardized
   predicates on the live object:
   - `is_closed` — terminal.
   - `is_runnable` — open **and** a forward `_pipeline` step is ready
     (`next_step` is not `None`); a driver could call `advance()` now.
   - `is_awaiting` — open but **no** forward step applies: it's parked on external
     input or a human (e.g. `TicketCase` in `waiting`, or an in-flight
     `awaiting_x` state). This is the "idle" you asked about.
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

## 7. `CaseManager` — host over a CachedFileFolders cache

```python
from totodev_pub.cached_file_folders import CachedFileFolders

OPEN_GROUPING = ["open"]

class CaseManager:
    """Host for a collection of cases, backed by a CachedFileFolders cache.
    Manages an `open` grouping (hot) + date-labeled archive groupings (cold)."""

    def __init__(self, cache: CachedFileFolders):
        self.cache = cache
        self._registry: dict[str, type[CaseInstanceBase]] = {}

    # ---- explicit, opt-in case-type registry (needed ONLY for rehydration) ----
    def register_case_type(self, case_cls: type[CaseInstanceBase]) -> None:
        self._registry[case_cls.__name__] = case_cls            # bare name; never automatic

    def register_case_types(self, classes) -> None:
        for c in classes: self.register_case_type(c)

    # optional convenience: `@manager.register` on a class definition (still explicit)
    def register(self, case_cls): self.register_case_type(case_cls); return case_cls

    def _resolve_case_cls(self, case_object_type: str) -> type[CaseInstanceBase]:
        try:
            return self._registry[case_object_type]
        except KeyError:
            raise UnregisteredCaseTypeError(
                f"case_object_type {case_object_type!r} is not registered; "
                f"call manager.register_case_type(...) before hydrating it"
            )

    # ---- the manager OBSERVES cases; it is never called "up" to (see §5a) ----
    def _attach(self, case: CaseInstanceBase) -> CaseInstanceBase:
        case.add_transition_listener(self._on_case_event)   # subscribe; case stays manager-agnostic
        return case

    def _on_case_event(self, case, event_name, info) -> None:
        # CASE_CLOSING = phase 1 (pre-purge): manager does NOT archive here;
        # assets may still exist and the record is not yet sealed.
        # CASE_CLOSED  = phase 2 (post-purge): folder is immutable, safe to move.
        if event_name == "CASE_CLOSED":
            self.on_case_closed(case)            # prompt archive; reap_closed() is the safety net

    # ---- creation (the concrete class is supplied; registry NOT required) ----
    def create_case(self, case_cls, *, case_id=None, external_key=None, nickname=None,
                    **fields) -> CaseInstanceBase:
        case_id = case_id or _new_time_slug()
        folder = self._allocate_case_folder(OPEN_GROUPING, case_id)   # == slave dir of a tracked entry
        case = case_cls.create_in_folder(folder, case_id=case_id,     # writes record + CASE_NEW
                                         external_key=external_key, nickname=nickname, **fields)
        self._write_listing_card(OPEN_GROUPING, case_id, case._record)  # thin, regenerable convenience card
        return self._attach(case)

    # ---- hydration (registry REQUIRED; raises if the type is unregistered) ----
    def _hydrate(self, case_folder: Path) -> CaseInstanceBase:
        skinny = CaseRecord.peek(case_folder / RECORD_NAME)     # read identity fields only
        case_cls = self._resolve_case_cls(skinny.case_object_type)
        return self._attach(case_cls(case_folder))

    # ---- lookup ----
    def get_by_case_id(self, case_id: str) -> CaseInstanceBase | None: ...
    def get_by_external_key(self, external_key: str, *, search_archives: bool = False
                            ) -> CaseInstanceBase | None: ...

    # ---- listing ----
    def iter_open_cases(self): ...                # scan ONLY the open grouping (bounded)
    def iter_closed_cases(self, *, date_range=None): ...  # date-bounded archive scan

    # ---- archival (the manager's job, reacting to a case's CASE_CLOSED signal) ----
    def on_case_closed(self, case: CaseInstanceBase) -> None:
        """Move the whole case folder (tracked entry + its slave dir) from `open`
        into case.archive_grouping_label(). Idempotent — safe if already moved."""
        label = case.archive_grouping_label()
        self._move_case(case.case_id, src=OPEN_GROUPING, dst=[label])

    def reap_closed(self) -> int:
        """Safety-net sweep: archive any case found `closed` but still in `open`
        (e.g. created by a non-subscribed loader, or a crash mid-move). Idempotent."""
        ...

    def reopen(self, case: CaseInstanceBase) -> None: ...   # move archive -> open
    def delete(self, case: CaseInstanceBase) -> None: ...   # rare; created-in-error open items

    # ---- pulse driver ----
    async def run_pulse_cycle(self) -> None: ...            # gather pulse() over open cases only
    async def run_pulse_loop(self, default_interval_secs: float = 300.0) -> None: ...
```

Design notes:
- Backed by `CachedFileFolders`; open cases live in the `open` grouping; closed
  cases are moved to date-labeled `YYYY-MM-archive` groupings (§3).
- **Explicit, opt-in registry** (§4a). `create_case()` is handed the class
  directly — no registry required; only `_hydrate()` consults it and raises
  `UnregisteredCaseTypeError` for an unknown `case_object_type`.
- `iter_open_cases()` reads the thin **listing cards** in the `open` grouping —
  no secondary index. Open-vs-closed is implicit in the grouping. Sorting by
  fine-grained status costs one event-log read per case, acceptable at ~500 and
  deferrable to a denormalized listing field only if measured slow.
- `iter_closed_cases()` is **date-bounded** — it scans only the relevant archive
  groupings, never one giant closed set.
- The manager **observes** cases via `add_transition_listener` (`_attach`); it
  never holds a backref and is never called from a case directly (§5a). Pulse
  runs on open cases only; archived cases are inert.

---

## 8. Lifecycle (end to end)

1. **Create** — `CaseManager.create_case(TicketCase, external_key=...)` (or
   `TicketCase.create_in_folder(...)` standalone) writes the skinny `CaseRecord`
   into the case folder, constructs the (load-only) instance, and logs the
   `CASE_NEW` bookend + the initial `ENTER_STATE`. The manager then subscribes its
   archival listener. (Registration is *not* required for this path.)
2. **Work** — caller `await`s FSM triggers (`await case.assign()`), or, for a
   linear pipeline, drives `await case.advance()` / `run_to_completion()`.
   Assets attach via `write_file(..., retain=?)`. Every transition fires
   `_on_state_changed`, which appends an `ENTER_STATE` event (status = the log)
   and refreshes the in-memory activity cache. The record is **not** rewritten on
   ordinary transitions.
3. **Close** — the non-closed → closed *edge* runs a two-phase sequence: log
   `CASE_CLOSED` event → `on_closing()` subclass hook → `CASE_CLOSING` notified
   (pre-purge observers inspect assets here) → purge ephemerals → stamp and save
   `record.closed` → `CASE_CLOSED` notified (manager archives the whole case
   folder in manager mode; standalone: no-op).
4. **Reopen** (rare) — manager moves it back to `open`.
5. **Delete** (rare) — only for open items created in error; remove the whole
   case folder.

---

## 9. Cross-cutting decisions

- **Scale target:** perform well to ~500 open cases; the open scan is the hot
  path and is kept bounded by archiving. No persistent secondary index in v1.
- **`external_key` lookup:** open set = bounded scan; archives =
  date-bounded scan when `search_archives=True`. Truly random cross-archive
  lookup by `external_key` would want a supplementary manifest — an **optional,
  deferred** index (`case_id`/`external_key` → archive grouping), not core v1.
- **Concurrency:** single-host advisory locking via `FileMappedPydanticMixin`
  lock files. No distributed locking. (Any "assignee"-style advisory claim would
  be an event-log entry / derived value, not a record field, per the skinny-record
  rule.)
- **Archive move atomicity (riskiest op):** copy-then-verify-then-delete (or
  same-filesystem rename), bracketed by `MOVING`/`MOVED` events so an
  interrupted move is detectable and resumable; `open` stays authoritative until
  the move is confirmed (never present in two groupings).
- **Range of use cases (be honest):** good fit for short-lived cases, bounded
  open sets, file-bundle-per-case, human/AI-inspectable storage, mostly
  single-writer; *not* a fit for high write throughput, ACID needs, huge open
  sets, relational queries, or genuinely complex workflows (those should
  hand-code their own manager rather than start from this convenience class).

---

## 10. Dependencies

- **`transitions`** (pytransitions) — the chosen FSM engine for this model
  (DEVDAVE). Lightweight, MIT, composition pattern (`model=self`). We use its
  **async** machine (`transitions.extensions.asyncio.AsyncMachine`), which ships
  in the same package (no extra dependency). Packaging TBD: core dependency vs.
  an optional extra (e.g. `casequeue = ["transitions"]`) consistent with the
  library's lean-core + extras convention.
- Reuses existing core deps: `pydantic>=2`, `pyyaml`, `portalocker`.

---

## 11. Open questions for review

| Question | Notes / options |
|---|---|
| `transitions` as core dep vs. optional extra | Lean-core convention suggests an extra; "good-enough default" argues core. (Note: async needs `transitions.extensions.asyncio`.) |
| Async-first base vs. offer sync too | Base uses `AsyncMachine`. Do we also ship a sync variant for purely human-driven cases, or is `await` everywhere acceptable? |
| Long / restart-spanning stages | For services that outlive the process, standardize the in-flight state-pair + "job handle as event-log entry" pattern (§6b) or leave it to implementers? |
| Prompt vs. lazy archival | Manager subscribes to `CASE_CLOSED` (phase 2, post-purge) for prompt archival, with `reap_closed()` as a safety-net sweep (§5a). Remaining choice: is this dual-mode approach sufficient, or go purely lazy/sweep-only to remove the listener mechanism? |
| `CASE_CLOSING` subscriber speed | Phase 1 subscribers are on the hot path. Enforce a timeout / make the notification async if I/O-heavy subscribers are anticipated? |
| Workflow versioning | When `_fsm_transitions` change, how are in-flight cases (whose state is replayed/derived from the event log) migrated? |
| Optional archive index | Add the `external_key → archive` manifest now, or defer until a real cross-archive random-access need appears? |
| Status in listings | Fine-grained status is off-record (event-derived). If filtering/sorting open lists by status is common, is per-case event-log reads OK at ~500, or do we add a denormalized listing field? |
| `closed` stamp vs. crash mid-close | The move is the real terminal commit; if `closed` is stamped but the move is interrupted, the `MOVING`/`MOVED` events (§9) drive recovery — confirm that ordering is sufficient. |
| `CaseGroupingVersioner` snapshots | Worth wiring archive snapshots for audit, or out of scope for v1? |

---

## 12. Naming reference

| Original sketch | This model | Notes |
|---|---|---|
| `CaseInstanceBase` | `CaseInstanceBase` | unchanged (logic object) |
| `CaseManager` | `CaseManager` | backed by `CachedFileFolders`; open + date-archive groupings |
| concrete `TicketCase` / `DocumentPipelineCase` | `TicketCase` (human-driven) / `InboundDocCase` (async pipeline) | pipeline uses verb-triggers + `_run_*`; persistence inherited |
| `case_meta.json` (serialized record) | `CaseRecord` → `case_record.yaml` (always **inside** the case folder) | now a dumb, **skinny** `FileMappedPydanticMixin` model (identity only) |
| `Machine` (sync) | `AsyncMachine` (`send_event=True`) | async-first; triggers awaitable; hook sees source+dest |
| `on_enter_<closed>` cleanup | `on_closing()` + `CASE_CLOSED` event on the non-closed→closed **edge** | one hook for all terminal states; subclass hook runs before purge |
| (none) | `advance()` / `run_to_completion()` + `_pipeline` | new: flat one-step-at-a-time pipeline driver |
| (none) | `reclassify_case(new_cls)` | new: rebind to a different subclass ("audible") |
| `case_type` (record field) | `case_object_type` | bare `cls.__name__`; resolved via the explicit registry |
| `state` on the record | (removed) | status is event-derived + in-memory cached, never persisted |
| `to_dict`/`from_dict`/`save` + `_index.json` | skinny record + registry hydration | constructor is folder-anchored, load-only |
| in-memory `event_log: list[dict]` | `PrimitiveEventLog` in `<case_folder>/events/` | now file-based + inspectable; **source of truth for status & retention** |
| `case_dir` | the **case folder** (standalone: any dir; manager: a tracked entry's slave dir) | identical internal layout in both modes |
| trunk dir + `_index.json` | `open` grouping + date archive groupings + thin **listing cards** | open scan replaces index; listing card is regenerable, not a source of truth |
| `create_case` / `get_by_case_id` / `get_by_external_key` | same | unchanged names |
| `iter_open_cases` / `iter_closed_cases` | same | closed iteration is date-bounded |
| `register_case_type` | same (+ `register_case_types`, `@register`) | **explicit / opt-in**, needed only for rehydration |
| `pulse` / `run_pulse_cycle` / `run_pulse_loop` | same | unchanged |
| `write_file` / `read_file` / `retain_file` | same | rooted in the case folder's `assets/` |
| `is_open` / `is_closed` / `is_idle_for` | same | unchanged |
| `archive_grouping_label()` | new | implementer picks the archive key |
