# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseReadProtocol: structural typing Protocol for lock-free case reads."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
    from totodev_pub.folder_backed_case_support.case_journal import (
        CaseEventJournalView,
    )


class CaseReadProtocol(Protocol):
    """Read-only surface shared by live FolderBackedCase and FolderBackedCaseReader.

    Type-checking only — no runtime isinstance checks required. Both implementers
    may differ on edge-case semantics (e.g. terminal detection) while exposing the
    same property names and types.

    DELIBERATE EXCLUSIONS:

    * Operational-liveness reads ``case_lease_secs_left`` and ``case_active_trigger``
      live ONLY on FolderBackedCaseReader. Their meaning does not translate cleanly
      to a live case (its own self-beaten lease; a trigger it is itself executing —
      which a live driver already learns synchronously via AdvanceResult.trigger),
      so putting them here would be an LSP smell.

    * Record-identity fields (``nickname``, ``case_object_type``, ``created``,
      ``terminal``, ``terminal_state``) live on CaseRecord. The reader may wrap them
      for closed-case inspection; the live case does not — use
      ``case_record()`` / ``peek_case_record()`` instead.

    * There is no last-activity timestamp here. A caller that needs "last touched"
      can read ``case_event_journal.last_activity_at`` (naive-local mtime of the
      newest event) and fall back to the record's ``created`` for an empty log.
    """

    @property
    def case_id(self) -> str: ...

    @property
    def case_external_key(self) -> str | None: ...

    @property
    def case_folder(self) -> Path: ...

    @property
    def case_state(self) -> str | None: ...

    @property
    def case_is_live(self) -> bool: ...

    @property
    def case_is_terminal(self) -> bool: ...

    @property
    def case_dwell_secs(self) -> float: ...

    @property
    def case_transition_fail_count(self) -> int: ...

    @property
    def case_assets(self) -> CaseAssets: ...

    def case_load_asset(self, alias: str) -> object: ...

    def case_load_assets(self, alias: str) -> list: ...

    @property
    def case_event_journal(self) -> CaseEventJournalView: ...
