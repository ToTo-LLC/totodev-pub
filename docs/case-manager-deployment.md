# Running a CaseManager in production

How to host, supervise, and shut down a `CaseManager` process. For what a
CaseManager *is*, see `src/totodev_pub/case_manager.py`; for the host contract in
code, see `case_manager_support/case_manager_host.py`.

## The host

One process hosts one manager. `serve()` owns the whole lifecycle — recovery,
start, signals, exit codes, and the watchdog — so the host program is small:

```python
import asyncio
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_host import serve
from myapp.cases import InquiryCase

manager = CaseManager.open("/data/inquiries", register_types=[InquiryCase])
asyncio.run(serve(manager))
```

`serve()` requires a freshly constructed manager. Do not call `recover()` or
`start()` yourself — it sequences those, and raises `ValueError` if they have
already run.

## Exit codes

This is the supervisor contract. Everything the manager does *in reaction to*
something is nonzero, even when it shuts down cleanly.

| Code | Meaning | Restart? |
|---|---|---|
| **0** | Deliberate, final stop: SIGTERM/SIGINT, or a `stop_when` predicate returned True. | No |
| **70** | In-process liveness failure: a watchdog detection, a stop that would not settle, or three consecutive manager-loop failures with no watchdog running. | Yes, with backoff |
| **75** | A shutdown was requested through the shutdown mailbox. | Yes, promptly |

70 and 75 are deliberately distinct. 75 means "please come back" — it is the
normal outcome of an operator-initiated restart. 70 means the process could not
keep itself healthy; restarting is right, but a *loop* of 70s is an incident, not
a retry.

Exits at 70 and 75 go through `os._exit()`, bypassing Python cleanup by design:
that path has to work even when the interpreter is too wedged for a clean
shutdown. Do not rely on `atexit` hooks or context-manager teardown running.

## Restart policy

- Always restart on 70 and 75. **Use backoff on 70** — a crash-looping manager
  writes a death record per minute (below) and hammers whatever wedged it.
- Do not restart on 0. That is a scale-down or a completed job.
- `totodev-manager-health` (below) is the liveness probe; the exit code is the
  post-mortem.

Kubernetes: `restartPolicy: Always` plus a `livenessProbe` on the health CLI.
Docker Compose: `restart: unless-stopped`.

## Shutdown

**SIGTERM / SIGINT** — the deliberate stop. `serve()` drains in-flight steps and
returns, so the process exits 0. This is what an orchestrator sends on scale-down.

**The shutdown mailbox** — an out-of-process request, submitted with
`CaseManagerClient.submit_shutdown()`. Graceful drains first; immediate bounds
the unwind at ~2s. Either way the process exits 75.

Two properties worth knowing:

- **Shutdown requests are never honored across a restart.** `recover()` discards
  any request it finds at startup and logs it. A request that arrives while the
  process is down is not queued — resubmit it.
- **The shutdown mailbox is polled even when the request mailboxes are off.**
  `enable_mailbox=False` disables fire/adopt/reclassify intake, not shutdown.

### Grace periods must nest

`serve(stop_grace_secs=...)` (default 30s) bounds how long a stop may take.
**The orchestrator's kill timeout must be larger**, or it will SIGKILL the
process mid-drain and you lose the clean exit:

- Docker: `stop_grace_period` > `stop_grace_secs`
- Kubernetes: `terminationGracePeriodSeconds` > `stop_grace_secs`

A stop that exceeds `stop_grace_secs` is treated as a liveness failure and exits
70, not 0.

## The watchdog

An in-process daemon thread that detects a manager which has stopped making
progress. On a kill-authorized detection it escalates, dumps all thread
tracebacks, writes a death record, attempts a graceful stop, then exits 70 —
always 70, even if the graceful stop succeeded, so a detection never masquerades
as a clean exit.

