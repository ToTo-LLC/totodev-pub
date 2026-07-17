"""Tests for CaseKeepManifest: case-root retention and purge."""

import logging
from pathlib import Path

import pytest

from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest
from totodev_pub.folder_backed_case_support.constants import (
    FRAMEWORK_KEEP_RULES,
    KEEP_LIST_NAME,
    LEASE_NAME,
    LOGS_DIR_NAME,
    LOG_FILE_NAME,
    RECORD_NAME,
)


def _write_text(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_ensure_framework_rules_seeds_baseline(tmp_path):
    case_folder = tmp_path / "case-fw"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    rules = manifest.list_rules()
    for rule in FRAMEWORK_KEEP_RULES:
        assert rule in rules


def test_framework_rules_protect_standard_artifacts(tmp_path):
    case_folder = tmp_path / "case-protect"
    case_folder.mkdir()
    _write_text(case_folder / RECORD_NAME)
    _write_text(case_folder / KEEP_LIST_NAME, "case_record.yaml\n")
    _write_text(case_folder / "events" / "evt.json")
    _write_text(case_folder / LOGS_DIR_NAME / LOG_FILE_NAME)
    _write_text(case_folder / "assets" / "kept.txt")
    _write_text(case_folder / "scratch.tmp")

    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    manifest.add_rules("assets/kept.txt")
    purged = manifest.purge()

    assert "scratch.tmp" in purged
    assert f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}" in purged
    assert (case_folder / RECORD_NAME).exists()
    assert (case_folder / "events" / "evt.json").exists()
    assert not (case_folder / LOGS_DIR_NAME / LOG_FILE_NAME).exists()
    assert (case_folder / "assets" / "kept.txt").exists()
    assert not (case_folder / "scratch.tmp").exists()


def test_keep_rule_retains_case_log(tmp_path):
    case_folder = tmp_path / "case-keep-log"
    case_folder.mkdir()
    _write_text(case_folder / LOGS_DIR_NAME / LOG_FILE_NAME, "keep-me\n")

    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    manifest.add_rules(f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}")
    purged = manifest.purge()

    assert f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}" not in purged
    assert (case_folder / LOGS_DIR_NAME / LOG_FILE_NAME).read_text(encoding="utf-8") == "keep-me\n"


def test_purge_deletes_ephemeral_assets(tmp_path):
    case_folder = tmp_path / "case-assets"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    _write_text(case_folder / "assets" / "keep.json")
    _write_text(case_folder / "assets" / "drop.json")
    manifest.add_rules("assets/keep.json")

    purged = manifest.purge()

    assert purged == ["assets/drop.json"]
    assert (case_folder / "assets" / "keep.json").exists()
    assert not (case_folder / "assets" / "drop.json").exists()


def test_glob_rules_case_wide(tmp_path):
    case_folder = tmp_path / "case-glob"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    _write_text(case_folder / "assets" / "results" / "a.json")
    _write_text(case_folder / "assets" / "results" / "b.txt")
    _write_text(case_folder / "assets" / "other" / "c.json")
    manifest.add_rules("assets/results/*.json")

    purged = manifest.purge()

    assert "assets/other/c.json" in purged
    assert "assets/results/b.txt" in purged
    assert (case_folder / "assets" / "results" / "a.json").exists()


def test_hard_skip_lease_never_deleted(tmp_path):
    case_folder = tmp_path / "case-lease"
    case_folder.mkdir()
    _write_text(case_folder / LEASE_NAME)
    manifest = CaseKeepManifest(case_folder)
    # No framework rules — lease still must survive.

    purged = manifest.purge()

    assert LEASE_NAME not in purged
    assert (case_folder / LEASE_NAME).exists()


def test_hard_skip_keep_manifest_never_deleted(tmp_path):
    case_folder = tmp_path / "case-keepfile"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.add_rules("assets/foo.txt")
    manifest_path = manifest.path
    assert manifest_path.exists()

    manifest.purge()

    assert manifest_path.exists()


def test_purge_prunes_empty_dirs(tmp_path):
    case_folder = tmp_path / "case-prune"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    empty = case_folder / "assets" / "empty" / "nested"
    _write_text(empty / "gone.txt")
    manifest.purge()

    assert not (case_folder / "assets" / "empty").exists()


def test_debug_logging_emits_per_purged_path(tmp_path, caplog):
    case_folder = tmp_path / "case-log"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.ensure_framework_rules()
    _write_text(case_folder / "ephemeral-a.tmp")
    _write_text(case_folder / "ephemeral-b.tmp")

    with caplog.at_level(logging.DEBUG, logger="totodev_pub.folder_backed_case_support.case_keep_manifest"):
        purged = manifest.purge()

    assert len(purged) == 2
    assert "purged ephemeral file: ephemeral-a.tmp" in caplog.text
    assert "purged ephemeral file: ephemeral-b.tmp" in caplog.text


def test_remove_rules_drops_entry(tmp_path):
    case_folder = tmp_path / "case-remove"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.add_rules("assets/reports/*.csv", "assets/keep.txt")

    manifest.remove_rules("assets/reports/*.csv")

    assert manifest.list_rules() == ["assets/keep.txt"]


def test_add_rules_idempotent(tmp_path):
    case_folder = tmp_path / "case-idem"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest.add_rules("assets/a.txt", "assets/a.txt")
    assert manifest.list_rules() == ["assets/a.txt"]


def test_comments_and_blank_lines_ignored(tmp_path):
    case_folder = tmp_path / "case-comments"
    case_folder.mkdir()
    manifest = CaseKeepManifest(case_folder)
    manifest._write_rules([])  # type: ignore[attr-defined]
    manifest.path.write_text(
        "# comment\n\nassets/foo.txt\n",
        encoding="utf-8",
    )
    assert manifest.list_rules() == ["assets/foo.txt"]
