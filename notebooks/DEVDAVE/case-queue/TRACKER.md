# CaseManager / CasePoolDriver — branch closeout tracker

Tracks remaining work to close out `explore/case-queue` (CaseManager, CasePoolDriver family,
PoolMembershipJournal, and their support packages) and merge into `main`.

**This is a living document.** Check items off, add new ones, and re-triage as work happens or as
new quirks surface — it is explicitly not expected to be complete on day one. Full background
research behind the initial pass lives in
`volatile/tmp/case_manager_pool_driver_assessment.md` (not committed — local scratch); this tracker
is the durable, evolving artifact.

Status tags used below: `OPEN`, `IN PROGRESS`, `DECISION NEEDED` (blocked on a call only a
maintainer can make), `DEFERRED` (deliberately not now).

---

## 0. Key classes to finalize

The classes this branch needs to close out on, grouped by layer. "Finalize" here means: settled
public API, cleared of the quirks in §5, adequately tested (§2), and consistent with the charter and
layering in §4.0. The storage question that previously blocked two of these is settled in shape: no
pluggable storage *behavior*, but a `CaseStore` cohesion boundary that `CaseManager` is constructed
with (§4.1.1, §4.1.2). The protocol-extraction question in §4.0.2 is still open and does change what
several of them are allowed to assume.

**Scheduling layer** (`folder_backed_case_support/`):

- `CasePoolDriver` (ABC, `case_pool_driver.py`) — the scheduling contract every driver must honor.
  Stable already; mainly needs its `§N` design-doc references resolved (§3). The tier-transition
  event once contemplated here is `DEFERRED` (§4.1.5 S-3), so no contract change is pending.
- `BalancedCasePoolDriver` (`balanced_case_pool_driver.py`) — the default concrete driver (MLFQ
  tiering, choke governance). Quirk list in §5; no longer implicated by the tier-transition question
  (§4.1.5 S-3).
- `SeniorityCasePoolDriver` (`seniority_case_pool_driver.py`) — FIFO/seniority variant. Has the most
  outstanding ergonomic debt of the three (manual `_Slot` field duplication, reach into base-module
  private names — §5) and its shutdown-drain scope boundary is undocumented.
- `PoolMembershipJournal` (`pool_membership_journal.py`) — crash-recovery observer. Smallest quirk
  list (one dead field, §5); mostly just needs the `deadline_margin_secs` question resolved.

**Fleet management layer** (`case_manager.py`, `case_manager_client.py`, `case_manager_support/*`):

- `CaseManager` (`case_manager.py`) — **the central class.** Largest quirk list in §5. Its
  adopt/eject/reclassify/reopen methods are the ones that hardcode `CachedFileFolders` move
  semantics, so they are the main site for the §4.1.2 `CaseStore` extraction and the §4.1.5 S-4 lease
  precondition. Its constructor is slated to take a `CaseStore` and drop `CachedFileFolders`
  entirely. Also the class that shrinks again if §4.0.2's protocol extraction goes forward.
- `CaseStore` (**does not exist yet** — §4.1.2) — the storage cohesion boundary: durable location +
  execution status, add/set-status, iterate all / by status, `case_id` ↔ path. Concrete class now,
  abstract interface deliberately deferred.
- `CaseManagerClient` (`case_manager_client.py`) — out-of-process façade. Smaller surface; main open
  item is the `CaseLocation.in_pool`-unreliable-from-client issue (§5).
- `MailboxProcessor` (`case_manager_support/mailbox/processor.py`) — fire/adopt/reclassify/shutdown
  request-response protocol. Needs the result-type-sniffing fragility addressed (§5) and per-request
  isolation audited (§4.2).
- `ManagerWatchdog` (`case_manager_support/watchdog.py`) — wedge detection + kill ladder. Functionally
  complete; blocked on the promotion-blocker test gaps in §1 (the exit-70 integration test above all).
- `CaseManagerPolicy` (`case_manager_support/case_manager_policy.py`) — the single source of truth for
  layout/addressing. Its Tier-1 layout fields are candidates to move into `CaseStore` construction
  (§4.1.2); it may also shed its mailbox/transport fields if §4.0.2's protocol extraction goes
  forward.
- `CaseManagerConfig` (`case_manager_support/case_manager_config.py`) — process-local wiring
  (driver/registry/cache overrides). No dedicated test yet (§2); its `cache_override` field goes away
  when the constructor takes a `CaseStore` (§4.1.2).
- `FleetStatusBoardWriter` / `FleetStatusBoardWatcher` (`fleet_status.py` / `fleet_status_watcher.py`)
  — fleet snapshot writer + client-side watcher. No open quirks beyond the private-symbol imports
  noted in the original mapping; lowest-risk pair in this list.
- `EscalationRegistry` (`escalation.py`) — in-process pub/sub for escalations. No open items beyond
  what's already covered by its existing test file.

Everything else in `case_manager_support/` (`adopt.py`, `termination.py`, `aberrant.py`, `eject.py`,
`reap.py`, `purge.py`, `recover.py`, `shutdown.py`, `staging.py`, `layout.py`,
`case_manager_host.serve()`) is function-based pipeline code rather than a class with its own
lifecycle — tracked by module in §2/§3/§5 rather than listed here, but `layout.py` and the four
move-shaped pipelines (`adopt`/`termination`/`aberrant`/`eject`) are the ones the §4.1.2 `CaseStore`
extraction and the §4.1.5 S-4 lease precondition reshape, so treat them as riding on those.

---

## 1. Promotion blockers (from `_backlog/finishing_watchdog.md`)

That doc's own header says these should be resolved before promoting to `main`. Carried forward
here so they're tracked in one place instead of two.

**All five are closed.** This section is the merge gate for `main`; clearing it makes the branch
mergeable, which is separate from deciding to merge. See §7 Wave 1.

- [x] Integration test drives 3 consecutive `_manager_loop` tick failures through a real `serve()`
      run and asserts exit 70, in both watchdog modes
      (`tests/test_case_manager_host_serve.py`).
- [x] The shutdown mailbox is polled on every maintenance tick, above the `enable_mailbox` gate, so
      the mailbox-off + watchdog-off combination is no longer dead. §4.0.2's move of shutdown to the
      host supersedes this in Wave 3.
- [x] `_loop_failure_no_watchdog` implemented: with the watchdog disabled, three consecutive loop
      failures escalate, dump tracebacks, write a death record, and exit `EXIT_WATCHDOG`.
- [x] Watchdog coverage for `mailbox_neglect`, the `tick_slow` alarm-only path, and stop-timeout →
      `EXIT_WATCHDOG` (`tests/test_manager_watchdog.py`, `tests/test_case_manager_host_serve.py`).
- [x] Operator deployment doc written: `docs/case-manager-deployment.md` — exit-code contract,
      restart policy, grace-period nesting, watchdog thresholds, health probe, container caveats.
      Needs a revision after Wave 3 moves shutdown into the host.

## 2. Test coverage gaps (found independently, not in finishing_watchdog.md)

- [x] `aberrant.py` covered by `tests/test_case_manager_aberrant.py` (both branches of
      `move_case_to_aberrant`). The orphan-rescue branch was non-functional (F1) and is fixed.
- [x] `purge.py` covered by `tests/test_case_manager_purge.py`, including the mid-purge error path
      and the tick isolation around it.
- [ ] `DECIDED 2026-08-01` — **Drop `PurgeReport.stragglers`.** Declared in `purge.py`, never
      populated, never read. Same disposition and same reasoning as F6.
- [ ] `OPEN` — `layout.py` has no direct unit test despite being the highest-fan-in module in the
      support package (9+ importers). Only exercised transitively through everything else.
- [ ] `OPEN` — `case_manager_config.py` (`CaseManagerConfig`) has no test that references it by name;
      only indirect coverage via `test_case_manager_provision_attach_open.py`.

## 3. Documentation / wiring staleness

- [ ] `OPEN` — The `case-designer` skill (`.claude/skills/case-designer/`) has essentially no
      knowledge that this pool-manager layer exists. It mentions "CaseManager" casually 4 times
      (never linked to a file, per its own reference-boundary rule) and never mentions
      `CaseManagerClient`, `CasePoolDriver`/`Balanced`/`Seniority`, `PoolMembershipJournal`,
      `ManagerWatchdog`, or the `manager_health` CLI at all. Someone scaffolding a new case type via
      this skill gets no guidance on choke interaction with fleet scheduling, or how the result
      actually gets run. (See also §6.3 below — this overlaps with the "low-effort manager script"
      gap.)
- [ ] `OPEN` — `folder_backed_case_support/__init__.py`'s `__all__` doesn't export `CasePoolDriver`,
      `BalancedCasePoolDriver`, `SeniorityCasePoolDriver`, or `PoolMembershipJournal`, despite living
      in that exact package, whose own docstring says "import the common names from here."
- [ ] `OPEN` — Several comments still say **"Planned CaseManager (draft:
      notebooks/DEVDAVE/case_manager_classes/CaseManager Model.md)"** — written before `CaseManager`
      existed, never updated once it shipped, and the referenced file doesn't exist at that path.
      Locations: `folder_backed_case.py` (x2), `folder_backed_case_support/case_type_registry.py`,
      `folder_backed_case_support/case_pool_driver.py`.
- [ ] `OPEN` — Tutorials 2 & 3 (`notebooks/DEVDAVE/case_manager_classes/`) cite the mailbox-removal
      backlog doc at a stale path (`src/totodev_pub/_backlog/...`, which doesn't exist — real path is
      `notebooks/DEVDAVE/case_manager_classes/_backlog/...`).
- [ ] `OPEN` — `proposed_manager_watchdog_and_host.md`'s "implemented" note points at
      `docs/superpowers/plans/2026-07-09-manager-watchdog-and-host.md`, which doesn't exist anywhere
      in the repo — dangling link.
- [ ] `OPEN` — Same proposal's "Code placement" section says `case_manager_host.py` is top-level
      (`src/totodev_pub/`); it actually shipped inside `case_manager_support/`. Its own
      `finishing_watchdog.md` "what shipped" table repeats the wrong path too.
