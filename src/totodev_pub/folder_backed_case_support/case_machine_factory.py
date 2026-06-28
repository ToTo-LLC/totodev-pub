# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Builds the instance-bound AsyncMachine for a FolderBackedCase.

This helper wires parser conventions at bind time:
- `@FACT<op>N` tokens become `conditions` callables.
- `perform_<trigger>` methods are wrapped as timed `before` callbacks.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import operator
import time
from typing import TYPE_CHECKING

from transitions.extensions.asyncio import AsyncMachine

from totodev_pub.folder_backed_case_support.case_journal import CaseJournal
from totodev_pub.folder_backed_case_support.constants import (
    TIMEOUT_KILL_MULTIPLE_OF_WARNING,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    OwnershipLostError, TriggerTimeout,
)
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    FsmChainSpec,
    _PERFORM_METHOD_PREFIX,
)

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase

logger = logging.getLogger(__name__)

# Comparator name -> callable, for compiling `@FACT<op>N` factual guards into condition
# functions. Equality is intentionally absent (the DSL forbids ==/!=).
_FACT_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class _LeaseKeepalive:
    """Keeps a case's heartbeat lease warm while a trigger's awaited work runs.

    A single coroutine cannot refresh its own lease while it is parked at the `await` of a
    slow `perform_`/`before` step, so this spawns ONE sibling `asyncio.Task` (the "pulse")
    that beats the lease on a short interval alongside the work. That decouples the lease
    TTL (the crash-recovery window) from how long a single, legitimately slow step may run:
    a live, working owner keeps the folder spoken-for no matter how long the step awaits.

    Cooperative only: the pulse advances solely because the work yields the event loop. A
    step that blocks the loop synchronously starves the pulse too (offload such work via
    `case_run_blocking`). The pulse is reaped when the work returns or raises.

    Ownership-loss policy (cancel-and-surface): a beat validates ownership; if the on-disk
    lease is no longer ours (an outside process reclaimed the folder past our TTL), the
    pulse cancels the in-flight work task and `__aexit__` re-raises that `OwnershipLostError`
    in place of the resulting `CancelledError`. An external cancellation (e.g. a driver
    wrapping `case_advance()` in `asyncio.wait_for`) is left untouched and propagates.
    """

    def __init__(self, case: "FolderBackedCase", state: str) -> None:
        self._case = case
        self._interval = max(0.0, case.lease_pulse_interval_for(state))
        self._work_task: asyncio.Task | None = None
        self._pulse: asyncio.Task | None = None
        self._lost: OwnershipLostError | None = None

    async def __aenter__(self) -> "_LeaseKeepalive":
        # The work runs in the task entering this context; the pulse cancels THIS task if it
        # loses ownership. interval <= 0 disables the pulse (see lease_pulse_interval_for).
        self._work_task = asyncio.current_task()
        if self._interval > 0:
            self._pulse = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    self._case.case_heartbeat(min_update_secs=0, validate_ownership=True)
                except OwnershipLostError as e:
                    # FATAL: another owner took the folder. Stop the in-flight work and
                    # surface this on the way out (the lease contract: the displaced owner
                    # must stop). This is the one beat failure we do NOT retry.
                    self._lost = e
                    if self._work_task is not None:
                        self._work_task.cancel()
                    return
                except Exception:
                    # TRANSIENT (e.g. a filesystem hiccup on the beat write): a single miss
                    # must NOT permanently disable the keepalive, so log and keep beating on
                    # the next cadence. With the default interval (TTL / 3) one miss still
                    # leaves margin; a PERSISTENT failure will lapse the lease and then surface
                    # as OwnershipLostError on a later beat, or at the next boundary/pre-step
                    # beat — we deliberately do not abort the (succeeding) work over it. The
                    # leading sleep paces retries, so this cannot busy-spin.
                    logger.warning(
                        "Case %s: in-flight lease beat failed; retrying on next cadence.",
                        getattr(self._case, "case_id", "?"), exc_info=True,
                    )
        except asyncio.CancelledError:
            return

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._pulse is not None:
            self._pulse.cancel()
            await asyncio.gather(self._pulse, return_exceptions=True)
        # If the cancellation in flight is OUR doing (ownership loss), replace it with the
        # real cause; otherwise let whatever happened propagate untouched.
        if self._lost is not None and exc_type is asyncio.CancelledError:
            raise self._lost from None
        return False


