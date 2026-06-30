"""Standalone tests for _CaseMachineFactory — instance-time FSM binding.

These exercise the factory directly against a real (tmp-folder) FolderBackedCase, proving
it owns machine construction and the two parser-left conventions: compiling `@FACT<op>N`
factual guards into `conditions`, and wrapping `perform_<trigger>` work as a TIMED `before`
(soft CASE_TRIGGER_SLOW warning, hard TriggerTimeout kill). The override seams the factory
reads back (`trigger_warn_secs`, `case_dwell_secs`) stay on the case."""

import asyncio
import datetime
import os
import time

import pytest
from transitions.extensions.asyncio import AsyncMachine

from totodev_pub.folder_backed_case import FolderBackedCase
import totodev_pub.folder_backed_case as _fbc
import totodev_pub.folder_backed_case_support.case_machine_factory as _cmf
from totodev_pub.folder_backed_case_support.case_machine_factory import _CaseMachineFactory
from totodev_pub.folder_backed_case_support.exceptions import (
    OwnershipLostError, TriggerTimeout,
)
from totodev_pub.folder_backed_case_support.constants import EV_TRIGGER_SLOW, LEASE_NAME
from totodev_pub.folder_backed_case_support.heartbeat_lease import HeartbeatLease


def _use_short_ttl(monkeypatch, ttl=0.3):
    """Shrink the (now-fixed) lease TTL for real-time keepalive tests so a step can outlive an
    un-pulsed window in a fraction of a second. The TTL is no longer a per-case seam, so patch
    BOTH module bindings the running code reads: the lease window (folder_backed_case) and the
    pulse cadence (the factory). The pulse then beats ~every ttl / 3 (the default divisor)."""
    monkeypatch.setattr(_fbc, "DEFAULT_LEASE_TTL_SECS", ttl)
    monkeypatch.setattr(_cmf, "DEFAULT_LEASE_TTL_SECS", ttl)


class _FactoryCase(FolderBackedCase):


    asset_schema = {}
    """Auto edge `go` (with timed/perform work), a manual `finish`, and a @FAIL-guarded
    auto edge `bail` — enough surface to test wiring, fact guards, and the timed wrapper."""

    fsm_state_chains = [
        "^new--go-->open==finish-->done^",
        "open--@FAIL>=2#bail-->failed^",
    ]

    sleep_secs: float = 0.0
    raise_in_perform: bool = False

    async def perform_go(self, tctx):
        if self.raise_in_perform:
            raise ValueError("boom")
        if self.sleep_secs:
            await asyncio.sleep(self.sleep_secs)

    async def perform_bail(self, tctx):
        return None


def _case(tmp_path, name="c"):
    return _FactoryCase.create_case_in_folder(tmp_path / name, case_id=name)


def _factory(case) -> _CaseMachineFactory:
    return _CaseMachineFactory(case, case._fsm, case._journal)


def _slow_events(case) -> list:
    return list(case._journal.primitive.events(label_glob=EV_TRIGGER_SLOW))


def _data_of(event) -> dict:
    payload = event.contents()
    return payload.as_dict() if payload is not None else {}


# ---------------------------------------------------------------------------
# build(): the bound machine
# ---------------------------------------------------------------------------

def test_build_produces_async_machine_bound_to_the_case(tmp_path):
    case = _case(tmp_path)
    try:
        # The case's machine is built by the factory at construction time.
        assert isinstance(case._machine, AsyncMachine)
        assert case in case._machine.models
        assert case.case_state == "new"
    finally:
        case.case_detach()


def test_built_machine_runs_an_auto_step(tmp_path):
    """End-to-end proof the factory wired a usable machine: firing `go` advances."""
    with _case(tmp_path) as case:
        asyncio.run(case.go())
        assert case.case_state == "open"


# ---------------------------------------------------------------------------
# _prepare_transitions(): convention binding + private-key stripping
# ---------------------------------------------------------------------------

