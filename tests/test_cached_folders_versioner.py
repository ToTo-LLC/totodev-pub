# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for CachedGroupingVersioner - Git-based versioning for CacheGrouping facets.
"""

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from git import Repo

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.cached_folders_versioner import (
    CachedGroupingVersioner,
    SnapshotInfo,
)
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy


def create_versioner(cache: CachedFileFolders, grouping_key=None, **kwargs) -> CachedGroupingVersioner:
    """Helper to create a grouping-scoped versioner."""
    grouping = cache.grouping(grouping_key)
    return CachedGroupingVersioner(grouping, **kwargs)


def _create_test_file(tmp_path: Path, name: str, content: str) -> Path:
    """Create a text file under tmp_path."""
    file_path = tmp_path / name
    file_path.write_text(content)
    return file_path


@pytest.fixture
def cache_root(tmp_path: Path) -> str:
    root = tmp_path / "cache_root"
    root.mkdir()
    return str(root.resolve())


@pytest.fixture
def flat_cache(cache_root: str) -> CachedFileFolders:
    return CachedFileFolders("everything/", cache_root)


@pytest.fixture
def grouped_cache(cache_root: str) -> CachedFileFolders:
    return CachedFileFolders("projects/{project}/", cache_root)


class TestVersionerInitialization:
    def test_init_creates_git_repo_for_flat_grouping(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache)

        assert versioner.is_versioned()
        assert (versioner.root_dir / ".git").exists()
        assert isinstance(versioner.repo, Repo)

    def test_init_creates_git_repo_for_grouped_grouping(self, grouped_cache: CachedFileFolders) -> None:
        versioner = create_versioner(grouped_cache, ["alpha"])

        grouping_root = grouped_cache.grouping(["alpha"]).grouping_root_dir()
        assert versioner.is_versioned()
        assert (grouping_root / ".git").exists()

    def test_init_without_repo_and_create_false_raises(self, flat_cache: CachedFileFolders) -> None:
        grouping = flat_cache.grouping(None)
        with pytest.raises(RuntimeError):
            CachedGroupingVersioner(grouping, create_if_unversioned=False)

    def test_init_creates_gitignore(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache)

        gitignore_path = versioner.root_dir / ".gitignore"
        assert gitignore_path.exists()
        content = gitignore_path.read_text()
        assert "*.sqlite" in content
        assert "*.portage.jsonl" in content

    def test_pattern_validation_warns(self, cache_root: str, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        caplog.set_level(logging.WARNING)
        cache = CachedFileFolders(".", cache_root)
        create_versioner(cache)

        assert any("may cause .git/ directory to mix" in record.message for record in caplog.records)

    def test_grouping_root_created_when_missing(self, grouped_cache: CachedFileFolders) -> None:
        grouping = grouped_cache.grouping(["beta"])
        grouping_root = grouping.grouping_root_dir()
        if grouping_root.exists():
            grouping_root.rmdir()

        assert not grouping_root.exists()
        create_versioner(grouped_cache, ["beta"])
        assert grouping_root.exists()


class TestSnapshotCommitOperations:
    @pytest.mark.asyncio
    async def test_snapshot_commit_creates_commit(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)

        test_file = _create_test_file(tmp_path, "config.txt", "content")
        await flat_cache.upsert_file(LocalFileProxy(str(test_file)), None)

        commit_hash = versioner.snapshot_commit("Add config")

        assert commit_hash
        assert len(commit_hash) == 40
        assert versioner.repo.commit(commit_hash).message.strip() == "Add config"

    @pytest.mark.asyncio
    async def test_snapshot_commit_generates_portage(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        data_file = _create_test_file(tmp_path, "data.txt", "payload")
        await flat_cache.upsert_file(LocalFileProxy(str(data_file)), None)

        versioner.snapshot_commit("Capture data")

        portage_files = list(Path(flat_cache.root_dir).rglob("*.portage.jsonl"))
        assert portage_files

    @pytest.mark.asyncio
    async def test_snapshot_commit_no_changes_returns_head(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache)

        first = versioner.snapshot_commit("Initial")
        second = versioner.snapshot_commit("No changes")

        assert first == second


class TestSnapshotExport:
    @pytest.mark.asyncio
    async def test_snapshot_export_directory(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        test_file = _create_test_file(tmp_path, "doc.txt", "documentation")
        await flat_cache.upsert_file(LocalFileProxy(str(test_file)), None)
        versioner.snapshot_commit("Baseline")

        export_root = tmp_path / "exports"
        export_root.mkdir()
        destination = export_root / "current"

        result = versioner.snapshot(destination)

        assert result == destination
        assert destination.exists()
        assert any(destination.rglob("*")), "Snapshot directory should contain files"

    @pytest.mark.asyncio
    async def test_snapshot_export_archive_zip(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        config = _create_test_file(tmp_path, "config.yaml", "version: 1.0")
        await flat_cache.upsert_file(LocalFileProxy(str(config)), None)
        versioner.snapshot_commit("Config")

        export_root = tmp_path / "archives"
        export_root.mkdir()
        destination = export_root / "config.zip"

        result = versioner.snapshot(destination)

        assert result == destination
        assert destination.exists()
        assert destination.stat().st_size > 0

    def test_snapshot_export_invalid_extension_raises(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        destination = tmp_path / "export.invalid"
        destination.parent.mkdir(exist_ok=True)

        with pytest.raises(ValueError):
            versioner.snapshot(destination)

    def test_snapshot_export_missing_parent_raises(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        destination = tmp_path / "missing" / "bundle.zip"

        with pytest.raises(ValueError):
            versioner.snapshot(destination)

    @pytest.mark.asyncio
    async def test_snapshot_export_requires_clean_worktree(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        base = _create_test_file(tmp_path, "base.txt", "base")
        await flat_cache.upsert_file(LocalFileProxy(str(base)), None)
        versioner.snapshot_commit("Base")

        dirty = versioner.root_dir / "dirty.txt"
        dirty.write_text("dirty")

        export_parent = tmp_path / "exports"
        export_parent.mkdir()
        with pytest.raises(RuntimeError):
            versioner.snapshot(export_parent / "dir")

    @pytest.mark.asyncio
    async def test_snapshot_export_with_ref_allows_dirty(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        base = _create_test_file(tmp_path, "base.txt", "base")
        await flat_cache.upsert_file(LocalFileProxy(str(base)), None)
        commit_hash = versioner.snapshot_commit("Base")

        dirty = versioner.root_dir / "dirty.txt"
        dirty.write_text("dirty")

        target_dir = tmp_path / "clean_export"
        target_dir.parent.mkdir(exist_ok=True, parents=True)
        result = versioner.snapshot(target_dir, ref=commit_hash)

        assert result == target_dir
        assert target_dir.exists()


class TestRestoreOperations:
    @pytest.mark.asyncio
    async def test_restore_to_commit(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        file_v1 = _create_test_file(tmp_path, "file.txt", "v1")
        await flat_cache.upsert_file(LocalFileProxy(str(file_v1)), None)
        commit_v1 = versioner.snapshot_commit("v1")

        file_v2 = _create_test_file(tmp_path, "file.txt", "v2")
        await flat_cache.upsert_file(LocalFileProxy(str(file_v2)), None)
        versioner.snapshot_commit("v2")

        versioner.restore(commit_v1)
        assert versioner.repo.head.commit.hexsha == commit_v1

    @pytest.mark.asyncio
    async def test_restore_requires_clean_worktree(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        payload = _create_test_file(tmp_path, "payload.txt", "payload")
        await flat_cache.upsert_file(LocalFileProxy(str(payload)), None)
        commit = versioner.snapshot_commit("payload")

        dirty = versioner.root_dir / "dirty.txt"
        dirty.write_text("dirty")

        with pytest.raises(RuntimeError):
            versioner.restore(commit)

    @pytest.mark.asyncio
    async def test_restore_force_allows_dirty(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        payload = _create_test_file(tmp_path, "payload.txt", "payload")
        await flat_cache.upsert_file(LocalFileProxy(str(payload)), None)
        commit = versioner.snapshot_commit("payload")

        dirty = Path(flat_cache.root_dir) / "dirty.txt"
        dirty.write_text("dirty")

        versioner.restore(commit, force=True)
        assert versioner.repo.head.commit.hexsha == commit

    @pytest.mark.asyncio
    async def test_restore_by_datetime(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        early = _create_test_file(tmp_path, "early.txt", "early")
        await flat_cache.upsert_file(LocalFileProxy(str(early)), None)
        commit_early = versioner.snapshot_commit("early")
        ts_early = datetime.fromtimestamp(versioner.repo.commit(commit_early).committed_date)

        time.sleep(1.1)
        late = _create_test_file(tmp_path, "late.txt", "late")
        await flat_cache.upsert_file(LocalFileProxy(str(late)), None)
        versioner.snapshot_commit("late")

        versioner.restore(ts_early + timedelta(seconds=0.5))
        assert versioner.repo.head.commit.hexsha == commit_early


class TestSnapshotsIteration:
    @pytest.mark.asyncio
    async def test_snapshots_returns_snapshot_info(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        for i in range(3):
            file_path = _create_test_file(tmp_path, f"file{i}.txt", f"content{i}")
            await flat_cache.upsert_file(LocalFileProxy(str(file_path)), None)
            versioner.snapshot_commit(f"commit-{i}")

        snapshots = versioner.snapshots(limit=5)
        assert snapshots
        assert all(isinstance(snapshot, SnapshotInfo) for snapshot in snapshots)


class TestTagOperations:
    @pytest.mark.asyncio
    async def test_tag_specific_commit(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        first_file = _create_test_file(tmp_path, "first.txt", "first")
        await flat_cache.upsert_file(LocalFileProxy(str(first_file)), None)
        commit1 = versioner.snapshot_commit("first")

        second_file = _create_test_file(tmp_path, "second.txt", "second")
        await flat_cache.upsert_file(LocalFileProxy(str(second_file)), None)
        versioner.snapshot_commit("second")

        tagged_commit = versioner.tag("stable", commit=commit1)
        assert tagged_commit == commit1

    def test_tag_duplicate_raises_without_force(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache)
        versioner.snapshot_commit("initial")
        versioner.tag("v1")

        with pytest.raises(RuntimeError):
            versioner.tag("v1")

    @pytest.mark.asyncio
    async def test_tag_force_replaces_existing(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)
        file_v1 = _create_test_file(tmp_path, "config.txt", "v1")
        await flat_cache.upsert_file(LocalFileProxy(str(file_v1)), None)
        commit_v1 = versioner.snapshot_commit("v1")
        versioner.tag("release")

        file_v2 = _create_test_file(tmp_path, "config.txt", "v2")
        await flat_cache.upsert_file(LocalFileProxy(str(file_v2)), None)
        commit_v2 = versioner.snapshot_commit("v2")

        replaced = versioner.tag("release", force=True)
        assert replaced == commit_v2
        assert versioner.repo.tags["release"].commit.hexsha == commit_v2


class TestBranchOperations:
    def test_branches_include_current(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache, branch="main")
        versioner.snapshot_commit("initial")
        versioner.switch_branch("develop", create=True)

        branches = versioner.branches()
        assert branches[0] == "develop"
        assert "main" in branches

    def test_switch_to_missing_branch_without_create_raises(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache)
        versioner.snapshot_commit("initial")

        with pytest.raises(RuntimeError):
            versioner.switch_branch("missing")


class TestGroupedBehavior:
    def test_each_grouping_has_independent_repo(self, grouped_cache: CachedFileFolders) -> None:
        versioner_a = create_versioner(grouped_cache, ["a"])
        versioner_b = create_versioner(grouped_cache, ["b"])

        root_a = grouped_cache.grouping(["a"]).grouping_root_dir()
        root_b = grouped_cache.grouping(["b"]).grouping_root_dir()

        assert root_a != root_b
        assert (root_a / ".git").exists()
        assert (root_b / ".git").exists()
        assert versioner_a.repo.git_dir != versioner_b.repo.git_dir


class TestIsVersioned:
    def test_is_versioned_true_after_init(self, flat_cache: CachedFileFolders) -> None:
        versioner = create_versioner(flat_cache)
        assert versioner.is_versioned()

    def test_is_versioned_false_without_repo(self, cache_root: str) -> None:
        cache = CachedFileFolders("flat/", cache_root)
        versioner = CachedGroupingVersioner.__new__(CachedGroupingVersioner)
        versioner.grouping = cache.grouping(None)
        versioner.parent_cache = cache
        versioner.grouping_key = None
        versioner.root_dir = Path(cache.root_dir)
        versioner.git_dir = versioner.root_dir / ".git"

        assert not versioner.is_versioned()


class TestIntegrationScenario:
    @pytest.mark.asyncio
    async def test_end_to_end_flow(self, flat_cache: CachedFileFolders, tmp_path: Path) -> None:
        versioner = create_versioner(flat_cache)

        v1_file = _create_test_file(tmp_path, "config.yaml", "version: 1")
        await flat_cache.upsert_file(LocalFileProxy(str(v1_file)), None)
        commit1 = versioner.snapshot_commit("v1")

        v2_file = _create_test_file(tmp_path, "config.yaml", "version: 2")
        await flat_cache.upsert_file(LocalFileProxy(str(v2_file)), None)
        commit2 = versioner.snapshot_commit("v2")

        export_path = tmp_path / "config.tar.gz"
        export_path.parent.mkdir(exist_ok=True, parents=True)
        versioner.snapshot(export_path, ref=commit2)
        assert export_path.exists()

        versioner.restore(commit1)
        assert versioner.repo.head.commit.hexsha == commit1
        versioner.restore(commit2)
        assert versioner.repo.head.commit.hexsha == commit2

