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
    CaseInFlightError,
    CaseTransitionInFlightError,
    UnregisteredCaseTypeError,
    CaseTypeMismatchError,
    RecordTypeMismatchError,
    IncompatibleReclassError,
    ReclassifyAssertionError,
    MissingFsmError,
    FsmChainParseError,
    FsmBindingError,
    AutoAdvanceBlocked,
    TriggerTimeout,
    CaseInvokedProcessError,
    UnconfiguredChokeError,
    PerformParamsError,
)
from .case_record import CaseRecord
from .case_read_protocol import CaseReadProtocol
from .case_journal import CaseEventJournal, CaseEventJournalView
from .case_assets import CaseAssets
from .asset_schema import AssetSpec
from .asset_dataclass_registry import AssetDataclassRegistry, asset_dataclass_registry
from .advance_result import AdvanceResult
from .state_chain_parser import StateChainParser, FsmChainSpec
from .case_type_registry import CaseTypeRegistry, case_type_registry
from .heartbeat_lease import (
    HeartbeatLease,
    LeaseAlreadyHeldError,
    LeaseOwnershipLostError,
    LeaseReleasedError,
)
from .case_type_spec import CaseTypeSpec
from .choke_permit_governor import (
    ChokeGrant,
    ChokeGrantError,
    ChokePermitGovernor,
    InvalidChokeLimitsError,
)

# The scheduling layer is resolved on first access rather than at package import.
# Those modules import FolderBackedCase, which imports this package — binding them
# eagerly here would close that cycle and break `import totodev_pub.case_manager`.
_LAZY_EXPORTS = {
    "CasePoolDriver": ".case_pool_driver",
    "CasePoolEvent": ".case_pool_driver",
    "CasePoolEventNames": ".case_pool_driver",
    "BalancedCasePoolDriver": ".balanced_case_pool_driver",
    "SeniorityCasePoolDriver": ".seniority_case_pool_driver",
    "PoolMembershipJournal": ".pool_membership_journal",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals().keys(), *_LAZY_EXPORTS])

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
    "CaseInFlightError",
    "CaseTransitionInFlightError",
    "UnregisteredCaseTypeError",
    "CaseTypeMismatchError",
    "RecordTypeMismatchError",
    "IncompatibleReclassError",
    "ReclassifyAssertionError",
    "MissingFsmError",
    "FsmChainParseError",
    "FsmBindingError",
    "AutoAdvanceBlocked",
    "TriggerTimeout",
    "CaseInvokedProcessError",
    "UnconfiguredChokeError",
    "PerformParamsError",
    "CaseRecord",
    "CaseReadProtocol",
    "CaseEventJournal",
    "CaseEventJournalView",
    "CaseAssets",
    "AssetSpec",
    "AssetDataclassRegistry",
    "asset_dataclass_registry",
    "AdvanceResult",
    "StateChainParser",
    "FsmChainSpec",
    "CaseTypeSpec",
    "CaseTypeRegistry",
    "case_type_registry",
    "HeartbeatLease",
    "LeaseAlreadyHeldError",
    "LeaseOwnershipLostError",
    "LeaseReleasedError",
    "ChokeGrant",
    "ChokeGrantError",
    "ChokePermitGovernor",
    "InvalidChokeLimitsError",
    "CasePoolDriver",
    "CasePoolEvent",
    "CasePoolEventNames",
    "BalancedCasePoolDriver",
    "SeniorityCasePoolDriver",
    "PoolMembershipJournal",
]
