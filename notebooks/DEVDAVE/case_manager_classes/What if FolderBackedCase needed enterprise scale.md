# What if `FolderBackedCase` needed enterprise scale?

- Status: **thought experiment / pre-design notes.** Not a proposal, not scheduled work.
  Written 2026-07-16 in response to a hypothetical city-level call-center bid that would
  need a `FolderBackedCase`-shaped case model but on object/database storage instead of a
  local filesystem, with high concurrency and parallel processing.
- Purpose: capture every design assumption we could find that is safe today (department-level
  users, low concurrency) but would need to be rethought — not just re-implemented — for that
  regime. The goal of this note is enumeration and risk-ranking, not solutioning.

## Framing: one root cause shows up in several places

Almost every item below is a variation on the same theme: **the current implementation gets a
lot of its correctness and performance "for free" from POSIX filesystem guarantees that most
object/database stores do not provide** — atomic exclusive-create, atomic rename, real directory
listing, cheap recursive walks, and true append. A port is not "swap `Path` calls for `boto3`
calls"; it's "identify every place a POSIX guarantee is silently load-bearing, and design a real
replacement primitive for it." The items below are grouped, but keep this thread in mind — several
of them are the *same* underlying gap wearing different clothes.

---

## 1. Exclusivity / ownership (the lease)

*(Carried over from the prior discussion — recorded here for completeness.)*

`HeartbeatLease.acquire()` (`heartbeat_lease.py`) is a plain check-then-act: `stat()` the lease
file's mtime, decide it's free, then `os.utime()` an arbitrary future expiry into it. There is no
compare-and-swap. It is safe today only because a local `stat`+`utime` pair is effectively
instantaneous and single-node. Object stores give you no equivalent write-an-arbitrary-timestamp-
into-metadata trick, and most don't give you unconditional read-then-write safety either — a real
port needs an actual conditional-write / distributed-lock primitive (DynamoDB conditional
`PutItem`, S3 `If-Match`/`If-None-Match`, a lock service, etc.), not a relocated version of the
same trick.

## 2. Event-log sequence numbers — agree with the pushback, but note the shared root cause

Fair pushback: a GUID-based or DB-sequence-based ID generator makes the *uniqueness* problem go
away, and that's not really a framework flaw — `PrimitiveEventLog` already isolates "how do I name
the next event" behind one method (`_get_next_sequence_number` / the claim loop in
`create_event()`), so swapping the ID strategy is a contained change.

Worth keeping in view, though: today's claim loop isn't just "ID generation," it's also this
codebase's *other* worked example of the same TOCTOU pattern as the lease — list-the-directory to
guess `next_seq`, then race to claim it via `Path.touch(exist_ok=False)`, which raises loudly
(`FileExistsError`) if you lose the race. A GUID generator sidesteps the *naming* collision, but
whatever replaces `touch(exist_ok=False)` as the "did my write actually win, uncontested" signal
still needs to exist — otherwise you lose the property that a lost race today is loud
(exception, retry) rather than a silent overwrite. So: not a blocker, but "pick a good ID scheme"
and "pick a real conditional-write primitive" are two separate decisions, and only the first one
is fully solved by a GUID.

## 3. Directory-shaped asset addressing

`CaseAssets` (`case_assets.py`) is not just "files live under `assets/`" — the *addressing model
itself* assumes a walkable tree:

- `list_assets()` does `root.rglob("*")` — a full recursive filesystem walk.
- `dataclass_paths()` resolves a glob-patterned `AssetSpec` (`relative_path` may contain `*`, `?`,
  `[...]`) by listing everything and filtering client-side with `PurePosixPath.match()`.
- Multi-file aliases (e.g. `resolution-log/customer--convo.md`-style patterns with wildcards) are a
  first-class, documented feature (`AssetSpec.relative_path` glob support), not an edge case.

None of this maps cleanly onto "asset lives in an S3 object / a row in a document store." A glob
like `reports/*.csv` requires either (a) listing an entire prefix and filtering client-side every
time (works, but is an O(N) network round-trip per lookup instead of a path stat), or (b) giving up
glob-style aliases entirely and requiring exact keys or an explicit index of what belongs to an
alias. Prefix listing on an object store is also not free the way `rglob` on a warm local directory
is — it's paginated, it's a network call, and (depending on provider/config) it may not be
strongly consistent immediately after a write, which the current "write then immediately relist to
find your own file" pattern (`dataclass_paths` → `list_assets` → filter) quietly assumes it can
rely on.

