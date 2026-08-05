# Changelog

Notable changes to `totodev-pub`. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [semantic](https://semver.org/spec/v2.0.0.html). Releases before 0.2.0 predate
this file and are not recorded here.

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
