# QueuedCasePoolDriver — Design Spec (v1 draft)

> **Status:** DRAFT — design-fidelity thinking, **not** production code.
> No implementation until approved.
>
> **Companion classes (already implemented):** `FolderBackedCase`,
> `CasePoolDriver` (ABC), `TieredCasePoolDriver`, `StateChainParser` /
> `FsmChainSpec`, `AdvanceResult`.
>
> **Relationship:** a stand-alone sibling/subclass of `TieredCasePoolDriver` —
> the family has always envisioned multiple pool-driver classes with different
> planning/execution strategies; this is the second one.

---

## 0. Problem & positioning

`TieredCasePoolDriver` is a load balancer at heart: every live case gets a
cadence (HOT/WARM/COLD) and the fleet makes a-little-progress-everywhere. That
is the right default. But some deployments want the opposite bias:

- **Finish cases one by one** rather than making incremental progress on many.
  At any given time, case #1 should have top claim on the pool's capacity while
  others wait their turn.
- Case activity is **bursty**: a case rushes forward through several automatic
  transitions, then halts at a manual gate awaiting user interaction, possibly
  for days. The "front of the line" should belong to whoever is actively
  bursting, oldest first.
- Some transitions consume **constrained resources** — CPU-heavy work, external
  APIs with concurrency ceilings ("ms-graph-api"), scarce connections
  ("sqlite-conn"). The pool should throttle these with counted permits, and
  when a permit is contested, **seniority wins**: the front-of-the-line case is
  always granted a permit it wants before anyone behind it.

So this driver's scheduling policy is *queue-ordered, seniority-first, with
choke-resource throttling* — behaving much more like a work queue than a
load balancer.

**What it is NOT:** it does not change what a case *is* or how a case takes a
step (`case_advance()` is untouched in its contract); it does not do
disk/folder management (same disclaimers as the tiered driver); and it does not
implement true *rate* limiting (requests/second) — a choke is a **concurrency**
cap (simultaneous executions), which is a workable proxy for most rate-limited
APIs but not a token bucket. (See §10 open questions.)

---

## 1. Requirements (as settled in design conversation)

1. **FIFO preeminence.** The sweep visits cases in queue order (oldest
   admission first); contested capacity — the in-flight concurrency ceiling and
   all choke permits — is granted front-first.
2. **Winner-takes-all permits.** A freed choke permit must never be "luckily
   grabbed" by a mid-queue case while a more senior case wants it. Seniority is
   enforced structurally, not probabilistically (§4f).
3. **Demote on wake, not on stall** (option "C", refined). A case that halts at
   a manual-only state *keeps its physical position* — it consumes nothing
   there, so its position is harmless. The demotion is enforced at the moment
   it **wakes**: when a case transitions from a manual-only state back to
   auto-advanceable, it moves to the **tail** of the queue. Position rewards
   continuous progress, not raw seniority.
4. **Declarative choke requirements.** Case types declare which triggers draw
   on which named resources *declaratively* (required class var
   `fsm_trigger_chokes`, possibly DSL later) — never procedurally inside hook
   code. `{}` means no constrained resources.
5. **Pool-level quantities.** The *names* of resources live with the case type;
   the *quantities* (permit counts) are deployment configuration passed to the
   driver's constructor (from config files or similar).
6. **Keep the tiered machinery's virtues.** Blocked cases must not be hammered
   (cadence ladder), failures must back off, dead-ends must demote, COLD must
   stay a slow watch loop — none of that is being re-invented.

---

## 2. Design overview: queue on top of tiers (the hybrid)

Two layers, answering two different questions:

| Layer | Question it answers | Mechanism |
|---|---|---|
| **Tier / cadence** (inherited) | "Is this case *due* for a poll this beat?" | HOT/WARM/COLD `skip_countdown`, failure backoff, no-op demotion ladder — unchanged from `TieredCasePoolDriver` |
| **Queue order** (new) | "Among the due, who gets contested capacity first?" | Sweep iteration order = queue order; permits and in-flight slots granted in that order |