def test_prepare_transitions_strips_private_keys(tmp_path):
    case = _case(tmp_path)
    try:
        prepared = _factory(case)._prepare_transitions(case._fsm.transitions)
        for td in prepared:
            assert not any(k.startswith("_") for k in td), td
    finally:
        case.case_detach()


def test_prepare_transitions_wires_perform_into_before(tmp_path):
    case = _case(tmp_path)
    try:
        prepared = _factory(case)._prepare_transitions(case._fsm.transitions)
        go = next(td for td in prepared if td["trigger"] == "go")
        finish = next(td for td in prepared if td["trigger"] == "finish")
        assert callable(go.get("before"))      # perform_go exists -> wrapped
        assert "before" not in finish          # manual edge, no perform_finish
    finally:
        case.case_detach()


def test_prepare_transitions_compiles_fact_guard_conditions(tmp_path):
    case = _case(tmp_path)
    try:
        prepared = _factory(case)._prepare_transitions(case._fsm.transitions)
        bail = next(td for td in prepared if td["trigger"] == "bail")
        # The @FAIL>=2 token became a compiled conditions callable on the edge.
        assert bail.get("conditions")
        assert all(callable(c) for c in bail["conditions"])
    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# _make_fact_guard(): facts sourced from the case + journal
# ---------------------------------------------------------------------------

def test_dwell_fact_guard_reads_case_dwell_secs(tmp_path):
    case = _case(tmp_path)
    try:
        guard = _factory(case)._make_fact_guard("DWELL", ">", 100.0)
        # Fresh case: dwell is tiny -> guard is False.
        assert guard(None) is False
        # Backdate the dwell anchor so case_dwell_secs clears the threshold.
        case._state_entered_at = case._state_entered_at - datetime.timedelta(seconds=500)
        assert guard(None) is True
    finally:
        case.case_detach()


def test_fail_fact_guard_reads_journal_count(tmp_path):
    case = _case(tmp_path)
    try:
        guard = _factory(case)._make_fact_guard("FAIL", ">=", 1)
        assert guard(None) is False            # no failures yet this dwell
        case._journal.log_fail_transition("go", {"trigger": "go"})
        assert guard(None) is True
    finally:
        case.case_detach()


def test_make_fact_guard_rejects_unknown_name(tmp_path):
    case = _case(tmp_path)
    try:
        with pytest.raises(ValueError):
            _factory(case)._make_fact_guard("MYSTERY", ">", 1)
    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# _make_perform_wrapper(): timed, time-bounded work
# ---------------------------------------------------------------------------

def test_perform_wrapper_hard_aborts_with_trigger_timeout(tmp_path):
    case = _case(tmp_path)
    try:
        case.sleep_secs = 0.5
        case.trigger_warn_secs = lambda trigger: 0.01    # kill ceiling = 0.02s
        wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")
        with pytest.raises(TriggerTimeout):
            asyncio.run(wrapped(None))
        # The wrapper raises the timeout; CASE_TRIGGER_TIMEOUT logging is _on_fsm_exception's
        # job, and a hard-abort never doubles as a slow warning.
        assert _slow_events(case) == []
    finally:
        case.case_detach()


def test_perform_wrapper_logs_slow_when_over_soft_but_under_kill(tmp_path):
    case = _case(tmp_path)
    try:
        case.sleep_secs = 0.3
        case.trigger_warn_secs = lambda trigger: 0.2    # soft 0.2s, kill 0.4s -> 0.3s is SLOW
        wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")
        asyncio.run(wrapped(None))                       # completes (no timeout)
        slow = _slow_events(case)
        assert len(slow) == 1
        data = _data_of(slow[0])
        assert data["trigger"] == "go"
        assert data["state"] == "new"
        assert data["warn_secs"] == 0.2
    finally:
        case.case_detach()


def test_perform_wrapper_reraises_non_timeout_error_unchanged(tmp_path):
    case = _case(tmp_path)
    try:
        case.raise_in_perform = True
        wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(wrapped(None))
        # A fast failure is not slow, so nothing is logged by the wrapper.
        assert _slow_events(case) == []
    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# _LeaseKeepalive: the in-flight heartbeat pulse around the work
