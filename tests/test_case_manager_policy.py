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