The key enabling observation: `_by_folder` is a plain `dict`, and Python dicts
iterate in insertion order — **the tiered driver's sweep already visits cases
in admission order**. The queue is not a new data structure; it is a promotion
of that incidental ordering into a contract, plus one mutation (requeue-on-wake
= pop + re-insert, moving an entry to the tail).

So the deltas over `TieredCasePoolDriver` are exactly three:

1. **Requeue-on-wake** (§3) — move a case to the tail when it wakes from a
   manual-only state.
2. **Choke permit gating** (§4) — a due case whose needed permits are not
   available this beat is skipped (retry next beat), and grants happen in
   sweep = queue order.
3. **Ordering guarantees** (§5) — the sweep-order/backpressure behavior that
   was incidental becomes documented contract (e.g. the concurrency-ceiling
   backpressure now favors seniors *by design*).

A bursting case at the front needs no special machinery: progress promotes it
to HOT (`M_HOT = 1`), so it is due **every beat**, and being first in queue it
claims permits and in-flight slots before anyone else. The burst-FIFO behavior
is emergent from the two layers.

---

## 3. The queue: ordering rules

**Admission.** `add()` appends to the tail (dict insertion — free). Oldest
admission = front, exactly as today.

**Stall at a manual gate.** No action. The case's tier machinery demotes it
(accelerated ladder — it is a structural dead-end for auto purposes) and it
sits wherever it sits, cheap to poll and consuming nothing. Its position among
other stalled cases is meaningless because position only matters when
capacity is contested, and stalled cases contest nothing.

**Requeue-on-wake (the one queue mutation).** Each slot tracks the case state
the driver last observed (`last_seen_state`). Whenever the driver notices the
state has changed — at step completion, or when the sweep polls a case whose
state moved out-of-band — and the **previously seen state was manual-only**
(no auto exits — statically knowable from the FSM spec) while the new state is
auto-advanceable, the case has just woken: move its entry to the tail. Two
observation points, covering both wake paths:

- **Step completion** (`_complete_step`): a manual trigger delivered via
  `fire()` progressed the case out of a manual-only state — the usual UI wake.
- **Pre-launch check in the sweep:** a direct out-of-band trigger invocation
  on the case object moved it while the driver wasn't looking; the comparison
  against `last_seen_state` catches it on the case's next poll, before the
  step launches (so the woken case bursts from the tail, not from its stale
  senior position).

