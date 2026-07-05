# CaseManager & CaseManagerClient — Specification (v1)

> **Status:** DRAFT — design specification, not production code.
>
> **Supersedes for implementation planning:** `CaseManager Model.md` (v2 outline). That
> document remains useful historical context but must not be treated as a requirements
> checklist for this spec.
>
> **Companion code (already implemented):** `FolderBackedCase`, `FolderBackedCaseReader`,
> `CasePoolDriver` / `TieredCasePoolDriver` / `QueuedCasePoolDriver`,
> `CaseTypeRegistry`, `PoolMembershipJournal`, `restore_pool_from_journal`,
> `CachedFileFolders`.

---

## 1. Purpose

`CaseManager` is the **fleet coordinator** for folder-backed cases on a shared
filesystem. It is the one object an application uses to:

- place new cases on disk and admit them to the live pool,
- **adopt** cases that were created elsewhere and handed off detached,
- find cases (live or archived),
- run the close → archive transition when a case finishes,
- restart safely after a crash,
- expose a cross-process client surface for stateless UI tiers.

It **composes** existing pieces. It does not implement scheduling, FSM behavior,
type resolution, or raw cache mechanics.

`CaseManagerClient` is the **stateless companion** for processes that must not hold
leases or run the driver beat — typically web workers. It reads cases through
`FolderBackedCaseReader` and submits durable requests (fire, adopt) through a
filesystem mailbox the manager owns.

---

## 2. Boundaries

### 2.1 What CaseManager owns

| Responsibility | Notes |
|---|---|
| Storage placement | Which `CachedFileFolders` grouping a case lives in (`open` vs archive buckets) |
| Admission | `create_case`, `adopt_case`, `reopen_case` → `driver.add()` |
| Close-out sequencing | On terminal state: `remove` → `detach` → `move` to archive bucket |
| Startup recovery | Enumerate open bucket, lease-aware pool restore, mailbox replay |
| Cross-process intake | Validate and execute fire/adopt requests from the mailbox |
| Operational telemetry | Emit **escalations** when cases look unhealthy (see §8) |

### 2.2 What CaseManager deliberately does NOT own

| Non-responsibility | Where it lives |
|---|---|
| Scheduling policy (tiers, cadence, choke acquisition order) | `CasePoolDriver` subclass |
| Case FSM, hooks, assets | `FolderBackedCase` subclasses |
| Type name → class resolution | `CaseTypeRegistry` |
| Raw file move/database mechanics | `CachedFileFolders` |
| **Policy for unhealthy cases** | **Application** (via escalation handlers) |

The manager **detects and reports** conditions like repeated transition failures or
long dwell in a blocked state. It does **not** by default halt, quarantine, force-close,
or reassign those cases. Callers register escalation handlers and decide.

### 2.3 One manager per cache (invariant)

Exactly **one** `CaseManager` instance per `CachedFileFolders` root in a running
deployment. `CachedFileFolders` groupings (`open`, `2026-07-archive`, …) are
**buckets inside that cache**, not separate manager domains.

Multiple driver processes against one cache root is out of scope for v1 and remains
a future sharding concern.

---

## 3. Deployment shape

Typical hybrid layout:

```text
┌──────────────────────────────────────────────────────────┐
│  Manager process (asyncio)                               │
│  CaseManager + CasePoolDriver + CachedFileFolders        │
│  await manager.recover(); await manager.start()          │
└──────────────────────────────────────────────────────────┘
                    shared filesystem
┌──────────────────────────────────────────────────────────┐
│  Web tier (N workers, no leases, no driver)              │
│  CaseManagerClient → mailbox (fire, adopt)               │
│  FolderBackedCaseReader → reads                          │
└──────────────────────────────────────────────────────────┘
```

Single-process apps (FastAPI lifespan owns both) may call `manager.fire(...)` directly
and skip the mailbox entirely.

---

## 4. Storage model

### 4.1 Physical layout

