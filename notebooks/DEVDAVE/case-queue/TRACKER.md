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
- `LocalCaseStore` (`case_manager_support/case_store.py`) — the storage boundary: location and
  pool-activity-status owned together, addressed by `case_id`. Deliberately concrete, with no ABC
  until a second implementation actually exists. Its vocabulary rule is enforced by a
  source-inspection test rather than convention.
- `CaseManagerPolicy` (`case_manager_support/case_manager_policy.py`) — the persisted deployment
  record. It still *declares* the Tier-1 layout facts, but only `LocalCaseStore` acts on them now; it
  may shed its mailbox/transport fields if §4.0.2's protocol extraction goes forward.
- `CaseManagerConfig` (`case_manager_support/case_manager_config.py`) — process-local wiring
  (driver/registry/notice handlers). Covered by `tests/test_case_manager_config.py`.
- `FleetStatusBoardWriter` / `FleetStatusBoardWatcher` (`fleet_status.py` / `fleet_status_watcher.py`)
  — fleet snapshot writer + client-side watcher. No open quirks beyond the private-symbol imports
  noted in the original mapping; lowest-risk pair in this list.
- `NoticeRegistry` (`notice.py`) — in-process pub/sub carrying both problem and lifecycle kinds, told
  apart by `kind.is_lifecycle`. Delivery is at-most-once by design and says so.

Everything else in `case_manager_support/` (`adopt.py`, `termination.py`, `quarantine.py`,
`eject.py`, `readmit.py`, `purge.py`, `recover.py`, `shutdown.py`, `staging.py`, `layout.py`,
`case_manager_host.serve()`) is function-based pipeline code rather than a class with its own
lifecycle — tracked by module in §2/§3/§5 rather than listed here. The four move-shaped pipelines
(`adopt`/`termination`/`quarantine`/`eject`) now relocate cases only through `LocalCaseStore`, which
is where the §4.1.5 S-4 lease precondition is enforced.

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

- [x] Quarantine covered by `tests/test_case_manager_quarantine.py` — immediate move, deferred move,
      the ticket surviving repeated attempts without spending a retry, the reason landing on the
      case's own journal, and orphan absorption.
- [x] `purge.py` covered by `tests/test_case_manager_purge.py`, including the mid-purge error path
      and the tick isolation around it.
- [x] `PurgeReport.stragglers` dropped (Wave 1).
- [x] The highest-fan-in storage code has direct unit tests: `tests/test_case_store.py` (23), which
      is what `layout.py` never had. `layout.py` itself is now four small functions, all covered
      through `tests/test_case_manager_lease_guard.py` and the store suite.
- [x] `case_manager_config.py` covered by `tests/test_case_manager_config.py`.

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

#### 4.1.3 Grounding: the meta-status vocabulary before `CaseStore` — historical

**This section describes the state Wave 2 replaced.** Pool-activity-status is now a named vocabulary
owned by `CaseStore`, projected onto bucket membership, with `quarantined` a real status and
in-transition carried by tickets that no longer double as receipts. The table is kept because it is
the evidence for why the boundary was worth building — it is what "three unrelated mechanisms" looked
like in code.

There was no meta-status enum anywhere. Status was expressed by **three unrelated mechanisms**, and
which one carried it varied per status:

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

#### 4.1.4 Defects confirmed while grounding this — **all closed**

F1, F6, and F8 closed in Wave 1; F2, F3, F4, F5, and F7 closed by the `CaseStore` extraction. The
descriptions below are kept as the grounding record — they are why the boundary exists, and several
of them were only findable by mapping the storage access the way this section did.

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

#### 4.1.6 Resolutions — settled by the `CaseStore` extraction

Two items that stood here — *where pool-activity-status durably lives* and *a `case_id`-keyed index* —
are closed by §4.1.2. Status is projected onto location and `CaseStore` owns both, so there is no
separate durable status store to design. The sidecar-vs-journal question evaporated with the deferral
model: because `set_status` refuses rather than defers, status and location never legally disagree, so
nothing needs to carry a status that location cannot express.

- **Convergence over atomicity — `IMPLEMENTED`, not yet written down.** Rather than demanding
  crash-atomicity from each pipeline — which the cache's move does not provide across the surrounding
  ticket unlink, and which `adopt`'s per-child `shutil.move` loop plainly does not — the contract is:
  **every relocation is idempotent and re-drivable, and startup readmission converges any
  disagreement.** `set_status` returns the existing folder when a case is already at the requested
  status; `create_location` returns an existing folder rather than failing; `absorb_orphan` tolerates
  source == destination. All three have tests. What is still missing is a single place that *states*
  the contract — that belongs with the Wave 3 documentation pass, not scattered across docstrings.
