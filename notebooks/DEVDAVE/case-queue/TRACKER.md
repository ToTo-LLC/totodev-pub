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
public API, cleared of the quirks in §5, adequately tested (§2), and — for the two marked
`ABSTRACTION PENDING` — resolved against the §4.1 storage-decoupling decision before their contracts
are considered stable, since that decision changes what they're allowed to assume.

**Scheduling layer** (`folder_backed_case_support/`):

- `CasePoolDriver` (ABC, `case_pool_driver.py`) — the scheduling contract every driver must honor.
  Stable already; mainly needs its `§N` design-doc references resolved (§3) and, if §4.1 lands on a
  new tier-transition event, a contract update here first since it's the abstract surface.
- `BalancedCasePoolDriver` (`balanced_case_pool_driver.py`) — the default concrete driver (MLFQ
  tiering, choke governance). Quirk list in §5; candidate site for the tier-transition event from
  §4.1 if that's the chosen hook point.
- `SeniorityCasePoolDriver` (`seniority_case_pool_driver.py`) — FIFO/seniority variant. Has the most
  outstanding ergonomic debt of the three (manual `_Slot` field duplication, reach into base-module
  private names — §5) and its shutdown-drain scope boundary is undocumented.
- `PoolMembershipJournal` (`pool_membership_journal.py`) — crash-recovery observer. Smallest quirk
  list (one dead field, §5); mostly just needs the `deadline_margin_secs` question resolved.

**Fleet management layer** (`case_manager.py`, `case_manager_client.py`, `case_manager_support/*`):

- `CaseManager` (`case_manager.py`) — **the central class.** Largest quirk list in §5; also the
  primary integration point if §4.1's storage abstraction goes forward (`ABSTRACTION PENDING` —
  its adopt/eject/reclassify/reopen methods are exactly the ones that assume `CachedFileFolders`
  move semantics today).
- `CaseManagerClient` (`case_manager_client.py`) — out-of-process façade. Smaller surface; main open
  item is the `CaseLocation.in_pool`-unreliable-from-client issue (§5).
- `MailboxProcessor` (`case_manager_support/mailbox/processor.py`) — fire/adopt/reclassify/shutdown
  request-response protocol. Needs the result-type-sniffing fragility addressed (§5) and per-request
  isolation audited (§4.2).
- `ManagerWatchdog` (`case_manager_support/watchdog.py`) — wedge detection + kill ladder. Functionally
  complete; blocked on the promotion-blocker test gaps in §1 (the exit-70 integration test above all).
- `CaseManagerPolicy` (`case_manager_support/case_manager_policy.py`) — `ABSTRACTION PENDING`. Currently
  the single source of truth for layout/addressing; §4.1 may split it (data that stays here vs. logic
  that moves into a new storage-backend interface).
- `CaseManagerConfig` (`case_manager_support/case_manager_config.py`) — process-local wiring
  (driver/registry/cache overrides). No dedicated test yet (§2); likely gains a storage-backend field
  if §4.1 lands.
- `FleetStatusBoardWriter` / `FleetStatusBoardWatcher` (`fleet_status.py` / `fleet_status_watcher.py`)
  — fleet snapshot writer + client-side watcher. No open quirks beyond the private-symbol imports
  noted in the original mapping; lowest-risk pair in this list.
- `EscalationRegistry` (`escalation.py`) — in-process pub/sub for escalations. No open items beyond
  what's already covered by its existing test file.

Everything else in `case_manager_support/` (`adopt.py`, `termination.py`, `aberrant.py`, `eject.py`,
`reap.py`, `purge.py`, `recover.py`, `shutdown.py`, `staging.py`, `layout.py`,
`case_manager_host.serve()`) is function-based pipeline code rather than a class with its own
lifecycle — tracked by module in §2/§3/§5 rather than listed here, but `layout.py` and the four
move-shaped pipelines (`adopt`/`termination`/`aberrant`/`eject`) are exactly what §4.1's storage
abstraction would need to reshape, so treat them as riding on that decision too.

---

## 1. Promotion blockers (from `_backlog/finishing_watchdog.md`)

That doc's own header says these should be resolved before promoting to `main`. Carried forward
here so they're tracked in one place instead of two.

- [ ] `OPEN` — No integration test drives 3 consecutive `_manager_loop` tick failures through a real
      `serve()` run to assert the process actually exits with code 70. Flagged in the source doc as
      the highest-risk gap — everything else is unit-tested in isolation only.
- [ ] `DECISION NEEDED` — With `enable_mailbox=False` **and** `watchdog_enabled=False`, the shutdown
      mailbox is completely dead (nothing polls it). Two options on the table: (A) hoist the
      shutdown-mailbox check above the `enable_mailbox`/`watchdog_enabled` gates so it's always live,
      or (B) leave it coupled and document the limitation clearly. Needs a call.
