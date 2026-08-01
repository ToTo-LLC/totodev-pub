# Designing a case that a CaseManager will drive

Most cases end up run by a fleet coordinator rather than by hand. That does not
change how a case type is *designed*, but a handful of design choices decide how
it *behaves* once a manager owns it — and those choices are made in the FSM, not
in the manager's configuration.

This is the case author's view. For hosting, see
`src/totodev_pub/case_manager_support/examples/README-case_manager.md` and
`docs/case-manager-deployment.md`.

## The layer above your case

Three things sit between a case folder and a running process:

| Layer | What it does to your case |
|---|---|
| **`CaseManager`** | Owns storage and the pool. Adopts, drives, archives, quarantines. |
| **`CasePoolDriver`** | Decides *when* your case gets a turn: tiers, chokes, concurrency. |
| **Signaling adapter** | Optional. Lets other processes fire triggers at your case by file drop. |

A case type never imports any of them. Everything below is about what the FSM
declaration implies once one is present.

## What your FSM declaration decides

**`--` edges auto-fire; `==` edges wait.** This is the single biggest choice.
A case whose path to terminal is all `--` self-completes: adopt it and the pool
walks it to the end unaided. A case with a `==` edge stops there until something
outside fires that trigger. Both are legitimate; a state that *looks* automatic
but is reached only by a manual edge is the common design error, and a case that
sits in the pool forever is what it looks like in production.

**A terminal state means the manager may archive the folder.** Reaching `[*]`
tells the manager the case is finished: it leaves the pool and its folder is
relocated to a dated archive bucket. Anything a downstream reader still needs
must be in the folder before that, not fetched afterwards.

**`fsm_trigger_chokes` is your admission control.** A trigger that names a choke
resource competes fleet-wide for a limited number of permits, so declaring one is
how a case type says "this step talks to something that will not tolerate 50
concurrent callers." Under-declaring produces a fleet that overwhelms a
dependency; over-declaring produces one that idles.

**Slow steps are fine; blocking ones are not.** `perform_*` hooks run on the
manager's event loop. A hook that blocks stalls *every* case in the pool, not
just this one — use `await`, and push genuinely blocking work to a thread.

## Things that happen to your case without you asking

**The heartbeat lease.** While the manager drives a case it holds a lease on the
folder, renewed as it works. Nothing else may relocate a leased folder — which is
why a case handed *to* a manager (adopt, or a bag of prepared cases) must be
detached first. `create_case_in_folder()` returns an attached case; call
`case_detach()` before handing it over.

**The keep manifest and the redundant purge.** Some time after a case reaches a
terminal or quarantined state, the manager purges files inside the folder that no
keep rule protects. If your case writes something that must outlive it, declare
it — see the retention pattern in `case_design_patterns.md`. The case record
itself is always retained.

**Quarantine.** A case the manager cannot drive — an unreadable record, a
transition that fails past its retry budget — is moved to a quarantined status
with a `MANAGER_QUARANTINED` event written to its own journal explaining why. It
stops being driven; it is not deleted, and `reopen_case()` brings it back. A case
type that can fail in an interesting way is easier to diagnose if its failure
path writes something to the journal too.

**`archive_grouping_label()`.** Decides which dated bucket a finished case lands
in — by default the month it closed. Override it if your domain partitions
archives differently (tenant, fiscal period), and the manager will honor it.

## Questions worth asking the developer

Raise these only when they plausibly apply, one at a time, the same way as the
Step 7 patterns:

- Should this case run to completion on its own, or wait for an outside signal
  at some point? (auto vs manual edges)
- Does any step call something with a concurrency limit? (chokes)
- After this case closes, what still has to be readable in its folder a month
  later? (keep rules)
- Is there a natural partition for the archive other than close month?