- **The quarantine ticket — `DONE`.** It follows the termination/eject shape, and both the eject and
  quarantine tickets are consulted before re-admitting. Two properties worth remembering: a held
  lease does not spend a retry (waiting is the designed behavior and can outlive many ticks), and
  enough *genuine* failures retire the ticket to `failed/` so a case that can never be relocated stops
  consuming a tick forever.
- **Departed cases are announced, not catalogued — `DONE`.** The manager does not become the
  librarian of departed cases. Departures emit lifecycle notices; an application that needs history
  subscribes and keeps its own record. This is the same refusal as §4.1.2's scope decision — a durable
  ledger would mean retention policy, sweeping, and a query API, all new manager responsibilities.
  - **Channel — `DECIDED 2026-08-01`.** One registry, widened to carry lifecycle kinds alongside
    problem kinds, with `kind.is_lifecycle` as the discriminator subscribers filter on. The public
    surface is renamed to match what it now publishes: `on_notice` / `off_notice`,
    `CaseEscalation` → `CaseNotice`, `CaseEscalationKind` → `CaseNoticeKind`,
    `EscalationRegistry` → `NoticeRegistry`, `escalation.py` → `notice.py`. `REAP_ANOMALY` became
    `READMIT_ANOMALY` with the rename of the pass that emits it.
  - **Delivery is at-most-once.** In-process pub/sub does not survive a crash: if a case departs and
    the process dies before a subscriber persists, the notice is lost. Acceptable — operator
    convenience, not correctness — and stated on the module and in the operator doc rather than left
    to be discovered.
  - **This fully resolved F4.** `done/` was load-bearing only because `ticket_exists()` used it for
    termination idempotency. Stored status is authoritative now, so `done/` is deleted outright rather
    than TTL'd: a case already at `terminated` needs no receipt to prove it.
- **`CaseKeepManifest` interaction — `OPEN` (small).** The store reads exactly one thing inside a case
  folder — `case_record.yaml`, and only on the reverse-address path — and `purge()` retains the
  record. That is the reasoning; it deserves a direct test rather than resting on the argument.

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

**KILLS THE FLEET — both fixed at the head of Wave 2**

- [x] `_live_or_evict` evicts on **any** rehydration failure, not just the three exception types it
      used to name. It runs mid-sweep, outside `_isolated_tick_item`, and repeats deterministically
      every beat — so one unreadable `case_record.yaml` used to exhaust `_LOOP_FAILURE_LIMIT` in three
      ticks and take the whole fleet down. Eviction is the fail-safe: slot released, pending fires
      rejected with the reason, `EVICTED` event emitted.
- [x] `_reconcile_terminal_in_pool()` isolates **per case** and `_detect_escalations()` runs as an
      isolated tick item. `begin_termination` is remove → detach → `write_ticket`; a ticket write that
      raises still strands that one case, but no longer aborts the pass for the others or spends the
      loop-failure budget.

**STALLS A CASE PERMANENTLY**

- [x] `eject_from_pool()` could hang forever — fixed in Wave 2. The waiter resolved only on success;
      hitting the retry cap retired the ticket to `failed/` with nothing left that could ever resolve
      it, so a caller passing `timeout=None` waited indefinitely. It now raises
      `EjectAbandonedError`, and an `EJECT_FAILED` notice is emitted alongside.
- [x] An unparseable ticket is no longer an infinite retry. `retry_count` lives inside the ticket, so
      one that will not parse can never record that it was tried — and the case it describes stays
      removed from the pool, detached, and un-enqueueable while the manager notices about it every
      tick. `TicketAttemptLedger` counts attempts **outside** the file; on exhaustion the ticket is
      retired to `failed/` and the case is quarantined, recoverable via `reopen_case()`. The
      `case_id` comes from the filename, which is the one field a corrupt ticket cannot take with it.
      Applies to all three ticket kinds.
- [x] Same trap via `verify_termination_peek` raising *before* `retry_count += 1` — closed by the
      same mechanism, since the ledger counts the whole item rather than the parse.
- [x] A failed `driver.add` during orphan re-admission released the lease `rehydrate()` took.
      Holding it made the orphan read as *owned* to every later pass, including the next restart's,
      so a case that failed to re-admit once was never looked at again.
