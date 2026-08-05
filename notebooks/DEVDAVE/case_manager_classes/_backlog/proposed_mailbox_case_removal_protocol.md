# Proposed: Mailbox protocol for removing cases from the manager

- Proposed 2026-07-08
- Status: **for discussion — do not implement yet.** We agreed this needs more
  exploration; this write-up captures the design space so the discussion has a starting
  point.

## Motivation

The mailbox now covers three request kinds — `FireRequest`, `AdoptRequest`, and
`ReclassifyRequest` — which lets an out-of-process tier (typically the web UI) push work
*into* the fleet and steer cases *within* it. There is no mailbox path for taking a case
*out* of the fleet. Today removal is in-process only:

- `CaseManager.eject_from_pool(case_id, export_to_folder=...)` — the §5.11 machinery:
  halt the slot, detach the case, write a crash-safe `EjectTicket`, export the folder out
  of managed filespace, drop the cache ref. Callable only from code running inside the
  manager process.
- Natural termination — a case reaching a terminal state is archived by policy. This is
  the *intended* exit for nearly all cases and needs no new protocol.

Real deployments occasionally need remote removal anyway:

- **Operator cleanup.** A case was adopted by mistake (wrong folder, duplicate submit,
  test data in a production pool) and the UI tier is where the operator notices.
- **Handoff to another system.** A case must leave this manager's custody mid-lifecycle —
  e.g. escalated to a different processing cluster or exported for offline forensics.
- **Compliance pull.** Confidential material must be expunged from managed filespace on
  request, ahead of any retention policy the archive would apply.

The current workaround is to run removal code in the manager process (a companion loop
watching some ad-hoc signal), which is exactly the kind of one-off glue this family of
classes exists to eliminate.

## Why this is harder than fire/reclassify (and why we are deferring)

The three existing request kinds share a safety property: **the case never leaves managed
filespace.** A malformed or malicious request can at worst fail a trigger or produce an
error result. Removal is categorically different:

1. **It moves data out of the managed root.** `eject_from_pool` takes an
   `export_to_folder`. Accepting a filesystem destination from a *file another process
   wrote* is a path-traversal / exfiltration surface. Any remote protocol must confine
   destinations (e.g. to pre-declared export roots in policy) rather than accept
   arbitrary paths.
2. **It is destructive and hard to undo.** A bad fire is retried; a bad reclassify is
   reclassified back (the shared state still exists). A bad removal may require manual
   re-adoption, and a bad *delete* is unrecoverable.
3. **It interacts with halt semantics.** Ejection must first halt the slot and wait for
   any in-flight step. In-process, `eject_from_pool` awaits a future with a timeout
   (`EjectTimeoutError`). Over the mailbox, "waiting" spans maintenance ticks and the
   requester only sees a result file — the protocol needs an explicit long-running
   request state (compare the adopt mailbox's `pending/`), not just intake→result.
4. **The intent is ambiguous.** "Remove" can mean at least three things, with different
   risk profiles (see Open Questions): eject-with-export, discard-to-aberrant, or
   hard-delete.

None of these are unsolvable; they just deserve deliberate answers rather than a pattern
copied from `FireRequest`.

## Sketch of the proposed protocol

Follow the established mailbox shape so client code stays uniform:

```python
class RemoveRequest(BaseModel, FileMappedPydanticMixin):
    protocol_version: int = MAILBOX_PROTOCOL_VERSION
    correlation_id: str
    requested_at: str
    case_id: str | None = None          # exactly one of case_id / case_folder
    case_folder: str | None = None
    mode: Literal["eject"] = "eject"    # v1: eject only (see Open Questions)
    export_root_alias: str = ""         # names a policy-declared export root; never a raw path
    reason: str = ""                    # audit trail; recorded in the case events before detach

class RemoveResult(BaseModel, FileMappedPydanticMixin):
    kind: Literal["remove"] = "remove"  # poll_result discriminator, like ReclassifyResult
    status: Literal["completed", "error", "in_progress"]
    correlation_id: str
    case_id: str | None = None
    export_folder: str | None = None
    error: str | None = None
    completed_at: str
```

Manager-side flow per maintenance tick, reusing the eject machinery rather than inventing
new removal semantics:

1. `intake/` → validate → `executing/<case_id>/` (malformed → `malformed/`, as elsewhere).
2. Resolve `export_root_alias` against a new **Tier-2 policy map**
   `remove_export_roots: dict[str, str]` (alias → absolute path). Unknown alias → error
   result. This is the containment answer to risk #1: the manager's operator, not the
   requester, decides where case folders may land.
3. Call `eject_from_pool(...)` with a bounded halt timeout. Because ejection can span
   ticks, write an `in_progress` result immediately and overwrite it with the terminal
   result when the `EjectTicket` settles — clients already poll, so a two-write result is
   cheap and keeps `wait_result` semantics unchanged.
4. Crash recovery: requests found in `executing/` are dead-lettered with an error result
   (the `EjectTicket` replay already guarantees the *case* ends consistent; the requester
   just resubmits).

Client surface: `CaseManagerClient.submit_remove(case_id=..., export_root_alias=...,
reason=...)` returning the usual `RequestHandle`.

## Open questions (the reason to explore before building)

1. **Which modes, if any, beyond `eject`?** Candidates: `discard` (move to the aberrant
   bucket — stays in managed filespace, recoverable, much safer) and `delete`
   (irreversible; probably should require a policy opt-in like
   `allow_remote_delete: bool = False`, and possibly never be remote at all). Shipping
   v1 as eject-only keeps the destructive surface minimal.
2. **Authorization.** The mailbox trust model today is "anyone who can write to the
   manager namespace is trusted." That is defensible for fire/reclassify; is it
   defensible for removal? Options range from accepting the current model (documented),
   to a per-kind enable flag (`enable_remove_mailbox`, default off), to a shared-secret
   field checked against policy. Filesystem permissions on the intake dir may be the
   pragmatic middle ground.
3. **Live vs terminal cases.** Should removal work on archived (terminal) cases too, or
   only pool members? Eject today is pool-only; compliance pulls would want archive
   reach, which is a different code path (no slot, no halt).
4. **Result lifetime vs the export.** Result files are swept after
   `mailbox_result_ttl_secs`; the export folder named in a completed result is the only
   pointer to where the case went. Is that acceptable, or does removal deserve a durable
   ledger (e.g. appending to a `removals.jsonl` in the manager namespace)?
5. **Idempotency / duplicate requests.** Two `RemoveRequest`s for the same case will race
   across ticks; second should error cleanly ("not in pool"), but we should decide
   whether that is an `error` or a benign `completed`-noop.

## Alternatives considered

1. **Do nothing; keep removal in-process (status quo).** Deployments that need remote
   removal write a companion loop. Zero new attack surface, but perpetuates the ad-hoc
   glue this library exists to remove.
2. **Mailbox `RemoveRequest`, eject-only, policy-confined destinations (recommended
   shape, pending the open questions).** Uniform with the other three request kinds;
   reuses the crash-safe `EjectTicket` machinery wholesale.
3. **Indirect removal via reclassification.** Provide a stock `EjectableCase`-style
   terminal type and use the existing `ReclassifyRequest` + archive policy to route
   unwanted cases into a quarantine grouping. No new protocol, fully recoverable — but it
   never actually removes data from managed filespace, so it fails the compliance
   motivation and abuses reclassify semantics.

## Recommendation

Adopt the shape in alternative 2, but only after the open questions — especially modes
(#1) and authorization (#2) — are settled with maintainers. Until then, remote removal
remains out of scope and `eject_from_pool` stays in-process only.
