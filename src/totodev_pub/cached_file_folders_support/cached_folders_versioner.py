# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CachedGroupingVersioner: Git-based Versioning for CacheGrouping facets

Provides simple git-based version control for CachedFileFolders instances, enabling
atomic snapshot and restore operations for cached file collections.

Primary Use Case:
    Version management of configuration file collections that need holistic update control.
    Particularly useful for maintaining historical snapshots of interconnected files where
    changes need to be tracked and rolled back as a group.

Key Features:
    - Snapshot: Create git commits of current cache state
    - Restore: Roll back to previous snapshots by commit hash, relative index, or datetime
    - Branch Management: Work with multiple branches for different configurations
    - Pattern-Based Versioning: Only version relevant files (portage, configs, slave dirs)
    - Safety Checks: Prevent operations during active cache operations

Basic Usage:
    from totodev_pub.cached_file_folders import CachedFileFolders
    from totodev_pub.cached_file_folders_support.cached_folders_versioner import CachedGroupingVersioner
    
    # Create cache and versioner
    cache = CachedFileFolders("projects/{project}/", "/cache/root")
    grouping = cache.grouping(["project-a"])
    versioner = CachedGroupingVersioner(grouping)
    
    # Take snapshot
    commit_hash = versioner.snapshot_commit("Added Q4 configuration files")
    
    # Make changes to cache...
    
    # List snapshots
    for snapshot in versioner.list_snapshots(limit=5):
        print(f"{snapshot.commit_hash[:8]}: {snapshot.message} ({snapshot.timestamp})")
    
    # Restore to previous state
    versioner.restore(-1)  # Go back one commit
    versioner.restore(commit_hash)  # Restore to specific commit