- [ ] `OPEN` — Two exceptions (`CacheRootStateError`, `PolicyMismatchError`) are defined in
      `case_manager_support/exceptions.py` but not re-exported in that package's `__init__.py`,
      unlike every other exception there. Confirm intentional or fix.
- [ ] `OPEN` — Stray `fleet_watcher.cpython-311.pyc` with no corresponding source file (module was
      renamed to `fleet_status_watcher.py`; old bytecode never cleaned up). Harmless, quick sweep.
- [ ] `OPEN` — `_backlog/README-_backlog.md` convention (move resolved proposals to an
      `implemented/`/`withdrawn/` subfolder) has never actually been followed — no such subfolders
      exist, including for the proposal marked "implemented." Either follow the convention now or
      update the README to describe how status is actually tracked (i.e. this tracker).

## 4. Bigger design questions raised in discussion (not yet scoped — need design work, not just fixes)

These came directly from conversation with the maintainer and are **not** small polish items — each
probably deserves its own short design note before implementation.

### 4.0 System charter and layering — `DECIDED IN PRINCIPLE 2026-08-01`, details open

**Methodological correction from the architect (2026-08-01):** §4.1's material was built bottom-up, by
inferring desired system behavior from a series of small decisions about existing code. That is
backwards. This section is the top-down statement the §4.1.x items should be subordinate to. **Where
the two conflict, this section wins.**

#### 4.0.1 What a `CaseManager` is — draft charter

> A `CaseManager` drives a set of folder-backed cases toward completion, on a schedule, within one
> process. It decides **which** cases are eligible to be driven and **when**. It does not decide what
> a case does, how requests reach it, or whether the process lives or dies.

| In scope | Out of scope (and where it goes) |
|---|---|
| Admission / eligibility — which cases are under management | What a case *does* → `FolderBackedCase` |
| Scheduling — delegated to a `CasePoolDriver` | How requests *arrive* → signaling adapter (§4.0.2) |
| Pool-activity-status — the manager's view of each case (§4.1.5 S-1) | Whether the *process* lives or dies → host |
| Reconciliation — converging its view with what is on disk (`reap`) | Where bytes physically *live* → the managed filespace layout (§4.0.3) |
| Escalation — telling the owning application something is wrong | |

#### 4.0.2 Three layers, not one — `DECIDED 2026-08-01` — go, as Wave 3

The architect proposes extracting the file-based protocols from `CaseManager` into an object owned by
the pipeline process (working name `CaseServerSignalingProtocol`), leaving `CaseManager` exposing
conventional sync/async methods that the protocol object invokes. Adopting that, plus the host that
already exists, gives three layers:

1. **Host** (`case_manager_host.serve()`) — process ownership, signals, exit codes, watchdog, **and
   the shutdown protocol**.
2. **Signaling adapter** (new) — translates a file-drop transport into `CaseManager` method calls:
   fire / adopt / reclassify, correlation IDs, result publication, intake dead-lettering, replay.
3. **`CaseManager`** — the charter above. No transport, no process lifecycle.

**Recommended refinement to the architect's proposal: split shutdown away from the request
protocols.** Shutdown is how the *process* is told to die, which is already the host's job (it owns
exit codes and signal wiring). Fire/adopt/reclassify are *request* transport. Putting shutdown in
layer 1 and requests in layer 2 has a concrete payoff: it structurally dissolves §1's open
promotion blocker — "with `enable_mailbox=False` **and** `watchdog_enabled=False` the shutdown
mailbox is completely dead." That bug exists precisely *because* both are flags on one object. Under
the split the host always polls shutdown, regardless of whether a signaling adapter exists. §1 takes
option A as an interim fix at the Wave 1 merge; this supersedes it.

**What the extraction buys (beyond the architect's own rationale):**

- Inverts a circular dependency. `MailboxProcessor(self)` (`case_manager.py:136`) is handed the whole
  manager and reaches into its internals. Inverted, the adapter holds a manager and calls its
  **public** API — which forces that public API to actually be complete, instead of letting the
  mailbox cheat via private access.
- Moves §5's result-type-sniffing fragility and §4.2's "does one malformed request poison the batch"
  into an object testable **without a running manager**.
- Substantially serves §4.3 (no low-effort path to a running manager) and §4.4 (bench-testing
  harness): a manager with no adapter attached is driven entirely through its own public methods.
- Lets `adopt_case()` shed `correlation_id` entirely. Today the manager carries transport-level
  idempotency (`_load_adopt_result` / `_publish_adopt_result`, keyed by correlation ID) — that is
  adapter business, not fleet business.

**Wrinkles the extraction must answer — these are real, not objections:**

- **Recovery becomes a two-party sequence.** `recover_manager()` today calls
  `replay_fire_on_recover()` / `replay_adopt_on_recover()` / `replay_reclassify_on_recover()`
  (`recover.py:82-85`). If the protocol leaves, the adapter needs its own `recover()` and the *host*
  must sequence the two. `RecoverReport`'s three mailbox fields move out with it — a public-surface
  change.
- **`is_idle` breaks.** It currently inspects mailbox intake dirs (`case_manager.py:286-292`) to
  answer §8 self-completion. Once the mailbox is external, `CaseManager` can no longer answer "is the
  whole system idle" alone; it becomes a composite the host computes from manager + adapter.
- **The watchdog's health model shifts.** `mailbox_neglect` detection assumes the manager owns the
  mailbox. §1 already flags that path as untested — decide its new owner before writing that test.
- Once the adapter is an adapter, other transports (HTTP, queue, in-process) become possible without
  touching the manager. Worth *noting* as a property; explicitly **not** worth building now.

#### 4.0.3 Single operating model: the manager owns its filespace — `DECIDED 2026-08-01`

A `CaseManager` always provisions and owns a managed filespace. Cases are adopted into it and are
relocated within it according to execution status. **There is no in-place / foreign-filespace variant
and no storage-ownership switch** — one operating model, one set of guarantees.

**Why one model.** Location-as-status is *free crash-consistency*: a folder sitting in
`terminal_2026-08/` is terminal, with no write-ahead log, no fsync discipline, and no torn-write
window, because the filesystem already supplies atomic rename. Restartability falls out of that —
`compact_from_live(live_paths)` rebuilds the pool by looking at where things are. An in-place variant
would have to reconstruct that same guarantee with a *second* durable-status mechanism, carrying its
own crash-consistency story, its own startup reconciliation, and its own answers to "status says live,
record says terminal" and "status missing entirely." That is a second implementation of the hard part,
in a system that already maintains three sources of truth (§4.1.3). **One mechanism for
restartability** is worth more than the convenience an in-place mode would buy.

**And it would buy little.** `adopt` *moves* rather than copies — `_transfer_into_slave` does
`shutil.move` per child then `source.rmdir()` (`adopt.py:126-137`) — and running a case mutates it
(record, events, logs, lease). So any "run this bag of cases" workflow has to copy the bag first
regardless of model; running in place would destroy the fixture and make the run unrepeatable.
In-place would therefore save only the adopt loop, at the cost of a parallel status mechanism.

**The residual need is construction ceremony, not storage architecture.** Getting from "a bag of case
folders" to "a running manager" should be one call plus a test fixture — tracked in §4.3, where it
belongs, rather than solved with a second storage model.

**Applications that genuinely need cases driven in place** — for example a very large batch of small
cases whose owning process has its own retention rules and discards completed records immediately —
are served by using a `CasePoolDriver` directly, without taking on `CaseManager`'s plumbing and
opinions. That is an existing, supported path and needs nothing new.

**Consequence:** there is no pluggable storage *behavior*. A cohesion boundary over storage is a
separate and accepted proposition — see §4.1.1 and §4.1.2.

#### 4.0.4 `aberrant` reconceived: a scheduling verdict, not a storage move — `DECIDED IN PRINCIPLE`

Architect's original conception, recovered 2026-08-01: aberrant is an **exception-handling strategy**.
Consider a case that keeps its lease alive yet raises on every attempted interaction. The manager's
options are all ugly; the least-bad is to **stop interacting and leave it in place**, hoping the lease
expires — after which it may be relocated or deleted while still honoring the heartbeat-lease policy.

That decomposes into three separable things, and today's code fuses them:

| Concern | When |
|---|---|
| Stop driving this case | **Immediately** |
| Record *why* | Immediately — as a custom event on the case, not in the store (§4.1.2) |
| Relocate / delete it | **Only once the lease has expired** — the store refuses until then (§4.1.2) |

Today `move_case_to_aberrant()` relocates **immediately** and inspects the lease **not at all**
(§4.1.5 S-4). That is not merely a missing guard — the implementation contradicts the original intent,
which was specifically to *wait out* the lease. Consequences:

- `quarantined` is the **pool-activity-status**; `aberrant` is a **reason** for entering it. Not two
  statuses. This also settles the fate of the orphaned `iter_quarantine()` (§4.1.4 F2) — it becomes
  the real API, with `aberrant` surviving as a legacy bucket name.
- The deferred relocation needs an owner. Per §4.1.2 it is **not** the store — `set_status` raises
  while the lease is held rather than queuing a pending move — and it is **not** `reap()`, which is
  startup orphan recovery only. The manager retries on ticket replay, driven by a durable quarantine
  ticket, until the lease expires and the move succeeds.
- It also gives the manager a principled answer to the "case that will not die" problem, which it
  currently lacks: refuse to drive it, record why, and let the lease clock do the rest.

### 4.1 Storage access, case addressing, and meta-status — `OPEN` / design work

`CaseManager` reaches directly into `CachedFileFolders` from a dozen call sites and hardcodes physical
moves inside its transition pipelines. This section covers what that costs, what the manager actually
owes its callers around addressing and status, and the defects found while mapping it.

#### 4.1.1 Scope: a cohesion boundary, not pluggable behavior — `DECIDED 2026-08-01`

Two different things have been proposed under the banner of "abstract away `CachedFileFolders`," and
the tracker draws a hard line between them:

- **Pluggable *behavior* — rejected.** A storage layer selected at construction that changes what the
  system *does*: files-move vs. files-never-move, each with its own restartability story. Both
  motivating policies are gone (networked checkout/checkin is out of scope per §4.1.5 S-2; in-place
  operation per §4.0.3), and a second set of semantics would mean a second implementation of the hard
  parts. Not happening.
- **A cohesion *boundary* — accepted.** One set of semantics, gathered behind one object that speaks
  domain vocabulary instead of cache mechanics. Nothing about system behavior changes. That is the
  `CaseStore` refactor in §4.1.2.

These are not in tension, and the distinction is the whole point: a `CaseStore` that could *later*
admit a second implementation of the *same* behavior is a different proposition from a policy layer
whose implementations behave differently. The interface is deferred precisely so the boundary is not
mistaken for the rejected thing (§4.1.2).

#### 4.1.2 The `CaseStore` refactor — `DECIDED IN PRINCIPLE 2026-08-01`, details open

Extract the nuts and bolts of storage management into a thin object that wraps the folder-backed cases
and converts mechanical file operations into `CaseManager` domain concepts. Working name `CaseStore`.
In its current form it is a thin wrapper around `CachedFileFolders`; a secondary purpose is to
localize that coupling to one place.

##### Responsibilities

1. Manage the storage location of a case, durably.
2. Track execution status (`live`, `terminated`, `quarantined`, …), durably.
3. Let the manager add cases and change their status — a status change **may implicitly move the
   folder**.
4. Iterate the overall list.
5. Iterate cases at a given execution status.
6. *Future, optional:* searching of some kind. Not required of the base implementation.
7. Find a case's file location from its `case_id`, regardless of execution status.
8. *Future, optional:* look up case(s) by external key.

##### Scope and naming — `DECIDED 2026-08-01`

The name stays **`CaseStore`**, and the object stores **cases only**. It does *not* become a
`CaseManagerStore` absorbing the manager's other persistence (policy records, manifest, tickets,
staging, fleet board, death records). Four reasons:

