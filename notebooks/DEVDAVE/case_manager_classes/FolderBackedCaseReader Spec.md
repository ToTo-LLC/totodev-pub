# FolderBackedCaseReader — Design Spec

> **Status:** DRAFT — ready for implementation review.
> **Priority:** implement BEFORE `CaseManager`; see §7 for rationale.
> **Companion doc:** `CaseManager Model.md` §6d.

---

## 1. Purpose

`FolderBackedCaseReader` is a **read-only OO façade** over the static lock-free peek
methods of `FolderBackedCase`. It provides a convenient, ergonomic object that:

- Works in **any process** — including web workers that have never imported a concrete
  case class, because it has **zero class-resolution dependencies.**
- Exposes the same read properties as a live `FolderBackedCase` (identity, state, open/
  closed, dwell, last activity, fail count, asset paths, raw event log) through a uniform
  interface.
- Satisfies a `CaseReadView` **Protocol** that the live `FolderBackedCase` also satisfies,
  enabling polymorphic read code that accepts either without knowing which it holds.

This is **not** a born-detached case object with mutations blocked. It is a genuinely
separate, smaller class with no write surface, no lease, and no FSM execution at all.

---

## 2. Why not just use the existing `peek_*` methods directly?

The static peeks (`peek_case_record`, `peek_case_events`, `peek_case_assets`) already
exist and are lock-free/registry-free. The reader adds:

1. **An object** — so call sites pass one thing around rather than a folder path plus
   three separate peek calls.
2. **Derived read properties** — `case_state`, `case_is_closed`, `case_dwell_secs`,
   `case_transition_fail_count` are composed from the raw peeks; the reader does that
   composition once.
3. **Polymorphism** — via the `CaseReadView` Protocol a function that reads cases can
   accept a reader *or* a live case object without caring which.

The `CaseEventLogReader` (already implemented) does most of the heavy lifting for
`case_state`, `last_activity`, and `case_transition_fail_count`; `case_dwell_secs`
is derived from the most recent `CASE_ENTER_STATE` event mtime. The reader is a thin
wrapper.

---

## 3. Relationship to `FolderBackedCase`

```text
FolderBackedCase
│   has static peek methods (peek_case_record, peek_case_events, peek_case_assets)
│   has CaseEventLogReader internally (same derivations)
│   satisfies CaseReadView Protocol (structurally, no change required)
│
FolderBackedCaseReader
    delegates to the same static peek methods
    satisfies CaseReadView Protocol
    NO inheritance from FolderBackedCase
    NO lease, NO FSM, NO write surface
```

The two classes share **no implementation inheritance**. They share a **structural
Protocol** (`CaseReadView`) that neither needs to explicitly subclass. `FolderBackedCase`
already satisfies it; the reader is written to satisfy it. This avoids any invasive
change to the mature live-case class.

---

## 4. `CaseReadView` Protocol

A small `typing.Protocol` (type-checking surface only; no runtime `isinstance` checks
required) defined in its own module,
e.g. `folder_backed_case_support/case_read_view.py`:

```python
from typing import Protocol
import datetime
from pathlib import Path

class CaseReadView(Protocol):
    @property
    def case_id(self) -> str: ...
    @property
    def case_external_key(self) -> str | None: ...
    @property
    def case_nickname(self) -> str | None: ...
    @property
    def case_object_type(self) -> str: ...   # bare class name from record; str only, not resolved
    @property
    def case_folder(self) -> Path: ...
    @property
    def case_state(self) -> str | None: ...  # most recent CASE_ENTER_STATE value; None if brand-new
    @property
    def case_is_open(self) -> bool: ...
    @property
    def case_is_closed(self) -> bool: ...
    @property
    def case_created(self) -> datetime.datetime: ...
    @property
    def case_closed_at(self) -> datetime.datetime | None: ...
    @property
    def case_dwell_secs(self) -> float: ...
    @property
    def case_last_activity(self) -> datetime.datetime | None: ...
    @property
    def case_transition_fail_count(self) -> int: ...
    @property
    def case_assets(self) -> "CaseAssets": ...  # list_assets(), asset_path(), keep_list()
    @property
    def case_events(self) -> "CaseEventLogReader": ...  # raw event log, for history views
```

