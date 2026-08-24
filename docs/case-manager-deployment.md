# Running a CaseManager in production

How to host, supervise, and shut down a `CaseManager` process. For what a
CaseManager *is*, see `src/totodev_pub/case_manager.py`; for the host contract in
code, see `case_manager_support/case_manager_host.py`.

## Three layers

A managed fleet runs as three things, and knowing which one owns what is most of
operating it:

| Layer | Owns |
|---|---|
| **Host** (`serve()`) | The process: signals, exit codes, the watchdog, **and shutdown** |
| **Signaling adapter** | The file-drop request transport: fire, adopt, reclassify, results |
| **`CaseManager`** | The fleet. No transport, no process lifecycle |

`serve()` sequences all of it, so the host program stays small:

```python
import asyncio
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_host import serve
from totodev_pub.case_manager_support.signaling_adapter import SignalingAdapter
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from myapp.cases import InquiryCase

store = CaseManager.open_local_store("/data/inquiries")
case_type_registry.register_case_types(InquiryCase)
manager = CaseManager(store)
asyncio.run(serve(manager, adapter=SignalingAdapter(manager)))
```

`open_local_store()` is the only call that brings a filespace into existence; it
creates one on the first run and opens it on every run after. Constructing a
manager never creates anything, so a host pointed at the wrong path fails loudly
instead of standing up an empty fleet. Once the filespace exists, a process that
should never create one can skip the first line and pass the path straight to the
constructor.

**Omit `adapter=` and the process serves no requests** — nothing drains
`requests/queued/`. That is a supported shape, not a degraded one: it is what an
embedded host looks like, driving the manager through its own methods. Shutdown
still works, because the host owns it and polls `requests/shutdown/` directly.

The on-disk shape those requests travel through is
[the layout map](case-manager-layout.md), which is generated from the code.