- **Policy has a bootstrap problem.** The policy is needed to *construct* the store — grouping
  pattern, bucket names, ref-path template. Today `attach()` reads `case_manager_policy.yaml`, then
  builds the cache from `pol.grouping_pattern` (`case_manager.py:203`, `:130-133`). A store that
  persisted the policy would need the policy to exist before itself. Solvable with a two-phase open;
  bought for nothing.
- **The consistency semantics are opposites.** Case storage is large, durable for months, relocated by
  status, lease-disciplined. Manager records are small and hot — the manifest heartbeat rewrites on a
  sub-second cadence and is read from the *watchdog thread*; tickets live for seconds. One object with
  two consistency stories is the pattern §4.0.3 and §4.1.2 exist to avoid.
- **The future tiering variant sharpens the split.** If non-live cases can move off server, case
  storage becomes tiered and possibly remote — while tickets, manifest, heartbeat, and mailbox are
  *process-local coordination* that must stay local and fast. Merged, a remote implementation would
  have to reimplement all of that for no benefit.
- **It would re-couple what §4.0.2 separates.** The signaling adapter takes mailbox intake, results,
  and dead-lettering with it. If those lived in a manager-wide store, the adapter would depend on it —
  reintroducing exactly the circular dependency the extraction exists to invert (`MailboxProcessor(self)`).

Names shape scope: `CaseManagerStore` invites "anything the manager persists"; `CaseStore` says what
it stores. That is the point of keeping the narrower name.

**The underlying observation is still valid** — manager-owned persistence *is* scattered across ~10
modules doing path arithmetic under `policy_manager_dir(...)`, with a known layering violation (F5).
But **defer designing an owner for it until §4.0.2 resolves**: once the adapter takes the mailbox and
the host takes death records and shutdown, the residue may be small enough not to warrant an object at
all. Designing a namespace owner while the namespace's contents are in flux is premature.

**Where a quarantine/aberrant *reason* goes: not here.** The store owns status, not narrative. If a
reason is recorded at all, it belongs as a **custom event on the case object** — the case's own event
journal (`CaseEventJournal` / `CaseEventJournalView`, `folder_backed_case_support/case_journal.py`)
already travels with the folder, survives relocation, and is the established mechanism for "what
happened to this case." That keeps `CaseStore` ignorant of case internals entirely — no `reason`
parameter on `set_status` — which is a stronger boundary than the alternative. Consequence, now
settled: today's `AberrantSidecar` is deleted outright (F6); it is write-only debris and had no
reader even before this decision.

##### Why this is worth doing (it closes real defects, not just tidiness)

- **One owner for location + status.** Responsibilities 1 and 2 together make "location is the
  projection of status" an internal invariant rather than a cross-module agreement six call sites have
  to remember. This is what closes §4.1.6's biggest open question.
- **One enforcement point for the lease precondition** (S-4), which is currently missing from three of
  five move paths with no structural reason it should be present in any of them.
- **An index, not a scan.** Responsibilities 4/5/7 are exactly F3's fix; an object can memoize and
  rebuild where a module of free functions cannot.
- **F1's root cause dissolves.** That bug exists because an index row and an on-disk folder are two
  representations of one thing with no owner.
- **It collapses storage access to one chokepoint.** Today `adopt.py`, `termination.py`, `eject.py`,
  `aberrant.py`, `purge.py`, `reap.py`, and `case_manager.py` each call `cache.*` directly (~12
  sites), plus one that bypasses the cache API entirely with path arithmetic (F1). All of them route
  through `CaseStore` instead — which also makes the package's highest-fan-in module worth the unit
  test §2 wants.

##### Interface: deferred, discipline enforced another way — `DECIDED`

The *class* is justified by today's code. An *abstract interface* would be justified only by a second
implementation nobody has written — the same speculative generality §4.1.1 rejects, one level down.
**Build the concrete class now; extract an ABC if and when a second implementation actually arrives**
(a mechanical refactor at that point, with one caller).

The real argument for an ABC up front is that it disciplines the vocabulary — and that discipline is
genuinely needed, because a concrete wrapper makes it very easy to let cache mechanics leak into
public methods, producing a chokepoint that is not an abstraction. Buy the discipline directly instead,
with a rule that is inspectable and plausibly lint-testable:

> **No `CachedFileFolders` type, no `ref_path`, no `grouping_key`, no `slave_dir`, and no placeholder
> concept crosses the `CaseStore` boundary in either direction.**

##### Status changes, leases, and timing — `DECIDED IN PRINCIPLE`

`set_status(case_id, new_status_name, ignore_lease=False)` **raises immediately** if a good lease is
held and `ignore_lease` is false.

- **The store refuses; it does not defer.** Waiting out a lease is `CaseManager`'s timing protocol,
  not the store's. `CaseStore` therefore carries no "status set, move pending" limbo state, needs no
  internal reconciliation pass, and stays free of pool/scheduling vocabulary — which is what keeps it
  a storage object.
- **The store is not responsible for watching a case wind down.** It must, however, be aware that a
  multi-file copy takes real time — `eject.py:126-127` already falls back to `copytree` when
  `shutil.move` hits a cross-device `OSError`.
- **Therefore `set_status` is async.** The codebase already runs slow storage work off-loop
  (`process_pending_ticket`, `run_redundant_purge` both via `run_in_executor`). It also matters for
  §4.2: a synchronous multi-gigabyte copy inside `_maintenance_tick` stalls a tick that currently has
  no per-item isolation.
