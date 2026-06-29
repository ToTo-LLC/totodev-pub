# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""PoolMembershipJournal — durable, append-only record of pool membership for crash recovery.

A ``CasePoolDriver`` is an in-memory scheduling layer: when the process dies, the knowledge
of *which* case folders this driver was responsible for dies with it. The folders themselves
are durable (record + lease + event log all live on disk), but nothing on disk says "this
particular driver/shard owned these folders" — a bare disk scan cannot tell which driver held
a case, and leases expire without recording assignment. This journal is that durable
assignment: a small jsonl of pointers (folder paths only) so the pool can be rebuilt after a
crash (design doc Section 8b).

It is a pure OBSERVER. It owns no driver behaviour and the driver knows nothing about it; the
two are wired together by app-setup. The journal rides the driver's existing event stream
(``ADMITTED`` -> ``add`` record, ``REMOVED`` / ``EVICTED`` -> ``remove`` record) and never
modifies the driver or its base class.

ON-DISK FORMAT
--------------
Append-only jsonl, one record per line: ``{"op": "add"|"remove", "path": <str>, "ts": <float>}``.
Only the path is stored; ``case_type_registry.rehydrate(path)`` reads the class from the
folder's own record on replay, and everything else is already durable in the folder. A torn
final line (a crash mid-append) is tolerated on read.

RECOVERY
--------
``rebuild(add_fn)`` replays the journal down to the current membership (paths whose most recent
op is ``add``) and reconciles each path against reality — the lease is the real arbiter:

- rehydrate succeeds (open OR closed) -> hand the live case to ``add_fn`` (the driver admits a
  closed case as dormant on its own);
- folder gone (``FileNotFoundError``) -> drop;
- owned elsewhere now (``CaseAlreadyOpenError``) -> drop (see fast-restart note below);
- type unreadable / unregistered / changed (``ValueError`` / ``UnregisteredCaseTypeError`` /
  ``CaseTypeMismatchError``) -> drop.

``rebuild`` is the simple, lease-naive entry point. For a real fast-restart (a fresh pool
starting before the dead pool's leases have expired) use ``restore_pool_from_journal``, the
lease-aware wiring helper below: it inspects every lease file FIRST and only instantiates cases
once it has confirmed no live owner is renewing them (Phase 1 gate), then reclaims frozen leases
incrementally as they lapse (Phase 2). A genuinely live competitor aborts the restart with a
clear ``PoolRestartConflictError`` rather than risking a split-brain. All of this fast-restart
policy lives in the wiring helper, never in the driver.

COMPACTION
----------
The live set is tracked in memory as events arrive, so the file can be rewritten from it via a
temp-file + atomic rename. Compaction runs automatically every ``compaction_threshold``
removals and once at the end of a rebuild.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Hashable

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_pool_driver import (
    CasePoolDriver, CasePoolEvent, CasePoolEventNames,
)
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry, case_type_registry,
)
from totodev_pub.folder_backed_case_support.constants import (
    DEFAULT_LEASE_TTL_SECS, LEASE_HEARTBEAT_THROTTLE_SECS, LEASE_NAME,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError, CaseTypeMismatchError, UnregisteredCaseTypeError,
)

logger = logging.getLogger(__name__)

# Outcomes of reconciling one journaled path against the folder on disk.
_OK = "ok"                  # rehydrated live (open or closed) -> re-add
_MISSING = "missing"        # folder/record gone -> drop
_OWNED = "owned_elsewhere"  # a live lease holds it (possibly our own, pre-expiry) -> drop
_BAD_TYPE = "bad_type"      # type unreadable / unregistered / changed -> drop

_DEFAULT_COMPACTION_THRESHOLD = 500


@dataclass
class DroppedMember:
    """Why one journaled path could not be recovered. ``reason`` is one of the ``_MISSING`` /
    ``_OWNED`` / ``_BAD_TYPE`` category constants; ``error_type`` and ``detail`` carry the exact
    exception that triggered the drop, so a jammed restart can be diagnosed precisely (a bad
    type vs. an unregistered class vs. an unreadable record all land here distinctly)."""
    path: Path
    reason: str
    error_type: str
    detail: str