`CaseKeepManifest.purge()` has the identical shape: `root.rglob("*")` to find candidates, glob
matching against `_keep.txt` rules, then `_prune_empty_dirs()` — another full `rglob` looking for
now-empty directories to `rmdir()`. Object stores have no directories to prune and no server-side
wildcard delete; retention-at-close would need to become "delete this explicit list of keys"
(batch delete, partial-failure-tolerant) rather than "walk the tree and delete what isn't listed."

## 4. Attach/dehydrate cost and process affinity

This is the sharpest of the three you raised, and it fans out into a few concrete sub-problems:

**a. `__init__` / `_bind_existing_case_dir` is not a light operation.** Every attach
(`FolderBackedCase(folder)`, `create_case_in_folder`, or `case_type_registry.rehydrate(folder)`)
does, unconditionally: read+parse the full record, build the journal handle, run
`_keep_manifest.ensure_framework_rules()` (a disk **write**, idempotent-but-still-a-write), acquire
the lease (a disk **write**, can raise `CaseAlreadyOpenError`), build the per-case logger, and build
the whole `transitions`-backed FSM carrier (`_CaseMachineFactory.build`). None of this is lazy —
there's no "just get me the status" partial-attach path. (There *is* a genuinely cheap, lock-free
"just get me the status" path — `get_case_reader()` / the `peek_*` staticmethods — which is exactly
the right escape valve today. The concern is what happens once you *do* need to act on the case,
not just read it.)

**b. The live object is process-pinned, and so is the pool that drives it.**
`CasePoolDriver`'s own contract (`case_pool_driver.py`) says membership is an in-memory container
keyed by folder path — "Cases arrive already bound (live, lease-held) and leave the same way" —
and `BalancedCasePoolDriver`/`SeniorityCasePoolDriver` are per-process schedulers. There is no
wire format for "the live case, machine state and all" that travels between processes; the closest
thing, `HeartbeatLease.handoff()`/`resume()`, only hands off the *lease token* — the receiver still
has to reconstruct the live object from scratch (a fresh attach), it just skips owning-collision.

Put those two together and you get exactly the worry you raised: behind a load balancer with
multiple stateless replicas, there is no cheap way to "reach" a case that's live in replica A's
process memory from a request that lands on replica B. Today's implicit answer is **sticky
routing** — always route a given case's work to whichever process/pool currently holds it — which
is a perfectly normal pattern (and probably still the right one at enterprise scale, e.g. via
consistent hashing over case_id to a worker shard), but it is *implicit* right now, nowhere stated
as a deployment requirement, and it interacts badly with "high concurrency, parallel processing" if
that phrase implies horizontally scaling stateless request handlers rather than sharding
long-lived case ownership across a fixed set of workers.

**c. There's no partial/lazy hydration strategy for a case with large or many assets.** Locally,
"the assets are all just files a few centimeters away on disk" makes lazy-loading a non-problem —
`case_load_asset()` reads on demand and the OS page cache absorbs repeat reads. Move assets to S3
(or a document store) and every one of those on-demand reads becomes a network round trip with
real latency and cost; a naive port would either (i) eagerly pull everything at attach time
(defeats the point of "lazy," and makes attach even heavier — see 4a), or (ii) keep the on-demand
model but now pay network latency on every `case_load_asset()` call that used to be a cheap local
read. Neither is obviously right; this needs a deliberate caching/prefetch policy, which is new
design, not a port.

## 5. Event log as "should this become a database?"

Every domain read on `CaseEventJournal` (`current_state`, `count_fails_this_dwell()`,
`last_activity_at`, `unresolved_trigger_started`, `has_event_since_enter()`) is a **linear scan**
of `PrimitiveEventLog.events()`, which itself is "list the directory, parse every filename that
matches the pattern, sort by (dir, filename)." There is no materialized "current state" column
separate from "the most recent `CASE_STATE_ENTERED` file in this directory" — the log *is* the
index. `PrimitiveEventLog` does offer an opt-in same-process scan cache (`cache_msecs`), which
softens this for repeated reads inside one attach, but it doesn't change the fundamental shape:
discovering "what happened in this case" means listing a prefix, every time, for every reader.

Locally this is fine — directory entries for a few hundred events are a handful of cheap syscalls
against a warm page cache. On an object store, "list objects under this prefix" is a paginated
network call, and doing that on every guard evaluation (`@FAIL`, `@DWELL` are both computed off
this log on every transition attempt) turns what is currently a non-event into a real latency and
cost line item, multiplied by however many transition attempts a busy call-center case makes.

