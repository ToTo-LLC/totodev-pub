# Manager Watchdog, Host Entry Point, and Die-Loudly Contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a `CaseManager` process in-process failure detection (watchdog thread + liveness pulse), a blessed host entry point (`serve()`), a disk-only remote shutdown lever (shutdown mailbox), and an external health probe — per the approved proposal in `notebooks/DEVDAVE/case_manager_classes/_backlog/proposed_manager_watchdog_and_host.md`.

**Architecture:** `CaseManager` gains only observability seams (loop hardening, a pulse task, two callback registration methods, three read-only properties). All process-lifecycle behavior lands in new modules: `case_manager_host.py` (serve, signals, exit codes), `case_manager_support/watchdog.py` (daemon thread, kill ladder, death records), `case_manager_support/shutdown.py` (request model + one shared parser). The watchdog never repairs — it detects, diagnoses, and dies; `recover()` on the next start makes hard death ordinary.

**Tech Stack:** Python ≥3.11, asyncio, `threading` (watchdog only), Pydantic v2 + `FileMappedPydanticMixin` (except the death record, which is primitive I/O on purpose), pytest + pytest-asyncio, `uv`.

---

## Context primer (read this first)

You are working in `totodev-pub`, a library for "folder-backed cases": state machines persisted in filesystem folders, coordinated by `CaseManager` (`src/totodev_pub/case_manager.py`, ~965 lines). Key facts you need:

- **The manager loop** (`CaseManager._manager_loop`, `case_manager.py:313-319`) runs three awaits per iteration: `driver.advance()` (case scheduling + lease heartbeats), `_reconcile_terminal_in_pool()`, `_maintenance_tick()` (termination/eject tickets, mailbox intake, manifest heartbeat, escalations). It currently has **no** exception handling — an exception silently kills the task.
- **Everything crosses through shared disk.** Clients (`CaseManagerClient`, `case_manager_client.py`) never talk to the manager process directly. They drop Pydantic-YAML request files into `intake/` dirs under `{cache_root}/.case_manager/` (the "manager dir") and poll `results/{correlation_id}.yaml`. Three request kinds exist: `FireRequest`, `AdoptRequest`, `ReclassifyRequest` (`case_manager_support/mailbox/processor.py:31-62`). Writes are atomic: save to `.{corr}.yaml` (dotfile), then `os.replace()` to `{corr}.yaml`. Intake scans use `glob("*.yaml")`, which skips dotfiles.
- **Liveness is cooperative today.** The manifest (`{manager_dir}/manifest.yaml`, `CaseManagerManifest`) gets `heartbeat_at` stamped at the end of every maintenance tick; clients raise `ManagerNotFreshError` when it's older than `manifest_stale_secs` (30s default). Case leases have a fixed 30s TTL (`DEFAULT_LEASE_TTL_SECS`, `folder_backed_case_support/constants.py:89`).
- **Policy is tiered.** `CaseManagerPolicy` (`case_manager_support/case_manager_policy.py`) has Tier 1 (layout facts, mismatches rejected) and Tier 2 (operational tunables, overridable in memory). `open()`/`attach()` slice kwargs by `tier1_field_names()` / `tier2_field_names()`.
- **Lifecycle:** `CaseManager.open(root, register_types=[...])` constructs; `await manager.recover()` is mandatory before `await manager.start()` (else `RecoverRequiredError`); `await manager.stop(timeout=...)` cancels the loop task, stops the driver, settles in-flight work, and writes `stopped_at` into the manifest.
- **Escalations:** `EscalationRegistry.emit_simple(kind, case_id, case_folder, detail)` coerces string kinds through the `CaseEscalationKind` enum (`case_manager_support/escalation.py`) — unknown strings raise `ValueError`.
- **Tests:** pytest + `@pytest.mark.asyncio`. `tests/case_manager_test_utils.py` provides `provision_manager(tmp_path, **overrides)` (builds a manager against `tmp_path/"cache"` with `maintenance_interval_secs=0.01`), `seed_detached_case`, `adopt_into_live`. Run tests with:

```bash
source .venv/bin/activate
PYTHONPATH=src pytest tests/<file>.py -v
```

**Recommended:** execute this plan on a clean branch/worktree — the working tree currently has unrelated modifications to `folder_backed_case*.py` and several tests.

## Design decisions adopted (closing the proposal's open questions)

