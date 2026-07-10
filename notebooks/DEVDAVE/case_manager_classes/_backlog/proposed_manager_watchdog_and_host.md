# Proposed: Manager self-watchdog, host entry point, and the die-loudly contract

- Status: **implemented 2026-07-09** — see
  `docs/superpowers/plans/2026-07-09-manager-watchdog-and-host.md`.
- Proposed 2026-07-09 (v3). `serve()` takes a pre-built `CaseManager`, not
  `(cache_root, **overrides)` — see "Resolved decisions" #2 for why. All team decisions
  are recorded in "Resolved decisions" near the end; open items are in "Remaining open
  questions" at the very end.

## Motivation

Many deployments will consist of simple programs whose main job is to host
`CaseManager`'s processing loop. That loop runs arbitrary subclass code (`perform_*`
hooks, guards, decorated I/O) inside one asyncio event loop, so it is exposed to the
full menu of ways a Python process goes bad: a hook that synchronously blocks the loop,
a deadlocked lock in a C extension, an unhandled exception that kills the loop task, a
subtly broken subsystem (e.g. mailbox processing) inside an otherwise-ticking loop.

Observable signs of a stuck or corrupted manager, from the outside:

- Case leases stop being renewed.
- A long time passes without a maintenance "tick" (stale manifest `heartbeat_at`).
- Inbound mailbox items accumulate unprocessed.

Today the library gives *clients* a way to notice this (`ManagerNotFreshError` via the
manifest heartbeat), but gives the **host process itself** nothing: no in-process
detection, no standard remediation, and no packaged way for a developer to write a
correct host program without hand-assembling recover/start/signal/shutdown wiring.

## What exists today (inventory)

| Mechanism | Where | What it covers |
|---|---|---|
| Manifest heartbeat (`heartbeat_at`, `manifest_stale_secs`, default 30s) | written at the end of every `_maintenance_tick()` | External detection of a dead/wedged manager, with detection lag |
| Case lease keepalive (driver `_heartbeat_slice()`, in-flight `_LeaseKeepalive` pulse) | inside `advance()` / an asyncio sibling task | Keeps a *live* manager's ownership fresh; expires within ~30s TTL when the manager wedges |
| Per-case escalations (`REPEATED_FAILURE`, `STALLED`, `AUTO_BLOCKED`) | `_detect_escalations()` each tick | Single-case stalls only — not queue- or manager-level |
| `_collect_stuck_triggers()` | only on `stop(timeout=...)` timeout | Shutdown diagnostics, not runtime monitoring |
| `recover()` gate (`RecoverRequiredError`), lease expiry, mailbox dead-lettering | startup | Makes hard process death an ordinary, safe recovery case |

Three facts shape the whole design:

1. **Every liveness mechanism above is cooperative.** The heartbeat, the lease pulses,
   and mailbox processing all run on the event loop they would need to report on.
   `_LeaseKeepalive`'s docstring is explicit: *"Cooperative only — blocking the event
   loop starves the pulse."* A synchronously blocked loop silences all of these signs
   at once. (This is coherent: a wedged manager loses its leases within the 30s TTL,
   which is exactly what makes it safe to replace.)
2. **A dead manager loop is currently silent.** `_manager_loop()` has no try/except. If
   any of its three awaits raises (e.g. an `OSError` writing the manifest), the loop
   task dies, the exception sits unretrieved, and the process lives on doing nothing.
   Clients eventually see a stale heartbeat; the host program itself never notices.
   (§2 repurposes the existing, currently-unused `_last_maintenance` field into explicit
   liveness stamps.)
3. **A healthy manager can legitimately look dead for minutes.** The maintenance tick
   awaits arbitrary case work *inline*: `_process_fire_intake()` (and the adopt and
   reclassify intakes) `await self._manager.fire(...)`, which awaits the **full case
   step to completion** — bounded only by the trigger's kill ceiling, which can
   legitimately be minutes. Because `driver.advance()` (which runs
   `_heartbeat_slice()`) and the manifest write are *serialized behind* the tick in
   `_manager_loop`, one slow mailbox fire already (a) lets idle pooled cases' leases
   lapse past the 30s TTL — violating the driver contract's "no lease ever expires"
   guarantee from the perspective of a concurrently recovering process — and (b) makes
   clients see `ManagerNotFreshError` for a manager that is fine. Any watchdog that
   keys on "tick completed recently" would convert this latent hazard into false
   kills. This fact forces the pulse-task design in §2 and is why the watchdog's
   kill-authorized check measures **event-loop responsiveness**, never tick duration.

## Design principle: detection can be smart; remediation must be dumb

A watchdog that merely kills the stuck thread is not enough, for three stacking
reasons:

1. **Python cannot safely kill a thread.** There is no public API; the
   `PyThreadState_SetAsyncExc` hack only delivers at a bytecode boundary, which a thread
   blocked in a C call or syscall never reaches — precisely the failure mode we care
   about.
2. **A successful thread kill creates a two-writer hazard.** Blocking work runs on
   executor threads (`case_run_blocking`), and "a running thread cannot be killed — the
   worker keeps going until `fn` returns." Kill the coordinator thread and executor
   threads may still be writing into case folders while the lapsed leases are reclaimed
   by a replacement manager. The lease-expiry safety story assumes the old **process**
   is dead, not just its coordinator thread.
3. **A corrupted interpreter cannot be trusted to repair itself.** Deadlock, extension
   misbehavior, heap damage — any in-process "recovery" code runs inside the same
   damaged runtime.

Therefore the unit of remediation is the **process**, and the library's crash-consistency
machinery (atomic renames, 30s lease TTL, mandatory `recover()`, mailbox dead-lettering)
already makes hard process death an ordinary event. The proposal leans into that: detect
precisely, diagnose loudly, die fast, and let a supervisor restart us. Everywhere else in
this document, "the watchdog never repairs — it detects, diagnoses, and dies" refers back
to this paragraph rather than re-arguing it.

## Code placement: CaseManager stays process-agnostic

`case_manager.py` is already ~1000 lines, and everything in this proposal except §1/§2
is a *process-lifecycle* concern (threads, signals, exit codes, `faulthandler`), which
is a different layer than fleet coordination. The partition principle:

> **`CaseManager` coordinates a fleet. A host runs a process. The watchdog observes a
> process. Support modules own mechanics.** `case_manager.py` must never import
> `signal`, `threading`, `faulthandler`, or define an exit code.

Concretely, this proposal's code lands as:

| Module | Contents | Notes |
|---|---|---|
| `src/totodev_pub/case_manager_host.py` (new) | `serve()` (incl. `stop_when`/`stop_when_empty`, §8), signal wiring, exit-code constants (`EXIT_WATCHDOG`, `EXIT_RESTART_REQUESTED`), watchdog arm/park orchestration, the shutdown-request execution path (drain vs. immediate) | The "worker/host class" layer. Everything that knows it owns a whole process lives here. A one-line `CaseManager.serve()` delegator MAY be added for discoverability; it must contain no logic. |
| `src/totodev_pub/case_manager_support/watchdog.py` (new) | `ManagerWatchdog` daemon thread, its checks, the kill ladder, death-record writing, `faulthandler` dumping | Depends on `CaseManager` only through the small read-only stamp/task surface in §2 plus the shutdown-scan helper below. Independently testable without a host. |
| `src/totodev_pub/case_manager_support/shutdown.py` (new) | `ShutdownRequest` model, the `SIGTERM` filename-token convention, the intake-scan/parse helper | Shared by three consumers: the mailbox processor (cooperative path), the watchdog (wedged path, §3), and `CaseManagerClient.submit_shutdown()`. One parser, one precedence rule, zero drift. |
| `case_manager_client.py` | `submit_shutdown()` | Thin delegation to `shutdown.py`, matching the existing `submit_fire`/`submit_adopt` shape. |
| `case_manager.py` | §1 loop hardening incl. an `on_loop_failure` callback seam (~25 lines), §2 pulse task and stamps (~30 lines), an `on_shutdown_request` callback seam (~10 lines), a read-only `is_idle` property (§8, a few lines) | Total growth well under ~80 lines. The manager gains *observability seams*, not lifecycle behavior. |
| health-probe CLI (new, small) | manifest read + exit code | Reads the manifest file directly — it must NOT construct a `CaseManagerClient` (whose constructor does a full `CaseManager.attach()`); a probe should be as dumb and failure-proof as possible. |

Both callback seams on `CaseManager` follow the same shape: a registration method (e.g.
`on_shutdown_request(cb)`, `on_loop_failure(cb)`), not behavior. The mailbox processor
parses a shutdown request and invokes its callback; `_manager_loop` invokes the other
after repeated failure (§1). `serve()` registers both — one to run the §6 protocol, one
to hand off to the watchdog's kill ladder (§3) — so `case_manager.py` keeps a single,
consistent dependency shape: it exposes seams, it never reaches into a support module
itself. An embedded `start()` user with no host registered gets a logged warning and the
event discarded — a manager that nobody hosts cannot promise process exit semantics.

This partition doubles as a template for future `case_manager.py` diet work (the fleet
board wiring and manifest assembly are candidates), but that refactor is out of scope
here; this proposal just avoids making that problem worse.

## Proposed changes

### 1. Make loop death loud (independent, do-first fix)