Note what does **not** requeue: a case bursting through auto states stays put
(its steps' initial states have auto exits); `boost()` does not move position;
tier changes never move position. Position changes on exactly two events —
admission and wake.

**Detection needs one small companion surface** (§6): the driver must ask
"does state X have any auto exits?" against the case's compiled spec. This is
structural FSM data (same character as `case_advanceable`), not domain
introspection, so it does not violate the driver's "never introspects domain
logic" doctrine.

---

## 4. Choke resources

### 4a. Declaration — class-level data, mirroring `fsm_state_chains`

`@CHOKE(...)` was considered as a DSL guard and rejected: a choke does not act
like a guard (it never selects *which* trigger fires) — it acts like a
**delay** (it can slow *when* a trigger fires). It is metadata about the
trigger's work, consumed by the pool, invisible to the case's own FSM
execution. The nearest existing precedent is the `~<dur>` soft-timeout
annotation, which is likewise a per-trigger property of the work
(`perform_<trigger>`), stored in `FsmChainSpec.trigger_timeouts`.

**Decided (implemented):** every concrete `FolderBackedCase` subclass must
declare `fsm_trigger_chokes` explicitly — `{}` is valid and means this case
type draws on no named constrained resources. Omitting it raises
`MissingTriggerChokesError` at **class-definition time** (same fail-fast
spirit as malformed chains). The base class carries a sentinel; subclasses
inherit nothing usable until they set the attribute.

Proposed declaration — a class variable beside `fsm_state_chains`:

```python
class InvoiceCase(FolderBackedCase):
    fsm_state_chains = [
        "^received --analyze-->analyzed --post_to_graph-->posted ==archive-->done^",
    ]
    # Required. Trigger name -> named throttled resources its work draws on.
    fsm_trigger_chokes = {
        "analyze":       {"cpu"},
        "post_to_graph": {"ms-graph-api", "sqlite-conn"},
    }
```

Compilation: `compile_fsm()` (which already reads `fsm_state_chains` off the
class) also reads `fsm_trigger_chokes` via `_fold_trigger_chokes()` and stores
it as `FsmChainSpec.trigger_chokes: dict[str, frozenset[str]]`, validated the
same way hook names are — a key naming an unknown trigger is a build-time error
(typo protection, consistent with `orphan_detection`). Author-time values are
plain `set[str]`; compile normalizes to `frozenset[str]`. Empty resource sets
on a trigger are ignored (trigger omitted from the compiled map).

**Future DSL sugar (deferred, non-blocking):** an edge annotation such as
`received--@CHOKE(cpu)#triggerX-->analyzed` could later compile into the same
`trigger_chokes` field, with the same conflict rule as soft-timeouts (the same
trigger annotated with two *different* choke sets is a parse error; annotated
+ bare edges are fine, the annotation wins). Because both inputs land in one
spec field, adding the sugar later costs nothing now. The class-var form ships
first because chokes are pool-facing metadata, and keeping the guard grammar
un-overloaded is worth more than one-line-ness.

**Out-of-pool behavior:** a case driven outside any pool (direct trigger
invocation) ignores chokes entirely. The declaration says "this work draws on
resource X"; only a pool decides whether X is scarce.

### 4b. Quantities — pool construction config

```python
driver = QueuedCasePoolDriver(
    choke_limits={"cpu": 4, "ms-graph-api": 2, "sqlite-conn": 1},
    concurrency_ceiling=50,
)
```

`choke_limits` comes from deployment config. Two validations, both **strict**
(decided — supersedes the earlier warn-once leaning):

- **At construction:** every limit must be an `int >= 1`. A limit of 0 would
  make any case drawing on that resource permanently unlaunchable — that is a
  config error, not a throttle, and it is the one configuration that could
  produce a *permanent* stall (§4g).
- **At `add()`:** every resource name in the admitted case's compiled spec
  (union over `FsmChainSpec.trigger_chokes` values) must appear in
  `choke_limits`. A missing name raises `UnconfiguredChokeError` (working
  name) with a message naming the case class, the missing resource(s), and
  the remedy, e.g.:

  > `InvoiceCase declares choke resource 'cpu' (trigger 'analyze') but this
  > pool's choke_limits does not configure it. Pass choke_limits={"cpu": <n>,
  > ...} to the driver constructor.`

`add()` is the right chokepoint: it is the single admission door
(`restore_pool_from_journal` also funnels through `driver.add()`), and it is
the earliest moment where both inputs — the case's compiled spec and the
pool's config — coexist. Because the declared names are static class data,
the validation result can be cached per case class (a set of
already-validated types); in practice the check is a frozenset-subset test,
so the cache is an optimization in name only.

Rationale for raising rather than warn-and-run-unthrottled: silently running
a declared-scarce resource unthrottled defeats the point of declaring it, and
the failure would surface at the worst time (production overload) instead of
the best (admission). The cost — config and code must deploy together when a
case type grows a new resource name — is accepted.

### 4c. Governor semantics — beat-quantized, never awaited

A small owned component (working name `ChokePermitGovernor`) holds the counts.
Its rules are the heart of the winner-takes-all guarantee:

1. **Try-acquire only, during the sweep, in queue order.** A sweep-launched
   step never *waits on* a permit (the one sanctioned waiter is `fire()`,
   §4e). A due case whose permits are unavailable is simply not eligible this
   beat: `skip_countdown = 1`, re-checked next beat (identical shape to the
   existing concurrency-ceiling backpressure).
2. **Snapshot at sweep start.** Available counts are frozen when the sweep
   begins; a permit released mid-sweep (step completions are async events and
   WILL land mid-sweep) becomes visible only to the **next** sweep. This
   closes the last lucky-grab window (§4f).
3. **Release on step completion** — all permits acquired for a step are
   returned when the step finishes, on every path (success, failure,
   exception, eviction).

Cost of quantization: a freed permit idles for at most one beat (~`I0`,
default 0.5 s) before reuse. Cheap for what it buys.

### 4d. Acquisition point — pre-launch union over auto candidates

The structural wrinkle: `case_advance()` chooses its trigger *internally*, so
the driver cannot know at launch which choke set the step will need. Guards
live case-side (`guard_<name>` methods, factual guards), so the driver cannot
pre-select the edge either.

**Rule: before launching an auto step, the driver computes the UNION of choke
sets across ALL auto out-edges of the case's current state, and try-acquires
that union. All-or-nothing: if the union is not fully available, the case
skips this beat.** Whatever edge `case_advance()` then picks is covered by
construction; the full union is released at completion.

Trade-offs, considered and accepted:

- *Over-acquisition:* permits for candidate edges that did not fire are held
  for the duration of one step. States with multiple differently-choked auto
  exits are rare; the window is one step.
- *Over-blocking:* one scarce resource can delay an unrelated sibling edge out
  of the same state. Same rarity argument; and the conservative direction is
  the safe one for rate-limited APIs.
- The rejected precise alternative — threading a pool-owned `resource_gate`
  callable into `case_advance()` for atomic acquire-at-edge-selection — buys
  precision at the cost of making the case's advance path pool-aware, a
  coupling the family has deliberately avoided. Kept in the back pocket if
  over-acquisition ever measurably hurts.

### 4e. `fire()` — manual triggers respect chokes, with super-priority

A UI-fired trigger consumes real resources too; letting `fire()` bypass chokes
would defeat the throttle exactly where bursts of user activity make it matter
most. But `fire()` returns an awaitable result, so it can afford what the
sweep cannot: **waiting.**

Rule: `fire()` performs a *priority acquire* of the pinned trigger's choke set
(known precisely — no union needed). If available, it proceeds immediately;
if not, it awaits, and pending priority acquires are serviced **before** the
next sweep hands out any queue-order grants. Manual user intent outranks the
queue; the queue outranks nobody twice (a senior case merely waits one more
beat, which is normal backpressure).

**The waiting acquire is atomic, all-or-nothing.** A waiter holds *no*
permits while queued — the governor grants a pending request only at a moment
when its full set is available, never piecewise. This is load-bearing:
piecewise acquisition by waiters is the one pattern that could deadlock two
overlapping `fire()` calls (§4g). Implement the governor's wait queue as
whole-set grants only.

`fire()` with **no** trigger (manual auto-sweep request) uses the §4d union
rule, but as a priority acquire.

### 4f. Why there is NO next-trigger prediction / reservation machinery

An earlier design sketch protected senior cases by *predicting* their next
triggers' resource wants and making junior cases pass on permits accordingly.
That machinery — assuming fired triggers won't fail, handling multiple
possible next triggers, protecting top-K only — is **dropped**, because
beat-quantized grants (§4c) remove the race it was solving:

- The only moment anyone acquires is during the sweep, front-first. A senior
  case's *future* want becomes a *present* want at the very next sweep, where
  it outranks everyone by position.
- A mid-sweep release is invisible until the next sweep, so a junior visited
  later in the *same* sweep cannot luckily grab it.

**Residual (accepted) case:** a senior case that is in-flight when a permit
frees cannot claim it (it is dormant in the sweep); a junior may legitimately
take it next beat, and the senior may find the cupboard bare when it
completes. If a real workload shows this biting, the minimal future fix is a
reservation knob — "hold back permits wanted by in-flight cases among the top
K queue positions" — which is statically computable from `trigger_chokes`
(union over the current state's auto exits), no prediction required. Deferred:
YAGNI until measured.

### 4g. Multi-resource safety — deadlock vs stall

**Deadlock (circular wait) is structurally prevented**, including when a case
(or `fire()` caller) needs several named resources at once (e.g.
`{"ms-graph-api", "sqlite-conn"}`). The governor never implements
hold-and-wait on a partial set:

| Path | Acquire rule | While blocked |
|---|---|---|
| Sweep (§4c, §4d) | Try full union; on failure acquire **nothing** | Case skips this beat (`skip_countdown = 1`); holds no permits |
| In-flight step | Full union at launch; released only at completion | Never requests additional permits mid-step |
| `fire()` waiter (§4e) | Grant only when **entire** pinned set is free | Waiter holds **no** permits while queued |

The classic two-waiter deadlock — A holds `cpu` waiting for `api`, B holds
`api` waiting for `cpu` — requires piecewise acquisition. That pattern is
explicitly rejected for `fire()` (§4e). The sweep path cannot create it
because it never waits and never retains a partial grant.

**Implication for overlapping `fire()` waiters:** with whole-set grants and
FIFO servicing among waiters, two pending manual triggers wanting overlapping
resource sets may both wait, but neither blocks the other's progress by
holding a subset. Liveness is preserved: permits cycle through in-flight
work → release → priority waiters → next sweep.

**Not deadlock — throttle vs misconfiguration:**

- **Temporary throttle:** limit too tight, or union over-acquisition (§4d)
  holding permits for an edge that did not fire — cases skip beats but
  eventually run as permits free. Normal backpressure.
- **Permanent stall (misconfiguration):** a `choke_limits` entry of `0`, or
  (equivalently) a resource name declared on the case type but missing from
  `choke_limits` if validation were ever relaxed — a case drawing on that
  resource can never launch. Constructor validation (`int >= 1`) and strict
  `add()` validation (§4b) exist to fail fast before the pool enters this
  state.

No resource-acquisition **ordering** convention (always take `cpu` before
`api`, etc.) is required: ordering rules are a deadlock workaround only when
partial grants are allowed. This design disallows partial grants everywhere.

---

## 5. The sweep (behavioral pseudocode)

```text
sweep_once():
    governor.snapshot()                        # freeze available permit counts (§4c)
    service_pending_priority_acquires()        # fire() waiters, FIFO among themselves (§4e)
    for slot in queue order:                   # = _by_folder insertion order
        if slot.skip_countdown <= 0: continue  # dormant (closed / in-flight)
        slot.skip_countdown -= 1
        if slot.skip_countdown != 0: continue  # not due this beat (tier cadence)
        if halt_requested: park; continue
        if slot state changed out-of-band AND last_seen_state was manual-only:
            move slot to queue tail               # wake observed by poll (§3)
        slot.last_seen_state = current state
        if in_flight_count >= ceiling:
            slot.skip_countdown = 1; continue  # ceiling backpressure — seniors got slots first
        needed = union of trigger_chokes over auto exits of slot's current state   (§4d)
        grant = governor.try_acquire(needed)   # against the sweep-start snapshot
        if grant is None:
            slot.skip_countdown = 1; continue  # choke backpressure: retry next beat
        if live_or_evict(slot):
            launch_case_step(slot, grant)      # grant released on completion, all paths
        else:
            governor.release(grant)

on step completion (extends _complete_step):
    governor.release(grant)
    ... existing reclassify / reload / events ...
    if result.progressed and result.initial_state was manual-only:   # woke via fire() (§3)
        move slot to queue tail
    slot.last_seen_state = current state
```

Everything else — heartbeat slice, rehydrate-or-evict chokepoint, halt
settlement, event emission order, `settle()` — is inherited behavior.

---

## 6. Companion changes required outside the driver

Small, and each independently sensible:

| Where | Change | Why |
|---|---|---|
| `FsmChainSpec` | field `trigger_chokes: dict[str, frozenset[str]]` (default empty) | one home for choke metadata; future DSL sugar lands here too |
| `FolderBackedCase` | required class var `fsm_trigger_chokes: dict[str, set[str]]` (sentinel on base; `{}` valid); `MissingTriggerChokesError` if omitted; `_fold_trigger_chokes()` in `compile_fsm()` | the declarative surface (§4a) |
| `FolderBackedCase` | small public read surface for the driver, e.g. `case_pending_chokes` (property: union of `trigger_chokes` over the current state's auto exits) and `case_state_has_auto_exits(state)` (classmethod or property variant) | §3 wake detection + §4d union, without the driver touching private `_fsm` / `_forward_candidates` |
| `TieredCasePoolDriver` | gentle seam extraction if subclassing (see below): the launch-eligibility check and the completion hook need to be overridable without copying the sweep | keep one sweep implementation |

**Subclass vs sibling.** Leaning: subclass `TieredCasePoolDriver`. The store
is already insertion-ordered; the new behavior is a wrapped sweep, a wrapped
completion hook, a governor, and a queue-move helper. If the private-method
surgery gets ugly, fall back to a sibling class sharing extracted helpers —
but the ABC stays untouched either way.

---

## 7. Diagnostics

Extend the existing pull-based surfaces rather than adding event chatter:

- `peek()` gains: `queue_position: int`, `choked: frozenset[str] | None`
  (resources that blocked its most recent skip, if any).
- `snapshot()` gains: `chokes: {name: {"limit": L, "in_use": U}}` and
  `fire_waiters: int`.
- No new pool events initially. A `THROTTLED` event per skipped beat would be
  noisy; if observability demands it later, a *streak-crossing* event (case
  choked N consecutive beats) is the shape to add. (§10)

---

## 8. Inherited unchanged (for the record)

- The `CasePoolDriver` ABC — untouched, all abstract methods still satisfied.
- Tier policy, cadence ladder, failure backoff, phase staggering.
- Heartbeat walk, lease keepalive, rehydrate-or-evict, eviction events.
- `boost()` semantics (schedule soon; now also does not move queue position).
- `request_halt()` / `HALTED` / `remove()` lifecycle; `settle()`; `stop()`.
- Event names and firing order (ALERTED → ADVANCED → FAILED / CLOSED).

---

## 9. Testing notes

The governor and the queue rules are the new logic; both are highly unit-testable:

1. **Governor alone:** snapshot semantics (mid-sweep release invisible until
   next snapshot), all-or-nothing union acquire, priority-acquire servicing
   order, release-on-every-path.
2. **Ordering:** with `advance(suggested_interval_secs=0.0)` + `settle()`
   (the tiered driver's deterministic test idiom), verify (a) front case gets
   the last permit when two cases contend; (b) a freed permit goes to the most
   senior *due* case on the next beat, never a junior on the same beat;
   (c) requeue-on-wake moves exactly the woken case to the tail; (d) ceiling
   backpressure admits seniors first.
3. **Burst FIFO end-to-end:** two auto-burst cases admitted in order; case #1
   drains to its manual gate before case #2 makes its first step (with a
   1-permit choke on the burst trigger).
4. **`fire()` priority:** a waiting `fire()` beats the whole queue to the next
   freed permit; a `fire()` on an unthrottled trigger is unaffected.
5. **Declaration validation:** unknown trigger in `fsm_trigger_chokes` fails
   at class definition (`FsmChainParseError`); omitting `fsm_trigger_chokes`
   fails at class definition (`MissingTriggerChokesError`); unknown resource
   name at pool `add()` raises `UnconfiguredChokeError` with remedy text (§4b).

---

## 10. Open questions to resolve as we refine

| Question | Notes / leaning |
|---|---|
| **Class name** | `QueuedCasePoolDriver` (working). Alternatives: `FifoCasePoolDriver` (crisper policy name, slightly wrong — it's hybrid), `SeniorityCasePoolDriver` (most precise for the contention rule). Decide before implementation. |
| **Unknown resource at `add()`: warn or raise?** | **Resolved — raise.** `UnconfiguredChokeError` at `add()` (§4b). Silent unthrottled execution defeats declarative chokes; fail at admission, not under load. |
| **Concurrency cap vs true rate limit** | Chokes are semaphore-style concurrency caps. If a real requests/second budget emerges (e.g. Graph throttling responses), a token-bucket resource type could slot into the governor behind the same names. Defer. |
| **Top-K reservation for in-flight seniors** | Deferred per §4f. Revisit only with workload evidence. |
| **`fire()` wait: unbounded or timeout?** | Leaning: honor the caller's own timeout discipline (asyncio.wait_for at the call site); no driver-imposed cap. Revisit if mailbox (§6e of CaseManager model) needs a bound. |
| **Multi-pool / multi-process chokes** | Two drivers (or processes) each get their own governor — limits are per-driver. A shared cross-process governor (file/lease-based, like everything else in this family) is a separate future design if a truly global API budget appears. |
| **Choked-streak observability** | Add a streak-crossing event or leave to `snapshot()` polling? Start with polling. |
| **DSL `@CHOKE` sugar** | Deferred; compiles into `FsmChainSpec.trigger_chokes` with soft-timeout-style conflict rejection when/if added (§4a). |
| **Starvation of juniors** | Deliberate and desired ("job #1 has top priority while others wait"). No aging mechanism. Documented so nobody "fixes" it later. |

---

## 11. Mapping from `TieredCasePoolDriver`

| Tiered driver | This driver | Notes |
|---|---|---|
| `_by_folder` dict, incidental insertion order | same dict, order is **contract** (the queue) | pop + re-insert = move to tail |
| sweep visits all due slots, order irrelevant | sweep order = grant order (seniority) | §5 |
| ceiling backpressure (`skip_countdown = 1`), arbitrary order | same mechanism, seniors first by construction | emergent from sweep order |
| (none) | `ChokePermitGovernor` + `choke_limits` ctor arg | §4b–4c |
| `fire()` runs immediately | `fire()` priority-acquires chokes, may await | §4e |
| `_complete_step()` reclassify + reload | same, plus permit release + requeue-on-wake check | §3, §5 |
| tier policy, heartbeat, rehydrate/evict, halt, events | inherited unchanged | §8 |

---

## 12. Related Architecture Improvements (driver family — not specific to this class)

> Captured from a design discussion adjacent to this spec: how should a pool
> driver defend against a **badly behaved case**? These apply to
> `TieredCasePoolDriver` and this driver equally; none block this spec.

### 12a. The threat model, and what is already handled

A case can misbehave four ways. The first two are already well-defended:

| Misbehavior | Status | Mechanism |
|---|---|---|
| Raises exceptions | **Handled** | `AdvanceResult.failed`, WARM backoff ladder, `@FAIL` divert edges, (future) manager quarantine policy |
| Hangs at an `await` | **Handled** | hard kill ceiling — `asyncio.wait_for(work, kill)` in the machine factory aborts with `TriggerTimeout` (journaled as `CASE_TRIGGER_TIMEOUT`, `@FAIL`-counted); `_LeaseKeepalive` pulse keeps the lease warm for legitimately slow steps |
| **Blocks the event loop** (sync CPU / blocking I/O in a hook that never yields) | **Discipline only** | hook contract demands well-behaved async; `case_run_blocking()` is the sanctioned offload. Nothing *enforces* it — a loop-blocker starves the driver beat, the heartbeat slice, AND the keepalive pulse itself, so leases can lapse fleet-wide, silently |
| Truly pathological (infinite loop, hang inside a C extension, memory exhaustion, segfault) | **Unhandled in-process** | nothing in-process can handle it |

The last two rows are what any isolation proposal is really about.

### 12b. Decision: thread as MONITOR, not thread as WORKER

**Rejected: running case work on separate worker thread(s).** Two disqualifiers:

1. **Threads cannot be killed in Python**, so the "recover the wedged case"
   promise is illusory — the only move is to abandon the thread, and an
   abandoned-but-alive thread still holds the live case object and may wake
   later and write to the folder while a rehydrated successor owns it. The
   lease protocol only surfaces `OwnershipLostError` to a zombie *when it
   yields*, which a wedged thread by definition does not. Worker threads
   manufacture exactly the split-brain risk the lease system exists to
   prevent. (`case_run_blocking()`'s docstring already documents this in
   miniature: a hard-abort "frees the case but leaks the thread.")
2. **The driver's simplicity is loop-confinement.** All driver state
   (`_by_folder`, counters, indexes, event emission, `settle()`) is lock-free
   *because* it mutates on one loop. Threaded work means cross-thread
   marshaling or locks throughout — a heavy tax. And under the GIL (target
   ≥3.11), worker threads add no CPU parallelism for pure-Python hooks anyway.

**Accepted: a watchdog thread** — the one place a thread genuinely earns its
keep, because a stalled loop cannot report on itself:

- Driver updates a `last_beat_completed_at` timestamp each beat.
- A tiny daemon thread checks it on an interval; when the loop goes silent
  past a threshold, it logs CRITICAL naming the in-flight slots (`slot.task`,
  `last_advanced_at`) and dumps `faulthandler.dump_traceback()` so the
  offending hook is identified at its exact line.
- Detection, not recovery — but it converts today's failure mode (silent
  fleet-wide lease lapse, mystery) into an actionable alarm naming the
  offender. ~50 lines, zero change to the concurrency model.

A no-thread sibling worth having regardless: **beat-duration alerting** — time
each sweep; a beat that took ≫ `I0` means some hook briefly abused the loop
and recovered. Cheap early warning for intermittent offenders the watchdog's
threshold never trips on.

### 12c. The real isolation boundary is the PROCESS (future, already designed)

For the truly pathological row, the boundary that actually works is a
**subprocess**: killable, memory-isolated — and its supervision protocol
already exists in this family. A worker process's lease lapses when it dies
(gracefully or not), and the existing machinery (lease reclaim,
`restore_pool_from_journal`, rehydrate, `EVICTED`) recovers its cases with no
new invention. This is the same shape as the multi-driver sharding story in
CaseManager Model §6a ("moving a case is an ownership handoff, not data
movement") — a badly-behaved-case bulkhead is just supervised sharding with a
kill switch. Folder-backed state makes it cheap to contemplate: everything
durable is already on disk.

### 12d. Multi-threading for throughput: YAGNI, with one cheap hedge

Both futures that would demand worker threads are better served by tools the
family already has: I/O concurrency by asyncio (that is what the in-flight
ceiling is), CPU parallelism by process pools (a `ProcessPoolExecutor` cousin
of `case_run_blocking()`) or by driver sharding. Free-threaded Python does not
change the killability problem, which is the heart of the recovery
requirement. So: no anticipation of worker threads in any driver design.

**The one hedge to take now** (costs a paragraph, buys every future door): make
the currently implicit rule explicit in the `CasePoolDriver` ABC docstring —
**all driver state is loop-confined; anything that ever runs work off-loop
must marshal results back onto the driver's loop**
(`call_soon_threadsafe` / `run_coroutine_threadsafe`). That keeps watchdog
threads, process workers, and even hypothetical work threads all possible
without paying for any of them today.

### 12e. Suggested sequencing

1. **Now / cheap:** beat-duration alerting + the watchdog thread (§12b).
2. **Now / free:** the loop-confinement sentence in the `CasePoolDriver` ABC
   docstring (§12d).
3. **Later / when evidence demands:** process-level bulkheads, designed as a
   variation of the sharding/ownership-handoff story (§12c).
4. **Never (absent contradicting evidence):** worker threads inside a driver.