| Question | Decision in this plan |
|---|---|
| Final numbers (open Q3) | Pulse interval 0.5s; pulse-stuck `max(10×interval, 5s)`; mailbox-stale `max(10×maintenance_interval_secs, 30s)`; tick-warn `min(lease_TTL, manifest_stale_secs)/2`; `IMMEDIATE_SHUTDOWN_GRACE_SECS = 2.0`; `stop_grace_secs` default 30.0 — all as proposed. |
| Packaging (open Q4) | Console scripts. `totodev-manager-health` via `[project.scripts]` in `pyproject.toml` (the repo has none today; this sets the convention). The zero-code host CLI stays follow-on per Resolved decision #7. |
| `watchdog_mailbox_stale_secs` "null-able to disable" | `None` → derived default; `0` → check disabled. (The proposal wanted `null` to mean both; it can't, so `0` disables.) |
| Escalation kind for the tick-duration alarm | One enum member, `MANAGER_UNRESPONSIVE`, for all watchdog escalations; the `detail` message names which check fired. Keeps the enum surface minimal per Resolved decision #4's "small stable family" spirit. |
| Nonzero exit mechanism in `serve()` | All nonzero exits funnel through a module-level `_hard_exit(code)` wrapper around `os._exit` (monkeypatch seam for tests). Exit 0 = `serve()` returning normally. |
| Open Q1 (tick awaiting case steps inline) & Q2 (lease renewal cadence) | Out of scope, exactly as the proposal says — the pulse task + alarm-only tick check contain the hazard. |

## File structure

| File | Status | Responsibility |
|---|---|---|
| `src/totodev_pub/case_manager.py` | modify | §1 loop hardening + `on_loop_failure` seam; §2 pulse task + liveness stamps (retiring `_last_maintenance`); `on_shutdown_request` seam; `recovered`/`running`/`is_idle` read-only properties. **Never imports `signal`, `threading`, `faulthandler`, never defines an exit code.** |
| `src/totodev_pub/case_manager_support/case_manager_policy.py` | modify | Tier 1: `shutdown_mailbox_subdir`. Tier 2: `watchdog_enabled`, `watchdog_action`, `watchdog_pulse_stuck_secs`, `watchdog_mailbox_stale_secs`, `watchdog_tick_warn_secs`. |
| `src/totodev_pub/case_manager_support/constants.py` | modify | `PULSE_INTERVAL_SECS`, `DEFAULT_SHUTDOWN_MAILBOX_SUBDIR`. |
| `src/totodev_pub/case_manager_support/shutdown.py` | **new** | `ShutdownRequest` / `ShutdownAck` models, `ShutdownDirective`, the SIGTERM filename-token convention, intake scan/parse, write/ack/discard helpers. One parser, one precedence rule, zero drift. |
| `src/totodev_pub/case_manager_support/watchdog.py` | **new** | `ManagerWatchdog` daemon thread, its five checks, the kill ladder, death-record write/read helpers. |
| `src/totodev_pub/case_manager_host.py` | **new** | `serve()` (incl. `stop_when`/`stop_when_empty`), signal wiring, `EXIT_WATCHDOG`/`EXIT_RESTART_REQUESTED`, watchdog arm/park orchestration, shutdown-request execution (drain vs. immediate). |
| `src/totodev_pub/case_manager_support/mailbox/processor.py` | modify | `shutdown_intake()` path helper, `submit_shutdown()`, cooperative shutdown pickup in `maintenance_tick()`, `poll_result` shutdown-ack branch. |
| `src/totodev_pub/case_manager_client.py` | modify | `submit_shutdown()` thin delegation. |
| `src/totodev_pub/case_manager_support/escalation.py` | modify | `MANAGER_UNRESPONSIVE` enum member. |
| `src/totodev_pub/case_manager_support/recover.py` | modify | Death-record surfacing; stale shutdown-request discard; two new `RecoverReport` fields. |
| `src/totodev_pub/case_manager_support/case_manager_manifest.py` | modify | Optional `shutdown_mailbox_intake` path (back-compat like `reclassify_mailbox_intake`). |
| `src/totodev_pub/cli/manager_health.py` | **new** | Health probe: read manifest file directly, exit 0/1/2/3. No `CaseManagerClient`. |
| `pyproject.toml` | modify | `[project.scripts]` entry for `totodev-manager-health`. |
| `tests/test_case_manager_loop_hardening.py`, `tests/test_case_manager_pulse.py`, `tests/test_case_manager_properties.py`, `tests/test_shutdown_protocol.py`, `tests/test_manager_watchdog.py`, `tests/test_case_manager_death_records.py`, `tests/test_case_manager_shutdown_mailbox.py`, `tests/test_case_manager_host_serve.py`, `tests/test_manager_health_cli.py` | **new** | One test module per task, following `case_manager_test_utils` conventions. |

---

### Task 1: Loop hardening + `on_loop_failure` seam (§1 — independent, do-first fix)

**Files:**
- Modify: `src/totodev_pub/case_manager.py` (loop at 313–319, `__init__` at ~121, `stop()` at ~292–298)
- Test: `tests/test_case_manager_loop_hardening.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_case_manager_loop_hardening.py`:

```python
# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import provision_manager


@pytest.mark.asyncio
async def test_loop_failure_invokes_callback_after_three_consecutive(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    boom = RuntimeError("tick exploded")

    async def bad_tick():
        raise boom

    monkeypatch.setattr(manager, "_maintenance_tick", bad_tick)
    failures = []
    manager.on_loop_failure(failures.append)
    await manager.start()
    for _ in range(300):
        if failures:
            break
        await asyncio.sleep(0.02)
    assert failures == [boom]
    assert manager._run_task.done()  # loop returned after handing off
    await manager.stop()


@pytest.mark.asyncio
async def test_loop_failure_without_callback_reraises_loudly(tmp_path, monkeypatch, caplog):
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def bad_tick():
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(manager, "_maintenance_tick", bad_tick)
    await manager.start()
    for _ in range(300):
        if manager._run_task.done():
            break
        await asyncio.sleep(0.02)
    assert manager._run_task.done()
    assert isinstance(manager._run_task.exception(), RuntimeError)
    assert any("Manager loop iteration failed" in r.message for r in caplog.records)
    # A dead loop task must not abort a deliberate stop().
    await manager.stop()


@pytest.mark.asyncio
async def test_loop_recovers_from_transient_failures(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    await manager.recover()
    calls = {"n": 0}
    original_tick = manager._maintenance_tick

    async def flaky_tick():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient")
        await original_tick()

    monkeypatch.setattr(manager, "_maintenance_tick", flaky_tick)
    failures = []
    manager.on_loop_failure(failures.append)
    await manager.start()
    for _ in range(300):
        if calls["n"] >= 5:
            break
        await asyncio.sleep(0.02)
    assert calls["n"] >= 5          # loop kept going past the two failures
    assert failures == []           # a successful iteration reset the count
    assert not manager._run_task.done()
    await manager.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_case_manager_loop_hardening.py -v`
Expected: FAIL — `AttributeError: 'CaseManager' object has no attribute 'on_loop_failure'`.

- [ ] **Step 3: Implement**

In `src/totodev_pub/case_manager.py`:

(a) Below `_TIER2_KWARGS` (line 94), add:

```python
# §1 loop hardening: consecutive failed loop iterations before the manager
# gives up retrying and hands the failure to the host (or re-raises).
_LOOP_FAILURE_LIMIT = 3
```

(b) In `__init__`, next to `self._run_task ...` (line 123), add:

```python
        self._loop_failure_cb: Callable[[BaseException], None] | None = None
```

(c) Replace `_manager_loop` (lines 313–319) with:

```python
    async def _manager_loop(self) -> None:
        interval = self._policy.maintenance_interval_secs
        consecutive_failures = 0
        while self._running and not self._stopping:
            try:
                await self._driver.advance(suggested_interval_secs=interval)
                self._reconcile_terminal_in_pool()
                await self._maintenance_tick()
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                logger.exception(
                    "Manager loop iteration failed (%d consecutive of %d allowed)",
                    consecutive_failures,
                    _LOOP_FAILURE_LIMIT,
                )
                if consecutive_failures >= _LOOP_FAILURE_LIMIT:
                    if self._loop_failure_cb is not None:
                        logger.error(
                            "Manager loop giving up after %d consecutive failures; "
                            "invoking on_loop_failure",
                            consecutive_failures,
                        )
                        self._loop_failure_cb(exc)
                        return
                    raise
            await asyncio.sleep(interval)
```

(d) Next to `on_escalation` (line 708), add the registration seam:

```python
    def on_loop_failure(self, callback: Callable[[BaseException], None]) -> None:
        """Register the single host callback invoked when the manager loop gives
        up after repeated consecutive failures. A host (``serve()``) wires this
        to the watchdog's kill ladder; with no callback registered the loop
        re-raises instead (embedded usage — logged loudly, task dies)."""
        self._loop_failure_cb = callback
```

(e) In `stop()`, the `await self._run_task` block (lines 292–298) must tolerate a task that already died with an exception (it was already logged by the loop):

```python
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # The loop already logged its own failure before dying; a dead
                # loop task must not abort a deliberate stop().
                logger.exception("Manager loop task had already died; continuing stop()")
            self._run_task = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_loop_hardening.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the existing case-manager suite to check for regressions**

Run: `PYTHONPATH=src pytest tests/test_case_manager_stop.py tests/test_case_manager_fire_mailbox.py tests/test_case_manager_recover_reap.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/totodev_pub/case_manager.py tests/test_case_manager_loop_hardening.py
git commit -m "feat: make manager loop death loud with retry policy and on_loop_failure seam"
```

---

### Task 2: Liveness pulse task + tick stamps (§2)

**Files:**
- Modify: `src/totodev_pub/case_manager_support/constants.py`
- Modify: `src/totodev_pub/case_manager.py` (`__init__`, `start()`, `stop()`, `_maintenance_tick()`)
- Test: `tests/test_case_manager_pulse.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_case_manager_pulse.py`:

```python
# Part of the totodev_pub library.

import asyncio

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME


def _heartbeat(manager) -> str | None:
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    return manifest.heartbeat_at


@pytest.mark.asyncio
async def test_pulse_survives_a_blocked_tick(tmp_path, monkeypatch):
    # Speed the pulse up so the test stays fast.
    monkeypatch.setattr("totodev_pub.case_manager.PULSE_INTERVAL_SECS", 0.02)
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def stuck_tick():
        await asyncio.sleep(3600)  # a legitimately long awaited tick

    monkeypatch.setattr(manager, "_maintenance_tick", stuck_tick)
    await manager.start()
    await asyncio.sleep(0.1)
    pulse_1, hb_1 = manager._last_pulse, _heartbeat(manager)
    await asyncio.sleep(0.15)
    pulse_2, hb_2 = manager._last_pulse, _heartbeat(manager)
    assert pulse_2 > pulse_1                     # loop turning under the stuck tick
    assert hb_1 is not None and hb_2 is not None # heartbeat decoupled from tick duration
    await manager.stop()
    assert manager._pulse_task is None


@pytest.mark.asyncio
async def test_tick_stamps_recorded(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    assert manager._last_tick_started is None
    await manager.start()
    for _ in range(200):
        if manager._last_tick_completed is not None:
            break
        await asyncio.sleep(0.02)
    assert manager._last_tick_started is not None
    assert manager._last_tick_completed is not None
    assert manager._last_tick_completed >= manager._last_tick_started
    await manager.stop()


def test_fossil_last_maintenance_removed(tmp_path):
    manager = provision_manager(tmp_path)
    assert not hasattr(manager, "_last_maintenance")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_case_manager_pulse.py -v`
Expected: FAIL — no `PULSE_INTERVAL_SECS` attribute on `totodev_pub.case_manager`.

- [ ] **Step 3: Implement**

(a) In `src/totodev_pub/case_manager_support/constants.py`, after `MANIFEST_PROTOCOL_VERSION`:

```python
# §2 liveness pulse: cadence of the manager's sibling pulse coroutine — the
# watchdog's kill-authorized signal. A blocked/starved event loop silences the
# pulse within one interval.
PULSE_INTERVAL_SECS = 0.5
```

(b) In `src/totodev_pub/case_manager.py`:

- Add `import time` to the stdlib imports at the top (next to `import shutil`), and remove the local `import time` at the top of `_maintenance_tick` (line 322).
- Add `PULSE_INTERVAL_SECS` to the existing `from totodev_pub.case_manager_support.constants import (...)` block.
- In `__init__`, **replace** `self._last_maintenance: float = 0.0` (line 126) with:

```python
        # §2 liveness stamps (read by the watchdog thread; write-only here).
        self._last_pulse: float | None = None
        self._last_tick_started: float | None = None
        self._last_tick_completed: float | None = None
        self._last_heartbeat_write: float = 0.0
        self._pulse_task: asyncio.Task[None] | None = None
```

- Add the pulse coroutine directly below `_manager_loop`:

```python
    async def _pulse_loop(self) -> None:
        """Liveness pulse — measures the event loop, not the tick.

        Stamps ``_last_pulse`` every ``PULSE_INTERVAL_SECS`` (the watchdog's
        kill-authorized signal) and writes the manifest heartbeat on its own
        cadence, so a healthy manager doing one slow mailbox fire never looks
        stale to clients."""
        while self._running:
            now = time.monotonic()
            self._last_pulse = now
            if now - self._last_heartbeat_write >= self._policy.maintenance_interval_secs:
                self._last_heartbeat_write = now
                self._write_manifest(running=True)
            await asyncio.sleep(PULSE_INTERVAL_SECS)
```

- In `start()`, replace the final line `self._run_task = asyncio.create_task(self._manager_loop())` with:

```python
        self._last_pulse = time.monotonic()
        self._run_task = asyncio.create_task(self._manager_loop())
        self._pulse_task = asyncio.create_task(self._pulse_loop())
```

- In `stop()`, immediately after `self._running = False` (line 279), cancel the pulse first so it cannot overwrite the stopped manifest:

```python
        if self._pulse_task is not None:
            self._pulse_task.cancel()
            try:
                await self._pulse_task
            except asyncio.CancelledError:
                pass
            self._pulse_task = None
```

- In `_maintenance_tick()`: first line becomes `self._last_tick_started = time.monotonic()`; **delete** the `self._write_manifest(running=True)` line (the heartbeat now lives on the pulse); append `self._last_tick_completed = time.monotonic()` as the final line, after `self._detect_escalations()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_pulse.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full case-manager suite** (the heartbeat write moved — client-freshness tests must still pass)

Run: `PYTHONPATH=src pytest tests/test_case_manager_client_reads.py tests/ -k "case_manager" -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/totodev_pub/case_manager.py src/totodev_pub/case_manager_support/constants.py tests/test_case_manager_pulse.py
git commit -m "feat: add liveness pulse task; move manifest heartbeat off the tick; add tick stamps"
```

---

### Task 3: Public read-only properties `recovered`, `running`, `is_idle`

**Files:**
- Modify: `src/totodev_pub/case_manager.py`
- Test: `tests/test_case_manager_properties.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_case_manager_properties.py`:

```python
# Part of the totodev_pub library.

import pytest

from case_manager_test_utils import TicketCase, adopt_into_live, provision_manager, seed_detached_case


@pytest.mark.asyncio
async def test_lifecycle_properties(tmp_path):
    manager = provision_manager(tmp_path)
    assert manager.recovered is False
    assert manager.running is False
    await manager.recover()
    assert manager.recovered is True
    await manager.start()
    assert manager.running is True
    await manager.stop()
    assert manager.running is False


@pytest.mark.asyncio
async def test_is_idle(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    assert manager.is_idle is True
    # A pooled case means not idle.
    staging = tmp_path / "staging"
    staging.mkdir()
    seed_detached_case(TicketCase, staging / "c1")
    await adopt_into_live(manager, staging / "c1")
    assert manager.is_idle is False


@pytest.mark.asyncio
async def test_is_idle_false_with_pending_intake(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    intake = manager._mailbox.fire_intake()
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "req.yaml").write_text("pending: true\n", encoding="utf-8")
    assert manager.is_idle is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_case_manager_properties.py -v`
Expected: FAIL — `AttributeError: ... 'recovered'`.

- [ ] **Step 3: Implement**

In `src/totodev_pub/case_manager.py`, directly above the `# Lifecycle` section header (~line 241), add:

```python
    # ------------------------------------------------------------------
    # Read-only lifecycle introspection (host/observability surface)
    # ------------------------------------------------------------------

    @property
    def recovered(self) -> bool:
        """True once recover() has completed (start() precondition)."""
        return self._recovered

    @property
    def running(self) -> bool:
        """True between start() and stop()."""
        return self._running

    @property
    def is_idle(self) -> bool:
        """No pooled cases and no pending mailbox intake (§8 self-completion)."""
        if len(self._driver) > 0:
            return False
        for intake in (
            self._mailbox.fire_intake(),
            self._mailbox.adopt_intake(),
            self._mailbox.reclassify_intake(),
        ):
            if intake.exists() and any(intake.glob("*.yaml")):
                return False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_properties.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/totodev_pub/case_manager.py tests/test_case_manager_properties.py
git commit -m "feat: promote recovered/running to public read-only properties; add is_idle"
```

---

### Task 4: Policy and constants — shutdown mailbox subdir + watchdog knobs

**Files:**
- Modify: `src/totodev_pub/case_manager_support/constants.py`
- Modify: `src/totodev_pub/case_manager_support/case_manager_policy.py`
- Test: extend `tests/test_case_manager_policy.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_case_manager_policy.py`:

```python
def test_watchdog_and_shutdown_policy_fields():
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

    policy = CaseManagerPolicy()
    assert policy.shutdown_mailbox_subdir == "shutdown_mailbox"
    assert policy.watchdog_enabled is True
    assert policy.watchdog_action == "exit"
    assert policy.watchdog_pulse_stuck_secs is None
    assert policy.watchdog_mailbox_stale_secs is None
    assert policy.watchdog_tick_warn_secs is None
    assert "shutdown_mailbox_subdir" in CaseManagerPolicy.tier1_field_names()
    for name in (
        "watchdog_enabled",
        "watchdog_action",
        "watchdog_pulse_stuck_secs",
        "watchdog_mailbox_stale_secs",
        "watchdog_tick_warn_secs",
    ):
        assert name in CaseManagerPolicy.tier2_field_names()
    tuned = policy.apply_tier2_overrides(watchdog_pulse_stuck_secs=2.5)
    assert tuned.watchdog_pulse_stuck_secs == 2.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_case_manager_policy.py -v`
Expected: the new test FAILS with `AttributeError: 'CaseManagerPolicy' object has no attribute 'shutdown_mailbox_subdir'`; existing tests pass.

- [ ] **Step 3: Implement**

(a) `constants.py` — after `DEFAULT_RECLASSIFY_MAILBOX_SUBDIR`:

```python
DEFAULT_SHUTDOWN_MAILBOX_SUBDIR = "shutdown_mailbox"
```

(b) `case_manager_policy.py`:

- Add `DEFAULT_SHUTDOWN_MAILBOX_SUBDIR` to the constants import block.
- Tier 1 section, after `reclassify_mailbox_subdir`:

```python
    shutdown_mailbox_subdir: str = DEFAULT_SHUTDOWN_MAILBOX_SUBDIR
```

- Tier 2 section, after `fleet_status_terminal_retention_secs`:

```python
    # Watchdog (§3). watchdog_enabled PERMITS a watchdog; only a host
    # (serve()) ever STARTS one — start() never does.
    watchdog_enabled: bool = True
    watchdog_action: str = "exit"  # "exit" | "alarm_only"
    watchdog_pulse_stuck_secs: Optional[float] = None    # None → max(10×pulse, 5s)
    watchdog_mailbox_stale_secs: Optional[float] = None  # None → max(10×tick, 30s); 0 → disabled
    watchdog_tick_warn_secs: Optional[float] = None      # None → min(lease_TTL, stale)/2; alarm-only always
```

- Add `"shutdown_mailbox_subdir"` to `tier1_field_names()` and the five `watchdog_*` names to `tier2_field_names()`.

Note: existing on-disk policy files lack these fields; Pydantic defaults fill them on load, so no `POLICY_SCHEMA_VERSION` bump is needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_policy.py tests/test_case_manager_provision_attach_open.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/totodev_pub/case_manager_support/constants.py src/totodev_pub/case_manager_support/case_manager_policy.py tests/test_case_manager_policy.py
git commit -m "feat: add shutdown-mailbox subdir (tier 1) and watchdog policy knobs (tier 2)"
```

---

### Task 5: `shutdown.py` — one parser, one precedence rule (§6 protocol core)

**Files:**
- Create: `src/totodev_pub/case_manager_support/shutdown.py`
- Test: `tests/test_shutdown_protocol.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shutdown_protocol.py`:

```python
# Part of the totodev_pub library.

from totodev_pub.case_manager_support.shutdown import (
    ShutdownRequest,
    discard_stale_requests,
    scan_shutdown_intake,
    write_shutdown_request,
)


def test_empty_intake_yields_none(tmp_path):
    assert scan_shutdown_intake(tmp_path / "missing") is None
    (tmp_path / "intake").mkdir()
    assert scan_shutdown_intake(tmp_path / "intake") is None


def test_hand_touched_file_means_immediate(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "please_stop").touch()
    directive = scan_shutdown_intake(intake)
    assert directive is not None
    assert directive.graceful is False
    assert directive.correlation_id is None


def test_sigterm_filename_token_means_graceful(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "drain_SIGTERM_now.txt").touch()
    assert scan_shutdown_intake(intake).graceful is True
    (intake / "drain_SIGTERM_now.txt").unlink()
    (intake / "sigterm_lowercase").touch()  # case-insensitive
    assert scan_shutdown_intake(intake).graceful is True


def test_parsed_content_is_authoritative_over_filename(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    req = ShutdownRequest(correlation_id="c1", requested_at="2026-07-09T00:00:00Z", graceful=False)
    req.save(str(intake / "SIGTERM_but_content_says_immediate.yaml"), retain_lock=False)
    directive = scan_shutdown_intake(intake)
    assert directive.graceful is False        # content wins
    assert directive.correlation_id == "c1"


def test_dotfiles_are_invisible(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / ".half-written.yaml").touch()
    assert scan_shutdown_intake(intake) is None


def test_write_shutdown_request_atomic_shape(tmp_path):
    intake = tmp_path / "intake"
    corr, path = write_shutdown_request(intake, graceful=True, reason="drain please")
    assert path.parent == intake
    assert not path.name.startswith(".")
    assert "SIGTERM" in path.name             # discoverable even without parsing
    directive = scan_shutdown_intake(intake)
    assert directive.graceful is True
    assert directive.reason == "drain please"
    assert directive.correlation_id == corr


def test_discard_stale_requests(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "stale_one").touch()
    (intake / "stale_two.yaml").touch()
    (intake / ".tmp-ignored").touch()
    assert discard_stale_requests(intake) == 2
    assert scan_shutdown_intake(intake) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_shutdown_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'totodev_pub.case_manager_support.shutdown'`.

- [ ] **Step 3: Implement**

Create `src/totodev_pub/case_manager_support/shutdown.py`:

```python
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Shutdown-request protocol (§6) — shared by the mailbox processor (cooperative
pickup), the watchdog (wedged pickup), and CaseManagerClient.submit_shutdown().
One parser, one precedence rule, zero drift.

Protocol: ANY non-hidden file in the shutdown mailbox's intake/ dir triggers a
shutdown. Parsed file content is authoritative; the SIGTERM filename token
applies only to unparseable or hand-touched files. The mailbox is a recovery
lever, not a decommission lever — every mailbox-triggered exit is nonzero.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

logger = logging.getLogger(__name__)

SHUTDOWN_PROTOCOL_VERSION = 1
# Case-insensitive filename substring that requests a graceful drain on files
# whose content does not parse as a ShutdownRequest.
GRACEFUL_FILENAME_TOKEN = "sigterm"


class ShutdownRequest(BaseModel, FileMappedPydanticMixin):
    protocol_version: int = SHUTDOWN_PROTOCOL_VERSION
    correlation_id: str
    requested_at: str
    graceful: bool = False
    reason: str | None = None


class ShutdownAck(BaseModel, FileMappedPydanticMixin):
    """Result written (graceful path only) before the drain begins, so
    poll_result/wait_result resolve normally. On the immediate path the handle
    may never resolve — the manifest's stopped_at is the real confirmation."""

    kind: str = "shutdown"
    correlation_id: str
    acknowledged_at: str
    graceful: bool
    reason: str | None = None


@dataclass(frozen=True)
class ShutdownDirective:
    """Normalized pickup result, whichever path (tick or watchdog) saw it."""

    graceful: bool
    reason: str | None
    correlation_id: str | None
    source_path: Path


def shutdown_intake_dir(manager_dir: Path, policy) -> Path:
    return manager_dir / policy.shutdown_mailbox_subdir / "intake"


def scan_shutdown_intake(intake: Path) -> ShutdownDirective | None:
    """Return a directive for the first non-hidden file in intake/, or None.

    "Non-hidden" is load-bearing: the structured API writes a dotfile and
    os.replace()s it into place; counting dotfiles would fire on a
    half-written request."""
    if not intake.exists():
        return None
    entries = sorted(
        p for p in intake.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    if not entries:
        return None
    path = entries[0]
    try:
        req = ShutdownRequest.load(str(path), acquire_lock=False)
        # Precedence rule: parsed content is authoritative; filename ignored.
        return ShutdownDirective(
            graceful=req.graceful,
            reason=req.reason,
            correlation_id=req.correlation_id,
            source_path=path,
        )
    except Exception:
        return ShutdownDirective(
            graceful=GRACEFUL_FILENAME_TOKEN in path.name.lower(),
            reason=None,
            correlation_id=None,
            source_path=path,
        )


def write_shutdown_request(
    intake: Path,
    *,
    graceful: bool = False,
    reason: str | None = None,
    correlation_id: str | None = None,
) -> tuple[str, Path]:
    """Structured writer (used by MailboxProcessor.submit_shutdown). Returns
    (correlation_id, final_path)."""
    intake.mkdir(parents=True, exist_ok=True)
    corr = correlation_id or str(uuid.uuid4())
    req = ShutdownRequest(
        correlation_id=corr,
        requested_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        graceful=graceful,
        reason=reason,
    )
    # Content is authoritative, but carry the token in the name anyway so a
    # human ls'ing the intake dir can read the intent.
    name = f"{corr}.SIGTERM.yaml" if graceful else f"{corr}.yaml"
    tmp = intake / f".{name}"
    final = intake / name
    req.save(str(tmp), retain_lock=False)
    os.replace(tmp, final)
    return corr, final


def write_shutdown_ack(results_dir: Path, directive: ShutdownDirective) -> None:
    if directive.correlation_id is None:
        return  # hand-touched file; nobody is polling a handle
    ack = ShutdownAck(
        correlation_id=directive.correlation_id,
        acknowledged_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        graceful=directive.graceful,
        reason=directive.reason,
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    tmp = results_dir / f".{directive.correlation_id}.yaml"
    final = results_dir / f"{directive.correlation_id}.yaml"
    ack.save(str(tmp), retain_lock=False)
    os.replace(tmp, final)


def discard_stale_requests(intake: Path) -> int:
    """Startup hygiene (§6): a request still in intake/ at recover() belongs to
    a previous process and must be logged and discarded, never honored —
    otherwise one stale request induces a restart-immediately loop."""
    if not intake.exists():
        return 0
    count = 0
    for path in sorted(intake.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            logger.warning(
                "Discarding stale shutdown request %s found at recover(); "
                "shutdown requests are never honored across a restart.",
                path.name,
            )
            path.unlink(missing_ok=True)
            count += 1
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_shutdown_protocol.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/totodev_pub/case_manager_support/shutdown.py tests/test_shutdown_protocol.py
git commit -m "feat: add shutdown-request protocol module (model, scan, token convention, ack)"
```

---

### Task 6: `ManagerWatchdog` — checks, kill ladder, death records (§3)

**Files:**
- Modify: `src/totodev_pub/case_manager_support/escalation.py` (one enum member)
- Create: `src/totodev_pub/case_manager_support/watchdog.py`
- Test: `tests/test_manager_watchdog.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_manager_watchdog.py`:

```python
# Part of the totodev_pub library.

import asyncio
import os
import time

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.shutdown import shutdown_intake_dir, write_shutdown_request
from totodev_pub.case_manager_support.watchdog import (
    ManagerWatchdog,
    log_recent_death_records,
    write_death_record,
)


class ExitRecorder:
    def __init__(self):
        self.codes = []

    def __call__(self, code):
        self.codes.append(code)


def _make_watchdog(manager, loop, exit_fn, **kwargs):
    kwargs.setdefault("check_interval_secs", 0.02)
    kwargs.setdefault("stop_grace_secs", 1.0)
    return ManagerWatchdog(manager, loop=loop, exit_code=70, exit_fn=exit_fn, **kwargs)


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_pulse_stuck_triggers_kill_ladder(tmp_path):
    manager = provision_manager(tmp_path, watchdog_pulse_stuck_secs=0.1)
    await manager.recover()
    escalations = []
    manager.on_escalation(escalations.append)
    # Simulate a running manager whose pulse went silent long ago.
    manager._running = True
    manager._last_pulse = time.monotonic() - 99.0
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog._armed_at = time.monotonic() - 99.0  # skip the arm-time grace for the test
    assert await _wait_for(lambda: exit_fn.codes)
    dog.stop()
    manager._running = False
    assert exit_fn.codes == [70]
    death_records = list(manager._manager_dir.glob("manager_death_*.yaml"))
    assert len(death_records) == 1
    body = death_records[0].read_text(encoding="utf-8")
    assert "check: pulse_stuck" in body
    assert escalations and escalations[0].kind.value == "MANAGER_UNRESPONSIVE"


@pytest.mark.asyncio
async def test_loop_task_death_detected(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    async def doomed():
        raise RuntimeError("dead loop")

    manager._running = True
    manager._last_pulse = time.monotonic()  # pulse looks healthy

    task = asyncio.get_running_loop().create_task(doomed())
    await asyncio.sleep(0)  # let it die
    await asyncio.sleep(0.05)
    manager._run_task = task
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    # Keep the fake pulse fresh so only the task-death check can fire.
    ok = False
    for _ in range(200):
        manager._last_pulse = time.monotonic()
        if exit_fn.codes:
            ok = True
            break
        await asyncio.sleep(0.02)
    dog.stop()
    manager._running = False
    manager._run_task = None
    assert ok and exit_fn.codes == [70]


@pytest.mark.asyncio
async def test_shutdown_request_pickup_wins_and_parks(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    manager._running = True
    picked = []
    exit_fn = ExitRecorder()
    dog = _make_watchdog(
        manager,
        asyncio.get_running_loop(),
        exit_fn,
        on_shutdown_request=picked.append,
    )
    intake = shutdown_intake_dir(manager._manager_dir, manager._policy)
    write_shutdown_request(intake, graceful=True, reason="test")
    dog.arm()
    ok = False
    for _ in range(200):
        manager._last_pulse = time.monotonic()
        if picked:
            ok = True
            break
        await asyncio.sleep(0.02)
    dog.stop()
    manager._running = False
    assert ok
    assert picked[0].graceful is True
    assert exit_fn.codes == []          # a shutdown request is not a failure
    assert dog._parked.is_set()         # choreography owns the process now
    assert not any(p for p in intake.iterdir() if not p.name.startswith("."))


@pytest.mark.asyncio
async def test_parked_watchdog_never_fires(tmp_path):
    manager = provision_manager(tmp_path, watchdog_pulse_stuck_secs=0.05)
    await manager.recover()
    manager._running = True
    manager._last_pulse = time.monotonic() - 99.0
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog.park()
    await asyncio.sleep(0.3)
    dog.stop()
    manager._running = False
    assert exit_fn.codes == []


@pytest.mark.asyncio
async def test_alarm_only_diagnoses_without_dying(tmp_path):
    manager = provision_manager(
        tmp_path, watchdog_action="alarm_only", watchdog_pulse_stuck_secs=0.05
    )
    await manager.recover()
    escalations = []
    manager.on_escalation(escalations.append)
    manager._running = True
    manager._last_pulse = time.monotonic() - 99.0
    exit_fn = ExitRecorder()
    dog = _make_watchdog(manager, asyncio.get_running_loop(), exit_fn)
    dog.arm()
    dog._armed_at = time.monotonic() - 99.0
    assert await _wait_for(lambda: dog._parked.is_set())
    dog.stop()
    manager._running = False
    assert exit_fn.codes == []
    assert not list(manager._manager_dir.glob("manager_death_*.yaml"))
    assert escalations  # diagnosed loudly, did not die


def test_death_record_roundtrip(tmp_path, caplog):
    path = write_death_record(tmp_path, check="pulse_stuck", reason="test wedge")
    assert path.name.startswith("manager_death_")
    assert path.name.endswith(".yaml")
    assert "check: pulse_stuck" in path.read_text(encoding="utf-8")
    import logging

    with caplog.at_level(logging.WARNING):
        count = log_recent_death_records(tmp_path)
    assert count == 1
    assert any("pulse_stuck" in r.getMessage() for r in caplog.records)
    # Old records fall outside the 24h window.
    old = time.time() - 90000
    os.utime(path, (old, old))
    assert log_recent_death_records(tmp_path) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_manager_watchdog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'totodev_pub.case_manager_support.watchdog'`.

- [ ] **Step 3: Add the escalation enum member**

In `src/totodev_pub/case_manager_support/escalation.py`, append to `CaseEscalationKind`:

```python
    MANAGER_UNRESPONSIVE = "MANAGER_UNRESPONSIVE"
```

(`emit_simple()` coerces strings through the enum and would raise `ValueError` otherwise. Docs note: unlike every other kind, `MANAGER_UNRESPONSIVE` may arrive on the watchdog thread.)

- [ ] **Step 4: Implement the watchdog module**

Create `src/totodev_pub/case_manager_support/watchdog.py`:

```python
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""ManagerWatchdog (§3) — a daemon thread inside the manager process.

A plain threading.Thread is immune to event-loop blockage, which is the entire
trick. The watchdog never repairs — it detects, diagnoses, and dies; the unit
of remediation is the process, and recover() makes hard death ordinary.

Depends on CaseManager only through the small read-only stamp/task surface
(_last_pulse, _last_tick_started/_completed, _run_task, _running) plus the
shutdown-scan helper. Independently testable without a host.
"""

from __future__ import annotations

import asyncio
import enum
import faulthandler
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from totodev_pub.case_manager_support.constants import PULSE_INTERVAL_SECS
from totodev_pub.case_manager_support.shutdown import (
    ShutdownDirective,
    scan_shutdown_intake,
    shutdown_intake_dir,
)
from totodev_pub.folder_backed_case_support.constants import DEFAULT_LEASE_TTL_SECS

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)

DEATH_RECORD_GLOB = "manager_death_*.yaml"
DEATH_RECORD_WINDOW_SECS = 24 * 3600.0
DEATH_RECORD_LOG_CAP = 5


class WatchdogDetection(str, enum.Enum):
    PULSE_STUCK = "pulse_stuck"
    LOOP_TASK_DEAD = "loop_task_dead"
    LOOP_FAILURE = "loop_failure"  # handed over via CaseManager.on_loop_failure
    MAILBOX_NEGLECT = "mailbox_neglect"


def write_death_record(manager_dir: Path, *, check: str, reason: str) -> Path:
    """Write a timestamped death record (UTC, minute resolution).

    DELIBERATELY primitive I/O — open()/write()/os.replace(), never
    FileMappedPydanticMixin: the mixin's locking/serialization machinery is
    exactly the kind of code a corrupted process might be wedged inside, and
    the death path must have no dependency that can itself hang. Do not
    "helpfully" normalize this onto the mixin.

    Same-minute records overwrite each other — accepted: collapsing a
    sub-minute crash loop to one file per minute beats writing hundreds."""
    now = datetime.now(timezone.utc)
    name = f"manager_death_{now.strftime('%Y-%m-%d_%H%M')}.yaml"
    safe_reason = reason.replace("'", "''")
    body = (
        f"detected_at: '{now.strftime('%Y-%m-%dT%H:%M:%SZ')}'\n"
        f"check: {check}\n"
        f"reason: '{safe_reason}'\n"
        f"pid: {os.getpid()}\n"
    )
    tmp = manager_dir / f".{name}.tmp"
    final = manager_dir / name
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, final)
    return final


def log_recent_death_records(manager_dir: Path) -> int:
    """Log one line per death record with mtime in the last 24h — capped at the
    5 most recent, plus the total count. Called from recover() so the
    reliability signal surfaces without the operator knowing to look."""
    try:
        now = time.time()
        records = [
            p
            for p in manager_dir.glob(DEATH_RECORD_GLOB)
            if now - p.stat().st_mtime <= DEATH_RECORD_WINDOW_SECS
        ]
    except OSError:
        return 0
    if not records:
        return 0
    records.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    logger.warning(
        "%d watchdog death record(s) in the last 24h under %s (showing %d most recent)",
        len(records),
        manager_dir,
        min(len(records), DEATH_RECORD_LOG_CAP),
    )
    for path in records[:DEATH_RECORD_LOG_CAP]:
        check = reason = "?"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("check:"):
                    check = line.partition(":")[2].strip()
                elif line.startswith("reason:"):
                    reason = line.partition(":")[2].strip()
        except OSError:
            pass
        logger.warning("  %s — check=%s reason=%s", path.name, check, reason)
    return len(records)


class ManagerWatchdog:
    """Daemon thread checking liveness on a short cadence.

    Kill-authorized checks: pulse liveness, loop-task death, mailbox neglect,
    plus loop failures handed in via request_kill(). The tick-duration check
    is ALARM ONLY, ALWAYS — long ticks are a throughput smell, never a
    liveness failure."""

    def __init__(
        self,
        manager: "CaseManager",
        *,
        loop: asyncio.AbstractEventLoop,
        exit_code: int,
        on_shutdown_request: Callable[[ShutdownDirective], None] | None = None,
        action: str | None = None,
        stop_grace_secs: float = 30.0,
        check_interval_secs: float = 1.0,
        exit_fn: Callable[[int], None] = os._exit,
    ) -> None:
        self._manager = manager
        self._loop = loop
        self._exit_code = exit_code
        self._on_shutdown_request = on_shutdown_request
        self._stop_grace_secs = stop_grace_secs
        self._check_interval = check_interval_secs
        self._exit_fn = exit_fn

        policy = manager._policy
        self._action = action if action is not None else policy.watchdog_action
        # Thresholds derive from existing policy rather than free-floating numbers.
        self._pulse_stuck_secs = (
            policy.watchdog_pulse_stuck_secs
            if policy.watchdog_pulse_stuck_secs is not None
            else max(10 * PULSE_INTERVAL_SECS, 5.0)
        )
        raw_mailbox = policy.watchdog_mailbox_stale_secs
        if not policy.enable_mailbox or raw_mailbox == 0:
            self._mailbox_stale_secs: float | None = None  # check disabled
        elif raw_mailbox is None:
            self._mailbox_stale_secs = max(10 * policy.maintenance_interval_secs, 30.0)
        else:
            self._mailbox_stale_secs = raw_mailbox
        self._tick_warn_secs = (
            policy.watchdog_tick_warn_secs
            if policy.watchdog_tick_warn_secs is not None
            else min(DEFAULT_LEASE_TTL_SECS, float(policy.manifest_stale_secs)) / 2
        )

        self._armed_at: float | None = None
        self._parked = threading.Event()
        self._parked.set()  # constructed parked; arm() unparks
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._kill_requested: BaseException | None = None
        self._tick_warned_for: float | None = None

    # -- host controls -------------------------------------------------

    def arm(self) -> None:
        """Start (or resume) watching with a fresh stamp, so the first check
        window never measures recovery/startup time."""
        self._armed_at = time.monotonic()
        self._parked.clear()
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="manager-watchdog", daemon=True
            )
            self._thread.start()

    def park(self) -> None:
        """Suspend all checks — called whenever a deliberate shutdown begins,
        else every clean docker stop becomes a nonzero 'wedge' exit."""
        self._parked.set()

    def stop(self) -> None:
        """Terminate the thread (deliberate shutdown / tests / teardown)."""
        self._parked.set()
        self._stopped.set()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def request_kill(self, exc: BaseException) -> None:
        """Entry point for CaseManager.on_loop_failure. Runs on the event-loop
        thread, so it must NOT run the ladder inline (the ladder blocks and
        schedules work back onto the loop — inline would deadlock). Flag it;
        the watchdog thread picks it up within one check interval."""
        self._kill_requested = exc

    # -- thread body -----------------------------------------------------

    def _run(self) -> None:
        while not self._stopped.wait(self._check_interval):
            if self._parked.is_set():
                continue
            try:
                self._check_once()
            except Exception:
                logger.exception("Watchdog check iteration failed (ignored)")

    def _check_once(self) -> None:
        m = self._manager
        now = time.monotonic()
        armed_at = self._armed_at if self._armed_at is not None else now

        # 0. Shutdown request — a command, not a failure. Checked first so the
        #    *requested* exit code wins over a concurrent wedge detection.
        if self._on_shutdown_request is not None:
            directive = scan_shutdown_intake(
                shutdown_intake_dir(m._manager_dir, m._policy)
            )
            if directive is not None:
                self._parked.set()  # shutdown choreography owns the process now
                directive.source_path.unlink(missing_ok=True)
                self._on_shutdown_request(directive)
                return

        # 1. Loop failure handed over by CaseManager.on_loop_failure.
        if self._kill_requested is not None:
            exc, self._kill_requested = self._kill_requested, None
            self._ladder(
                WatchdogDetection.LOOP_FAILURE, f"manager loop gave up: {exc!r}"
            )
            return

        if not m._running:
            return

        # 2. Pulse liveness — the primary, kill-authorized signal. Measures the
        #    event loop, never tick duration.
        ref = max(m._last_pulse or 0.0, armed_at)
        if now - ref > self._pulse_stuck_secs:
            self._ladder(
                WatchdogDetection.PULSE_STUCK,
                f"event loop pulse silent for {now - ref:.1f}s "
                f"(threshold {self._pulse_stuck_secs:.1f}s)",
            )
            return

        # 3. Loop-task death (backstop for a task killed despite §1 hardening).
        task = m._run_task
        if task is not None and task.done() and not task.cancelled():
            try:
                exc = task.exception()
            except Exception:  # pragma: no cover - cross-thread paranoia
                exc = None
            self._ladder(
                WatchdogDetection.LOOP_TASK_DEAD, f"manager loop task died: {exc!r}"
            )
            return

        # 4. Mailbox neglect — loop ticking but the mailbox processor broken.
        #    Excludes the shutdown mailbox (a command with its own pickup path,
        #    not a backlog). A large-but-fresh backlog does not alarm.
        if (
            self._mailbox_stale_secs is not None
            and now - armed_at > self._mailbox_stale_secs
        ):
            oldest_age = self._oldest_intake_age()
            if oldest_age is not None and oldest_age > self._mailbox_stale_secs:
                self._ladder(
                    WatchdogDetection.MAILBOX_NEGLECT,
                    f"oldest mailbox intake file unprocessed for {oldest_age:.1f}s "
                    f"(threshold {self._mailbox_stale_secs:.1f}s)",
                )
                return

        # 5. Tick duration — ALARM ONLY, ALWAYS (inventory fact 3: a healthy
        #    manager can legitimately look busy for minutes). Never a kill.
        started, completed = m._last_tick_started, m._last_tick_completed
        if started is not None and (completed is None or completed < started):
            duration = now - started
            if duration > self._tick_warn_secs and self._tick_warned_for != started:
                self._tick_warned_for = started
                detail = (
                    f"maintenance tick running for {duration:.1f}s (warn threshold "
                    f"{self._tick_warn_secs:.1f}s); idle-case leases may lapse past "
                    f"the {DEFAULT_LEASE_TTL_SECS:.0f}s TTL. Alarm only — never a kill."
                )
                logger.warning("Watchdog: %s", detail)
                self._emit_escalation(f"tick_slow: {detail}", prefer_threadsafe=True)

    def _oldest_intake_age(self) -> float | None:
        mailbox = self._manager._mailbox
        oldest_mtime: float | None = None
        for intake in (
            mailbox.fire_intake(),
            mailbox.adopt_intake(),
            mailbox.reclassify_intake(),
        ):
            if not intake.exists():
                continue
            for path in intake.glob("*.yaml"):  # non-hidden by construction
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if oldest_mtime is None or mtime < oldest_mtime:
                    oldest_mtime = mtime
        if oldest_mtime is None:
            return None
        return time.time() - oldest_mtime

    # -- the kill ladder ---------------------------------------------------

    def _ladder(self, detection: WatchdogDetection, detail: str) -> None:
        """Detect → diagnose loudly → die. Never repairs. Order matters."""
        # 1. Escalation. For a wedged loop call_soon_threadsafe would enqueue a
        #    callback that never runs before os._exit, so PULSE_STUCK dispatches
        #    directly on this thread (documented: MANAGER_UNRESPONSIVE may
        #    arrive off-loop).
        self._emit_escalation(
            f"{detection.value}: {detail}",
            prefer_threadsafe=detection is not WatchdogDetection.PULSE_STUCK,
        )
        # 2. Every thread's stack to stderr — the primary postmortem artifact;
        #    works from this thread even when the loop is wedged.
        faulthandler.dump_traceback(all_threads=True)
        if self._action == "alarm_only":
            logger.error(
                "Watchdog detection %s (%s) — watchdog_action=alarm_only; "
                "parking instead of exiting.",
                detection.value,
                detail,
            )
            self._parked.set()  # don't re-alarm every second on the same wedge
            return
        # 3. Death record (primitive I/O — see write_death_record).
        try:
            write_death_record(
                self._manager._manager_dir, check=detection.value, reason=detail
            )
        except Exception:
            logger.exception("Failed to write death record (continuing to exit)")
        # 4. Graceful attempt — detection-aware. A wedged loop won't run a
        #    scheduled coroutine any sooner than a signal handler, so
        #    PULSE_STUCK gets only a ~2s cap instead of the full grace.
        grace = (
            2.0
            if detection is WatchdogDetection.PULSE_STUCK
            else self._stop_grace_secs
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._manager.stop(timeout=grace), self._loop
            )
            fut.result(timeout=grace + 5.0)
        except Exception:
            pass
        # 5. Exit — ALWAYS the watchdog code, even when rung 4 succeeded: a
        #    watchdog detection is never allowed to end in exit 0. os._exit
        #    bypasses all Python cleanup so it works even when the interpreter
        #    is too damaged for sys.exit. (The park is unreachable in
        #    production; it keeps a monkeypatched exit_fn in tests from
        #    re-firing on the next check interval.)
        self._parked.set()
        self._exit_fn(self._exit_code)

    def _emit_escalation(self, detail: str, *, prefer_threadsafe: bool) -> None:
        def _do() -> None:
            self._manager._escalations.emit_simple(
                "MANAGER_UNRESPONSIVE", None, None, detail
            )

        if prefer_threadsafe:
            try:
                self._loop.call_soon_threadsafe(_do)
                return
            except RuntimeError:
                pass  # loop closed/unusable — which is, after all, the diagnosis
        _do()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_manager_watchdog.py tests/test_case_manager_escalations.py -v`
Expected: all pass. (The pulse-stuck test prints a faulthandler stack dump to stderr — that is correct behavior, not a failure.)

- [ ] **Step 6: Commit**

```bash
git add src/totodev_pub/case_manager_support/watchdog.py src/totodev_pub/case_manager_support/escalation.py tests/test_manager_watchdog.py
git commit -m "feat: add ManagerWatchdog daemon thread with kill ladder and death records"
```

---

### Task 7: Death-record surfacing + stale-shutdown discard at `recover()`

**Files:**
- Modify: `src/totodev_pub/case_manager_support/recover.py`
- Test: `tests/test_case_manager_death_records.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_case_manager_death_records.py`:

```python
# Part of the totodev_pub library.

import logging

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.shutdown import shutdown_intake_dir, write_shutdown_request
from totodev_pub.case_manager_support.watchdog import write_death_record


@pytest.mark.asyncio
async def test_recover_surfaces_recent_death_records(tmp_path, caplog):
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()
    write_death_record(manager._manager_dir, check="pulse_stuck", reason="wedged hook")
    with caplog.at_level(logging.WARNING):
        report = await manager.recover()
    assert report.death_records_recent == 1
    assert any("pulse_stuck" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_recover_discards_stale_shutdown_requests(tmp_path, caplog):
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()
    intake = shutdown_intake_dir(manager._manager_dir, manager._policy)
    write_shutdown_request(intake, graceful=False, reason="from a dead process")
    with caplog.at_level(logging.WARNING):
        report = await manager.recover()
    assert report.shutdown_requests_discarded == 1
    assert not any(p for p in intake.iterdir() if not p.name.startswith("."))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_case_manager_death_records.py -v`
Expected: FAIL — `RecoverReport` has no `death_records_recent` field.

- [ ] **Step 3: Implement**

In `src/totodev_pub/case_manager_support/recover.py`:

(a) Add imports:

```python
from totodev_pub.case_manager_support.shutdown import discard_stale_requests, shutdown_intake_dir
from totodev_pub.case_manager_support.watchdog import log_recent_death_records
```

(b) Add two fields to `RecoverReport`:

```python
    death_records_recent: int = 0
    shutdown_requests_discarded: int = 0
```

(c) In `recover_manager()`, immediately after `manager._write_manifest()`:

```python
    report.death_records_recent = log_recent_death_records(manager._manager_dir)
    report.shutdown_requests_discarded = discard_stale_requests(
        shutdown_intake_dir(manager._manager_dir, manager._policy)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_death_records.py tests/test_case_manager_recover_reap.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/totodev_pub/case_manager_support/recover.py tests/test_case_manager_death_records.py
git commit -m "feat: surface recent death records and discard stale shutdown requests at recover()"
```

---

### Task 8: Shutdown mailbox wiring — manager seam, processor, client (§6)

**Files:**
- Modify: `src/totodev_pub/case_manager.py` (seam + namespace dirs + manifest paths)
- Modify: `src/totodev_pub/case_manager_support/case_manager_manifest.py`
- Modify: `src/totodev_pub/case_manager_support/mailbox/processor.py`
- Modify: `src/totodev_pub/case_manager_client.py`
- Test: `tests/test_case_manager_shutdown_mailbox.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_case_manager_shutdown_mailbox.py`:

```python
# Part of the totodev_pub library.

import asyncio
import logging

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.shutdown import ShutdownRequest


@pytest.mark.asyncio
async def test_client_submit_shutdown_writes_structured_request(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_shutdown(graceful=True, reason="drain", only_if_fresh=False)
    intake = manager._mailbox.shutdown_intake()
    files = [p for p in intake.iterdir() if not p.name.startswith(".")]
    assert len(files) == 1
    req = ShutdownRequest.load(str(files[0]), acquire_lock=False)
    assert req.graceful is True
    assert req.reason == "drain"
    assert req.correlation_id == handle.correlation_id


@pytest.mark.asyncio
async def test_cooperative_pickup_invokes_registered_callback(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    seen = []
    manager.on_shutdown_request(seen.append)
    await manager.start()
    client = CaseManagerClient(tmp_path / "cache")
    client.submit_shutdown(graceful=False, reason="stop now", only_if_fresh=False)
    for _ in range(300):
        if seen:
            break
        await asyncio.sleep(0.02)
    await manager.stop()
    assert seen and seen[0].graceful is False and seen[0].reason == "stop now"
    # The request file was consumed at pickup.
    intake = manager._mailbox.shutdown_intake()
    assert not any(p for p in intake.iterdir() if not p.name.startswith("."))


@pytest.mark.asyncio
async def test_pickup_without_host_warns_and_discards(tmp_path, caplog):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    client = CaseManagerClient(tmp_path / "cache")
    client.submit_shutdown(only_if_fresh=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(300):
            if any("no host is registered" in r.getMessage() for r in caplog.records):
                break
            await asyncio.sleep(0.02)
    await manager.stop()
    assert any("no host is registered" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_manifest_advertises_shutdown_intake(tmp_path):
    from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
    from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME

    manager = provision_manager(tmp_path)
    await manager.recover()
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    assert manifest.paths.shutdown_mailbox_intake is not None
    assert "shutdown_mailbox" in manifest.paths.shutdown_mailbox_intake
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_case_manager_shutdown_mailbox.py -v`
Expected: FAIL — `CaseManagerClient` has no `submit_shutdown`.

- [ ] **Step 3: Implement — manifest model**

In `src/totodev_pub/case_manager_support/case_manager_manifest.py`, add to `ManifestPaths` (after `reclassify_mailbox_intake`):

```python
    # Optional for manifest back-compat: absent in manifests written before the
    # shutdown mailbox existed.
    shutdown_mailbox_intake: Optional[str] = None
```

- [ ] **Step 4: Implement — `case_manager.py`**

(a) Add to the shutdown import section (new import line near the other support imports):

```python
from totodev_pub.case_manager_support.shutdown import ShutdownDirective
```

(b) In `__init__`, next to `self._loop_failure_cb`:

```python
        self._shutdown_request_cb: Callable[[ShutdownDirective], None] | None = None
```

(c) Next to `on_loop_failure`, add the seam pair:

```python
    def on_shutdown_request(self, callback: Callable[[ShutdownDirective], None]) -> None:
        """Register the single host callback invoked when a shutdown-mailbox
        request is picked up cooperatively (the watchdog has its own pickup
        path). serve() wires this to the §6 shutdown protocol."""
        self._shutdown_request_cb = callback

    def _notify_shutdown_request(self, directive: ShutdownDirective) -> None:
        if self._shutdown_request_cb is None:
            logger.warning(
                "Shutdown request %s received but no host is registered "
                "(embedded start() usage?); discarded — a manager that nobody "
                "hosts cannot promise process exit semantics.",
                directive.source_path.name,
            )
            return
        self._shutdown_request_cb(directive)
```

(d) In `_ensure_namespace_dirs()` (line ~800), after the reclassify-mailbox loop:

```python
        (mgr_dir / policy.shutdown_mailbox_subdir / "intake").mkdir(parents=True, exist_ok=True)
```

(e) In `_write_manifest()`, add to the `ManifestPaths(...)` construction (after `reclassify_mailbox_intake=...`):

```python
            shutdown_mailbox_intake=rel(
                self._manager_dir / self._policy.shutdown_mailbox_subdir / "intake"
            ),
```

- [ ] **Step 5: Implement — `mailbox/processor.py`**

(a) Add imports:

```python
from totodev_pub.case_manager_support.shutdown import (
    ShutdownAck,
    scan_shutdown_intake,
    write_shutdown_request,
)
```

(b) Path helper next to `reclassify_intake()`:

```python
    def shutdown_intake(self) -> Path:
        return self._mgr_dir / self._policy.shutdown_mailbox_subdir / "intake"
```

(c) In `_ensure_dirs()`, add creation of `self.shutdown_intake()` alongside the other intake dirs:

```python
        self.shutdown_intake().mkdir(parents=True, exist_ok=True)
```

(d) Submit method, matching the `submit_fire` shape:

```python
    def submit_shutdown(
        self,
        *,
        graceful: bool = False,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        self._ensure_dirs()
        corr, _path = write_shutdown_request(
            self.shutdown_intake(),
            graceful=graceful,
            reason=reason,
            correlation_id=correlation_id,
        )
        return RequestHandle(corr, self.results_dir() / f"{corr}.yaml")
```

(e) Cooperative pickup — first thing in `maintenance_tick()` after `_ensure_dirs()` (before fire processing, so a shutdown is never queued behind a slow fire):

```python
    async def maintenance_tick(self) -> None:
        if not self._policy.enable_mailbox:
            return
        self._ensure_dirs()
        self._check_shutdown_intake()
        await self._process_fire_intake()
        await self._process_reclassify_intake()
        await self._process_adopt_intake()
        self._sweep_old_results()

    def _check_shutdown_intake(self) -> None:
        directive = scan_shutdown_intake(self.shutdown_intake())
        if directive is None:
            return
        directive.source_path.unlink(missing_ok=True)
        self._manager._notify_shutdown_request(directive)
```

(f) In `poll_result()`, add a shutdown-ack branch as the FIRST discriminator try-block (mirroring the reclassify branch):

```python
        try:
            if "kind: shutdown" in handle.result_path.read_text():
                return ShutdownAck.load(str(handle.result_path), acquire_lock=False)
        except Exception:
            pass
```

Also extend the return annotation: `AdvanceResultSerializable | AdoptResult | ReclassifyResult | ShutdownAck | None`.

- [ ] **Step 6: Implement — `case_manager_client.py`**

Add next to `submit_adopt`, following the client shape (`_check_fresh` + thin delegate):

```python
    def submit_shutdown(
        self,
        *,
        graceful: bool = False,
        reason: str | None = None,
        only_if_fresh: bool = True,
    ) -> RequestHandle:
        """Ask the manager process to shut down via the shutdown mailbox.

        ``graceful=True`` requests a drain (in-flight case steps settle first);
        the default is an immediate exit (leases lapse, recover() reconciles —
        the same hard path the system already tolerates, requested on purpose).

        This always exits nonzero — the case-manager process cannot be told
        through this API to stay down. Under a supervisor configured to restart
        on nonzero exit (e.g. Docker with ``restart: on-failure``, this process
        as PID 1 via an exec-form entrypoint), it will come back. To
        decommission permanently, stop it by other means (an orchestrator
        scale-down, an OS signal from something with process reach, or — if the
        manager itself should decide when it's done —
        ``serve(..., stop_when_empty=True)``). Kubernetes caveat: Deployments
        default to ``restartPolicy: Always``, which restarts exit 0 too — on
        k8s, decommission means scaling the workload down; the exit code alone
        cannot express "stay down" there.

        Result semantics: on the graceful path an "acknowledged, shutting down"
        result is written before the drain, so poll_result/wait_result resolve.
        On the immediate path the handle may never resolve — the manifest's
        ``stopped_at`` is the real confirmation either way."""
        self._check_fresh(only_if_fresh)
        return self._mailbox.submit_shutdown(graceful=graceful, reason=reason)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_shutdown_mailbox.py tests/test_case_manager_fire_mailbox.py tests/test_case_manager_adopt_mailbox.py tests/test_case_manager_reclassify.py -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/totodev_pub/case_manager.py src/totodev_pub/case_manager_support/case_manager_manifest.py src/totodev_pub/case_manager_support/mailbox/processor.py src/totodev_pub/case_manager_client.py tests/test_case_manager_shutdown_mailbox.py
git commit -m "feat: wire shutdown mailbox — manager seam, cooperative pickup, submit_shutdown"
```

---

### Task 9: `case_manager_host.serve()` — exit codes, signals, watchdog orchestration (§4)

**Files:**
- Create: `src/totodev_pub/case_manager_host.py`
- Test: `tests/test_case_manager_host_serve.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_case_manager_host_serve.py`:

```python
# Part of the totodev_pub library.

import asyncio
import os
import signal
import threading

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub import case_manager_host
from totodev_pub.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_host import EXIT_RESTART_REQUESTED, EXIT_WATCHDOG, serve
from totodev_pub.case_manager_support.case_manager_manifest import CaseManagerManifest
from totodev_pub.case_manager_support.constants import MANIFEST_FILENAME


class _HardExit(BaseException):
    def __init__(self, code):
        self.code = code


@pytest.fixture
def hard_exit_recorder(monkeypatch):
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise _HardExit(code)

    monkeypatch.setattr(case_manager_host, "_hard_exit", fake_exit)
    return codes


def _stopped_at(manager):
    manifest = CaseManagerManifest.load(
        str(manager._manager_dir / MANIFEST_FILENAME), acquire_lock=False
    )
    return manifest.stopped_at


@pytest.mark.asyncio
async def test_serve_rejects_prelifecycled_manager(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    with pytest.raises(ValueError):
        await serve(manager)


@pytest.mark.asyncio
async def test_serve_rejects_both_stop_when_and_stop_when_empty(tmp_path):
    manager = provision_manager(tmp_path)
    with pytest.raises(ValueError):
        await serve(manager, stop_when=lambda: True, stop_when_empty=True)


@pytest.mark.asyncio
async def test_sigterm_is_a_clean_exit_zero_stop(tmp_path):
    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.running:
            break
        await asyncio.sleep(0.02)
    assert manager.running
    assert any(t.name == "manager-watchdog" for t in threading.enumerate())
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10.0)   # returns None → process exit 0
    assert manager.running is False
    assert _stopped_at(manager) is not None


@pytest.mark.asyncio
async def test_serve_respects_watchdog_enabled_false(tmp_path):
    manager = provision_manager(tmp_path, watchdog_enabled=False)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.running:
            break
        await asyncio.sleep(0.02)
    assert not any(t.name == "manager-watchdog" for t in threading.enumerate())
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10.0)


@pytest.mark.asyncio
async def test_graceful_mailbox_shutdown_acks_then_exits_75(tmp_path, hard_exit_recorder):
    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.running:
            break
        await asyncio.sleep(0.02)
    client = CaseManagerClient(tmp_path / "cache")
    handle = client.submit_shutdown(graceful=True, reason="drain", only_if_fresh=False)
    with pytest.raises(_HardExit) as excinfo:
        await asyncio.wait_for(task, timeout=10.0)
    assert excinfo.value.code == EXIT_RESTART_REQUESTED
    assert hard_exit_recorder == [EXIT_RESTART_REQUESTED]
    ack = client.poll_result(handle)             # ack written before the drain
    assert ack is not None and ack.kind == "shutdown"


@pytest.mark.asyncio
async def test_immediate_mailbox_shutdown_exits_75_and_teaches(tmp_path, hard_exit_recorder, caplog):
    import logging

    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))
    for _ in range(300):
        if manager.running:
            break
        await asyncio.sleep(0.02)
    client = CaseManagerClient(tmp_path / "cache")
    with caplog.at_level(logging.WARNING):
        client.submit_shutdown(only_if_fresh=False)  # default: immediate
        with pytest.raises(_HardExit) as excinfo:
            await asyncio.wait_for(task, timeout=10.0)
    assert excinfo.value.code == EXIT_RESTART_REQUESTED
    assert _stopped_at(manager) is not None      # the one refinement over the hard path
    assert any("Immediate shutdown triggered" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_case_manager_host_serve.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'totodev_pub.case_manager_host'`.

- [ ] **Step 3: Implement**

Create `src/totodev_pub/case_manager_host.py`:

```python
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""case_manager_host — the blessed host entry point (§4).

CaseManager coordinates a fleet; this module runs a process. Everything that
knows it owns a whole process (signals, exit codes, the watchdog's arm/park
choreography, the shutdown-request execution path) lives here — case_manager.py
never imports signal/threading/faulthandler and never defines an exit code.

Typical host program:

    import asyncio
    from totodev_pub.case_manager import CaseManager
    from totodev_pub.case_manager_host import serve
    from myapp.cases import InquiryCase

    manager = CaseManager.open("/data/inquiries", register_types=[InquiryCase])
    asyncio.run(serve(manager))
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import TYPE_CHECKING, Any, Callable

from totodev_pub.case_manager_support.exceptions import CaseManagerStopTimeoutError
from totodev_pub.case_manager_support.shutdown import ShutdownDirective, write_shutdown_ack
from totodev_pub.case_manager_support.watchdog import ManagerWatchdog

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)

# Supervisor exit-code contract — defined once, here. Exit 0 is reserved for a
# stop that both originates outside the manager's own reactive machinery and
# is genuinely meant to be final: an OS-signaled stop, or stop_when
# self-completion. Anything the manager does *in reaction to* something is
# nonzero, even if it shuts down cleanly.
EXIT_WATCHDOG = 70           # EX_SOFTWARE: every watchdog kill, unconditionally.
EXIT_RESTART_REQUESTED = 75  # EX_TEMPFAIL: every mailbox-requested shutdown —
                             # the mailbox has exactly one outcome, "please come back".

# Bound on how long the immediate (non-graceful) shutdown path lets the current
# loop iteration unwind before exiting.
IMMEDIATE_SHUTDOWN_GRACE_SECS = 2.0


def _hard_exit(code: int) -> None:
    """os._exit wrapper — bypasses all Python cleanup by design (works even
    when the interpreter is too damaged for sys.exit). Module-level so tests
    can monkeypatch it."""
    os._exit(code)


async def serve(
    manager: "CaseManager",
    *,
    stop_grace_secs: float = 30.0,
    stop_when: Callable[[], bool] | None = None,
    stop_when_empty: bool = False,
) -> None:
    """Recover, start, and host ``manager`` until stopped.

    Returns normally (→ process exit 0) on an OS-signaled stop (SIGTERM/SIGINT)
    or a True ``stop_when`` — a deliberate, final stop. Exits the process
    directly with ``EXIT_RESTART_REQUESTED`` for every mailbox-requested
    shutdown, and the watchdog exits with ``EXIT_WATCHDOG`` on any detection.

    ``manager`` must be freshly constructed — not yet recovered or started;
    serve() owns that sequencing itself. ``stop_grace_secs`` is host wiring,
    not deployment policy: Docker's ``stop_grace_period`` must exceed it.

    ``stop_when`` is polled once per maintenance interval on the manager's
    event loop — a blocking predicate is the caller's bug, exactly like a
    blocking ``perform_*`` hook. ``stop_when_empty=True`` is sugar for
    "stop when manager.is_idle" (job-manager hosts, §8); mutually exclusive
    with an explicit ``stop_when``.
    """
    if manager.recovered or manager.running:
        raise ValueError(
            "serve() requires a freshly constructed CaseManager (not recovered "
            "or started); it owns the recover()/start() sequencing itself."
        )
    if stop_when is not None and stop_when_empty:
        raise ValueError("stop_when and stop_when_empty are mutually exclusive")
    if stop_when_empty:
        stop_when = lambda: manager.is_idle  # noqa: E731

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    cause: dict[str, Any] = {"kind": None, "directive": None}

    def _record_cause(kind: str, directive: ShutdownDirective | None = None) -> None:
        if cause["kind"] is None:
            cause["kind"] = kind
            cause["directive"] = directive
            stop_event.set()

    # 1. Sequencing: recover() then start() (which starts the pulse task).
    await manager.recover()
    await manager.start()

    # Debugger sessions: a paused process looks exactly like a wedged one.
    action: str | None = None
    if sys.gettrace() is not None and manager._policy.watchdog_action == "exit":
        logger.info(
            "Debugger detected (sys.gettrace()); watchdog defaulting to alarm_only."
        )
        action = "alarm_only"

    def _shutdown_from_watchdog(directive: ShutdownDirective) -> None:
        # Watchdog-thread pickup path (§6): hand off to the loop; if the loop
        # is too wedged to run the cooperative path, exit directly with the
        # *requested* code rather than whatever the wedge detection would pick.
        try:
            loop.call_soon_threadsafe(_record_cause, "shutdown", directive)
        except RuntimeError:
            _hard_exit(EXIT_RESTART_REQUESTED)
        deadline = (
            (stop_grace_secs + 5.0)
            if directive.graceful
            else (IMMEDIATE_SHUTDOWN_GRACE_SECS + 2.0)
        )
        time.sleep(deadline)  # a healthy path exits the process before this returns
        _hard_exit(EXIT_RESTART_REQUESTED)

    # 2/3. Watchdog + both callback seams.
    watchdog: ManagerWatchdog | None = None
    if manager._policy.watchdog_enabled:
        watchdog = ManagerWatchdog(
            manager,
            loop=loop,
            exit_code=EXIT_WATCHDOG,
            on_shutdown_request=_shutdown_from_watchdog,
            action=action,
            stop_grace_secs=stop_grace_secs,
            exit_fn=_hard_exit,
        )
        manager.on_loop_failure(watchdog.request_kill)
    manager.on_shutdown_request(
        lambda directive: _record_cause("shutdown", directive)
    )

    # SIGTERM/SIGINT: the deliberate, external, final stop (exit 0).
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _record_cause, "signal")

    # 4. Arm after start(), with a fresh stamp — recovery time is never measured.
    if watchdog is not None:
        watchdog.arm()

    poller: asyncio.Task[None] | None = None
    if stop_when is not None:
        async def _poll_stop_when() -> None:
            interval = manager._policy.maintenance_interval_secs
            while not stop_event.is_set():
                if stop_when():
                    _record_cause("stop_when")
                    return
                await asyncio.sleep(interval)

        poller = asyncio.create_task(_poll_stop_when())

    # 5. Block until signaled, shutdown-requested, or watchdog-killed.
    try:
        await stop_event.wait()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        if poller is not None:
            poller.cancel()

    # Park before any deliberate shutdown: an armed watchdog during a settle
    # (which stops the pulse by design) would turn every clean docker stop into
    # a nonzero "wedge" exit.
    if watchdog is not None:
        watchdog.park()

    if cause["kind"] in ("signal", "stop_when"):
        try:
            await manager.stop(timeout=stop_grace_secs)
        except CaseManagerStopTimeoutError:
            logger.exception(
                "Deliberate stop failed to settle within %.1fs; exiting hard.",
                stop_grace_secs,
            )
            _hard_exit(EXIT_WATCHDOG)
        if watchdog is not None:
            watchdog.stop()  # join the thread; serve() may run inside a larger program
        return  # exit 0 — deliberate, external (or self-completed), and final.

    # Mailbox-requested shutdown (§6): a recovery lever, never exit 0.
    directive: ShutdownDirective = cause["directive"]
    if directive.graceful:
        # Ack before the drain so poll_result/wait_result resolve normally.
        write_shutdown_ack(manager._mailbox.results_dir(), directive)
        try:
            await manager.stop(timeout=stop_grace_secs)
        except CaseManagerStopTimeoutError:
            logger.exception(
                "Requested drain failed to settle within %.1fs.", stop_grace_secs
            )
        _hard_exit(EXIT_RESTART_REQUESTED)
    else:
        logger.warning(
            "Immediate shutdown triggered by mailbox request %s. For a graceful "
            "shutdown that waits for in-flight work to settle, include 'SIGTERM' "
            "in the request filename (or use "
            "CaseManagerClient.submit_shutdown(graceful=True)).",
            directive.source_path.name,
        )
        # The same "hard path" the system already tolerates (SIGKILL, watchdog
        # exit) — leases lapse, recover() reconciles — just requested on purpose.
        manager._stopping = True
        manager._running = False
        if manager._run_task is not None:
            await asyncio.wait(
                {manager._run_task}, timeout=IMMEDIATE_SHUTDOWN_GRACE_SECS
            )
        try:
            # One refinement over the pure hard path: this process is healthy
            # enough to have parsed the request, so one cheap manifest write
            # spares clients the 30s "dead or stopped?" ambiguity.
            manager._write_manifest(running=False, stopped=True)
        except Exception:
            pass
        _hard_exit(EXIT_RESTART_REQUESTED)
```

Note: `loop.add_signal_handler` is POSIX-only; this library targets macOS/Linux hosts. Do not add a Windows fallback.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_case_manager_host_serve.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/totodev_pub/case_manager_host.py tests/test_case_manager_host_serve.py
git commit -m "feat: add case_manager_host.serve() with exit-code contract, signal wiring, watchdog orchestration"
```

---

### Task 10: `stop_when` / `stop_when_empty` self-completion (§8)

**Files:**
- Modify: none (implemented in Task 9's `serve()`)
- Test: extend `tests/test_case_manager_host_serve.py`

- [ ] **Step 1: Write the failing/verifying tests**

Append to `tests/test_case_manager_host_serve.py`:

```python
@pytest.mark.asyncio
async def test_stop_when_empty_self_completes_exit_zero(tmp_path):
    manager = provision_manager(tmp_path)
    # Empty manager: is_idle is True immediately, so serve() should self-stop.
    await asyncio.wait_for(
        serve(manager, stop_grace_secs=5.0, stop_when_empty=True), timeout=10.0
    )
    assert manager.running is False
    assert _stopped_at(manager) is not None      # clean stopped_at, like a signal stop


@pytest.mark.asyncio
async def test_stop_when_custom_predicate(tmp_path):
    manager = provision_manager(tmp_path)
    polls = {"n": 0}

    def done_after_five():
        polls["n"] += 1
        return polls["n"] >= 5

    await asyncio.wait_for(
        serve(manager, stop_grace_secs=5.0, stop_when=done_after_five), timeout=10.0
    )
    assert polls["n"] >= 5
    assert manager.running is False
```

- [ ] **Step 2: Run tests**

Run: `PYTHONPATH=src pytest tests/test_case_manager_host_serve.py -v`
Expected: all pass (Task 9 already implemented the predicate poller; these tests verify §8 end-to-end). If either fails, fix `serve()`'s poller/`is_idle` interplay before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_case_manager_host_serve.py
git commit -m "test: cover stop_when/stop_when_empty self-completion exit-0 path"
```

---

### Task 11: Health-probe CLI (§5)

**Files:**
- Create: `src/totodev_pub/cli/manager_health.py`
- Modify: `pyproject.toml`
- Test: `tests/test_manager_health_cli.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_manager_health_cli.py`:

```python
# Part of the totodev_pub library.

from datetime import datetime, timedelta, timezone

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.cli.manager_health import main


def test_missing_manifest_exits_3(tmp_path):
    assert main([str(tmp_path / "nowhere")]) == 3


def test_unreadable_manifest_exits_3(tmp_path):
    mgr_dir = tmp_path / "cache" / ".case_manager"
    mgr_dir.mkdir(parents=True)
    (mgr_dir / "manifest.yaml").write_text(":::not yaml{{", encoding="utf-8")
    assert main([str(tmp_path / "cache")]) == 3


@pytest.mark.asyncio
async def test_fresh_manager_exits_0(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    try:
        assert main([str(tmp_path / "cache")]) == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stopped_manager_exits_2(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()
    await manager.stop()   # writes stopped_at
    assert main([str(tmp_path / "cache")]) == 2


def test_stale_heartbeat_exits_1(tmp_path):
    mgr_dir = tmp_path / "cache" / ".case_manager"
    mgr_dir.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (mgr_dir / "manifest.yaml").write_text(
        f"heartbeat_at: '{old}'\nmanifest_stale_secs: 30\n", encoding="utf-8"
    )
    assert main([str(tmp_path / "cache")]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_manager_health_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'totodev_pub.cli.manager_health'`.

- [ ] **Step 3: Implement**

Create `src/totodev_pub/cli/manager_health.py`:

```python
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Health probe for a CaseManager deployment (§5).

Reads the manifest file DIRECTLY — it must NOT construct a CaseManagerClient
(whose constructor does a full CaseManager.attach()); a probe should be as dumb
and failure-proof as possible. This is the piece Kubernetes livenessProbes and
monitoring hook into; it catches what no in-process mechanism can ("the process
is gone entirely" / "the restart loop itself is failing").

Exit codes:
  0 — heartbeat fresh
  1 — heartbeat stale (page someone)
  2 — stopped_at set (deliberately stopped; expected during decommission)
  3 — no/unreadable manifest

Stale and deliberately-stopped are distinct on purpose: a probe that conflates
them pages people for scale-downs.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_NAMESPACE = ".case_manager"
DEFAULT_STALE_SECS = 30


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="totodev-manager-health",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cache_root", help="Cache root the manager serves")
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Manager namespace dir under the cache root (default {DEFAULT_NAMESPACE})",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.cache_root) / args.namespace / "manifest.yaml"
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest is not a mapping")
    except Exception as exc:
        print(f"no/unreadable manifest at {manifest_path}: {exc}", file=sys.stderr)
        return 3

    if data.get("stopped_at"):
        print(f"deliberately stopped (stopped_at={data['stopped_at']})")
        return 2

    heartbeat = data.get("heartbeat_at")
    stale_secs = data.get("manifest_stale_secs") or DEFAULT_STALE_SECS
    if heartbeat:
        try:
            beat = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
        except ValueError:
            print(f"unparseable heartbeat_at: {heartbeat!r}", file=sys.stderr)
            return 3
        age = (datetime.now(timezone.utc) - beat).total_seconds()
        if age <= stale_secs:
            print(f"fresh (heartbeat age {age:.1f}s, threshold {stale_secs}s)")
            return 0
        print(f"stale (heartbeat age {age:.1f}s > {stale_secs}s)")
        return 1

    print("no heartbeat recorded (manager never started or stale manifest)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

In `pyproject.toml`, add (this is the repo's first console script; console scripts are the chosen packaging convention per the design decisions above):

```toml
[project.scripts]
totodev-manager-health = "totodev_pub.cli.manager_health:main"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_manager_health_cli.py -v`
Expected: 6 passed. Then verify the entry point installs: `uv sync --extra dev && .venv/bin/totodev-manager-health --help` prints usage.

- [ ] **Step 5: Commit**

```bash
git add src/totodev_pub/cli/manager_health.py pyproject.toml tests/test_manager_health_cli.py
git commit -m "feat: add totodev-manager-health probe CLI (manifest-only, 0/1/2/3 exit codes)"
```

---

### Task 12: Integration pass — full suite + docs cross-check

**Files:**
- Possibly touch any file above (regression fixes only)

- [ ] **Step 1: Run the full core test lane**

Run: `PYTHONPATH=src pytest -m "not pipes and not connectors and not lucidspark and not llm and not git"`
Expected: everything passes. Pay particular attention to `test_case_manager_client_reads.py` (heartbeat moved to the pulse), `test_case_manager_stop.py` (pulse-task teardown), and `test_truncated_entries.py`/other tests already modified in the working tree.

- [ ] **Step 2: Verify the docstring/comment mandates from the proposal are present**

Checklist (grep each):
- `watchdog.py` `write_death_record` carries the "DELIBERATELY primitive I/O … Do not 'helpfully' normalize" comment.
- `submit_shutdown` docstring contains "This always exits nonzero" and the Kubernetes `restartPolicy: Always` caveat.
- The immediate-shutdown log line in `case_manager_host.py` names the `SIGTERM` filename convention and `submit_shutdown(graceful=True)`.
- `case_manager.py` contains no `import signal`, `import threading`, `import faulthandler`, and no exit-code constant (`grep -n "signal\|threading\|faulthandler\|EXIT_" src/totodev_pub/case_manager.py` — only the `EscalationRegistry`-related hits and none of the forbidden imports).

- [ ] **Step 3: Update the proposal's status line**

In `notebooks/DEVDAVE/case_manager_classes/_backlog/proposed_manager_watchdog_and_host.md`, change the status bullet from "for discussion — do not implement yet" to note implementation (e.g. `Status: **implemented 2026-07-XX** — see docs/superpowers/plans/2026-07-09-manager-watchdog-and-host.md`).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: integration pass for watchdog/host/shutdown-mailbox feature; mark proposal implemented"
```

---

## Spec-coverage map (self-review)

| Proposal section | Task(s) |
|---|---|
| §1 loop death loud + `on_loop_failure` | Task 1 |
| §2 pulse task, heartbeat relocation, tick stamps, `_last_maintenance` retirement | Task 2 |
| §3 watchdog checks, thresholds, kill ladder, death records, policy knobs | Tasks 4, 6 |
| §3 death-record surfacing at `recover()` (Resolved #5) | Task 7 |
| §4 `serve()`, signals, exit codes, watchdog arm/park, debugger detection, precondition, `recovered`/`running` promotion | Tasks 3, 9 |
| §5 health-probe CLI | Task 11 |
| §6 shutdown mailbox: model/protocol/precedence, two pickup paths, ack semantics, teaching log, startup hygiene, `submit_shutdown` (no `restart` param — Resolved #8), never exit 0 (Resolved #11) | Tasks 5, 6 (watchdog pickup), 7 (hygiene), 8, 9 (execution paths) |
| §7 lease succession | Deferred by team decision — no task, correctly |
| §8 `stop_when` / `stop_when_empty` / `is_idle` (Resolved #12) | Tasks 3, 9, 10 |
| Docker / desktop behavior sections | Behavioral consequences of the above; no separate code. The compose example belongs in user docs — out of scope here (no docs/tutorial tree change requested by the proposal). |
| Mailbox-neglect exclusions (Resolved #6) | Task 6 (`enable_mailbox` gate, shutdown mailbox excluded from `_oldest_intake_age`) |
| Exit-code contract (Resolved #4) | Task 9 constants + watchdog `exit_code` injection in Task 6 |
