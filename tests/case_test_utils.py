# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Test-only helpers for driving FolderBackedCase instances.

This is intentionally NOT part of the shipped library. Driving a single case to
its terminal state (seizing a worker until one case finishes) is an anti-pattern
for the framework's intended many-cases / round-robin deployment, so it lives
here purely as a testing convenience. Production driving is a scheduler concern
that belongs to a driver layer (see totodev_pub.folder_backed_case.CasePoolDriver),
not to the case.
"""

from __future__ import annotations

from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult


async def drive_to_completion(
    case: FolderBackedCase, *, stop_before: str | None = None
) -> AdvanceResult | None:
    """Drive a SINGLE case forward over its AUTO (`--`) edges until it closes, until
    nothing auto-advances (no progress — a failed attempt or a block), or until an auto
    step from the current state could ENTER ``stop_before`` (for staged inspection).

    Returns the LAST AdvanceResult (so a caller can inspect why the drive stopped), or
    None if it never stepped. For tests and one-off scripts only — see module docstring.
    """
    last: AdvanceResult | None = None
    while case.case_is_open:
        candidates = case._forward_candidates(case.case_state)
        if not candidates:
            break
        if stop_before and any(dest == stop_before for _, dest in candidates):
            break
        last = await case.case_advance()
        if not last.progressed:          # failed / all guards declined / blocked
            break
    return last
