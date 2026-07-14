# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""SeniorityCasePoolDriver — queue-ordered, seniority-first driver for bursty workflows.

**When to use:** When cases should behave more like a work queue than a load
balancer — finish (or burst through) one case at a time, with the front of the
line holding top claim on pool capacity. Fits workflows that rush through
automatic transitions then idle at manual gates for long periods: the actively
bursting case at the head should win contested in-flight slots and choke permits
before cases behind it. For steady fleet-wide progress without seniority, use
``BalancedCasePoolDriver``.

**Strategy:** Subclasses ``BalancedCasePoolDriver`` and keeps its MLFQ cadence
(HOT/WARM/COLD) for *when* cases are due, but adds a queue contract on *who
wins* when capacity is scarce: ``_by_folder`` insertion order is seniority.
Cases requeue to the tail when waking from a manual-only state so newly active
work does not jump ahead of the line.

**Beat tempo is inherited, unchanged.** ``advance()`` is not overridden here, so
the base driver's fixed-rate pacing applies as-is — including the eager beat
(``_TierPolicy.EAGER_BEAT_FRACTION``, default ``0.25``): while a sweep keeps
launching steps, the target period shrinks to a quarter of ``I0``, which is what
lets a bursting senior case race through its auto chain faster than the nominal
beat would allow. See ``BalancedCasePoolDriver.advance()`` for the full pacing
contract (fixed-rate target period, yield floor, eager window).

**HOT-only senior acceleration (optional):** Contested capacity alone is not
enough when the pool is under load ceilings — a case only becomes due on its
own countdown. Callers may widen the junior/senior HOT gap by raising the
baseline ``policy.M_HOT`` above 1 and setting ``senior_hot_multiple`` (default
still 1) so the front ``senior_count`` queue positions keep the faster HOT
reload. WARM/COLD are deliberately untouched: tier demotion is already the
signal that a case is not actively racing, so seniority never overrides those
cadences. With stock defaults (``M_HOT == senior_hot_multiple == 1``) this is a
no-op.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import (
    CasePeek,
    BalancedCasePoolDriver,
    Tier,
    _Slot,
    _TierPolicy,
)


@dataclass
class _SenioritySlot(_Slot):
    """Tiered slot plus queue wake tracking."""

    last_seen_state: str = ""


@dataclass(frozen=True)
class SeniorityCasePeek(CasePeek):
    """Scheduling peek including queue position (0 = front) and senior membership."""

    queue_position: int = 0
    is_senior: bool = False


class SeniorityCasePoolDriver(BalancedCasePoolDriver):
    """MLFQ cadence with queue-ordered seniority on contested capacity.

    Optionally accelerates the front ``senior_count`` HOT slots via
    ``senior_hot_multiple`` (see module docstring). Defaults are a no-op.
    """

    def __init__(
        self,
        *,
        policy: Optional[_TierPolicy] = None,
        concurrency_ceiling: int = 50,
        choke_limits: dict[str, int] | None = None,
        senior_count: int = 3,
        senior_hot_multiple: int = 1,
    ) -> None:
        super().__init__(
            policy=policy,
            concurrency_ceiling=concurrency_ceiling,
            choke_limits=choke_limits,
        )
        self._senior_count = senior_count
        self._senior_hot_multiple = senior_hot_multiple

    def _is_senior(self, folder: Path) -> bool:
        """True when ``folder`` is among the front ``senior_count`` queue positions."""
        return folder in set(itertools.islice(self._by_folder, self._senior_count))

    def _apply_senior_hot(self, slot: _Slot) -> None:
        """Override a HOT slot's cadence to the senior multiple when eligible."""
        if slot.tier is Tier.HOT and not slot.terminal:
            slot.reset_multiple = self._senior_hot_multiple
            slot.skip_countdown = max(1, self._senior_hot_multiple)

    def _make_slot(self, case) -> _Slot:
        slot = super()._make_slot(case)
        # Case is not in ``_by_folder`` yet; its future position is ``len(_by_folder)``.
        # When the pool is smaller than ``senior_count``, admit as a senior-hotty.
        apply_senior = (
            slot.tier is Tier.HOT
            and not slot.terminal
            and len(self._by_folder) < self._senior_count
        )
        if apply_senior:
            reset_multiple = self._senior_hot_multiple
            skip_countdown = max(1, self._senior_hot_multiple)
        else:
            reset_multiple = slot.reset_multiple
            skip_countdown = slot.skip_countdown
        return _SenioritySlot(
            case=slot.case,
            tier=slot.tier,
            reset_multiple=reset_multiple,
            skip_countdown=skip_countdown,
            noop_streak=slot.noop_streak,
            fail_streak=slot.fail_streak,
            choke_wait_streak=slot.choke_wait_streak,
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
            pending_fires=slot.pending_fires,
            active_fire=slot.active_fire,
            last_seen_state=case.case_state,
        )

    def _slot_prelaunch(self, slot: _Slot) -> None:
        if not isinstance(slot, _SenioritySlot):
            return
        spec = slot.case.case_type_spec()
        if (
            slot.last_seen_state != slot.case.case_state
            and not spec.fsm.has_auto_exits(slot.last_seen_state)
        ):
            self._requeue_to_tail(slot)
        slot.last_seen_state = slot.case.case_state

    def _slot_post_step(self, slot: _Slot, result: AdvanceResult) -> None:
        if not isinstance(slot, _SenioritySlot):
            return
        spec = slot.case.case_type_spec()
        if result.progressed and not spec.fsm.has_auto_exits(result.initial_state):
            self._requeue_to_tail(slot)
        slot.last_seen_state = slot.case.case_state
        # After any requeue, apply senior HOT override using current queue order.
        # Base ``_complete_step`` already reclassified and set baseline cadence;
        # terminal/halt dormancy assignments that follow this hook still win.
        if (
            slot.tier is Tier.HOT
            and not slot.terminal
            and self._is_senior(slot.case.case_folder)
        ):
            self._apply_senior_hot(slot)

    def _requeue_to_tail(self, slot: _SenioritySlot) -> None:
        folder = slot.case.case_folder
        if folder not in self._by_folder:
            return
        del self._by_folder[folder]
        self._by_folder[folder] = slot

    def _order_chokeables(self, chokeables):
        """Preserve queue order for choke contention too: the front of the line
        (``_by_folder`` insertion order, which the walk already visited in order)
        holds top claim on choke permits, same as it does for in-flight slots."""
        return chokeables

    def peek(self, case_folder: Path) -> SeniorityCasePeek:
        base = super().peek(case_folder)
        queue_position = list(self._by_folder.keys()).index(case_folder)
        return SeniorityCasePeek(
            case_folder=base.case_folder,
            case_id=base.case_id,
            state=base.state,
            tier=base.tier,
            in_flight=base.in_flight,
            terminal=base.terminal,
            halt_requested=base.halt_requested,
            noop_streak=base.noop_streak,
            fail_streak=base.fail_streak,
            choke_wait_streak=base.choke_wait_streak,
            skip_countdown=base.skip_countdown,
            last_result=base.last_result,
            choked=base.choked,
            pending_fire_count=base.pending_fire_count,
            queue_position=queue_position,
            is_senior=self._is_senior(case_folder),
        )
