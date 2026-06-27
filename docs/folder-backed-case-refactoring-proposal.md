# Refactoring Proposal: Decomposing `FolderBackedCase`

**Audience:** Senior architect
**Scope:** `src/totodev_pub/folder_backed_case.py` (the `FolderBackedCase` ABC, ~1,166 lines)
**Status:** Proposal for review. No code written yet. We are pre-live, so backward compatibility is *not* a constraint — we are free to change public call sites.

---

## 1. Motivation

`FolderBackedCase` has accreted into a single class that carries roughly a dozen distinct
responsibilities: FSM compilation, a global type registry, folder inception, instance-time
machine binding, the heartbeat lease, event-log domain reads/writes, the two-phase close
choreography, exception handling, the flat pipeline driver, and reclassification. The class
works, but its size makes it hard to onboard to and hard to reason about in pieces.

The goal of this refactor is **not** to change behavior. It is to break the class into a small
number of **tightly-coupled helper objects**, each owning one coherent idea, so that:

- a reader can understand one concept at a time (the lease, the journal, the registry) without
  holding the whole class in their head;
- state that exists *only* to serve one concern (e.g. the four lease-bookkeeping attributes)
  lives with that concern rather than as loose instance fields;
- the scattered, low-level calls into supporting objects (notably
  `self._events.primitive.create_event(...)`, which appears ~10 times) are funneled through
  one domain-aware surface.

A secondary, enabling goal: establish a sanctioned **change-notification channel** so that once
responsibilities are split, a delegate that mutates the case folder (e.g. appends a state-change
event) can tell the core object "disk moved, re-derive your cached state." Today the core object
is the only writer, so it updates its caches eagerly; the moment we delegate writes, that
invariant needs an explicit seam.

---

## 2. Design principles

1. **Tightly-coupled, not generic.** These are not reusable abstractions for the world; they are
   purpose-built collaborators for `FolderBackedCase`. A back-reference to the owning case (or a
   small callback) is acceptable and expected.
2. **No behavior change.** Every extraction is a move + rewire. The event-log format, the lease
   semantics, the close sequence, and the public lifecycle all stay identical.
3. **Preserve subclass override seams.** Several methods (`lease_ttl_for`, `generate_case_id`,
   `on_closing`, `on_transition_exception`, `compile_fsm`, `trigger_warn_secs`,
   `archive_grouping_label`) are documented extension points. Any extraction must keep these
   overridable on the case subclass, even when the *mechanism* moves into a helper.
4. **Sequence by coupling.** Do the zero-coupling extractions first to build confidence and shrink
   the class, then tackle the more entangled ones using the seams established earlier.

---

## 3. Proposed enhancements (in implementation priority order)

### 3.1 `CaseTypeRegistry` — the type catalog and folder-peek surface

**Priority: 1 (do first).** This is the cleanest extraction in the file because almost none of it
touches instance state — it is class/static logic over a global dict and over folders on disk.

**What it owns**

- The global registry dict (`_case_class_registry`) and its mutators/lookups.
- Type sniffing and resolution.
- Opening a folder as the correct subclass (`rehydrate`), and the read-only "peek" family that
  reads a folder *without* constructing a live, lease-holding instance.

**Methods removed from `FolderBackedCase` (moved into `CaseTypeRegistry`)**

| Current method | Disposition |
|---|---|
| `register_case_types` | move |
| `register` (decorator sugar) | move |
| `resolve_case_type` | move |
| `_sniff_case_type` | move |
| `rehydrate` | move |
| `peek_record` | move |
| `peek_events` | move |
| `peek_assets` | move |
| `_case_class_registry` (class attr) | move |

**Deliberately *not* moved:** `generate_case_id` + `_last_generated_case_id_ms`. ID minting is a
documented per-subclass override seam and is conceptually distinct from type resolution; it stays
on the case.

**Caller-facing?** **Yes — this is a public surface.** External callers currently do
`FolderBackedCase.register_case_types(...)`, `FolderBackedCase.rehydrate(...)`,
`FolderBackedCase.peek_record(...)`, etc. Because we are pre-live, the cleanest end state is a
module-level singleton registry that callers use directly (e.g. `registry.rehydrate(folder)`). If
we want to soften the transition we can leave thin classmethod facades on `FolderBackedCase` that
delegate to the singleton, but the intent is to make the registry the home of these calls.

