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
    the registry — i.e. the class was never passed to register_case_type()."""
    def __init__(self, type_name: Optional[str]):
        super().__init__(
            f"Case type {type_name!r} is not in the FolderBackedCase registry. "
            "Call FolderBackedCase.register_case_type(YourClass) at startup."
        )
        self.type_name = type_name


class RecordTypeMismatchError(Exception):
    """Raised by _flush_record when the record's case_object_type doesn't match the
    class attempting the write. A FolderBackedCase may only ever write its OWN name.

    If you hit this inside reclassify_to(): the friction is DELIBERATE. Phase 2
    constructs the NEW class over a record that still carries the OLD name (so a
    crash mid-reclassify reopens cleanly as the old class). Before the committing
    flush you must CONSCIOUSLY stamp the new name (and migrate any new-schema
    fields, if the new record_cls added some):

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
    in the target class's _fsm_states list."""
    def __init__(self, current_state: str, target_class: str):
        super().__init__(
            f"Cannot reclassify to {target_class!r}: current state {current_state!r} "
            "is not in that class's _fsm_states."
        )
