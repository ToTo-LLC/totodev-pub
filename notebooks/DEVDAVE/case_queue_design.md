# CaseQueue — Design Exploration (DRAFT — awaiting approval)

> Status: **DRAFT — near-complete, awaiting DEVDAVE approval.** Filled in
> collaboratively during brainstorming. Nothing here is final and no
> implementation code should be written until this design is approved.

Branch: `explore/case-queue`
Started: 2026-06-22
Author: DEVDAVE (with AI pair)

---

## 1. Motivation

Many of our projects re-invent the same recurring concept: a **Case** — a job,
task, issue, estimate, ticket, or similar unit of work that:

- is **initiated** with a unique, referenceable ID,
- is **worked** (usually by one person at a time) over a short calendar lifespan
  (days to weeks),
- is eventually **closed**, after which no further activity occurs,
- carries a **bundle of related files / assets / details**,
- needs **grouping, filtering, and sorting** (commonly by date, priority, status),
- is mostly viewed as the set of **currently-open** cases, with closed cases of
  occasional historical interest.

A non-generalized instance of this already exists in the library's bug-tracking
example (Briefing 9). The goal of `CaseQueue` is to generalize that recurring
shape into a reusable mechanism built on `CachedFileFolders`.

## 2. Why CachedFileFolders as the substrate

Honest trade-offs of the file-based substrate:

**Strengths**
- Extremely easy to understand and inspect (plain files/dirs; `ls`/`grep`/`git`).
- Each case gets an automatic slave dir for asset bundling, artifacts, events.
- Excellent for AI-driven tooling to read and reason about the data.
- Built-in lifecycle (event log / state machine), metadata summary cards, and
  optional versioning/snapshots.

**Limitations (to be honest about)**
- No database-grade speed: queries are filesystem scans, not indexed lookups.
- No schema enforcement — callers must impose their own integrity (Pydantic).
- No ACID transactions or built-in concurrency control.
- Cross-entity / relational queries are manual.

This frames the **range of use cases** `CaseQueue` should target (and where it
should *not* be used). See Section 5.

## 3. Existing building blocks we can reuse

- `CachedFileFolders` / `CacheGrouping` — storage + `files()` / `filtered_map()`.
- `SimpleCacheORM` + `PrimitiveSchemaResolver` + `SlugProvider` — Pydantic model
  persistence as `{ClassName}/{ClassName}-{slug}.yaml`.
- Per-file slave dir — case asset bundle + artifacts.
- `PrimitiveEventLog` / `PrimitiveStateMachineLog` — case lifecycle/status.
- `metadata()` — fast "summary card" for filtering/sorting without replaying events.
- `CachedGroupingVersioner` — snapshots/tags for audit/history.
- File proxies + truncation — optional ingestion from external document sources.

## 4. Design decisions (resolved during brainstorming)

- [x] **Positioning / core win**: DECIDED — the primary job is to be a fast
      **"currently-open vs closed" view/query layer**: keep listing/sorting/
      filtering *open* cases cheap despite the file-based substrate being
      scan-based. Lifecycle, asset bundling, and status are supporting features
      arranged around this spine.
- [x] **Case identity & schema**: SUPERSEDED by the "Two-layer object model"
      decision below. (`case_id` lives on the dumb `CaseMarker`; logic/hooks live
      on `CaseInstance`.) `archive_grouping_label()` is a `CaseInstance` method
      the implementer can override (build it from open date, close date, or
      anything), called at archival time to pick the destination archive grouping.
- [x] **case_id generation**: DECIDED — default to the library's existing
      time-based base36 slug (as `PrimitiveSchemaResolver`), with an override
      hook for natural keys.
- [x] **external_key (NEW per DEVDAVE)**: DECIDED — `CaseMarker` carries an
      optional, CALLER-SUPPLIED `external_key: str | None` (arbitrary string,
      maps to an external system; never auto-generated). The queue supports
      `get_by_external_key(external_key, search_archives=False)`:
      - Open set: a bounded marker scan (≤~500) matching `external_key`.
      - Archives: a date-bounded scan when `search_archives=True`; truly random
        cross-archive lookup by external key would need a supplementary index —
        DEFERRED to the optional indexing future (Section 8/Indexing).
- [x] **Extensibility**: DECIDED — SUBCLASSING. Implementers subclass
      `CaseInstance` (override hook methods: `archive_grouping_label`,
      `is_terminal`, status model) and subclass `CaseMarker` (add domain fields).