- [ ] `OPEN` (design, not defect) — Orphans whose rehydration fails are still only retried on
      restart. `readmit_orphans()` has one caller by design (§4.1.2: mid-run divergence is ticket
      replay's job), so this is a deliberate boundary rather than an oversight — but with the lease
      leak fixed, a restart genuinely does revisit them. Revisit only if operators find the restart
      cadence too coarse.

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

**Where this stands.** `_isolated_tick_item` contains the *manager*, not the *case*: it converts
"loop death" into "this case is stalled and notices once per tick." That was the intended trade, and
it is now applied everywhere a per-case failure can reach the loop.

The other half — **the retry counter that would eventually give up on a bad item living inside the
very file that failed to parse** — is closed. Attempts are counted in `TicketAttemptLedger`, in
memory and keyed by path, which is enough on purpose: the failure is deterministic so the threshold
is reached within a few ticks of one process run, and the *action* on exhaustion is durable. A
durable counter would be another file that can itself corrupt, which is the trap being escaped.

What remains in this section is the `LOSES INFORMATION / NOISE` group — non-atomic YAML writes,
event-journal scans racing deletion, and the client's unguarded manifest read. None of them strand a
case; they degrade diagnosis.

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

- [x] **Bag-loading construction convenience** — `case_manager_support/bag_loading.py`.
      `load_case_bag()` provisions a root, copies each case in, and adopts it; `case_folders_in()`
      is the (deliberately shallow) discovery rule; `make_case_bag_fixture()` is the pytest factory,
      which stops every manager it created at teardown and gives each load its own root. The bag is
      **copied, never consumed** — adopt moves, and running a case mutates it, so adopting the
      caller's folders directly would destroy the input on first use and make a second run
      meaningless.
- [x] **Shape decided `2026-08-01`: runnable examples + a README**, mirroring the
      `cached_file_folders_support/examples/` convention. Neither a CLI nor a generator: both add
      public surface to teach a three-line idea, and a config-driven CLI would additionally need case
      types importable by dotted path. Built: `example_01_minimal_host` (no adapter — the supported
      embedded shape), `example_02_bag_runner` (batch, `stop_when_empty`), `example_03_request_serving_host`
      (adapter + client round trip), a shared `example_cases.py`, and `README-case_manager.md`.
      **The examples are executed by `tests/test_case_manager_examples.py`**, including a check that
      the README still lists every script — an example nobody runs is a claim about the API, not a
      fact about it.

### 4.4 Hands-on bench-testing pass — **done**

The empirical counterpart to §5's static inspection: run the thing, catalogue what breaks. Harness at
`volatile/tmp/bench_case_manager.py` (scratch, not committed) — five scenarios, all now clean.
Everything it found has a fix and a regression test.

| Scenario | Result |
|---|---|
| adopt → live → terminate, 60 cases | 5 ms/case to load, 0.6s to drain |
| SIGKILL a real host mid-run, then recover | all 25 cases accounted for and archived; **recover() takes ~29s** |
| eject while a step is in flight | works after the fix below (1.7s — it waits the step out) |
| reclassify while a step is in flight | correctly refused with `CaseInFlightError`, pool intact |
| 30 choked cases through Balanced vs Seniority | 0.77s each, no divergence |

**Found and fixed**

- [x] **`eject_from_pool()` could not eject a busy case.** `begin_eject` called `request_halt()` and
      then removed immediately, but the driver's contract says plainly: *wait for HALTED before
      remove() if an advance may be in progress*. So ejecting a case mid-step raised
      `CaseInFlightError` — and ejecting a *busy* case is the case an operator most often wants. The
      manager now awaits HALTED (subscribing before requesting, since an already-idle case fires it
      synchronously) and `begin_eject` documents that the case must arrive halted.
- [x] **A failed `begin_eject` leaked its waiter.** The future was registered before the call, so any
      raise left an entry in `_eject_waiters` that nothing could ever resolve.
- [x] **A case that terminated while we waited for its halt raised a bare `KeyError`.** Now
      `LiveCaseNotFoundError` — it left the pool on its own, which is not a failure.
- [x] **The health probe reported a recovering manager as dead.** This is the serious one. Recovery
      after a crash waits out the previous owner's heartbeat leases — a held lease is
      indistinguishable from a live owner's until you watch it for longer than one heartbeat period —
      and *nothing beats* meanwhile, because the loop has not started. The probe saw no heartbeat and
      returned 1. A Kubernetes livenessProbe would therefore kill the process **every single time**,
      restarting the wait from zero: a crashed fleet could never recover, and no amount of waiting
      would fix it. The manifest now carries `recovering_at`, and the probe reports 0 for up to 120s
      (the lease-reclaim wait is itself bounded at two TTLs), then 1 — generous, not infinite.

**Recorded, not a defect**

- The ~29s crash-recovery cost is correct and is a fixed cost, not proportional to fleet size. It is
  now in the operator doc along with what it means for `failureThreshold` and restart backoff.

## 5. Architecture / ergonomics inspection pass — per-class quirk checklist

Raw material surfaced while mapping the code, **not yet judged** — this is the starting checklist for
the "round of inspection to improve ergonomics and architecture" pass. Expect this list to grow as the
pass proceeds; it is known to be incomplete.

### `CaseManager` / `CaseManagerClient`
- [x] `IncompatibleReclassError` imported from one path (Wave 1).
- [x] `_log_startup_summary()` uses public `__len__` on both collaborators (Wave 1).
- [x] `_replay_*_pending()` only counted; neither replayed anything. Renamed to
      `_count_termination_pending()` / `_count_eject_pending()`.
- [x] `begin_eject`'s vestigial `wait_halted` branch removed (Wave 1).
- [ ] `reclassify_case`'s failure-path re-admission logic (nested try/except that re-raises the
      *original* exception regardless of whether the recovery attempt itself also failed) is dense
      and easy to misread — candidate for a readability pass.
