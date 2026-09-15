# The CaseManager on-disk layout

Everything a running manager owns, in one place. **This file is generated** from
`case_manager_support/namespace_map.py` — the same declaration
`CaseManager._ensure_namespace_dirs` provisions from. Edit that module and
regenerate; a hand edit here will fail `tests/test_case_manager_namespace_map.py`.

To regenerate the tree below in place:

```bash
uv run python -m totodev_pub.case_manager_support.namespace_map docs/case-manager-layout.md
```

Run with no argument to print the tree to stdout instead. Only the block between
the generated markers is rewritten; the prose around it is hand-written.

## The tree

Paths shown with the default policy. Every name below `cache_root/` is a Layout
field, so a deployment that renamed one sees its own name here instead.

<!-- BEGIN GENERATED LAYOUT -->
```text
cache_root/
├── live/                                     CASE DATA — cases being worked on. Stable path per case
├── terminated/                               CASE DATA — finished cases, in date folders  (on demand)
├── quarantined/                              CASE DATA — cases set aside for a human, in date folders  (on demand)
└── .case_manager/                            The manager's own workspace. Not a status bucket, not case data
    ├── case_manager_policy.yaml              Layout and tunables. Written once, at creation
    ├── manifest.yaml                         Heartbeat and protocol paths, for clients
    ├── fleet_status.jsonl                    One row per case, for cheap whole-fleet reads
    ├── incoming/                             Case material being assembled. Admitted once a .ready marker appears
    ├── requests/                             The request channel. Every action, one message format
    │   ├── queued/                           Submitted; the manager has not claimed it yet
    │   ├── claimed/                          Claimed, awaiting a slot. One dir per case
    │   │   └── {case_id}/                    One case's claimed work  (on demand)
    │   ├── running/                          Executing right now. One dir per case
    │   │   └── {case_id}/                    One case's running work  (on demand)
    │   ├── failed/                           Gave up, or would not parse. Never retried — a human must look
    │   ├── results/                          Answers by correlation id. Swept after result_ttl_secs
    │   └── shutdown/                         Any non-hidden file requests a stop. Polled by the host, not the adapter
    ├── termination/                          Tickets for termination. Job files, never case data
    │   ├── pending/                          Owed, and will be retried
    │   └── failed/                           Gave up after max retries. A human must look
    ├── eject/                                Tickets for ejection out of storage. Job files, never case data
    │   ├── pending/                          Owed, and will be retried
    │   └── failed/                           Gave up after max retries. A human must look
    └── quarantine/                           Tickets for quarantine. Job files, never case data
        ├── pending/                          Owed, and will be retried
        └── failed/                           Gave up after max retries. A human must look
```
<!-- END GENERATED LAYOUT -->

## How to read it

**The three status buckets hold case data; nothing else does.** A case's bucket
*is* its status — there is no status field that could disagree with the tree,
because moving a folder is atomic and therefore crash-safe. Only `live/` is
created with the filespace; `terminated/` and `quarantined/` appear when a case
first reaches them.

**`.case_manager/` is the manager's workspace, and holds instructions rather than
case data.** A request file is a few hundred bytes naming a case by id; the case
it names lives in a bucket and may be gigabytes. `incoming/` is the exception — a
folder there is a case being assembled, and it leaves the namespace the moment it
is admitted.

**`(on demand)` means the directory is created when first needed**, not at
provisioning. A filespace with no work in flight has no
`requests/running/{case_id}/`, so its absence is not a fault.

## The request channel

Every action travels as one message format through one queue. The action is a
field *inside* the file rather than the folder it sits in:

```yaml
op: fire                        # was the mailbox folder name
id: xyz-789                     # correlation id
attempts: 0
requested_at: '2026-08-14T10:00:00Z'
case_id: abc-999                # what claimed/ and running/ are keyed by
payload:
  trigger: approve
  trigger_kwargs: {}
```

A request **moves between folders and is never rewritten** — the bytes are
identical at every stage, and only the path changes, by atomic rename:

```text
requests/queued/xyz-789.yaml               submitted
requests/claimed/abc-999/xyz-789.yaml      claimed, awaiting a slot
requests/running/abc-999/xyz-789.yaml      executing
(deleted, after the answer is written)     finished
```

That is why state belongs in the path. Moving a file is atomic, so if two
drainers ever raced, only one could win the claim; rewriting a `state:` field
inside a file is not atomic, and a crash mid-rewrite leaves a corrupt file.

The answer is written to `results/` **before** the request file is deleted. If the
process dies between the two, recovery finds a request whose answer already
exists and knows the work completed.

Two stages that look redundant are not: `claimed` means accepted but waiting on a
concurrency slot or a choke permit, and `running` means executing. Collapsing them
would hide the queue depth an operator needs to see.

**Requests with no case id** are keyed under `_unknown/`. A fire may address its
case by folder rather than id, and an adopt's id is read off its source on a
best-effort basis. The `_` prefix is reserved — a real case id is a base-36 time
slug or a uuid4 hex, so neither can collide with it.

**Shutdown keeps its own leaf** rather than joining `queued/`. It is process
control owned by the host, served whether or not an adapter exists, and its
protocol is "any non-hidden file" rather than a parsed envelope. Routing it
through the adapter's queue would break `enable_mailbox=False`, which must still
allow a stop.

### One name collision worth knowing about

`termination/`, `eject/` and `quarantine/` inside the namespace are **ticket
directories** — small job files recording work still owed, carrying `case_id`,
`retry_count` and `last_error`. They sit one or three letters away from the
`terminated/` and `quarantined/` **status buckets**, which hold the case data
itself. A job folder is the *intent*; the matching bucket is the *destination*.

Folding these three into `requests/` is a separate, later change; until then they
keep their own retry vocabulary.

### What each directory tells you when something is wrong

| What you see | What it means |
|---|---|
| Files piling up in `queued/` | Nothing is draining — the manager is dead, blocked, or restarting. Check the heartbeat in `manifest.yaml` |
| Files sitting in `claimed/` for a long time | Accepted, but no free slot: check `concurrency_ceiling` and `choke_limits` |
| A file left in `running/` after a restart | The process died mid-step; adapter recovery settles it. Repeatedly, and that case is the problem |
| Anything in `requests/failed/` | The manager gave up, or the request would not parse. Nothing will retry it |
| Anything in a ticket `failed/` | A ticket gave up after `*_max_retries`. A human must look |
| A folder in `incoming/` with no `.ready` | An upload was abandoned. Harmless; the cleaner removes it |
| An unread file in `results/` | The submitter timed out or crashed. Harmless; swept after `result_ttl_secs` |