Each managed case occupies the **slave directory** of a thin cache entry under a
grouping bucket:

```text
<cache_root>/
  open/
    cases/<case_id>.yaml              # thin cache entry (may be empty placeholder)
    cases/<case_id>.yaml._slave/      # THE CASE FOLDER (record, events, assets, lease)
  closed/
    2026-07/
      cases/<case_id>.yaml
      cases/<case_id>.yaml._slave/
  .case_manager/                      # manager protocol namespace (never a grouping)
    manifest.yaml
    intake/ …
```

**Bucket names are configurable** (§6) but default to `open` for live cases and
`closed/{YYYY-MM}/` for archives.

The placeholder file exists only because `CachedFileFolders` keys entries by
`ref_path`; the case's real payload lives in `._slave/`. The placeholder may hold
a minimal manifest (case_id, external_key) for cheap directory scans but is not
required for correctness — the case record inside the slave dir is authoritative.

### 4.2 Archive bucket selection

When a case closes, the manager moves it from the **open bucket** to an archive
bucket. The destination bucket name comes from `case.archive_grouping_label()` on
the live case object (default on `FolderBackedCase`: current month as
`YYYY-MM`). The manager prefixes the configured **closed root** label
(default `closed`) so the full grouping key becomes e.g. `("closed", "2026-07")`.

Applications needing tenant- or fiscal-period partitioning override
`archive_grouping_label()` on the case class; the manager does not second-guess it.

### 4.3 Manager namespace

Everything under `.case_manager/` belongs to the manager protocol. It must be
**invisible to grouping enumeration** — use a dot-prefix directory directly under
`cache_root`, not under a grouping bucket.

---

## 5. Core lifecycle (manager responsibilities)

### 5.1 Create (manager-native)

```text
manager.create_case(TicketCase, *, case_id=..., external_key=..., **record_fields)
  → allocate cache entry in open bucket (slave dir path)
  → TicketCase.create_case_in_folder(slave_dir, ...)
  → driver.add(case)
  → return case (or a handle / case_id)
```

The manager chooses the slave-dir path; the case class writes the record and logs
inception events.

### 5.2 Adopt (external origin)

Supports the workflow where a case is **created and initially worked outside**
the manager's cache layout — e.g. a UI handler that needs a live lease locally
for a multi-step upload — then **handed off**:

```text
External process:
  case = TicketCase.create_case_in_folder(/tmp/staging/ticket-42, ...)
  … work while lease-held …
  case.case_detach()          # lease cleared; folder is movable

CaseManagerClient (or in-process manager.adopt_case):
  submit_adopt(case_folder=/tmp/staging/ticket-42, correlation_id=...)
```

Manager adopt sequence:

```text
1. Validate source folder:
     - case_record.yaml present
     - no active lease (FolderBackedCase.is_heartbeat_expired → True or lease absent)
     - case type registered (if registry required for this deployment)
2. Allocate destination in open bucket (new cache entry + empty slave dir target)
3. Move (not copy) case folder contents into slave dir
     - prefer atomic rename where source and cache share a filesystem
     - fall back to move-via-temp if cross-device
4. Register cache entry (upsert placeholder + slave dir populated)
5. registry.rehydrate(slave_dir) → driver.add(case)
6. Publish AdoptResult to results/
7. Remove source folder if empty
```

**Idempotency:** an adopt request names a `correlation_id`. If the manager already
published a completed result for that id, the client gets the same outcome. If the
case folder already appears in the open bucket under the same `case_id`, adopt
succeeds as a no-op.

**Reject** (with structured result, no partial move): active lease on source,
duplicate `case_id` already live, unregistered type, malformed record, source path
inside an existing cache slave dir.

### 5.3 Work

The driver advances cases on its beat. Human or UI triggers reach the case via:

- **In-process:** `await manager.fire(case_id=…, trigger=…, **kwargs)`
- **Cross-process:** mailbox fire request (§10)

The manager validates addressing and delegates to `driver.fire()`. It does not
schedule.

