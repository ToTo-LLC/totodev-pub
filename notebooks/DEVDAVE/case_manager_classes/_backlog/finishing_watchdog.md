# Finishing: Manager watchdog, host entry point, and die-loudly contract

- Status: **follow-up work — implement before promoting to `main`.**
- Written 2026-07-10. The core feature landed on branch `feature/manager-watchdog-host`
  (worktree `.worktrees/manager-watchdog-host`, commits `eab148b`–`12c2a6a`).
- Parent proposal: [`proposed_manager_watchdog_and_host.md`](proposed_manager_watchdog_and_host.md)
  (implemented 2026-07-09).
- Implementation plan: [`docs/superpowers/plans/2026-07-09-manager-watchdog-and-host.md`](../../../../docs/superpowers/plans/2026-07-09-manager-watchdog-and-host.md).

## What shipped

The approved proposal is implemented end-to-end:

| Area | Module(s) | Notes |
|---|---|---|
| Loop hardening | `case_manager.py` | `on_loop_failure` seam, 3-strike retry |
| Liveness pulse | `case_manager.py`, `constants.py` | Heartbeat decoupled from tick duration |
| Watchdog | `case_manager_support/watchdog.py` | Daemon thread, kill ladder, death records |
| Host entry point | `case_manager_host.py` | `serve()`, exit codes 0 / 70 / 75 |
| Shutdown mailbox | `shutdown.py`, `mailbox/processor.py`, `case_manager_client.py` | Shared parser, dual pickup paths |
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
        if manager.running:
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

### Problem

When `watchdog_enabled=False`, `serve()` does not register `on_loop_failure`. A manager
hosted via `serve()` with watchdog disabled reverts to §1 embedded behavior: after three
consecutive loop failures the task dies, the process keeps running, and no exit code is
emitted. The existing test only verifies the watchdog thread is absent — not failure
behavior.

### Recommended fix

**Document as misconfiguration for production hosts.** Add to `serve()` docstring:

> Production deployments should leave `watchdog_enabled=True` (the default). Hosting with
> `watchdog_enabled=False` disables in-process wedge remediation; loop failures become
> silent after three retries unless an external supervisor (health probe, orchestrator
> restart policy) catches the stale heartbeat.

Optionally, register a fallback `on_loop_failure` when watchdog is disabled:

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

- Docstring clearly states production expectation.
- Team decides: doc-only vs. fallback exit on loop failure when watchdog disabled.

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

### `is_idle` vs. shutdown intake race

`stop_when_empty=True` can observe `is_idle=True` while a shutdown request file is
landing in intake milliseconds later. The parent proposal accepts this as a latency gap,
not a correctness bug. Add a one-line note to the `is_idle` property docstring:

> Does not include the shutdown mailbox — a shutdown file may arrive after an idle check
> but before the process exits.

### `recovered` after `stop()`

`test_lifecycle_properties` does not assert `manager.recovered is True` after `stop()`.
Task 9's precondition guard depends on `recovered` staying true once set. Add:

```python
await manager.stop()
assert manager.running is False
assert manager.recovered is True
```

### `CaseManager.serve()` delegator

The parent proposal allowed an optional one-line delegator on `CaseManager` for
discoverability. Not implemented. Consider:

```python
# case_manager.py
async def serve(self, **kwargs) -> None:
    from totodev_pub.case_manager_host import serve as host_serve
    await host_serve(self, **kwargs)
```

Low priority; `from totodev_pub.case_manager_host import serve` is the blessed import path.

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
- [ ] Follow-up 3: `watchdog_enabled=False` behavior documented (and optionally hardened)
- [ ] Follow-up 4: at least `mailbox_neglect` unit test (recommended)
- [ ] Follow-up 5: `recovered` after `stop()` assertion (trivial)
- [ ] Full core test lane still green (1505+ pass; 6 pre-existing `test_case_machine_factory` failures acceptable)
- [ ] Move or archive this document to `_backlog/implemented/` or update status to **done** when complete