`serve()` requires a freshly constructed manager. Do not call `recover()` or
`start()` yourself — it sequences those (and the adapter's own recovery), and
raises `ValueError` if they have already run.

## Exit codes

This is the supervisor contract. Everything the manager does *in reaction to*
something is nonzero, even when it shuts down cleanly.

| Code | Meaning | Restart? |
|---|---|---|
| **0** | Deliberate, final stop: SIGTERM/SIGINT, or a `stop_when` predicate returned True. | No |
| **70** | In-process liveness failure: a watchdog detection, a stop that would not settle, or three consecutive manager-loop failures with no watchdog running. | Yes, with backoff |
| **75** | A shutdown was requested through the shutdown mailbox. | Yes, promptly |
| **1** | Startup refused: an unhandled exception before the fleet was running. In practice almost always `CompetingManagerError` — another manager already owns this cache root. | Yes, with backoff |

70 and 75 are deliberately distinct. 75 means "please come back" — it is the
normal outcome of an operator-initiated restart. 70 means the process could not
keep itself healthy; restarting is right, but a *loop* of 70s is an incident, not
a retry.

1 is Python's own exit code for an exception that escaped `serve()`, not a code
the host chooses. It is listed because it is *reachable in normal operation* and
a supervisor must not treat it as unexpected-and-unrecoverable. The way to reach
it is to point two workers at one cache root: the lease refuses the second one
before it can touch anything (see "One manager per cache root, enforced"), and
refusing to start is the containment. Restarting with backoff is correct — the
incumbent may be a corpse whose lease has yet to lapse, in which case a later
attempt succeeds — but a sustained loop of 1s means two workers are genuinely
configured against the same directory, which no amount of restarting fixes.

Anything else nonzero is a bug; report it rather than tuning a restart policy
around it.

Exits at 70 and 75 go through `os._exit()`, bypassing Python cleanup by design:
that path has to work even when the interpreter is too wedged for a clean
shutdown. Do not rely on `atexit` hooks or context-manager teardown running.

## Restart policy

- Always restart on 70 and 75. **Use backoff on 70** — a crash-looping manager
  writes a death record per minute (below) and hammers whatever wedged it.
- Do not restart on 0. That is a scale-down or a completed job.
- Restart on 1 with backoff, and alert on a sustained loop of them: a manager
  that cannot claim its cache root will not start no matter how often it tries.
- `totodev-manager-health` (below) is the liveness probe; the exit code is the
  post-mortem.

Kubernetes: `restartPolicy: Always` plus a `livenessProbe` on the health CLI.
Docker Compose: `restart: unless-stopped`.

## Shutdown

**SIGTERM / SIGINT** — the deliberate stop. `serve()` drains in-flight steps and
returns, so the process exits 0. This is what an orchestrator sends on scale-down.

**A shutdown request** — submitted out-of-process with
`CaseManagerClient.submit_shutdown()`. Graceful drains first; immediate bounds
the unwind at ~2s. Either way the process exits 75.

Two properties worth knowing:

- **Shutdown requests are never honored across a restart.** `recover()` discards
  any request it finds at startup and logs it. A request that arrives while the
  process is down is not queued — resubmit it.
- **Shutdown is always available.** The host polls for it, not the fleet and not
  the adapter, so it works with no adapter attached, with `enable_mailbox=False`,
  and with the watchdog off. Asking a process to stop is process control, and
  the layer that owns exit codes owns it.

That ownership is why shutdown keeps its own leaf, `requests/shutdown/`, rather
than joining `requests/queued/` with every other action. Its protocol is "any
non-hidden file" rather than a parsed envelope, and routing it through the queue
would make the host parse messages the adapter owns — breaking exactly the case
(`enable_mailbox=False`) where stopping the process matters most.

### Grace periods must nest

`serve(stop_grace_secs=...)` (default 30s) bounds how long the *host* will wait
for `CaseManager.stop()` to finish. Stop itself has no timeout — aborting it
mid-teardown is unsafe — so a grace expiry is a process-level hard exit, not a
cancelled stop. **The orchestrator's kill timeout must be larger**, or it will
SIGKILL the process mid-drain and you lose the clean exit:

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
| `mailbox_neglect` — oldest request left in `requests/queued/` | `max(10 × maintenance_interval, 30s)` | exit 70 |
| `tick_slow` — a tick is taking too long | `min(lease TTL, manifest_stale_secs) / 2` | **alarm only, always** |

`tick_slow` never kills, regardless of `watchdog_action` — a slow tick is a
symptom, not a wedge.

**`watchdog_action="alarm_only"`** keeps the diagnosis (escalation + traceback
dump) but never exits. Useful when an external supervisor owns remediation.

**`mailbox_neglect` needs an adapter.** With no request transport attached
nothing owns a queue backlog, so the check is structurally absent rather than
merely disabled — there is nothing that could be neglected.

It measures `requests/queued/` only. A request in `claimed/` is already the
fleet's problem and may legitimately sit there waiting for a concurrency slot or
a choke permit, so counting it would turn a healthy backpressure signal into a
process kill.

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
| 0 | heartbeat fresh, **or** recovery in progress within its grace window |
| 1 | heartbeat stale, or recovery overran its grace window — page someone |
| 2 | `stopped_at` set — deliberately stopped, expected during decommission |
| 3 | no or unreadable manifest |

1 and 2 are distinct on purpose: a probe that conflates them pages people for
scale-downs.

### One manager per cache root, enforced

`recover()` claims a heartbeat lease on the filespace before it does anything
else — the same primitive a case holds on its own folder, one scope up. A second
manager over the same root raises `CompetingManagerError` and never starts.

**Claiming can take up to ~60s after a hard kill, and that is the point.** A held
lease is not evidence of a live owner: a SIGKILLed manager leaves one behind with
its expiry still in the future. The two are only distinguishable over time, so
acquisition watches for longer than one beat period and then decides:

- the expiry **advanced** — an owner is alive. Fail immediately; no wait will
  outlast a lease that is being renewed.
- the expiry is **frozen** — a dead process's shadow. Wait for it to lapse
  (bounded at two lease TTLs), then claim it and log that the previous owner
  died rather than stopping cleanly.

A clean `stop()` releases the lease, so an orderly restart pays none of this.
Read-only `CaseManagerClient`s never claim it — ownership is taken by `recover()`,
not by construction, which is what lets a client attach to a running fleet.

If a manager loses the lease while running — someone else took it — the pulse
raises `LeaseOwnershipLostError` into the loop-failure path and the manager stands
down rather than competing.

### What recovery reports

Every `recover()` logs one INFO line — what the pool restored, orphans revived,
tickets still pending, adopt-drop counts — whether or not anything went wrong. A
fleet that came back from a crash and says nothing is indistinguishable from one
that had nothing to do.

Two conditions log at ERROR instead, because each is a standing defect rather
than a repair:

- **Orphans that could not be rehydrated.** These are live cases nobody will ever
  drive, and they will fail identically on every restart until resolved. The
  usual cause is a build whose case classes no longer match what is on disk — a
  renamed class, or a state removed from an FSM while cases still sit in it.
  Watch this line after every deploy; it is the one that catches a rollout
  stranding live work.
- **Pooled cases the store no longer calls live.** These are evicted, with the
  case's real status named. Under a singly-owned cache root this cannot happen,
  so it means something else wrote to the filespace.

Both are also delivered as `READMIT_ANOMALY` notices, and both are on
`manager.last_recover_report` for a host that wants to act on them.

**`strict_recovery` turns either into an exception.** Off by default: one
unrestorable case must not ground a fleet that is otherwise healthy. Turn it on
in dev and test, where the same case is a defect that should stop the build
rather than scroll past — `recover()` then raises `RecoveryIntegrityError`
carrying both lists. The detection and the logging are identical either way; only
the landing changes.

### Restarting after a crash takes ~30 seconds, and that is correct

A manager that was SIGKILLed leaves its cases' heartbeat leases held. The
replacement cannot simply take them: a held lease is indistinguishable from a
*live* owner's until you watch it for longer than one heartbeat period. So
recovery observes, waits for the leases to lapse, and only then reclaims —
measured at **~29s for 25 cases**, bounded at two lease TTLs. It is a fixed cost,
not proportional to fleet size.

**Nothing beats during that window**, because the manager loop has not started.
The probe therefore reports **0 — recovering**, from a `recovering_at` stamp the
manifest carries, for up to 120s. This matters more than it looks: a liveness
probe that read recovery as death would kill the process every single time,
restarting the wait from zero, and a crashed fleet would never come back at all.

Consequences for orchestrators:

- Set `failureThreshold` × `periodSeconds` comfortably above 120s, or rely on the
  probe's own grace window and leave the threshold small.
- **Back off on exit 70.** A crash-loop that restarts faster than a lease TTL
  spends every cycle waiting on leases and never reaches steady state. Use exit 1 as the liveness signal.

The probe reads the manifest file directly and never constructs a client, so it
stays useful when the manager itself is wedged. It catches what no in-process
mechanism can — "the process is gone" and "the restart loop is itself failing."

**Death records** — `<cache-root>/.case_manager/manager_death_<UTC>.yaml`, written
on every watchdog kill and on loop failure without a watchdog. Each names the
detection and the reason. `recover()` logs any from the last 24 hours at startup,
so a restart loop announces itself. Same-minute records overwrite, which
deliberately collapses a sub-minute crash loop to one file per minute.

**Notices** — subscribe with `manager.subscribe_notices(...)` (drop with
`unsubscribe_notices(handle)`). Discrete in-process announcements, not a live
feed of case activity. One channel carries two families, told apart by
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

**Fleet status board** — optional observer (`FleetStatusBoard`) that maintains an
in-memory abbreviated snapshot of the live pool and may publish
`.case_manager/fleet_status.jsonl` for out-of-process readers
(`FleetStatusBoardWatcher` / `CaseManagerClient.read_fleet_status`). `serve()`
attaches a publishing board by default when `enable_fleet_status_board=True`
(policy host knob). Pass `fleet_status=False` to skip, or pass a custom board
instance (e.g. `publish_file=False` for in-memory only).

## Container caveats

- **Run one manager per cache root.** `recover()` claims a lease on the filespace
  and raises `CompetingManagerError` if another manager holds it, so a second
  process refuses to start rather than corrupting shared state. Enforced, not
  advisory — but it is a safety net, not a scheduling mechanism: two managers
  started in the same instant can both see an unheld lease, and only the second
  one's next heartbeat catches it.
- **The cache root must be a real, durable, POSIX-ish filesystem.** The manager
  relies on atomic rename and on mtime-based lease expiry. Network filesystems
  with weak rename semantics or coarse mtime granularity are not supported.
- **Do not run the cache root on a container-local layer.** It holds live case
  state; mount a volume.
- **Clock changes affect leases.** Lease expiry is baked into file mtimes, so a
  large backward clock jump makes leases look held for longer than they are.