**Why first:** ~120 lines leave the class with essentially zero coupling to instance state, so the
risk is minimal and the win is immediate.

---

### 3.2 `HeartbeatLease` — single-owner lease and heartbeat

**Priority: 2.** The second-cleanest boundary. The lease is a self-contained concept with its own
fiddly mechanics (a content-free `.case.lease` file whose mtime is a "valid-until" token, a
throttle clock, ownership validation) **and its own state** — four instance attributes that exist
for nothing else.

**State removed from `FolderBackedCase` (moved into `HeartbeatLease`)**

- `_holds_lease`, `_detached`, `_my_mtime`, `_last_beat_local`

**Methods removed/replaced**

| Current method | Disposition |
|---|---|
| `_lease_path` | move |
| `_on_disk_mtime` | move |
| `_beat` | move |
| `_acquire_lease` | move |
| `heartbeat` | move (case keeps a thin pass-through; see below) |
| `detach` | move (case keeps a thin pass-through) |
| `_check_active` | replace — case delegates to `lease.is_active()` |
| `is_heartbeat_expired` (static) | move |
| `lease_ttl_for` | **stays on the case** (override seam); lease asks the case for it |

**Coupling and how we resolve it**

- The lease needs the **folder path** (supplied at construction) and the **current state** (the
  TTL is state-aware). Because `lease_ttl_for(state)` is a subclass override point, it stays on the
  case; the lease holds a back-reference (or a `ttl_provider` callback) and calls back for the TTL.
- "Detached?" is partly a case-level question because `_check_active` gates `advance()` and every
  mutating operation. Resolution: the case stops keeping its own `_detached` flag and asks
  `lease.is_active()`.
