# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""case_manager_host — the blessed host entry point (§4).

CaseManager coordinates a fleet; this module runs a process. Everything that
knows it owns a whole process (signals, exit codes, the watchdog's arm/park
choreography, the shutdown-request execution path) lives here — case_manager.py
never imports signal/threading/faulthandler and never defines an exit code.

Typical host program:

    import asyncio
    from totodev_pub.case_manager import CaseManager
    from totodev_pub.case_manager_host import serve
    from myapp.cases import InquiryCase

    manager = CaseManager.open("/data/inquiries", register_types=[InquiryCase])
    asyncio.run(serve(manager))
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import TYPE_CHECKING, Any, Callable

from totodev_pub.case_manager_support.exceptions import CaseManagerStopTimeoutError
from totodev_pub.case_manager_support.shutdown import ShutdownDirective, write_shutdown_ack
from totodev_pub.case_manager_support.watchdog import ManagerWatchdog

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)

# Supervisor exit-code contract — defined once, here. Exit 0 is reserved for a
# stop that both originates outside the manager's own reactive machinery and
# is genuinely meant to be final: an OS-signaled stop, or stop_when
# self-completion. Anything the manager does *in reaction to* something is
# nonzero, even if it shuts down cleanly.
EXIT_WATCHDOG = 70           # EX_SOFTWARE: every watchdog kill, unconditionally.
EXIT_RESTART_REQUESTED = 75  # EX_TEMPFAIL: every mailbox-requested shutdown —
                             # the mailbox has exactly one outcome, "please come back".

# Bound on how long the immediate (non-graceful) shutdown path lets the current
# loop iteration unwind before exiting.
IMMEDIATE_SHUTDOWN_GRACE_SECS = 2.0


def _hard_exit(code: int) -> None:
    """os._exit wrapper — bypasses all Python cleanup by design (works even
    when the interpreter is too damaged for sys.exit). Module-level so tests
    can monkeypatch it."""
    os._exit(code)


