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
            "Wait for the current owner to release() or for the lease to expire."
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


class ReleasedCaseError(Exception):
    """A mutating operation was attempted on a case husk that has already release()d its
    lease (detached). Construct a fresh instance (rehydrate) to act on the folder again."""
    def __init__(self, folder: Path):
        super().__init__(
            f"This FolderBackedCase for {folder} has already been released. "
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
    (un-starred) transitions may still be perfectly available, which is why it is "auto
    advance" blocked, not "all transitions" blocked.

    USAGE: advance() does NOT raise this — it CARRIES it in AdvanceResult.exceptions as
    data, so a blind driver can inspect it without a try/except (a direct/manual trigger
    call, having no AdvanceResult to return, still raises). It is deterministic and
    idempotent: the same state yields the same block on every call until something changes.

    REMEDY: give the state a timed escape, e.g. `--*@DWELL>{N}h#timeout-->somewhere`, or a
    blanket net like `*--*@DWELL>=2d#timeout-->expired^`; or resolve/route it manually.
    """
    def __init__(self, case_id: str, state: str, *, candidates: Optional[list] = None):
        super().__init__(
            f"Case {case_id!r} is auto-advance blocked in state {state!r}: no auto edge can "
            "fire now or ripen with time. Add a @DWELL timed escape, or act on it manually."
        )
        self.case_id = case_id
        self.state = state
        self.candidates = candidates or []


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