- `__enter__` / `__exit__` / `__del__` stay on the case (they are the object's lifecycle protocol)
  but their bodies reduce to `self._lease.release()`.

**Caller-facing?** **Mixed.** `heartbeat()` and `detach()` are part of the case's public lifecycle
and should remain callable on the case (as thin delegations). `is_heartbeat_expired(folder)` is
used by a manager's recovery sweep and can move to the lease class as a static/`classmethod`
utility; that call site updates accordingly.

**Why second:** ~90 lines plus four attributes leave the class, and the only coupling is one
well-defined callback (the TTL lookup).

---

### 3.3 `CaseJournal` — domain-aware event-log read/write facade

**Priority: 3.** This is the highest-leverage extraction that did *not* appear on the original
idea list, and it is the **keystone** that de-risks everything after it.

**The problem it solves**

Two things are smeared across the class today:

- **Writes:** ~10 direct `self._events.primitive.create_event(LABEL, value, {...})` calls, each
  re-encoding a piece of domain knowledge (what label, what value, what payload) inline.
- **Domain reads:** `_fail_count` (a "fail" = `FAIL_TRANSITION` *and* `TRIGGER_TIMEOUT` since the
  last enter), `_has_event_since_enter`, `_derive_state`, and the dwell anchor — all
  case-specific *interpretations* of the generic log.

`CaseEventLogReader` already exists as the generic read side; `CaseJournal` wraps it and becomes
the one place that knows how *this case family* reads and writes its log.

**Base-event naming invariant (new in this refactor point)**

- Add a public constant in support constants (e.g. `CASE_BASE_EVENT_PREFIX = "CASE_"`).
- Treat this as a class-family invariant: every event label auto-generated by the
  `FolderBackedCase` base flow (now funneled through `CaseJournal`) MUST start with that prefix.
- Keep all base labels in centralized `EV_*` constants, and make journal writes go through a
  single internal helper (e.g. `_append_base_event(...)`) that fail-fast validates the prefix.
- Subclasses remain free to emit custom labels, but by convention they SHOULD avoid the reserved
  base prefix so observers can separate base lifecycle events from domain-specific custom events
  with a simple prefix filter.

**Impact on existing `CaseEventLogReader`**

- No architectural rewrite needed: it already models generic case conventions and should remain the
  read-oriented dependency that `CaseJournal` builds on.
- Recommended small adjustment only: consume the shared prefix constant in reader-side helper logic
  (if/when we add helpers like `is_base_event_label`), so read and write surfaces agree on the same
  reserved-prefix definition.
- Keep the reader decoupled from write policy enforcement; prefix validation belongs on the write
  side (`CaseJournal`), not in passive reads.

**Methods removed/absorbed from `FolderBackedCase`**

| Current method / call pattern | Disposition |
|---|---|
| `_derive_state` | move (→ `journal.current_state`) |
| `_fail_count` | move (→ `journal.count_fails_this_dwell`) |
| `_has_event_since_enter` | move |
| `_log_trigger_slow` | move (→ `journal.log_slow`) |
| inline `create_event(EV_ENTER_STATE, ...)` (×2) | replace (→ `journal.log_enter_state`) |
| inline `create_event(EV_CLOSED, ...)` | replace (→ `journal.log_closed`) |
| inline `create_event(EV_NEW, ...)` | replace (→ `journal.log_new`) |
| inline `create_event(EV_FAIL_TRANSITION / EV_ENTRY_EXCEPTION / EV_TRIGGER_TIMEOUT, ...)` | replace |
| inline `create_event(EV_RECLASSIFY, ...)` | replace |
| `log_alert` (`EV_ALERT`) | likely move (→ `journal.log_alert`); case keeps a public pass-through |

**Caller-facing?** **Mostly a pure helper.** The journal is an internal collaborator. The one
exception is `log_alert`, which is a documented public method ("this case needs a human"); the case
should retain a public `log_alert(...)` that delegates to the journal.

**This is where the change-notification channel lives.** Because every append now funnels through
the journal, the journal is the natural emitter of "the log just changed." Two viable shapes:

1. a monotonic `revision` counter the case checks lazily before trusting its cached `state` /
   `_last_activity` / `_state_entered_at`; or
2. an `on_appended(case)` callback the case registers — this mirrors the existing
   `add_transition_listener` / `_notify` pattern already in the class, so it would feel idiomatic.

Establishing this seam here is what makes it safe for *later* delegates to write to the log without
silently desynchronizing the core object's caches. (Today, `_on_fsm_exception` contains an explicit
"no-drift remedy" that re-writes a missed `CASE_ENTER_STATE`; a real notification channel is the
principled version of that patch.)

**Why third:** it has more coupling than the registry or lease (it is consulted from many call
sites), but it pays for itself by removing scattered low-level noise and by providing the
invalidation seam the remaining work depends on.

---

### 3.4 `CaseMachineFactory` — instance-time FSM binding

**Priority: 4 (do after the journal).** Roughly 150 lines are dedicated to turning the per-class
compiled FSM spec (`_fsm`) into a live, instance-bound `AsyncMachine`, complete with factual
guards and timed `before` wrappers. This is a cohesive concern — *machine construction and callback
wiring* — that is distinct from the case's runtime domain behavior.

**Methods removed from `FolderBackedCase` (moved into `CaseMachineFactory`)**

| Current method | Disposition |
|---|---|
| `_build_machine` | move |
| `_prepare_machine_transitions` | move |
| `_make_perform_wrapper` | move |
| `_make_fact_guard` | move |
| `run_blocking` | move (or co-locate; it is the perform-time blocking escape hatch) |
| `trigger_warn_secs` | **stays on the case** (override seam); factory reads it |
| `dwell_secs` | **stays on the case** as a public fact, but its *value* comes from the journal |

**Coupling and why it follows the journal:** the guard and wrapper closures need per-instance facts
— `dwell_secs()` and the fail count — which become journal-derived after 3.3. So the factory takes
the case (for its overridable `trigger_warn_secs` and `perform_<trigger>` methods) and the journal
(for the facts). Doing this *after* the journal means the factory consumes a clean facts surface
instead of reaching back into ad-hoc instance methods.

**Caller-facing?** **Pure helper.** No external caller constructs a machine directly; this is
invoked once from the case constructor.

---

## 4. Considered and rejected

