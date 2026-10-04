# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for hidden-directory handling when enumerating grouping keys.

Dot-directories inside the store (e.g. .git/) are skipped, but dot-directories
above the store root (e.g. ~/.claude/, ~/.cache/) must not hide the store's groupings.
"""

from pathlib import Path

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders


def _make_root(tmp_path: Path, *parents: str) -> Path:
    root = tmp_path.joinpath(*parents, "store")
    root.mkdir(parents=True)
    return root.resolve()


@pytest.mark.parametrize("parents", [(), (".hidden_parent",), (".a", "b", ".c")])
def test_groupings_found_regardless_of_dot_dirs_above_root(tmp_path: Path, parents: tuple) -> None:
    root = _make_root(tmp_path, *parents)
    for project in ["alpha", "beta", ".git"]:
        (root / "projects" / project).mkdir(parents=True)

    cache = CachedFileFolders("projects/{project}/", root)

    assert [g.grouping_key for g in cache.groupings()] == [("alpha",), ("beta",)]


def test_hidden_dir_at_any_level_inside_root_is_skipped(tmp_path: Path) -> None:
    root = _make_root(tmp_path, ".hidden_parent")
    (root / "groups" / "webapp" / "api").mkdir(parents=True)
    (root / "groups" / "webapp" / ".git").mkdir(parents=True)
    (root / "groups" / ".cache" / "api").mkdir(parents=True)

    cache = CachedFileFolders("groups/{project}/{category}/", root)

    assert [g.grouping_key for g in cache.groupings()] == [("webapp", "api")]
