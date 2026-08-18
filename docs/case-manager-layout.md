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
    ├── staging/                              Scratch dock for in-process assembly. Holds a lease while building
    ├── adopt_drop/                           Scratch dock for a folder handed over by another process. No lease
    ├── fire_mailbox/                         Mailbox: 'make this case take a step'
    │   ├── intake/                           Submitted fires, awaiting the next drain
    │   ├── malformed/                        Dead-lettered: the request would not parse
    │   ├── pending/                          Accepted, awaiting a slot or choke permit  (on demand)
    │   │   └── {case_id}/                    One case's queued step  (on demand)
    │   └── firing/                           Launched by the sweep  (on demand)
    │       └── {case_id}/                    One case's running step  (on demand)
    ├── adopt_mailbox/                        Mailbox: 'take ownership of this folder'
    │   ├── intake/                           Submitted adopts, awaiting the next drain
    │   └── pending/                          Executing now. Settled against the store after a crash
    ├── reclassify_mailbox/                   Mailbox: 'change this case to a different type'
    │   ├── intake/                           Submitted reclassifies, awaiting the next drain
    │   ├── malformed/                        Dead-lettered: the request would not parse
    │   └── executing/                        Executing now, one subfolder per case
    │       └── {case_id}/                    One case's step  (on demand)
    ├── shutdown_mailbox/                     Mailbox: 'stop'. Polled by the host, not the adapter
    │   └── intake/                           Any non-hidden file here requests a shutdown
    ├── results/                              Answers by correlation id. Swept after result_ttl_secs
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
it names lives in a bucket and may be gigabytes. The two scratch docks are the
exception — a folder in `staging/` or `adopt_drop/` is a case being assembled,
and it leaves the namespace the moment it is admitted.

**`(on demand)` means the directory is created when first needed**, not at
provisioning. A filespace that has never fired a step has no
`fire_mailbox/firing/`, so its absence is not a fault.

### Two name collisions worth knowing about

`termination/`, `eject/` and `quarantine/` inside the namespace are **ticket
directories** — small job files recording work still owed, carrying `case_id`,
`retry_count` and `last_error`. They sit one or three letters away from the
`terminated/` and `quarantined/` **status buckets**, which hold the case data
itself. A job folder is the *intent*; the matching bucket is the *destination*.

`pending/` means two different things in two sibling mailboxes: in
`fire_mailbox/` it is *accepted but not yet launched*, and in `adopt_mailbox/` it
is *executing now*. Three words are in use for "in progress" — `firing`,
`executing`, and adopt's `pending`.

### What each directory tells you when something is wrong

| What you see | What it means |
|---|---|
| Files piling up in any `intake/` | Nothing is draining — the manager is dead, blocked, or restarting |
| Files in `fire_mailbox/pending/` for a long time | Accepted, but no free slot: check `concurrency_ceiling` and `choke_limits` |
| A file left in `firing/` or `executing/` after a restart | The process died mid-step; adapter recovery settles it |
| Anything in `malformed/` | A request would not parse. Nothing will retry it |
| Anything in `*/failed/` | A ticket gave up after `*_max_retries`. A human must look |
| An unread file in `results/` | The submitter timed out or crashed. Harmless; swept after `result_ttl_secs` |
