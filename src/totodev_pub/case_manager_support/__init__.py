# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Support package for CaseManager."""

from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.exceptions import (
    AmbiguousExternalKeyError,
    CaseNotFoundError,
    DuplicateCaseIdError,
    FleetStatusBoardDisabledError,
    LiveCaseNotFoundError,
    ManagerNotFreshError,
    PolicyFileMissingError,
    RecoverRequiredError,
)
from totodev_pub.case_manager_support.fleet_status import FleetStatusRow
from totodev_pub.case_manager_support.fleet_watcher import (
    FleetBoardWatcher,
    FleetEvent,
    FleetEventKind,
)

__all__ = [
    "CaseManagerPolicy",
    "AmbiguousExternalKeyError",
    "CaseNotFoundError",
    "DuplicateCaseIdError",
    "FleetStatusBoardDisabledError",
    "FleetBoardWatcher",
    "FleetEvent",
    "FleetEventKind",
    "FleetStatusRow",
    "LiveCaseNotFoundError",
    "ManagerNotFreshError",
    "PolicyFileMissingError",
    "RecoverRequiredError",
]