`FolderBackedCase` is intended to satisfy this same surface once the §6 preparatory
properties are confirmed/added. The reader implements all of them atop the static peeks.

---

## 5. `FolderBackedCaseReader` design

### Construction

```python
class FolderBackedCaseReader:
    def __init__(self, case_folder: Path) -> None: ...
```

That's it. No registry, no class, no options. The folder is the sole key.

### Data source for each property

| Property | Source | Notes |
|---|---|---|
| `case_id` | `peek_case_record(folder).case_id` | from record |
| `case_external_key` | record | from record |
| `case_nickname` | record | from record |
| `case_object_type` | record | `case_object_type` field; string only, never resolved |
| `case_folder` | constructor arg | stored directly |
| `case_created` | record | already aware-UTC via `CaseRecord` validators |
| `case_closed_at` | record | `record.closed`; `None` if open |
| `case_is_closed` | `CaseEventLogReader.is_closed` | event-based status (preferred read source) |
| `case_is_open` | `not case_is_closed` | derived |
| `case_state` | `CaseEventLogReader.current_state` | most recent `CASE_ENTER_STATE` value |
| `case_dwell_secs` | latest successful state-entry timestamp (`CASE_ENTER_STATE`) from the event log, then elapsed seconds to now | semantic target: seconds since last successful state transition |
| `case_last_activity` | `CaseEventLogReader.last_activity` | most recent event mtime |
| `case_transition_fail_count` | `CaseEventLogReader.transition_fail_count` | same guard-count as live case |
| `case_assets` | `peek_case_assets(folder)` | `CaseAssets` — `list_assets()`, `asset_path()`, `keep_list()` |
| `case_events` | `peek_case_events(folder)` | `CaseEventLogReader` — for history/audit views |
| `case_lease_secs_left` | `peek_lease_secs_left(folder)` | `float \| None`; lock-free lease-liveness peek. **Reader-only — NOT on the `CaseReadView` Protocol** (see §5 "lease observability" and §9). |

#### Lease observability (`case_lease_secs_left`)

The reader cannot *hold* a lease, but it *can* lock-free **observe** whether one is
held. `case_lease_secs_left` returns the positive seconds remaining when a live lease
is held, or `None` when the lease is free. "File absent" and "already expired" are
**collapsed into `None`** on purpose: to a read-only observer both mean the lease is
reclaimable, and — per the design constraint — the actual owner is never knowable by
inspection, so exposing raw file timestamps would help almost no one. A consumer that
genuinely needs the absent-vs-expired tri-state already has
`FolderBackedCase.is_heartbeat_expired(folder)` (`None`/`True`/`False`).

Like `case_dwell_secs`, this is a `now()`-relative computed value: it decays between
reads and two accesses will differ. It is a property (not a method) for idiom
consistency with the rest of the reader; the volatility is documented behavior.

### Snapshot semantics

Each property **reads from disk when accessed** — the simplest possible behavior and
right for the primary use case (construct-per-request in a web handler). No caching,
no `refresh()` API. This means multiple accesses to the same reader may produce
inconsistent results if the case is advancing underneath it (the event log is
append-only, so the worst that can happen is a state read slightly before an in-flight
transition completes). This is documented behavior — the reader is explicitly a
point-in-time-per-property view — and acceptable for UI rendering.

If a snapshot-consistent view is needed (e.g. building a report), construct once and
hold the resulting property values, or read the relevant sub-objects (record, event-log
reader) directly and hold those.

Implementation note: include an explicit class-level docstring statement that most
properties are expected to perform file reads unless their docstring says otherwise; any
caching policy belongs outside this class.

### Read-failure behavior

Read failures propagate exceptions (do not suppress). Keeping the underlying exception is
acceptable/preferred as long as path context is preserved (which file/folder read failed).

### What the reader intentionally does NOT expose

- **Lease (holding)** — it has no lease and cannot acquire, heartbeat, or release one;
  construction never *writes* the lease file. Note this excludes only *owning* a lease;
  the reader may still lock-free **observe** lease liveness via `case_lease_secs_left`
  (read-only stat, never a write). See "Lease observability" above.
