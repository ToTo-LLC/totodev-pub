# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""``overwrite_data_file`` writes atomically, and its temp file behaves.

The atomic write was already the default here; what it lacked was a temp file
that could be relied on. A name derived from the wall clock is not unique, so
two writers to one target could compute the same temp path and race each other
through it; and a *visible* temp name defeats the directory scanners in this
library that skip dotfiles precisely so an in-progress write cannot be mistaken
for real content.
"""

import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData


def _write(path: Path, data, **kwargs) -> None:
    LazyLoadedFileData.overwrite_data_file(data, str(path), **kwargs)


# ------------------------------------------------------------------ the basics


@pytest.mark.parametrize("name,loader", [
    ("cfg.yaml", yaml.safe_load),
    ("cfg.json", json.loads),
])
def test_a_round_trip_still_works(tmp_path, name, loader):
    target = tmp_path / name
    payload = {"database": {"host": "localhost", "port": 5432}}
    _write(target, payload)
    assert loader(target.read_text(encoding="utf-8")) == payload


def test_a_first_write_creates_the_file(tmp_path):
    target = tmp_path / "fresh.yaml"
    _write(target, {"a": 1})
    assert target.exists()


def test_no_temp_file_survives_a_successful_write(tmp_path):
    target = tmp_path / "cfg.yaml"
    for i in range(5):
        _write(target, {"n": i})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cfg.yaml"]


# ------------------------------------------------------------- the temp file


def test_the_temp_file_is_hidden_and_beside_its_target(tmp_path, monkeypatch):
    """Hidden so scanners skip it; co-located so the rename is actually atomic."""
    import totodev_pub.lazy_loaded_file_data as llfd

    seen: list[str] = []
    real_mkstemp = llfd.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        seen.append(path)
        return fd, path

    monkeypatch.setattr(llfd.tempfile, "mkstemp", spy)
    target = tmp_path / "cfg.yaml"
    _write(target, {"a": 1})

    assert len(seen) == 1
    temp = Path(seen[0])
    assert temp.name.startswith("."), f"temp file was visible to scanners: {temp.name}"
    assert temp.parent == target.parent, "a cross-filesystem rename would not be atomic"


def test_concurrent_writers_do_not_share_a_temp_path(tmp_path):
    """The collision the old clock-derived name allowed.

    Two writers to one target inside the same naming granule computed the *same*
    temp path: one truncated the other's partial write, and both raced to rename
    it. Unique names make that structurally impossible — so every temp path
    handed out here must be distinct, and the final file must be one writer's
    complete output rather than a blend.
    """
    import totodev_pub.lazy_loaded_file_data as llfd

    target = tmp_path / "contended.json"
    _write(target, {"writer": "initial"})

    handed_out: list[str] = []
    lock = threading.Lock()
    real_mkstemp = llfd.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        with lock:
            handed_out.append(path)
        return fd, path

    llfd.tempfile.mkstemp = spy
    errors: list[BaseException] = []
    try:
        def writer(tag: str) -> None:
            try:
                for _ in range(25):
                    _write(target, {"writer": tag, "filler": "x" * 20_000})
            except BaseException as exc:      # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        llfd.tempfile.mkstemp = real_mkstemp

    assert errors == [], f"a concurrent write failed: {errors[0]!r}"
    assert len(handed_out) == len(set(handed_out)), "two writers shared a temp path"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["contended.json"], "temp debris"
    final = json.loads(target.read_text(encoding="utf-8"))
    assert final["writer"] in ("a", "b", "c")
    assert len(final["filler"]) == 20_000, "the winner's write landed whole"


def test_a_concurrent_reader_never_sees_a_partial_file(tmp_path):
    """The property the whole mechanism exists for."""
    target = tmp_path / "cfg.json"
    big = "x" * 300_000
    _write(target, {"tag": "initial", "payload": big})

    stop = threading.Event()
    torn: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                torn.append(repr(exc))
                continue
            if data.get("tag") not in ("initial", "rewritten"):
                torn.append(f"incoherent: {str(data)[:60]}")
            elif len(data.get("payload", "")) != len(big):
                torn.append(f"truncated: {len(data.get('payload', ''))}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for _ in range(30):
            _write(target, {"tag": "rewritten", "payload": big})
    finally:
        stop.set()
        thread.join(timeout=15)

    assert torn == [], f"{len(torn)} torn read(s), first: {torn[0]}"


# ---------------------------------------------------------------- edge cases


def test_a_failed_write_leaves_no_debris_and_no_damage(tmp_path, monkeypatch):
    target = tmp_path / "cfg.yaml"
    _write(target, {"keep": "me"})

    import totodev_pub.lazy_loaded_file_data as llfd

    def explode(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(llfd.os, "replace", explode)
    with pytest.raises(OSError, match="disk went away"):
        _write(target, {"keep": "doomed"})
    monkeypatch.undo()

    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"keep": "me"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cfg.yaml"]


def test_existing_permissions_are_preserved(tmp_path):
    """mkstemp creates 0600; replacing a readable file must not make it private."""
    target = tmp_path / "cfg.yaml"
    _write(target, {"a": 1})
    os.chmod(target, 0o644)

    _write(target, {"a": 2})

    assert target.stat().st_mode & 0o777 == 0o644


def test_the_non_atomic_path_still_writes_in_place(tmp_path):
    """atomic=False keeps the inode, which is its only reason to exist."""
    target = tmp_path / "cfg.yaml"
    _write(target, {"a": 1})
    before = target.stat().st_ino

    _write(target, {"a": 2}, atomic=False)

    assert target.stat().st_ino == before
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"a": 2}


def test_the_atomic_path_replaces_the_inode(tmp_path):
    """The counterpart, and the reason atomic=False is offered at all."""
    target = tmp_path / "cfg.yaml"
    _write(target, {"a": 1})
    before = target.stat().st_ino

    _write(target, {"a": 2})

    assert target.stat().st_ino != before


def test_unsupported_formats_still_raise_before_any_temp_file(tmp_path):
    """A rejected format must not leave a temp file behind."""
    for name in ("cfg.toml", "cfg.csv", "cfg.tsv"):
        with pytest.raises(ValueError):
            _write(tmp_path / name, {"a": 1})
    assert list(tmp_path.iterdir()) == []


def test_writing_into_a_missing_directory_raises_cleanly(tmp_path):
    with pytest.raises(OSError):
        _write(tmp_path / "nope" / "cfg.yaml", {"a": 1})
    assert list(tmp_path.iterdir()) == []
