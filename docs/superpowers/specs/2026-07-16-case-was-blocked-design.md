# Design: `case_was_blocked` (Approach A)

## Semantics

- **In-memory only** on the live instance; `False` after create/open/rehydrate.
- Past-tense: last observation from this process, not a live proof and not journal-backed.
- Orthogonal to `case_advanceable` (structural). Drivers that want today’s `blocked_cases` meaning use:
  `case.case_was_blocked or not case.case_advanceable`

## Set / clear

| Event | Effect |
|---|---|
| Unrestricted `case_advance()` proves `AutoAdvanceBlocked` | **set** `True` |
| Unrestricted `case_advance()` progresses or fails | **clear** |
| Unrestricted same-state no-fail no-op (e.g. timed-escape dwell) | **leave unchanged** |
| Restricted `case_advance(trigger=…)` (any outcome) | **clear** (observation stale; path can’t re-prove the whole state) |
| Direct `await case.<trigger>(…)` | **clear** at call time (same reason) |

## Surface

- Declare `case_was_blocked: bool` on `FolderBackedCaseInterface` next to `case_advanceable` / `case_transition_fail_count`, with a short docstring that it’s last-observation / process-lifetime.
- Implement as a private flag on `FolderBackedCase`, updated inside `_CaseAdvancer` (and trigger entry for the direct-call clear).
- `BalancedCasePoolDriver.blocked_cases()` switches to the property (keep the `or not case_advanceable` half).

## Out of scope

- No new journal event; existing one-per-dwell `CASE_ALERTED` stays as-is.
- `slot.last_result` stays for events/peek; just not the source of truth for “was blocked.”
