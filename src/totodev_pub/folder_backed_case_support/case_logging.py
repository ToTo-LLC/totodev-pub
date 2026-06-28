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
  * Retention is a process-global, out-of-band knob defaulting to PURGE.

This module owns all logging mechanics; FolderBackedCase only wires them in.
"""

from __future__ import annotations

import enum
import logging
import os
from collections.abc import Callable
from pathlib import Path

from totodev_pub.folder_backed_case_support.constants import LOG_PURGE_SENTINEL
from totodev_pub.folder_backed_case_support.helpers import _utcnow


# The per-case file line format. Static identity (case_id, case_object_type) and
# the dynamic current state are injected onto each record by _CaseContextFilter,
# so a tee'd file is self-identifying without the caller doing anything.
_LOG_FORMAT = (
    "%(asctime)s %(levelname)s "
    "[%(case_object_type)s %(case_id)s @%(case_state)s] "
    "%(name)s: %(message)s"
)

# The shared, registry-backed parent of every per-instance case logger. Per-case
# loggers chain to this (and thence to root) for propagation, but are NOT children
# of it in the registry sense — they are constructed directly (see build_case_logger).
_CASE_LOGGER_PARENT_NAME = "totodev_pub.case"


class LogRetention(enum.Enum):
    """What happens to a case's `logs/case.log` when the case reaches a closed state."""

    PURGE = "purge"     # rewrite the file with a single sentinel line (default)
    RETAIN = "retain"   # keep the full contents (typical for dev/test)


# Process-global default. Deliberately PURGE for a privacy-conscious production
# posture; dev/test calls set_case_log_retention(RETAIN) once at startup.
_RETENTION: LogRetention = LogRetention.PURGE


def set_case_log_retention(policy: LogRetention) -> None:
    """Set the process-global closure retention policy for per-case folder logs.

    This is a coarse, out-of-band developer-debugging knob — NOT a per-object or
    mainstream-API setting. Call it once at process startup (e.g. a dev/test
    bootstrap calls `set_case_log_retention(LogRetention.RETAIN)` to keep logs).
    """
    global _RETENTION
    if not isinstance(policy, LogRetention):
        raise TypeError(f"policy must be a LogRetention, got {type(policy).__name__}")
    _RETENTION = policy


def get_case_log_retention() -> LogRetention:
    """The current process-global closure retention policy (defaults to PURGE)."""
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
    lg = logging.Logger(f"{_CASE_LOGGER_PARENT_NAME}.{case_id}")
    lg.parent = logging.getLogger(_CASE_LOGGER_PARENT_NAME)
    lg.propagate = True
    # Capture verbose detail in the per-case file regardless of the app's root
    # level; propagated records are still filtered by the app's own handlers.
    lg.setLevel(logging.DEBUG)

    handler = _CaseFileLogHandler(Path(log_path))
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_CaseContextFilter(case_id, case_object_type, state_provider))
    lg.addHandler(handler)
    return lg


def write_attach_banner(logger: logging.Logger) -> None:
    """Emit a one-line banner marking a fresh attach session, so multiple
    open/close episodes are visually separable within the single appended file."""
    logger.info("--- attached %s (pid %d) ---", _utcnow().isoformat(), os.getpid())


def purge_case_log(log_path: Path) -> None:
    """Apply the PURGE policy: rewrite the log file with a single sentinel line.

    The file is rewritten in place (never unlinked) and `logs/` is left intact,
    so the case folder layout stays stable. Because the handler holds no open
    descriptor, this is a plain write with no live-handle coordination. A missing
    file is a no-op.
    """
    p = Path(log_path)
    if not p.exists():
        return
    p.write_text(LOG_PURGE_SENTINEL + "\n", encoding="utf-8")