- **No write-ahead log inside the store.** The ticket *is* the WAL, one layer up, and it already
  exists. A second in-progress-move journal inside `CaseStore` is the two-mechanisms trap again, just
  hidden. What the ticket approach requires instead is that moves be **idempotent and re-drivable**
  (§4.1.6's convergence contract) — a property of the move, not a new subsystem.
- **Durability gap this opens, and its fix.** Between "manager stops driving" and "`set_status`
  finally succeeds," the store still says `live`; a crash in that window means restart re-admits the
  case — for §4.0.4's pathological case, putting the thing that raises on every interaction straight
  back in the pool. The manager's *intent* must be durable independently of the move. **Give
  quarantine a ticket, as termination and eject already have**, and have recovery consult quarantine
  tickets before re-admitting live cases. Note that aberrant is today the only transition with no
  ticket — it moves inline and writes a sidecar nobody reads afterward — and it is also the broken
  one (F1, F6).
- **`ignore_lease=True` is an operator escape hatch, not a routine parameter.** It has one genuine
  use: §4.0.4's case whose lease never expires because a live process keeps renewing it while failing
  every interaction. Because leases are time-based, that is the only situation where waiting does not
  work. But moving a folder out from under a process that believes it owns it is split-brain — open
  handles follow the inode while new path-based opens fail. Name it so it reads as dangerous
  (`force_despite_lease`), emit an escalation when used, and keep it out of every automatic
  `CaseManager` path.

##### Addressing protocol — `DECIDED IN PRINCIPLE`

Keep the interface file-centric and simple, but adopt a protocol that leaves room for future
materialization without building any of it now:

1. `CaseStore` talks in `case_id`, and offers a method to retrieve the path for a given `case_id`.
2. Internally it is optimized for fast `case_id` lookup of **live** cases — an in-memory dict is
   enough. This is the right *scope*: live cases are bounded by the pool/concurrency ceiling, while
   terminal buckets grow without bound, so indexing everything in memory is the one thing that would
   not scale.
3. *Future:* requesting the path of a non-live case might trigger a lock-and-retrieve from a remote
   location. Do not build this; just leave the space.
4. **Protocol rule:** when a case's status changes, callers must discard any path they hold and
   re-retrieve before next access.

Four notes on making this work:

- **Make the path getter `async` from day one**, and name it to suggest cost (`resolve_path` /
  `materialize_path`, not `get_path`). Remote retrieval is inherently slow; if the method is already
  async, adding it later touches no call site. This is the cheapest possible preparation and costs
  nothing today.
- **Say plainly which lookups stay slow.** Live-only in-memory indexing means non-live lookups still
  scan. One caller needs global coverage regardless of status: the adopt duplicate check
  (`_case_id_exists`) must see terminal cases, or a duplicate `case_id` gets admitted. Adopt is rare
  enough to eat a scan — it just must not be a surprise.
- **Both directions are needed.** `path → case_id` as well as `case_id → path`:
  `fire(case_folder=...)`, `normalize_case_folder_path()`, and the public `locate(case_folder=)` all
  go that way.
- **Do not over-apply `case_id`-centrism at the boundaries.** Adopt takes a source path that is not
  yet a case in the store; eject produces a path outside it. Those legitimately traffic in paths.

Rule 4 is cheaper than it first appears, and for a structural reason: status changes happen when the
manager is *losing* interest, so the case has already left the pool before any move. `reopen_case` is
the sole status change in the *gaining* direction, and it already re-resolves after the move. There is
also existing evidence the rule is real — `termination.py:163-167` already re-resolves when a ticket's
recorded `case_folder` has gone stale. Rule 4 promotes that ad-hoc fix to a stated invariant.

**Consequence to document:** `CaseLocation` becomes an explicitly point-in-time snapshot. It already
carries one field with a validity caveat (§5's `in_pool`-from-client issue); `case_folder` becomes a
second. Say so on the dataclass rather than leaving both as folklore.

##### Construction and wiring — `DECIDED IN PRINCIPLE`

- **`CaseManager.provision_local_case_store(root_dir)`** — a static convenience that constructs one of
  these. **Idempotent with validation:** provisioning the same thing again is harmless; provisioning
  something *different* at the same root raises. There is precedent to reuse rather than invent —
  today's `provision()` already compares an existing on-disk policy and raises `PolicyMismatchError`
  on mismatch (`case_manager.py:182-187`), and `CacheRootStateError` already covers "occupied root
  that isn't ours." Keep both behaviors.
  - Minor placement note: the knowledge belongs on the store (`LocalCaseStore.provision(root)`), with
    `CaseManager`'s static method a thin delegating convenience — discoverability without putting
    store-provisioning knowledge in the manager. The `local` in the name is worth keeping; it
    front-loads the distinction a future non-local variant would need.
- **The `CaseManager` constructor receives a `CaseStore`.** Consequences, all of them the point of the
  exercise: `case_manager.py` drops its `CachedFileFolders` import and construction entirely
  (`:24`, `:121`, `:130-133`, `:193`), and `CaseManagerConfig.cache_override` goes away. `open()` /
  `attach()` survive as conveniences that provision a store and then construct — the store-taking
  constructor is the primary path.
- **At startup the manager scans `live` to build its working set.** This maps directly onto today's
  `recover_manager()`, which already does `iter_case_folders_in_grouping(cache, live_grouping_key(...))`
  (`recover.py:56-61`); it becomes `store.iter_by_status("live")`. It also composes with
  `PoolMembershipJournal.compact_from_live(live_paths)` unchanged.
  - **Do not scan twice.** The store builds its live index by scanning at startup, and the manager
    builds its working set by scanning live. That is one scan — the manager should take its working
    set from the store's index, not run its own pass.

##### What `reap()` is actually for, and the seam — `DECIDED 2026-08-01`

**Purpose, restated.** Both of today's reap branches are gated on `not in_pool` (`reap.py:56`, `:61`),
so reap never touches a pooled case at all, and it has exactly one caller — `recover_manager()`
(`recover.py:97`). It is **startup orphan recovery**: *"my live storage contains a case nobody is
driving — what happened?"* Nothing is harvested or destroyed, despite the name (see the rename note
at the end).

It is explicitly **not** the mechanism for terminal-in-pool. A terminal case sitting in the active
pool is harmless — it no-ops on its next visit — and `_reconcile_terminal_in_pool()` archives it
within one tick (`case_manager.py:408`). That distinction gives the division of labor:

| | mechanism | cadence |
|---|---|---|
| Mid-run divergence (a half-completed move) | **ticket replay** (`replay_pending` in `_maintenance_tick`) | every tick |
| Startup orphans (crash left a case undriven) | **`reap()`** | once, at recover |

**The collapse — `DECIDED`.** Because terminal-in-pool is harmless, reap's first branch (terminal on
disk → enqueue termination from disk) is redundant: re-admit *every* orphan and let the per-tick
reconciler archive the terminal ones next tick. Reap becomes one rule:

> A live-status case that is not in the pool and whose lease has expired gets re-admitted. If it
> cannot be rehydrated, escalate.

This deletes `enqueue_termination_from_disk()` and `destination_key_from_record()`, removes reap's
need to read case records at all, and **fixes F7** by eliminating the second archive-label
computation. Keep the expired-lease gate (never re-admit something another process is actively
working) and the eject/quarantine-ticket skip. Accepted cost: each orphaned terminal case is admitted
and then removed by the next tick's reconciler — one rehydrate plus one lease acquire/release per
orphan, once, at startup.

**The seam — `DECIDED`.** `reap()` is a join across the two things this refactor separates, but it is
not one relationship; it is three, and they split along the new boundary:

| Relationship | Who can answer it | Where it goes |
|---|---|---|
| The store's index vs. what is actually on disk | Only the store | **Inside `CaseStore`** — on startup scan, repairable on demand |
| The store's status vs. pool membership | Only the manager knows the pool | **`CaseManager`** |
| Whether an orphan can be rehydrated at all | Needs the registry | **`CaseManager`** |

**The join stays in `CaseManager`; `reap()` does not move into `CaseStore`.** Moving it would require
injecting the driver, the registry, and the ticket queues into the store — three things it must not
know about — which is precisely the circular dependency this refactor exists to remove. The store
reports `(case_id, status, folder)`; the decisions are policy, and policy belongs to the manager. All
resulting actions go back through the store's public API or the ticket queues, never through direct
file operations.

Resulting shape:

```
live   = {e.case_id: e for e in store.iter_by_status(LIVE)}   # snapshot
pooled = {c.case_id for c in driver}

# 1. orphan re-admission — the actual job
for cid, entry in live.items():
    if cid in pooled:                        continue
    if not lease_expired(entry.folder):      continue    # another owner is working it
    if has_eject_or_quarantine_ticket(cid):  continue
    try:    driver.add(registry.rehydrate(entry.folder))
    except: escalate(ANOMALY)                            # the uniquely-reap branch

# 2. consistency assertion — pool holds a case the store no longer calls live
for cid in pooled - live.keys():             -> escalate + remove   (stale-path leak)
```

**Do not confuse the two "terminal in pool" situations** — S-1's two axes make them different:

- *Case record says terminal, store status still `live`, case in pool* → **harmless and expected.**
  `_reconcile_terminal_in_pool()` archives it next tick. Reap must not touch it.
- *Store status is `terminated` (the move already happened), case still in pool* → **a leak.** The
  pooled object holds a path that addressing rule 4 has invalidated. Direction 2 above catches it.

Two improvements fall out of the refactor rather than being extra work:

- **The missing direction becomes cheap.** §4.1.3 notes reap only asks "live-on-disk missing from
  pool," never the reverse. With an index that is a set difference; today it would need a scan. Note
  it is a consistency *assertion*, not a routine repair — under §4.0.3's exclusive filespace it should
  never fire.
- **Keying moves from path to `case_id`.** Today reap builds `pool_folders = {c.case_folder.resolve()
  for c in driver}` (`reap.py:49`). Path-keying is exactly what addressing rule 4 makes fragile;
  `case_id` is stable across moves.

**Iterator staleness rule.** Reap iterates the store while the manager may mutate status, and rule 4
says a status change invalidates any path already held. So **store iterators are point-in-time
snapshots**, documented as such, and reap must re-verify an entry before acting on it rather than
trusting a path read at the top of the loop. Cheap, but it has to be stated or the racy version gets
written.

**Rename — `DECISION NEEDED`.** "Reap" is wrong for what this does. The word connotes harvesting or
destruction; in its Unix sense it means collecting dead children. This method *revives* orphans — it
puts abandoned cases back to work — and after the collapse above, destruction is no part of it. The
codebase already uses the right verb internally: `ReapReport.readmitted`.

**Recommended: `readmit_orphans()`**, with `OrphanReadmitReport` (fields reduce to `readmitted` +
`anomalies`, since `termination_enqueued` disappears with the collapse). The obvious alternatives are
all taken or overloaded: `adopt` means external intake, `recover` is the whole startup sequence,
`sweep` is staging GC and the pool sweep, and `reclaim` already has a lease-adjacent meaning in
`LeaseReclaimTimings`. `recover_orphans()` is the runner-up — clearer about *when* it runs, but it
sits confusingly close to `manager.recover()`.

##### Sequencing

Do this **before** §4.0.2's protocol extraction. Both shrink `CaseManager`, and doing both at once
produces a very large diff on the class with the longest quirk list in §5. This work is inward-facing
(no public API change) and fixes actual defects; the protocol extraction changes the public method
surface. Within this work, do §4.2's per-item isolation fix and S-4's
lease precondition first — both are small, independent, and give the moved code a stable failure
contract before it moves.

#### 4.1.3 Grounding: what the meta-status vocabulary actually is today — `OPEN`

There is no meta-status enum anywhere in the code. Status is expressed by **three unrelated
mechanisms**, and which one carries it varies per status:

| Meta-status | Mechanism today | Written by | Findable by `locate()`? |
|---|---|---|---|
| staging (pre-adopt scratch) | `.case_manager/staging/<uuid>/` | `staging.allocate_staging_folder` | No (not a bucket) |
| inbound / offered | `.case_manager/adopt_drop/<name>/` | caller drops a folder | No |
| inbound rejected | rename to `ADOPT_REJECTED_<name>` in place | `case_manager.py:1006` | No |
| **live** | membership in the `live` bucket | `adopt.py` (`upsert_file`) | Yes |
| **in-transition (terminating)** | a ticket file in `.case_manager/termination/pending/` | `begin_termination` | No — folder still reads as live |
| in-transition (ejecting) | a ticket file in `.case_manager/eject/pending/` | `begin_eject` | No — folder still reads as live |
| **closed** | membership in a `terminal_<label>` bucket | `termination.py:187` `move_file` | Yes |
| aberrant (mechanical failure) | `aberrant` bucket (the `.case_manager/aberrant/<id>.yaml` sidecar is write-only — F6) | `aberrant.py` | Yes |
| **quarantined** | `quarantine_*` bucket glob | **nothing — see F2** | No (not in `managed_grouping_globs`) |
| ejected / exported | absence from managed space + `eject/done/<id>.yaml` | `eject.py` | No — indistinguishable from never-existed |
| purged | files unlinked in place by `CaseKeepManifest.purge()`; folder stays | `purge.py` | Yes (folder still there, contents gutted) |

Three structural observations fall out of that table:

- **Three parallel sources of truth for "what is this case's status."** (a) bucket membership, via the
  cache index; (b) the case's own `case_record.yaml` (`case_is_terminal`, `case_terminal_at`,
  heartbeat lease); (c) manager-side records (termination/eject tickets, `PoolMembershipJournal`;
  the aberrant sidecar is write-only debris and is being removed — F6).
  `_reconcile_terminal_in_pool()` exists because (b) and the pool disagree; `reap()` exists because
  (a), (b) and (c) disagree. S-1 resolves this by naming which source owns which question.
- **`in-transition` is the odd one out.** It is the only meta-status carried purely by a sidecar
  ticket and *not* visible in storage. A folder mid-termination still sits in `live` and still reads
  as live to `locate()` / `iter_live_bucket()`.
- **`reap()` reconciles one bucket in one direction.** It scans only the `live` bucket
  (`reap.py:48-51`) and only asks "is this on-disk case missing from the pool." It never asks the
  reverse ("is this pooled case still where I left it"), and never looks at `aberrant` or `terminal_*`
  for cases that should not be there. A bidirectional, all-bucket reconciler is a prerequisite for the
  convergence contract in §4.1.6.
- **No tombstone.** After eject, or after a case's folder is removed, `locate()` returns `None` —
  operationally identical to "never existed." `termination/done/` and `eject/done/` are a de facto
  ledger, but they are per-operation, never swept (F4), and consulted by no read API. Decide whether
  the manager owes a durable "this case used to be here, and left this way" answer.

#### 4.1.4 Defects confirmed while grounding this — `OPEN` (feed §5/§2)

- **F1 — `move_case_to_aberrant`'s orphan fallback cannot work.** When no cache index row exists
  (`aberrant.py:54` `find_file` → `None`), the else-branch writes a placeholder by raw path arithmetic
  on `cache.root_dir` (`aberrant.py:66`), bypassing `upsert_file` — so no index row is created. It
  then calls `cache.get_slave_dir(dst_grouping, ref_path)` (`aberrant.py:68`), whose per-file branch
  calls `find_file` again (`cached_file_folders.py:1516-1522`) and **raises `ValueError` when the ref
  is not in the index**. `find_file` is index-driven only (`cached_file_folders.py:1376`) and never
  discovers files from disk. So the fallback raises rather than copying the orphan folder. This is the
  exact path §2 already flags as untested — it is worse than untested, it appears non-functional.
- **F2 — `iter_quarantine()` is an orphaned public API.** `quarantine` appears exactly once in `src/`
  (`case_manager.py:879`). Nothing ever creates a `quarantine_*` grouping, and `quarantine_*` is not
  in `managed_grouping_globs()`, so even if one existed `locate()` / `reap()` / `purge()` could not
  see it. The method can only ever yield nothing. Per §4.0.4 this becomes the real API for the
  `quarantined` status rather than being deleted.
- **F3 — `locate()` is a full-fleet scan, and it is on the hot path.** `locate(case_id=...)` walks
  every managed bucket and reads `case_record.yaml` for every case until it matches
  (`iter_all_managed_folders` → `read_case_id_from_folder`), with no memoization.
  `locate_all(external_key=...)` is strictly worse: it constructs a `FolderBackedCaseReader` for
  *every* case in the fleet, every call. Callers include `fire()` (via `_resolve_single`) — so **every
  mailbox-submitted fire costs an O(fleet) record scan** — plus `_case_id_exists` on every adopt, and
  `_fleet_locate` on every fleet-board publish. Responsibilities 3 and 4 in §4.1.2 are exactly "there
  should be an index here"; the cache's index is keyed by `ref_path`, so it can serve neither query.
- **F4 — `termination/done/` and `eject/done/` grow without bound.** Nothing sweeps them.
  `ticket_exists()` checks `done/` too (`termination.py:62`), so these files are also load-bearing for
  termination idempotency — they cannot simply be TTL'd without deciding what replaces that guard.
- **F5 — layering violation to fix during the §4.1.2 extraction.** `_ensure_namespace_dirs()`, which
  owns the manager's *protocol* namespace, reaches out and creates a *storage* bucket:
  `live = mgr_dir.parent / policy.live_bucket; live.mkdir(...)` (`case_manager.py:1038-1039`).
- **F6 — `AberrantSidecar` is write-only. Delete it. `DECIDED`.** Grep across `src/` and `tests/`
  finds four references, all inside `aberrant.py`: the class, `aberrant_meta_dir()`, the construction,
  and the save. **Nothing anywhere loads it** — not `CaseManager`, not `CaseManagerClient`, not the
  CLI, not a single test. `iter_aberrant()` reads the *bucket*, not the meta dir. Three further
  problems: `reason=reason, last_error=reason` stores one value in two fields (`aberrant.py:76,79`);
  `source_path` records the pre-move path, stale the instant the move completes — exactly what
  addressing rule 4 forbids holding; and nothing sweeps `.case_manager/aberrant/`, so it accumulates
  one YAML per aberrant case forever (same class as F4). The operator-visible signal it might have
  justified already exists independently — `ADOPT_FAILED` and `TERMINATION_VERIFICATION_FAILED` go
  through `EscalationRegistry` on the same paths — and with the *reason* moving to a case event
  (§4.1.2), nothing is left. **Remove `AberrantSidecar` and `aberrant_meta_dir()`, and drop
  `"aberrant"` from `_ensure_namespace_dirs`.**
- **F7 — two archive-label computations that disagree three ways.** A case's terminal bucket is
  computed by `destination_key_for_case()` via `case.archive_grouping_label()` on the normal path
  (`termination.py:68-70`), but by `destination_key_from_record()` via `terminal_at.strftime("%Y-%m")`
  on the reap path (`termination.py:85-93`). They diverge:

  | source | computes |
  |---|---|
  | `archive_grouping_label()` docstring | "close month" |
  | `archive_grouping_label()` implementation | `_utcnow().strftime("%Y-%m")` — the month it was *archived* (`folder_backed_case.py:763-766`) |
  | `destination_key_from_record()` | `terminal_at.strftime("%Y-%m")` — the month it actually *closed* |

  A case terminating 31 July and archived after a 1 August restart lands in `terminal_2026-08` via one
  path and `terminal_2026-07` via the other. Worse, a case type that **overrides**
  `archive_grouping_label()` for tenant or fiscal period has that override silently ignored on the
  reap path, which hardcodes `%Y-%m`. §4.1.2's reap collapse removes the second computation entirely;
  separately, the docstring-vs-implementation mismatch wants fixing — `terminal_at` looks like the
  correct source.
- **F8 — `ReapReport` is computed and discarded at its only call site.** `recover.py:97` calls
  `await manager.reap()` without binding the result, and `RecoverReport` carries no reap fields. So
  orphan re-admissions and rehydrate anomalies never reach the caller of `recover()` — they surface
  only as logs and escalations. Either fold the counts into `RecoverReport` or state that escalations
  are the intended channel.

#### 4.1.5 Decisions taken — `DECIDED`

**S-1 — Two status axes, named separately.**

- The case's own FSM status stays owned by, and authoritative in, `case_record.yaml`.
- The manager gains a **pool-activity-status**: the *manager's* view of the case, explicitly not the
  case's internal status. An open-ended string with a handful of predefined values (`live`,
  `terminated`, `quarantined`, …). Semantics: **only `live` means the pool is actively attempting to
  drive the case forward.** Every other value — *including unrecognized ones* — means the case is not
  driven. That makes the field fail-safe by construction; write it down as an invariant rather than
  leaving it an accident of implementation.
- The two axes are expected to agree in ~99% of situations (notably `terminated`) but are deliberately
  **not the same field**. Disagreement is legal, and is something `reap()` *reports* rather than
  silently "fixes."
- This resolves §4.1.3's three-sources-of-truth problem by *naming which source owns which question*,
  instead of collapsing them.
- Bucket membership is the **projection** of pool-activity-status, not its definition — which is what
  lets §4.0.4's deferred, lease-gated relocation be legal without the status being in limbo meanwhile.

**S-2 — Networked / checkout-checkin storage is out of scope, and deliberately excluded.**

Architect's words: *"Locally speaking, this class thinks and talks in terms of filepaths."*

- The vocabulary **is** local `Path`, by decision. Signatures need not be path-agnostic, and should
  not be contorted to pretend otherwise.
- Case identity remaining folder-path-keyed in `CasePoolDriver` / `PoolMembershipJournal` is therefore
  fine and needs no change.
- A networked scenario is handled below/outside this class by whatever materializes local paths;
  `case_id`-plus-server addressing is not this class's concern.
- **Action:** state the exclusion in the class docstring so a future reader does not re-derive it.

**S-3 — The tier-transition hook is `DEFERRED`.**

A `TIER_CHANGED` pool event (or a polling seam over `driver.by_tier()`) was proposed to support
"reflexive commit when the case grows cold." That requirement belonged to the networked policy, which
S-2 rules out. **Do not extend the pool driver's event surface as part of this work** unless a fresh
motivation appears. `CasePoolEventNames`, `BalancedCasePoolDriver`, `SeniorityCasePoolDriver`, and the
`CasePoolDriver` ABC all stay as they are.

**S-4 — Lease-aware move refusal is a precondition on every relocation.** Its home is
`CaseStore.set_status(..., ignore_lease=False)` (§4.1.2); the grounding below is why it needs one.

The manager must be *marginally* aware of `FolderBackedCase`'s heartbeat-lease protocol: **if an
operation would move a case folder while the lease is held, raise** instead of moving.

- Grounding: that check exists today in only **2 of 5** move paths.
  - `adopt` — checks (`adopt.py:105-107`, rejects `ACTIVE_LEASE`); a TOCTOU window remains between
    validation and `_transfer_into_slave`.
  - `termination` — checks (`verify_termination_peek`, `termination.py:80-81`; retries on active lease).
  - `aberrant` — **no check.** `move_case_to_aberrant` relocates regardless (and per §4.0.4 should not
    be relocating at that moment at all).
  - `eject` export — **no check.** `begin_eject` calls `case_detach()`, but `process_eject_ticket`'s
    `shutil.move` (`eject.py:125`) never verifies the lease actually released.
  - `reopen_case` — **no check.** `move_file` (`case_manager.py:658`) relocates a terminal/aberrant
    folder with no lease inspection at all.
