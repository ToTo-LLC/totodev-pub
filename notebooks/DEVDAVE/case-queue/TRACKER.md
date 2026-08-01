# CaseManager / CasePoolDriver — remaining open items

The design/implementation work tracked in this file across Waves 1–3 (storage extraction,
signaling-adapter extraction, tooling, bench pass, atomic writes, docs sweep) is complete and merged
into `explore/case-queue`. That history now lives in the branch's commit log, not here.

**What follows is only what is still unresolved.** Every item below is up for debate on whether we
proceed with it at all — none of them block the branch from being mergeable, and several are
explicitly framed as "fix it" vs. "leave it" rather than pre-decided work.

---

## Open items

### 1. Orphan re-admission is restart-only, by design — is that still right?

`readmit_orphans()` has exactly one caller (`recover_manager()`), so a live case whose rehydration
fails during recovery is not retried until the next restart. That was a deliberate boundary, not an
oversight: mid-run divergence is ticket replay's job, and orphan re-admission is startup-only per the
`CaseStore` design.

Since then, the lease-leak bug that would have made a restart *not* actually revisit a failed orphan
was fixed — so a restart now genuinely reaches it. The open question is whether restart-only cadence
is still acceptable, or whether operators want something that revisits failed orphans without a full
process restart.

**Debate:** leave as-is (matches the original design boundary) vs. add a lighter-weight in-process
retry for orphans that failed rehydration during recovery.

### 2. Event-journal scans race deletion

Three call sites (`reap`-equivalent code, `verify_termination_peek`, and the fleet board) do
`.exists()` then `iterdir()`, or a bare `stat()` on a path obtained from an earlier `iterdir()` call.
A concurrent deletion between the check and the use can raise where the caller did not expect it.

**Debate:** worth hardening (wrap in try/except for the specific race, or restructure to avoid the
TOCTOU shape) vs. low enough probability/impact to leave alone.

### 3. `restore_pool_from_journal`'s classifier misses binding errors

One bad folder aborts the whole recovery pass with no partial `RebuildReport` — the operator loses
the record of what *was* successfully recovered, and every path after the bad one in iteration order
is silently skipped rather than reported.

**Debate:** worth fixing so recovery degrades gracefully per-folder (consistent with how the rest of
the fleet now treats per-case failures) vs. accept that a malformed pool-membership journal is rare
enough not to warrant it.

### 4. `CaseManagerClient`'s manifest read is unlocked and unguarded

A client reading the manifest concurrently with a write can get a raw pydantic `ValidationError`
instead of a clean "not fresh yet" signal, on every `only_if_fresh` API. This is meaningfully less
dangerous now that manifest writes are atomic (a client will see a whole old or whole new file,
never a torn one) — but a client polling at exactly the wrong moment can still observe a
structurally different manifest schema mid-transition and get an unhandled parse error rather than a
typed `ManagerNotFreshError`.

**Debate:** worth wrapping the read in a try/except that maps parse failures to the existing
not-fresh error type vs. leave it, given atomic writes already closed the worse failure mode.

### 5. `reclassify_case`'s failure-path re-admission logic is dense

The nested try/except that re-raises the *original* exception regardless of whether the recovery
attempt itself also failed is correct but hard to read at a glance. Flagged purely as a readability
concern, not a correctness one.

**Debate:** worth a dedicated refactor/readability pass vs. leave it — it works, and touching
exception-reraise logic for cosmetic reasons carries its own small risk.

### 6. `CaseLocation.in_pool` is still silently meaningless from a `CaseManagerClient`

A `CaseManagerClient` runs its own unsynced manager instance, so `in_pool` on a `CaseLocation` it
produces never reflects the real fleet's pool membership. `_preflight_reclassify` already works
around this by checking `status` instead, and the dataclass documents `in_pool` as a point-in-time
value — but nothing on the object itself distinguishes a *trustworthy* `in_pool` (from the real
manager) from an *untrustworthy* one (from a client). A caller of `client.locate()` who naively
trusts `.in_pool` gets a wrong-but-plausible answer.

**Debate:** worth adding an explicit marker/separate field so a client-produced `CaseLocation` can't
be mistaken for a manager-produced one vs. rely on documentation and the existing `_preflight_*`
workaround pattern.

### 7. The convergence contract is true but not written down in one place

"Every relocation is idempotent and re-drivable, and startup readmission converges any
disagreement" is now actually true of every relocation path and is exercised by tests — but there is
no single doc/docstring that states it as a contract. Originally slated for a "Wave 3 documentation
pass" that didn't end up producing this specific writeup.

**Debate:** worth writing a short, explicit statement of the contract (and where) vs. treat the
test coverage itself as sufficient documentation of the guarantee.

### 8. "Nothing `CaseStore` relies on inside a case folder is purgeable" is argued, not tested

The store reads exactly one thing inside a case folder — `case_record.yaml`, via
`read_case_id_from_folder`, only on the reverse-address path — and `CaseKeepManifest.purge()`
retains the record. The reasoning holds up, but there is no direct test pinning "the store still
resolves the case correctly after a purge."

**Debate:** worth writing that direct test vs. accept the reasoning as sufficient given how narrow
the store's in-folder read surface is.
