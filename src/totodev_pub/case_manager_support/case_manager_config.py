# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""In-memory merged CaseManager configuration (policy Layout/Tunables + Bindings)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case_support.case_pool_driver import CasePoolDriver
    from totodev_pub.folder_backed_case_support.case_type_registry import CaseTypeRegistry


@dataclass
class CaseManagerConfig:
    """Merged view built during construction: persisted policy + Bindings."""

    cache_root: Path
    policy: CaseManagerPolicy
    policy_path: Path
    manager_dir: Path
    tunables_overrides: dict[str, Any] = field(default_factory=dict)
    driver: "CasePoolDriver | None" = None
    registry: "CaseTypeRegistry | None" = None
    notice_handlers: list[Callable[..., None]] = field(default_factory=list)
