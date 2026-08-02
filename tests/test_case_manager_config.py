# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Coverage for CaseManagerConfig, the merged policy + wiring view construction builds."""

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


def test_wiring_fields_default_to_empty(tmp_path):
    config = _minimal(tmp_path)
    assert config.tier2_overrides == {}
    assert config.driver is None
    assert config.driver_class is None
    assert config.driver_kwargs == {}
    assert config.registry is None
    assert config.register_types == ()
    assert config.notice_handlers == []


def test_mutable_defaults_are_not_shared_between_instances(tmp_path):
    first = _minimal(tmp_path)
    second = _minimal(tmp_path)
    first.driver_kwargs["policy"] = TierPolicy()
    first.notice_handlers.append(lambda e: None)
    assert second.driver_kwargs == {}
    assert second.notice_handlers == []


def test_construction_populates_the_config_from_policy_and_wiring(tmp_path):
    root = tmp_path / "cache"
    CaseManager.open_local_store(root)
    registry = CaseTypeRegistry()
    manager = CaseManager(
        root,
        registry=registry,
        register_types=[TicketCase],
        driver_class=SeniorityCasePoolDriver,
        concurrency_ceiling=7,
    )
    config = manager._config

    assert config.cache_root == root
    assert config.manager_dir == root / ".case_manager"
    assert config.policy_path.exists()
    assert config.registry is registry
    assert list(config.register_types) == [TicketCase]
    assert config.driver_class is SeniorityCasePoolDriver
    # Tier 2 overrides are applied to the in-memory policy and recorded as-is.
    assert config.tier2_overrides == {"concurrency_ceiling": 7}
    assert config.policy.concurrency_ceiling == 7


def test_driver_class_selects_the_driver(tmp_path):
    manager = provision_manager(tmp_path, driver_class=SeniorityCasePoolDriver)
    assert isinstance(manager._driver, SeniorityCasePoolDriver)


def test_default_driver_is_balanced(tmp_path):
    manager = provision_manager(tmp_path)
    assert type(manager._driver) is BalancedCasePoolDriver


def test_driver_kwargs_reach_the_driver(tmp_path):
    """The documented escape hatch for beat-tempo tunables."""
    tempo = TierPolicy(I0=0.5)
    manager = provision_manager(tmp_path, driver_kwargs={"policy": tempo})
    assert manager._driver._policy is tempo
    # concurrency_ceiling/choke_limits still default from CaseManagerPolicy.
    assert manager._driver._ceiling == manager._policy.concurrency_ceiling


def test_an_explicit_driver_instance_wins_over_driver_class(tmp_path):
    """An injected driver is empty, and a CasePoolDriver defines __len__ -- so a
    truthiness-based selection would silently build a default one instead."""
    driver = SeniorityCasePoolDriver()
    assert not driver, "precondition: a fresh driver is falsy"
    manager = provision_manager(tmp_path, driver=driver, driver_class=BalancedCasePoolDriver)
    assert manager._driver is driver
