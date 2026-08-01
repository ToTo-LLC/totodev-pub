# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""ManagerWatchdog (§3) — a daemon thread inside the manager process.

A plain threading.Thread is immune to event-loop blockage, which is the entire
trick. The watchdog never repairs — it detects, diagnoses, and dies; the unit
of remediation is the process, and recover() makes hard death ordinary.

Depends on CaseManager only through the small read-only stamp/task surface
(_last_pulse, _last_tick_started/_completed, _run_task, _running) plus the
shutdown-scan helper. Independently testable without a host.
"""

from __future__ import annotations

import asyncio
import enum
import faulthandler
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from totodev_pub.case_manager_support.constants import PULSE_INTERVAL_SECS
from totodev_pub.case_manager_support.shutdown import (
    ShutdownDirective,
    scan_shutdown_intake,
    shutdown_intake_dir,
)
from totodev_pub.folder_backed_case_support.constants import DEFAULT_LEASE_TTL_SECS

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)

DEATH_RECORD_GLOB = "manager_death_*.yaml"
DEATH_RECORD_WINDOW_SECS = 24 * 3600.0
DEATH_RECORD_LOG_CAP = 5


class WatchdogDetection(str, enum.Enum):
    PULSE_STUCK = "pulse_stuck"
    LOOP_TASK_DEAD = "loop_task_dead"
    LOOP_FAILURE = "loop_failure"  # handed over via CaseManager.on_loop_failure
    MAILBOX_NEGLECT = "mailbox_neglect"


def write_death_record(manager_dir: Path, *, check: str, reason: str) -> Path:
    """Write a timestamped death record (UTC, minute resolution).

    DELIBERATELY primitive I/O — open()/write()/os.replace(), never
    FileMappedPydanticMixin: the mixin's locking/serialization machinery is
    exactly the kind of code a corrupted process might be wedged inside, and
    the death path must have no dependency that can itself hang. Do not
    "helpfully" normalize this onto the mixin.

    Same-minute records overwrite each other — accepted: collapsing a
    sub-minute crash loop to one file per minute beats writing hundreds."""
    now = datetime.now(timezone.utc)
    name = f"manager_death_{now.strftime('%Y-%m-%d_%H%M')}.yaml"
    safe_reason = reason.replace("'", "''")
    body = (
        f"detected_at: '{now.strftime('%Y-%m-%dT%H:%M:%SZ')}'\n"
        f"check: {check}\n"
        f"reason: '{safe_reason}'\n"
        f"pid: {os.getpid()}\n"
    )
    tmp = manager_dir / f".{name}.tmp"
    final = manager_dir / name
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, final)
    return final


def log_recent_death_records(manager_dir: Path) -> int:
    """Log one line per death record with mtime in the last 24h — capped at the
    5 most recent, plus the total count. Called from recover() so the
    reliability signal surfaces without the operator knowing to look."""
    try:
        now = time.time()
        records = [
            p
            for p in manager_dir.glob(DEATH_RECORD_GLOB)
            if now - p.stat().st_mtime <= DEATH_RECORD_WINDOW_SECS
        ]
    except OSError:
        return 0
    if not records:
        return 0
    records.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    logger.warning(
        "%d watchdog death record(s) in the last 24h under %s (showing %d most recent)",
        len(records),
        manager_dir,
        min(len(records), DEATH_RECORD_LOG_CAP),
    )
    for path in records[:DEATH_RECORD_LOG_CAP]:
        check = reason = "?"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("check:"):
                    check = line.partition(":")[2].strip()
                elif line.startswith("reason:"):
                    reason = line.partition(":")[2].strip()
        except OSError:
            pass
        logger.warning("  %s — check=%s reason=%s", path.name, check, reason)
    return len(records)


class ManagerWatchdog:
    """Daemon thread checking liveness on a short cadence.

    Kill-authorized checks: pulse liveness, loop-task death, mailbox neglect,
    plus loop failures handed in via request_kill(). The tick-duration check
    is ALARM ONLY, ALWAYS — long ticks are a throughput smell, never a
    liveness failure."""

    def __init__(
        self,
        manager: "CaseManager",
        *,
        loop: asyncio.AbstractEventLoop,
        exit_code: int,
        on_shutdown_request: Callable[[ShutdownDirective], None] | None = None,
        action: str | None = None,
        stop_grace_secs: float = 30.0,
        check_interval_secs: float = 1.0,
        exit_fn: Callable[[int], None] = os._exit,
    ) -> None:
        self._manager = manager
        self._loop = loop
        self._exit_code = exit_code
        self._on_shutdown_request = on_shutdown_request
        self._stop_grace_secs = stop_grace_secs
        self._check_interval = check_interval_secs
        self._exit_fn = exit_fn

        policy = manager._policy
        self._action = action if action is not None else policy.watchdog_action
        # Thresholds derive from existing policy rather than free-floating numbers.
        self._pulse_stuck_secs = (
            policy.watchdog_pulse_stuck_secs
            if policy.watchdog_pulse_stuck_secs is not None
            else max(10 * PULSE_INTERVAL_SECS, 5.0)
        )
        raw_mailbox = policy.watchdog_mailbox_stale_secs
        if not policy.enable_mailbox or raw_mailbox == 0:
            self._mailbox_stale_secs: float | None = None  # check disabled
        elif raw_mailbox is None:
            self._mailbox_stale_secs = max(10 * policy.maintenance_interval_secs, 30.0)
        else:
            self._mailbox_stale_secs = raw_mailbox
        self._tick_warn_secs = (
            policy.watchdog_tick_warn_secs
            if policy.watchdog_tick_warn_secs is not None
            else min(DEFAULT_LEASE_TTL_SECS, float(policy.manifest_stale_secs)) / 2
        )

        self._armed_at: float | None = None
        self._parked = threading.Event()
        self._parked.set()  # constructed parked; arm() unparks
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._kill_requested: BaseException | None = None
        self._tick_warned_for: float | None = None

    # -- host controls -------------------------------------------------

    def arm(self) -> None:
        """Start (or resume) watching with a fresh stamp, so the first check
        window never measures recovery/startup time."""
        self._armed_at = time.monotonic()
        self._parked.clear()
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="manager-watchdog", daemon=True
            )
            self._thread.start()

    def park(self) -> None:
        """Suspend all checks — called whenever a deliberate shutdown begins,
        else every clean docker stop becomes a nonzero 'wedge' exit."""
        self._parked.set()

    def stop(self) -> None:
        """Terminate the thread (deliberate shutdown / tests / teardown)."""
        self._parked.set()
        self._stopped.set()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def request_kill(self, exc: BaseException) -> None:
        """Entry point for CaseManager.on_loop_failure. Runs on the event-loop
        thread, so it must NOT run the ladder inline (the ladder blocks and
        schedules work back onto the loop — inline would deadlock). Flag it;
        the watchdog thread picks it up within one check interval."""
        self._kill_requested = exc

    # -- thread body -----------------------------------------------------

    def _run(self) -> None:
        while not self._stopped.wait(self._check_interval):
            if self._parked.is_set():
                continue
            try:
                self._check_once()
            except Exception:
                logger.exception("Watchdog check iteration failed (ignored)")

    def _check_once(self) -> None:
        m = self._manager
        now = time.monotonic()
        armed_at = self._armed_at if self._armed_at is not None else now

        # 0. Shutdown request — a command, not a failure. Checked first so the
        #    *requested* exit code wins over a concurrent wedge detection.
        if self._on_shutdown_request is not None:
            directive = scan_shutdown_intake(
                shutdown_intake_dir(m._manager_dir, m._policy)
            )
            if directive is not None:
                self._parked.set()  # shutdown choreography owns the process now
                directive.source_path.unlink(missing_ok=True)
                self._on_shutdown_request(directive)
                return

        # 1. Loop failure handed over by CaseManager.on_loop_failure.
        if self._kill_requested is not None:
            exc, self._kill_requested = self._kill_requested, None
            self._ladder(
                WatchdogDetection.LOOP_FAILURE, f"manager loop gave up: {exc!r}"
            )
            return

        if not m._running:
            return

        # 2. Pulse liveness — the primary, kill-authorized signal. Measures the
        #    event loop, never tick duration.
        ref = max(m._last_pulse or 0.0, armed_at)
        if now - ref > self._pulse_stuck_secs:
            self._ladder(
                WatchdogDetection.PULSE_STUCK,
                f"event loop pulse silent for {now - ref:.1f}s "
                f"(threshold {self._pulse_stuck_secs:.1f}s)",
            )
            return

        # 3. Loop-task death (backstop for a task killed despite §1 hardening).
        task = m._run_task
        if task is not None and task.done() and not task.cancelled():
            try:
                exc = task.exception()
            except Exception:  # pragma: no cover - cross-thread paranoia
                exc = None
            self._ladder(
                WatchdogDetection.LOOP_TASK_DEAD, f"manager loop task died: {exc!r}"
            )
            return

        # 4. Mailbox neglect — loop ticking but the mailbox processor broken.
        #    Excludes the shutdown mailbox (a command with its own pickup path,
        #    not a backlog). A large-but-fresh backlog does not alarm.
        if (
            self._mailbox_stale_secs is not None
            and now - armed_at > self._mailbox_stale_secs
        ):
            oldest_age = self._oldest_intake_age()
            if oldest_age is not None and oldest_age > self._mailbox_stale_secs:
                self._ladder(
                    WatchdogDetection.MAILBOX_NEGLECT,
                    f"oldest mailbox intake file unprocessed for {oldest_age:.1f}s "
                    f"(threshold {self._mailbox_stale_secs:.1f}s)",
                )
                return

        # 5. Tick duration — ALARM ONLY, ALWAYS (inventory fact 3: a healthy
        #    manager can legitimately look busy for minutes). Never a kill.
        started, completed = m._last_tick_started, m._last_tick_completed
        if started is not None and (completed is None or completed < started):
            duration = now - started
            if duration > self._tick_warn_secs and self._tick_warned_for != started:
                self._tick_warned_for = started
                detail = (
                    f"maintenance tick running for {duration:.1f}s (warn threshold "
                    f"{self._tick_warn_secs:.1f}s); idle-case leases may lapse past "
                    f"the {DEFAULT_LEASE_TTL_SECS:.0f}s TTL. Alarm only — never a kill."
                )
                logger.warning("Watchdog: %s", detail)
                self._emit_notice(f"tick_slow: {detail}", prefer_threadsafe=True)

    def _oldest_intake_age(self) -> float | None:
        mailbox = self._manager._mailbox
        oldest_mtime: float | None = None
        for intake in (
            mailbox.fire_intake(),
            mailbox.adopt_intake(),
            mailbox.reclassify_intake(),
        ):
            if not intake.exists():
                continue
            for path in intake.glob("*.yaml"):  # non-hidden by construction
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if oldest_mtime is None or mtime < oldest_mtime:
                    oldest_mtime = mtime
        if oldest_mtime is None:
            return None
        return time.time() - oldest_mtime

    # -- the kill ladder ---------------------------------------------------

    def _ladder(self, detection: WatchdogDetection, detail: str) -> None:
        """Detect → diagnose loudly → die. Never repairs. Order matters."""
        # 1. Escalation. For a wedged loop call_soon_threadsafe would enqueue a
        #    callback that never runs before os._exit, so PULSE_STUCK dispatches
        #    directly on this thread (documented: MANAGER_UNRESPONSIVE may
        #    arrive off-loop).
        self._emit_notice(
            f"{detection.value}: {detail}",
            prefer_threadsafe=detection is not WatchdogDetection.PULSE_STUCK,
        )
        # 2. Every thread's stack to stderr — the primary postmortem artifact;
        #    works from this thread even when the loop is wedged.
        faulthandler.dump_traceback(all_threads=True)
        if self._action == "alarm_only":
            logger.error(
                "Watchdog detection %s (%s) — watchdog_action=alarm_only; "
                "parking instead of exiting.",
                detection.value,
                detail,
            )
            self._parked.set()  # don't re-alarm every second on the same wedge
            return
        # 3. Death record (primitive I/O — see write_death_record).
        try:
            write_death_record(
                self._manager._manager_dir, check=detection.value, reason=detail
            )
        except Exception:
            logger.exception("Failed to write death record (continuing to exit)")
        # 4. Graceful attempt — detection-aware. A wedged loop won't run a
        #    scheduled coroutine any sooner than a signal handler, so
        #    PULSE_STUCK gets only a ~2s cap instead of the full grace.
        grace = (
            2.0
            if detection is WatchdogDetection.PULSE_STUCK
            else self._stop_grace_secs
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._manager.stop(timeout=grace), self._loop
            )
            fut.result(timeout=grace + 5.0)
        except Exception:
            pass
        # 5. Exit — ALWAYS the watchdog code, even when rung 4 succeeded: a
        #    watchdog detection is never allowed to end in exit 0. os._exit
        #    bypasses all Python cleanup so it works even when the interpreter
        #    is too damaged for sys.exit. (The park is unreachable in
        #    production; it keeps a monkeypatched exit_fn in tests from
        #    re-firing on the next check interval.)
        self._parked.set()
        self._exit_fn(self._exit_code)

    def _emit_notice(self, detail: str, *, prefer_threadsafe: bool) -> None:
        def _do() -> None:
            self._manager._notices.emit_simple(
                "MANAGER_UNRESPONSIVE", None, None, detail
            )

        if prefer_threadsafe:
            try:
                self._loop.call_soon_threadsafe(_do)
                return
            except RuntimeError:
                pass  # loop closed/unusable — which is, after all, the diagnosis
        _do()
