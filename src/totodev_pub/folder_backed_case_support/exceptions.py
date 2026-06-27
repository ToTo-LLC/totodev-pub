# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Exception types raised by the FolderBackedCase family. Grouped here so callers can
import the whole error vocabulary from one place and the main module stays lean."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class CaseAlreadyOpenError(Exception):
    """Raised on construction when a non-expired lease (future mtime) already exists on
    the folder — another live owner holds it. `expires_in` is seconds until it lapses."""
    def __init__(self, folder: Path, *, expires_in: float):
        super().__init__(
            f"{folder} is already open (lease valid for ~{expires_in:.0f}s more). "
            "Wait for the current owner to detach() or for the lease to expire."
        )
        self.folder = folder
        self.expires_in = expires_in


class OwnershipLostError(Exception):
    """FATAL: our heartbeat found the on-disk lease no longer matches what we wrote —
    another process reclaimed this folder past our TTL. The displaced owner must stop."""
    def __init__(self, folder: Path):
        super().__init__(
            f"Ownership of {folder} has been lost: the lease file was overwritten by "
            "another process. This instance must not continue operating on this folder."
        )
        self.folder = folder


class DetachedCaseError(Exception):
    """A mutating operation was attempted on a case husk that has already detach()ed from
    its folder (lease cleared). Construct a fresh instance (rehydrate) to act on it again."""
    def __init__(self, folder: Path):
        super().__init__(
            f"This FolderBackedCase for {folder} has already been detached. "
            "Use rehydrate() to open a fresh instance."
        )
        self.folder = folder


class UnregisteredCaseTypeError(Exception):
    """Raised by rehydrate() when the stored case_object_type has no matching entry in
    the registry — i.e. the class was never passed to register_case_types()."""
    def __init__(self, type_name: Optional[str]):
        super().__init__(
            f"Case type {type_name!r} is not in the FolderBackedCase registry. "
            "Call FolderBackedCase.register_case_types(YourClass) at startup."
        )
        self.type_name = type_name


class RecordTypeMismatchError(Exception):
    """Raised by _flush_record when the record's case_object_type doesn't match the
    class attempting the write. A FolderBackedCase may only ever write its OWN name.

    If you hit this inside reclassify_to(): the friction is DELIBERATE. Phase 2
    constructs the NEW class over a record that still carries the OLD name (so a
    crash mid-reclassify reopens cleanly as the old class). Before the committing
    flush you must CONSCIOUSLY stamp the new name (and migrate any new-schema
    fields, if the new _record_cls added some):

        fresh._record.case_object_type = NewClass.__name__
        # ...initialize/migrate any new-schema fields here, if needed...
        fresh._flush_record(force=True)
    """
    def __init__(self, on_record: str, writing_class: str):
        super().__init__(
            f"record case_object_type={on_record!r} but {writing_class!r} is writing it. "
            f"A case may only write its own name. If reclassifying, set "
            f"_record.case_object_type to the new class name (and migrate any new "
            f"fields) before flushing — this guard is intentional."
        )


class IncompatibleReclassError(Exception):
    """Raised when reclassify_to() is called but the current FSM state is not present
    among the target class's FSM states."""
    def __init__(self, current_state: str, target_class: str):
        super().__init__(
            f"Cannot reclassify to {target_class!r}: current state {current_state!r} "
            "is not among that class's FSM states."
        )


class AutoAdvanceBlocked(Exception):
    """A case is OPEN but no auto-advance edge can fire from the current state — now OR
    ever, by the mere passage of time. Every auto candidate's guard declined this pass and
    the state has no self-relaxing time guard (`@DWELL>...`) that would ripen to let one
    fire later, so the case is genuinely stuck waiting on out-of-band help.

    SCOPE: only the unattended advance() path is walled off — manual or event-driven
    (`==`) transitions may still be perfectly available, which is why it is "auto
    advance" blocked, not "all transitions" blocked.

    USAGE: advance() does NOT raise this — it CARRIES it in AdvanceResult.exceptions as
    data, so a blind driver can inspect it without a try/except (a direct/manual trigger
    call, having no AdvanceResult to return, still raises). It is deterministic and
    idempotent: the same state yields the same block on every call until something changes.

    REMEDY: give the state a timed escape, e.g. `--@DWELL>{N}h#timeout-->somewhere`, or a
    blanket net like `*--@DWELL>=2d#timeout-->expired^`; or resolve/route it manually.
    """
    def __init__(self, case_id: str, state: str, *, candidates: Optional[list] = None):
        super().__init__(
            f"Case {case_id!r} is auto-advance blocked in state {state!r}: no auto edge can "
            "fire now or ripen with time. Add a @DWELL timed escape, or act on it manually."
        )
        self.case_id = case_id
        self.state = state
        self.candidates = candidates or []


class TriggerTimeout(Exception):
    """A trigger's work (`perform_<trigger>`) was HARD-ABORTED for outrunning its kill
    ceiling (the `~<dur>` soft timeout, or the default, times the kill multiple). Raised by
    the timing wrapper after asyncio.wait_for cancels the work; it is an ordinary Exception
    so it funnels through the machine's on_exception handler like any other pre-commit
    failure — but is recognized there and logged as CASE_TRIGGER_TIMEOUT (NOT
    CASE_FAIL_TRANSITION), keeping a timeout visually distinct in the event log.

    A timeout IS a failed pre-commit attempt: the case never left its source state, and it
    counts toward @FAIL (see _fail_count) so the retry cap applies and a timing-out trigger
    cannot hammer forever. advance() folds it into AdvanceResult.failed like any other
    absorbed failure.

    NOTE: aborting an async-native await cancels it cleanly; a call offloaded via
    run_blocking() cannot truly be killed (the thread runs on), so the abort frees the case
    but may leak the worker — prefer async-native clients for anything that can hang."""
    def __init__(self, case_id: str, trigger: Optional[str], state: str, *,
                 elapsed: float, ceiling: float):
        super().__init__(
            f"Case {case_id!r}: trigger {trigger!r} hard-aborted in state {state!r} after "
            f"{elapsed:.1f}s (kill ceiling {ceiling:.1f}s)."
        )
        self.case_id = case_id
        self.trigger = trigger
        self.state = state
        self.elapsed = elapsed
        self.ceiling = ceiling


