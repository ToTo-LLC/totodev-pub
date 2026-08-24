# Changelog

Notable changes to `totodev-pub`. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [semantic](https://semver.org/spec/v2.0.0.html). Releases before 0.2.0 predate
this file and are not recorded here.

## [Unreleased]

### Changed — BREAKING: on-disk layout cutover

The four per-action mailboxes became one request queue, and the two scratch docks
became one loading dock. **`POLICY_SCHEMA_VERSION` 2 → 3, with no migration.**
Layout is immutable per filespace, so a filespace created at schema 2 cannot be
opened by this version — a deliberate one-time break taken while the layout was
not yet depended on in production. Recreate the filespace.

- **`requests/` replaces `fire_mailbox/`, `adopt_mailbox/`, `reclassify_mailbox/`
  and `shutdown_mailbox/`.** The action moved out of the folder path and into the
  message's `op` field, so adding one costs a message type and a handler rather
  than an edit to four files. Stages are `queued/`, `claimed/{case_id}/`,
  `running/{case_id}/`, `failed/`, `results/`, plus a `shutdown/` leaf.
- **One word per state.** Three words meant "in progress" (`firing`, `executing`,
  and adopt's `pending`) while fire's `pending` meant "accepted, not started" — so
  `pending` denoted two opposite things in two sibling mailboxes.
- **`incoming/` replaces `staging/` and `adopt_drop/`.** Two docks meant two
  cleaner rules with no stated rule for choosing between them, and `adopt_drop`
  was named after one of its readers rather than its contents. One rule now:
  reclaimed only when old, unleased, **and** carrying no `.ready` marker.
- **`incoming/` is drained every tick.** Its predecessor scanned once at startup,
  behind an off-by-default flag, so a folder dropped by another process waited for
  the next restart.
- The manager namespace is **17 directories, down from 24**.

### Changed — API

- `Policy`: `requests_subdir` / `incoming_subdir` replace the four `*_mailbox_subdir`
  fields plus `staging_subdir` and `adopt_drop_subdir`. `staging_min_age_secs` →
  `incoming_min_age_secs`, `staging_stale_lease_secs` → `incoming_stale_lease_secs`,
  `startup_adopt_scan` → `startup_incoming_scan`.
- `allocate_staging_folder()` → `allocate_incoming_folder()`, on both `CaseManager`
  and `CaseManagerClient`.
- `FireRequest` / `AdoptRequest` / `ReclassifyRequest` → one `RequestEnvelope`
  carrying `op` plus a typed payload (`FirePayload`, `AdoptPayload`,
  `ReclassifyPayload`). `MAILBOX_PROTOCOL_VERSION` 1 → 2.
- `MailboxTransport`: `queued()` / `claimed()` / `running()` / `failed()` replace the
  per-mailbox intake and stage accessors.
- `ManifestPaths` publishes `requests_queued`, `requests_claimed`, `requests_running`,
  `requests_failed`, `shutdown_intake` and `incoming` in place of the four intakes,
  `staging` and `adopt_drop`. `MANIFEST_PROTOCOL_VERSION` 1 → 2.
- `AdapterRecoverReport` gains `requeued`; requeueing from `claimed/` is now
  op-agnostic, since claiming happens before any work touches a case.

### Behaviour changes worth knowing

- **Drain order is submission order.** It was fire → reclassify → adopt within a
  tick, which meant a fire co-submitted with an adopt was processed *before* the
  adopt that would have pooled its case. One FIFO queue removes that trap.
- **A request too broken to name its own `op` gets a generic error result.** With
  the action in the folder path, even unparseable garbage could be answered in the
  right shape; now the action lives in the file. A body that still parses as YAML
  keeps its op and its result type. Either way the submitter gets a terminal
  error rather than silence, which is the property that actually matters.

### Added

- `docs/case-manager-layout.md` — a generated map of the whole on-disk layout,
  rendered from the same declaration the manager provisions from
  (`case_manager_support/namespace_map.py`), with tests pinning declaration,
  disk and document together.

## [0.2.1] - 2026-08-15

### Added

- **`CaseManager.export_case(case_id, *, dest)`** — remove a terminated or quarantined
  case from managed storage. Pass a path to keep the folder; pass `dest=None` to
  destroy it (`dest` is required as a keyword so omission cannot silently delete).
  Live cases still use `eject_from_pool`. Combined with `iter_terminal(before=...)`
  / `iter_quarantine(before=...)`, this is the host-side recipe for retiring old
  archives. Distinct from redundant purge, which only strips ephemeral files
  inside a finished folder.

## [0.2.0] - 2026-08-04

Introduces `CaseManager`, the supervision layer for the `FolderBackedCase` family.

### Added

- **`CaseManager`** — supervises a fleet of `FolderBackedCase` work items over a shared
  cache root: admission, scheduling, quarantine, termination, and crash recovery.
- **Deployment tier** for running a fleet as a real process — a host (`serve()`) owning
  signals and exit codes, a watchdog, a file-drop request transport for out-of-process
  clients, and a fleet-status board for observability. See
  [`docs/case-manager-deployment.md`](docs/case-manager-deployment.md).
- **Pool drivers** — pluggable scheduling policy, defaulting to seniority-aware
  multi-level feedback queueing.
- **`totodev-manager-health`** console script: a dependency-light health probe for
  container liveness checks.
- `ReclassifyAssertionError`, raised when a reclassify commits but the new class's
  assertions fail the post-commit sweep.

### Changed

- **Breaking:** `FolderBackedCaseReader` moved from `totodev_pub.folder_backed_case_reader`
  to `totodev_pub.folder_backed_case_support.folder_backed_case_reader`. No compatibility
  shim — update imports.
- `FolderBackedCase.case_detach()` now returns the case folder `Path` instead of `None`,
  so create → detach → hand-off can be written fluently.
- Assertion sweeps now return a result object describing what ran and what failed, rather
  than reporting only through the journal.

### Removed

- **Breaking:** `FolderBackedCase.archive_grouping_label()`. It was never wired to an
  archiving implementation. Note that an existing override becomes dead code **silently** —
  no error is raised — so remove any override deliberately.

### Fixed

- Small files written by `FileMappedPydanticMixin` now use write-then-rename, so a
  concurrent reader can no longer observe a half-written file. This matters for state
  rewritten on a sub-second cadence and read from other processes.
