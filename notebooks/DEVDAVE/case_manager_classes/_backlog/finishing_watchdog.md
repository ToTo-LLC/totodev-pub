# Finishing: Manager watchdog, host entry point, and die-loudly contract

- Status: **follow-up work — implement before promoting to `main`.** The low-impact,
  no-decision-required items (part of Follow-up 3, all of Follow-up 5's cheap items) were
  implemented 2026-07-16; Follow-ups 1, 2, and 4, plus Follow-up 3's optional hardening
  and Follow-up 5's operator doc, remain open — see "What remains" under the promotion
  checklist below.
- Written 2026-07-10. The core feature landed on branch `feature/manager-watchdog-host`
  (worktree `.worktrees/manager-watchdog-host`, commits `eab148b`–`12c2a6a`), later merged
  into `explore/case-queue` (`31e8795`).
- Parent proposal: [`proposed_manager_watchdog_and_host.md`](proposed_manager_watchdog_and_host.md)
  (implemented 2026-07-09).

## What shipped

The approved proposal is implemented end-to-end:

| Area | Module(s) | Notes |
|---|---|---|
| Loop hardening | `case_manager.py` | `on_loop_failure` seam, 3-strike retry |
| Liveness pulse | `case_manager.py`, `constants.py` | Heartbeat decoupled from tick duration |
| Watchdog | `case_manager_support/watchdog.py` | Daemon thread, kill ladder, death records |
| Host entry point | `case_manager_host.py` | `serve()`, exit codes 0 / 70 / 75 |
| Shutdown mailbox | `shutdown.py`, `mailbox/processor.py`, `case_manager_support/case_manager_client.py` | Shared parser, dual pickup paths |
| Recover surfacing | `recover.py` | Death-record log lines; stale shutdown discard |
| Health probe | `cli/manager_health.py` | `totodev-manager-health`, manifest-only |
| Self-completion | `serve(stop_when=..., stop_when_empty=True)` | Job-manager hosts exit 0 when idle |

**Test status at integration pass:** 51 targeted watchdog/host tests pass; 1505 core-lane
tests pass. Six known pre-existing failures in `test_case_machine_factory.py` (unrelated
context-manager protocol issue).

**Merge guidance from final review:** ready to merge into `explore/case-queue` now;
address the items below before promoting to `main`.

---

## Follow-up 1 (Important): End-to-end `EXIT_WATCHDOG` through `serve()`

### Problem

`serve()` wires `manager.on_loop_failure(watchdog.request_kill)` when
`watchdog_enabled=True`, and the watchdog unit tests cover `LOOP_FAILURE` detection in
isolation. No integration test drives three consecutive tick failures through a running
`serve()` and asserts exit code **70**.

`EXIT_WATCHDOG` is imported in `test_case_manager_host_serve.py` but never asserted on the
loop-failure path. This is the highest-risk gap: the seam works in isolation, but the full
host orchestration is unverified.

### Recommended fix

Add one test to `tests/test_case_manager_host_serve.py`:

```python
@pytest.mark.asyncio
async def test_loop_failure_through_serve_exits_watchdog(tmp_path, monkeypatch):
    manager = provision_manager(tmp_path)
    task = asyncio.ensure_future(serve(manager, stop_grace_secs=5.0))

    async def bad_tick():
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(manager, "_maintenance_tick", bad_tick)

    for _ in range(300):
        if manager.is_running:
            break
        await asyncio.sleep(0.02)

  # Wait for 3 consecutive failures → request_kill → watchdog ladder → exit 70
    with pytest.raises(_HardExit) as excinfo:
        await asyncio.wait_for(task, timeout=15.0)
    assert excinfo.value.code == EXIT_WATCHDOG

    death_records = list(manager._manager_dir.glob("manager_death_*.yaml"))
    assert len(death_records) >= 1
    assert "loop_failure" in death_records[0].read_text(encoding="utf-8")
```

Use the existing `hard_exit_recorder` fixture pattern so the test does not actually
terminate the pytest process.

### Acceptance criteria

- Test fails on `feature/manager-watchdog-host` without the test (proves gap), passes with it.
- Asserts `EXIT_WATCHDOG` (70), not merely that the loop task died.
- Optionally asserts a death record with `check: loop_failure`.

---

## Follow-up 2 (Important): Cooperative shutdown vs. `enable_mailbox`

### Problem

`MailboxProcessor.maintenance_tick()` returns immediately when `enable_mailbox=False`:

```python
async def maintenance_tick(self) -> None:
    if not self._policy.enable_mailbox:
        return
    self._ensure_dirs()
    self._check_shutdown_intake()
    ...
```

When `enable_mailbox=False` (e.g. `CaseManager.open_inprocess()` for tests/read-only
attach), cooperative shutdown pickup never runs. The watchdog path still scans shutdown
intake independently (~1s latency), so **`serve()` with `watchdog_enabled=True` still
works**.

But `enable_mailbox=False` + `watchdog_enabled=False` → the shutdown mailbox is
completely dead. This coupling is not documented in `submit_shutdown()` or the parent
proposal's §6.

### Options

**A. Hoist shutdown pickup above the gate (recommended).**

Shutdown is a process-control lever, not fleet mail. Move `_check_shutdown_intake()` to
run even when `enable_mailbox=False`:

```python
async def maintenance_tick(self) -> None:
    self._ensure_dirs()
    self._check_shutdown_intake()
    if not self._policy.enable_mailbox:
        return
    await self._process_fire_intake()
    ...
```

**B. Document the coupling.**

If the gate stays, document explicitly in `CaseManagerClient.submit_shutdown()` and
`serve()` docs:

- Cooperative shutdown requires `enable_mailbox=True`.
- Wedged-loop shutdown still works via the watchdog when `watchdog_enabled=True`.
- `enable_mailbox=False` + `watchdog_enabled=False` → shutdown mailbox is inert.

### Acceptance criteria

- Team picks A or B and records the decision here.
- If A: add a test that cooperative shutdown works with `enable_mailbox=False` under `serve()`.
- If B: docstring updates only; no behavior change.

---

## Follow-up 3 (Important): `watchdog_enabled=False` under `serve()`

**Status: doc-only half done (2026-07-16).** `serve()`'s docstring now states the
production expectation. The fallback `on_loop_failure` (fail-loud exit even with the
watchdog explicitly disabled) is still an open team decision — not implemented.

### Problem

When `watchdog_enabled=False`, `serve()` does not register `on_loop_failure`. A manager
hosted via `serve()` with watchdog disabled reverts to §1 embedded behavior: after three
consecutive loop failures the task dies, the process keeps running, and no exit code is
emitted. The existing test only verifies the watchdog thread is absent — not failure
behavior.

### Recommended fix

**Document as misconfiguration for production hosts — done.** `serve()`'s docstring now
reads:

> Production deployments should leave `watchdog_enabled=True` (the default). Hosting with
> `watchdog_enabled=False` disables in-process wedge remediation entirely: after three
> consecutive loop failures the task dies and this coroutine never returns, but no exit
> code is emitted — the process just sits there unless an external supervisor (health
> probe, orchestrator restart policy) catches the resulting stale heartbeat.

**Still open:** optionally register a fallback `on_loop_failure` when watchdog is
disabled:

```python
def _loop_failure_no_watchdog(exc: BaseException) -> None:
    logger.critical(
        "Manager loop failed with watchdog disabled; exiting with EXIT_WATCHDOG"
    )
    _hard_exit(EXIT_WATCHDOG)

if manager._policy.watchdog_enabled:
  ...
else:
    manager.on_loop_failure(_loop_failure_no_watchdog)
```

Only add the fallback if the team wants fail-loud behavior even when watchdog is explicitly
disabled (e.g. for integration tests that set `watchdog_enabled=False` but still expect
process exit on loop death).

### Acceptance criteria

- [x] Docstring clearly states production expectation.
- [ ] Team decides: doc-only (current state) vs. fallback exit on loop failure when
  watchdog disabled.

---

## Follow-up 4 (Suggested): Additional watchdog test coverage

The following paths are implemented but not covered by automated tests:

| Path | Risk if untested | Suggested test |
|---|---|---|
| `mailbox_neglect` detection | Stale intake never triggers exit 70 | Seed an old file in `fire_intake/`, arm watchdog with short `watchdog_mailbox_stale_secs`, assert exit 70 |
| Tick-duration alarm (`tick_slow`) | Long ticks starve leases silently | Set `_last_tick_started` old, `_last_tick_completed` None, assert escalation logged and **no** exit |
| Deliberate stop timeout → `EXIT_WATCHDOG` | `serve()` signal stop that can't settle exits 0 instead of 70 | Monkeypatch `manager.stop` to hang; SIGTERM; assert `_hard_exit(70)` |

These are lower priority than Follow-ups 1–2 but cheap to add and high diagnostic value.

---

## Follow-up 5 (Minor): Documentation and polish

**Status (2026-07-16):** the three cheap doc/test items are done. The operator-facing
deployment doc remains unwritten; the `--namespace` note was never a gap (see below).

### `is_idle` vs. shutdown intake race — done

`stop_when_empty=True` can observe `is_idle=True` while a shutdown request file is
landing in intake milliseconds later. The parent proposal accepts this as a latency gap,
not a correctness bug. The `is_idle` property docstring now carries the note:

> Does not include the shutdown mailbox — a shutdown file may arrive after an idle check
> but before the process exits.

### `is_recovered` after `stop()` — done

`test_lifecycle_properties` now asserts `manager.is_recovered is True` after `stop()`,
guarding Task 9's precondition (`is_recovered` staying true once set).

### `CaseManager.serve()` delegator — done

Added on `CaseManager` for discoverability:

```python
# case_manager.py
async def serve(self, **kwargs: Any) -> None:
    from totodev_pub.case_manager_host import serve as host_serve
    await host_serve(self, **kwargs)
```

`from totodev_pub.case_manager_host import serve` remains the blessed import path; this
is sugar only.

### Operator-facing deployment docs

The parent proposal's Docker compose example and Kubernetes `restartPolicy: Always` caveat
are implemented in code (`submit_shutdown` docstring) but not in a standalone operator
guide. Consider a short section in Tutorial 3 or a `docs/` note covering:

- `stop_grace_period` must exceed `serve(stop_grace_secs=...)`
- `init: true` + exec-form entrypoint
- `restart: on-failure` vs. k8s `restartPolicy: Always` (exit 0 does not mean stay down on k8s)
- Wiring `totodev-manager-health` as a liveness probe

### Health CLI `--namespace`

Defaults to `.case_manager`. Custom `manager_namespace` requires `--namespace` on the CLI.
Fine for v1; a future enhancement could read `case_manager_policy.yaml` from the cache root.

---

## Design note: why `case_advance()` has no self-pulse watchdog

(Referenced from `FolderBackedCase.case_advance()`'s docstring.)

A case cannot watchdog its own `case_advance()` from inside a single suspended
coroutine — if the step is wedged, so is any monitor living in the same coroutine. So
stall handling is split by failure mode instead of adding a self-pulse:

- **Stalled external job** — rides a `@DWELL>...` timed-escape edge that ripens with
  time; `case_advance()` fires it on a later pass. In-band, declarative, self-healing.
- **Genuinely stuck state** — no auto edge can fire now or ever ripen; surfaces as the
  BLOCKED outcome (a synthetic `AutoAdvanceBlocked` carried in
  `AdvanceResult.exceptions`), deterministic on every no-argument sweep.
- **Hung `case_advance()` call itself** — an out-of-band concern for the DRIVER (e.g.
  wrapping the call in `asyncio.wait_for`); the case cannot observe that itself. This
  is the layer the manager watchdog in this document backstops.

## Deferred from parent proposal (not in scope here)

These remain open in the parent proposal's "Remaining open questions" and are **not**
blockers for promoting the watchdog feature:

1. **Stop awaiting case steps inline in the maintenance tick** — root fix for idle-lease
   starvation during long mailbox fires; pulse task + alarm-only check contain the hazard
   for now.
2. **Independent lease-renewal cadence** — `_heartbeat_slice` still rides `driver.advance()`;
   pulse + tick-warn is the tripwire.
3. **Lease succession on requested restart (§7)** — team deferred; ~30s TTL wait is
   acceptable.
4. **Zero-code host CLI** — `python -m totodev_pub.case_manager_host`; follow-on after
   `serve()` stabilizes.

---

## Promotion checklist

Before merging `feature/manager-watchdog-host` to `main`:

- [ ] Follow-up 1: `serve()` → `EXIT_WATCHDOG` integration test
- [ ] Follow-up 2: decision on `enable_mailbox` vs. shutdown pickup (implement or document)
- [x] Follow-up 3: `watchdog_enabled=False` behavior documented — fallback-exit hardening
  still an open decision
- [ ] Follow-up 4: at least `mailbox_neglect` unit test (recommended)
- [x] Follow-up 5: `recovered` after `stop()` assertion (trivial) — done 2026-07-16, along
  with the `is_idle` docstring note and the `CaseManager.serve()` delegator. Operator
  deployment doc still outstanding (low priority).
- [ ] Full core test lane still green (1505+ pass; 6 pre-existing `test_case_machine_factory` failures acceptable)
- [ ] Move or archive this document to `_backlog/implemented/` or update status to **done** when complete

### What remains

The real remaining work, in priority order:

1. **Follow-up 1** — the highest-risk gap. No test drives three consecutive tick failures
   through a running `serve()` and asserts exit 70; the seam is only unit-tested in
   isolation (`ManagerWatchdog`) and via `test_serve_respects_watchdog_enabled_false`
   (which only checks thread absence, not failure behavior).
2. **Follow-up 2** — still an open design decision. `MailboxProcessor.maintenance_tick()`
   still gates `_check_shutdown_intake()` behind `enable_mailbox`, so
   `enable_mailbox=False` + `watchdog_enabled=False` (e.g. a misconfigured
   `CaseManager.open_inprocess()` host) leaves the shutdown mailbox completely dead.
   Needs a team call between Option A (hoist the check above the gate) and Option B
   (document the coupling instead).
3. **Follow-up 3's optional hardening** — whether to add the fail-loud
   `_loop_failure_no_watchdog` fallback when `watchdog_enabled=False`, or leave it
   doc-only as it is now.
4. **Follow-up 4** — `mailbox_neglect` (untested `_oldest_intake_age`/ladder branch),
   `tick_slow` alarm-only path, and a deliberate-stop-timeout → `EXIT_WATCHDOG` scenario
   under `serve()` are all still uncovered by tests.
5. **Follow-up 5's operator-facing deployment doc** — still unwritten; low priority.