class MissingFsmError(Exception):
    """Raised at first instantiation of a concrete FolderBackedCase subclass that defines
    no FSM at all — it neither declares `fsm_state_chains` nor overrides `compile_fsm()`
    to build a spec by hand, so its compiled `_fsm` has zero states.

    An empty spec is intentionally LEGAL for the base class and for abstract intermediates
    (which are never instantiated), so this omission cannot be caught at class-definition
    time — only at the first attempt to construct a concrete case. Distinct from its
    siblings: FsmChainParseError means the chains are malformed; FsmBindingError means the
    chains are fine but the carrier lacks (or mis-types) their methods; THIS means there is
    no state model to bind at all. The message names the corrective action."""
    def __init__(self, carrier_name: str):
        self.carrier_name = carrier_name
        super().__init__(
            f"{carrier_name!r} defines no FSM: declare `fsm_state_chains` "
            "(e.g. [\"^new--begin-->done^\"]) or override `compile_fsm()` to build an "
            "FsmChainSpec by hand."
        )


class FsmChainParseError(Exception):
    """Raised by StateChainParser when an `fsm_state_chains` entry cannot be parsed
    into a well-formed FSM. Carries the offending chain (and its index in the list,
    when known) plus a human-readable reason so a typo surfaces at class-definition
    time with enough context to fix it immediately."""
    def __init__(self, reason: str, *, chain: Optional[str] = None, index: Optional[int] = None):
        where = ""
        if index is not None:
            where += f" (chain #{index}"
            where += f": {chain!r})" if chain is not None else ")"
        elif chain is not None:
            where += f" (in {chain!r})"
        super().__init__(f"Cannot parse fsm_state_chains{where}: {reason}")
        self.reason = reason
        self.chain = chain
        self.index = index


class FsmBindingError(Exception):
    """Raised at first instantiation of a FolderBackedCase subclass when the class is not a
    suitable CARRIER for its compiled FSM. Where FsmChainParseError is about the state model
    being well-FORMED, this is about the model being well-BOUND to its host object. Both kinds
    of gap are reported together so a developer can fix them in a single pass:

      * MISSING — a method guard (or other explicitly-named transition callback) the FSM
        references is not defined on the class. This is almost always a typo in a chain's
        `guard#trigger`. A DSL guard token maps to a `guard_<token>` method (e.g. `funded#`
        => `guard_funded`), so that is the name reported as missing. Additionally,
        `perform_<trigger>` is REQUIRED when that trigger is reachable from an auto-advance
        edge (`--`), so unattended paths are explicit.
      * SYNC — a referenced callable exists but is synchronous while the case requires async.
        FolderBackedCase is driven through async (advance(), the generated triggers), so every
        guard, action method, and callback the FSM touches MUST be a coroutine function
        (`async def`). A stray `def` would silently block the event loop for every other case
        a driver is advancing, so we reject it loudly here. (Relax with force_async=False.)

    The whole point is a loud, early, unambiguous failure: future developers WILL write a sync
    guard or misspell a method name, and this turns a baffling event-loop stall or an
    AttributeError deep inside `transitions` into a precise message at construction time.

    Carries `carrier_name` and structured `missing` / `sync` / `orphaned` lists for
    programmatic inspection."""

    # transition-dict slot -> human label, for readable messages.
    _SLOT_LABEL = {
        "conditions": "method guard",
        "unless": "method guard ('unless')",
        "before": "before-callback",
        "after": "after-callback",
        "prepare": "prepare-callback",
        "hook": "trigger action method",
        "auto_hook": "auto-advance trigger action method",
    }

    def __init__(self, carrier_name: str, *, missing=None, sync=None, orphaned=None):
        self.carrier_name = carrier_name
        self.missing = list(missing or [])
        self.sync = list(sync or [])
        self.orphaned = list(orphaned or [])
        lines = [f"{carrier_name!r} is not a valid carrier for its FSM:"]
        for name, slot, trigger in self.missing:
            label = self._SLOT_LABEL.get(slot, slot)
            if slot == "auto_hook":
                lines.append(
                    f"  - missing {label} {name!r} for auto-advance trigger {trigger!r}; "
                    f"this trigger is reachable from a `--` edge, so define it explicitly "
                    f"(a no-op is fine): `async def {name}(self, event): ...`"
                )
            else:
                lines.append(
                    f"  - missing {label} {name!r} (referenced by trigger {trigger!r}); "
                    f"define `async def {name}(self, event)` on the class (check for a typo)"
                )
        for name, slot, trigger in self.sync:
            label = self._SLOT_LABEL.get(slot, slot)
            where = f" for trigger {trigger!r}" if trigger else ""
            lines.append(
                f"  - {label} {name!r}{where} is synchronous; declare it with 'async def' "
                "(this case is driven through async methods like advance())"
            )
        for name, kind, suffix in self.orphaned:
            lines.append(
                f"  - orphan hook-like method {name!r}: suffix {suffix!r} does not match any "
                f"known {kind}; rename/fix it or disable orphan detection for this check"
            )
        super().__init__("\n".join(lines))
