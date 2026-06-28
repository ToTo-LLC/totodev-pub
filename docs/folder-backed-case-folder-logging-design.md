# FolderBackedCase Per-Case Folder Logging

Design spec — 2026-06-28

## Scope and Intent

`FolderBackedCase` instances currently log through a single module-level
`logger = logging.getLogger(__name__)`. In a pool holding many thousands of
live case objects, a stack trace or warning in the shared application log gives
no reliable way to tell *which* in-memory case produced it.

This feature adds an **unconditional tee**: every case instance also writes its
log records to a file inside its own case folder (`logs/`). The application's
normal/default logging is unchanged; the per-case file is a parallel copy whose
sole purpose is developer post-mortem debugging — slow, after-the-fact
inspection, not realtime/automated consumption.

### Goals

- Per-case log capture that is **always on**, with no per-object enable knob.
- "Typical logger semantics": a derived-class developer uses an ordinary
  `logging.Logger` and does not have to reason about handlers, files, or paths.
- A clear, coarse-grained retention policy at case closure, defaulting to a
  privacy-conscious purge, with a single global override for dev/test.

### Non-Goals

- Capturing log records emitted through unrelated module loggers or third-party
  libraries (no global interception / contextvar routing). Only records sent to
  the case's own logger are teed.
- Per-instance retention configuration. The retention switch is process-global.
- Realtime log shipping, rotation by size/time, or structured-log transport.

## 1) Capture Mechanism — Per-Instance Logger (Registry-Free)

Each live case owns a logger exposed as `self.log`, built so that it leaks
nothing process-wide:

```
# Constructed DIRECTLY (not via getLogger) so it never enters the global
# logger registry and is garbage-collected when the case object is dropped.
self.log = logging.Logger(f"totodev_pub.case.{self.case_id}")
self.log.parent = logging.getLogger("totodev_pub.case")  # chains to root
self.log.propagate = True
```

- `self.log` is an ordinary `logging.Logger`. Derived classes call
  `self.log.info(...)`, `self.log.exception(...)`, etc. Nothing else is required
  of them.
- Because the logger **propagates** through its manually-set parent to the root
  logger, records flow to the application's existing handlers exactly as before
  — this is the "default logging" half of the tee.
- Constructing the `Logger` directly is deliberate: `logging.getLogger(name)`
  permanently registers each named logger in a global dict that is never
  evicted, so a process that churns thousands of cases would accumulate dead
  `Logger` objects forever. A directly-constructed logger is owned solely by the
  case instance and collected with it.
- A per-instance **close-after-write handler** (see §11, Resource Management) is
  the per-case-file half of the tee. Propagate + that handler = tee.

The base class routes its own instance-context messages through `self.log` so
framework-level events land in the per-case file too. Specifically, the
`_on_fsm_exception` handler's `logger.exception(...)` becomes
`self.log.exception(...)`. Class-level messages that have no instance (e.g. the
`__init_subclass__` FSM-compilation debug line) stay on the module logger.

A `logging.Filter`/`Formatter` stamps each record with `case_id`,
`case_object_type`, and current `case_state`, so the file is self-identifying.

### Why not capture everything?

A contextvar-routed handler on the root logger could capture *all* logging
(including third-party) during a case's active span. It was rejected: it does
not obey typical logger semantics, it is fragile under concurrent async cases,
and it produces surprising attribution. The per-instance logger is the
recommended sweet spot; a "capture everything" mode can be added later as an
explicit opt-in without disturbing this design.

## 2) On-Disk Layout

A new directory at the **case root**, alongside `events/`, `assets/`, etc.:

```
<case_folder>/logs/case.log
```

- `LOGS_DIR_NAME = "logs"` is added to `constants.py` and to
  `CASE_RESERVED_ARTIFACT_NAMES`, so `create_case_in_folder()` continues to
  reject a non-clean target folder.