- [ ] `OPEN` — `CaseLocation.in_pool` is still silently meaningless when produced by a
      `CaseManagerClient`'s own (unsynced) manager instance. `_preflight_reclassify` works around it
      by checking `status` instead, and the dataclass now documents `in_pool` as point-in-time, but
      nothing distinguishes a *trustworthy* `in_pool` from an untrustworthy one. A caller of
      `client.locate()` who naively trusts it still gets a wrong-but-plausible answer.
- [x] Both bucket iterators ask the store for a status now — no globs, no inconsistent sourcing, and
      `iter_quarantine()` returns real cases (F2). `iter_terminal(partition=…)` narrows to one
      archive partition.
- [x] `locate()` is one index lookup for a live case, in both addressing directions (F3).
      `locate_all(external_key=…)` is still a scan and says so in its docstring — external keys are
      not indexed, and that is the honest statement rather than a silent cost.
- [x] Unused `import shutil` removed (Wave 1).
- [x] `_ensure_namespace_dirs()` creates only the manager namespace (F5).
- [x] `done/` is gone from termination and eject; stored status carries idempotency (F4).
- [x] `CaseManager.serve()` removed (Wave 1) — a public method whose own docstring said not to call
      it. `case_manager_host.serve()` is the one way in.
- [ ] The `§N` comment convention (`§1`, `§2`, `§6`, `§8`, `§11`...) throughout both files references
      an external design doc not present anywhere in the repo — a reader loses the "why" behind
      several behaviors without it. Either locate/attach the referenced doc or replace with inline
      rationale.

### Pool drivers (`case_pool_driver.py`, `balanced_case_pool_driver.py`, `seniority_case_pool_driver.py`)
- [x] `SeniorityCasePoolDriver._make_slot` builds from `dataclasses.fields(base)` (Wave 1), so a new
      base-class field propagates automatically. `dataclasses.replace` does not work here — it
      reconstructs `type(base)` and cannot widen a `_Slot` into a `_SenioritySlot`.
- [x] The cross-file `_Slot` / `_TierPolicy` coupling named explicitly (Wave 1).
- [x] Duplicated always-true `isinstance(slot, _SenioritySlot)` guards in `_slot_prelaunch` /
      `_slot_post_step` removed (Wave 1).
- [x] `_order_chokeables` asserts its queue-order invariant instead of relying on an implicit chain
      (Wave 1).
- [x] `peek()`'s `queue_position` no longer materializes the key list (Wave 1). Still O(N) in the
      worst case — an allocation-free early exit, not an index.
- [x] The seniority scope boundary — contested scheduling capacity, *not* shutdown drain — stated in
      the class docstring (Wave 1).
- [x] `LeaseReclaimTimings.deadline_margin_secs` removed (Wave 1) — declared, never referenced.

### `case_manager_support/*`
- [x] The subdir constants are wired to their actual usages (Wave 1); `ABERRANT_META_SUBDIR` is gone
      with F6 and `QUARANTINE_SUBDIR` joined them in Wave 2.