- Consolidating this into one enforced precondition — rather than five ad-hoc per-pipeline checks,
  three of them missing — is a straight win and is independently testable ahead of any other work
  here.
- Contract wrinkle to pin down: `is_heartbeat_expired()` is **tri-state** (`bool | None`,
  `folder_backed_case.py:513`), and `None` (no lease record at all) is handled inconsistently today —
  `adopt.py:106` refuses only on `is False`, while `staging.py:49-53` folds `None` in with `False`.
  Settle the tri-state rule once, centrally, instead of leaving it per-caller.

**S-5 — `quarantined` is a status; `aberrant` is a reason.** See §4.0.4 for the derivation.

#### 4.1.6 Still open — `DECISION NEEDED`

Two items that stood here — *where pool-activity-status durably lives* and *a `case_id`-keyed index* —
are **closed by §4.1.2**. Status is projected onto location and `CaseStore` owns both, so there is no
separate durable status store to design; and the index is responsibility 2/7 of that object. The
sidecar-vs-journal question evaporated with the deferral model: because `set_status` refuses rather
than defers, status and location never legally disagree, so nothing needs to carry a status that
location cannot express. The *storage-access chokepoint* item is likewise subsumed by §4.1.2.

What remains genuinely open:

- **Convergence over atomicity.** Rather than demanding crash-atomicity from each pipeline — which
  `move_file` does not provide across the surrounding ticket unlink, and which `adopt`'s per-child
  `shutil.move` loop plainly does not — make the contract: **every relocation is idempotent and
  re-drivable, and `reap()` converges any disagreement.** That is already the de facto design; making
  it explicit requires the bidirectional, all-bucket `reap()` noted in §4.1.3. This is now a stated
  precondition of §4.1.2's no-WAL decision, not merely a preference.
- **The quarantine ticket** (§4.1.2): confirm it follows the termination/eject ticket shape, and that
  recovery consults quarantine tickets before re-admitting live cases.
- **Departed cases are announced, not catalogued.** The manager does
  not become the librarian of departed cases. Case departures (terminated, quarantined, ejected) are
  **emitted as lifecycle events**; an owning application that needs history subscribes and keeps its
  own record. This is the same refusal as §4.1.2's scope decision — a durable ledger would mean
  retention policy, sweeping, and a query API, all of which are new manager responsibilities.
  - **Channel — `DECISION NEEDED` (small).** Do not build a second registry. `EscalationRegistry` is
    already kind-tagged pub/sub (`emit_simple("ADOPT_REJECTED", ...)`, `"REAP_ANOMALY"`, …), so widen
    it to carry lifecycle kinds alongside problem kinds and let subscribers filter. That is less
    machinery than a parallel channel, at the cost of renaming the public
    `on_escalation` / `off_escalation` surface, since "escalation" would no longer describe a normal
    termination. A pre-release rename is acceptable.
  - **Delivery is at-most-once.** In-process pub/sub does not survive a crash: if a case departs and
    the process dies before a subscriber persists, the event is lost. Acceptable — this is operator
    convenience, not correctness — but say so rather than implying an audit log.
  - **This fully resolves F4.** `done/` is load-bearing today only because `ticket_exists()` uses it
    for termination idempotency. Once `CaseStore` status is authoritative, that check consults the
    store instead — a case already in `terminated` status needs no ticket to prove it — so `done/`
    becomes purely informational and can be TTL'd or dropped outright.
- **`CaseKeepManifest` interaction.** No longer needed for a status sidecar, but still worth
  confirming: `purge()` deletes every file matching no keep rule, skipping only `_keep.txt` and the
  lease (`case_keep_manifest.py:25`, `100-107`). Verify nothing `CaseStore` relies on inside a case
  folder is purgeable.

### 4.2 Error / edge-case handling within a running pool — audit done, fixes `OPEN`

The Wave 1 audit pass is complete. Two of its findings were fixed in Wave 1 (`_maintenance_tick`
per-item isolation, and the adopt-failure lease release); the rest are recorded here, ranked by blast
radius. **None of them block the Wave 1 merge** — every one predates this branch's work — but the two
`KILLS THE FLEET` items should lead Wave 2.

**Fixed in Wave 1**

- [x] `_maintenance_tick()` isolates each item: termination tickets, eject tickets, the mailbox drain,
      and purge each degrade to log + `MAINTENANCE_ITEM_FAILED` escalation instead of aborting the tick.
- [x] Adopt's failure path released the heartbeat lease before quarantining, so the handler can
      finish instead of raising a second, more confusing error.

**KILLS THE FLEET**

- [ ] `OPEN` — `_live_or_evict` (`balanced_case_pool_driver.py:773-774`) catches only
      `FileNotFoundError`, `CaseAlreadyOpenError`, and `CaseTypeMismatchError`. A corrupt or truncated
      `case_record.yaml` raises `ValidationError`, and an unregistered type raises
      `UnregisteredCaseTypeError` — neither is caught. It is called mid-sweep, so the exception exits
      `advance()` into `_manager_loop`, which is **outside** `_isolated_tick_item`. It repeats
      deterministically every beat, so `_LOOP_FAILURE_LIMIT` is exhausted in three ticks. **One corrupt
      record kills the whole fleet.**
- [ ] `OPEN` — `_reconcile_terminal_in_pool()` and `_detect_escalations()` run in `_manager_loop` but
      outside `_isolated_tick_item`. `begin_termination` does remove → detach → `write_ticket`; if the
      ticket write raises (stale `.lock` sidecar), the case is out of the pool, detached, and
      ticketless — stalled until restart — *and* the failure counts against the loop budget.

**STALLS A CASE PERMANENTLY**

- [ ] `OPEN` — A corrupt termination ticket is an infinite retry with no failure path.
      `retry_count` lives *inside* the ticket, so an unparseable one can never reach
      `TerminationState.FAILED` and is never unlinked. Meanwhile `begin_termination` already removed
      and detached the case, and `ticket_exists()` returns True forever, so nothing re-enqueues it.
      The case is stranded in the live bucket and the manager escalates about it every tick. The
      general shape: **the counters that would eventually quarantine a bad item live in the very file
      that failed to parse.**
- [ ] `OPEN` — Same trap in `process_pending_ticket`: `verify_termination_peek` reads the record and
      can raise *before* `retry_count += 1`. TOCTOU between `folder.exists()` and the read.
- [ ] `OPEN` — `eject_from_pool()` can hang forever. The waiter future is resolved only on the success
      path; `process_eject_ticket` returns `None` on every failure, and when the retry cap is hit the
      ticket is moved to `failed/` and unlinked — so the future is never resolved and never will be.
      With `timeout=None` the caller waits indefinitely.
- [ ] `OPEN` — Orphans whose rehydration fails are never retried. `reap()` logs and escalates, but has
      exactly one caller (`recover.py`), so nothing revisits them until a restart. Worse, a failed
      `driver.add` leaves the case object attached, so its lease never lapses and later passes skip
      the folder as owned.

**LOSES INFORMATION / NOISE**

- [ ] `OPEN` — Every YAML write is non-atomic (`open(path, 'w')`, truncate in place — no temp+rename),
      and the reader converts *any* parse failure into an empty dict. Models with required fields turn
      that into a `ValidationError`, which is the lucky case; a model whose fields all have defaults
      would silently load as a valid default object. Every hot reader also bypasses the advisory lock.
- [ ] `OPEN` — Event-journal scans race deletion in three places (`.exists()` then `iterdir()`, and
      bare `stat()` on paths from an earlier `iterdir()`), reachable from `reap`, from
      `verify_termination_peek`, and from the fleet board.
- [ ] `OPEN` — `restore_pool_from_journal`'s classifier misses binding errors, so one bad folder
      aborts recovery with no partial `RebuildReport` — the operator loses the record of what *was*
      recovered, and every path after the bad one is silently skipped.
- [ ] `OPEN` — `CaseManagerClient`'s manifest read is unlocked and unguarded, so a client reading
      mid-write gets a raw pydantic error from every `only_if_fresh` API.

**Framing for Wave 2.** `_isolated_tick_item` contains the *manager*, not the *case*: it converts
"loop death" into "this case is stalled forever and escalates once per tick." That was the intended
trade, but it is only half a solution while the retry counters live inside the files that fail to
parse. A quarantine path keyed on something outside the corrupt artifact is the missing piece, and it
belongs with `CaseStore`'s status model (§4.1.2).

### 4.3 Low-effort path to a running case-manager script — `OPEN`, tooling gap

Confirmed gap: `cached_file_folders_support/examples/` has a rich set of runnable examples (Sharepoint
sync, Gmail sync, retention policy, etc.), and `folder_backed_case_support/case_examples/` has one
worked case type (`indexable_file_case.py`). The `case-designer` skill scaffolds a new
`FolderBackedCase` subclass with stub hooks. **Nothing plays the equivalent role for actually running a
fleet** — there is no example script or generator that takes "I have N registered case types" to "a
runnable process serving them," and no CLI (`manager_health.py` only health-checks an already-running
manager, it doesn't start one). Today, getting a manager running requires hand-assembling
`CaseManager.open(...)` + registering types + calling `case_manager_host.serve(...)`, currently only
demonstrated piecemeal across tests and the tutorials' prose.

- [ ] `OPEN` — **Bag-loading construction convenience.** The common shape "I have a folder of case
      folders; run them" currently requires provision + register types + an `adopt_case()` loop, and
      `open()` refuses a non-empty root outright (`case_manager.py:232-240`). A single entry point
      that provisions a fresh managed root and bulk-adopts a source folder — plus the matching pytest
      fixture — removes that ceremony without a second storage model (§4.0.3). Note that `adopt`
      *moves* rather than copies (`adopt.py:126-137`), so the convenience should copy the source bag
      first and leave the caller's folder untouched, which also makes repeated runs repeatable.
