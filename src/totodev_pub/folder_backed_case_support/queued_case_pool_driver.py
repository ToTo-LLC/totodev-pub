# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""QueuedCasePoolDriver — queue-ordered, seniority-first driver for bursty workflows.

**When to use:** When cases should behave more like a work queue than a load
balancer — finish (or burst through) one case at a time, with the front of the
line holding top claim on pool capacity. Fits workflows that rush through
automatic transitions then idle at manual gates for long periods: the actively
bursting case at the head should win contested in-flight slots and choke permits
before cases behind it. For steady fleet-wide progress without seniority, use
``TieredCasePoolDriver``.

**Strategy:** Subclasses ``TieredCasePoolDriver`` and keeps its MLFQ cadence
(HOT/WARM/COLD) for *when* cases are due, but adds a queue contract on *who
wins* when capacity is scarce: ``_by_folder`` insertion order is seniority.
Cases requeue to the tail when waking from a manual-only state so newly active
work does not jump ahead of the line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.tiered_case_pool_driver import (
    CasePeek,
    TieredCasePoolDriver,
    _Slot,
)


@dataclass
class _QueuedSlot(_Slot):
    """Tiered slot plus queue wake tracking."""

    last_seen_state: str = ""


@dataclass(frozen=True)
class QueuedCasePeek(CasePeek):
    """Scheduling peek including queue position (0 = front)."""

    queue_position: int = 0


class QueuedCasePoolDriver(TieredCasePoolDriver):
    """MLFQ cadence with queue-ordered seniority on contested capacity."""

    def _make_slot(self, case) -> _Slot:
        slot = super()._make_slot(case)
        return _QueuedSlot(
            case=slot.case,
            tier=slot.tier,
            reset_multiple=slot.reset_multiple,
            skip_countdown=slot.skip_countdown,
            noop_streak=slot.noop_streak,
            fail_streak=slot.fail_streak,
            in_flight=slot.in_flight,
            terminal=slot.terminal,
            halt_requested=slot.halt_requested,
            halt_settled=slot.halt_settled,
            last_result=slot.last_result,
            last_advanced_at=slot.last_advanced_at,
            last_heartbeat_at=slot.last_heartbeat_at,
            task=slot.task,
            choked=slot.choked,
            pending_grant=slot.pending_grant,
            last_seen_state=case.case_state,
        )

    def _slot_prelaunch(self, slot: _Slot) -> None:
        if not isinstance(slot, _QueuedSlot):
            return
        spec = slot.case.case_type_spec()
        if (
            slot.last_seen_state != slot.case.case_state
            and not spec.fsm.has_auto_exits(slot.last_seen_state)
        ):
            self._requeue_to_tail(slot)
        slot.last_seen_state = slot.case.case_state

    def _slot_post_step(self, slot: _Slot, result: AdvanceResult) -> None:
        if not isinstance(slot, _QueuedSlot):
            return
        spec = slot.case.case_type_spec()
        if result.progressed and not spec.fsm.has_auto_exits(result.initial_state):
            self._requeue_to_tail(slot)
        slot.last_seen_state = slot.case.case_state

    def _requeue_to_tail(self, slot: _QueuedSlot) -> None:
        folder = slot.case.case_folder
        if folder not in self._by_folder:
            return
        del self._by_folder[folder]
        self._by_folder[folder] = slot

    def peek(self, case_folder: Path) -> QueuedCasePeek:
        base = super().peek(case_folder)
        queue_position = list(self._by_folder.keys()).index(case_folder)
        return QueuedCasePeek(
            case_folder=base.case_folder,
            case_id=base.case_id,
            state=base.state,
            tier=base.tier,
            in_flight=base.in_flight,
            terminal=base.terminal,
            halt_requested=base.halt_requested,
            noop_streak=base.noop_streak,
            fail_streak=base.fail_streak,
            skip_countdown=base.skip_countdown,
            last_result=base.last_result,
            choked=base.choked,
            queue_position=queue_position,
        )