### `CloseChoreographer` (transition / close / exception choreography) — **rejected**

We evaluated extracting the ~140 lines of `_on_state_changed` (the two-phase close),
`_on_fsm_exception` (the no-drift remedy, exception decoration, countable-failure logging), and
the surrounding hooks into a dedicated `CloseChoreographer`. The concept is cohesive on paper.

**Why we decided against it:**

- The two methods are wired into `AsyncMachine` *by name* as `after_state_change` and
  `on_exception`. Moving them means either re-pointing those callbacks into a helper that holds a
  back-reference, or leaving thin shims on the case — either way the "clean extraction" story is
  diluted.
- The genuinely complex pieces that callers/subclasses care about — `on_closing` and
  `on_transition_exception` — are **override seams** and must remain on the case as part of its
  public contract. So at best we could move the *machinery* while the *extension points* stay
  behind, splitting one idea across two classes.
- The net result is more entanglement (a helper that calls back into case hooks mid-dispatch) for
  a modest reduction in line count, and it would partially duplicate the change-notification
  responsibility we are already giving to `CaseJournal` (3.3).

Net: the cost/benefit is unfavorable relative to the other extractions, so the close/exception
choreography **stays on `FolderBackedCase`** for now. The `CaseJournal` notification seam already
addresses the underlying "no-drift" concern that motivated looking at this area.

---

## 5. How the pieces relate

```
FolderBackedCase (slimmed core: identity, FSM compile, construction,
                  reclassify, record flush/fetch, close/exception
                  choreography, public lifecycle + override hooks)
   |
   |-- uses --> CaseTypeRegistry   (3.1)  global type catalog + folder peek/rehydrate  [public]
   |-- owns --> HeartbeatLease     (3.2)  single-owner lease; calls back for lease_ttl_for  [mixed]
   |-- owns --> CaseJournal        (3.3)  domain log read/write; emits change notifications [helper]
   |-- uses --> CaseMachineFactory (3.4)  builds the bound AsyncMachine from _fsm + journal facts [helper]
```

Key relationships:

- **Registry ↔ core:** the registry resolves a type and constructs the case; the case no longer
  owns resolution. Independent of the other three.
- **Journal ↔ lease:** independent, but both are constructed early in `__init__`. The journal's
  change-notification channel is what later lets *any* delegate signal cache invalidation.
- **Factory → journal:** the factory's guards/wrappers source their facts (`dwell_secs`,
  fail count) from the journal, which is why the journal must land first.
- **Override seams stay on the case** throughout (`lease_ttl_for`, `trigger_warn_secs`,
  `generate_case_id`, `on_closing`, `on_transition_exception`, `compile_fsm`,
  `archive_grouping_label`) so the subclass authoring experience is unchanged.

---

## 6. Explicitly out of scope (decisions already made)

- **No `CaseDriver`.** We considered extracting `advance` / `run_to_completion` /
  `_forward_candidates` / `_make_blocked` into a driver. The "protocols around advance()"
  (exception-return, timeout) are not co-located with `advance` — they live in the machine
  callbacks and the `perform_` wrapper — so a driver extraction would be too tightly coupled to
  realize the intended cohesion. The driver loop **stays on the case.**
- **No extraction of the convenience callthroughs** (`case_id`, `external_key`, `nickname`,
  `assets`, etc.). They are intentionally retained: beyond ergonomics, they carry documentation
  value, presenting a curated, self-describing surface for the case.

---

## 7. Suggested rollout

1. **`CaseTypeRegistry`** — zero-coupling, ~120 lines out. Update call sites to the singleton.
2. **`HeartbeatLease`** — ~90 lines + 4 attrs out; one TTL callback.
3. **`CaseJournal`** — centralizes log I/O *and* establishes the change-notification channel.
4. **`CaseMachineFactory`** — now clean, because it consumes journal-provided facts.

Each step is an independent, behavior-preserving move that leaves the test suite green before the
next begins. After all four, `FolderBackedCase` retains its identity: FSM compilation, construction,
the close/exception choreography, reclassification, record flush/fetch, the driver loop, and the
public lifecycle + override hooks — but each delegated concern reads as a self-contained chapter.
