# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Per-case folder logging: the always-on tee that mirrors a FolderBackedCase
instance's log records into its own `logs/case.log`.

Design goals (see docs/folder-backed-case-folder-logging-design.md):
  * Typical logger semantics — `case.log` is an ordinary `logging.Logger`.
  * Zero idle file descriptors — a close-after-write handler opens, writes one
    line, and closes per record, so N live cases cost 0 open fds at rest.
  * No process-global growth — the per-instance logger is constructed DIRECTLY
    (never via getLogger), so it is garbage-collected with the case and never
    enters the global logger registry.
  * Log-file survival across purge is keepfile-driven (delete unless kept).
    A process-global LogRetention knob can seed a keep rule at bind time.

This module owns all logging mechanics; FolderBackedCase only wires them in.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from pathlib import Path

# The per-case file line format. Static identity (case_id, case_object_type) and
# the dynamic current state are injected onto each record by _CaseContextFilter,
# so a tee'd file is self-identifying without the caller doing anything. `process`
# is a standard LogRecord field (no filter needed) — carrying pid here means
# callers never have to embed it in message text (see write_attach_banner).
_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)s pid=%(process)d "
    "[%(case_object_type)s %(case_id)s @%(case_state)s] "
    "%(name)s: %(message)s"
)
# Every other timestamp this framework produces is aware-UTC (see `_utcnow()`), but
# `logging.Formatter.converter` defaults to `time.localtime`. Force UTC here so
# `asctime` agrees with the rest of the codebase instead of silently drifting to
# whatever timezone the host process happens to run in.
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# The shared, registry-backed parent of every per-instance case logger. Per-case
# loggers chain to this (and thence to root) for propagation, but are NOT children
# of it in the registry sense — they are constructed directly (see build_case_logger).
_CASE_LOGGER_PARENT_NAME = "totodev_pub.case"


class LogRetention(enum.Enum):
    """Whether bind-time framework seeding should keep ``logs/case.log`` across purge.

    Purge itself only deletes unmatched files — this enum does not invent a second
    purge mode. ``RETAIN`` causes ``CaseKeepManifest.ensure_framework_rules()`` to
    seed a keep rule for the log file; ``PURGE`` leaves it unmatched (deleted).
    """

    PURGE = "purge"     # default: do not seed a keep rule; purge deletes the log
    RETAIN = "retain"   # seed CASE_LOG_KEEP_RULE at bind (typical for dev/test)


# Process-global default. Deliberately PURGE for a privacy-conscious production
# posture; dev/test calls set_case_log_retention(RETAIN) once at startup.
_RETENTION: LogRetention = LogRetention.PURGE


def set_case_log_retention(policy: LogRetention) -> None:
    """Set the process-global bind-time log keep-rule seeding policy.

    This is a coarse, out-of-band developer-debugging knob — NOT a per-object or
    mainstream-API setting. Call it once at process startup (e.g. a dev/test
    bootstrap calls `set_case_log_retention(LogRetention.RETAIN)` so new/rebound
    cases seed a keep rule for ``logs/case.log``). Per-case retention without the
    global knob is just ``case_keep_files("logs/case.log")``.
    """
    global _RETENTION
    if not isinstance(policy, LogRetention):
        raise TypeError(f"policy must be a LogRetention, got {type(policy).__name__}")
    _RETENTION = policy


def get_case_log_retention() -> LogRetention:
    """The current process-global bind-time log keep-rule seeding policy (defaults to PURGE)."""
    return _RETENTION

class _CaseFileLogHandler(logging.Handler):
    """A close-after-write file handler: holds NO persistent descriptor.

    On each record it opens the target file in append mode, writes one formatted
    line, and closes it — under the lock `logging` already holds around emit. This
    bounds open file descriptors to ~0 at rest and 1 transiently during a write,
    so thousands of live cases never exhaust a modest `ulimit -n`. The cost is a
    couple of extra syscalls per line, negligible at diagnostic volume.
    """

    def __init__(self, path: Path):
        super().__init__()
        self._path = Path(path)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except Exception:                       # never let logging crash the case
            self.handleError(record)