Wrap the body of `_manager_loop()` — **all three awaits** (`driver.advance()`,
`_reconcile_terminal_in_pool()`'s surroundings, `_maintenance_tick()`), not just the
tick — so an exception is logged with full traceback and triggers deliberate behavior
instead of silently killing the task. Concrete policy so the implementer doesn't have
to invent one:

- On an exception, log with traceback, sleep one maintenance interval, continue.
- On **3 consecutive** failed iterations (any successful full iteration resets the
  count), stop retrying and invoke the `on_loop_failure(exc)` callback (Code placement)
  — which `serve()` wires to the watchdog's kill ladder (§3). With no callback
  registered (embedded usage, no host), re-raise instead so the task dies *after*
  having logged loudly — still strictly better than today, though nothing then notices
  the task died until the next external observation (stale heartbeat, etc.).

Highest-probability real-world "corrupted but running" mode; near-zero cost; valuable
even if nothing else in this proposal is adopted.

### 2. Liveness pulse task — measure the loop, not the tick

Given fact 3 in the inventory, "did the tick complete recently" is the wrong liveness
question. The right one is "is the event loop turning." Add a tiny sibling coroutine,
started by `start()` and cancelled by `stop()`:

```python
async def _pulse_loop(self):
    while self._running:
        self._last_pulse = time.monotonic()      # the watchdog's kill-authorized signal
        self._maybe_write_manifest_heartbeat()   # decoupled from tick duration
        await asyncio.sleep(PULSE_INTERVAL_SECS) # ~0.5s
```

Properties:

- **A blocked or starved loop silences the pulse within one interval** — the pulse
  preserves the "all cooperative signals die together" property that makes the watchdog
  trick work, while being immune to legitimately long awaited work inside the tick
  (the loop keeps turning underneath an awaited `fire()`).
- **The manifest heartbeat moves here** (written on its own cadence, e.g. every
  `maintenance_interval_secs`), which fixes the pre-existing "healthy manager doing one
  slow mailbox fire looks stale to every client" hazard for free.
- The tick additionally stamps `_last_tick_started` / `_last_tick_completed`
  (repurposing the fossil `_last_maintenance`). Tick *duration* becomes a slow,
  **alarm-only** signal (§3) — long ticks are a throughput smell, not a liveness
  failure, and must never authorize a kill.
- Idle-case lease renewal (`_heartbeat_slice`) still rides `driver.advance()` in this
  cut; the tick-duration alarm is deliberately set at the lease-TTL horizon so the
  starvation hazard is at least *announced* before leases lapse. Moving lease renewal
  to an independent cadence is remaining-open-question 2; ending the inline awaiting of
  case steps in the tick is remaining-open-question 1 (the root fix).

### 3. `ManagerWatchdog` — a daemon *thread* inside the manager process

A plain `threading.Thread` (daemon) — immune to event-loop blockage, which is the entire
trick. Lives in `case_manager_support/watchdog.py`. It checks, on a short cadence (~1s):

| Check | Mechanism | Failure it catches | May kill? |
|---|---|---|---|
| Pulse liveness | §2's `_last_pulse` age exceeds threshold | Blocked or starved event loop (the primary target) | Yes |
| Loop-task death | `_run_task.done()` while `_running` is true → retrieve and log the stored exception | Loop task killed despite §1 (backstop) | Yes |
| Mailbox neglect | oldest **non-hidden** file mtime in the fire/adopt/reclassify `intake/` dirs vs. threshold | Subtle corruption: loop ticking but the mailbox processor broken | Yes |
| Shutdown request | any non-hidden file in the shutdown mailbox `intake/` (§6), parsed via `shutdown.py` | Not a failure — gives disk-only actors a shutdown lever that works even when the loop is wedged or busy, honoring the *requested* exit code instead of the wedge code | Executes §6 |
| Tick duration | `_last_tick_started` set, `_last_tick_completed` older than threshold | A tick awaiting case work long enough to threaten idle-case leases (inventory fact 3) | **No — alarm only, always** |

Thresholds derive from existing policy rather than introducing free-floating numbers:

- Pulse stuck: `max(10 × PULSE_INTERVAL_SECS, 5s)` — fires well before clients'
  `manifest_stale_secs` (30s) verdict, so the manager is usually restarting before the
  web tier ever reports it down. Safe at 5s now that the signal is immune to long ticks.
- Mailbox neglect: `max(10 × maintenance_interval_secs, 30s)` age on the oldest intake
  file (generous; intake→pickup is normally one tick). Skipped entirely when
  `enable_mailbox: false`; excludes the shutdown mailbox (§6) — a shutdown file is a
  command with its own pickup path, not a backlog. A large-but-fresh backlog does not
  alarm — that is throughput, a fleet-health question, not liveness.
- Tick duration (alarm only): `min(lease_TTL, manifest_stale_secs) / 2` — i.e. ~15s at
  the defaults, announcing the lease-starvation hazard before it bites.

The watchdog is **initialized with a fresh stamp at arm time** (so the first check
window never measures recovery, which can legitimately run long), and is **parked
whenever a deliberate shutdown begins** — see §4; an armed watchdog during
`stop(timeout=...)`'s settle (which stops the pulse by design) would otherwise turn
every clean `docker stop` into a nonzero "wedge" exit and resurrect deliberately
stopped containers.

**Escalation ladder on detection** (order matters — diagnose before dying):

1. Emit a `MANAGER_UNRESPONSIVE` escalation through the existing `EscalationRegistry`.
   Implementation notes: (a) `CaseEscalationKind` needs the new member —
   `emit_simple()` coerces strings through the enum and would raise `ValueError`
   otherwise; (b) every existing escalation fires on the event-loop thread and handlers
   were written under that assumption, so the watchdog first attempts dispatch via
   `loop.call_soon_threadsafe` and falls back to a direct call only if the loop cannot
   accept work (which is, after all, the diagnosis) — and the docs state that
   `MANAGER_UNRESPONSIVE` may arrive on the watchdog thread.
2. `faulthandler.dump_traceback()` to stderr — dumps every thread's stack, showing the
   exact line the loop is blocked on. Works from another thread even when the loop is
   wedged; this is the primary postmortem artifact.
3. Write a timestamped death record into the manager dir (reason, timestamps, which
   check fired) — filename pattern: `manager_death_YYYY-MM-DD_HHMM.yaml`, **UTC**. Two
   deliberate properties of this naming: an operator can eyeball recency without opening
   any file, and a directory that accumulates several files in a short span is a
   visible, file-system-level reliability signal on its own (files with the same
   minute-resolution name overwrite each other — accepted: collapsing a sub-minute
   crash loop to one file per minute beats writing hundreds of files, and per-minute
   accumulation still tells the story). Written with **primitive I/O only** —
   `open()`/`write()` to a temp name plus `os.replace()`, never
   `FileMappedPydanticMixin`: the mixin's locking/serialization machinery is exactly the
   kind of code a corrupted process might be wedged inside, and the death path must have
   no dependencies that can themselves hang. **The implementation must carry a comment
   at the write site explaining this**, so a future cleanup pass doesn't "helpfully"
   normalize it onto the mixin.
   On the next `recover()` run, the code finds `manager_death_*.yaml` files whose mtime
   (not filename) falls within the last 24 hours and logs **one line per file —
   timestamp, reason, which check fired — capped at the 5 most recent, plus the total
   count**, surfacing the reliability signal automatically without requiring the
   operator to know to look, or spamming the startup log.
4. Graceful attempt — **detection-aware**. For loop-task-death and mailbox-neglect
   detections (the process's event loop is still turning, even though `_manager_loop`
   itself died or is misbehaving): schedule `manager.stop(timeout=...)` directly via
   `asyncio.run_coroutine_threadsafe(...)` and wait a bounded grace period for it to
   finish, rather than raising `SIGTERM` at all. Scheduling the stop directly (instead
   of faking a signal) means the watchdog never has to share — or be confused with —
   the OS-signal path in §4, and it settles the question of what exit code a
   watchdog-triggered stop should use (see below). For **pulse-stuck** detections this
   rung is skipped (or capped at ~2s): a wedged loop won't run a scheduled coroutine any
   sooner than it would run a signal handler, so a long grace here would just burn time
   on a corpse.
5. Final: `os._exit(EXIT_WATCHDOG)` (70, see §4) — bypasses all Python cleanup, so it
   works even when the interpreter is too damaged for `sys.exit`. **This is also the
   exit path when rung 4's scheduled stop succeeds**, not just when it times out: a
   watchdog detection is never allowed to end in exit `0`, because exit `0` means
   "deliberate, external, successful stop" (§4) and a watchdog detection is neither
   external nor, really, successful — the manager just happened to be able to shut
   itself down cleanly on the way out. (This also resolves what was previously flagged
   here as an open question against the exit-code contract in §4.)

This mirrors the design principle above: the watchdog never repairs, it only detects,
diagnoses, and dies.

Policy knobs (Tier 2):

```yaml
watchdog_enabled: true            # PERMITS a watchdog; only a host (serve()) ever STARTS one.
                                  # start() never does. Embedded opt-in: an explicit
                                  # host-level call, never implicit.
watchdog_action: "exit"           # "exit" | "alarm_only"
watchdog_pulse_stuck_secs: null   # null → derived default above
watchdog_mailbox_stale_secs: null # null → derived default; also null-able to disable the check
watchdog_tick_warn_secs: null     # null → derived default; alarm-only regardless of watchdog_action
```

### 4. `serve()` — the blessed host entry point (`totodev_pub.case_manager_host`)

The real requirement is that many developers will write "a simple program that hosts the
loop." Rather than each of them hand-wiring recover/start/signals/watchdog/shutdown,
ship one entry point that a host program reduces to:

```python
import asyncio
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_host import serve
from myapp.cases import InquiryCase

manager = CaseManager.open("/data/inquiries", register_types=[InquiryCase])
asyncio.run(serve(manager))
```

**`serve()` takes an already-constructed `CaseManager`, not a `cache_root` +
`**overrides` pair that mirrors `open()`.** Earlier drafts of this proposal had
`serve()` accept `(cache_root, **overrides)` and forward the bag into `open()`
internally. Review of that shape surfaced a real coupling problem: `open()`/`attach()`
already slice one flat kwargs dict three ways (Tier 1 policy fields, Tier 2 policy
fields, and five specific wiring keys pulled by literal name — see
`CaseManager.attach()`), silently dropping anything that matches none of those. Having
`serve()` forward into that same ungoverned bag — or, worse, maintain its own parallel
filter to pull out its own kwargs (e.g. `stop_grace_secs`) before forwarding the rest —
piles a third loosely-typed consumption layer onto a boundary the "Code placement"
section above is otherwise careful to keep clean. Taking a pre-built `CaseManager`
instead has four concrete benefits:

- **Construction and hosting are textually separated.** Everything left of `serve(...)`
  is a fleet-construction concern (`open()`'s existing Tier 1/Tier 2/wiring semantics,
  including its `PolicyMismatchError` validation); everything `serve()` itself accepts
  is a process-hosting concern. No reader has to know which bucket a given kwarg falls
  into by memory.
- **`register_types` was always construction-time anyway** — `CaseManager.__init__`
  calls `registry.register_case_types(...)` immediately, before `serve()` would ever
  see the object. This shape makes that ordering explicit instead of tunneling it
  through a second layer.
- **Watchdog policy needs no `serve()`-side parameters at all.** `watchdog_enabled` and
  the `watchdog_*_secs` knobs are Tier 2 policy fields (§3), so they already live on
  `manager._policy` by the time `serve()` receives the manager. `serve()` just reads
  them off the object instead of re-accepting them.
- **Testability.** `serve()`'s own logic (signals, watchdog arm/park, shutdown exit
  codes) can be exercised against a manager built with a fake driver or an in-memory
  cache override, with no real cache root or disk I/O involved.

