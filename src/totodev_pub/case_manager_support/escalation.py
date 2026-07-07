# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Escalation detection and handler dispatch (§8)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class CaseEscalationKind(str, enum.Enum):
    REPEATED_FAILURE = "REPEATED_FAILURE"
    STALLED = "STALLED"
    AUTO_BLOCKED = "AUTO_BLOCKED"
    REAP_ANOMALY = "REAP_ANOMALY"
    ADOPT_REJECTED = "ADOPT_REJECTED"
    ADOPT_FAILED = "ADOPT_FAILED"
    TERMINATION_VERIFICATION_FAILED = "TERMINATION_VERIFICATION_FAILED"
    EJECT_FAILED = "EJECT_FAILED"
    EPHEMERAL_PURGE_STRAGGLERS = "EPHEMERAL_PURGE_STRAGGLERS"


@dataclass(frozen=True)
class CaseEscalation:
    kind: CaseEscalationKind
    case_id: str | None
    case_folder: Path | None
    case_state: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class EscalationRegistry:
    def __init__(self) -> None:
        self._handlers: dict[int, Callable[[CaseEscalation], None]] = {}
        self._next_id = 0

    def register(self, callback: Callable[[CaseEscalation], None]) -> int:
        handle = self._next_id
        self._next_id += 1
        self._handlers[handle] = callback
        return handle

    def unregister(self, handle: int) -> None:
        self._handlers.pop(handle, None)

    def emit(self, escalation: CaseEscalation) -> None:
        for callback in list(self._handlers.values()):
            try:
                callback(escalation)
            except Exception:
                pass  # handlers must not block; swallow to continue

    def emit_simple(
        self,
        kind: CaseEscalationKind | str,
        case_id: str | None,
        case_folder: Path | None,
        detail: str | None = None,
        *,
        case_state: str | None = None,
    ) -> None:
        if isinstance(kind, str):
            kind = CaseEscalationKind(kind)
        self.emit(
            CaseEscalation(
                kind=kind,
                case_id=case_id,
                case_folder=case_folder,
                case_state=case_state,
                detail={"message": detail} if detail else {},
            )
        )
