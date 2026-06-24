# CaseManager — Design Model

> **Status:** DRAFT — illustrative code is design-fidelity, **not** production
> code. No implementation until approved.
>
> **Companion document:** `CaseInstanceBase Model.md` covers `CaseRecord`,
> `CaseInstanceBase`, the case folder layout, and all concrete case patterns.
> This document covers `CaseManager`: one complete implementation of a collection
> host built on top of that class family.

---

## 1. Overview

`CaseManager` is an *optional* collection host for `CaseInstanceBase` subclasses.
It is not the only way to manage a collection of cases — any code that allocates
folders and subscribes to lifecycle signals qualifies — but it is the
full-featured, opinionated reference implementation.

Key responsibilities:
- Maintain an **`open` grouping** (bounded hot partition, ~500 cases) and
  **date-labeled archive groupings** (cold, date-bounded) inside a
  `CachedFileFolders` cache.
- Create cases (delegate to `CaseInstanceBase.create_in_folder()`), write thin
  **listing cards** for cheap open-set enumeration without descending into every
  case folder.
- Hydrate cases from disk via an **explicit, opt-in case-type registry**.
- Subscribe to `CASE_CLOSED` via `add_transition_listener` and move the case
  folder to the appropriate archive grouping.
- Provide `reap_closed()` as an idempotent safety-net sweep for cases that closed
  without a listener (e.g. after a crash or a standalone load).
- Drive the `pulse` cycle over open cases.

---

## 2. Storage layout in manager mode

In manager mode each case folder is the **slave dir** of a thin tracked entry in
a `CachedFileFolders` grouping. The tracked file itself is a regenerable
**listing card** — the manager uses it to enumerate and sort the open set cheaply
without descending into every case folder. It is *not* a second source of truth
(the authoritative record is always `<case_folder>/case_record.yaml`).

```text
<cache_root>/
  open/                                       # the hot grouping (bounded ~500)
    Case/Case-<case_id>.yaml                  # listing card (regenerable; or empty touch-file)
    Case/Case-<case_id>.yaml._slave/          # == THE CASE FOLDER (see CaseInstanceBase Model §3)
  2026-08-archive/                            # cold grouping, label from archive_grouping_label()
    Case/Case-<case_id>.yaml (+ ._slave/ == the case folder)
  ...
```

The **case folder's internal layout** (`case_record.yaml`, `events/`, `assets/`)
is identical here and in standalone mode — see `CaseInstanceBase Model.md §3` for
the canonical layout. Nothing about the case folder changes when it is managed;
only the outer shell (the listing card and which grouping it sits in) is added by
the manager.

Why this structure:
- **Open listing is naturally bounded** — `iter_open_cases()` reads listing cards
  in the `open` grouping (≤ ~500), never the ever-growing closed history.
- **Aging out** = dropping a whole archive grouping.
- **The whole case folder travels** atomically on archive/reopen (record + assets
  + event log move together, since the slave dir moves with its tracked entry).
- The listing card is **regenerable**, so the `_index.json` race risk (a stale
  index driving wrong results) never arises for the hot path.

---

## 3. `CaseManager`

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

    # ---- the manager OBSERVES cases; it is never called "up" to ----
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
  cases are moved to date-labeled `YYYY-MM-archive` groupings (§2).
- **Explicit, opt-in registry** (`CaseInstanceBase Model §4a`). `create_case()`
  is handed the class directly — no registry required; only `_hydrate()` consults
  it and raises `UnregisteredCaseTypeError` for an unknown `case_object_type`.
- `iter_open_cases()` reads the thin **listing cards** in the `open` grouping —
  no secondary index. Open-vs-closed is implicit in the grouping. Sorting by
  fine-grained status costs one event-log read per case, acceptable at ~500 and
  deferrable to a denormalized listing field only if measured slow.
- `iter_closed_cases()` is **date-bounded** — it scans only the relevant archive
  groupings, never one giant closed set.
- The manager **observes** cases via `add_transition_listener` (`_attach`); it
  never holds a backref and is never called from a case directly. Pulse runs on
  open cases only; archived cases are inert.

---

## 4. Lifecycle (end to end)

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

## 5. Cross-cutting decisions

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

## 6. Open questions for review

| Question | Notes / options |
|---|---|
| `transitions` as core dep vs. optional extra | Lean-core convention suggests an extra; "good-enough default" argues core. (Note: async needs `transitions.extensions.asyncio`.) |
| Async-first base vs. offer sync too | Base uses `AsyncMachine`. Do we also ship a sync variant for purely human-driven cases, or is `await` everywhere acceptable? |
| Long / restart-spanning stages | For services that outlive the process, standardize the in-flight state-pair + "job handle as event-log entry" pattern (`CaseInstanceBase Model §6b`) or leave it to implementers? |
| Prompt vs. lazy archival | Manager subscribes to `CASE_CLOSED` (phase 2, post-purge) for prompt archival, with `reap_closed()` as a safety-net sweep. Remaining choice: is this dual-mode approach sufficient, or go purely lazy/sweep-only to remove the listener mechanism? |
| `CASE_CLOSING` subscriber speed | Phase 1 subscribers are on the hot path. Enforce a timeout / make the notification async if I/O-heavy subscribers are anticipated? |
| Workflow versioning | When `_fsm_transitions` change, how are in-flight cases (whose state is replayed/derived from the event log) migrated? |
| Optional archive index | Add the `external_key → archive` manifest now, or defer until a real cross-archive random-access need appears? |
| Status in listings | Fine-grained status is off-record (event-derived). If filtering/sorting open lists by status is common, is per-case event-log reads OK at ~500, or do we add a denormalized listing field? |
| `closed` stamp vs. crash mid-close | The move is the real terminal commit; if `closed` is stamped but the move is interrupted, the `MOVING`/`MOVED` events (§5) drive recovery — confirm that ordering is sufficient. |
| `CaseGroupingVersioner` snapshots | Worth wiring archive snapshots for audit, or out of scope for v1? |

---

## 7. Naming reference

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