class _CaseLogger(logging.Logger):
    """A directly-constructed (never-registered) ``Logger`` whose ``getChild`` ALSO
    constructs directly.

    Plain ``logging.Logger.getChild`` routes through ``Manager.getLogger``, which
    would resolve against the GLOBAL registry — landing the child on the registered
    `totodev_pub.case` parent (see ``_CASE_LOGGER_PARENT_NAME``) instead of on this
    per-instance tee, silently skipping the per-case file handler, and leaking one
    registry entry per case per distinct child name (defeating the whole
    no-process-global-growth point of building this class directly in the first
    place). Overriding ``getChild`` here means a helper sub-logger a hook creates
    (``self.log.getChild("payments")``) behaves like an ordinary child logger —
    propagates, has no handlers of its own — while still tee-ing into the same
    per-case file, transparently.

    Children are memoized per suffix so repeated calls return the same object
    (matching the registry-backed lookup semantics a caller would otherwise expect),
    without the children themselves ever touching the registry.
    """

    def getChild(self, suffix: str) -> "_CaseLogger":
        cache: dict[str, "_CaseLogger"] = self.__dict__.setdefault("_case_children", {})
        child = cache.get(suffix)
        if child is None:
            child = _CaseLogger(f"{self.name}.{suffix}")
            child.parent = self
            child.propagate = True
            # NOTSET: no opinion of its own — defers to the parent's effective level
            # (DEBUG, per build_case_logger), same as an ordinary child logger would.
            child.setLevel(logging.NOTSET)
            cache[suffix] = child
        return child


class _CaseContextFilter(logging.Filter):
    """Injects case identity + current state onto every record bound for the file.

    Attached to the file handler (not the logger), so it decorates records only
    on their way to the per-case file; propagation to root handlers is untouched.
    The state provider is expected to NOT strongly capture the case (the caller
    passes a weakref-based accessor), so the logger never pins its owning case.
    """

    def __init__(
        self,
        case_id: str,
        case_object_type: str,
        state_provider: Callable[[], str | None],
    ):
        super().__init__()
        self._case_id = case_id
        self._case_object_type = case_object_type
        self._state_provider = state_provider

    def filter(self, record: logging.LogRecord) -> bool:
        record.case_id = self._case_id
        record.case_object_type = self._case_object_type
        try:
            record.case_state = self._state_provider() or "?"
        except Exception:
            record.case_state = "?"
        return True


def build_case_logger(
    case_id: str,
    log_path: Path,
    *,
    case_object_type: str,
    state_provider: Callable[[], str | None],
) -> logging.Logger:
    """Build a per-instance case logger that tees to `log_path`.

    The logger is constructed DIRECTLY (not via logging.getLogger): it never
    enters the global registry, so it is collected with the owning case and a
    process that churns many cases does not accumulate dead Logger objects. Its
    parent is set to the shared `totodev_pub.case` logger so records still
    propagate up to root (the "default logging" half of the tee). A single
    close-after-write handler supplies the per-case-file half.
    """
    lg = _CaseLogger(f"{_CASE_LOGGER_PARENT_NAME}.{case_id}")
    lg.parent = logging.getLogger(_CASE_LOGGER_PARENT_NAME)
    lg.propagate = True
    # Capture verbose detail in the per-case file regardless of the app's root
    # level; propagated records are still filtered by the app's own handlers.
    lg.setLevel(logging.DEBUG)

    handler = _CaseFileLogHandler(Path(log_path))
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    formatter.converter = time.gmtime      # UTC, matching every other timestamp we emit
    handler.setFormatter(formatter)
    handler.addFilter(_CaseContextFilter(case_id, case_object_type, state_provider))
    lg.addHandler(handler)
    return lg


def write_attach_banner(logger: logging.Logger) -> None:
    """Emit a one-line banner marking a fresh attach session, so multiple
    open/close episodes are visually separable within the single appended file.

    Carries no timestamp or pid of its own — ``%(asctime)s`` and ``%(process)d``
    in ``_LOG_FORMAT`` already supply both as ordinary, parseable record fields,
    same as every other line in the file."""
    logger.info("--- case attached ---")


def write_detach_banner(logger: logging.Logger) -> None:
    """Emit a one-line banner marking a closed attach session — the symmetric
    bookend to ``write_attach_banner``. Call BEFORE ``disable_case_file_tee``
    so the banner itself still lands in the per-case file as its closing line."""
    logger.info("--- case detached ---")


def disable_case_file_tee(logger: logging.Logger) -> None:
    """Remove and close this logger's per-case file handler(s); leaves the logger
    otherwise usable (still propagates to root — the "default logging" half of the
    tee keeps working) but no longer able to write into the case folder.

    Call this on detach: a detached object no longer holds the folder's lease, so it
    must never write into a folder another process may now own by then (the SAME
    invariant that delays building this tee, at attach time, until AFTER the lease is
    acquired — see FolderBackedCase._bind_existing_case_dir). Targets only
    ``_CaseFileLogHandler`` instances, so any handler a caller added by hand (e.g.
    ``case.log.addHandler(...)``) is left alone. Idempotent — a no-op past the first
    call, including on repeat ``case_detach()`` calls.
    """
    for handler in list(logger.handlers):
        if isinstance(handler, _CaseFileLogHandler):
            logger.removeHandler(handler)
            handler.close()

