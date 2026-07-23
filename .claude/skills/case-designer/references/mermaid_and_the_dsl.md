# Mermaid and the chain DSL: differences and convertibility

Orientation for working with lifecycle sketches drawn in Mermaid (or tools
that export it). The DSL grammar itself lives in `dsl_and_hooks.md`; the
authoritative tool contracts live in `src/` docstrings (pointers at the end).
Read this when a developer brings a diagram, or asks why a converted sketch
looks different from what they drew.

## Why they are close

The chain DSL is deliberately Mermaid-flavoured: `[*]` boundary hops,
`-- label -->` edges, `A --> B : label` colon labels, and `%%` comments are
shared vocabulary. The relationship is asymmetric by design:

- **DSL → Mermaid is lossless.** `case_doc.to_mermaid` (state style) emits a
  diagram that is *itself a valid declaration* — it parses back to the same
  FSM. Rendering is safe to do at any time.
- **Mermaid → DSL is lossy but visible.** A sketch under-specifies an FSM, so
  the CLI's `--convert` fills gaps with `%% TODO` markers and comments out
  what it cannot express — it never guesses silently, and it is idempotent
  (valid DSL passes through untouched).

## Concept mapping

| Concept | stateDiagram-v2 | flowchart | chain DSL |
|---|---|---|---|
| Initial / terminal | `[*] --> s` / `s --> [*]` | styling only | same hops as stateDiagram |
| Labeled edge | `A --> B : label` (one per line) | `A -- label --> B`, `A -->\|label\| B` | both forms accepted |
| Multi-hop chains | no | yes | yes |
| Manual vs auto edge | **no such concept** | thick `==>` (visual only) | **semantic**: `-- -->` auto, `== ==>` manual, `: ==` label marker |
| Guards | free text in label | free text | parsed: `trigger [g1, @DWELL>2d]` |
| Trigger soft-timeout | none | none | `trigger~30s` |
| Any-state wildcard | none | none | bare `*` source |
| Comments | `%%` | `%%` | `%%` |

## The semantic gaps (what no diagram can tell you)

These are the reasons conversion can never be fully automatic — each one is an
**interview question**, not a transform:

1. **Auto vs manual.** Mermaid's `-->` carries no execution policy; the DSL's
   does. A pasted sketch converts to all-auto, meaning `case_advance()` would
   sprint it to terminal unattended. Every human/event gate must be marked
   manual (`==>` / `: ==`) by a person who knows the process — the CLI's
   "no manual edges" lint exists precisely to force this conversation.
2. **Trigger identity.** A Mermaid label is prose ("Open Ticket!"); a DSL
   trigger is a method name (`perform_open_ticket`). `--convert` slugs labels
   and invents `to_<dest>` placeholders for unlabeled edges, but whether those
   names match the business vocabulary is a design decision.
3. **Guards and timing are load-bearing.** `[approved]` in a Mermaid label is
   decoration; in the DSL it binds `guard_approved` and gates the edge. Never
   assume bracketed text in a sketch was meant with DSL semantics — confirm.

## Re-expressing constructs the DSL does not have

`--convert` comments these out with a TODO; *you* translate the intent:

- **`<<choice>>` pseudo-state** → guarded auto branching from one ordinary
  state: `case_advance()` tries a state's auto edges in declared order and
  fires the first whose guards permit — that IS the choice diamond.
  `screened -- route [eligible] --> awaiting_review` /
  `screened -- reject --> denied` replaces the diamond.
- **Fork/join (parallel regions)** → the DSL has no concurrency inside one
  case. Either keep one coarse state whose `perform_` runs the parallel work
  internally (`case_invoke_threaded`/`case_invoke_process`), or split the
  parallel branches into separate case types coordinated by their own cases.
- **Composite / nested states** → flatten. If grouping matters, prefix the
  inner names (`review_triaged`, `review_priced`) and keep the graph honest.
- **History state `[H]`** → no equivalent; if "resume where it left off"
  matters, make that state explicit or record position in an asset.
- **Notes** → docstrings on the hooks, or a `case_assert_*` if the note is
  really an invariant ("humans look here" is often an assertion in disguise).

## Convertibility guarantees, precisely

- `to_mermaid(style="state")` round-trips through `StateChainParser.parse()`
  when the graph is built with `include_implied_caps=False` and the spec has
  no wildcard chains (a wildcard renders via a synthetic `ANY_STATE` node,
  which would re-parse as an ordinary state).
- `to_mermaid(style="flowchart")` is presentational only (thick `==>` manual
  arrows, colored initial/terminal classes); it does not round-trip.
- `--convert` output always **parses**; it **validates** only if the sketch
  declared an initial and a terminal (`[*]` hops). "Parses but does not
  validate yet" is a normal mid-workflow condition, not a failure.

## Where the truth lives

This file is orientation, not contract. Authoritative behavior:

- Grammar: module docstring of
  `src/totodev_pub/folder_backed_case_support/state_chain_parser.py`
- Conversion rules: module docstring of
  `src/totodev_pub/folder_backed_case_support/mermaid_intake.py`
- Rendering / round-trip contract: `to_mermaid` docstring in
  `src/totodev_pub/folder_backed_case_support/case_doc.py`
- Lint heuristics: `lint_spec` docstring in `state_chain_parser.py`