# ---------------------------------------------------------------------------
# These run on real time (the lease uses time.time()), so they deliberately shrink the fixed
# TTL to ~0.3s via _use_short_ttl (pulse interval then ~0.1s). The work outruns the TTL, so the
# lease would lapse WITHOUT the pulse — that is the property under test.

def _other_tasks() -> list:
    """Live tasks other than the one running the test coroutine (i.e. a leaked pulse)."""
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not current and not t.done()]


def test_keepalive_holds_lease_across_a_slow_step(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)                       # pulse interval defaults to ~0.1s
    async def scenario():
        with _case(tmp_path) as case:
            case.sleep_secs = 0.8                     # work outlives the 0.3s TTL
            lease_path = case.case_folder / LEASE_NAME
            wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")

            task = asyncio.create_task(wrapped(None))
            await asyncio.sleep(0.5)                  # well past one un-pulsed TTL
            # The pulse has been beating, so the lease is still firmly held.
            assert HeartbeatLease.is_expired(lease_path) is False
            await task
            assert HeartbeatLease.is_expired(lease_path) is False

    asyncio.run(scenario())


def test_keepalive_reaps_pulse_on_success(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _case(tmp_path) as case:
            case.sleep_secs = 0.2
            wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")
            await wrapped(None)
            await asyncio.sleep(0)                    # let any cancellation settle
            assert _other_tasks() == []              # no leaked pulse task

    asyncio.run(scenario())


def test_keepalive_reaps_pulse_on_exception(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _case(tmp_path) as case:
            case.raise_in_perform = True             # fails fast inside the keepalive
            wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")
            with pytest.raises(ValueError, match="boom"):
                await wrapped(None)
            await asyncio.sleep(0)
            assert _other_tasks() == []              # pulse still reaped on the error path

    asyncio.run(scenario())


def test_keepalive_surfaces_ownership_loss_and_cancels_work(tmp_path, monkeypatch):
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _case(tmp_path) as case:
            case.sleep_secs = 1.0                     # long; we expect it cancelled early
            lease_path = case.case_folder / LEASE_NAME
            wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")

            started = time.monotonic()
            task = asyncio.create_task(wrapped(None))
            await asyncio.sleep(0.15)                 # let the first pulse beat re-stamp ours
            # Simulate ANOTHER owner overwriting the lease token: the next beat's ownership
            # check will mismatch and raise.
            foreign = time.time() + 999
            os.utime(lease_path, (foreign, foreign))

            with pytest.raises(OwnershipLostError):
                await task
            elapsed = time.monotonic() - started
            assert elapsed < 0.6                     # work was cancelled, not run to 1.0s
            assert _slow_events(case) == []          # an aborted step is not a "slow" step
            assert _other_tasks() == []

    asyncio.run(scenario())


def test_keepalive_survives_transient_beat_errors(tmp_path, monkeypatch):
    """A non-ownership beat failure (e.g. a filesystem hiccup) must not kill the pulse: it
    logs and keeps beating on the next cadence, so the lease stays held once beats recover."""
    _use_short_ttl(monkeypatch)
    async def scenario():
        with _case(tmp_path) as case:
            real_heartbeat = case.case_heartbeat
            calls = {"n": 0}

            def flaky_heartbeat(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] <= 2:                   # first two in-flight beats blow up
                    raise OSError("transient beat failure")
                return real_heartbeat(*args, **kwargs)

            case.case_heartbeat = flaky_heartbeat
            case.sleep_secs = 0.8
            lease_path = case.case_folder / LEASE_NAME
            wrapped = _factory(case)._make_perform_wrapper("go", "perform_go")

            task = asyncio.create_task(wrapped(None))
            await asyncio.sleep(0.6)                   # beats 1-2 fail, later ones recover
            assert calls["n"] >= 3                     # the pulse kept calling after failures
            assert HeartbeatLease.is_expired(lease_path) is False  # recovered beats hold it
            await task

    asyncio.run(scenario())
