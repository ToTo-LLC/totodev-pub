# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Support package for CaseManager."""

from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.case_store import (
    LIVE,
    QUARANTINED,
    TERMINATED,
    CaseEntry,
    LocalCaseStore,
)
from totodev_pub.case_manager_support.escalation import (
    CaseEscalation,
    CaseEscalationKind,
)
from totodev_pub.case_manager_support.exceptions import (
    AmbiguousExternalKeyError,
    CacheRootStateError,
    CaseLeaseHeldError,
    CaseManagerStopTimeoutError,
    CaseNotFoundError,
    CaseNotInStoreError,
    DuplicateCaseIdError,
    EjectAbandonedError,
    EjectTimeoutError,
    FleetStatusBoardDisabledError,
    InvalidAddressingError,
    LiveCaseNotFoundError,
    ManagerNotFreshError,
    ManagerNotRunningError,
    PolicyFileMissingError,
    PolicyMismatchError,
    RecoverRequiredError,
    StuckTrigger,
    UnknownCaseStatusError,
)
from totodev_pub.case_manager_support.layout import CaseLocation
from totodev_pub.case_manager_support.fleet_status import FleetStatusRow
from totodev_pub.case_manager_support.fleet_status_watcher import (
    FleetStatusBoardWatcher,
    FleetEvent,
    FleetEventKind,
)

__all__ = [
    # Storage boundary
    "CaseEntry",
    "CaseLocation",
    "LocalCaseStore",
    "LIVE",
    "QUARANTINED",
    "TERMINATED",
    # Policy and notices
    "CaseManagerPolicy",
    "CaseEscalation",
    "CaseEscalationKind",
    # Exceptions
    "AmbiguousExternalKeyError",
    "CacheRootStateError",
    "CaseLeaseHeldError",
    "CaseManagerStopTimeoutError",
    "CaseNotFoundError",
    "CaseNotInStoreError",
    "DuplicateCaseIdError",
    "EjectAbandonedError",
    "EjectTimeoutError",
    "FleetStatusBoardDisabledError",
    "InvalidAddressingError",
    "LiveCaseNotFoundError",
    "ManagerNotFreshError",
    "ManagerNotRunningError",
    "PolicyFileMissingError",
    "PolicyMismatchError",
    "RecoverRequiredError",
    "StuckTrigger",
    "UnknownCaseStatusError",
    # Fleet status board
    "FleetStatusBoardWatcher",
    "FleetEvent",
    "FleetEventKind",
    "FleetStatusRow",
]