| Detection | Default threshold | Outcome |
|---|---|---|
| `pulse_stuck` — the event loop stopped pulsing | 5s | exit 70 |
| `loop_task_dead` — the manager loop task died | immediate | exit 70 |
| `loop_failure` — 3 consecutive tick failures | immediate | exit 70 |
| `mailbox_neglect` — oldest intake file unserved | `max(10 × maintenance_interval, 30s)` | exit 70 |
| `tick_slow` — a tick is taking too long | `min(lease TTL, manifest_stale_secs) / 2` | **alarm only, always** |

`tick_slow` never kills, regardless of `watchdog_action` — a slow tick is a
symptom, not a wedge.

**`watchdog_action="alarm_only"`** keeps the diagnosis (escalation + traceback
dump) but never exits. Useful when an external supervisor owns remediation.

**`watchdog_enabled=False`** gives up detection — no pulse, task-death, or
mailbox-neglect monitoring — but *not* the fail-loud contract: three consecutive
loop failures still exit 70. Prefer leaving the watchdog on.

**Debuggers and coverage tracers force `alarm_only`.** A paused or traced process
looks exactly like a wedged one, so `serve()` downgrades when `sys.gettrace()` is
set. Consequence for CI: a coverage run does not exercise the real watchdog.

## Observability

**Health probe** — `totodev-manager-health /data/inquiries` (the cache root is positional):

| Code | Meaning |
|---|---|
| 0 | heartbeat fresh |
| 1 | heartbeat stale — page someone |
| 2 | `stopped_at` set — deliberately stopped, expected during decommission |
| 3 | no or unreadable manifest |

1 and 2 are distinct on purpose: a probe that conflates them pages people for
scale-downs. Use exit 1 as the liveness signal.

The probe reads the manifest file directly and never constructs a client, so it
stays useful when the manager itself is wedged. It catches what no in-process
mechanism can — "the process is gone" and "the restart loop is itself failing."

**Death records** — `<cache-root>/.case_manager/manager_death_<UTC>.yaml`, written
on every watchdog kill and on loop failure without a watchdog. Each names the
detection and the reason. `recover()` logs any from the last 24 hours at startup,
so a restart loop announces itself. Same-minute records overwrite, which
deliberately collapses a sub-minute crash loop to one file per minute.

**Notices** — register with `manager.on_notice(...)` for in-process
notification. One channel carries two families, told apart by
`kind.is_lifecycle`:

- **Problems** — `MANAGER_UNRESPONSIVE`, `ADOPT_REJECTED`,
  `TERMINATION_VERIFICATION_FAILED`, `MAINTENANCE_ITEM_FAILED`,
  `READMIT_ANOMALY`, and the rest. This is the hook for paging and metrics.
- **Departures** — `CASE_TERMINATED`, `CASE_QUARANTINED`, `CASE_EJECTED`: a case
  has left the pool. Normal events, not problems. Filter on `is_lifecycle` so a
  paging handler does not wake someone for a case closing normally.

**Departures are announced, not catalogued, and delivery is at-most-once.** The
manager keeps no durable ledger of departed cases; if the process dies between a
case departing and your handler persisting, the notice is lost. Subscribe and
keep your own record if you need history — this is operator convenience, not an
audit log.

**Fleet status board** — `.case_manager/fleet_status.jsonl`, a periodically
refreshed snapshot of every case in the pool. Read it with
`FleetStatusBoardWatcher`; disable with `enable_fleet_status_board=False`.

## Container caveats

- **Run one manager per cache root.** Two processes over one root will fight over
  leases. `recover()` detects a competing live manager, but do not rely on it as
  a scheduling mechanism.
- **The cache root must be a real, durable, POSIX-ish filesystem.** The manager
  relies on atomic rename and on mtime-based lease expiry. Network filesystems
  with weak rename semantics or coarse mtime granularity are not supported.
- **Do not run the cache root on a container-local layer.** It holds live case
  state; mount a volume.
- **Clock changes affect leases.** Lease expiry is baked into file mtimes, so a
  large backward clock jump makes leases look held for longer than they are.