- It lives outside `assets/`, so `CaseAssets.list_assets()` never reports it and
  `purge_ephemeral()` never touches it.

## 3) File Granularity — Single Appended File

One file, `logs/case.log`, appended across the case's entire life (including
multiple attach/detach sessions). Each attach writes a banner line so episodes
are visually separable within the single file, e.g.:

```
--- attached 2026-06-28T16:57:03Z (pid 12345) ---
```

A single file is the simplest clean target for the truncate-at-close purge
(§5) and avoids accumulating many timestamped files for long-lived cases.

## 4) Lifecycle Integration

### Setup — at bind, not at `__init__`

The logger and its close-after-write handler are created in
`_bind_existing_case_dir(...)`, the shared worker behind `__init__`,
`rehydrate()`, and `case_reclassify_to()`. This guarantees every path that
produces a live, folder-bound instance gets the tee (reclassify uses `__new__` +
`_bind_existing_case_dir`, so wiring it in `__init__` alone would miss it).
Because the handler holds no open descriptor, "creating" it allocates only a
small in-memory object.

### Purge — at FSM close

Log purging is a **policy action** performed at FSM close, inside
`_on_state_changed` phase 2, immediately alongside `assets.purge_ephemeral()`.
The case logically *ends* at `is_closed()`; this is the natural finalization
point, and co-locating it with asset purge keeps all closure-time cleanup in one
place.

### Teardown — at detach

`case_detach()` needs no logging-specific teardown. Because the close-after-write
handler holds no open descriptor between records, there is no file to close; the
per-instance logger and handler are simply released by GC when the case object
is dropped. Detach performs **no purge and deletes nothing**. Detaching ends the
in-memory handle, not the case, and is irrelevant to log retention.

## 5) Purge Semantics — Truncate, Don't Delete

"Purging" the log does **not** unlink the file or remove `logs/`. Instead, purge
**rewrites the file with a single sentinel line**:

```
Log auto-truncated by FolderBackedCase closure policy.
```

Because the close-after-write handler holds no open descriptor (§11), this is a
plain `open(path, "w")` of the sentinel — no live-handle coordination, no
Windows lock concern. Any records that arrive after closure simply append below
the sentinel on their next write — acceptable by design. `logs/` and `case.log`
always continue to exist, keeping the folder layout stable.

## 6) Retention Policy — Global, Default Purge

Retention is a **process-global** setting (not per-object), defaulting to purge:

```python
class LogRetention(enum.Enum):
    PURGE = "purge"     # default: truncate-and-stamp at close
    RETAIN = "retain"   # keep full contents (dev/test)
```

The switch is exposed as a **module-level config function** — discoverable but
deliberately out-of-band, signalling that it is a developer-debugging override
rather than mainstream API:

```python
from totodev_pub.folder_backed_case_support import set_case_log_retention
set_case_log_retention(LogRetention.RETAIN)   # e.g. in dev/test bootstrap
```

The default is `PURGE` for production privacy/security posture. Dev and test
environments call the setter once at startup to suspend purging globally.

Rationale for default-purge: while a closed case stays bound, its folder is
still owned/in-use, so retaining logs buys no realtime value (the use case is
slow developer inspection). A privacy-conscious default that a developer can
trivially suspend globally is the better trade-off.

## 7) Developer-Preservation Workaround

If specific cases must keep their logs under a global `PURGE` policy, the
existing `on_closing()` hook (phase 1, before purge) is the natural seam: a
subclass copies/harvests `logs/case.log` to a durable location before the
phase-2 truncation runs. No new API is required for this.

## 8) Edge Cases

- **Reclassify:** the old and fresh instances each hold their own
  directly-constructed `Logger` and handler (no shared registry entry, so no
  shared object to disentangle). Both append to the same `logs/case.log`; the
  banner line marks the new episode. Single-file append tolerates this cleanly.
- **`peek_case_record()`:** does not construct a live instance, so no logger and
  no file are created.