- [ ] `OPEN` — Decide the shape of this: a runnable example script under
      `case_manager_support/examples/` (mirroring the `cached_file_folders_support/examples/`
      convention)? A generator/skill akin to `case-designer` but for the manager side ("scaffold a
      runnable manager script for these case types")? A `totodev-manager-*` CLI subcommand that
      provisions + serves from a small config file? Needs a decision on the right level of
      abstraction before building it.

### 4.4 Hands-on bench-testing pass — `OPEN`, distinct from the static inspection pass in §5

The maintainer wants a dedicated round of hands-on, interactive experimentation with these classes —
running them, poking at them, deliberately trying odd sequences — specifically because this kind of
bench-testing tends to expose rough edges that a read-through of the source (which is what produced
§5's checklist) doesn't surface. This is a **separate workstream from §5**: §5 is desk-review
(reading code, cataloging quirks); this is empirical (running code, cataloging friction), and either
can feed new items into the other.

- [ ] `OPEN` — This workstream is naturally blocked on / motivated by §4.3: bench-testing needs
      exactly the kind of low-effort harness described there (spin up a manager, register a couple of
      case types, drive them, observe) — solving 4.3 first would double as the tool for this pass,
      rather than each hand-experiment starting from scratch against raw `CaseManager.open(...)` calls.
      Consider sequencing 4.3 before or alongside this.
- [ ] `OPEN` — Scope not yet defined: which classes/flows get the bench-testing treatment first?
      Candidates based on the map above: the adopt → live → terminate happy path; a forced
      reclassify-mid-flight; an eject under contention; a deliberate crash-and-recover cycle (kill -9
      mid-tick, restart, confirm `PoolMembershipJournal`/`recover_manager` behave); choke-limit
      contention between `BalancedCasePoolDriver` and `SeniorityCasePoolDriver` running the same
      workload side by side. This list itself should be treated as a starting point, not a fixed plan.
- [ ] `OPEN` — As rough edges are found, record them back into this tracker (§5, or a new "found via
      bench-testing" subsection) rather than only as ad hoc notes, so the desk-review and hands-on
      findings stay in one place.

## 5. Architecture / ergonomics inspection pass — per-class quirk checklist

Raw material surfaced while mapping the code, **not yet judged** — this is the starting checklist for
the "round of inspection to improve ergonomics and architecture" pass. Expect this list to grow as the
pass proceeds; it is known to be incomplete.

### `CaseManager` / `CaseManagerClient`
- [ ] `IncompatibleReclassError` is imported from two different paths across `case_manager.py` (via
      `totodev_pub.folder_backed_case` re-export) and `case_manager_client.py` (direct from
      `folder_backed_case_support.exceptions`) with no apparent reason for the difference.
- [ ] `_log_startup_summary()` reaches into other objects' private attributes
      (`self._registry._registry`, `self._escalations._handlers`) just to log counts, rather than
      those classes exposing a public count/`__len__`.
- [x] `_replay_*_pending()` only counted; neither replayed anything. Renamed to
      `_count_termination_pending()` / `_count_eject_pending()`.
- [ ] `eject_from_pool`'s `wait_halted=lambda f: None` passed into `begin_eject` looks vestigial —
      the actual wait happens via a separately tracked future elsewhere. No comment explains the
      redundancy; worth simplifying or documenting.
- [ ] `reclassify_case`'s failure-path re-admission logic (nested try/except that re-raises the
      *original* exception regardless of whether the recovery attempt itself also failed) is dense
      and easy to misread — candidate for a readability pass.
- [ ] `CaseLocation.in_pool` is silently meaningless when produced by a `CaseManagerClient`'s own
      (unsynced) manager instance — `_preflight_reclassify` works around this with a different check,
      but `CaseLocation` itself carries no marker distinguishing a trustworthy `in_pool` value from an
      untrustworthy one. A caller of `client.locate()` who naively trusts `.in_pool` gets a
      wrong-but-plausible answer.
- [ ] `iter_terminal()` sources its glob from `self._policy.terminal_prefix`; `iter_quarantine()`
      hardcodes `"quarantine_*"` inline instead of a corresponding policy field — inconsistent
      sourcing between the two "special bucket" iterators. Nothing writes a `quarantine_*` bucket,
      so `iter_quarantine()` can only ever yield nothing (F2); it becomes the real API for the
      `quarantined` status in Wave 2.
- [ ] `locate()` / `locate_all()` are full-fleet record scans on the hot path (every `fire()`, every
      adopt, every fleet-board publish) — see §4.1.4 F3 for the measurement and §4.1.6 for the
      proposed fix. Called out here too because it is a `CaseManager` API-shape problem, not only a
      storage-abstraction problem.
- [ ] `import shutil` at `case_manager.py:19` is unused — the only `shutil` reference in the file.
- [ ] `_ensure_namespace_dirs()` creates a *storage* bucket (`mgr_dir.parent / policy.live_bucket`,
      `case_manager.py:1038-1039`) from inside the routine that owns the *protocol* namespace — see
      §4.1.4 F5.
- [ ] `termination/done/` and `eject/done/` are never swept, and `done/` presence is load-bearing for
      `ticket_exists()` idempotency — see §4.1.4 F4.
- [ ] `CaseManager.serve()` is a full public instance method whose own docstring says not to call it
      directly (the "blessed way" is the module-level `case_manager_host.serve()` import) — consider
      whether it should be removed, renamed to signal discouragement more strongly, or left as-is.
- [ ] The `§N` comment convention (`§1`, `§2`, `§6`, `§8`, `§11`...) throughout both files references
      an external design doc not present anywhere in the repo — a reader loses the "why" behind
      several behaviors without it. Either locate/attach the referenced doc or replace with inline
      rationale.

### Pool drivers (`case_pool_driver.py`, `balanced_case_pool_driver.py`, `seniority_case_pool_driver.py`)
- [ ] `SeniorityCasePoolDriver._make_slot` manually re-lists all 16 `_Slot` fields to build a
      `_SenioritySlot` — any future field added to `_Slot` in the base class silently won't propagate
      here; nothing enforces sync. Candidate for `dataclasses.replace`-based construction instead.
- [ ] `SeniorityCasePoolDriver` imports `_Slot`/`_TierPolicy` (underscore-prefixed,
      module-private-by-convention) directly from `balanced_case_pool_driver` — a same-package-style
      coupling across files worth naming explicitly (protected/internal API?) if it's going to stay.
- [ ] Duplicated `isinstance(slot, _SenioritySlot)` defensive guards in two hook overrides
      (`_slot_prelaunch`, `_slot_post_step`), seemingly always-true given the only slot-construction
      path, with no comment on why the check exists.
- [ ] `_order_chokeables`'s seniority-preserving behavior (return the list unchanged) depends on an
      implicit, un-enforced invariant chain (sweep visit order == dict insertion order == queue
      order) rather than any structural guarantee. Consider asserting the invariant or documenting it
      where the chain could break.
- [ ] `SeniorityCasePoolDriver.peek()`'s `queue_position` is computed via
      `list(self._by_folder.keys()).index(...)` — O(N) per call, inconsistent with the O(1) spirit of
      the rest of `peek()`/`CasePeek`.
- [ ] Seniority ordering is documented to govern contested *scheduling* capacity but does **not**
      extend to shutdown-drain ordering (`stop()`/`settle()` aren't overridden) — this scope boundary
      isn't called out in the class's own docstring; a reader could reasonably assume it applies to
      drain too.
- [ ] `LeaseReclaimTimings.deadline_margin_secs` (`pool_membership_journal.py`) is declared but never
      referenced anywhere — looks like a parameter that was never wired into the actual deadline
      logic. Wire it up or remove it.

### `case_manager_support/*`
- [ ] `constants.py` declares `TERMINATION_SUBDIR`/`EJECT_SUBDIR`/`RESULTS_SUBDIR`/
      `ABERRANT_META_SUBDIR` (itself dead once F6 lands), but `termination.py`/`eject.py`/`aberrant.py` all hardcode their own
      subdir name strings inline instead of importing these constants — values agree today, but it's
      a drift risk. Wire the actual usages to the constants.
- [ ] `mailbox/__init__.py` is a single empty comment line — the sibling `processor.py` defines a
      large public surface (`MailboxProcessor`, 4 request/result types, `RequestHandle`) that nothing
      re-exports at the `mailbox` package level; every caller reaches into `mailbox.processor`
      directly. Consider re-exporting for a cleaner import surface.
- [ ] `MailboxProcessor.poll_result()` determines a result's type by sniffing substrings in the raw
      YAML text (`"kind: shutdown"`, `"adopt"` in the first 200 chars, etc.) rather than a single
      structured discriminator field, with a 3-deep try/except fallback cascade. Works today; fragile
      if a future result type's YAML happens to contain one of those substrings. Candidate fix: a
      `kind:` field on every result type (mirroring what `ShutdownAck`/`ReclassifyResult` already
      do).
- [ ] `recover.py` imports `escalation.CaseEscalationKind` but it appears unused in the function
      body — check for a dead import.
- [ ] `eject.py`'s `begin_eject` calls `request_halt`/`wait_halted` but discards the result
      (`if ... is not None: pass`) with a comment saying "caller awaits halt separately" — a confusing
      dead branch even though it's by design. Consider simplifying the signature so it doesn't look
      like an unfinished implementation.
- [ ] `termination.py`'s `process_pending_ticket` is async-shaped (lives in an async pipeline) but has
      no actual `await` in its body — not wrong, just inconsistent with its neighbors; either make it
      a plain sync function called via `run_in_executor` (as it already is) with a non-async signature,
      or note why it's async-shaped anyway.

---

## 6. Deferred / explicitly out of scope (tracked for visibility, not actionable now)

- `DEFERRED` — Mailbox case-removal protocol (`_backlog/proposed_mailbox_case_removal_protocol.md`):
  deliberately not implemented pending 5 open design questions and maintainer discussion. Revisit as
  its own effort, not folded into this branch's closeout.
- `DEFERRED` — `SimplifiedLLM`/LangChain replacement (`_backlog/proposed_simplified_llm_refactor.md`):
  unrelated topic, not part of this feature's scope at all.
- `DEFERRED` — Enterprise/object-storage scale-out (`What if FolderBackedCase needed enterprise
  scale.md`): explicitly labeled a thought experiment, "not a proposal, not scheduled work."

---

## 7. Implementation waves

Three sequential waves covering everything actionable in §1–§5. Items are referenced by their home
section; this is a grouping and an ordering, not a restatement. A handful of items are deliberately
split across waves and are marked **(split)**.

### Wave 1 — Correct and cover what exists

No structural change. Everything here lands in the current shape of the code.

**Status: landed.** §1 is closed and the branch is mergeable on demand; the merge itself is held
until the design settles. Shipped across five commits —
deletions/naming/driver ergonomics, the aberrant orphan fix, the lease guard and archive label, the
§1 blockers, and purge/config coverage.

Defects found while doing the work, none of which were in this tracker beforehand: the aberrant
orphan-rescue path always raised (F1); an explicitly injected `CasePoolDriver` was silently discarded
because `CasePoolDriver.__len__` makes an empty driver falsy; and the two archive-label computations
disagreed, so a case could land in different terminal buckets depending on which path ran (F7).

**Exit criterion: §1 is fully closed — done.** That makes the branch *mergeable*; it does not merge
it. The merge into `main` is deliberately held until the maintainer is satisfied with the design, so
readiness and landing are separate decisions. Every §1 promotion blocker still had to land here,
including the two whose permanent fix arrives in Wave 3, because the branch has to be mergeable on
demand.

**Waves 2 and 3 therefore branch from `explore/case-queue`, not from `main`.**

**Defect fixes**

- [ ] F6 — delete `AberrantSidecar`, `aberrant_meta_dir()`, the `"aberrant"` entry in
      `_ensure_namespace_dirs`, and the now-dead `ABERRANT_META_SUBDIR` constant.
- [ ] F1 — `move_case_to_aberrant()` orphan fallback (raises `ValueError` today).
- [ ] F7 **(split)** — fix `archive_grouping_label()`'s docstring-vs-implementation mismatch and
      settle `terminal_at` as the label source. The duplicate computation is removed in Wave 2.
- [ ] F8 — surface `ReapReport` at `recover.py:97`, or state that escalations are the channel.
- [ ] S-4 **(split)** — add the missing lease-before-move checks to `aberrant`, `eject` export, and
      `reopen_case`; settle the `is_heartbeat_expired()` tri-state rule. Consolidation into
      `CaseStore.set_status()` is Wave 2; the tests written here carry over unchanged.
- [ ] §4.2 — per-item isolation in `_maintenance_tick()` (termination loop, eject loop, purge).
- [ ] §4.2 — audit rehydration-raises during sweep / `fire()` / adopt.
- [ ] §4.2 — adversarial filesystem pass (truncated / concurrently modified / deleted mid-sweep).

**Promotion blockers with an interim fix here** (permanent fix in Wave 3)

- [ ] §1 — hoist the shutdown-mailbox check above the `enable_mailbox` / `watchdog_enabled` gates
      (option A). Superseded when shutdown moves to the host.
- [ ] §1 **(split)** — `mailbox_neglect` coverage, written against current ownership. Wave 3 updates
      it when the adapter takes over.

**Small resolved items to land**

- [ ] §1 — implement the `_loop_failure_no_watchdog` fail-loud fallback.
- [ ] §2 — drop `PurgeReport.stragglers`.

**Test coverage**

- [ ] §1 — exit-70 integration test (3 consecutive `_manager_loop` tick failures through a real
      `serve()` run).
- [ ] §1 — watchdog coverage for `tick_slow` alarm-only and stop-timeout → `EXIT_WATCHDOG`.
- [ ] §2 — `aberrant.py` direct coverage (both branches), ahead of F1.
- [ ] §2 — `purge.py` coverage, including mid-purge error.
- [ ] §2 — `CaseManagerConfig` test.

**Independent ergonomics — pool drivers** (nothing in Waves 2–3 touches these)

- [ ] §5 — `SeniorityCasePoolDriver._make_slot` field duplication.
- [ ] §5 — `_Slot`/`_TierPolicy` cross-file private import.
- [ ] §5 — redundant `isinstance(slot, _SenioritySlot)` guards.
- [ ] §5 — `_order_chokeables` implicit invariant chain.
- [ ] §5 — `peek()`'s O(N) `queue_position`.
- [ ] §5 — seniority shutdown-drain scope boundary in the class docstring.
- [ ] §5 — `LeaseReclaimTimings.deadline_margin_secs` dead field.

**Independent ergonomics — manager and support**

- [ ] §5 — `IncompatibleReclassError` imported via two paths.
- [ ] §5 — `_log_startup_summary()` reaching into `_registry._registry` / `_escalations._handlers`.
- [ ] §5 — rename `_replay_termination_pending` / `_replay_eject_pending` to counting names.
- [ ] §5 — `eject.begin_eject`'s vestigial `wait_halted` branch.
- [ ] §5 — `reclassify_case` failure-path readability.
- [ ] §5 — `CaseManager.serve()` docstring / naming.
- [ ] §5 — unused `import shutil` (`case_manager.py:19`).
- [ ] §5 — `recover.py` dead `CaseEscalationKind` import.
- [ ] §5 — `termination.process_pending_ticket` async-shaped signature.
- [ ] §5 — wire `TERMINATION_SUBDIR` / `EJECT_SUBDIR` / `RESULTS_SUBDIR` to their usages.

**Mechanical hygiene**

- [ ] §3 — `folder_backed_case_support/__init__.py` `__all__` exports.
- [ ] §3 — re-export `CacheRootStateError` / `PolicyMismatchError`.
- [ ] §3 — stale `"Planned CaseManager (draft: ...)"` comments (4 locations).
- [ ] §3 — tutorials 2 & 3 stale backlog path.
- [ ] §3 — `proposed_manager_watchdog_and_host.md` dangling link and wrong code-placement path.
- [ ] §3 — remove stray `fleet_watcher.cpython-311.pyc`.
- [ ] §3 — `_backlog/README` convention: follow it or update it.

**De-risk**

- [ ] §4.4 **(split)** — rough bench pass against hand-assembled `CaseManager.open(...)`, before any
      structural work. Findings feed §5. The tooled pass is Wave 3.

### Wave 2 — Extract `CaseStore`

Inward structural change. No public API change.

- [ ] §4.1.2 — build `CaseStore` (concrete class, no ABC) and the boundary vocabulary rule.
- [ ] §4.1.2 — `CaseManager` constructor takes a `CaseStore`; drop `CachedFileFolders` from
      `case_manager.py` and `cache_override` from `CaseManagerConfig`; `open()` / `attach()` become
      conveniences.
- [ ] §4.1.2 — `provision_local_case_store(root_dir)`, idempotent with validation.
- [ ] §4.1.2 — startup live scan builds the working set from the store's index (one scan, not two).
- [ ] §4.1.2 — `set_status(case_id, status, ignore_lease=False)`: async, refuses on held lease,
      consolidates S-4 **(split)**.
- [ ] §4.1.2 — addressing protocol: `case_id` ↔ path both directions, async path resolution,
      point-in-time iterators, stale-path rule.
- [ ] §4.1.2 — reap collapse to the single re-admit rule, plus the pool-holds-non-live assertion;
      removes the duplicate archive-label computation **(split, completes F7)**.
- [ ] §4.1.2 — rename `reap()` → `readmit_orphans()` / `OrphanReadmitReport` (name pending).
- [ ] §4.0.4 — aberrant as stop-driving + deferred lease-gated relocation.
- [ ] §4.1.6 — quarantine ticket, following the termination/eject shape; recovery consults it before
      re-admitting.
- [ ] §4.1.6 — state the convergence contract (idempotent, re-drivable, ticket-replayed).
- [ ] §4.1.6 — emit case-departure lifecycle events (widen `EscalationRegistry` to kind-tagged
      lifecycle + problem events; rename the `on_escalation` surface accordingly).
- [ ] §4.1.6 / F4 — move `ticket_exists()` idempotency onto store status, then TTL or drop `done/`.
- [ ] §4.1.6 — confirm nothing `CaseStore` relies on inside a case folder is purgeable.
- [ ] F2 — `iter_quarantine()` becomes the real API for the `quarantined` status.
- [ ] F3 — `locate()` / `locate_all()` served by the index.
- [ ] F5 — `_ensure_namespace_dirs()` stops creating a storage bucket.
- [ ] §5 — `CaseLocation` documented as a point-in-time snapshot (`in_pool` and `case_folder`).
- [ ] §5 — `iter_terminal()` / `iter_quarantine()` glob sourcing.
- [ ] §2 — `CaseStore` unit tests, satisfying the `layout.py` coverage gap.
- [ ] §0 — `CaseManagerPolicy` Tier-1 layout fields move into `CaseStore` construction.

### Wave 3 — Extract the protocols, then make it runnable and documented

Outward structural change, then the tooling and documentation that can only be written truthfully
once the shape is final.

- [ ] §4.0.2 — extract the signaling adapter (fire / adopt / reclassify, correlation IDs, result
      publication, dead-lettering, replay); shutdown moves to the host.
- [ ] §4.0.2 — two-party recovery sequencing; `RecoverReport` mailbox fields move out.
- [ ] §4.0.2 — `is_idle` becomes a host-computed composite.
- [ ] §4.0.2 — `adopt_case()` sheds `correlation_id`.
- [ ] §1 — shutdown-mailbox handling moves to the host, superseding Wave 1's interim hoist.
- [ ] §4.2 — `MailboxProcessor` per-request isolation, in the rewritten intake loops. **Known gap
      shipped at the Wave 1 merge** — a malformed request can still poison an intake batch until this
      lands.
- [ ] §1 **(split)** — update `mailbox_neglect` coverage for the adapter's ownership.
- [ ] §5 — `poll_result()` result-type discriminator replacing substring sniffing.
- [ ] §5 — `mailbox/__init__.py` re-exports.
- [ ] §4.3 — bag-loading construction convenience plus pytest fixture.
- [ ] §4.3 — runnable manager example / generator / CLI: decide shape and build.
- [ ] §4.4 **(split)** — full bench-testing pass on the tooled harness; findings back into §5.
- [ ] §1 — operator deployment doc (restart policy, exit codes, container caveats).
- [ ] §3 — `case-designer` skill updated for the pool-manager layer.
- [ ] §5 — replace the `§N` comment convention with inline rationale.
- [ ] §0 — confirm every class in the key-class list meets the finalize bar.