So: yes, this probably wants to become a real database-backed log for that deployment — not
because the *event-sourced* design is wrong (it's a genuinely good fit for "authoritative audit
trail" + "derive current state from history" + "recoverable after a crash"), but because "the
directory listing is my index" needs to become "an actual index" (a table with a
`case_id, seq, label, value, ts` shape and a `WHERE case_id = ? ORDER BY seq DESC LIMIT 1`-style
query, or the moral equivalent in whatever store is chosen). That's a genuine redesign of the
storage layer underneath `CaseEventJournal`, done in a way that preserves its read *contract*
(current_state, dwell anchor, fail count, "has this fired since entering state") — which is
good news, actually: the domain-read API is already the right seam to port behind.

## 6. Other assumptions found while looking for the above

Flagging these because they're the same shape of problem and are easy to miss if the review stops
at "the big three you already named":

- **Case-ID generation documents its own multi-process limitation.** `TimeSlugCaseIDGenerator`
  (`case_id_generation.py`) says outright: "This does not guarantee uniqueness across multiple
  processes or machines" — monotonicity is only enforced per generator *instance*. Harmless today
  because case creation is low-volume and typically single-writer; a real risk the moment case
  intake is horizontally scaled across workers without a shared ID authority. (Straightforward
  fix — swap the generator — but worth listing because it's a silent multi-process correctness gap
  hiding in the *default*, not just a hypothetical.)

- **The per-case log file assumes true, cheap, concurrency-safe append.** `_CaseFileLogHandler`
  (`case_logging.py`) opens the file, appends one line, and closes — on every single log record.
  Unlike the event log (which sidesteps append by writing one immutable file per event),
  `logs/case.log` is a genuine growing-file append. Object stores don't have a native append
  operation at all (you rewrite the whole object, or you don't use the object store for this and
  instead ship logs to a real logging/observability sink). This is a straightforward fix
  (redirect to structured logging / a log shipper instead of a file) but it's a different fix than
  "point the existing handler at a different path," so it's worth calling out on its own.

- **Resource choking is in-process only.** `ChokePermitGovernor` (`choke_permit_governor.py`) is an
  in-memory `asyncio`-based permit counter per named resource (e.g. `"cpu"`, `"ms-graph-api"`).
  Fine for one pool driver in one process; at enterprise scale with many worker processes/nodes
  all drawing on the same named external resource, this needs to become a distributed rate
  limiter/semaphore (Redis, a token-bucket service, etc.) or every node will happily over-commit
  the shared resource independently.

- **The case *record* has the same lock-and-low-volume assumption as the event log, from a
  different module.** `FileMappedPydanticMixin` (`file_mapped_pydantic_mixin.py`) — which backs
  `CaseRecord` — describes itself, in its own docstring, as a "database alternative for **low-volume
  data**... hundreds to thousands of records, not millions," with concurrency control provided by
  **file locking**. That's a second, independent instance of "OS-level exclusive access is the
  concurrency primitive" (distinct from the lease, which governs the *case* as a whole — this one
  governs the *record file* specifically), and a second independent "this was explicitly scoped for
  low volume" admission worth taking at face value when sizing the port.

- **"Atomically movable/archivable folder" is a stated feature, not an implementation detail.**
  The `FolderBackedCaseInterface` docstring states the folder "can be archived or moved
  atomically" as a *feature* of the design. Object stores have no atomic move (archival becomes
  copy-every-key then delete-every-key, with a window where a crash leaves both a live and a
  half-copied archive, or neither). Anything downstream that currently leans on "just `mv` the
  case folder" as a cheap, safe operation (archival-on-close, tests, ops tooling) would need an
  explicit two-phase or idempotent-retry archival protocol instead.

---

## A pattern worth naming

Reading across all six sections, the honest summary is: **`FolderBackedCase` isn't "a case
framework that happens to use files" so much as "a case framework whose concurrency, indexing, and
atomicity model *is* the POSIX filesystem," with a genuinely clean domain contract
(`FolderBackedCaseInterface`) sitting on top of that foundation.** That split is actually good news
for a port: the interface layer (triggers, guards, FSM chains, asset aliases, the event-journal
*read contract*) looks like it was designed independently of the storage mechanics, and mostly
doesn't leak `Path` into its public surface. The work isn't "rewrite the interface" — it's "design
a real distributed-systems foundation (conditional writes, an actual index instead of a directory
listing, a resource-throttling service, a log-shipping story, an explicit archival protocol) and
re-implement the concrete class against that foundation instead of against `pathlib`." That's a
substantial project — closer to "write a new backend from the same contract" than "port an
existing one" — which is the framing worth carrying into any scoping conversation about that bid.
