# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Support package for CaseManager.

Every name below is resolved on first access rather than at package import, using
the same ``_LAZY_EXPORTS`` idiom as ``folder_backed_case_support``. Two reasons it
matters more here than convenience:

- ``case_manager.py`` imports this package's *submodules*, so Python executes this
  file first on every CaseManager import. Anything bound here is unconditional for
  every user, whether or not they touch it. Fleet status is an opt-in observability
  feature and should not be charged to a manager that never publishes a board.
- ``fleet_status_board`` imports ``folder_backed_case_support.case_pool_driver``,
  which that package deliberately defers because it closes a cycle back through
  ``FolderBackedCase``. Binding it eagerly here reaches around a guard the sibling
  package put up on purpose.
"""

_LAZY_EXPORTS = {
    # Policy
    "CaseManagerPolicy": ".case_manager_policy",
    # Storage boundary
    "CaseEntry": ".case_store",
    "LocalCaseStore": ".case_store",
    "LIVE": ".case_store",
    "QUARANTINED": ".case_store",
    "TERMINATED": ".case_store",
    "CaseLocation": ".layout",
    # Notices
    "CaseNotice": ".notice",
    "CaseNoticeKind": ".notice",
    # Exceptions
    "AmbiguousExternalKeyError": ".exceptions",
    "CacheRootStateError": ".exceptions",
    "CaseLeaseHeldError": ".exceptions",
    "CaseManagerStopTimeoutError": ".exceptions",
    "CaseNotFoundError": ".exceptions",
    "CaseNotInStoreError": ".exceptions",
    "DuplicateCaseIdError": ".exceptions",
    "EjectAbandonedError": ".exceptions",
    "EjectTimeoutError": ".exceptions",
    "InvalidAddressingError": ".exceptions",
    "LiveCaseNotFoundError": ".exceptions",
    "ManagerNotFreshError": ".exceptions",
    "ManagerNotRunningError": ".exceptions",
    "PolicyFileMissingError": ".exceptions",
    "PolicyMismatchError": ".exceptions",
    "RecoverRequiredError": ".exceptions",
    "StuckTrigger": ".exceptions",
    "UnknownCaseStatusError": ".exceptions",
    # Fleet status board (opt-in observability)
    "FleetStatusRow": ".fleet_status",
    "FleetStatusBoard": ".fleet_status_board",
    "FleetStatusBoardWatcher": ".fleet_status_watcher",
    "FleetEvent": ".fleet_status_events",
    "FleetEventKind": ".fleet_status_events",
    "diff_rows": ".fleet_status_events",
    "diff_snapshots": ".fleet_status_events",
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
    # Storage boundary
    "CaseEntry",
    "CaseLocation",
    "LocalCaseStore",
    "LIVE",
    "QUARANTINED",
    "TERMINATED",
    # Policy and notices
    "CaseManagerPolicy",
    "CaseNotice",
    "CaseNoticeKind",
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
    "FleetStatusBoard",
    "FleetStatusBoardWatcher",
    "FleetEvent",
    "FleetEventKind",
    "FleetStatusRow",
    "diff_rows",
    "diff_snapshots",
]
