# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Exception types raised by CaseManager and CaseManagerClient."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CacheRootStateError(Exception):
    """cache_root exists, is non-empty, and lacks a provisioned policy file."""

    def __init__(self, cache_root: Path, *, detail: str = ""):
        msg = (
            f"Cannot use cache root {cache_root}: directory exists and is non-empty "
            "but has no CaseManager policy file. Provision explicitly or use an empty root."
        )
        if detail:
            msg = f"{msg} {detail}"
        super().__init__(msg)
        self.cache_root = cache_root


class PolicyFileMissingError(Exception):
    """Policy file expected but absent or unreadable."""

    def __init__(self, policy_path: Path):
        super().__init__(
            f"CaseManager policy file missing or unreadable at {policy_path}. "
            "Call CaseManager.provision() first."
        )
        self.policy_path = policy_path


class PolicyMismatchError(Exception):
    """Explicit Tier 1 override disagrees with the persisted policy file."""

    def __init__(self, field_name: str, *, file_value: Any, override_value: Any):
        super().__init__(
            f"Tier 1 policy field {field_name!r} override {override_value!r} "
            f"does not match persisted value {file_value!r} in case_manager_policy.yaml."
        )
        self.field_name = field_name
        self.file_value = file_value
        self.override_value = override_value


class CaseNotFoundError(Exception):
    """Requested case_id is not present in the managed deployment."""

    def __init__(self, case_id: str, *, scope: str = "managed"):
        super().__init__(f"Case {case_id!r} not found in {scope} groupings.")
        self.case_id = case_id
        self.scope = scope


class LiveCaseNotFoundError(CaseNotFoundError):
    """case_id is not in the live pool."""

    def __init__(self, case_id: str):
        super().__init__(case_id, scope="live pool")
        self.case_id = case_id


class AmbiguousExternalKeyError(Exception):
    """external_key matches more than one case."""

    def __init__(self, external_key: str, *, case_ids: list[str]):
        super().__init__(
            f"external_key {external_key!r} is ambiguous — matches case_ids: {case_ids}. "
            "Use locate_all() or readers_by_external_key()."
        )
        self.external_key = external_key
        self.case_ids = case_ids


class DuplicateCaseIdError(Exception):
    """Adopt rejected: case_id already exists in a managed grouping."""

    def __init__(self, case_id: str, *, existing_grouping: str | None = None):
        msg = f"case_id {case_id!r} already exists in managed storage."
        if existing_grouping:
            msg = f"{msg} Found in grouping {existing_grouping!r}."
        super().__init__(msg)
        self.case_id = case_id
        self.existing_grouping = existing_grouping


class ManagerNotFreshError(Exception):
    """Manager heartbeat stale or stopped_at set while only_if_fresh=True."""

    def __init__(self, *, stopped_at: str | None = None, heartbeat_at: str | None = None):
        super().__init__(
            "CaseManager is not fresh (stopped or stale heartbeat). "
            "Pass only_if_fresh=False to submit intentionally."
        )
        self.stopped_at = stopped_at
        self.heartbeat_at = heartbeat_at


@dataclass(frozen=True)
class StuckTrigger:
    case_id: str
    trigger: str
    elapsed_secs: float
    case_folder: Path


class CaseManagerStopTimeoutError(Exception):
    """stop(timeout=…) expired before all in-flight triggers settled."""

    def __init__(
        self,
        *,
        timeout_secs: float,
        stuck: list[StuckTrigger],
        detail: dict[str, Any] | None = None,
    ):
        self.timeout_secs = timeout_secs
        self.stuck = stuck
        self.detail = detail or {}
        parts = [
            f"stop() timed out after {timeout_secs}s:",
            *(
                f" trigger {s.trigger!r} for case_id {s.case_id!r} still working "
                f"({s.elapsed_secs:.1f}s in process)"
                for s in stuck
            ),
        ]
        super().__init__("\n".join(parts) if stuck else parts[0])


class EjectTimeoutError(Exception):
    """eject_from_pool(timeout=…) expired before export completed."""

    def __init__(self, *, case_id: str, stuck: list[StuckTrigger]):
        self.case_id = case_id
        self.stuck = stuck
        super().__init__(f"eject_from_pool timed out for case_id {case_id!r}.")


class InvalidAddressingError(Exception):
    """Lookup or fire request must specify exactly one identifier."""

    def __init__(self, message: str = "Provide exactly one of case_id or case_folder."):
        super().__init__(message)


class RecoverRequiredError(Exception):
    """start() was called before recover() ran this session.

    recover() is the ownership-and-membership gate: it rebuilds the live pool from
    disk (lease-aware), replays durable queues, and detects a competing live manager.
    Starting the beat loop without it would silently schedule an empty pool. Call
    ``await manager.recover()`` first (inspect its RecoverReport), then ``start()``."""

    def __init__(self) -> None:
        super().__init__(
            "CaseManager.start() requires a prior recover() this session. "
            "Call `await manager.recover()` before `await manager.start()` "
            "(recover() rebuilds the pool from disk and is where a competing live "
            "manager is detected). Its RecoverReport is then on manager.last_recover_report."
        )