- [x] **Open vs. closed lifecycle / grouping strategy**: DECIDED (subtle scheme) —
      - A single **"open" grouping** is the hot partition (bounded ~500): cheap to
        scan/sort/filter; the core-win path.
      - **Archive groupings keyed by a date attribute**, e.g. `2026-08-archive`.
        Each archive grouping stays small. Archive inquiries are usually
        date-bounded ("closed/created in last 90 days / last year"), so only the
        relevant date groupings are scanned. Aging out old data = dropping whole
        groupings.
      - Closing a case MOVES its file + slave dir (assets included) from the open
        grouping into the appropriate date archive grouping. Reopen = move back.
      - **Optional supplementary ID->location lookup** for random access to
        archived cases by ID (only if needed).
      - The date attribute + granularity that produce the archive label are NOT
        fixed by the library: `CaseInstance.archive_grouping_label()` computes the
        label (from open date, close date, or anything), so implementers choose
        (close-date/monthly is the suggested default).
- [x] **Asset bundle**: DECIDED — assets live in the case's slave dir.
      `CaseInstance` exposes attach/list/read/remove helpers over the slave dir
      (a `LocalFileProxy`-style copy-in for attach). Assets travel with the case
      on archive/reopen because the whole slave dir moves.
- [x] **Querying/sorting/filtering**: DECIDED — `list_open()` scans only the open
      grouping's marker files (≤~500), reading the denormalized summary fields
      (`cached_status`, `priority`, dates, `tags`, `assignee`); sort/filter is
      done in memory. No persistent secondary index in v1 (YAGNI at this scale);
      an optional per-session in-memory snapshot can be added if needed. Archive
      queries are date-bounded and only scan the relevant archive groupings.
- [x] **Concurrency / single-worker assumptions**: DECIDED — rely on the marker's
      `FileMappedPydanticMixin` lock-file locking for safe single-writer edits;
      `assignee` is just a field (advisory claim). No distributed locking in v1.
- [ ] **Indexing**: NONE in v1 (deferred). Optional future: open-set snapshot,
      tag index, and `case_id -> archive grouping` lookup for random access.
- [x] **Two-layer object model (REVISED per DEVDAVE)**: DECIDED — split the
      "dumb data" from the "logic":
      - **`CaseMarker`** = a DUMB Pydantic data model
        (`class CaseMarker(BaseModel, FileMappedPydanticMixin)`). It is the
        serialized "case marker file." It carries domain fields plus
        denormalized *summary* fields used for cheap listing/sorting/filtering:
        `case_id`, `external_key`, `created_at`, `updated_at`, `cached_status`,
        `priority`, optional `assignee`, and **`tags`**. NO domain logic /
        lifecycle methods live here (keep Pydantic objects dumb).
      - **`CaseInstance`** = a plain (non-Pydantic) logic class that OWNS/MANAGES
        one `CaseMarker`. Constructed from the marker filepath; it CREATES or
        LOADS the marker. It also owns the case's slave-dir asset bundle and its
        event log, and hosts all the logic: `status()`, `archive_grouping_label()`,
        terminal detection, `close()`/`reopen()`, asset-bundle ops, event logging.
        Implementers subclass `CaseInstance` for logic hooks and subclass
        `CaseMarker` for domain fields.
      - Tag SEARCH may later need a supplementary structure (index) — deferred,
        not a v1 concern.