- [x] `mailbox/__init__.py` re-exports the package's public surface (Wave 3).
- [x] `poll_result()` reads a `kind` discriminator that every result type carries (Wave 3). The
      substring sniffing and its 3-deep fallback cascade are gone.
- [x] `recover.py`'s dead notice-kind import removed (Wave 1).
- [x] `eject.py`'s vestigial `wait_halted` branch removed (Wave 1).
- [x] `termination.py`'s `process_pending_ticket` genuinely awaits now — path resolution, the status
      change, and the quarantine fallback are all async through the store.

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

### Wave 2 — Extract `CaseStore` — **complete**

Inward structural change; the public method surface is unchanged in shape, though several signatures
and vocabulary names changed with it (an unreleased library, so a clean break rather than aliases).

**Landed at the head of the wave** — §4.1.2 sequences these first so the code being moved has a
stable failure contract before it moves.

- [x] §4.2 — `_live_or_evict` evicts on any rehydration failure.
- [x] §4.2 — `_reconcile_terminal_in_pool` isolates per case; `_detect_escalations` is an isolated
      tick item.

**The boundary**

- [x] §4.1.2 — `LocalCaseStore` (concrete class, no ABC) in `case_manager_support/case_store.py`.
      The vocabulary rule is enforced by a source-inspection test, not just stated: no
      `CachedFileFolders`, `ref_path`, `grouping_key`, slave dir, or placeholder appears anywhere in
      the package outside the store, its constants, and the persisted policy that declares them.
- [x] §4.1.2 — `CaseManager(config, store=…)`; `CachedFileFolders` and `cache_override` are gone from
      `case_manager.py`. `open()` / `attach()` / `provision()` survive as conveniences.
- [x] §4.1.2 — `CaseManager.provision_local_case_store(root_dir)` delegating to
      `LocalCaseStore.provision()`, idempotent with validation.
- [x] §4.1.2 — recovery takes its working set from `store.iter_by_status(LIVE)` — one scan, not two.
- [x] §4.1.2 — `set_status(case_id, status, *, partition=None, force_despite_lease=False)`: async,
      refuses on a held lease, idempotent, off-loop. It is the single enforcement point for S-4.
- [x] §4.1.2 — addressing: `find()` / `resolve_path()` / `case_id_at()`, point-in-time `CaseEntry`,
      snapshot iterators, stale-path rule stated on the dataclass.
- [x] §4.1.2 — orphan-recovery collapse to the single re-admit rule, plus the pool-holds-non-live
      assertion. Deletes the second archive-label computation **(completes F7)**.
- [x] §4.1.2 — `reap()` → `readmit_orphans()`, `ReapReport` → `OrphanReadmitReport`,
      `reap.py` → `readmit.py`.
- [x] §4.0.4 — quarantine is stop-driving now, reason on the case's own journal, relocation deferred
      until the lease lapses. `aberrant.py` → `quarantine.py`.
- [x] §4.1.6 — quarantine ticket following the termination/eject shape; recovery consults it (with
      the eject ticket) before re-admitting. Waiting out a lease never spends a retry.
- [x] §4.1.6 — lifecycle notices for departures: `CASE_TERMINATED`, `CASE_QUARANTINED`,
      `CASE_EJECTED`, filterable via `kind.is_lifecycle`. Surface renamed to
      `on_notice` / `off_notice`; `CaseEscalation*` → `CaseNotice*`; `escalation.py` → `notice.py`.
      At-most-once delivery is documented on the module and in the operator doc.
- [x] §4.1.6 / F4 — `done/` is gone from termination and eject. Stored status is the receipt, so no
      per-case file accumulates and nothing needs sweeping.
- [x] F2 — `iter_quarantine()` reads the `quarantined` status and is the real API;
      `iter_aberrant()` is gone.
- [x] F3 — `locate()` is one index lookup for a live case, both directions. `locate_all()` is still a
      full scan and now says so — external keys are not indexed.
- [x] F5 — `_ensure_namespace_dirs()` creates only the manager namespace.
- [x] §5 — `CaseLocation` carries `status` instead of `grouping_key` and documents both mutable
      fields as point-in-time.
- [x] §2 — `case_store` unit tests (23), plus quarantine, orphan-readmit, lifecycle-notice, and
      loop-isolation suites.
- [x] §0 — no module outside the store touches a bucket name, ref template, or grouping pattern. The
      client manifest no longer publishes `live_bucket` / `terminal_prefix` either: publishing the
      layout invites out-of-process path arithmetic into managed storage.