@dataclass
class RebuildReport:
    """Outcome of a ``rebuild`` / ``reconcile`` pass, partitioned by what happened to each
    journaled path. ``dropped_owned_elsewhere`` is the subset the wiring helper retries after a
    TTL back-off (those may be this process's own not-yet-expired leases).

    The ``dropped_*`` lists are the quick path-only buckets; ``failures`` carries the SAME drops
    enriched with the triggering exception (type + message) for diagnosing a stuck restart."""
    readded: list[Path] = field(default_factory=list)
    dropped_missing: list[Path] = field(default_factory=list)
    dropped_owned_elsewhere: list[Path] = field(default_factory=list)
    dropped_bad_type: list[Path] = field(default_factory=list)
    failures: list[DroppedMember] = field(default_factory=list)

    @property
    def dropped(self) -> list[Path]:
        """Every path that was NOT re-added, across all drop reasons."""
        return (
            self.dropped_missing
            + self.dropped_owned_elsewhere
            + self.dropped_bad_type
        )


class PoolMembershipJournal:
    """An append-only jsonl of pool membership (folder paths) that can rebuild a driver's
    membership after a crash. Pure observer: it subscribes to a driver's membership events and
    never drives or mutates the driver."""

    def __init__(
        self,
        journal_path: str | os.PathLike[str],
        *,
        compaction_threshold: int = _DEFAULT_COMPACTION_THRESHOLD,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(journal_path)
        self._compaction_threshold = compaction_threshold
        self._clock = clock

        # In-memory mirror of the live membership, so compaction can rewrite the file from it
        # without re-reading. Kept in sync by every add/remove the journal records.
        self._live: set[str] = set()
        self._removals_since_compaction = 0

        # driver-id -> (driver, subscription handle) for attach/detach symmetry.
        self._subscriptions: dict[int, tuple[CasePoolDriver, Hashable]] = {}

    # -- Properties --------------------------------------------------------

    @property
    def path(self) -> Path:
        """The journal file path."""
        return self._path

    def live_paths(self) -> set[Path]:
        """A copy of the currently-journaled live membership (folder paths)."""
        return {Path(p) for p in self._live}

    # -- Driver wiring (observer attach/detach) ----------------------------

    def attach(self, driver: CasePoolDriver) -> Hashable:
        """Subscribe to a driver's membership events so admissions/removals are journaled.

        Records an ``add`` on ``ADMITTED`` and a ``remove`` on ``REMOVED`` / ``EVICTED``.
        Returns the subscription handle (also retained for ``detach``)."""
        handle = driver.case_event_subscribe(
            {
                CasePoolEventNames.ADMITTED,
                CasePoolEventNames.REMOVED,
                CasePoolEventNames.EVICTED,
            },
            self._on_pool_event,
        )
        self._subscriptions[id(driver)] = (driver, handle)
        return handle

    def detach(self, driver: CasePoolDriver) -> None:
        """Unsubscribe from a previously-attached driver. No-op if not attached."""
        entry = self._subscriptions.pop(id(driver), None)
        if entry is not None:
            attached_driver, handle = entry
            attached_driver.case_event_unsubscribe(handle)

    def _on_pool_event(self, event: CasePoolEvent) -> None:
        if event.event == CasePoolEventNames.ADMITTED:
            self._log_add(event.case.case_folder)
        elif event.event in (CasePoolEventNames.REMOVED, CasePoolEventNames.EVICTED):
            self._log_remove(event.case.case_folder)

    # -- Recovery (replay + reconcile) -------------------------------------

    def rebuild(
        self,
        add_fn: Callable[[FolderBackedCase], None],
        *,
        registry: CaseTypeRegistry = case_type_registry,
    ) -> RebuildReport:
        """Replay the on-disk journal to its current membership and re-admit each live case.

        For every path whose most recent op is ``add``, reconcile against the folder on disk
        (see module docstring) and, when it rehydrates, hand the fresh live case to ``add_fn``
        (typically ``driver.add``). Afterwards the in-memory live set is reset to exactly the
        re-added paths and the file is compacted to match, so the journal is clean and
        authoritative regardless of whether the journal was already attached.

        Returns a ``RebuildReport`` partitioning every path by outcome.
        """
        members = self._read_members()
        report = self._reconcile_paths(members, add_fn, registry)
        self._live = {str(p) for p in report.readded}
        self._compact()
        return report

    def reconcile(
        self,
        paths: list[Path],
        add_fn: Callable[[FolderBackedCase], None],
        *,
        registry: CaseTypeRegistry = case_type_registry,
    ) -> RebuildReport:
        """Reconcile an explicit list of paths and re-admit any that rehydrate.

        Used by the wiring helper to retry the ``dropped_owned_elsewhere`` subset after a TTL
        back-off. Re-added paths are merged into the live set and the file is compacted."""
        report = self._reconcile_paths(paths, add_fn, registry)
        self._live |= {str(p) for p in report.readded}
        self._compact()
        return report

    def _reconcile_paths(
        self,
        paths: list[Path],
        add_fn: Callable[[FolderBackedCase], None],
        registry: CaseTypeRegistry,
    ) -> RebuildReport:
        report = RebuildReport()
        for raw_path in paths:
            path = Path(raw_path)
            outcome, case, error = self._classify(path, registry)
            if outcome == _OK and case is not None:
                add_fn(case)
                report.readded.append(path)
                continue
            if outcome == _MISSING:
                report.dropped_missing.append(path)
            elif outcome == _OWNED:
                report.dropped_owned_elsewhere.append(path)
            else:
                report.dropped_bad_type.append(path)
            detail = str(error) if error is not None else ""
            error_type = type(error).__name__ if error is not None else ""
            report.failures.append(
                DroppedMember(path=path, reason=outcome, error_type=error_type, detail=detail)
            )
            logger.warning(
                "PoolMembershipJournal could not recover %s [%s] %s: %s",
                path, outcome, error_type, detail,
            )
        return report

    @staticmethod
    def _classify(
        path: Path, registry: CaseTypeRegistry
    ) -> tuple[str, FolderBackedCase | None, BaseException | None]:
        """Reconcile a single journaled path against the folder on disk. The lease is the
        arbiter: a successful rehydrate means we (re)acquired ownership. On a drop, the
        triggering exception is returned alongside the category so callers can see WHY."""
        try:
            case = registry.rehydrate(Path(path))
        except FileNotFoundError as err:
            return _MISSING, None, err
        except CaseAlreadyOpenError as err:
            return _OWNED, None, err
        except (CaseTypeMismatchError, UnregisteredCaseTypeError, ValueError) as err:
            return _BAD_TYPE, None, err
        return _OK, case, None

    # -- Reading the journal ----------------------------------------------

    def _read_members(self) -> list[Path]:
        """Replay the file to the set of paths whose most recent op is ``add``, preserving
        first-seen order. A torn final line (crash mid-append) is tolerated; a malformed line
        anywhere else is a real corruption and raised."""
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        last_op: dict[str, str] = {}
        order: list[str] = []
        for index, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    # Only the very last line may be torn by a crash mid-append.
                    logger.warning(
                        "PoolMembershipJournal tolerating torn final line in %s", self._path
                    )
                    continue
                raise
            op = record.get("op")
            path = record.get("path")
            if op not in ("add", "remove") or not isinstance(path, str):
                continue
            if path not in last_op:
                order.append(path)
            last_op[path] = op
        return [Path(p) for p in order if last_op[p] == "add"]

    def members(self) -> list[Path]:
        """The current journaled membership (paths whose most recent op is ``add``), replayed
        from disk. The starting point for a lease-aware restore that wants to inspect leases
        before instantiating anything."""
        return self._read_members()

    # -- Writing the journal ----------------------------------------------

    def _log_add(self, folder: Path) -> None:
        path = str(folder)
        self._append_record("add", path)
        self._live.add(path)

    def _log_remove(self, folder: Path) -> None:
        path = str(folder)
        self._append_record("remove", path)
        self._live.discard(path)
        self._removals_since_compaction += 1
        if self._removals_since_compaction >= self._compaction_threshold:
            self._compact()

    def _append_record(self, op: str, path: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"op": op, "path": path, "ts": self._clock()})
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def compact_from_live(self, paths: list[Path] | set[Path]) -> None:
        """Replace the journal's contents with an ``add`` record per supplied live path,
        written atomically. Resets the in-memory live set to match. Useful when an external
        owner of the authoritative live set (e.g. the driver) wants to force a clean rewrite."""
        self._live = {str(p) for p in paths}
        self._compact()

    def _compact(self) -> None:
        """Rewrite the journal from the in-memory live set via temp-file + atomic rename, so a
        crash mid-compaction leaves either the old file or the new one, never a partial."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for path in sorted(self._live):
                handle.write(json.dumps({"op": "add", "path": path, "ts": self._clock()}) + "\n")
        os.replace(tmp_path, self._path)
        self._removals_since_compaction = 0


# ---------------------------------------------------------------------------
# Lease-aware fast-restart recovery (the wiring seam; driver stays ignorant)
# ---------------------------------------------------------------------------
#
# The crash-recovery problem the journal cannot solve alone: a fresh pool may start before the
# dead pool's leases have expired. Every still-held lease then looks identical to a live
# competitor's — and you cannot tell them apart from a single look. The discriminator is
# MOVEMENT over time: a live owner re-stamps each lease's "valid-until" mtime within the TTL,
# while a dead owner's leases sit frozen and count down. So recovery runs in two phases:
#
#   Phase 1 (gate, instantiates NOTHING): stat every member's lease file, then look again after
#       one beat window. If ANY held lease advanced, a live owner exists -> abort, having taken
#       nothing (this is what prevents a half-and-half split-brain). If all held leases are
#       frozen, the old owner is believed dead.
#   Phase 2 (reclaim, incremental): acquire the already-free cases now and poll the frozen ones,
#       admitting each as its lease lapses, up to a TTL-bounded deadline. Instantiating a case
#       secures its lease (the constructor calls acquire()), so a successful add IS the claim.
#       Belt-and-suspenders: if a frozen lease springs back to life mid-reclaim (an owner that
#       stalled past its own TTL and resumed — already a contract violation it must self-detect
#       via OwnershipLostError), it is surfaced as a conflict rather than fought over.


@dataclass
class LeaseReclaimTimings:
    """Tunable windows for the lease-aware restore. Defaults are derived from the fixed lease
    timing (``constants.py``): the freeze-observation window must exceed one heartbeat period so
    a live owner is reliably caught beating, and the deadline must exceed one TTL so a genuinely
    frozen lease is guaranteed to lapse within it."""
    freeze_observe_secs: float = LEASE_HEARTBEAT_THROTTLE_SECS + 5.0   # > one beat period (~15s)
    poll_secs: float = 2.0
    deadline_margin_secs: float = 3.0
    max_total_secs: float = DEFAULT_LEASE_TTL_SECS * 2.0               # hard ceiling on the wait


@dataclass
class LeaseConflict:
    """One case folder that could not be reclaimed because a live owner holds its lease.
    ``kind`` is ``"live"`` (lease observed advancing) or ``"timeout"`` (still held when the
    deadline elapsed). ``remaining_secs`` is the lease's remaining validity at detection."""
    path: Path
    remaining_secs: float
    kind: str