### 5.4 Close → archive

When the driver emits `CLOSED`:

```text
driver.remove(folder)     # still lease-held; raises CaseInFlightError if mid-step
case.case_detach()        # lease released — folder now movable
cache.move_file(…open… → …archive…)   # moves entry + slave dir together
```

Archival I/O runs on the manager's **maintenance loop**, not inside the sync event
callback, so the driver beat is not blocked on disk moves.

For a non-closed case that must leave the pool (admin action, Pattern B loan):
`request_halt` → await `HALTED` → same remove/detach/move sequence. v1 does not
require a driver `PARKED` slot; loan/park is deferred.

### 5.5 Reopen (rare)

```text
move archive → open bucket
rehydrate → driver.add()
```

Exposed as `manager.reopen_case(case_id=…)` in-process; cross-process via mailbox
(deferred unless needed).

### 5.6 Recover

On startup, before `start()`:

```text
1. Ensure .case_manager/ layout + write/refresh manifest.yaml
2. Enumerate open bucket → list of case folder paths (slave dirs)
3. Seed PoolMembershipJournal.compact_from_live(paths)  [in-memory or ephemeral journal]
4. await restore_pool_from_journal(driver, journal, attach=False)
5. Replay mailbox: intake + pending (normal); firing (dead-letter per §10)
6. Optional: manager.reap() — idempotent fixups (§7)
```

The **open bucket directory listing** is the durable source of truth for membership.
The journal is a **startup reclaim engine**, not a parallel membership authority, unless
`journal_attach_steady_state=True` (opt-in, §6).

### 5.7 Reap (safety net, not policy)

`reap()` is an idempotent sweep the manager runs on a timer and optionally at
recover. It fixes **mechanical** inconsistencies only:

- closed case still in open bucket → run close-out sequence,
- expired lease on a folder in open bucket with no live driver slot → attempt
  re-admit via rehydrate + add (or escalate if re-admit fails).

`reap()` does **not** interpret business meaning of failures or blocks.

---

## 6. Configuration

All knobs live on a **`CaseManagerConfig`** dataclass. Constructors accept either
a config object or keyword shortcuts that build one.

### 6.1 Storage

| Field | Default | Purpose |
|---|---|---|
| `cache_root` | *(required)* | Absolute path; becomes `CachedFileFolders` root |
| `grouping_pattern` | `"{bucket}/"` | Cache layout pattern |
| `open_bucket` | `"open"` | Grouping key segment for live cases |
| `closed_bucket` | `"closed"` | Prefix segment for archives; combined with `archive_grouping_label()` |
| `case_ref_path_template` | `"cases/{case_id}.yaml"` | ref_path under each bucket |
| `manager_namespace` | `".case_manager"` | Dot-dir under cache_root for protocol files |

### 6.2 Driver

| Field | Default | Purpose |
|---|---|---|
| `driver_class` | `TieredCasePoolDriver` | Constructor for the pool driver |
| `driver_kwargs` | `{}` | Passed to driver constructor |
| `concurrency_ceiling` | *(via driver_kwargs)* | In-flight case cap |
| `choke_limits` | `{}` | Resource limits keyed by choke name |

Common override: `driver_class=QueuedCasePoolDriver` for queue-ordered bursting.

The manager may accept a **pre-built driver instance** instead of a class (tests,
exotic wiring). When a class is given, the manager constructs it.

### 6.3 Registry

| Field | Default | Purpose |
|---|---|---|
| `registry` | `case_type_registry` (module singleton) | Type resolution for rehydrate |
| `register_types` | `()` | Extra case classes to register at init |

If the application already registers types at import time via decorators, pass
nothing. Pass `register_types=(TicketCase, …)` when the manager should own
registration for standalone apps.

### 6.4 Recovery