- [x] **Lean on FileMappedPydanticMixin (for the marker only)**: DECIDED — gives
      the `CaseMarker` (a) the non-serialized bound filepath (`persisted_file()`,
      private `_file_path`), (b) file-lock concurrency control (supports "one
      worker at a time"), (c) change tracking + context-manager autosave,
      (d) `reload_from_file()` / `would_conflict()`. This is generic persistence
      plumbing, not domain logic, so the marker stays "dumb."
- [x] **Serialization**: DECIDED — YAML, deterministic key order. The mixin's
      YAML dumper uses `sort_keys=False` => fields serialize in Pydantic
      definition order => clean, churn-free git diffs. (No JSON unless a specific
      reason arises.)
- [x] **Status model (REVISED per DEVDAVE)**: DECIDED —
      - **Status is a kind of EVENT** in the case's event log (Briefing 4). The
        event log is the source of truth for lifecycle/status transitions.
      - `CaseInstance.status()` DEDUCES the current status by examining the event
        log (default semantics via `PrimitiveStateMachineLog`). Status may be
        richer than open/closed.
      - The current status is also CACHED on the marker file (`cached_status`)
        so `list_open` can sort/filter without opening every event log. Event log
        = truth; `cached_status` = derived summary (Briefing 5 pattern).
      - "Terminal" status is what makes a case eligible to archive. Which statuses
        are terminal is defined by the (pluggable) status model.
- [x] **Pluggable FSM (NEW per DEVDAVE)**: DECIDED — separate *transition
      validation/logic* (in-memory FSM, PLUGGABLE) from *transition
      record/persistence* (event log, ALWAYS).
      - Default status model = `PrimitiveStateMachineLog` (zero new deps,
        file-backed, already aligned with the event log).
      - Define a thin **status-model protocol** (states, allowed transitions,
        terminal states, current-state deduction) so downstream implementers can
        plug a real FSM — candidates: `transitions` (pytransitions, ~6.5k★, MIT,
        imperative, no structural validation) or `python-statemachine`
        (declarative, validates unreachable/trap states at definition time).
      - The chosen FSM validates/drives a transition; the result is recorded as
        an event and the marker's `cached_status` is updated.
- [x] **Delete (clarified per DEVDAVE)**: supported but RARE — typically only for
      open items created in error. Not a hot path; simple removal of marker +
      slave dir from the open grouping.
- [x] **External source integration**: OUT of scope for v1. Assets are just
      files, so external mirroring (proxies/sync) can be layered on later without
      changing the core. (DEVDAVE confirmed Option A, local-first.)
- [x] **Scope of v1**: DECIDED — Option A (local-first core) PLUS a first-class
      per-case **event log** and a **pluggable status/FSM model**. See Section 5a.

## 4a. Scale constraints (DECIDED)

- Open set is typically **< 1000**, often **200–500 at most**, frequently **< 100**.
- Design target: **perform well up to ~500 open cases**.
- Implication: scanning a single "open" partition (~500 small summary cards) is
  affordable. A heavyweight persistent secondary index is likely premature
  (YAGNI) at this scale; the priority is keeping *closed* cases out of the hot
  path so the open scan stays bounded. An in-process cached snapshot per session
  is a cheaper optimization if needed.

## 5. Intended range of use cases

**Good fit when:**
- Cases have short lifespans (days–weeks) and a bounded open set (≤ ~500).
- The open set is the primary view; closed cases are mostly historical.
- Each case is a bundle: a small record + related files + a lifecycle.
- Human-inspectable, git-diffable, AI-readable storage is valued over raw speed.
- Worked largely by one person at a time (advisory assignment is enough).

**NOT a good fit when:**
- You need high write throughput, ACID transactions, or distributed locking.
- The open set is very large (tens of thousands) or queries are relational.
- You need rich secondary-index queries (full-text, joins) as a core feature.
- Many writers contend on the same case concurrently.

### 5a. v1 scope (DECIDED — Option A + event log + pluggable FSM)

IN: create/get; `list_open` (sort + filter by date/priority/status/assignee/
tags); update; `close` (move to date archive grouping); `reopen`; `delete`
(rare); per-case asset bundle (attach/list/read/remove); date-bounded archive
queries; per-case event log; pluggable status/FSM model (default
`PrimitiveStateMachineLog`).

OUT (v1): external document-source mirroring (proxies/sync); persistent
secondary indexes; tag-search index; distributed locking.

## 6. Proposed architecture (Approach 1, refined)

Three collaborating pieces:

1. **`CaseQueue`** — the container/manager. Owns one `CachedFileFolders` cache
   and manages multiple groupings: one `open` grouping (hot) and N date-labeled
   archive groupings (cold), created on demand. Responsibilities:
   - factory: `create_case(...)`, `open_case(case_id)` -> `CaseInstance`
   - queries: `list_open(...)`, `iter_archive(date_range=...)`, `get(case_id)`
   - lifecycle orchestration: performs the physical move on `close`/`reopen`
   - knows the `CaseInstance` subclass + `CaseMarker` subclass to use.

2. **`CaseInstance`** — a plain (non-Pydantic) logic object that manages ONE
   case. Constructed from the marker filepath; creates or loads its marker.
   Owns:
   - `self.marker: CaseMarker` — the dumb data model (persisted via mixin)
   - the slave-dir **asset bundle** (`attach_file`, `list_assets`, `read_asset`,
     `remove_asset`)
   - the **event log** (`PrimitiveEventLog` in the slave dir)
   - the **status model** (default `PrimitiveStateMachineLog`; pluggable)
   - logic hooks (overridable): `archive_grouping_label()`, `is_terminal()`,
     `status()` (deduce from event log), `record_status(new_status, payload)`.

3. **`CaseMarker(BaseModel, FileMappedPydanticMixin)`** — dumb data, the marker
   file. Domain fields + denormalized summary fields for cheap listing. No
   lifecycle logic.

### Data flow
- **Create**: `CaseQueue.create_case()` allocates a `case_id`, writes a
  `CaseMarker` into the open grouping, returns a `CaseInstance`; logs an initial
  status event.
- **Work**: caller mutates marker fields / attaches assets / records status
  events via the `CaseInstance`. Each status event updates `marker.cached_status`.
- **List open**: `CaseQueue.list_open()` iterates open-grouping marker files,
  reads summary fields, filters/sorts in memory.
- **Close**: when status becomes terminal, `CaseQueue.close(case)` computes
  `case.archive_grouping_label()` and MOVES the marker file + slave dir into the
  archive grouping. Reopen moves it back to `open`.

### Storage layout (illustrative)
```text
<cache_root>/
  open/                                  # the hot grouping
    Case/Case-<id>.yaml                  # marker file (summary card)
    Case/Case-<id>.yaml._slave/
      events/  e001_ENTER_STATE@OPEN.yaml ...
      assets/  contract.pdf, photo.jpg ...
      metadata.yaml
  2026-08-archive/                       # cold grouping (by archive_grouping_label)
    Case/Case-<id>.yaml (+ ._slave/)
  2026-07-archive/
    ...
```

### Error handling
- **Close/reopen move** is the riskiest op (moves a directory across groupings).
  Strategy: copy-then-verify-then-delete (or rename when same filesystem), record
  a `MOVING`/`MOVED` event so an interrupted move is detectable/resumable; never
  leave a case in two groupings (open is authoritative until move confirmed).
- **Marker edits** use the mixin lock; `would_conflict()` guards against
  clobbering external edits.
- **Status/FSM**: invalid transitions raise (when an FSM is plugged in);
  default `PrimitiveStateMachineLog` records linearly without enforcement.

### Testing
- pytest under `src/totodev_pub/tests/` (per project convention).
- Unit: marker round-trip (YAML determinism), status deduction from event log,
  archive label computation, terminal detection.
- Integration: create -> work -> close -> archive -> reopen; `list_open`
  sort/filter correctness; date-bounded archive iteration; interrupted-move
  recovery; delete of an open (created-in-error) case.

## 7. Public API sketch (illustrative, not final)

```python
class CaseMarker(BaseModel, FileMappedPydanticMixin):
    case_id: str                       # default: time-based base36 slug
    external_key: str | None = None    # caller-supplied; maps to an external system
    created_at: datetime
    updated_at: datetime
    cached_status: str
    priority: int = 0
    assignee: str | None = None
    tags: list[str] = []
    # ... implementers subclass to add domain fields ...

class CaseInstance:
    def __init__(self, marker_path: str | Path, *, create_with: CaseMarker | None = None): ...
    @property
    def marker(self) -> CaseMarker: ...
    def status(self) -> str: ...                       # deduced from event log
    def record_status(self, new_status: str, payload=None) -> None: ...
    def is_terminal(self) -> bool: ...
    def archive_grouping_label(self) -> str: ...        # e.g. "2026-08-archive"
    def event_log(self) -> PrimitiveEventLog: ...
    # asset bundle
    def attach_file(self, src: str | Path, name: str | None = None) -> Path: ...
    def list_assets(self) -> list[Path]: ...
    def read_asset(self, name: str) -> bytes: ...
    def remove_asset(self, name: str) -> None: ...

class CaseQueue:
    def __init__(self, cache: CachedFileFolders, *, instance_cls=CaseInstance,
                 marker_cls=CaseMarker, status_model=...): ...
    def create_case(self, *, external_key: str | None = None, **fields) -> CaseInstance: ...
    def get(self, case_id: str) -> CaseInstance | None: ...
    def get_by_external_key(self, external_key: str, *, search_archives: bool = False
                            ) -> CaseInstance | None: ...
    def list_open(self, *, where=None, sort_by="created_at", reverse=False
                  ) -> list[CaseInstance]: ...
    def close(self, case: CaseInstance) -> None: ...    # move open -> archive
    def reopen(self, case: CaseInstance) -> None: ...   # move archive -> open
    def delete(self, case: CaseInstance) -> None: ...   # rare; created-in-error
    def iter_archive(self, *, date_range=None, where=None): ...
```

## 8. Out of scope (v1)

- External document-source mirroring (proxies/sync) — layer on later.
- Persistent secondary indexes / tag-search index — add only if scale demands.
- Distributed/multi-host locking — single-host advisory locking only.
- Relational queries / joins across cases.
