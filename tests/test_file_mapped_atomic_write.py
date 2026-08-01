# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""A reader must never see a file half-written.

Truncating in place opens a window in which the file on disk is neither the old
content nor the new one. Every reader in this library turns a parse failure into
an empty dict — which a model with required fields reports as a validation
error, and a model whose fields all have defaults **silently accepts as a valid
default object**. The second outcome is the dangerous one: nothing raises, and
the caller acts on data that was never written.

That window is not hypothetical for files rewritten on a sub-second cadence,
like the manager manifest's heartbeat, read concurrently by out-of-process
clients and by a watchdog thread.
"""

import os
import threading
import time
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin


class Doc(BaseModel, FileMappedPydanticMixin):
    name: str = "unset"
    payload: str = ""
    count: int = 0


def _save(path: Path, **fields) -> None:
    doc = Doc(**fields)
    doc.save(str(path), retain_lock=False)


def test_a_concurrent_reader_never_sees_a_partial_file(tmp_path):
    """The property that matters, exercised the way it actually fails: under load.

    A big payload widens the write window enough that in-place truncation loses
    this reliably; write-then-rename makes the window structurally impossible.
    """
    target = tmp_path / "doc.yaml"
    big = "x" * 400_000
    _save(target, name="initial", payload=big, count=0)

    stop = threading.Event()
    torn: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                torn.append(f"unreadable: {exc!r}")
                continue
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                torn.append(f"unparseable: {exc!r}")
                continue
            if not isinstance(data, dict) or data.get("name") not in ("initial", "rewritten"):
                torn.append(f"incoherent: {str(data)[:80]}")
            elif len(data.get("payload", "")) != len(big):
                torn.append(f"truncated payload: {len(data.get('payload', ''))}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for i in range(40):
            _save(target, name="rewritten", payload=big, count=i)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert torn == [], f"{len(torn)} torn read(s), first: {torn[0]}"


def test_the_replacement_is_a_rename_not_a_truncation(tmp_path):
    """Same-directory rename is what makes it atomic; a cross-device one is not.

    Pinning the inode change is how this test notices if someone "simplifies"
    the write back to an in-place one.
    """
    target = tmp_path / "doc.yaml"
    _save(target, name="first")
    before = target.stat().st_ino

    _save(target, name="second")

    assert target.stat().st_ino != before, "the file was replaced, not overwritten in place"
    assert Doc.load(str(target), acquire_lock=False).name == "second"


def test_no_temp_file_survives_a_successful_write(tmp_path):
    target = tmp_path / "doc.yaml"
    for i in range(5):
        _save(target, name="x", count=i)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doc.yaml"]


def test_the_temp_file_is_hidden_and_beside_its_target(tmp_path, monkeypatch):
    """Two properties that make the rename work in practice rather than in principle.

    Hidden, because directory scanners here skip dotfiles precisely so a partial
    write cannot be picked up. Beside its target, because a rename across
    filesystems is not atomic — and usually not even possible.
    """
    import tempfile as tempfile_mod

    import totodev_pub.file_mapped_pydantic_mixin as mixin

    created: list[str] = []
    real_mkstemp = tempfile_mod.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(mixin.tempfile, "mkstemp", spy)
    target = tmp_path / "doc.yaml"
    _save(target, name="x")

    assert len(created) == 1
    temp = Path(created[0])
    assert temp.name.startswith("."), f"temp file was visible to scanners: {temp.name}"
    assert temp.parent == target.parent, "temp file must share the target's filesystem"


def test_a_failed_write_leaves_no_debris_and_no_damage(tmp_path, monkeypatch):
    """A serialization failure must not destroy what was already there."""
    target = tmp_path / "doc.yaml"
    _save(target, name="survivor")

    import totodev_pub.file_mapped_pydantic_mixin as mixin

    def explode(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(mixin.os, "replace", explode)
    with pytest.raises(OSError, match="disk went away"):
        _save(target, name="doomed")
    monkeypatch.undo()

    assert Doc.load(str(target), acquire_lock=False).name == "survivor"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doc.yaml"], "temp file cleaned up"


def test_existing_permissions_are_preserved(tmp_path):
    """mkstemp creates 0600; replacing a readable file must not make it private."""
    target = tmp_path / "doc.yaml"
    _save(target, name="first")
    os.chmod(target, 0o644)

    _save(target, name="second")

    assert target.stat().st_mode & 0o777 == 0o644


def test_a_first_write_still_creates_the_file(tmp_path):
    target = tmp_path / "fresh" / "doc.yaml"
    target.parent.mkdir(parents=True)   # save() locks before it writes, so the dir must exist
    _save(target, name="created")
    assert Doc.load(str(target), acquire_lock=False).name == "created"