| Field | Default | Purpose |
|---|---|---|
| `journal_path` | `{manager_namespace}/pool.jsonl` | On-disk journal; `None` = ephemeral |
| `journal_attach_steady_state` | `False` | Keep journal subscribed after recover |
| `lease_reclaim_timings` | module defaults | Tuning for `restore_pool_from_journal` |

### 6.5 Cross-process protocol

| Field | Default | Purpose |
|---|---|---|
| `enable_mailbox` | `True` | File-based intake/results |
| `maintenance_interval_secs` | `1.0` | Mailbox + archival queue poll |
| `result_ttl_secs` | `86400` | TTL for completed request results |
| `manifest_stale_secs` | `30` | Client treats manager as dead if heartbeat older |

Set `enable_mailbox=False` for strictly in-process deployments.

### 6.6 Escalation detection (thresholds only)

These control **when the manager emits escalations**, not what happens afterward.

| Field | Default | Purpose |
|---|---|---|
| `escalation_fail_threshold` | `None` | Emit after N transition fails in current state (`None` = disabled) |
| `escalation_stall_secs` | `None` | Emit when `case_dwell_secs` exceeds this in a non-terminal state |
| `escalation_blocked` | `False` | Emit when driver reports case as blocked (no auto edge available) |

All default to **disabled** or conservative values so a bare `CaseManager.open(root)`
is quiet until the application opts in and registers handlers.

---

## 7. Constructors

Three entry points, shallow to deep.

### 7.1 `CaseManager.open(cache_root, **overrides)` — primary

Opinionated defaults:

- `open_bucket="open"`, `closed_bucket="closed"`, month-based archive sub-buckets
- `TieredCasePoolDriver` with reasonable `concurrency_ceiling`
- module `case_type_registry`
- mailbox enabled
- no escalation thresholds active

```python
manager = CaseManager.open("/data/cases")
manager = CaseManager.open("/data/cases", register_types=[TicketCase])
manager = CaseManager.open("/data/cases", driver_class=QueuedCasePoolDriver)
```

### 7.2 Named presets (convenience)

Optional class methods for recurring deployment shapes:

```python
CaseManager.open_balanced(cache_root, **overrides)   # TieredCasePoolDriver (same as .open)
CaseManager.open_queued(cache_root, **overrides)     # QueuedCasePoolDriver
CaseManager.open_inprocess(cache_root, **overrides)  # enable_mailbox=False
```

Presets are sugar over `.open()` — they set `driver_class` and one or two flags,
not a parallel config system.

### 7.3 `CaseManager(config: CaseManagerConfig, *, driver=…)` — full control

Composition constructor for tests and integrators:

```python
CaseManager(
    config,
    driver=my_driver,           # optional override of config.driver_class
    cache=my_cache,             # optional override — rarely needed
)
```

---

## 8. Escalation model

Unhealthy cases are an **application concern**. The manager provides observability.

### 8.1 Escalation kinds (v1)

| Kind | Typical trigger |
|---|---|
| `REPEATED_FAILURE` | `case_transition_fail_count` crossed threshold |
| `STALLED` | dwell time exceeded threshold in non-terminal state |
| `AUTO_BLOCKED` | driver classifies case as blocked (no auto edge) |
| `REAP_ANOMALY` | reap could not fix a mechanical inconsistency |
| `ADOPT_REJECTED` | adopt intake failed (informational for ops dashboards) |

### 8.2 API

```python
manager.on_escalation(callback: Callable[[CaseEscalation], None]) → handle
manager.off_escalation(handle)
```

`CaseEscalation` carries: `kind`, `case_id`, `case_folder`, `case_state`,
`detail` (structured dict), `observed_at`.

Escalations are **advisory**. The manager continues driving unless the application's
handler calls driver/manager APIs (e.g. `request_halt`, custom admin fire).

### 8.3 Explicit non-goals for v1

The manager will **not** ship built-in quarantine buckets, penalty boxes, or
force-terminal-close policies. Those may be implemented **in application handlers**
using public manager/driver APIs.

---

## 9. CaseManager — runtime API (sketch)

### Lifecycle

