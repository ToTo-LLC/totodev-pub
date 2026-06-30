# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseReadView: structural typing Protocol for lock-free case reads."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
    from totodev_pub.folder_backed_case_support.case_event_log_reader import (
        CaseEventLogReader,
    )


class CaseReadView(Protocol):
    """Read-only surface shared by live FolderBackedCase and FolderBackedCaseReader.

    Type-checking only — no runtime isinstance checks required. Both implementers
    may differ on edge-case semantics (e.g. closed detection) while exposing the
    same property names and types.
    """

    @property
    def case_id(self) -> str: ...

    @property
    def case_external_key(self) -> str | None: ...

    @property
    def case_nickname(self) -> str | None: ...

    @property
    def case_object_type(self) -> str: ...

    @property
    def case_folder(self) -> Path: ...

    @property
    def case_state(self) -> str | None: ...

    @property
    def case_is_open(self) -> bool: ...

    @property
    def case_is_closed(self) -> bool: ...

    @property
    def case_created(self) -> datetime.datetime: ...

    @property
    def case_closed_at(self) -> datetime.datetime | None: ...

    @property
    def case_dwell_secs(self) -> float: ...

    @property
    def case_last_activity(self) -> datetime.datetime | None: ...

    @property
    def case_transition_fail_count(self) -> int: ...

    @property
    def case_assets(self) -> CaseAssets: ...

    @property
    def case_events(self) -> CaseEventLogReader: ...
