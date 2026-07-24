# FolderBackedCase design patterns — an optional-enhancement catalog

Once the base subclass exists and binds cleanly, most designs benefit from one
or more of the recurring patterns below. None are mandatory. They exist because
the same handful of concerns — data survival past purge, human feedback,
failure/retry, stall/expiry, operator visibility — come up again and again once
a case type leaves the whiteboard and starts running unattended.

## How to use this list (agent instructions)

Consult this catalog **after** the base design binds (`SKILL.md` Step 6), not
before — the developer needs something concrete to react to.

**Exception:** pattern **0 (Inert intake)** is part of the *base* lifecycle
draft whenever the case ingests files. Raise it in Steps 1–2 of `SKILL.md`,
not as a post-bind optional — waiting until Step 7 is how create-time import
sneaks into the skeleton.

- Read the whole list, then judge each pattern against *this* case. Most cases
  warrant two or three of these, not all of them, and not none.
- **Raise only the patterns that plausibly apply**, one topic at a time, framed
  as a question ("Your `issue_permit` step calls an external API — want a retry
  guard so one flaky call doesn't stall the case?"). Do not dump the whole
  catalog at the developer or ask about patterns that obviously don't fit.
- Adopt a pattern **only on explicit confirmation.** When adopted, fold it into
  the declarations and stubs exactly as the base design was built — new states
  get `case_assert_*`, new triggers get `perform_`/`guard_` stubs, new assets
  get an `AssetSpec` and a pydantic class — then **re-run the bind check**
  (`SKILL.md` Step 6). A pattern that adds a state or trigger can break the FSM
  just like a hand-written one.
- Still never write real hook bodies — an adopted pattern adds *stubs*, same as
  everything else this skill generates.

DSL grammar, hook naming, and `AssetSpec` fields referenced below are all
defined in `dsl_and_hooks.md`; read it if any syntax here is unfamiliar.

## The patterns

### 0. Inert intake (default when files enter the case)

**When:** the case's work starts from one or more files the caller supplies
(uploads, drops, fixtures). This is the **default** shape unless the developer
specifically needs create-time import.

**Shape:**

- Initial state is **inert and empty**, named outside the domain (`new`,
  `initial`, …) — not `submitted` / `received` / `uploaded`.
- A **manual** trigger (e.g. `add_attachments`) takes a filepath or filepaths
  via kwargs (`tctx.kwargs`; keep them JSON-serializable when the manager may
  relay them). Its `perform_` copies/links those files into the case assets.
- Transition into a very temporary state such as `attachments_added`, then
  either loop back to `new` or enter the first real-flow state. Prefer a
  distinct intermediate state over an auto self-loop on `new`; if you do
  use a same-state auto edge, it must carry a named method guard (see
  `dsl_and_hooks.md`).

```text
[*] --> new == add_attachments ==> attachments_added -- begin --> submitted -- ...
%% or: attachments_added -- accept --> new   (then a separate edge starts the flow)
```

**Why not `create_case_in_folder`:** trigger ops get semantics, event tracking,
timing, and the rest of the case machinery. Pre-creation's main virtue is
speed. Heavy init in `create_case_in_folder` (or an override) is allowed when
the developer needs it — just note that testing and tracking get harder.
Pressure like "skip extra states" / "create with the PDF already there" is
exactly when to propose this shape and explain the tradeoff, not when to
collapse intake into construction.

**Notes:** pair with #6 (import manifest) when ingesting a *set* of files with
per-file status. `add_attachments` (or equivalent) gets a normal
`perform_` stub; do not implement the copy logic in the skeleton.

### 1. Pre-final data extraction

**When:** the case produces something worth keeping, but a terminal state
(`state --> [*]`) auto-purges every asset not explicitly kept, soon after
entry.

**Shape — three options, roughly increasing in cost:**

- **(a) Externalize on the way in.** An `on_enter_<terminal>` (or the
  `perform_` of the trigger into it) copies/writes the deliverable *outside* the
  case folder before the purge runs. Good when the destination is another
  system of record.
- **(b) Mark it `keep`.** Declare `AssetSpec(keep=True)` for anything *always*
  worth preserving, or call `self.case_keep_files("assets/result.yaml", ...)`
  from `on_terminating()` for a runtime, case-by-case decision. Simplest when
  the data can just stay in the (now-retained) case folder.
- **(c) Delay the purge with a manual pre-final gate.** Insert a non-terminal
  "done, pending pickup" state exited only by a manual (`==`) trigger, e.g.
  `... -- finish --> extract_pending == confirm_extracted ==> done --> [*]`. The case parks in
  `extract_pending` (it will not auto-advance past a `==` edge) until an
  external process reads the data and fires `confirm_extracted`. Use when a
  separate consumer must pull the data on its own schedule.

**Notes:** (a)/(b) are declare-time and cheap; (c) adds a state, a trigger, and
a real dependency on an external actor firing the manual edge — pair it with an
expiry (#4) so a consumer that never shows up doesn't park the case forever.

### 2. User review / feedback asset

**When:** a human's judgement of how well the system handled the case is
valuable (quality tracking, training data, dispute trail).

**Shape:** a `FileMappedPydanticMixin` model (e.g. `rating: int`,
`approved: bool`, `comments: str = ""`) behind
`asset_aliases = {"user_review": AssetSpec(relative_path="user_review.yaml",
loader=UserReview, trust_states={...}, keep=True)}`. `keep=True` because feedback is
almost always worth surviving termination. Often paired with a manual gate
(pattern 1c / 7) so the case waits for the review to be submitted.

**Notes:** decide which states the review is trustworthy in — usually only
late/terminal ones. If review is optional, don't gate advancement on it.

### 3. Retries for fallible steps

**When:** a trigger can fail transiently (network blip, rate limit, racey
input) and simply re-running it is a reasonable response.

**Shape:** a `@FAIL` guard pair on the edge —
`working -- do_thing [@FAIL<3] --> done` plus
`working -- give_up [@FAIL>=3] --> needs_attention`. `@FAIL` counts failed transition
attempts since entering the current state; a raised exception in the
`perform_`/`before_`/`guard_` counts as one attempt.

**Notes:** the default with no `@FAIL` guard is an implied `@FAIL<1` — one
attempt, then the failure just sits. Retry is opt-in on purpose. Always give
the exhausted branch (`@FAIL>=n`) *somewhere to go* (a triage state, an alert,
an abandon) rather than leaving the case wedged — see #5.

### 4. Expiring / abandoning stale cases

**When:** the fleet is not closely watched by humans, so a case that stalls
(waiting on a person, wedged on a repeated failure) could sit forever.

**Shape:** a `@DWELL` escape, often from the wildcard source so it applies
everywhere: `* -- mark_abandoned [@DWELL>30d] --> abandoned --> [*]`. Or
narrow it to the states that actually rot:
`awaiting_review -- mark_abandoned [@DWELL>14d] --> abandoned --> [*]`.

**Notes:** `@DWELL` measures time in the *current* state, so a case that keeps
transitioning normally never trips it — only genuinely idle ones. This is the
safety net that makes the manual gates in #1c/#2/#7 safe to add.

### 5. Failure / exception handling (avoiding the silent stall)

**When:** always worth a moment's thought. By default, an exception raised
while attempting a trigger aborts that transition and blocks forward progress;
with no retry (#3), no divert, and no expiry (#4), the case stalls quietly.

**Shape / options (usually a combination):**

- Add a **divert edge** off the failing state to a human-triaged state:
  `working -- escalate [@FAIL>=3] --> needs_attention`, with
  `needs_attention == reassign ==> working` and
  `needs_attention == abandon ==> abandoned --> [*]`.
- **Alert an operator** from the hook that detects the integrity problem via
  `self.case_emit_alert_event("what went wrong")` — a type-agnostic
  dashboard/fleet-scan marker. Use sparingly, for real deviations, not routine
  recoverable defects (those belong on `self.log`).
- Rely on **expiry (#4)** as the last-resort backstop.

**Notes:** decide, per fallible step, whether a failure should retry, divert,
alert, expire, or intentionally park for a human. "It stalls silently" is
rarely the answer you want.

### 6. Import manifest

**When:** the case ingests a *set* of documents/files and processes them,
possibly at different rates or with per-file outcomes.

**Shape:** a manifest asset — a `FileMappedPydanticMixin` model holding a list
of per-file entries (filename, source, size/hash, `status: "pending" |
"processed" | "failed"`, error note). Declared as a single-file
`AssetSpec(...)` alongside the `many=True` alias that globs
the actual incoming files. Pair with **#0 (Inert intake)** — populate the
manifest from `add_attachments` (or equivalent), not from
`create_case_in_folder`.

**Notes:** the manifest becomes the source of truth for "what came in and
where each item is," which a `guard_` can consult to decide when the batch is
complete. Consider `keep=True` if the processing record has audit value.

## Further patterns to consider

Shorter sketches — same "raise only if it fits, adopt only on confirmation"
rule applies.

### 7. Manual approval gate before an irreversible/expensive action

Convert the automated (`--`) edge into a costly, external, or irreversible step
into a manual (`==`) one, so a human explicitly authorizes it
(`ready == approve_send ==> sending`). The case parks until someone/something fires
the trigger. Pair with expiry (#4). This is the same mechanism as 1c, applied
for authorization rather than data pickup.

### 8. Failure diversion (dead-letter) state

A concrete companion to #5: one dedicated `needs_attention` / `error` state that
several failing steps divert into (via `[@FAIL>=n]`-guarded edges), with manual exits
to retry-from-scratch, reassign, or abandon. Keeps failure handling in one
inspectable place instead of scattered per-step dead-ends.

### 9. Resumable expensive steps

For a choked/costly trigger (one listed in `fsm_trigger_chokes`, or wrapped via
`case_invoke_process()` / `case_invoke_threaded()`), have its `perform_` write
partial progress to an asset and check that asset on entry, so a retry (#3)
*resumes* rather than redoing completed work. Trades implementation complexity
for not paying twice on a mid-step failure.

### 10. Run summary / outcome asset

A `keep=True` structured asset written on entering a terminal state (or in
`on_terminating`) capturing the outcome and a few key metrics (duration,
attempt counts, final decision). Complements the event journal with a compact,
type-specific record that a fleet-level analysis can read without replaying the
whole journal.

## Advanced pattern

This one is heavier than the rest, relies on a `FolderBackedCase` feature that
is **deliberately absent from `FolderBackedCaseInterface`**, and produces a
*family* of case types rather than a single class. Only raise it when the
domain genuinely has divergent processing rules that a single case type would
turn into a sprawling mess. When adopted, each specialized type is scaffolded
with this same skill — this pattern just describes how they fit together.

### 11. Classifier front-door (reclassify to a specialized type)

**When:** incoming work looks uniform at the door but then splits into families
whose processing diverges *wildly* — e.g. inbound email that turns out to be
spam vs. an invoice vs. a support request. Cramming every branch into one giant
case type is the smell this pattern removes.

**The mechanism it rests on — `case_reclassify_to`:** a case can change its own
type mid-life via `fresh = case.case_reclassify_to(SomeOtherCase)`, keeping its
folder, id, and history. It is a two-phase, crash-atomic switch (a crash
mid-swap reopens cleanly as the *old* type). Its **only** built-in validation is
that the case's current state name also exists in the target class — otherwise
`IncompatibleReclassError` is raised. Everything else — asset-schema
compatibility, whether the target's logic can pick up from that state — is the
**caller's** responsibility; there are no guardrails. (Under a running
`CaseManager`, do this through `CaseManager.reclassify_case()` /
`submit_reclassify()` rather than calling the raw method inside the pool. See
`case_reclassify_to`'s own docstring in `folder_backed_case.py` for the
authoritative contract — it is outside this skill's normal interface-only
scope, so read it before implementing.)

**Shape — a "front-door" case plus one specialized case per family:**

- A **classifier / intake case** (e.g. `NewMailIntakeCase`) does the common
  initial processing, then advances to a **penultimate handoff state** named
  something like `classified`. Give that state a *manual* exit to a terminal
  state it is **never actually meant to reach**, purely to satisfy the FSM's
  "non-terminal states need an exit" rule, e.g.
  `... -- identify --> classified == never ==> closed --> [*]`. Because the edge is manual
  (`==`), the case simply **parks** in `classified` and waits.
- An **external force** (a watcher, the manager, an operator) notices cases
  sitting in `classified`, decides the family, and calls
  `case_reclassify_to(InvoiceCase)` (or `SpamCase`, etc.). The case lives out
  the rest of its life as that specialized type.
- Each **specialized case type** (`InvoiceCase`, `SpamCase`, ...) must declare a
  state also named `classified` (the shared handoff name) — and since that state
  is reached *only* via reclassify, not by any edge, declare it **initial**
  (`[*] --> classified`) so the parser's reachability check passes; give it
  automated edges onward into that family's real processing:
  `[*] --> classified -- extract --> extracting -- post --> posted --> [*]`.

**Notes / tradeoffs:**

- The handoff state name is the whole contract between front door and
  specialists — keep it identical and stable across all of them.
- Assets carry over by folder, not by schema: if the intake wrote assets the
  specialist needs, make sure the specialist declares compatible aliases; there
  is no automatic reconciliation.
- Pair the parked `classified` state with an expiry (#4) so a case that never
  gets reclassified doesn't sit forever.
- This is genuinely advanced. Prefer the simpler patterns (a fat guard-driven
  branch, or separate top-level case types chosen at creation) unless the
  divergence really justifies a reclassifying front door.