```python
await manager.recover() -> RecoverReport
await manager.start()   # driver.start() + maintenance loop
await manager.stop()
```

### Case fleet

```python
await manager.create_case(case_cls, *, case_id, external_key, **fields) -> str  # case_id
await manager.adopt_case(source_folder: Path, *, correlation_id) -> AdoptResult
await manager.reopen_case(case_id: str) -> None

manager.get(case_id: str) -> FolderBackedCase          # live pool only
manager.find(external_key: str) -> list[FolderBackedCase]
manager.reader(case_id: str) -> FolderBackedCaseReader  # live or archive
manager.iter_open() -> Iterator[FolderBackedCase]
manager.iter_closed(*, buckets: ...) -> Iterator[FolderBackedCaseReader]

await manager.fire(case_id, trigger, **trigger_kwargs) -> FireResult
await manager.reap() -> ReapReport
```

`get` raises a typed not-found for archived or unknown ids; `reader` resolves
across open + closed buckets.

---

## 10. Cross-process mailbox

Only used when `enable_mailbox=True`. Polling-based (no inotify) for bind-mount
reliability.

### 10.1 Layout

```text
.case_manager/
  manifest.yaml          # protocol version, paths, manager heartbeat, bucket names
  intake/                # submitters atomic-rename here
    malformed/           # quarantine unparseable
  pending/<case_id>/     # validated, not started
  firing/<case_id>/      # fire only — ambiguous on crash
  results/<correlation_id>.yaml
```

Adopt requests use a separate **`adopt/`** queue (single-phase: no firing ambiguity)
because moving an unregistered folder into place is either done or not:

```text
  adopt/intake/
  adopt/pending/
  adopt/results/
```

### 10.2 Request types

**FireRequest** (YAML, Pydantic v2):

- `protocol_version`, `correlation_id`, `requested_at`
- exactly one of: `case_id`, `external_key`, `case_folder`
- `trigger`, `trigger_kwargs`

**AdoptRequest**:

- `protocol_version`, `correlation_id`, `requested_at`
- `source_folder` (absolute path to detached case)
- optional `expected_case_id` (reject if mismatch — catches UI bugs)

### 10.3 Fire execution flow

```text
intake → validate → pending/<case_id>/ → firing/<case_id>/ → driver.fire()
  → write FireResult → delete request → driver.boost(folder)
```

Crash semantics:

| Queue | On recover |
|---|---|
| `intake/` | process normally |
| `pending/` | process normally |
| `firing/` | dead-letter + error FireResult (at-most-once; client re-reads state) |

### 10.4 Adopt execution flow

```text
adopt/intake → validate source detached → adopt/pending/ → adopt sequence (§5.2)
  → AdoptResult → delete request
```

Pending adopt replays on recover. No `firing/` ratchet — partial moves roll back or
escalate as `REAP_ANOMALY`.

### 10.5 Result types

**FireResult** — sanitized projection of `AdvanceResult` (no live exception objects):
`status: completed | rejected | error`, states, `progressed/blocked/failed`, string
exceptions, timestamps.

**AdoptResult** — `status: completed | rejected | error`, `case_id`, destination
`case_folder`, `source_folder`, timestamps, rejection reason.

---

## 11. CaseManagerClient

Stateless; safe in web workers. Never acquires leases or imports concrete case classes
(for reads).

### 11.1 Construction

```python
client = CaseManagerClient(cache_root)
# or
client = CaseManagerClient.from_manifest(manifest_path)
```

Reads `manifest.yaml` on each operation (or caches with TTL). Fails fast if manager
heartbeat is stale.

### 11.2 Reads

```python
reader = client.reader(case_id=...)           # → FolderBackedCaseReader
summary = client.lookup(external_key=...)   # id, folder, state, closed, bucket
cases = client.list_open()                  # scan open bucket (O(n), consistent)
```

When `journal_attach_steady_state` is enabled and manifest advertises a pool index,
`list_open()` may use the journal file instead of directory scan. Optional optimization.