- [ ] `DECISION NEEDED` — Whether to implement a `_loop_failure_no_watchdog` fail-loud fallback for
      deployments that run with `watchdog_enabled=False`. Doc-only hardening landed 2026-07-16; the
      actual behavior is still undecided.
- [ ] `OPEN` — No test coverage for: `mailbox_neglect` detection, `tick_slow` alarm-only path,
      stop-timeout → `EXIT_WATCHDOG` path. All three are implemented in `watchdog.py` but unverified.
- [ ] `OPEN` — Operator-facing deployment doc (how to actually run this thing in prod — restart
      policy, exit code meanings, Docker/k8s caveats already called out in code comments) is
      unwritten. The `submit_shutdown()` docstring in `case_manager_client.py` has good raw material.

## 2. Test coverage gaps (found independently, not in finishing_watchdog.md)

- [ ] `OPEN` — `aberrant.py` has **zero direct test coverage**. `AberrantSidecar` and
      `move_case_to_aberrant()` are only reached as a side effect deep inside adopt-failure /
      termination-failure paths, and none of the existing adopt/termination tests exercise the
      failure branch. Both code paths inside `move_case_to_aberrant` (cache-hit vs.
      copytree-from-disk fallback) are unverified.
- [ ] `OPEN` — `purge.py` has **zero test coverage**. `run_redundant_purge` silently deletes files
      based on retention age; nothing currently verifies it does the right thing, including in the
      face of a mid-purge error.