async def serve(
    manager: "CaseManager",
    *,
    stop_grace_secs: float = 30.0,
    stop_when: Callable[[], bool] | None = None,
    stop_when_empty: bool = False,
) -> None:
    """Recover, start, and host ``manager`` until stopped.

    Returns normally (→ process exit 0) on an OS-signaled stop (SIGTERM/SIGINT)
    or a True ``stop_when`` — a deliberate, final stop. Exits the process
    directly with ``EXIT_RESTART_REQUESTED`` for every mailbox-requested
    shutdown, and the watchdog exits with ``EXIT_WATCHDOG`` on any detection.

    ``manager`` must be freshly constructed — not yet recovered or started;
    serve() owns that sequencing itself. ``stop_grace_secs`` is host wiring,
    not deployment policy: Docker's ``stop_grace_period`` must exceed it.

    ``stop_when`` is polled once per maintenance interval on the manager's
    event loop — a blocking predicate is the caller's bug, exactly like a
    blocking ``perform_*`` hook. ``stop_when_empty=True`` is sugar for
    "stop when manager.is_idle" (job-manager hosts, §8); mutually exclusive
    with an explicit ``stop_when``.
    """
    if manager.recovered or manager.running:
        raise ValueError(
            "serve() requires a freshly constructed CaseManager (not recovered "
            "or started); it owns the recover()/start() sequencing itself."
        )
    if stop_when is not None and stop_when_empty:
        raise ValueError("stop_when and stop_when_empty are mutually exclusive")
    if stop_when_empty:
        stop_when = lambda: manager.is_idle  # noqa: E731

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    cause: dict[str, Any] = {"kind": None, "directive": None}

    def _record_cause(kind: str, directive: ShutdownDirective | None = None) -> None:
        if cause["kind"] is None:
            cause["kind"] = kind
            cause["directive"] = directive
            stop_event.set()

    # 1. Sequencing: recover() then start() (which starts the pulse task).
    await manager.recover()
    await manager.start()

    # Debugger sessions: a paused process looks exactly like a wedged one.
    action: str | None = None
    if sys.gettrace() is not None and manager._policy.watchdog_action == "exit":
        logger.info(
            "Debugger detected (sys.gettrace()); watchdog defaulting to alarm_only."
        )
        action = "alarm_only"

    def _shutdown_from_watchdog(directive: ShutdownDirective) -> None:
        # Watchdog-thread pickup path (§6): hand off to the loop; if the loop
        # is too wedged to run the cooperative path, exit directly with the
        # *requested* code rather than whatever the wedge detection would pick.
        try:
            loop.call_soon_threadsafe(_record_cause, "shutdown", directive)
        except RuntimeError:
            _hard_exit(EXIT_RESTART_REQUESTED)
        deadline = (
            (stop_grace_secs + 5.0)
            if directive.graceful
            else (IMMEDIATE_SHUTDOWN_GRACE_SECS + 2.0)
        )
        time.sleep(deadline)  # a healthy path exits the process before this returns
        _hard_exit(EXIT_RESTART_REQUESTED)

    # 2/3. Watchdog + both callback seams.
    watchdog: ManagerWatchdog | None = None
    if manager._policy.watchdog_enabled:
        watchdog = ManagerWatchdog(
            manager,
            loop=loop,
            exit_code=EXIT_WATCHDOG,
            on_shutdown_request=_shutdown_from_watchdog,
            action=action,
            stop_grace_secs=stop_grace_secs,
            exit_fn=_hard_exit,
        )
        manager.on_loop_failure(watchdog.request_kill)
    manager.on_shutdown_request(
        lambda directive: _record_cause("shutdown", directive)
    )

    # SIGTERM/SIGINT: the deliberate, external, final stop (exit 0).
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _record_cause, "signal")

    # 4. Arm after start(), with a fresh stamp — recovery time is never measured.
    if watchdog is not None:
        watchdog.arm()

    poller: asyncio.Task[None] | None = None
    if stop_when is not None:
        async def _poll_stop_when() -> None:
            interval = manager._policy.maintenance_interval_secs
            while not stop_event.is_set():
                if stop_when():
                    _record_cause("stop_when")
                    return
                await asyncio.sleep(interval)

        poller = asyncio.create_task(_poll_stop_when())

    # 5. Block until signaled, shutdown-requested, or watchdog-killed.
    try:
        await stop_event.wait()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        if poller is not None:
            poller.cancel()

    # Park before any deliberate shutdown: an armed watchdog during a settle
    # (which stops the pulse by design) would turn every clean docker stop into
    # a nonzero "wedge" exit.
    if watchdog is not None:
        watchdog.park()

    if cause["kind"] in ("signal", "stop_when"):
        try:
            await manager.stop(timeout=stop_grace_secs)
        except CaseManagerStopTimeoutError:
            logger.exception(
                "Deliberate stop failed to settle within %.1fs; exiting hard.",
                stop_grace_secs,
            )
            _hard_exit(EXIT_WATCHDOG)
        if watchdog is not None:
            watchdog.stop()  # join the thread; serve() may run inside a larger program
        return  # exit 0 — deliberate, external (or self-completed), and final.

    # Mailbox-requested shutdown (§6): a recovery lever, never exit 0.
    directive: ShutdownDirective = cause["directive"]
    if directive.graceful:
        # Ack before the drain so poll_result/wait_result resolve normally.
        write_shutdown_ack(manager._mailbox.results_dir(), directive)
        try:
            await manager.stop(timeout=stop_grace_secs)
        except CaseManagerStopTimeoutError:
            logger.exception(
                "Requested drain failed to settle within %.1fs.", stop_grace_secs
            )
        _hard_exit(EXIT_RESTART_REQUESTED)
    else:
        logger.warning(
            "Immediate shutdown triggered by mailbox request %s. For a graceful "
            "shutdown that waits for in-flight work to settle, include 'SIGTERM' "
            "in the request filename (or use "
            "CaseManagerClient.submit_shutdown(graceful=True)).",
            directive.source_path.name,
        )
        # The same "hard path" the system already tolerates (SIGKILL, watchdog
        # exit) — leases lapse, recover() reconciles — just requested on purpose.
        manager._stopping = True
        manager._running = False
        if manager._run_task is not None:
            await asyncio.wait(
                {manager._run_task}, timeout=IMMEDIATE_SHUTDOWN_GRACE_SECS
            )
        try:
            # One refinement over the pure hard path: this process is healthy
            # enough to have parsed the request, so one cheap manifest write
            # spares clients the 30s "dead or stopped?" ambiguity.
            manager._write_manifest(running=False, stopped=True)
        except Exception:
            pass
        _hard_exit(EXIT_RESTART_REQUESTED)
