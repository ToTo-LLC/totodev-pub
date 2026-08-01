# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Kind-tagged notices the manager publishes, and their handler dispatch.

Two families share one channel: **problems** the operator should act on, and
**lifecycle facts** about cases leaving the pool. One channel rather than two
because subscribers filter by kind anyway, and a parallel registry would be more
machinery for the same result.

Departures are *announced, not catalogued*. The manager does not become the
librarian of departed cases — an application that needs history subscribes and
keeps its own record. Note what that implies: **delivery is at-most-once.**
In-process pub/sub does not survive a crash, so a case that departs just before
the process dies is never announced. That is acceptable for operator convenience
and unacceptable as an audit log; do not use it as one.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class CaseNoticeKind(str, enum.Enum):
    # Problems.
    REPEATED_FAILURE = "REPEATED_FAILURE"
    STALLED = "STALLED"
    AUTO_BLOCKED = "AUTO_BLOCKED"
    READMIT_ANOMALY = "READMIT_ANOMALY"
    ADOPT_REJECTED = "ADOPT_REJECTED"
    ADOPT_FAILED = "ADOPT_FAILED"
    TERMINATION_VERIFICATION_FAILED = "TERMINATION_VERIFICATION_FAILED"
    EJECT_FAILED = "EJECT_FAILED"
    MANAGER_UNRESPONSIVE = "MANAGER_UNRESPONSIVE"
    MAINTENANCE_ITEM_FAILED = "MAINTENANCE_ITEM_FAILED"

    # Lifecycle: a case has left the pool. Normal events, not problems.
    CASE_TERMINATED = "CASE_TERMINATED"
    CASE_QUARANTINED = "CASE_QUARANTINED"
    CASE_EJECTED = "CASE_EJECTED"

    @property
    def is_lifecycle(self) -> bool:
        """True for departure announcements, False for problems.

        The discriminator subscribers filter on — a paging handler wants the
        problems and nothing else."""
        return self in _LIFECYCLE_KINDS


_LIFECYCLE_KINDS = frozenset(
    {
        CaseNoticeKind.CASE_TERMINATED,
        CaseNoticeKind.CASE_QUARANTINED,
        CaseNoticeKind.CASE_EJECTED,
    }
)


@dataclass(frozen=True)
class CaseNotice:
    kind: CaseNoticeKind
    case_id: str | None
    case_folder: Path | None
    case_state: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class NoticeRegistry:
    def __init__(self) -> None:
        self._handlers: dict[int, Callable[[CaseNotice], None]] = {}
        self._next_id = 0

    def __len__(self) -> int:
        """Number of registered notice handlers."""
        return len(self._handlers)

    def register(self, callback: Callable[[CaseNotice], None]) -> int:
        handle = self._next_id
        self._next_id += 1
        self._handlers[handle] = callback
        return handle

    def unregister(self, handle: int) -> None:
        self._handlers.pop(handle, None)

    def emit(self, notice: CaseNotice) -> None:
        for callback in list(self._handlers.values()):
            try:
                callback(notice)
            except Exception:
                pass  # handlers must not block; swallow to continue

    def emit_simple(
        self,
        kind: CaseNoticeKind | str,
        case_id: str | None,
        case_folder: Path | None,
        detail: str | None = None,
        *,
        case_state: str | None = None,
    ) -> None:
        if isinstance(kind, str):
            kind = CaseNoticeKind(kind)
        self.emit(
            CaseNotice(
                kind=kind,
                case_id=case_id,
                case_folder=case_folder,
                case_state=case_state,
                detail={"message": detail} if detail else {},
            )
        )