- **Concurrency:** `logging` acquires the handler lock around each `emit`, so
  the open-write-close cycle is serialized per handler; a single appended file
  per case avoids cross-file interleaving concerns. Cross-process contention is
  precluded by the single-owner lease.
- **Crash before close:** the file simply survives un-truncated; a later owner
  can read it. (A future cleanup sweep, if desired, is out of scope here.)

## 9) Testing Plan (pytest, under `tests/`)

- Tee: a record sent to `self.log` appears both via a root-attached caplog
  handler and in `logs/case.log`.
- Always-on: `create_case_in_folder`, `rehydrate`, and reclassify each produce a
  populated `logs/case.log`.
- Reserved artifact: `create_case_in_folder` rejects a folder pre-seeded with a
  `logs/` directory.
- Purge default: after reaching a closed state, `case.log` contains only the
  sentinel line (plus possibly trailing post-close records).
- Retain override: with `set_case_log_retention(RETAIN)`, the full contents
  survive close.
- Layout isolation: `logs/` never appears in `assets.list_assets()` and is
  untouched by `purge_ephemeral()`.
- Detach: dropping the handler at detach performs no purge.
- Resource frugality: after logging from many cases (e.g. a few hundred), the
  process holds no persistent open descriptor for `case.log` (assert open-fd
  count does not grow with the number of live cases).
- No registry leak: per-case loggers do not accumulate in
  `logging.Logger.manager.loggerDict` after cases are dropped.

## 10) Touched Surfaces (Summary)

- `constants.py`: add `LOGS_DIR_NAME` and extend `CASE_RESERVED_ARTIFACT_NAMES`.
- `folder_backed_case_support/`: new `LogRetention` enum + module-level
  `set_case_log_retention()` / current-policy accessor; new
  `_CaseFileLogHandler` (close-after-write) and a helper that builds the
  directly-constructed per-instance logger + handler and the rewrite-with-
  sentinel purge.
- `folder_backed_case.py`: create `self.log` in `_bind_existing_case_dir` (after
  the lease is acquired, so we never write into a folder owned by another
  process); write the attach banner; invoke log purge in `_on_state_changed`
  phase 2 next to `purge_ephemeral()`; route `_on_fsm_exception`'s message
  through `self.log`. No change to `case_detach()` (no descriptor to release).

## 11) Resource Management — File Handles & Logger Lifecycle

This facility runs for *every* live case unconditionally, so it must consume
resources modestly even with many hundreds of concurrent cases on a developer
machine whose `ulimit -n` may be as low as 256. The design must not assume a
generous descriptor budget.

### Close-after-write handler (no persistent fd)

The standard `logging.FileHandler` opens its file on construction and keeps the
descriptor open for the handler's entire life — that would mean **one open fd
per live case**, and a few hundred cases could exhaust a 256-descriptor limit
and break unrelated file I/O. `FileHandler(delay=True)` only defers the first
open; once a case logs, the fd stays open.

Instead, a small custom `_CaseFileLogHandler(logging.Handler)` overrides
`emit()` to: format the record, `open(self.path, "a", encoding="utf-8")`, write
one line, and close — under the handler lock that `logging` already holds.

Resource profile for N live cases:

- **Open fds at rest: 0.** Transient during a write: 1 (per emitting case).
- **Memory:** one small `Logger` + one handler + formatter per live case,
  released by GC when the case object is dropped.
- **No process-global growth:** the logger is constructed directly (§1), so the
  global registry never grows.

### Cost trade-off

Open/close per record adds syscalls versus a held-open handler. For
diagnostic-volume logging this is negligible, and it is the correct trade: bound
descriptors beat saving a few microseconds per line. If a future hot path makes
this measurable, a bounded buffer (flush every K records or T seconds, flush on
close/purge) can be added behind the same handler without changing the public
surface. Not built now (YAGNI).
