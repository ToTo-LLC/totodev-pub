# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Persisted CaseManager policy (Layout and Tunables)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.constants import (
    DEFAULT_GROUPING_PATTERN,
    DEFAULT_INCOMING_SUBDIR,
    DEFAULT_LIVE_BUCKET,
    DEFAULT_MANAGER_NAMESPACE,
    DEFAULT_REQUESTS_SUBDIR,
    POLICY_SCHEMA_VERSION,
)


class CaseManagerPolicy(BaseModel, FileMappedPydanticMixin):
    """Authoritative deployment policy stored at .case_manager/case_manager_policy.yaml."""

    schema_version: int = POLICY_SCHEMA_VERSION

    # Layout — fixed once the filespace exists
    grouping_pattern: str = DEFAULT_GROUPING_PATTERN
    live_bucket: str = DEFAULT_LIVE_BUCKET
    manager_namespace: str = DEFAULT_MANAGER_NAMESPACE
    incoming_subdir: str = DEFAULT_INCOMING_SUBDIR
    requests_subdir: str = DEFAULT_REQUESTS_SUBDIR

    # Tunables — operational settings (file holds defaults; process may override)
    concurrency_ceiling: int = 50
    choke_limits: Dict[str, int] = Field(default_factory=dict)
    enable_mailbox: bool = True
    startup_incoming_scan: bool = False
    maintenance_interval_secs: float = 1.0
    result_ttl_secs: int = 86400
    manifest_stale_secs: int = 30
    incoming_min_age_secs: int = 300
    incoming_stale_lease_secs: int = 86400
    termination_max_retries: int = 3
    eject_max_retries: int = 3
    redundant_purge_terminal_after_secs: Optional[int] = 86400
    redundant_purge_quarantined_after_secs: Optional[int] = 86400
    escalation_fail_threshold: Optional[int] = None
    escalation_stall_secs: Optional[float] = None
    escalation_blocked: bool = False
    journal_attach_steady_state: bool = False
    journal_path: Optional[str] = None
    enable_fleet_status_board: bool = True
    fleet_status_full_flush_interval_secs: float = 1.0
    fleet_status_terminal_retention_secs: float = 120.0
    watchdog_enabled: bool = True
    watchdog_action: str = "exit"  # "exit" | "alarm_only"
    watchdog_pulse_stuck_secs: Optional[float] = None
    watchdog_mailbox_stale_secs: Optional[float] = None  # None → derived; 0 → disabled
    watchdog_tick_warn_secs: Optional[float] = None
    # Recovery always logs what it could not restore. This decides whether it also
    # refuses to hand back a manager. Off in production, where one unrestorable
    # case must not ground a healthy fleet; on in dev and test, where the same
    # case is a defect that should stop the build rather than scroll past.
    strict_recovery: bool = False

    @classmethod
    def layout_field_names(cls) -> frozenset[str]:
        return frozenset({
            "schema_version",
            "grouping_pattern",
            "live_bucket",
            "manager_namespace",
            "incoming_subdir",
            "requests_subdir",
        })

    @classmethod
    def tunables_field_names(cls) -> frozenset[str]:
        return frozenset({
            "concurrency_ceiling",
            "choke_limits",
            "enable_mailbox",
            "startup_incoming_scan",
            "maintenance_interval_secs",
            "result_ttl_secs",
            "manifest_stale_secs",
            "incoming_min_age_secs",
            "incoming_stale_lease_secs",
            "termination_max_retries",
            "eject_max_retries",
            "redundant_purge_terminal_after_secs",
            "redundant_purge_quarantined_after_secs",
            "escalation_fail_threshold",
            "escalation_stall_secs",
            "escalation_blocked",
            "journal_attach_steady_state",
            "journal_path",
            "enable_fleet_status_board",
            "fleet_status_full_flush_interval_secs",
            "fleet_status_terminal_retention_secs",
            "watchdog_enabled",
            "watchdog_action",
            "watchdog_pulse_stuck_secs",
            "watchdog_mailbox_stale_secs",
            "watchdog_tick_warn_secs",
            "strict_recovery",
        })

    def apply_tunables_overrides(self, **overrides: Any) -> "CaseManagerPolicy":
        """Return a copy with in-memory Tunables overrides (file untouched)."""
        data = self.model_dump()
        for key, value in overrides.items():
            if key in self.tunables_field_names():
                data[key] = value
        return CaseManagerPolicy.model_validate(data)