class PoolRestartConflictError(Exception):
    """Raised by ``restore_pool_from_journal`` when one or more case folders are still held by a
    live owner — i.e. another pool instance is almost certainly running on them. Carries the
    conflicting folders (with remaining lease seconds) and whatever WAS recovered before the
    conflict was detected, so an operator can act with full context instead of a bare abort."""
    def __init__(
        self,
        conflicts: list[LeaseConflict],
        *,
        recovered: list[Path],
        elapsed_secs: float,
    ) -> None:
        self.conflicts = conflicts
        self.recovered = recovered
        self.elapsed_secs = elapsed_secs
        detail = "\n".join(
            f"  - {c.path}: ~{c.remaining_secs:.0f}s lease remaining ({c.kind})"
            for c in conflicts
        )
        super().__init__(
            f"Pool restart aborted: {len(conflicts)} case folder(s) are still held by a live "
            f"owner after {elapsed_secs:.0f}s — another pool instance is likely running on "
            f"them. Waiting out the lease TTL did not free them.\n{detail}\n"
            f"Recovered {len(recovered)} case(s) before the conflict was detected."
        )


def _lease_valid_until(folder: Path) -> float | None:
    """The lease's 'valid-until' wall-clock time (its file mtime), or None if no lease file
    exists. Pure stat — never constructs a case or touches the lease."""
    try:
        return (Path(folder) / LEASE_NAME).stat().st_mtime
    except FileNotFoundError:
        return None