**Fixed in passing** (found while moving the code; each has a regression test)

- [x] `eject_from_pool()` could hang forever — the waiter resolved only on success, and hitting the
      retry cap retired the ticket with nothing left to resolve it. It now fails with
      `EjectAbandonedError`.
- [x] Export moved the case folder away and then asked the cache to delete an entry whose storage
      was gone. The resulting error was swallowed by the retry path, so the operation "worked" only
      by accident on a later tick.
- [x] Orphan recovery skipped cases with **no** lease file — the state a cleanly released case is
      left in, and precisely the orphans it exists to rescue. It now applies the same tri-state rule
      as every relocation: only a *held* lease blocks.

**Deferred out of Wave 2, with reasons**

- [ ] §4.1.6 — convergence contract (idempotent, re-drivable, ticket-replayed) is now true of every
      relocation and is asserted by tests, but is not yet written down as a contract in one place.
      Belongs with the Wave 3 documentation pass.
- [ ] §4.1.6 — confirm nothing `CaseStore` relies on inside a case folder is purgeable. The store
      reads only `case_record.yaml` (via `read_case_id_from_folder`, and only on the reverse-address
      path); `CaseKeepManifest.purge()` retains the record. Worth a direct test rather than the
      reasoning alone.

### Wave 3 — Extract the protocols, then make it runnable and documented

Outward structural change, then the tooling and documentation that can only be written truthfully
once the shape is final.

**The extraction — done**

- [x] §4.0.2 — signaling adapter extracted (`case_manager_support/signaling_adapter.py`): fire /
      adopt / reclassify, correlation ids, result publication, dead-lettering, replay. The transport
      itself split out separately (`mailbox/transport.py`) as the *writer* half, so a submitting
      client needs no manager and no scheduling layer at all.
- [x] §4.0.2 — the dependency is inverted for real. `MailboxProcessor(self)` was handed the whole
      manager and reached into its internals; the adapter holds a manager and calls only public
      methods. `CaseManager.attach_fire()` is public because of it, and a test boobytraps
      `manager._driver` to prove the adapter never reaches past the boundary.
- [x] §4.0.2 — two-party recovery. `AdapterRecoverReport` carries what the transport found;
      `RecoverReport` carries what the fleet found; `serve()` sequences fleet-then-adapter, because a
      dead-lettered fire has to name a case the manager has already accounted for.
- [x] §4.0.2 — `is_idle` is the manager's pool alone. The host composes it with `adapter.is_idle`
      for `stop_when_empty`, since neither half can answer the whole question.
- [x] §4.0.2 — `adopt_case()` sheds `correlation_id`. De-duplicating a re-delivered request is
      transport business; the fleet has no opinion about client retries.
- [x] §1 — shutdown moved to the host, superseding Wave 1's interim hoist. It is polled
      unconditionally, so it survives no-adapter, `enable_mailbox=False`, and `watchdog_enabled=False`
      alike. The manager has no shutdown surface left to misuse.
- [x] §4.2 — per-request isolation. One malformed or exploding request produces an error result for
      *that* correlation id and the drain continues; silence would leave submitters unable to tell
      "still queued" from "dropped". **This closes the gap knowingly shipped at the Wave 1 merge.**
- [x] §1 **(split)** — `mailbox_neglect` now reads an injected backlog-age callable. With no adapter
      the check is structurally absent rather than flag-disabled: nothing owns a backlog, so nothing
      can neglect one.
- [x] §5 — every result type carries a `kind`, and `poll_result()` reads it. The old substring
      sniffing is gone, along with its 3-deep fallback cascade; a test publishes a result whose
      folder name contains every sniffed substring and asserts it is still identified correctly.
- [x] §5 — `mailbox/__init__.py` re-exports the package's public surface.

**Remaining**

- [x] §4.3 — bag-loading convenience (`bag_loading.py`) plus the pytest fixture factory.
- [x] §4.3 — shape decided and built: runnable examples + README under
      `case_manager_support/examples/`, exercised by their own test module.
- [x] §4.4 — full bench pass on the tooled harness: five scenarios, four defects fixed (eject of a
      busy case, a leaked eject waiter, a bare `KeyError` on a mid-halt departure, and the health
      probe calling a recovering manager dead). Findings recorded in §4.4 itself.
- [ ] §3 — `case-designer` skill updated for the pool-manager layer.
- [ ] §5 — replace the `§N` comment convention with inline rationale.
- [ ] §0 — confirm every class in the key-class list meets the finalize bar.
