# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Persisted CaseManager policy (Tiers 1 and 2)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.constants import (
    DEFAULT_ABERRANT_BUCKET,
    DEFAULT_ADOPT_DROP_SUBDIR,
    DEFAULT_ADOPT_MAILBOX_SUBDIR,
    DEFAULT_CASE_REF_PATH_TEMPLATE,
    DEFAULT_FIRE_MAILBOX_SUBDIR,
    DEFAULT_GROUPING_PATTERN,
    DEFAULT_LIVE_BUCKET,
    DEFAULT_MANAGER_NAMESPACE,
    DEFAULT_STAGING_SUBDIR,
    DEFAULT_TERMINAL_PREFIX,
    POLICY_SCHEMA_VERSION,
)


class CaseManagerPolicy(BaseModel, FileMappedPydanticMixin):
    """Authoritative deployment policy stored at .case_manager/case_manager_policy.yaml."""

    schema_version: int = POLICY_SCHEMA_VERSION

    # Tier 1 — layout facts
    grouping_pattern: str = DEFAULT_GROUPING_PATTERN
    live_bucket: str = DEFAULT_LIVE_BUCKET
    terminal_prefix: str = DEFAULT_TERMINAL_PREFIX
    aberrant_bucket: str = DEFAULT_ABERRANT_BUCKET
    case_ref_path_template: str = DEFAULT_CASE_REF_PATH_TEMPLATE
    manager_namespace: str = DEFAULT_MANAGER_NAMESPACE
    staging_subdir: str = DEFAULT_STAGING_SUBDIR
    adopt_drop_subdir: str = DEFAULT_ADOPT_DROP_SUBDIR
    fire_mailbox_subdir: str = DEFAULT_FIRE_MAILBOX_SUBDIR
    adopt_mailbox_subdir: str = DEFAULT_ADOPT_MAILBOX_SUBDIR

    # Tier 2 — operational tunables
    concurrency_ceiling: int = 50
    choke_limits: Dict[str, int] = Field(default_factory=dict)
    enable_mailbox: bool = True
    startup_adopt_scan: bool = False
    maintenance_interval_secs: float = 1.0
    result_ttl_secs: int = 86400
    manifest_stale_secs: int = 30
    staging_min_age_secs: int = 300
    staging_stale_lease_secs: int = 86400
    termination_max_retries: int = 3
    eject_max_retries: int = 3
    redundant_purge_terminal_after_secs: Optional[int] = 86400
    redundant_purge_aberrant_after_secs: Optional[int] = 86400
    escalation_fail_threshold: Optional[int] = None
    escalation_stall_secs: Optional[float] = None
    escalation_blocked: bool = False
    journal_attach_steady_state: bool = False
    journal_path: Optional[str] = None
    enable_fleet_status_board: bool = False
    fleet_status_refresh_interval_secs: float = 1.0
    fleet_status_terminal_retention_secs: float = 120.0

    @classmethod
    def tier1_field_names(cls) -> frozenset[str]:
        return frozenset({
            "schema_version",
            "grouping_pattern",
            "live_bucket",
            "terminal_prefix",
            "aberrant_bucket",
            "case_ref_path_template",
            "manager_namespace",
            "staging_subdir",
            "adopt_drop_subdir",
            "fire_mailbox_subdir",
            "adopt_mailbox_subdir",
        })

    @classmethod
    def tier2_field_names(cls) -> frozenset[str]:
        return frozenset({
            "concurrency_ceiling",
            "choke_limits",
            "enable_mailbox",
            "startup_adopt_scan",
            "maintenance_interval_secs",
            "result_ttl_secs",
            "manifest_stale_secs",
            "staging_min_age_secs",
            "staging_stale_lease_secs",
            "termination_max_retries",
            "eject_max_retries",
            "redundant_purge_terminal_after_secs",
            "redundant_purge_aberrant_after_secs",
            "escalation_fail_threshold",
            "escalation_stall_secs",
            "escalation_blocked",
            "journal_attach_steady_state",
            "journal_path",
            "enable_fleet_status_board",
            "fleet_status_refresh_interval_secs",
            "fleet_status_terminal_retention_secs",
        })

    def apply_tier2_overrides(self, **overrides: Any) -> "CaseManagerPolicy":
        """Return a copy with in-memory Tier 2 overrides (file untouched)."""
        data = self.model_dump()
        for key, value in overrides.items():
            if key in self.tier2_field_names():
                data[key] = value
        return CaseManagerPolicy.model_validate(data)