Design Notes:
    - Git repository (.git/) is placed inside cache root_dir for self-contained cleanup
    - Portage files serve as source of truth; SQLite databases are regenerated from them
    - Works seamlessly with cache's optimistic concurrency control (no locking required)
    - Pattern validation warns (but doesn't block) on potentially problematic patterns

See Also:
    - CachedFileFolders.get_version_control_patterns(): Get patterns for .gitignore
    - CachedFileFolders.portage(): Generate portage files for versioning
"""

import fnmatch
import logging
import shutil
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union, TYPE_CHECKING

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping

logger = logging.getLogger(__name__)


class SnapshotInfo(BaseModel):
    """Information about a snapshot (git commit) in the cache history.
    
    Attributes:
        commit_hash: Git commit SHA hash
        message: Commit message describing the snapshot
        timestamp: When the snapshot was created (as datetime)
        branch: Branch name where this commit exists
        tags: List of tag names pointing to this commit
    """
    commit_hash: str = Field(..., description="Git commit SHA hash")
    message: str = Field(..., description="Commit message")
    timestamp: datetime = Field(..., description="Snapshot creation time")
    branch: str = Field(..., description="Branch name")
    tags: List[str] = Field(default_factory=list, description="Tags pointing to this commit")


class CachedGroupingVersioner:
    """Git-based versioning for CacheGrouping facets.
    
    This class provides snapshot and restore capabilities for a specific CacheGrouping
    within a CachedFileFolders instance using git as the underlying version control system.
    It's particularly useful for managing configuration files that need to be versioned as
    cohesive grouping-level collections.
    
    The versioner places a .git/ directory inside the grouping's root directory, making
    each grouping self-contained and easy to clean up by deleting that specific directory.
    
    Key Operations:
        - snapshot(): Export repository content to directory or archive
        - snapshot_commit(): Create a git commit of the current grouping state
        - restore(): Roll back to a previous snapshot (by commit, tag, index, or datetime)
        - snapshots(): Iterate through snapshot history with optional tag filtering
        - tag(): Create named tags for important snapshots
        - branches(): List available branches
        - switch_branch(): Switch between branches
    
    Safety Features:
        - Detects uncommitted changes before restore
        - Validates pattern appropriateness (warns on issues)
        - Auto-generates .gitignore for proper file filtering
        - Compatible with cache's optimistic concurrency model (no locking required)
    
    Example:
        cache = CachedFileFolders("config/", "/app/cache")
        grouping = cache.grouping(["config"])
        versioner = CachedGroupingVersioner(grouping)
        
        # Create initial snapshot
        versioner.snapshot_commit("Initial configuration")
        versioner.tag("v1.0")  # Tag important snapshots
        
        # Make changes...
        await cache.upsert_file(new_config, None)
        
        # Create another snapshot
        versioner.snapshot_commit("Updated database settings")
        versioner.tag("prod-2024-q4", message="Production config for Q4")
        
        # Export current state to a tarball
        versioner.snapshot(Path("/exports/config.tar.gz"), ref="v1.0")
        
        # View history
        for snap in versioner.snapshots(limit=10):
            print(f"{snap.commit_hash[:8]}: {snap.message} {snap.tags}")
        
        # View only tagged snapshots
        for snap in versioner.snapshots(tag_glob="*"):
            print(f"Tagged: {snap.tags[0]}")
        
        # Restore if needed
        versioner.restore(-1)  # Go back one commit
        versioner.restore("v1.0")  # Or restore to a tag
    """
    
    _SUPPORTED_ARCHIVE_SUFFIXES: Dict[Tuple[str, ...], str] = {
        (".zip",): "zip",
        (".tar",): "tar",
        (".tar", ".gz"): "tar.gz",
        (".tgz",): "tar.gz",
    }

    def __init__(self, grouping: 'CacheGrouping', branch: str = "main", 
                 create_if_unversioned: bool = True):
        """Initialize versioner for a CacheGrouping instance.
        
        Args:
            grouping: CacheGrouping facet to version control
            branch: Git branch to use (default: "main")
            create_if_unversioned: If True, initialize git repo if not present
        
        Raises:
            ValueError: If pattern is problematic (warns but doesn't block)
            RuntimeError: If git repo doesn't exist and create_if_unversioned is False
        """
        self.grouping = grouping
        self.parent_cache = grouping.parent_cache
        self.grouping_key = grouping.grouping_key
        self.root_dir = self._resolve_grouping_root()
        self.git_dir = self.root_dir / ".git"
        self._requested_branch = branch
        
        # Validate pattern appropriateness
        self._validate_pattern()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize or attach to git repository
        if self.is_versioned():
            self.repo = Repo(str(self.root_dir))
            logger.info("Attached to existing git repository at %s", self.root_dir)
        elif create_if_unversioned:
            self._initialize_repository()
        else:
            raise RuntimeError(
                f"Git repository not found at {self.root_dir} and "
                f"create_if_unversioned is False"
            )
        
        # Switch to requested branch if it exists, otherwise create it
        self._ensure_branch(branch)

    def _resolve_grouping_root(self) -> Path:
        """Determine and prepare the filesystem root for this grouping."""
        try:
            grouping_root = self.grouping.grouping_root_dir()
        except ValueError:
            grouping_root = Path(self.parent_cache.root_dir)
        grouping_root = Path(grouping_root)
        grouping_root.mkdir(parents=True, exist_ok=True)
        return grouping_root

    @contextmanager
    def _temporary_directory(self, prefix: str) -> Iterator[Path]:
        """Provide a temporary directory under the volatile tree for working data."""
        base_dir = Path.cwd() / "volatile" / "cached_grouping_versioner"
        base_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=prefix, dir=str(base_dir)) as tmp_dir:
            yield Path(tmp_dir)

    def _classify_snapshot_destination(self, destination: Path) -> Tuple[str, Optional[str]]:
        """Determine whether destination is an archive or directory target."""
        suffixes = tuple(s.lower() for s in destination.suffixes)
        if suffixes:
            fmt = self._SUPPORTED_ARCHIVE_SUFFIXES.get(suffixes)
            if fmt is None:
                raise ValueError(
                    f"Unsupported snapshot archive extension '{''.join(destination.suffixes)}'. "
                    f"Supported extensions: {', '.join(sorted(''.join(k) for k in self._SUPPORTED_ARCHIVE_SUFFIXES))}"
                )
            return "archive", fmt
        return "directory", None

    def _resolve_snapshot_commit(self, ref: Optional[Union[str, int, datetime]]) -> str:
        """Resolve requested ref/tag/commit into a concrete commit hash for snapshot exports."""
        if ref is None:
            if self._has_uncommitted_changes():
                raise RuntimeError(
                    "Uncommitted changes detected. Commit changes or provide an explicit ref to snapshot."
                )
            try:
                return self.repo.head.commit.hexsha
            except ValueError as exc:
                raise RuntimeError("Unable to snapshot: no commits available in repository.") from exc
        commit_hash = self._resolve_target_to_commit(ref)
        return commit_hash

    def _create_archive(self, commit_hash: str, output_path: Path, archive_format: str) -> None:
        """Create an archive for the given commit."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.repo.git.archive(
                f"--format={archive_format}",
                "-o",
                str(output_path),
                commit_hash
            )
        except GitCommandError as exc:
            raise RuntimeError(f"Failed to create snapshot archive: {exc}") from exc

    def _extract_tar(self, archive_path: Path, destination: Path) -> None:
        """Extract a tar archive safely to the destination directory."""
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r") as archive:
            for member in archive.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
                    raise RuntimeError(
                        f"Unsafe path '{member.name}' encountered while extracting snapshot archive."
                    )
            archive.extractall(path=destination)

    def _snapshot_archive(self, commit_hash: str, destination: Path, archive_format: str, overwrite: bool) -> Path:
        """Write snapshot archive to the requested destination."""
        if destination.exists():
            if destination.is_dir():
                raise IsADirectoryError(
                    f"Destination '{destination}' is a directory; expected a file for archive snapshot."
                )
            if not overwrite:
                raise FileExistsError(
                    f"Destination '{destination}' already exists. Use overwrite=True to replace it."
                )
        with self._temporary_directory("snapshot-archive-") as temp_dir:
            temp_archive = temp_dir / destination.name
            self._create_archive(commit_hash, temp_archive, archive_format)
            if destination.exists():
                destination.unlink()
            shutil.move(str(temp_archive), str(destination))
        return destination

    def _snapshot_directory(self, commit_hash: str, destination: Path, overwrite: bool) -> Path:
        """Materialize snapshot contents into a directory."""
        if destination.exists():
            if not destination.is_dir():
                raise NotADirectoryError(
                    f"Destination '{destination}' exists and is not a directory."
                )
            if not overwrite:
                raise FileExistsError(
                    f"Destination directory '{destination}' already exists. Use overwrite=True to replace it."
                )
            shutil.rmtree(destination)
        with self._temporary_directory("snapshot-dir-") as temp_dir:
            archive_path = temp_dir / "payload.tar"
            extract_root = temp_dir / "extract"
            self._create_archive(commit_hash, archive_path, "tar")
            self._extract_tar(archive_path, extract_root)
            destination.mkdir()
            try:
                for item in extract_root.iterdir():
                    target_path = destination / item.name
                    shutil.move(str(item), str(target_path))
            except Exception as exc:
                shutil.rmtree(destination, ignore_errors=True)
                raise RuntimeError(f"Failed to materialize snapshot directory: {exc}") from exc
        return destination

    def snapshot(
        self,
        destination: Path,
        ref: Optional[Union[str, int, datetime]] = None,
        overwrite: bool = False
    ) -> Path:
        """Export a snapshot of the grouping to a directory or archive.
        
        Args:
            destination: Target path for the snapshot. Directories are created when no
                supported archive extension is present. Supported archive extensions are:
                .zip, .tar, .tar.gz, .tgz.
            ref: Optional tag/commit/relative selector. When None, snapshots the current HEAD.
            overwrite: Allow replacing existing destination content.
        
        Returns:
            Absolute Path to the exported directory or archive file.
        
        Raises:
            RuntimeError: On uncommitted changes (when ref is None) or export failures.
            ValueError: If the destination parent does not exist or extension unsupported.
            FileExistsError: When destination exists and overwrite=False.
        """
        destination_path = Path(destination).expanduser()
        if not destination_path.is_absolute():
            destination_path = destination_path.resolve(strict=False)
        dest_parent = destination_path.parent
        if not dest_parent.exists():
            raise ValueError(
                f"Destination parent directory '{dest_parent}' does not exist."
            )
        destination_type, archive_format = self._classify_snapshot_destination(destination_path)
        commit_hash = self._resolve_snapshot_commit(ref)
        if destination_type == "archive":
            assert archive_format is not None
            logger.info(
                "Exporting snapshot of commit %s to archive %s", commit_hash[:8], destination_path
            )
            return self._snapshot_archive(commit_hash, destination_path, archive_format, overwrite)
        logger.info(
            "Exporting snapshot of commit %s to directory %s", commit_hash[:8], destination_path
        )
        return self._snapshot_directory(commit_hash, destination_path, overwrite)
    
    def _validate_pattern(self) -> None:
        """Validate that the cache pattern is appropriate for git versioning.
        
        Warns if the pattern might cause .git/ to mix with cached files.
        Does not raise exceptions - lets user proceed if they know what they're doing.
        """
        pattern = self.grouping.pattern.strip('/')
        
        if not pattern or pattern == '.':
            logger.warning(
                "Pattern '%s' may cause .git/ directory to mix with cached files. "
                "Consider using a pattern like 'everything/' to create subdirectory isolation.",
                self.grouping.pattern
            )
    
    def _initialize_repository(self) -> None:
        """Initialize a new git repository in the cache root directory."""
        try:
            self.repo = Repo.init(str(self.root_dir))
            logger.info("Initialized new git repository at %s", self.root_dir)
            
            # Generate initial .gitignore
            self._generate_gitignore()
            
            # Make initial commit with .gitignore
            self.repo.index.add(['.gitignore'])
            self.repo.index.commit("Initial commit: Add .gitignore for CacheGrouping")
            
        except Exception as e:
            raise RuntimeError(f"Failed to initialize git repository: {e}") from e
    
    def _generate_gitignore(self) -> None:
        """Generate .gitignore file using cache's version control patterns."""
        patterns = self.parent_cache.get_version_control_patterns()
        
        gitignore_path = self.root_dir / ".gitignore"
        
        with open(gitignore_path, 'w') as f:
            f.write("# CachedFileFolders Version Control\n")
            f.write("# Auto-generated by CachedGroupingVersioner\n")
            f.write("# DO NOT EDIT - Regenerated on initialization\n\n")
            
            f.write("# Files to ignore (regenerated from portage)\n")
            for pattern in patterns['ignore']:
                f.write(f"{pattern}\n")
            
            f.write("\n# Files to version (source of truth)\n")
            for pattern in patterns['version']:
                f.write(f"!{pattern}\n")
        
        logger.info("Generated .gitignore at %s", gitignore_path)
    
    def _ensure_branch(self, branch: str) -> None:
        """Ensure the specified branch exists and switch to it.
        
        Args:
            branch: Branch name to ensure and switch to
        """
        # Check if branch exists
        branch_exists = any(ref.name == f'refs/heads/{branch}' or ref.name == branch 
                          for ref in self.repo.refs)
        
        if branch_exists:
            # Switch to existing branch
            try:
                self.repo.git.checkout(branch)
                logger.info("Switched to existing branch '%s'", branch)
            except GitCommandError as e:
                logger.warning("Failed to checkout branch '%s': %s", branch, e)
        else:
            # Create new branch
            try:
                # Check if we have any commits yet
                try:
                    self.repo.head.commit
                    # We have commits, create branch from current HEAD
                    self.repo.create_head(branch)
                    self.repo.git.checkout(branch)
                    logger.info("Created and switched to new branch '%s'", branch)
                except ValueError:
                    # No commits yet, branch will be created on first commit
                    logger.info("Branch '%s' will be created on first commit", branch)
            except GitCommandError as e:
                logger.warning("Failed to create branch '%s': %s", branch, e)
    
    def is_versioned(self) -> bool:
        """Check if the cache is under git version control.
        
        Returns:
            True if .git/ directory exists and is valid, False otherwise
        """
        try:
            if not self.git_dir.exists():
                return False
            # Try to open as repo to validate
            Repo(str(self.root_dir))
            return True
        except (InvalidGitRepositoryError, Exception):
            return False
    
    def _has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes in the working directory.
        
        Returns:
            True if there are uncommitted changes, False otherwise
        """
        return self.repo.is_dirty(untracked_files=True)
    
    def snapshot_commit(self, message: str) -> str:
        """Create a snapshot (git commit) of the current grouping state.
        
        This method:
        1. Generates fresh portage files
        2. Stages relevant files
        3. Creates a git commit
        
        Note: Safe to call during resync operations due to the cache's optimistic concurrency model.
        
        Args:
            message: Commit message describing the snapshot
        
        Returns:
            Commit hash (SHA) of the created snapshot
        
        Raises:
            RuntimeError: If commit fails
            
        Example:
            commit = versioner.snapshot_commit("Updated configuration for Q4 2024")
            print(f"Created snapshot: {commit}")
        """
        
        # Generate fresh portage files (source of truth for restoration)
        logger.info("Generating portage files for snapshot")
        self.parent_cache.portage(self.grouping_key, include_metadata=True)
        
        # Stage all files (respecting .gitignore)
        try:
            # Add all files (git respects .gitignore automatically)
            self.repo.git.add(A=True)
            
            # Check if there are changes to commit
            if not self.repo.is_dirty(untracked_files=False):
                logger.info("No changes to snapshot")
                # Return current HEAD commit hash
                return self.repo.head.commit.hexsha
            
            # Create commit
            commit = self.repo.index.commit(message)
            logger.info("Created snapshot: %s - %s", commit.hexsha[:8], message)
            
            return commit.hexsha
            
        except GitCommandError as e:
            raise RuntimeError(f"Failed to create snapshot: {e}") from e
    
    def restore(self, target: Union[str, int, datetime], force: bool = False) -> None:
        """Restore cache to a previous snapshot.
        
        This method:
        1. Validates target and safety conditions
        2. Checks out the target commit
        3. Deletes SQLite databases (they regenerate from portage)
        4. Validates cache integrity
        
        Args:
            target: What to restore to:
                - str: Commit hash (full or abbreviated) or tag name
                - int: Relative index (0=current, -1=previous, -2=two back, etc.)
                - datetime: Restore to closest commit not after this time.
                           Note: Git stores timestamps at second precision only.
                           If multiple commits share the same timestamp, restores to the newest one.
            force: Skip safety checks (use with caution)
        
        Raises:
            RuntimeError: If uncommitted changes exist or restore fails
            ValueError: If target is invalid
            
        Example:
            # Restore to previous commit
            versioner.restore(-1)
            
            # Restore to specific commit
            versioner.restore("abc123")
            
            # Restore to specific time
            versioner.restore(datetime(2024, 10, 1))
            
            # Force restore despite uncommitted changes
            versioner.restore(-1, force=True)
        """
        # Validate target first (before checking uncommitted changes)
        # This ensures we give clear error messages about invalid targets
        commit_hash = self._resolve_target_to_commit(target)
        
        # Safety check for uncommitted changes (after validation)
        if not force and self._has_uncommitted_changes():
            raise RuntimeError(
                "Uncommitted changes detected. Create a snapshot first or use force=True"
            )
        
        try:
            # Checkout the target commit
            logger.info("Restoring to commit %s", commit_hash[:8])
            self.repo.git.checkout(commit_hash)
            
            # Delete SQLite databases so they regenerate from portage files
            self._cleanup_databases()
            
            logger.info("Successfully restored to commit %s", commit_hash[:8])
            
        except GitCommandError as e:
            raise RuntimeError(f"Failed to restore to {commit_hash}: {e}") from e
    
    def _resolve_target_to_commit(self, target: Union[str, int, datetime]) -> str:
        """Resolve a restore target to a specific commit hash.
        
        Args:
            target: Commit hash, tag name, relative index, or datetime
        
        Returns:
            Full commit hash
        
        Raises:
            ValueError: If target cannot be resolved
        """
        if isinstance(target, str):
            # Try as tag first, then as commit hash
            try:
                # Check if it's a tag
                if target in [tag.name for tag in self.repo.tags]:
                    commit = self.repo.tags[target].commit
                    logger.info("Resolved tag '%s' to commit %s", target, commit.hexsha[:8])
                    return commit.hexsha
            except Exception:
                pass
            
            # Try as commit hash (full or abbreviated)
            try:
                commit = self.repo.commit(target)
                return commit.hexsha
            except Exception as e:
                raise ValueError(f"Invalid commit hash or tag '{target}': {e}") from e
        
        elif isinstance(target, int):
            # Relative index (0=current, -1=previous, etc.)
            try:
                # Get commits in reverse chronological order
                commits = list(self.repo.iter_commits(max_count=abs(target) + 1))
                index = abs(target) if target < 0 else target
                if index >= len(commits):
                    raise ValueError(
                        f"Relative index {target} is out of range "
                        f"(only {len(commits)} commits available)"
                    )
                return commits[index].hexsha
            except Exception as e:
                raise ValueError(f"Failed to resolve relative index {target}: {e}") from e
        
        elif isinstance(target, datetime):
            # Find commit closest to the given datetime (but not after target)
            try:
                # Get all commits
                commits = list(self.repo.iter_commits())
                if not commits:
                    raise ValueError("No commits available for datetime restore")
                
                # Note: Git only stores timestamps at second precision.
                # When multiple commits share the same timestamp, we return the first one found
                # (newest with that timestamp, since iter_commits goes newest->oldest).
                target_timestamp = target.timestamp()
                best_commit = None
                best_diff = float('inf')
                
                for commit in commits:
                    commit_timestamp = commit.committed_date
                    if commit_timestamp <= target_timestamp:
                        diff = target_timestamp - commit_timestamp
                        # Use < (not <=) so first commit with a given diff wins
                        # This selects the newest commit with the closest timestamp
                        if diff < best_diff:
                            best_diff = diff
                            best_commit = commit
                
                if best_commit is None:
                    raise ValueError(
                        f"No commits found before {target}. "
                        f"Earliest commit is at {datetime.fromtimestamp(commits[-1].committed_date)}"
                    )
                
                logger.info(
                    "Resolved datetime %s to commit %s (%s)",
                    target,
                    best_commit.hexsha[:8],
                    datetime.fromtimestamp(best_commit.committed_date)
                )
                return best_commit.hexsha
                
            except Exception as e:
                raise ValueError(f"Failed to resolve datetime {target}: {e}") from e
        
        else:
            raise ValueError(
                f"Invalid target type {type(target)}. "
                f"Expected str (commit hash), int (relative index), or datetime"
            )
    
    def _cleanup_databases(self) -> None:
        """Delete SQLite databases so they regenerate from portage files."""
        # Close all open databases first to avoid file handle issues
        self.parent_cache._storage.close_databases()
        
        # Find all .sqlite files in the cache
        for sqlite_file in self.root_dir.rglob("*.sqlite"):
            try:
                sqlite_file.unlink()
                logger.debug("Deleted SQLite database: %s", sqlite_file)
                
                # Also delete associated -shm and -wal files
                shm_file = sqlite_file.with_suffix('.sqlite-shm')
                wal_file = sqlite_file.with_suffix('.sqlite-wal')
                
                if shm_file.exists():
                    shm_file.unlink()
                if wal_file.exists():
                    wal_file.unlink()
                    
            except Exception as e:
                logger.warning("Failed to delete SQLite database %s: %s", sqlite_file, e)
    
    def snapshots(
        self,
        limit: Optional[int] = None,
        reverse_chronological: bool = True,
        tag_glob: Optional[str] = None
    ) -> Union[List[SnapshotInfo], Iterator[SnapshotInfo]]:
        """Iterate through snapshots with optional filtering and ordering.
        
        Args:
            limit: Maximum number of snapshots to return. If None, returns all snapshots
                  as an iterator. If specified, returns a list.
            reverse_chronological: If True, most recent first. If False, oldest first.
            tag_glob: Glob pattern to filter by tag name. If None, returns all snapshots.
                     If "*", returns only tagged snapshots. If a pattern like "v1.*",
                     returns only snapshots with matching tags.
        
        Returns:
            Iterator[SnapshotInfo] if limit is None, otherwise List[SnapshotInfo]
            
        Example:
            # Get all snapshots (iterator for efficiency)
            for snapshot in versioner.snapshots():
                print(f"{snapshot.commit_hash[:8]}: {snapshot.message}")
            
            # Get 10 most recent snapshots
            recent = versioner.snapshots(limit=10)
            
            # Get all tagged snapshots
            for snapshot in versioner.snapshots(tag_glob="*"):
                print(f"Tags: {snapshot.tags}")
            
            # Get production tags only
            for snapshot in versioner.snapshots(tag_glob="prod-*"):
                print(f"Production: {snapshot.tags[0]}")
            
            # Get oldest snapshots first
            for snapshot in versioner.snapshots(limit=5, reverse_chronological=False):
                print(f"Early: {snapshot.message}")
        """
        # Build tag lookup for efficient checking
        tag_lookup = {}  # commit_hash -> [tag_names]
        try:
            for tag_ref in self.repo.tags:
                commit_hash = tag_ref.commit.hexsha
                if commit_hash not in tag_lookup:
                    tag_lookup[commit_hash] = []
                tag_lookup[commit_hash].append(tag_ref.name)
        except Exception as e:
            logger.warning("Failed to build tag lookup: %s", e)
        
        def _generate_snapshots():
            """Generator function for snapshots."""
            try:
                # Get commits in requested order
                commits = self.repo.iter_commits(reverse=not reverse_chronological)
                
                for commit in commits:
                    # Get tags for this commit
                    commit_tags = tag_lookup.get(commit.hexsha, [])
                    
                    # Apply tag filtering
                    if tag_glob is not None:
                        # If no tags on this commit, skip it
                        if not commit_tags:
                            continue
                        # Check if any tags match the glob pattern
                        if not any(fnmatch.fnmatch(tag, tag_glob) for tag in commit_tags):
                            continue
                        # For filtered results, only include matching tags
                        commit_tags = [tag for tag in commit_tags if fnmatch.fnmatch(tag, tag_glob)]
                    
                    # Determine which branches contain this commit
                    branches = [
                        ref.name.replace('refs/heads/', '')
                        for ref in self.repo.refs
                        if hasattr(ref, 'commit') and ref.commit == commit
                    ]
                    
                    # Use current branch if commit is on it, otherwise first branch found
                    try:
                        current_branch = self.repo.active_branch.name
                        branch = current_branch if current_branch in branches else (branches[0] if branches else 'detached')
                    except Exception:
                        branch = branches[0] if branches else 'detached'
                    
                    yield SnapshotInfo(
                        commit_hash=commit.hexsha,
                        message=commit.message.strip(),
                        timestamp=datetime.fromtimestamp(commit.committed_date),
                        branch=branch,
                        tags=commit_tags
                    )
            except Exception as e:
                logger.error("Failed to iterate snapshots: %s", e)
        
        # Return list if limit specified, otherwise return iterator
        if limit is not None:
            result = []
            for i, snapshot in enumerate(_generate_snapshots()):
                if i >= limit:
                    break
                result.append(snapshot)
            return result
        else:
            return _generate_snapshots()
    
    def tag(self, name: str, commit: Optional[str] = None, message: str = "", force: bool = False) -> str:
        """Create a tag for a commit.
        
        Creates either a lightweight tag (if no message) or an annotated tag (with message).
        Tags provide semantic names for important snapshots like releases or milestones.
        
        Note: Safe to call during resync operations due to cache's optimistic concurrency model.
        
        Args:
            name: Tag name (e.g., "v1.0", "prod-2024-q4", "stable-config")
            commit: Commit hash to tag (default: HEAD/current commit)
            message: Optional annotation message (creates annotated tag if provided)
            force: If True, replace an existing tag with the same name.
        
        Returns:
            Commit hash that was tagged
        
        Raises:
            RuntimeError: If tag already exists or creation fails
            
        Example:
            # Tag current state
            versioner.snapshot_commit("Production configuration")
            versioner.tag("prod-2024-q4")
            
            # Tag with annotation message
            versioner.tag("v1.0", message="First production release")
            
            # Tag a specific commit
            versioner.tag("pre-migration", commit="abc123")
            
            # Later restore to tag
            versioner.restore("prod-2024-q4")
        """
        
        try:
            # Resolve commit (default to HEAD)
            target_commit = self.repo.commit(commit) if commit else self.repo.head.commit
            
            existing_tag_names = [tag.name for tag in self.repo.tags]
            if name in existing_tag_names:
                if not force:
                    raise RuntimeError(
                        f"Tag '{name}' already exists. Delete it first, use force=True, "
                        f"or choose a different name."
                    )
                try:
                    self.repo.git.tag("-d", name)
                    logger.info("Deleted existing tag '%s' prior to recreation", name)
                except GitCommandError as exc:
                    raise RuntimeError(f"Failed to delete existing tag '{name}': {exc}") from exc
            
            # Create annotated tag if message provided, lightweight otherwise
            if message:
                self.repo.create_tag(name, ref=target_commit, message=message)
                logger.info("Created annotated tag '%s' at commit %s", name, target_commit.hexsha[:8])
            else:
                self.repo.create_tag(name, ref=target_commit)
                logger.info("Created lightweight tag '%s' at commit %s", name, target_commit.hexsha[:8])
            
            return target_commit.hexsha
            
        except GitCommandError as e:
            raise RuntimeError(f"Failed to create tag '{name}': {e}") from e
    
    def branches(self) -> List[str]:
        """List all branches with current branch first.
        
        Returns:
            List of branch names, with current branch as first element
            
        Example:
            branches = versioner.branches()
            print(f"Current branch: {branches[0]}")
            print(f"Other branches: {', '.join(branches[1:])}")
        """
        try:
            current = self.repo.active_branch.name
            all_branches = [ref.name.replace('refs/heads/', '') 
                          for ref in self.repo.heads]
            
            # Put current branch first
            if current in all_branches:
                all_branches.remove(current)
            return [current] + sorted(all_branches)
            
        except Exception as e:
            logger.error("Failed to list branches: %s", e)
            return []
    
    def switch_branch(self, branch: str, create: bool = False) -> None:
        """Switch to a different branch.
        
        Args:
            branch: Branch name to switch to
            create: If True, create branch if it doesn't exist
        
        Raises:
            RuntimeError: If branch doesn't exist and create is False
            RuntimeError: If there are uncommitted changes
            
        Example:
            # Switch to existing branch
            versioner.switch_branch("production")
            
            # Create and switch to new branch
            versioner.switch_branch("feature-x", create=True)
        """
        # Check for uncommitted changes
        if self._has_uncommitted_changes():
            raise RuntimeError(
                "Cannot switch branches with uncommitted changes. "
                "Create a snapshot first."
            )
        
        try:
            # Check if branch exists
            branch_exists = any(
                ref.name == f'refs/heads/{branch}' or ref.name == branch
                for ref in self.repo.refs
            )
            
            if branch_exists:
                self.repo.git.checkout(branch)
                logger.info("Switched to branch '%s'", branch)
                
                # Cleanup databases after branch switch
                self._cleanup_databases()
                
            elif create:
                self.repo.create_head(branch)
                self.repo.git.checkout(branch)
                logger.info("Created and switched to new branch '%s'", branch)
            else:
                raise RuntimeError(
                    f"Branch '{branch}' does not exist. Use create=True to create it."
                )
                
        except GitCommandError as e:
            raise RuntimeError(f"Failed to switch to branch '{branch}': {e}") from e

