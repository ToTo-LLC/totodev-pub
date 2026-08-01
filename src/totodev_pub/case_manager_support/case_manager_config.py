# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""In-memory merged CaseManager configuration (policy + wiring)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, TYPE_CHECKING

from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase
    from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolDriver
    from totodev_pub.folder_backed_case_support.case_type_registry import CaseTypeRegistry


@dataclass
class CaseManagerConfig:
    """Merged view built by attach(): persisted policy + Tier 3 wiring."""

    cache_root: Path
    policy: CaseManagerPolicy
    policy_path: Path
    manager_dir: Path
    tier2_overrides: dict[str, Any] = field(default_factory=dict)
    driver: "CasePoolDriver | None" = None
    driver_class: type | None = None
    # Passed as-is to driver_class(**driver_kwargs) by _build_default_driver(). For
    # BalancedCasePoolDriver / SeniorityCasePoolDriver this is the only way to override
    # beat-tempo tunables (I0, EAGER_BEAT_FRACTION, BEAT_YIELD_FLOOR, ...): pass
    # {"policy": TierPolicy(...)} — concurrency_ceiling/choke_limits are defaulted
    # from CaseManagerPolicy automatically and need not be repeated here.
    driver_kwargs: dict[str, Any] = field(default_factory=dict)
    registry: "CaseTypeRegistry | None" = None
    register_types: Sequence[type["FolderBackedCase"]] = ()
    notice_handlers: list[Callable[..., None]] = field(default_factory=list)