- **FSM** — no `case_advanceable`, no state-graph queries. These require the compiled
  FSM, which requires the concrete class. If needed, use `case_type_registry.rehydrate`.
- **Write surface** — no `case_advance`, no triggers, no `case_log_alert`, no asset
  writes. Not even a "would raise" stub; they simply don't exist.
- **Typed domain `_record_cls` fields** — the base `CaseRecord` is parsed; subclass
  fields are silently absent (Pydantic drops unknown fields on the base model). Anyone
  who needs `ticket.priority` should rehydrate the live object or parse `_record_cls`
  themselves. No sugar for this on the reader.
- **Class resolution sugar** — `case_object_type` is a string. The reader does not
  import, look up, or provide the class. `case_type_registry` and `peek_class` are the
  first-class tools for that.

---

## 6. Preparatory changes to `FolderBackedCase` (minor)

Before implementing the reader, confirm or add the following to `FolderBackedCase` so
the Protocol can be satisfied uniformly by both classes:

1. **`case_object_type` property** — exposes `self._record.case_object_type` as a
   property. Verify this is already a public property or add a one-liner. (The live
   class already has `case_id`, `case_external_key`, etc.; `case_object_type` may be
   the one that's missing as a property.)
2. **`case_closed_at` property** — exposes `self._record.closed` (the UTC datetime or
   `None`). Distinct from `case_is_closed` (bool). Confirm or add.
3. **`case_events` property** — a `CaseEventLogReader` over the case's event log.
   Already backed by `self._journal.reader`; just needs a public property if not present.
4. **`case_last_activity` property** — `self._journal.last_activity`; confirm or add.
5. **`get_case_reader(path)` staticmethod** — add a discoverability factory on
   `FolderBackedCase` that returns `FolderBackedCaseReader` for the folder path.
6. **`peek_lease_secs_left(folder)` staticmethod** — DONE. Lock-free lease-liveness
   peek returning `float | None`, sibling to the existing `is_heartbeat_expired`. It
   delegates to `HeartbeatLease.secs_left(folder / LEASE_NAME)` (also added). The
   reader's `case_lease_secs_left` property is a thin passthrough to this static.

None of these are behavior changes; they are read-only property/peek additions that
expose data the class already has internally.

---

## 7. Rationale for implementing before `CaseManager`

1. **Independent utility.** The reader is useful in any process that reads cases —
   dashboards, CLI tools, monitoring scripts, web handlers — entirely without the
   manager.
2. **Removes hypotheticals from the manager design.** `manager.peek_case(case_id)` is
   currently described as "→ `FolderBackedCaseReader`" in the manager spec. With the
   reader implemented, that surface is concrete.
3. **May surface prep changes.** The §6 preparatory changes to `FolderBackedCase` are
   small, but better discovered and made before the manager wraps them.
4. **Independently testable.** Tests for the reader can be written against real case
   folders without a driver or manager in the picture.

---

## 8. File locations (proposed)

| Artifact | Path |
|---|---|
| `CaseReadView` Protocol | `src/totodev_pub/folder_backed_case_support/case_read_view.py` |
| `FolderBackedCaseReader` | `src/totodev_pub/folder_backed_case_reader.py` |
| Tests | `tests/test_folder_backed_case_reader.py` |

Re-export `CaseReadView` and `FolderBackedCaseReader` from `folder_backed_case.py`
and add `FolderBackedCase.get_case_reader(path)` for discoverability.

---

## 9. Open questions

| Question | Notes |
|---|---|
| **`case_advanceable` on the Protocol?** | Excluded today (needs FSM). If FSM-derived properties become important in the read path, reconsider. |
| **`case_lease_secs_left` on the Protocol?** | Excluded today (reader-only). It is operational liveness, not case *content*, and the value would mean subtly different things on a reader (peek of whoever holds it) vs a live `FolderBackedCase` (its own self-beaten lease), which is a Protocol/LSP smell. Promote only if a real consumer needs it polymorphically across both types — and define the live-case semantics deliberately at that point. |
