# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Support package for FolderBackedCase: the tightly-coupled classes, exceptions,
constants, and helpers that the main module composes. Import the pieces from here
(or, for the common names, from totodev_pub.folder_backed_case which re-exports them)."""

from .constants import (
    RECORD_NAME,
    LEASE_NAME,
    ASSETS_DIR_NAME,
    KEEP_LIST_NAME,
    LOGS_DIR_NAME,
    CASE_BASE_EVENT_PREFIX,
)
from .case_logging import (
    LogRetention,
    set_case_log_retention,
    get_case_log_retention,
)
from .exceptions import (
    CaseAlreadyOpenError,
    OwnershipLostError,
    DetachedCaseError,
    UnregisteredCaseTypeError,
    CaseTypeMismatchError,
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
from .case_journal import CaseJournal
from .case_assets import CaseAssets
from .advance_result import AdvanceResult
from .state_chain_parser import StateChainParser, FsmChainSpec
from .case_type_registry import CaseTypeRegistry, case_type_registry
from .heartbeat_lease import (
    HeartbeatLease,
    LeaseAlreadyHeldError,
    LeaseOwnershipLostError,
    LeaseReleasedError,
)

__all__ = [
    "RECORD_NAME",
    "LEASE_NAME",
    "ASSETS_DIR_NAME",
    "KEEP_LIST_NAME",
    "LOGS_DIR_NAME",
    "CASE_BASE_EVENT_PREFIX",
    "LogRetention",
    "set_case_log_retention",
    "get_case_log_retention",
    "CaseAlreadyOpenError",
    "OwnershipLostError",
    "DetachedCaseError",
    "UnregisteredCaseTypeError",
    "CaseTypeMismatchError",
    "RecordTypeMismatchError",
    "IncompatibleReclassError",
    "MissingFsmError",
    "FsmChainParseError",
    "FsmBindingError",
    "AutoAdvanceBlocked",
    "TriggerTimeout",
    "CaseRecord",
    "CaseEventLogReader",
    "CaseJournal",
    "CaseAssets",
    "AdvanceResult",
    "StateChainParser",
    "FsmChainSpec",
    "CaseTypeRegistry",
    "case_type_registry",
    "HeartbeatLease",
    "LeaseAlreadyHeldError",
    "LeaseOwnershipLostError",
    "LeaseReleasedError",
]
