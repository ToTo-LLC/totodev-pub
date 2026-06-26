# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Support package for FolderBackedCase: the tightly-coupled classes, exceptions,
constants, and helpers that the main module composes. Import the pieces from here
(or, for the common names, from totodev_pub.folder_backed_case which re-exports them)."""

from .constants import RECORD_NAME, LEASE_NAME, ASSETS_DIR_NAME, KEEP_LIST_NAME
from .exceptions import (
    CaseAlreadyOpenError,
    OwnershipLostError,
    DetachedCaseError,
    UnregisteredCaseTypeError,
    RecordTypeMismatchError,
    IncompatibleReclassError,
    MissingFsmError,
    FsmChainParseError,
    FsmBindingError,
    AutoAdvanceBlocked,
    TriggerTimeout,
)
from .case_record import CaseRecord
from .case_event_log_reader import CaseEventLogReader
from .case_assets import CaseAssets
from .advance_result import AdvanceResult
from .state_chain_parser import StateChainParser, FsmChainSpec

__all__ = [
    "RECORD_NAME",
    "LEASE_NAME",
    "ASSETS_DIR_NAME",
    "KEEP_LIST_NAME",
    "CaseAlreadyOpenError",
    "OwnershipLostError",
    "DetachedCaseError",
    "UnregisteredCaseTypeError",
    "RecordTypeMismatchError",
    "IncompatibleReclassError",
    "MissingFsmError",
    "FsmChainParseError",
    "FsmBindingError",
    "AutoAdvanceBlocked",
    "TriggerTimeout",
    "CaseRecord",
    "CaseEventLogReader",
    "CaseAssets",
    "AdvanceResult",
    "StateChainParser",
    "FsmChainSpec",
]
