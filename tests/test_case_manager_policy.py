# Part of the totodev_pub library.

from pathlib import Path

import pytest

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.exceptions import (
    CacheRootStateError,
    PolicyFileMissingError,
    PolicyMismatchError,
)
from totodev_pub.folder_backed_case import FolderBackedCase


def test_policy_defaults():
    p = CaseManagerPolicy()
    assert p.live_bucket == "live"
    assert p.terminal_prefix == "terminal"
    assert p.concurrency_ceiling == 50


def test_archive_grouping_label_default(tmp_path):
    import re
    from case_manager_test_utils import TicketCase

    case = TicketCase.create_case_in_folder(tmp_path / "c")
    label = case.archive_grouping_label()
    assert re.match(r"^\d{4}-\d{2}$", label)
    case.case_detach()


def test_open_local_store_fresh_root(tmp_path):
    root = tmp_path / "cache"
    CaseManager.open_local_store(root)
    assert (root / ".case_manager" / "case_manager_policy.yaml").exists()
    assert (root / ".cached_file_folders.json").exists()


def test_open_local_store_idempotent(tmp_path):
    s1 = CaseManager.open_local_store(tmp_path / "cache")
    s2 = CaseManager.open_local_store(tmp_path / "cache")
    assert s1.root_dir == s2.root_dir


def test_open_local_store_rejects_foreign_data(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    (root / "foreign.txt").write_text("data")
    with pytest.raises(CacheRootStateError):
        CaseManager.open_local_store(root)


def test_construction_will_not_create_a_filespace(tmp_path):
    with pytest.raises(PolicyFileMissingError):
        CaseManager(tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_init_if_new_false_refuses_an_absent_filespace(tmp_path):
    with pytest.raises(PolicyFileMissingError):
        CaseManager.open_local_store(tmp_path / "cache", init_if_new=False)


def test_tier1_override_mismatch(tmp_path):
    CaseManager.open_local_store(tmp_path / "cache", live_bucket="live")
    with pytest.raises(PolicyMismatchError) as excinfo:
        CaseManager.open_local_store(tmp_path / "cache", live_bucket="other")
    assert "live_bucket" in str(excinfo.value)


def test_tier2_override_is_never_a_mismatch(tmp_path):
    """Tier 2 is a tunable: the record holds a default, not the only value."""
    root = tmp_path / "cache"
    CaseManager.open_local_store(root, concurrency_ceiling=7)
    store = CaseManager.open_local_store(root, concurrency_ceiling=9)
    assert store.policy.concurrency_ceiling == 9
    assert CaseManager(root, concurrency_ceiling=9)._policy.concurrency_ceiling == 9
    # ...and the record still carries the default it was created with.
    persisted = CaseManagerPolicy.load(
        str(root / ".case_manager" / "case_manager_policy.yaml"), acquire_lock=False
    )
    assert persisted.concurrency_ceiling == 7


def test_unknown_policy_field_is_rejected(tmp_path):
    with pytest.raises(TypeError, match="concurency_ceiling"):
        CaseManager.open_local_store(tmp_path / "cache", concurency_ceiling=9)


def test_a_stores_own_tuning_survives_into_the_manager(tmp_path):
    """A store is the filespace resolved, so its policy wins over the record.

    Re-reading the record here would discard exactly the tuning the caller opened
    the store to set.
    """
    root = tmp_path / "cache"
    CaseManager.open_local_store(root)  # record keeps the default
    tuned = CaseManager.open_local_store(root, concurrency_ceiling=9)
    assert CaseManager(tuned)._policy.concurrency_ceiling == 9


def test_watchdog_and_shutdown_policy_fields():
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

    policy = CaseManagerPolicy()
    assert policy.shutdown_mailbox_subdir == "shutdown_mailbox"
    assert policy.watchdog_enabled is True
    assert policy.watchdog_action == "exit"
    assert policy.watchdog_pulse_stuck_secs is None
    assert policy.watchdog_mailbox_stale_secs is None
    assert policy.watchdog_tick_warn_secs is None
    assert "shutdown_mailbox_subdir" in CaseManagerPolicy.tier1_field_names()
    for name in (
        "watchdog_enabled",
        "watchdog_action",
        "watchdog_pulse_stuck_secs",
        "watchdog_mailbox_stale_secs",
        "watchdog_tick_warn_secs",
    ):
        assert name in CaseManagerPolicy.tier2_field_names()
    tuned = policy.apply_tier2_overrides(watchdog_pulse_stuck_secs=2.5)
    assert tuned.watchdog_pulse_stuck_secs == 2.5