### 11.3 Writes (mailbox)

```python
handle = client.submit_fire(case_id=..., trigger=..., trigger_kwargs={...})
result = client.poll_result(handle)                  # None until ready
result = await client.wait_result(handle, timeout=30)

handle = client.submit_adopt(source_folder=..., expected_case_id=...)
result = await client.wait_adopt(handle, timeout=60)
```

`RequestHandle` carries `correlation_id` and result path — opaque to callers.

### 11.4 Adopt workflow (UI recipe)

```python
# 1. UI process — staging outside cache layout
staging = Path("/tmp/case-staging") / uuid
case = TicketCase.create_case_in_folder(staging, external_key=form.ticket_no)
try:
    await case.upload_attachment(...)   # needs lease + live object
finally:
    case.case_detach()

# 2. Hand off to manager
handle = client.submit_adopt(source_folder=staging, expected_case_id=case.case_id)
adopt = await client.wait_adopt(handle, timeout=60)
# adopt.case_folder is now under cache_root/open/…/._slave/
```

The UI must detach before adopt. The manager rejects an held lease.

---

## 12. Manifest handshake

Manager writes/refreshes on `recover()` and touches heartbeat each maintenance tick.

```yaml
protocol_version: 1
cache_root: /data/cases
manager_namespace: .case_manager
open_bucket: open
closed_bucket: closed
paths:
  fire_intake: .case_manager/intake
  fire_results: .case_manager/results
  adopt_intake: .case_manager/adopt/intake
  adopt_results: .case_manager/adopt/results
heartbeat_at: "2026-07-05T10:00:00Z"
pool_index: null   # optional path to journal jsonl
```

Clients derive all paths from the manifest — no hardcoded layout in consumer code.

---

## 13. Close-out ordering (invariant)

These constraints come from existing case/driver code and are non-negotiable:

1. Folder is immovable while any object holds the lease.
2. `driver.remove()` returns a still-bound case; raises `CaseInFlightError` if busy.
3. `case_detach()` is the only supported way to release the lease before a move.
4. A `CLOSED` case has already run case-side closing hooks.

Manager sequence: **remove → detach → move**. Never reorder.

---

## 14. Deferred / out of scope (v1)

| Topic | Notes |
|---|---|
| Multi-manager sharding | One manager per cache root |
| Park / loan protocol | Web-side mutation with lease; needs driver `PARKED` slot |
| Cross-process reopen | Mailbox request type |
| Listing-card content | Placeholder file may be empty; no business data required |
| Built-in quarantine / penalty box | Application handlers only |
| HTTP transport for mailbox | Filesystem-first; swap behind client if needed later |
| Case created directly inside cache without manager | Supported via `create_case`; adopt is for **external** paths |

---

## 15. Implementation sequencing

1. **`CaseManagerConfig` + `CaseManager.open()`** with in-process create/get/fire only
   (no mailbox).
2. **Close-out + maintenance loop** (CLOSED handler, archive move).
3. **`recover()` + `reap()`** wired to open-bucket enumeration and
   `restore_pool_from_journal(attach=False)`.
4. **`CaseManagerClient` reads** (manifest + reader/lookup/list_open).
5. **Mailbox fire** + client `submit_fire`.
6. **`adopt_case` + adopt mailbox** + client `submit_adopt`.
7. **Escalation emission** + handler registration.

`FolderBackedCaseReader` already exists — do not block on it.

---

## 16. Open questions

| Question | Lean |
|---|---|
| Move vs copy for cross-device adopt | Move preferred; copy+delete fallback with escalation on failure |
| Placeholder file content | Empty file acceptable for v1; optional minimal yaml later |
| `iter_closed` scope | Default: all closed buckets; filter by month prefix |
| Should `create_case` return case_id or live object? | `case_id` for cross-process symmetry; use `get()` in-process |
| Register types at import vs manager init | Both supported; document that registry is process-local |
