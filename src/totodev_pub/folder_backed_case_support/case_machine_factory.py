# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Builds the instance-bound AsyncMachine for a FolderBackedCase.

Wires parser conventions at bind time: ``@FACT<op>N#`` factual guards become
``conditions`` callables; ``perform_<trigger>`` methods become timed ``before``
callbacks with lease keepalive, trigger-start journal markers, and timeouts.
"""

from __future__ import annotations

import asyncio
import inspect
import operator
import time
from typing import TYPE_CHECKING

from transitions.extensions.asyncio import AsyncMachine

from totodev_pub.folder_backed_case_support.case_journal import CaseEventJournal
from totodev_pub.folder_backed_case_support.constants import (
    TIMEOUT_KILL_MULTIPLE_OF_WARNING,
    DEFAULT_LEASE_TTL_SECS,
    LEASE_PULSE_FRACTION_DIVISOR,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    OwnershipLostError, PerformParamsError, TriggerTimeout,
)
from totodev_pub.folder_backed_case_support.perform_signature import bind_perform_kwargs
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    FsmChainSpec,
    _PERFORM_METHOD_PREFIX,
    _is_fact_guard,
    _is_method_guard,
)

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase

# Comparator name -> callable, for compiling `@FACT<op>N` factual guards into condition
# functions. Equality is intentionally absent (the DSL forbids ==/!=).
_FACT_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class _LeaseKeepalive:
    """Keeps the heartbeat lease warm during awaited trigger work.

    Spawns a sibling pulse task that beats the lease while ``perform_``/``before``
    work runs. Cooperative only — blocking the event loop starves the pulse.
    On ``OwnershipLostError``, cancels work and re-raises; external cancellation
    propagates unchanged.
    """

    def __init__(self, case: "FolderBackedCase") -> None:
        self._case = case
        # Fixed cadence derived from the lease TTL (constants.py): two beats before expiry.
        self._interval = DEFAULT_LEASE_TTL_SECS / LEASE_PULSE_FRACTION_DIVISOR
        self._work_task: asyncio.Task | None = None
        self._pulse: asyncio.Task | None = None
        self._lost: OwnershipLostError | None = None

    async def __aenter__(self) -> "_LeaseKeepalive":
        # The work runs in the task entering this context; the pulse cancels THIS task if it
        # loses ownership.
        self._work_task = asyncio.current_task()
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
                    # Tee'd on the case's OWN log (not the module logger): this is a fact
                    # about this case's health, and the case unambiguously still owns the
                    # folder at this point (we are mid in-flight work, lease presumed live —
                    # OwnershipLostError, handled above, is the only case that says otherwise).
                    self._case.log.warning(
                        "in-flight lease beat failed; retrying on next cadence.",
                        exc_info=True,
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
        self, case: "FolderBackedCase", fsm: FsmChainSpec, journal: CaseEventJournal
    ) -> None:
        self._case = case
        self._fsm = fsm
        self._journal = journal

    def build(self, initial_state: str) -> AsyncMachine:
        """Construct the case-bound machine (keepalive, timeouts, journal markers).

        `queued=False` (the library default) is set EXPLICITLY here: this project
        deliberately does not use `transitions`' async event-queueing mode, which would
        silently defer a second concurrent trigger call rather than reject it. The
        non-reentrancy guard (`_on_prepare_fsm_event` / `_on_finalize_fsm_event`, wired
        below) gives immediate, cheap failure instead — see
        CaseTransitionInFlightError."""
        return AsyncMachine(
            model=self._case,
            states=self._fsm.states,
            transitions=self._prepare_transitions(self._fsm.transitions),
            initial=initial_state,
            # Writes the private backing field, not the public `case_state` property
            # (which has no setter) — see FolderBackedCase.case_state.
            model_attribute="_case_state",
            prepare_event="_on_prepare_fsm_event",
            after_state_change="_on_state_changed",
            finalize_event="_on_finalize_fsm_event",
            on_exception="_on_fsm_exception",
            send_event=True,
            queued=False,
        )

    def _prepare_transitions(self, transitions: list[dict]) -> list[dict]:
        """Returns machine-ready transitions.

        - Strips `_`-prefixed parser metadata keys.
        - Compiles `_guards` in declaration order into transitions-library
          `conditions` (method-guard name strings and factual-guard callables).
        - Wires `perform_<trigger>` into `before` when no explicit `before` exists.
        """
        prepared: list[dict] = []
        for td in transitions:
            trigger = td["trigger"]
            clean = {k: v for k, v in td.items() if not k.startswith("_")}
            guards = td.get("_guards") or []
            if guards:
                conds = []
                for item in guards:
                    if _is_method_guard(item):
                        conds.append(item)  # transitions resolves str names on the model
                    elif _is_fact_guard(item):
                        conds.append(
                            self._make_fact_guard(item["name"], item["op"], item["operand"])
                        )
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

        async def _invoke(tctx, bound):
            result = getattr(case, method_name)(tctx, **bound)
            if inspect.isawaitable(result):
                result = await result
            return result

        async def _wrapped(tctx):
            state = case.case_state
            warn = case.trigger_warn_secs(trigger)
            kill = warn * TIMEOUT_KILL_MULTIPLE_OF_WARNING
            # Defense-in-depth: if the work could run longer than the lease lives, the pulse
            # normally covers it — but a step that BLOCKS the event loop starves the pulse and
            # the lease can still lapse. Warn once per state so an over-long trigger budget
            # (its `~<dur>` warning, doubled to the kill ceiling) is visible against the TTL.
            if kill > DEFAULT_LEASE_TTL_SECS and state not in ttl_warned_states:
                ttl_warned_states.add(state)
                # Tee'd on the case's own log: an advisory about THIS case's trigger
                # budget, and the case owns its folder throughout normal dispatch.
                case.log.warning(
                    "trigger %r in state %r has a kill ceiling (%.1fs) above the "
                    "lease TTL (%.1fs). The in-flight keepalive will refresh the lease for "
                    "well-behaved async work, but a step that blocks the event loop could "
                    "still let the lease lapse; consider a shorter trigger_warn_secs.",
                    trigger, state, kill, DEFAULT_LEASE_TTL_SECS,
                )
            # Bind/check kwargs BEFORE journal start so invalid calls never look "started".
            method = getattr(case, method_name)
            raw_kwargs = getattr(tctx, "kwargs", None) or {}
            try:
                bound = bind_perform_kwargs(method, dict(raw_kwargs))
            except PerformParamsError as exc:
                raise PerformParamsError(f"trigger {trigger!r}: {exc}") from None
            # CASE_TRIGGER_STARTED before work (see CaseEventJournal.log_trigger_started).
            journal.log_trigger_started(trigger, state=state, warn=warn, kill=kill)
            start = time.monotonic()
            completed = False
            try:
                # The keepalive beats the lease for the duration of this (possibly long but
                # kill-bounded) awaited step, so a slow step does not lapse the lease. It
                # only spans the work — guards already ran and passed before `before` fires.
                async with _LeaseKeepalive(case):
                    result = await asyncio.wait_for(_invoke(tctx, bound), kill)
                completed = True
                return result
            except asyncio.TimeoutError:
                raise TriggerTimeout(
                    case.case_id, trigger, case.case_state,
                    elapsed=time.monotonic() - start, ceiling=kill,
                ) from None
            finally:
                # Slow-warn on a completed-but-slow step only. A hard-abort already speaks
                # for itself via CASE_TRIGGER_TIMED_OUT, and an ownership-loss cancel (or any
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