async def restore_pool_from_journal(
    driver: CasePoolDriver,
    journal: PoolMembershipJournal,
    *,
    registry: CaseTypeRegistry = case_type_registry,
    timings: LeaseReclaimTimings | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    attach: bool = True,
) -> RebuildReport:
    """Recover a pool from its journal at startup, lease-aware, then attach for steady state.

    Two phases (see the section comment above): a liveness GATE that instantiates nothing and
    aborts wholesale if any held lease is actively being renewed (a live competitor), followed
    by an INCREMENTAL reclaim that admits each surviving case as its frozen lease lapses, up to
    a TTL-bounded deadline. Missing folders and unreadable/unregistered types are benign partial
    drops recorded in the returned ``RebuildReport``; a live competitor (or a lease that never
    frees by the deadline) raises ``PoolRestartConflictError``.

    Keeps all fast-restart policy OUT of the driver and the journal: the driver is only ever
    touched through ``driver.add`` and (optionally) ``journal.attach(driver)``.

    ``clock`` and ``sleep`` are injectable for deterministic tests; ``timings`` tunes the
    observation/deadline windows.
    """
    timings = timings or LeaseReclaimTimings()
    started_at = clock()
    members = journal.members()

    # --- Phase 1: liveness gate (no instantiation) ---
    baseline: dict[Path, float | None] = {p: _lease_valid_until(p) for p in members}
    held = [p for p in members if baseline[p] is not None and baseline[p] > clock()]
    if held:
        await sleep(timings.freeze_observe_secs)
        live = [
            p for p in held
            if (vu := _lease_valid_until(p)) is not None
            and baseline[p] is not None
            and vu > baseline[p]
        ]
        if live:
            conflicts = [
                LeaseConflict(
                    path=p,
                    remaining_secs=max(0.0, (_lease_valid_until(p) or clock()) - clock()),
                    kind="live",
                )
                for p in live
            ]
            logger.error(
                "Pool restart conflict: %d lease(s) actively renewed by a live owner; "
                "taking nothing.", len(conflicts),
            )
            raise PoolRestartConflictError(
                conflicts, recovered=[], elapsed_secs=clock() - started_at
            )

    # --- Phase 2: incremental reclaim (no live owner detected) ---
    report = RebuildReport()
    conflicts: list[LeaseConflict] = []
    deadline = clock() + timings.max_total_secs
    pending = list(members)
    while pending and clock() < deadline:
        progressed = False
        still: list[Path] = []
        for path in pending:
            vu = _lease_valid_until(path)
            if vu is not None and vu > clock():
                # Still held. A frozen lease just waits; one that ADVANCED past its gate
                # baseline is a resurrected owner -> conflict, stop contending for it.
                base = baseline.get(path)
                if base is not None and vu > base:
                    conflicts.append(
                        LeaseConflict(path=path, remaining_secs=vu - clock(), kind="live")
                    )
                    logger.error(
                        "Pool restart conflict: lease for %s resurrected mid-recovery.", path
                    )
                    progressed = True            # removed from contention
                    continue
                still.append(path)
                continue
            # Lease is free (absent or lapsed): instantiating now secures it.
            outcome, case, err = journal._classify(path, registry)
            if outcome == _OK and case is not None:
                driver.add(case)
                report.readded.append(path)
            elif outcome == _OWNED:
                # A racer grabbed it between our stat and the construct; re-evaluate next pass.
                still.append(path)
                continue
            elif outcome == _MISSING:
                report.dropped_missing.append(path)
                report.failures.append(_failure(path, outcome, err))
            else:
                report.dropped_bad_type.append(path)
                report.failures.append(_failure(path, outcome, err))
            progressed = True
        pending = still
        if pending and not progressed:
            await sleep(timings.poll_secs)

    # Anything still held at the deadline is treated as an unresolved live conflict.
    for path in pending:
        vu = _lease_valid_until(path) or clock()
        conflicts.append(
            LeaseConflict(path=path, remaining_secs=max(0.0, vu - clock()), kind="timeout")
        )

    if conflicts:
        raise PoolRestartConflictError(
            conflicts, recovered=list(report.readded), elapsed_secs=clock() - started_at
        )

    journal.compact_from_live(report.readded)
    if attach:
        journal.attach(driver)
    return report


def _failure(path: Path, reason: str, error: BaseException | None) -> DroppedMember:
    return DroppedMember(
        path=Path(path),
        reason=reason,
        error_type=type(error).__name__ if error is not None else "",
        detail=str(error) if error is not None else "",
    )
