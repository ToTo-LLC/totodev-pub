# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Support package for CaseManager."""

from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.exceptions import (
    AmbiguousExternalKeyError,
    CacheRootStateError,
    CaseManagerStopTimeoutError,
    CaseNotFoundError,
    DuplicateCaseIdError,
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
)
from totodev_pub.case_manager_support.fleet_status import FleetStatusRow
from totodev_pub.case_manager_support.fleet_status_watcher import (
    FleetStatusBoardWatcher,
    FleetEvent,
    FleetEventKind,
)

__all__ = [
    "CaseManagerPolicy",
    "AmbiguousExternalKeyError",
    "CacheRootStateError",
    "CaseManagerStopTimeoutError",
    "CaseNotFoundError",
    "DuplicateCaseIdError",
    "EjectTimeoutError",
    "FleetStatusBoardDisabledError",
    "FleetStatusBoardWatcher",
    "FleetEvent",
    "FleetEventKind",
    "FleetStatusRow",
    "InvalidAddressingError",
    "LiveCaseNotFoundError",
    "ManagerNotFreshError",
    "ManagerNotRunningError",
    "PolicyFileMissingError",
    "PolicyMismatchError",
    "RecoverRequiredError",
    "StuckTrigger",
]