Per the code-placement section, `serve()` is a module-level function in a new
`case_manager_host.py`, not a `CaseManager` classmethod (an optional logic-free
delegator aside). Signature:

```python
async def serve(manager: CaseManager, *, stop_grace_secs: float = 30.0) -> None: ...
```

`manager` must not already be recovered or started — `serve()` owns that sequencing
itself (step 1 below) and does not attempt to detect or tolerate a manager handed to
it mid- or post-lifecycle; passing one that is already running is a caller error.
(This is also the moment to promote `CaseManager._recovered` / `_running` to public
read-only properties — `serve()` living in a different module needing to read them at
all, even just to assert the precondition, is a small sign the sequencing state
shouldn't be single-underscore-private.)

It wires, in order:

1. `recover()` → `start()` (which starts the §2 pulse task).
2. SIGTERM/SIGINT handlers → **park the watchdog** (or flip it to a shutdown-deadline
   mode whose sole check is "did `stop()` finish within `stop_grace_secs` plus margin,
   else `os._exit`") → `stop(timeout=stop_grace_secs)` → clean `stopped_at` in the
   manifest → **exit 0**.
3. Constructs the `ManagerWatchdog` and registers both callback seams (Code placement)
   on the manager: `on_loop_failure` → the watchdog's kill ladder (§1, §3), and
   `on_shutdown_request` → the §6 protocol.
4. Arms the watchdog (per `manager._policy`) with a fresh stamp — after `start()`, so
   recovery time is never measured.
5. Blocks until signaled, shutdown-requested, or watchdog-killed.

`stop_grace_secs` is a **`serve()` keyword argument** with a default (proposed 30s) —
it is host wiring, not deployment policy (unlike the watchdog knobs, it has no
sensible per-deployment-policy-file meaning independent of how `serve()` is invoked);
the Docker `stop_grace_period` comment in the compose example references it.

Exit codes are the supervisor contract, defined once in `case_manager_host.py`. The
governing rule is narrower than "0 = stop, nonzero = restart": **exit `0` is reserved
for a stop that both originates outside the manager's own reactive machinery and is
genuinely meant to be final** — an OS-signaled stop (step 2 above) or the deliberate
self-completion path in §8. Anything the manager does *in reaction to* something —
a detected failure, or a request that arrived through the recovery-oriented mailbox
lever in §6 — is nonzero, **even if it shuts down cleanly**, because "shut down
cleanly" and "this is a good time to stay down" are different questions and only an
external, deliberate stop gets to answer the second one with "yes."

- `EXIT_WATCHDOG = 70` (`EX_SOFTWARE`) for **all** watchdog kills, unconditionally —
  one code, not one per check, and not contingent on whether the watchdog's own
  graceful-attempt rung (§3, step 4/5) happened to succeed. Monitoring keys on a small
  stable family; "which check fired" lives in the death record and stderr dump where
  forensics belong.
- `EXIT_RESTART_REQUESTED = 75` (`EX_TEMPFAIL`, "temporary failure, retry") for
  **every** shutdown requested through the mailbox (§6) — the mailbox has exactly one
  outcome, "please come back," so there is nothing left to select; see §6 for why it
  never produces `0`.

Optionally, a zero-code CLI for the simplest deployments (agreed follow-on, not first
cut): `python -m totodev_pub.case_manager_host --cache-root /data/inquiries --types
myapp.cases:InquiryCase`. Packaging note: pick ONE convention — console scripts or `-m`
runners — for both this and the health probe in §5, not one of each.

### 5. Health-probe CLI (external backstop)

The manifest heartbeat is already the externally visible pulse; the missing piece is
packaging. A tiny command for orchestrators and alerting that **reads the manifest file
directly** (no `CaseManagerClient`, whose constructor does a full `CaseManager.attach()`
— a probe must be as dumb and failure-proof as possible):

```bash
totodev-manager-health /data/inquiries
# exit 0 — fresh
# exit 1 — heartbeat stale (page someone)
# exit 2 — stopped_at set (deliberately stopped; expected during decommission —
#          note a mailbox-triggered restart (§6) also sets stopped_at, so this
#          reads as "expected" for the brief window before the successor starts
#          and clears it; harmless in practice, since that window is the same
#          length as any ordinary restart cycle)
# exit 3 — no/unreadable manifest
```

Stale and deliberately-stopped are distinct codes on purpose: alerting wants "dead"
and "expected" distinguishable, and a probe that conflates them pages people for
scale-downs.

This catches what no in-process mechanism can ("the process is gone entirely" /
"the restart loop itself is failing") and is the piece Kubernetes and monitoring hook
into.

### 6. Remote shutdown request (mailbox-based)

The signal mechanism in §4 requires OS- or orchestrator-level reach on the manager
process. `CaseManagerClient` — by design — has none: Tutorial 3 §1 is explicit that
"the case-manager process and the web process never talk to each other directly.
Everything crosses through the cache root on shared disk." Today there is genuinely no
channel for a disk-only actor (a web tier, an admin console, a developer's script) to
ask the manager to shut down. This adds a fourth mailbox request kind, alongside
`FireRequest` / `AdoptRequest` / `ReclassifyRequest`, following the `*_mailbox_subdir`
convention those already use (e.g. `shutdown_mailbox_subdir`, with an `intake/`
directory). Request model and filename conventions live in
`case_manager_support/shutdown.py` (see code placement) so the mailbox processor, the
watchdog, and the client all share one parser.

**Two pickup paths, one protocol.** The mailbox processor scans the shutdown intake
each tick (the cooperative path) and invokes the host's registered callback. The
watchdog thread *also* polls the same intake (§3) — one extra stat per second — so a
shutdown request still works when the loop is wedged or busy inside a long tick, and
exits with the *requested* code rather than whatever the wedge detection would have
chosen. Without this, the one actor with no OS-level reach would lose its shutdown
lever precisely when the manager is least responsive.

**Protocol — deliberately dead simple, and discoverable by a sloppily-named file:**

- **Any non-hidden file present** in the shutdown mailbox's `intake/` dir → shutdown is
  triggered. ("Non-hidden" is load-bearing: the structured API writes a `.tmp`-style
  dotfile and `os.replace()`s it into place, matching the other mailboxes — a scan
  that counted dotfiles would fire on a half-written request.)
- Default behavior is an **immediate exit**: skip waiting for in-flight case steps to
  settle beyond a short, fixed grace window. Mechanics: the pickup path sets a
  shutdown-pending flag and returns; the host allows up to
  `IMMEDIATE_SHUTDOWN_GRACE_SECS` (proposed default 2.0s, tunable later) for the
  current loop iteration to unwind, then exits. This is intentionally the same "hard
  path" the system already tolerates (SIGKILL, watchdog exit) — leases lapse,
  `recover()` reconciles, in-flight mailbox items are dead-lettered — just requested on
  purpose instead of imposed by force. **One refinement over the hard path:** since a
  mailbox-requested exit runs in a *healthy* process (it just parsed the request), the
  host best-effort writes `stopped_at` into the manifest before exiting — one cheap
  file write that spares clients the 30-second "is it dead or stopped?" ambiguity.
  Reserve the truly-dirty exit for the watchdog.
- **A filename containing `SIGTERM`** (case-insensitive substring match) instead
  triggers a **graceful drain**: wait for in-flight steps to settle, bounded by the
  same `stop_grace_secs` `serve()` uses for its own signal handling (and with the same
  park-the-watchdog choreography).
- **Precedence rule:** when the file's content parses as a `ShutdownRequest`, the
  structured content is authoritative and the filename is ignored; the filename
  substring conventions apply only to unparseable or empty (hand-`touch`ed) files. Two
  encodings of the same fact with equal authority is how protocols drift.
- Whenever the *default* (immediate) path fires, the log entry explicitly names the
  convention that would have gotten a graceful drain instead — e.g. *"Immediate
  shutdown triggered by mailbox file `<name>`. For a graceful shutdown that waits for
  in-flight work to settle, include `SIGTERM` in the request filename (or use
  `CaseManagerClient.submit_shutdown()`)."* This means a developer who doesn't know or
  care about the protocol can `touch` any file in the intake dir and get a working (if
  blunt) shutdown; the log entry teaches the sharper option for next time.
- `CaseManagerClient.submit_shutdown(...)` is the structured entry point and writes a
  correctly-shaped file under the hood — sloppy manual file-dropping is a deliberate,
  documented fallback for quick/manual use, not the primary interface. There is no
  `restart` parameter: every mailbox-triggered exit already means "please come back"
  (see the exit-code bullet below), so a flag that could only ever be set to that one
  value would just be clutter — keeping things simple. `graceful` is the only axis:

  ```python
  handle = client.submit_shutdown(graceful=True, reason="operator-requested drain")
  ```

- **Result semantics** (the handle has to mean something for a process that is about
  to exit): on the graceful path, the manager writes an "acknowledged, shutting down"
  result file *before* initiating the drain, so `poll_result`/`wait_result` resolve
  normally. On the immediate path the handle may never resolve — documented; the
  manifest's `stopped_at` is the real confirmation either way.
- **The mailbox is a recovery lever, not a decommission lever — it never exits `0`.**
  `submit_shutdown`, and any hand-dropped file, request a shutdown of a process that
  might currently be misbehaving; the mailbox exists so a disk-only actor has *some*
  way to pull that lever without OS-level reach (§6 intro). It is deliberately not a
  general "manage this deployment" API. An operator who wants the manager to go away
  and *stay* away needs a lever with real decommission authority — an OS signal (§4
  step 2) sent by something with process/orchestrator reach, or the self-completion
  path in §8 if the decision to stop is the manager's own to make — not this mailbox.
  Concretely: **every** mailbox-triggered exit uses `EXIT_RESTART_REQUESTED` (75) —
  there is no `restart` flag to vary it by (dropped; see Resolved decision #8).
  `submit_shutdown`'s docstring makes the asymmetry explicit: *"This always exits
  nonzero — the case-manager process cannot be told through this API to stay down.
  Under a supervisor configured to restart on nonzero exit (e.g. Docker with
  `restart: on-failure`, this process as PID 1 via an exec-form entrypoint), it will
  come back. To decommission permanently, stop it by other means (an orchestrator
  scale-down, an OS signal from something with process reach, or — if the manager
  itself should decide when it's done — `serve(..., stop_when_empty=True)`, §8)."*
- **Startup hygiene.** A shutdown-mailbox file that is still sitting in `intake/` when a
  *new* process runs `recover()` (e.g. the request arrived after the old process was
  already down, or was never picked up before a crash) must be logged and discarded,
  never honored on the new process's behalf — otherwise a single stale request could
  induce a restart-immediately-after-recovering loop.
- The watchdog's mailbox-neglect check (§3) excludes the shutdown mailbox — a shutdown
  file is a command with its own pickup path, not a backlog.

### 7. Lease succession for a requested restart — DEFERRED

**Team decision (2026-07-09): not in scope.** We very seldom commit to hard real-time
availability, and we prefer reliability over speed: a requested restart paying the
existing ~30s lease-TTL wait — identical to today's hard-kill path — is an acceptable
cost, and §6 is fully useful without any fast-handoff machinery. This section records
what the review established, so the ground doesn't have to be re-surveyed if the
latency ever starts to matter.

**What happens today, concretely.** `CaseManager.stop()` and the driver's `settle()`
neither release nor hand off any case's lease — every pooled case's lease file is left
valid on disk, so even a clean, deliberate shutdown makes the successor wait out the
TTL (the recovery path's incremental reclaim admits each case as its frozen lease
lapses).

**Why the sketched handoff design is bigger than it looks.** Beyond the known
resume-race (closable by an atomic claim-by-rename of a published
`restart_handoff.yaml`), the review found two structural gaps:

1. **There is no bind-with-handoff seam.** `HeartbeatLease.from_handoff()` yields an
   active lease *object*, but a successor needs a live *case* bound to that lease —
   and `FolderBackedCase`'s binding path constructs its own lease and calls
   `acquire()`, which raises `LeaseAlreadyHeldError` on the very parked lease being
   adopted. A real implementation must thread a handoff parameter through
   `registry.rehydrate()` into case binding. That touches the case-lifecycle core and
   is most of the true cost.
2. **Ordering against the journal gate.** `handoff()` *beats* each lease (advances its
   mtime), and `restore_pool_from_journal()`'s phase-1 gate treats an advancing lease
   as a live competitor and aborts wholesale. The handoff claim must run strictly
   before the gate establishes its baseline, and claimed cases must be excluded from
   journal reconciliation.

**If succession latency ever matters, evaluate the simpler design first:**
release-on-clean-settle. On a graceful stop, after `settle()` completes, `release()`
each pooled case's lease (detaching the cases). A released lease means "not held" —
the successor acquires immediately, with no handoff record, no claim file, no new
binding seam; competing successors degrade to the same bounded `acquire()` race plus
the journal liveness gate that exists today. It would likely ship behind an explicit
flag (e.g. `stop(release_leases=True)`) since it changes what a stopped manager leaves
on disk. Only if *that* proves insufficient should the handoff-record design come back,
and then with its own design review — it touches the lease-safety invariant directly.

### 8. Deliberate self-completion — a "job manager" lever for `serve()`

§4 covers a perpetual server being told to stop from outside (OS signal). §6 covers a
disk-only actor pulling a recovery lever. Neither fits a host that is meant to run to
completion on its own: wake up, load a bounded batch of cases, drain the pool, and exit
— permanently, successfully, with nobody watching for the right moment to signal it.
Nothing external can know when that host is "done" except the host itself.

Proposal: give `serve()` an optional predicate it polls itself, on the same cadence as
the maintenance tick, and treats a `True` result exactly like an OS-signaled stop —
because, per §4's exit-code rule, that is what it is: a deliberate, successful,
external-to-the-*reactive*-machinery stop, even though the decision was made in-process
rather than by an operator.

```python
async def serve(
    manager: CaseManager,
    *,
    stop_grace_secs: float = 30.0,
    stop_when: Callable[[], bool] | None = None,
    stop_when_empty: bool = False,
) -> None: ...
```

- `stop_when` is checked once per maintenance interval, synchronously, on the manager's
  event loop — same trust model as the tick itself: a blocking predicate is the
  caller's bug, exactly like a blocking `perform_*` hook.
- On a `True` result: park the watchdog, `stop(timeout=stop_grace_secs)`, write a clean
  `stopped_at`, **exit `0`** — the same sequence §4 uses for an OS-signaled stop, because
  the exit-code rule cares about *why* the process is stopping, not about who noticed
  first.
- `stop_when_empty=True` is sugar for the overwhelmingly common predicate — "nothing
  left to do" — built from a new read-only introspection point,
  `CaseManager.is_idle` (no pooled cases, no pending mailbox intake), in the spirit of
  the `_recovered`/`_running` promotions already proposed in §4. Mutually exclusive
  with passing an explicit `stop_when`.
- A predicate composes better than a single flag: "empty *and* two consecutive empty
  polls" (to avoid racing a mailbox item that lands the instant after the check) or
  "empty *and* a wall-clock deadline has passed" are both expressible without adding
  more `serve()` parameters. `stop_when_empty` is offered anyway because most job
  managers shouldn't have to hand-write `lambda: manager.is_idle`.
- **This is a process-management concern, not a fleet concern**, per "Code placement":
  it lives entirely in `serve()`/`case_manager_host.py`; `CaseManager` only grows the
  one read-only `is_idle` bit.
- A queue item that lands in the gap between the `stop_when` check and the process
  actually exiting is not a correctness problem, only a latency one — it sits in
  `intake/` until something (a restarted perpetual host, or an operator) picks it up,
  the same "safe to die anytime" story the whole proposal already leans on.

Naming/polarity confirmed: `stop_when` (`True` → stop), reading consistently with
`on_shutdown_request` and `stop_when_empty` — see Resolved decision #12.

## Behavior under Docker

Docker is the friendliest environment for this design, but it inverts one common mental
model: **plain Docker does not detect unresponsiveness — it only reacts to process
exit.** Even a failing `HEALTHCHECK` merely marks the container "unhealthy" in vanilla
Docker/docker-compose; nothing restarts it (only Kubernetes `livenessProbe`, Swarm, or a
bolt-on like autoheal act on health). Under plain Docker, the in-process watchdog is
therefore the **only** reliable remediation trigger — the process is its own watchman,
and Docker is the resurrection service.

Expected runtime behavior:

1. Watchdog detects a wedged/dead loop (or its own bounded graceful-stop attempt for a
   loop-task-death/mailbox-neglect detection — §3 step 4 — times out or simply
   finishes) → stack dump to stderr (captured and retained by `docker logs` after
   death) → `os._exit(70)` either way.
2. Container exits nonzero → `restart: on-failure` / `unless-stopped` starts a fresh one.
3. Fresh process runs `recover()`: pool rebuilt from disk, the dead process's leases
   expire within the 30s TTL and are reclaimed, half-executed mailbox items are
   dead-lettered with explicit error results.
4. `docker stop` (deploys, scale-down) → SIGTERM → watchdog parked → graceful `stop()`
   → exit 0 → with `restart: on-failure`, the container stays down as intended. A
   `serve(..., stop_when_empty=True)` job manager (§8) reaches the same exit-0 ending
   on its own once its pool drains, with no signal involved.
5. A disk-only actor (the web tier, an admin script) drops a request into the shutdown
   mailbox (§6) — always exits `EXIT_RESTART_REQUESTED` (75), regardless of the
   actor's `graceful` (drain) choice: `docker logs` lets an operator tell a mailbox
   recovery-kill (75) apart from a detected wedge (70) apart from a deliberate stop
   (0) — but under `restart: on-failure`, (5) **always** comes back, unlike (4).

Required Docker configuration:

```yaml
services:
  case-manager:
    # 1. Exec-form entrypoint (in the Dockerfile): ENTRYPOINT ["python", "-m", "myapp.host"]
    #    Shell form wraps the process in /bin/sh, which swallows signals.
    init: true                    # 2. tini as PID 1: zombie reaping; our handlers still get SIGTERM.
    restart: on-failure           # 3. The resurrection service. Consider max_attempts + alerting
                                  #    so a crash-loop is noticed rather than spinning forever.
    stop_grace_period: 45s        # 4. MUST exceed serve()'s stop_grace_secs — Docker's default
                                  #    10s would SIGKILL a manager mid-settle. (SIGKILL is still
                                  #    safe — that's the hard path recover() exists for — but a
                                  #    clean stopped_at is nicer for clients.)
    volumes:
      - casedata:/data/inquiries  # 5. The cache root MUST survive container replacement;
                                  #    recover() reconstructs everything from these folders.
```

Cautions specific to containers:

- **PID 1 signal semantics.** A PID 1 process ignores signals with no installed handler.
  `serve()` installs handlers, so `docker stop` works; `init: true` plus exec-form
  entrypoint keeps this robust regardless.
- **Do not scale replicas against one cache root.** `replicas: 2` would put two managers
  in a lease fight over the same folders. One manager per cache root; availability comes
  from fast restart, not redundancy.
- **Keep stderr unredirected** inside the container so the faulthandler dump survives in
  `docker logs`.
- On Kubernetes, additionally wire the health-probe CLI as a `livenessProbe` — there it
  genuinely restarts the pod and backstops the watchdog itself.
- **Kubernetes caveat on the exit-code contract:** Deployments default to
  `restartPolicy: Always`, which restarts a container that exits **0** too — so
  "0 = stay down" holds under docker-compose `on-failure` but NOT under the most
  common k8s deployment shape. Decommission intent on k8s means scaling the workload
  down; the exit code alone cannot express "stay down" there. Document this in
  `submit_shutdown`'s docstring alongside the existing supervisor caveat.

## Behavior on a developer desktop (no Docker)

The same code runs unwrapped; the differences are who restarts it and how failures
present:

- `asyncio.run(serve(...))` in a terminal. Ctrl-C (SIGINT) → watchdog parked → graceful
  `stop()` → clean `stopped_at` → exit 0. Identical shutdown path to `docker stop`.
- The shutdown mailbox (§6) works identically here — useful on a desktop for a
  developer's *other* terminal or script to stop a locally-running manager without
  finding its PID, and for exercising the remote-shutdown code path in tests without
  Docker at all.
- If a hook wedges the loop, the developer sees the full escalation in the terminal:
  the `MANAGER_UNRESPONSIVE` escalation log line, then the faulthandler dump of every
  thread's stack — **pointing at the exact line of their code that blocked the loop** —
  then process exit with the watchdog code. This turns the watchdog into a first-class
  *development* diagnostic, not just a production safety net: the most common cause (a
  synchronous call inside a `perform_*` that should have used `case_run_blocking`) is
  identified by the dump directly.
- Nothing restarts the process automatically — there is no supervisor. That is the
  correct desktop default: the developer reads the dump, fixes the hook, reruns. (Anyone
  wanting restart-on-crash locally can use their own loop — `while true; python -m ...;
  done` honoring exit 0 — but we should not build one in.)
- `watchdog_action: "alarm_only"` is the knob for debugger sessions, where a paused
  process would otherwise look exactly like a wedged one and get killed mid-breakpoint.
  `serve()` detects a debugger (`sys.gettrace()`) and defaults to alarm-only with a log
  note, so breakpoints don't fight the watchdog by default.

## What this proposal deliberately does not do

- **No in-process repair.** One remediation path — process death + `recover()` (Design
  principle) — for every failure mode, including interpreter corruption.
- **No manager-level "queue stuck" heuristics** (e.g. "all cases blocked for an hour").
  That is a fleet-health question, orthogonal to process liveness, and better served by
  the fleet board / escalations. Could be a follow-on proposal.
- **No fast lease succession.** Deferred — see §7 and Resolved decision #10.
- **No watchdog for the watchdog.** The watchdog thread does trivial, allocation-light
  work (compare two floats, stat a directory); if *it* dies, the external probe
  (manifest staleness) remains as the backstop. Two layers is enough.

## Resolved decisions (architectural review + team, 2026-07-09)

1. **Placement:** see "Code placement." `serve()`, the watchdog, and the shutdown
   protocol live in new modules; `case_manager.py` gains only the pulse task, loop
   hardening, and two callback seams.
2. **`serve()` signature:** `serve(manager, *, stop_grace_secs=...)`, taking a
   pre-built `CaseManager`, not `(cache_root, **overrides)`. Full rationale in §4.
3. **Watchdog scope:** armed by `serve()` only, never by `start()` (embedded/pytest
   usage legitimately blocks the loop) — policy knobs in §3. Debugger sessions default
   to alarm-only (desktop behavior section).
4. **Exit codes:** `0` is reserved for a stop that is both external to the manager's
   reactive machinery and genuinely final — an OS-signaled stop (§4) or self-completion
   (§8). `EXIT_WATCHDOG` (70, `EX_SOFTWARE`) for *all* watchdog kills, unconditionally
   — including when the watchdog's own graceful-attempt rung (§3) succeeds.
   `EXIT_RESTART_REQUESTED` (75, `EX_TEMPFAIL`) for *every* mailbox-triggered shutdown
   (§6), unconditionally — the mailbox is a recovery lever, not a decommission lever,
   and never gets to answer "stay down." "Which check fired" / "which request" is
   forensics, kept out of the exit status — see §4, §6.
5. **Death-record surfacing at `recover()`:** one log line per recent file, capped at 5
   plus a total count; primitive I/O only, never `FileMappedPydanticMixin`;
   minute-resolution filenames accepted. Detail in §3, escalation step 3.
6. **Mailbox-neglect check:** skipped when `enable_mailbox: false`; excludes the
   shutdown mailbox; a large-but-fresh backlog does not alarm (throughput, not
   liveness). Detail in §3.
7. **Zero-code CLI:** follow-on, not first cut — `serve()` is the contract.
8. **`graceful` is the only axis on a mailbox shutdown request — `restart` is dropped.**
   Once every mailbox exit is unconditionally `EXIT_RESTART_REQUESTED` (decision #4,
   #11), a `restart` flag could only ever be set to the one value that's already true
   — pure clutter. `submit_shutdown(graceful: bool = False, reason: str | None = None)`;
   no `restart` parameter, no `RESTART` filename token. Team decision 2026-07-09,
   superseding the earlier "two independent booleans" framing.
9. **Filename convention:** parsed file *content* is authoritative whenever present;
   filename substrings apply only to unparseable/hand-touched files. Detail in §6.
10. **Lease succession:** deferred — see §7. Reliability over speed; revisit
    release-on-clean-settle before any handoff-record design if latency ever matters.
11. **Mailbox shutdown requests never exit `0`.** The shutdown mailbox (§6) is a
    recovery lever for actors with no OS-level reach, not a general "manage this
    deployment" API — an actor that wants the process to stay down needs a different
    lever (an OS signal, or the manager's own self-completion path, §8). Team decision
    2026-07-09.
12. **Self-completion (§8):** `serve()` gets an optional `stop_when` predicate (polled
    on the maintenance-tick cadence, `True` → stop) plus `stop_when_empty=True` sugar,
    for hosts built to run to completion rather than perpetually; on `True`, it exits
    exactly like an OS-signaled stop (`0`). Naming/polarity (`stop_when`, `True` means
    stop) confirmed 2026-07-09.

## Remaining open questions

1. **Should the maintenance tick stop awaiting case steps inline?** The root cause of
   inventory fact 3 is that `_process_fire_intake` (and adopt/reclassify) await full
   case steps inside the tick. A fire-and-write-result-on-completion shape (schedule
   via the driver, persist the result from the step's completion path) would fix the
   idle-lease starvation and tick-latency hazards at their source. That is a behavioral
   change to mailbox semantics deserving its own short proposal; this proposal only
   contains the hazard (pulse task + alarm-only tick-duration check).
2. **Should idle-case lease renewal move to an independent cadence in this cut?**
   Moving `_heartbeat_slice` onto the pulse cadence would need a small public driver
   seam (e.g. `driver.heartbeat_now()`) and interleaves lease beats with tick work at
   await points (same loop, so no parallelism — but a driver-contract review is
   warranted). First cut keeps it riding `advance()` with the tick-duration alarm as
   the tripwire; question is whether that's good enough to ship or the seam goes in now.
3. **Final numbers:** pulse interval (proposed 0.5s), pulse-stuck threshold (proposed
   `max(10 × interval, 5s)`), tick-warn derivation (proposed
   `min(lease_TTL, manifest_stale_secs) / 2`), `IMMEDIATE_SHUTDOWN_GRACE_SECS`
   (proposed 2.0s), `stop_grace_secs` default (proposed 30s).
4. **Packaging convention:** console scripts (`totodev-manager-health`) vs. `-m`
   module runners — pick one for both the host CLI and the health probe.
5. ~~Watchdog graceful attempt vs. the exit-code contract~~ — **resolved 2026-07-09**:
   the watchdog now schedules its own stop directly rather than raising `SIGTERM`, and
   always exits `EXIT_WATCHDOG` regardless of whether that stop finished cleanly. See
   §3 escalation steps 4–5 and Resolved decision #4.
6. ~~What is `restart` still for, on a mailbox shutdown request?~~ — **resolved
   2026-07-09**: dropped. Once every mailbox exit is unconditionally
   `EXIT_RESTART_REQUESTED`, the flag could only ever be set to the one value that's
   already true, so it added nothing. See §6 and Resolved decision #8.
7. ~~`stop_when` naming/polarity (§8)~~ — **resolved 2026-07-09**: `stop_when`,
   `True` → stop. See Resolved decision #12.
