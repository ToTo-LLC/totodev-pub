# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the per-case folder-logging tee (logs/case.log).

Covers: the tee itself, always-on across every live-instance path, the reserved
artifact guard, the default-PURGE / RETAIN closure policy, layout isolation from
the asset playground, and resource frugality (no idle file descriptors, no
process-global logger-registry growth).
"""

import asyncio
import logging
import os

import pytest

from totodev_pub.folder_backed_case import (
    FolderBackedCase,
    LogRetention,
    set_case_log_retention,
    LOGS_DIR_NAME,
)
from totodev_pub.folder_backed_case_support import get_case_log_retention
from totodev_pub.folder_backed_case_support.constants import (
    LOG_FILE_NAME,
    LOG_PURGE_SENTINEL,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


# ---------------------------------------------------------------------------
# Concrete subclasses used across the tests
# ---------------------------------------------------------------------------

class LogCase(FolderBackedCase):

    asset_schema = {}
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]


class LogReclassTarget(FolderBackedCase):


    asset_schema = {}
    """Shares the 'new' state with LogCase so reclassify from a fresh case is legal."""
    fsm_state_chains = ["^new==go-->finished^"]


# ---------------------------------------------------------------------------
# Isolation fixtures: both the retention policy and the registry are process-wide
# mutable state, so snapshot/restore them around every test.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_log_retention():
    saved = get_case_log_retention()
    try:
        yield
    finally:
        set_case_log_retention(saved)


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


def _log_path(folder):
    return folder / LOGS_DIR_NAME / LOG_FILE_NAME


# ---------------------------------------------------------------------------
# Tee
# ---------------------------------------------------------------------------

def test_tee_to_both_root_and_file(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    folder = tmp_path / "tee"
    with LogCase.create_case_in_folder(folder, case_id="tee-1") as case:
        case.log.info("tee-marker-xyz")
        # Propagated to root (caplog attaches at root).
        assert any("tee-marker-xyz" in r.getMessage() for r in caplog.records)
        # And mirrored to the per-case file.
        contents = _log_path(folder).read_text(encoding="utf-8")
        assert "tee-marker-xyz" in contents
        # The file line is self-identifying (case id + type stamped by the filter).
        assert "tee-1" in contents
        assert "LogCase" in contents


# ---------------------------------------------------------------------------
# Always-on across every live-instance path
# ---------------------------------------------------------------------------

def test_logging_on_for_create(tmp_path):
    folder = tmp_path / "create"
    with LogCase.create_case_in_folder(folder, case_id="c-1"):
        assert _log_path(folder).exists()
        assert _log_path(folder).read_text(encoding="utf-8").strip() != ""


def test_logging_on_for_rehydrate(tmp_path):
    case_type_registry.register_case_types(LogCase)
    folder = tmp_path / "rehydrate"
    with LogCase.create_case_in_folder(folder, case_id="c-2"):
        pass
    set_case_log_retention(LogRetention.RETAIN)  # keep contents across the closed-but-open reopen
    with case_type_registry.rehydrate(folder) as case:
        case.log.info("rehydrated-marker")
        assert "rehydrated-marker" in _log_path(folder).read_text(encoding="utf-8")


def test_logging_on_for_reclassify(tmp_path):
    folder = tmp_path / "reclass"
    case = LogCase.create_case_in_folder(folder, case_id="c-3")
    fresh = case.case_reclassify_to(LogReclassTarget)
    try:
        fresh.log.info("reclassified-marker")
        contents = _log_path(folder).read_text(encoding="utf-8")
        assert "reclassified-marker" in contents
        assert "LogReclassTarget" in contents
    finally:
        fresh.case_detach()


# ---------------------------------------------------------------------------
# Reserved-artifact guard
# ---------------------------------------------------------------------------

def test_logs_dir_is_reserved_artifact(tmp_path):
    folder = tmp_path / "preseeded"
    (folder / LOGS_DIR_NAME).mkdir(parents=True)
    with pytest.raises(FileExistsError):
        LogCase.create_case_in_folder(folder, case_id="c-4")


# ---------------------------------------------------------------------------
# Closure retention policy
# ---------------------------------------------------------------------------

def test_purge_default_on_close(tmp_path):
    folder = tmp_path / "purge"
    with LogCase.create_case_in_folder(folder, case_id="c-5") as case:
        case.log.info("should-be-purged-marker")
        asyncio.run(case.begin())
        asyncio.run(case.finish())
        assert case.case_is_closed
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert contents.strip() == LOG_PURGE_SENTINEL
    assert "should-be-purged-marker" not in contents


def test_retain_preserves_contents_on_close(tmp_path):
    set_case_log_retention(LogRetention.RETAIN)
    folder = tmp_path / "retain"
    with LogCase.create_case_in_folder(folder, case_id="c-6") as case:
        case.log.info("should-survive-marker")
        asyncio.run(case.begin())
        asyncio.run(case.finish())
        assert case.case_is_closed
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "should-survive-marker" in contents
    assert LOG_PURGE_SENTINEL not in contents


# ---------------------------------------------------------------------------
# Layout isolation from the asset playground
# ---------------------------------------------------------------------------

def test_logs_isolated_from_assets(tmp_path):
    folder = tmp_path / "isolation"
    with LogCase.create_case_in_folder(folder, case_id="c-7") as case:
        case.log.info("isolation-marker")
        case.case_assets.write("ephemeral.bin", b"data")  # not kept
        # logs/ lives at the case root, never under assets/
        assert all(not rel.startswith(LOGS_DIR_NAME) for rel in case.case_assets.list_assets())
        # Asset purge must not touch the log file.
        case.case_assets.purge_ephemeral()
        assert _log_path(folder).exists()
        assert "isolation-marker" in _log_path(folder).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Resource frugality
# ---------------------------------------------------------------------------

def test_no_persistent_log_descriptor(tmp_path):
    """The close-after-write handler must hold no open stream between records."""
    folder = tmp_path / "nofd"
    with LogCase.create_case_in_folder(folder, case_id="c-8") as case:
        case.log.info("write-1")
        case.log.info("write-2")
        handler = case.log.handlers[0]
        # Stock FileHandler keeps a `.stream`; ours never does.
        assert getattr(handler, "stream", None) is None
        # The writes still landed.
        assert "write-2" in _log_path(folder).read_text(encoding="utf-8")


def test_many_live_cases_hold_no_log_fds(tmp_path):
    """Many live cases that have all logged must hold zero open log descriptors.

    The portable guarantee is structural (no handler keeps a `.stream`); where
    `/proc/self/fd` exists (Linux) we additionally verify no real descriptor
    resolves to a case.log. No third-party dependency is required.
    """
    cases = []
    try:
        for i in range(60):
            c = LogCase.create_case_in_folder(tmp_path / f"case-{i}", case_id=f"m-{i}")
            c.log.info("hello from %d", i)
            cases.append(c)

        # Structural: open-file-descriptor cost does not scale with live cases.
        assert all(getattr(c.log.handlers[0], "stream", None) is None for c in cases)

        # Real fd check on platforms that expose /proc.
        fd_dir = "/proc/self/fd"
        if os.path.isdir(fd_dir):
            targets = []
            for name in os.listdir(fd_dir):
                try:
                    targets.append(os.readlink(os.path.join(fd_dir, name)))
                except OSError:
                    pass
            assert not any(t.endswith(LOG_FILE_NAME) for t in targets)
    finally:
        for c in cases:
            c.case_detach()


def test_per_case_loggers_do_not_pollute_registry(tmp_path):
    """Per-instance loggers are constructed directly, so they never enter the
    global registry and cannot accumulate across a high-churn process."""
    registry = logging.Logger.manager.loggerDict
    ids = [f"reg-{i}" for i in range(20)]
    cases = [
        LogCase.create_case_in_folder(tmp_path / cid, case_id=cid)
        for cid in ids
    ]
    try:
        for cid in ids:
            assert f"totodev_pub.case.{cid}" not in registry
    finally:
        for c in cases:
            c.case_detach()