- [ ] `DECISION NEEDED` — `PurgeReport.stragglers` is declared but never populated anywhere in
      `purge.py`. Either finish wiring it (what should populate it — files that were eligible but
      couldn't be removed?) or drop the field.
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

### 4.1 Decouple `CaseManager` from `CachedFileFolders` via a storage abstraction — `DECISION NEEDED` / design work

**Revised framing (supersedes the earlier, narrower "pluggable addressing scheme" note below the
line).** The maintainer's actual goal is bigger than swapping grouping-key *functions*: `CaseManager`
should depend on a storage **abstraction/interface**, with `CachedFileFolders` demoted to being just
the default implementation of it — not a hardcoded dependency. Two concrete alternate policies were
raised that the current design cannot express at all, which is what motivates the abstraction (not
just parametrization):

1. **Files never move.** Cases are worked in place, run to completion, and finalized, but never
   physically relocated — no move-on-terminal, no move-on-aberrant. This is a real behavior change,
   not a config knob: today `termination.py` calls `cache.move_file(...)` directly
   (`termination.py:187`) and `aberrant.py`/`eject.py` do the analogous thing for their own cases —
   physical relocation is baked into the pipeline logic itself, not mediated through anything
   override-able. Under this policy, only the owning application decides if/when/how case folders are
   renamed or archived; `CaseManager` itself would need to track live/terminal/aberrant status some
   other way (a status field? a marker file alongside the case, left in place?) instead of via
   directory membership.
2. **Case files as checked-out/checked-in networked resources.** A case's files live on a remote
   resource fetched via an external API (checkout) and committed back via another API call (checkin),
   rather than always resident on local disk. The maintainer specifically wants a **"reflexive commit
   when the case grows cold in the driver pool"** — i.e., checkin triggered by pool *tier* state, not
   just by termination/eject.
   - **Concrete gap found while grounding this:** there is no hook for this today. Tier state
     (`HOT`/`WARM`/`COLD`) is internal to `BalancedCasePoolDriver`'s `_Slot`
     (`balanced_case_pool_driver.py:72-77`, transitions at `_TierPolicy.reclassify`, lines ~153-159)
     and is only exposed for read-only diagnostics via `peek()`/`by_tier()`. `CasePoolEventNames`
     (`case_pool_driver.py:143-151`) has no tier-transition event — only `ADMITTED`, `HALTED`,
     `REMOVED`, `EVICTED`, `ALERTED`, `ADVANCED`, `TERMINATED`, `FAILED`. So a storage layer wanting to
     react to "this case just went COLD" has nothing to subscribe to; it would need either (a) a new
     event (e.g. `TIER_CHANGED` or specifically `WENT_COLD`) added to the pool driver's public event
     surface, or (b) `CaseManager` polling `driver.by_tier()`/`peek()` itself each maintenance tick and
     driving checkin logic externally. This is a real design fork, not just plumbing.

**What a storage abstraction would need to cover**, based on everywhere `CaseManager`/
`case_manager_support` currently reach directly into `CachedFileFolders` or hardcode a move:

- Addressing/identity: mapping a `case_id` to a location (today: `layout.py`'s grouping-key + ref-path
  functions).
- Discovery/iteration: enumerating cases in a bucket (today: `cache.files()`/`cache.groupings()` via
  `iter_all_managed_folders`/`iter_case_folders_in_grouping`).
- State-transition operations, currently expressed as physical moves: adopt-in (`adopt.py`,
  `cache.upsert_file`/`get_slave_dir`), terminate (`termination.py`, `cache.move_file`), move-to-aberrant
  (`aberrant.py`), eject-out (`eject.py`, out of managed space entirely), reopen (`case_manager.py`,
  terminal/aberrant back to live). Each of these needs to become an abstracted operation whose
  *default* (CachedFileFolders-backed) implementation does today's physical move, but whose in-place
  or checkout/checkin implementations can legitimately do nothing, or trigger a remote commit, instead.
- Staging (`staging.py`) — allocation of scratch space during adopt/construction; may or may not make
  sense under a "no local disk" networked-resource policy.
- Physical file access primitives used pervasively by `FolderBackedCase` itself for records/events/
  assets/leases — not part of this package, but worth confirming during design whether the abstraction
  needs to reach that deep or can stop at the `CaseManager`/`case_manager_support` boundary.

**Open questions to resolve before implementing** (this needs a short design note of its own, given
the scope — touches `layout.py`, `adopt.py`, `termination.py`, `aberrant.py`, `eject.py`, `reap.py`,
`purge.py`, `staging.py`, and potentially the `CasePoolDriver` event surface):

- Where does the abstraction boundary sit — between `CaseManager` and `CachedFileFolders` only, or
  does it need to reach into `FolderBackedCase`'s own file access too?
- Does the tier-hook problem (checkin-on-cold) get solved by extending `CasePoolEventNames`, or by a
  polling seam in `CaseManager`'s maintenance tick? This choice affects whether `CasePoolDriver`
  implementations (both `Balanced` and `Seniority`) need to change.
- Is a full `Protocol`/ABC the right shape (e.g. a `CaseStorageBackend` with methods like
  `admit`/`finalize`/`quarantine`/`export`/`reopen`/`iter_bucket`), with the in-place and
  checkout/checkin policies as two more concrete implementations alongside a `CachedFileFolders`-backed
  default? That's the leading candidate shape given the two concrete policies already in hand, but
  should be confirmed against a third hypothetical policy before locking the interface, to avoid
  designing to just the two examples seen so far.

### 4.2 Error / edge-case handling within a running pool — `OPEN`, needs an audit pass

Initial per-case-step isolation already exists and looks reasonably solid: `_run_case_step` in
`balanced_case_pool_driver.py` catches `BaseException` per in-flight task (line ~621), clears the
slot's in-flight state, and re-raises only to the task's own awaiter — one case failing to advance
does not crash the sweep or take down other cases. `_manager_loop` also has a coarse safety net (3
consecutive tick failures → `on_loop_failure` callback or re-raise, `case_manager.py:390-424`).

That said, coverage is uneven and was not exhaustively audited — flagging specific soft spots found
so far, and leaving room for more to turn up:

- [ ] `OPEN` — `CaseManager._maintenance_tick()` (`case_manager.py:450-480`) has **no per-item
      isolation** around its termination-ticket loop, eject-ticket loop, mailbox drain, or purge call.
      A single malformed/poison termination ticket raises out of the `for ticket_file in
      replay_pending(...)` loop and aborts the *entire* tick — meaning mailbox drain, purge, and fleet
      board publish for that whole tick silently don't run either, and if the same bad ticket recurs
      every tick, it can burn through the 3-consecutive-failure budget and kill the whole manager loop
      over one non-transient bad ticket rather than just quarantining that ticket. Worth deciding: should
      each ticket/queue in `_maintenance_tick` be wrapped so one bad item degrades gracefully (e.g. logs
      + escalates + skips) instead of aborting the tick?
- [ ] `OPEN` — Audit `MailboxProcessor`'s per-request-type processing (`_process_fire_intake`,
      `_process_reclassify_intake`, `_process_adopt_intake` in `mailbox/processor.py`) for the same
      question: does one malformed mailbox request file poison the whole intake batch, or is each
      request isolated?
- [ ] `OPEN` — Audit what happens when a case's `FolderBackedCase` rehydration itself raises (corrupt
      `case_record.yaml`, missing asset, etc.) during a sweep, vs. during `fire()`, vs. during adopt —
      confirm the failure surfaces as an escalation/aberrant-move rather than silently stalling that
      case's slot forever.
- [ ] `OPEN` — More generally: walk every place the pool driver or manager touches the filesystem
      (lease files, event logs, record files) mid-sweep and ask "what happens if this file is
      truncated / concurrently modified / deleted out from under us right now" — this hasn't had a
      dedicated adversarial pass yet, only the failure modes anticipated by the original author.

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
- [ ] `_replay_termination_pending()` / `_replay_eject_pending()` appear to be dead code — no call
      site found; `_maintenance_tick()` already inlines equivalent logic separately. Confirm dead and
      remove, or find the missing call site.
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
      sourcing between the two "special bucket" iterators.
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
      `ABERRANT_META_SUBDIR`, but `termination.py`/`eject.py`/`aberrant.py` all hardcode their own
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
