# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Wire-safe projection of AdvanceResult for mailbox results (§10.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult


class AdvanceResultSerializable(BaseModel, FileMappedPydanticMixin):
    """A fire's outcome, as a result file.

    ``kind`` is the discriminator every result type carries, so a reader can tell
    them apart by asking rather than by guessing from the shape of the text."""

    kind: Literal["advance"] = "advance"
    status: Literal["completed", "rejected", "error"]
    initial_state: str
    final_state: str
    trigger: Optional[str] = None
    progressed: bool = False
    blocked: bool = False
    failed: bool = False
    alerted: bool = False
    exception_messages: tuple[str, ...] = Field(default_factory=tuple)
    completed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    @classmethod
    def from_advance_result(
        cls,
        ar: AdvanceResult,
        *,
        status: Literal["completed", "rejected", "error"] = "completed",
    ) -> "AdvanceResultSerializable":
        return cls(
            status=status,
            initial_state=ar.initial_state,
            final_state=ar.final_state,
            trigger=ar.trigger,
            progressed=ar.progressed,
            blocked=ar.blocked,
            failed=ar.failed,
            alerted=ar.alerted,
            exception_messages=tuple(str(e) for e in ar.exceptions),
        )
