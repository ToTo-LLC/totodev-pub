# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Coverage for CaseManagerConfig, the merged policy + bindings view construction builds."""

from pathlib import Path

import pytest

from case_manager_test_utils import TicketCase, provision_manager
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_config import CaseManagerConfig
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.folder_backed_case_support.balanced_case_pool_driver import (
    BalancedCasePoolDriver,
    TierPolicy,
)
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry,
    case_type_registry,
)
from totodev_pub.folder_backed_case_support.seniority_case_pool_driver import (
    SeniorityCasePoolDriver,
)


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


def _minimal(tmp_path: Path, **overrides) -> CaseManagerConfig:
    policy = CaseManagerPolicy()
    return CaseManagerConfig(
        cache_root=tmp_path,
        policy=policy,
        policy_path=tmp_path / ".case_manager" / "case_manager_policy.yaml",
        manager_dir=tmp_path / ".case_manager",
        **overrides,
    )


def test_bindings_fields_default_to_empty(tmp_path):
    config = _minimal(tmp_path)
    assert config.tunables_overrides == {}
    assert config.driver is None
    assert config.registry is None
    assert config.notice_handlers == []


def test_mutable_defaults_are_not_shared_between_instances(tmp_path):
    first = _minimal(tmp_path)
    second = _minimal(tmp_path)
    first.notice_handlers.append(lambda e: None)
    assert second.notice_handlers == []


def test_construction_populates_the_config_from_policy_and_bindings(tmp_path):
    root = tmp_path / "cache"
    CaseManager.open_local_store(root)
    registry = CaseTypeRegistry()
    registry.register_case_types(TicketCase)
    driver = SeniorityCasePoolDriver()
    manager = CaseManager(
        root,
        registry=registry,
        driver=driver,
        concurrency_ceiling=7,
    )
    config = manager._config

    assert config.cache_root == root
    assert config.manager_dir == root / ".case_manager"
    assert config.policy_path.exists()
    assert config.registry is registry
    assert config.driver is driver
    # Tunables overrides are applied to the in-memory policy and recorded as-is.
    assert config.tunables_overrides == {"concurrency_ceiling": 7}
    assert config.policy.concurrency_ceiling == 7


def test_default_driver_is_seniority(tmp_path):
    manager = provision_manager(tmp_path)
    assert type(manager._driver) is SeniorityCasePoolDriver
    assert manager._driver._ceiling == manager._policy.concurrency_ceiling


def test_an_explicit_driver_instance_is_kept_even_when_empty(tmp_path):
    """An injected driver is empty, and a CasePoolDriver defines __len__ -- so a
    truthiness-based selection would silently build a default one instead."""
    driver = BalancedCasePoolDriver(policy=TierPolicy(I0=0.5))
    assert not driver, "precondition: a fresh driver is falsy"
    manager = provision_manager(tmp_path, driver=driver)
    assert manager._driver is driver


def test_an_empty_registry_instance_is_kept(tmp_path):
    """Same truthiness trap as the driver: CaseTypeRegistry defines __len__."""
    root = tmp_path / "cache"
    CaseManager.open_local_store(root)
    registry = CaseTypeRegistry()
    assert not registry, "precondition: an empty registry is falsy"
    manager = CaseManager(root, registry=registry)
    assert manager._registry is registry
