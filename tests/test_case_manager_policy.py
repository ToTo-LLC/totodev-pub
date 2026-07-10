# Part of the totodev_pub library.

from pathlib import Path

import pytest

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.exceptions import CacheRootStateError, PolicyMismatchError
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


def test_provision_fresh_root(tmp_path):
    root = tmp_path / "cache"
    CaseManager.provision(root)
    assert (root / ".case_manager" / "case_manager_policy.yaml").exists()
    assert (root / ".cached_file_folders.json").exists()


def test_open_idempotent(tmp_path):
    m1 = CaseManager.open(tmp_path / "cache")
    m2 = CaseManager.open(tmp_path / "cache")
    assert m1._cache_root == m2._cache_root


def test_open_rejects_foreign_data(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    (root / "foreign.txt").write_text("data")
    with pytest.raises(CacheRootStateError):
        CaseManager.open(root)


def test_tier1_override_mismatch(tmp_path):
    CaseManager.provision(tmp_path / "cache", live_bucket="live")
    with pytest.raises(PolicyMismatchError):
        CaseManager.attach(tmp_path / "cache", live_bucket="other")


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
