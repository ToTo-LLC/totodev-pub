# CaseRecord: capture `fsm_state_chains` — Design Spec

> **Status:** Approved design — ready for implementation plan.
> **Date:** 2026-06-30
> **Scope:** `CaseRecord` (`folder_backed_case_support/case_record.py`) and the two
> write sites in `FolderBackedCase` (`folder_backed_case.py`).

---

## 1. Purpose

Persist a case's declarative FSM definition — the case class's `fsm_state_chains`
list — into the on-disk `case_record.yaml`. This lets a reader who has only the case
folder (no access to the Python source) see the state/transition/guard DSL that
describes what the object does.

The chains are an extremely stable, class-level attribute: they almost never change
during a single object's life, though they may evolve across project versions. They
are therefore a natural fit for the near-immutable identity record rather than the
volatile event log.

---

## 2. Non-goals / contract boundaries

- **Documentation only.** The persisted value is purely informational. It is
  **never read back** to build, validate, or alter the FSM. The live class attribute
  `cls.fsm_state_chains` (compiled into `cls._fsm`) remains the sole source of truth
  for behavior. Editing the value in the YAML file does **not** change the state
  machine.
- **Not a live mirror.** The field is stamped at case inception and persisted at the
  normal record-write cadence. No special effort is made to keep it fresh beyond the
  existing flush machinery. By contract, the field **generally remains as stamped at
  creation** and is *not* guaranteed to track later edits to the class's
  state-machine model during an existing object's life. It is a best-effort snapshot
  for human readers.

---

## 3. Design

### 3.1 Field on `CaseRecord` (strict, required)

Add to the **base** `CaseRecord`:

```python
fsm_state_chains: list[str]   # required: no default, not Optional
```

- **Required / no backward compatibility.** The library is unreleased; we choose
  strictness over leniency. A record on disk lacking this field will fail to load.
  This is intentional.
- **On the base record.** Living on the base (not a subclass) means it is readable
  lock-free via `FolderBackedCase.peek_case_record()` and by the planned
  `FolderBackedCaseReader` with no concrete class in hand.
- **Field order.** Placed **last** in field-definition order (after `closed`) so the
  skinny identity fields stay at the top of the YAML and the potentially multi-line
  chains list trails them, keeping diffs clean. `CaseRecord` emits in definition order
  (`sort_keys=False`).

### 3.2 Write sites in `FolderBackedCase`

1. **Inception — `create_case_in_folder()`** (`folder_backed_case.py` ~line 386):
   pass `fsm_state_chains=cls.fsm_state_chains` into the `cls._record_cls(...)`
   constructor alongside the other identity fields.

2. **Reclassify — `case_reclassify_to()`** (`folder_backed_case.py` ~line 1014):
   `case_reclassify_to()` is a deliberate identity change that already re-stamps
   `case_object_type` and force-flushes the record in the new class's schema. Re-stamp
   `fsm_state_chains` there too:
   `fresh._record.fsm_state_chains = new_cls.fsm_state_chains`, set adjacent to the
   existing `fresh._record.case_object_type = new_cls.__name__` line and committed by
   the same `fresh._flush_record(force=True)`. This keeps the record internally
   consistent: the stamped type and the documented chains both describe the new class.

3. **No reconcile-on-load.** `_bind_existing_case_dir()` is **not** modified. There is
   deliberately no compare-and-restamp on every bind/rehydrate; the field rides the
   existing `_flush_record()` cadence only.

### 3.3 Edge case — manual-FSM classes

A class that overrides `compile_fsm()` and leaves `fsm_state_chains = []` will stamp
`[]`. "Required" means *present*, so an empty list is a legal value; it truthfully
reflects that class's empty DSL attribute. (Such a class builds its FSM by hand, so
there is no DSL to document.)

---

## 4. Persistence example

`case_record.yaml` after this change (illustrative):

```yaml
case_object_type: TicketCase
case_id: c-001
external_key: null
nickname: null
created: 2026-06-30T15:40:00+00:00
closed: null
fsm_state_chains:
- ^new --open_ticket-->open ==close_ticket-->closed^*--@DWELL>14d#non_responsive-->auto_closed^
```

---

## 5. Impact

- **Existing tests / fixtures.** Every record is born through
  `create_case_in_folder()`, which will supply the field; `TypedRecord` only *adds* a
  field. No direct `CaseRecord(...)` construction exists in the library or tests that
  would break. Any committed on-disk record fixture lacking the field (if any) must be
  regenerated.
- **Peek / reader paths.** `peek_case_record()` with the base `CaseRecord` continues
  to work because the field is on the base and present on disk. The field becomes
  available to `FolderBackedCaseReader` for free.

---

## 6. Testing

- A newly created case's `case_record.yaml` contains `fsm_state_chains` equal to the
  class's `fsm_state_chains`.
- The value survives a detach + rehydrate round-trip.
- `peek_case_record(folder)` (base `CaseRecord`) exposes the chains without the
  concrete class.
- After `case_reclassify_to(NewClass)`, the record's `fsm_state_chains` equals
  `NewClass.fsm_state_chains` (and `case_object_type` equals `NewClass.__name__`).
- Loading a record on disk that lacks the field raises (strictness verified).
- A manual-FSM class (empty `fsm_state_chains`, FSM built via `compile_fsm()`) stamps
  `[]` without error.

---

## 7. Files touched

| File | Change |
|---|---|
| `src/totodev_pub/folder_backed_case_support/case_record.py` | add required `fsm_state_chains: list[str]` field + docstring note |
| `src/totodev_pub/folder_backed_case.py` | stamp in `create_case_in_folder()`; re-stamp in `case_reclassify_to()` |
| `tests/test_folder_backed_case.py` | add coverage per §6 |
