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
import operator
import time
from typing import TYPE_CHECKING

from transitions.extensions.asyncio import AsyncMachine

from totodev_pub.folder_backed_case_support.case_journal import CaseJournal
from totodev_pub.folder_backed_case_support.constants import (
    TIMEOUT_KILL_MULTIPLE_OF_WARNING,
)
from totodev_pub.folder_backed_case_support.exceptions import TriggerTimeout
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    FsmChainSpec,
    _PERFORM_METHOD_PREFIX,
)

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase

# Comparator name -> callable, for compiling `@FACT<op>N` factual guards into condition
# functions. Equality is intentionally absent (the DSL forbids ==/!=).
_FACT_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}

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

        async def _invoke(tctx):
            result = getattr(case, method_name)(tctx)
            if inspect.isawaitable(result):
                result = await result
            return result

        async def _wrapped(tctx):
            warn = case.trigger_warn_secs(trigger)
            kill = warn * TIMEOUT_KILL_MULTIPLE_OF_WARNING
            start = time.monotonic()
            timed_out = False
            try:
                return await asyncio.wait_for(_invoke(tctx), kill)
            except asyncio.TimeoutError:
                timed_out = True
                raise TriggerTimeout(
                    case.case_id, trigger, case.case_state,
                    elapsed=time.monotonic() - start, ceiling=kill,
                ) from None
            finally:
                # Slow-warn on a completed-but-slow OR failed-but-slow step; a hard-abort
                # already speaks for itself via CASE_TRIGGER_TIMEOUT, so don't double-log.
                if not timed_out:
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