class _CaseMachineFactory:
    """Builds an AsyncMachine from a case's compiled `_fsm` spec.

    Internal by convention (leading underscore): not part of any public surface and
    not re-exported from the support package. Don't depend on or use it directly."""

    def __init__(
        self, case: "FolderBackedCase", fsm: FsmChainSpec, journal: CaseJournal
    ) -> None:
        self._case = case
        self._fsm = fsm
        self._journal = journal

    def build(self, initial_state: str) -> AsyncMachine:
        """Constructs the case-bound machine with event and exception hooks enabled."""
        return AsyncMachine(
            model=self._case,
            states=self._fsm.states,
            transitions=self._prepare_transitions(self._fsm.transitions),
            initial=initial_state,
            model_attribute="case_state",
            after_state_change="_on_state_changed",
            on_exception="_on_fsm_exception",
            send_event=True,
        )

    def _prepare_transitions(self, transitions: list[dict]) -> list[dict]:
        """Returns machine-ready transitions.

        - Strips `_`-prefixed parser metadata keys.
        - Compiles `_fact_guards` tokens into condition callables.
        - Wires `perform_<trigger>` into `before` when no explicit `before` exists.
        """
        prepared: list[dict] = []
        for td in transitions:
            trigger = td["trigger"]
            clean = {k: v for k, v in td.items() if not k.startswith("_")}
            fact_guards = td.get("_fact_guards")
            if fact_guards:
                conds = list(clean.get("conditions", []))
                for fg in fact_guards:
                    conds.append(self._make_fact_guard(fg["name"], fg["op"], fg["operand"]))
                clean["conditions"] = conds
            if "before" not in clean:
                method = f"{_PERFORM_METHOD_PREFIX}{trigger}"
                if callable(getattr(type(self._case), method, None)):
                    clean["before"] = self._make_perform_wrapper(trigger, method)
            prepared.append(clean)
        return prepared

    def _make_perform_wrapper(self, trigger: str, method_name: str):
        """Wraps a `perform_<trigger>` method as a timed async `before` callback."""
        case = self._case
        journal = self._journal
        # Backstop advisory dedup: states already warned that their kill ceiling outruns the
        # lease TTL (per state, once for this case's machine — see the check in _wrapped).
        ttl_warned_states: set[str] = set()

        async def _invoke(tctx):
            result = getattr(case, method_name)(tctx)
            if inspect.isawaitable(result):
                result = await result
            return result

        async def _wrapped(tctx):
            state = case.case_state
            warn = case.trigger_warn_secs(trigger)
            kill = warn * TIMEOUT_KILL_MULTIPLE_OF_WARNING
            # Defense-in-depth: if the work could run longer than the lease lives, the pulse
            # normally covers it — but a step that BLOCKS the event loop starves the pulse and
            # the lease can still lapse. Warn once per state so a misconfigured TTL is visible.
            ttl = case.lease_ttl_for(state)
            if kill > ttl and state not in ttl_warned_states:
                ttl_warned_states.add(state)
                logger.warning(
                    "Case %s: trigger %r in state %r has a kill ceiling (%.1fs) above the "
                    "lease TTL (%.1fs). The in-flight keepalive will refresh the lease for "
                    "well-behaved async work, but verify lease_ttl_for/trigger_warn_secs — a "
                    "step that blocks the event loop could still let the lease lapse.",
                    case.case_id, trigger, state, kill, ttl,
                )
            start = time.monotonic()
            completed = False
            try:
                # The keepalive beats the lease for the duration of this (possibly long but
                # kill-bounded) awaited step, so a slow step does not lapse the lease. It
                # only spans the work — guards already ran and passed before `before` fires.
                async with _LeaseKeepalive(case, case.case_state):
                    result = await asyncio.wait_for(_invoke(tctx), kill)
                completed = True
                return result
            except asyncio.TimeoutError:
                raise TriggerTimeout(
                    case.case_id, trigger, case.case_state,
                    elapsed=time.monotonic() - start, ceiling=kill,
                ) from None
            finally:
                # Slow-warn on a completed-but-slow step only. A hard-abort already speaks
                # for itself via CASE_TRIGGER_TIMEOUT, and an ownership-loss cancel (or any
                # other raise) is not a "slow work" condition, so neither should double-log.
                if completed:
                    elapsed = time.monotonic() - start
                    if elapsed > warn:
                        journal.log_trigger_slow(
                            trigger, elapsed=elapsed, warn=warn, state=case.case_state
                        )

        return _wrapped

    def _make_fact_guard(self, name: str, op: str, operand):
        """Builds a `transitions` condition callable for a `@FACT<op>N` guard."""
        cmp = _FACT_OPS[op]
        case = self._case
        journal = self._journal
        if name == "DWELL":
            def _dwell_guard(tctx) -> bool:
                return cmp(case.case_dwell_secs, operand)
            return _dwell_guard
        if name == "FAIL":
            def _fail_guard(tctx) -> bool:
                return cmp(journal.count_fails_this_dwell(), operand)
            return _fail_guard
        raise ValueError(f"unknown factual guard {name!r}")   # parser should prevent this
