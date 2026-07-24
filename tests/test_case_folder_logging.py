# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for the per-case folder-logging tee (logs/case.log).

Covers: the tee itself, always-on across every live-instance path, the reserved
artifact guard, keepfile-driven log retention (delete unless kept), layout
isolation from the asset playground, and resource frugality (no idle file
descriptors, no process-global logger-registry growth).
"""

import asyncio
import logging
import os

import pytest

from totodev_pub.folder_backed_case import (
    FolderBackedCase,
    LogRetention,
    set_case_log_retention,
)
from totodev_pub.folder_backed_case_support import get_case_log_retention
from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest
from totodev_pub.folder_backed_case_support.constants import (
    LOGS_DIR_NAME,
    LOG_FILE_NAME,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


# ---------------------------------------------------------------------------
# Concrete subclasses used across the tests
# ---------------------------------------------------------------------------


class LogCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new == begin ==> open == finish ==> done --> [*]"]


class LogReclassTarget(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    """Shares the 'new' state with LogCase so reclassify from a fresh case is legal."""
    fsm_state_chains = ["[*] --> new == go ==> finished --> [*]"]


class FailingHookCase(FolderBackedCase):
    """A manual trigger whose perform_ hook always raises, for exception-tee tests."""

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new == begin ==> open --> [*]"]

    async def perform_begin(self, tctx) -> None:
        raise RuntimeError("boom-in-hook")


class KeepLogsOnTerminateCase(FolderBackedCase):
    """Opts into log retention from on_terminating() via the keep manifest."""

    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> new == finish ==> done --> [*]"]

    def on_terminating(self) -> None:
        self.case_keep_files(f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}")


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
    case = LogCase.create_case_in_folder(folder, case_id="tee-1")
    try:
        case.log.info("tee-marker-xyz")
        # Propagated to root (caplog attaches at root).
        assert any("tee-marker-xyz" in r.getMessage() for r in caplog.records)
        # And mirrored to the per-case file.
        contents = _log_path(folder).read_text(encoding="utf-8")
        assert "tee-marker-xyz" in contents
        # The file line is self-identifying (case id + type stamped by the filter).
        assert "tee-1" in contents
        assert "LogCase" in contents

    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# Trigger/guard/hook exceptions are tee'd automatically (full traceback)
# ---------------------------------------------------------------------------


def test_trigger_exception_teed_with_traceback(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    folder = tmp_path / "exc"
    case = FailingHookCase.create_case_in_folder(folder, case_id="exc-1")
    try:
        with pytest.raises(RuntimeError, match="boom-in-hook"):
            asyncio.run(case.begin())
        # Full traceback in the per-case file — NOT just the journal's terse fact.
        contents = _log_path(folder).read_text(encoding="utf-8")
        assert "boom-in-hook" in contents
        assert "Traceback" in contents
        assert "RuntimeError" in contents
        # And propagated to root, same as any other self.log record.
        assert any("boom-in-hook" in r.getMessage() for r in caplog.records)
    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# Always-on across every live-instance path
# ---------------------------------------------------------------------------


def test_logging_on_for_create(tmp_path):
    folder = tmp_path / "create"
    _case = LogCase.create_case_in_folder(folder, case_id="c-1")
    try:
        assert _log_path(folder).exists()
        assert _log_path(folder).read_text(encoding="utf-8").strip() != ""

    finally:
        _case.case_detach()


def test_logging_on_for_rehydrate(tmp_path):
    case_type_registry.register_case_types(LogCase)
    folder = tmp_path / "rehydrate"
    _case = LogCase.create_case_in_folder(folder, case_id="c-2")
    try:
        pass
    finally:
        _case.case_detach()
    set_case_log_retention(
        LogRetention.RETAIN
    )  # keep contents across the closed-but-open reopen
    case = case_type_registry.rehydrate(folder)
    try:
        case.log.info("rehydrated-marker")
        assert "rehydrated-marker" in _log_path(folder).read_text(encoding="utf-8")

    finally:
        case.case_detach()


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
# Closure retention: keepfile delete-or-keep (no truncation)
# ---------------------------------------------------------------------------


def test_purge_default_deletes_log_on_close(tmp_path):
    """Default: logs/case.log is not a framework keep rule, so purge deletes it.
    Detach may recreate a short session-banner file afterward — pre-purge content
    must still be gone."""
    folder = tmp_path / "purge"
    case = LogCase.create_case_in_folder(folder, case_id="c-5")
    try:
        case.log.info("should-be-purged-marker")
        asyncio.run(case.begin())
        asyncio.run(case.finish())
        assert case.case_is_terminal
    finally:
        case.case_detach()
    assert "should-be-purged-marker" not in _log_path(folder).read_text(encoding="utf-8")


def test_global_retain_preserves_log_on_close(tmp_path):
    """LogRetention.RETAIN seeds a keep rule at bind time; purge then keeps the file."""
    set_case_log_retention(LogRetention.RETAIN)
    folder = tmp_path / "retain"
    case = LogCase.create_case_in_folder(folder, case_id="c-6")
    try:
        case.log.info("should-survive-marker")
        asyncio.run(case.begin())
        asyncio.run(case.finish())
        assert case.case_is_terminal
    finally:
        case.case_detach()
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "should-survive-marker" in contents


def test_case_keep_files_keeps_log_across_termination_purge(tmp_path):
    """Per-case opt-in is just an ordinary keep rule."""
    folder = tmp_path / "retain-logs"
    case = LogCase.create_case_in_folder(folder, case_id="rl-1")
    try:
        case.log.info("keep-me-marker")
        case.case_keep_files(f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}")
        assert get_case_log_retention() is LogRetention.PURGE
        asyncio.run(case.begin())
        asyncio.run(case.finish())
        assert case.case_is_terminal
    finally:
        case.case_detach()
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "keep-me-marker" in contents


def test_keep_logs_from_on_terminating(tmp_path):
    """Judged at wind-down via case_keep_files() in on_terminating()."""
    folder = tmp_path / "retain-on-terminate"
    case = KeepLogsOnTerminateCase.create_case_in_folder(folder, case_id="rot-1")
    try:
        case.log.info("keep-me-too-marker")
        asyncio.run(case.finish())
        assert case.case_is_terminal
    finally:
        case.case_detach()
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "keep-me-too-marker" in contents


# ---------------------------------------------------------------------------
# Direct CaseKeepManifest.purge() — same keepfile rules, no special log path
# ---------------------------------------------------------------------------


def test_direct_manifest_purge_deletes_log_by_default(tmp_path):
    folder = tmp_path / "direct-purge"
    case = LogCase.create_case_in_folder(folder, case_id="dp-1")
    try:
        case.log.info("direct-purge-marker")
    finally:
        case.case_detach()
    CaseKeepManifest(folder).purge()
    assert not _log_path(folder).exists()


def test_direct_manifest_purge_keeps_log_with_keep_rule(tmp_path):
    folder = tmp_path / "direct-purge-retain"
    case = LogCase.create_case_in_folder(folder, case_id="dp-2")
    try:
        case.log.info("direct-purge-retain-marker")
        case.case_keep_files(f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}")
    finally:
        case.case_detach()
    CaseKeepManifest(folder).purge()
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "direct-purge-retain-marker" in contents


def test_direct_manifest_purge_keeps_log_under_global_retain(tmp_path):
    set_case_log_retention(LogRetention.RETAIN)
    folder = tmp_path / "direct-purge-global-retain"
    case = LogCase.create_case_in_folder(folder, case_id="dp-3")
    try:
        case.log.info("global-retain-marker")
    finally:
        case.case_detach()
    CaseKeepManifest(folder).purge()
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "global-retain-marker" in contents


# ---------------------------------------------------------------------------
# Layout isolation from the asset playground
# ---------------------------------------------------------------------------


def test_logs_isolated_from_assets(tmp_path):
    folder = tmp_path / "isolation"
    case = LogCase.create_case_in_folder(folder, case_id="c-7")
    try:
        case.log.info("isolation-marker")
        case.case_assets.write("ephemeral.bin", b"data")  # not kept
        # logs/ lives at the case root, never under assets/
        assert all(
            not rel.startswith(LOGS_DIR_NAME) for rel in case.case_assets.list_assets()
        )
        # Default purge deletes the log file like any other unmatched path.
        case._keep_manifest.purge()
        assert not _log_path(folder).exists()

    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# Resource frugality
# ---------------------------------------------------------------------------


def test_no_persistent_log_descriptor(tmp_path):
    """The close-after-write handler must hold no open stream between records."""
    folder = tmp_path / "nofd"
    case = LogCase.create_case_in_folder(folder, case_id="c-8")
    try:
        case.log.info("write-1")
        case.log.info("write-2")
        handler = case.log.handlers[0]
        # Stock FileHandler keeps a `.stream`; ours never does.
        assert getattr(handler, "stream", None) is None
        # The writes still landed.
        assert "write-2" in _log_path(folder).read_text(encoding="utf-8")

    finally:
        case.case_detach()


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
    cases = [LogCase.create_case_in_folder(tmp_path / cid, case_id=cid) for cid in ids]
    try:
        for cid in ids:
            assert f"totodev_pub.case.{cid}" not in registry
    finally:
        for c in cases:
            c.case_detach()


# ---------------------------------------------------------------------------
# self.log.getChild(): stays tee'd, stays out of the registry
# ---------------------------------------------------------------------------


def test_get_child_stays_teed(tmp_path):
    folder = tmp_path / "getchild"
    case = LogCase.create_case_in_folder(folder, case_id="gc-1")
    try:
        helper_log = case.log.getChild("helper")
        helper_log.info("child-marker-xyz")
        contents = _log_path(folder).read_text(encoding="utf-8")
        assert "child-marker-xyz" in contents
        # Self-identifying like any tee'd record (same filter, same case identity).
        assert "gc-1" in contents
    finally:
        case.case_detach()


def test_get_child_is_memoized_and_registry_clean(tmp_path):
    registry = logging.Logger.manager.loggerDict
    folder = tmp_path / "getchild-registry"
    case = LogCase.create_case_in_folder(folder, case_id="gc-2")
    try:
        child_a = case.log.getChild("helper")
        child_b = case.log.getChild("helper")
        assert child_a is child_b  # same suffix -> same object, ordinary getChild semantics
        assert "totodev_pub.case.gc-2.helper" not in registry
    finally:
        case.case_detach()


# ---------------------------------------------------------------------------
# Detach: closing banner + tee disabled (post-detach records still propagate to
# root, but never reach the case folder again)
# ---------------------------------------------------------------------------


def test_detach_writes_banner_then_disables_tee(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    folder = tmp_path / "detach"
    case = LogCase.create_case_in_folder(folder, case_id="det-1")
    log = case.log
    case.case_detach()

    contents_after_detach = _log_path(folder).read_text(encoding="utf-8")
    assert "detached" in contents_after_detach

    caplog.clear()
    log.info("post-detach-marker")
    # Still an ordinary logger — still propagates to root...
    assert any("post-detach-marker" in r.getMessage() for r in caplog.records)
    # ...but the file handler is gone, so the folder is never written to again.
    contents_unchanged = _log_path(folder).read_text(encoding="utf-8")
    assert contents_unchanged == contents_after_detach
    assert "post-detach-marker" not in contents_unchanged


def test_detach_is_idempotent(tmp_path):
    folder = tmp_path / "detach-twice"
    case = LogCase.create_case_in_folder(folder, case_id="det-2")
    case.case_detach()
    contents_once = _log_path(folder).read_text(encoding="utf-8")
    case.case_detach()  # must not raise, must not write a second banner
    contents_twice = _log_path(folder).read_text(encoding="utf-8")
    assert contents_once == contents_twice


def test_detach_on_terminal_case_writes_banner_after_purge(tmp_path):
    """Purge deletes the log; detach then recreates a short session-banner file.
    Pre-purge content must not reappear."""
    folder = tmp_path / "detach-terminal"
    case = LogCase.create_case_in_folder(folder, case_id="det-3")
    case.log.info("pre-purge-marker")
    asyncio.run(case.begin())
    asyncio.run(case.finish())
    assert case.case_is_terminal
    case.case_detach()
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "pre-purge-marker" not in contents
    assert "detached" in contents


def test_rehydrate_of_terminal_case_writes_attach_banner(tmp_path):
    """Reopening an already-terminal case still gets a fresh attach banner."""
    folder = tmp_path / "rehydrate-terminal"
    case = LogCase.create_case_in_folder(folder, case_id="det-4")
    case.log.info("pre-purge-marker")
    asyncio.run(case.begin())
    asyncio.run(case.finish())
    case.case_detach()
    sealed = _log_path(folder).read_text(encoding="utf-8")
    assert "pre-purge-marker" not in sealed
    assert "attached" not in sealed  # only this case's closing banner so far

    reopened = LogCase(folder)
    contents = _log_path(folder).read_text(encoding="utf-8")
    assert "attached" in contents
    assert "pre-purge-marker" not in contents
    reopened.case_detach()
