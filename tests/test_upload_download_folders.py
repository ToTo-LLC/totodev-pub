# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for UploadDownloadFolders example."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from totodev_pub.cached_file_folders_support.examples.upload_download_folders import (
    AmbiguousPayload,
    TicketConsumed,
    TicketNotReady,
    TicketSessionMismatch,
    TicketUnavailable,
    UnsafeArchive,
    UploadDownloadFolders,
)


class _Clock:
    """Injectable clock for deadline tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest_asyncio.fixture
async def slots(tmp_path: Path, clock: _Clock) -> UploadDownloadFolders:
    root = tmp_path / "scratch"
    mgr = UploadDownloadFolders(
        root,
        minimum_retention=timedelta(minutes=30),
        available_for=timedelta(minutes=30),
        now=clock,
    )
    await mgr.startup(sweep=False)
    yield mgr
    mgr.close()


@pytest.mark.asyncio
async def test_create_leaves_empty_work_dir_and_resolve_returns_it(
    slots: UploadDownloadFolders,
) -> None:
    ticket = await slots.create_download()
    assert ticket.work_dir.is_dir()
    assert list(ticket.work_dir.iterdir()) == []

    (ticket.work_dir / "report.pdf").write_bytes(b"%PDF")
    got = await slots.resolve(ticket.token, kind="download")
    assert got.token == ticket.token
    assert got.work_dir == ticket.work_dir
    assert (got.work_dir / "report.pdf").read_bytes() == b"%PDF"


@pytest.mark.asyncio
async def test_upload_token_does_not_resolve_as_download(
    slots: UploadDownloadFolders,
) -> None:
    ticket = await slots.create_upload()
    (ticket.work_dir / "a.txt").write_text("hi")
    with pytest.raises(TicketUnavailable, match="no longer available"):
        await slots.resolve(ticket.token, kind="download")


@pytest.mark.asyncio
async def test_available_for_zero_raises_while_directory_still_exists(
    tmp_path: Path, clock: _Clock
) -> None:
    mgr = UploadDownloadFolders(
        tmp_path / "scratch",
        available_for=timedelta(0),
        minimum_retention=timedelta(hours=1),
        now=clock,
    )
    await mgr.startup(sweep=False)
    try:
        ticket = await mgr.create_download()
        (ticket.work_dir / "f.txt").write_text("x")
        assert ticket.work_dir.exists()
        with pytest.raises(TicketUnavailable):
            await mgr.resolve(ticket.token, kind="download")
        assert ticket.work_dir.exists()
    finally:
        mgr.close()


@pytest.mark.asyncio
async def test_sweep_respects_retain_until(tmp_path: Path, clock: _Clock) -> None:
    mgr = UploadDownloadFolders(
        tmp_path / "scratch",
        available_for=timedelta(minutes=5),
        minimum_retention=timedelta(minutes=10),
        now=clock,
    )
    await mgr.startup(sweep=False)
    try:
        keep = await mgr.create_download(minimum_retention=timedelta(minutes=10))
        (keep.work_dir / "k.txt").write_text("keep")
        drop = await mgr.create_download(minimum_retention=timedelta(0))
        (drop.work_dir / "d.txt").write_text("drop")

        removed = await mgr.startup(sweep=True)
        assert removed >= 1
        # drop should be gone
        with pytest.raises(TicketUnavailable):
            await mgr.resolve(drop.token, kind="download")
        # keep still inside retain window — resolve still works while available
        got = await mgr.resolve(keep.token, kind="download")
        assert (got.work_dir / "k.txt").read_text() == "keep"
    finally:
        mgr.close()


@pytest.mark.asyncio
async def test_permission_error_defers_cleanup_and_later_sweep_removes(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = UploadDownloadFolders(
        tmp_path / "scratch",
        available_for=timedelta(minutes=30),
        minimum_retention=timedelta(0),
        now=clock,
    )
    await mgr.startup(sweep=False)
    try:
        ticket = await mgr.create_upload()
        (ticket.work_dir / "blob.bin").write_bytes(b"data")

        calls = {"n": 0}
        original = UploadDownloadFolders._clear_payload

        def flaky(self: UploadDownloadFolders, work_dir: Path) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("simulated lock")
            return original(self, work_dir)

        monkeypatch.setattr(UploadDownloadFolders, "_clear_payload", flaky)

        await mgr.discard(ticket.token, kind="upload")
        # Sentinel still findable after deferred cleanup
        assert mgr._cache.find_file(
            f"{ticket.token}/ticket.sentinel", ("upload",)
        ) is not None

        removed = await mgr.startup(sweep=True)
        assert removed >= 1
        assert mgr._cache.find_file(
            f"{ticket.token}/ticket.sentinel", ("upload",)
        ) is None
    finally:
        mgr.close()


@pytest.mark.asyncio
async def test_session_binding(slots: UploadDownloadFolders) -> None:
    bound = await slots.create_download(session_id="sess-a")
    (bound.work_dir / "a.txt").write_text("a")
    got = await slots.resolve(bound.token, kind="download", session_id="sess-a")
    assert got.token == bound.token

    with pytest.raises(TicketSessionMismatch):
        await slots.resolve(bound.token, kind="download", session_id="sess-b")
    with pytest.raises(TicketSessionMismatch):
        await slots.resolve(bound.token, kind="download")

    unbound = await slots.create_download()
    (unbound.work_dir / "b.txt").write_text("b")
    got2 = await slots.resolve(unbound.token, kind="download")
    assert got2.token == unbound.token


@pytest.mark.asyncio
async def test_upload_once_seal_then_resolve_consumed_move_out_still_works(
    slots: UploadDownloadFolders, tmp_path: Path
) -> None:
    ticket = await slots.create_upload(once=True)
    payload = ticket.work_dir / "upload.dat"
    payload.write_bytes(b"payload-bytes")

    await slots.seal(ticket.token, kind="upload")
    with pytest.raises(TicketConsumed):
        await slots.resolve(ticket.token, kind="upload")

    dest = tmp_path / "permanent" / "out.dat"
    result = await slots.move_out(
        ticket.token, dest, kind="upload"
    )
    assert result == dest
    assert dest.read_bytes() == b"payload-bytes"
    assert not payload.exists()
    with pytest.raises(TicketUnavailable):
        await slots.resolve(ticket.token, kind="upload")


@pytest.mark.asyncio
async def test_download_once_not_ready_then_consume(
    slots: UploadDownloadFolders,
) -> None:
    ticket = await slots.create_download(once=True)
    with pytest.raises(TicketNotReady):
        await slots.resolve(ticket.token, kind="download")

    (ticket.work_dir / "report.pdf").write_bytes(b"pdf")
    first = await slots.resolve(ticket.token, kind="download")
    assert (first.work_dir / "report.pdf").exists()

    with pytest.raises(TicketConsumed):
        await slots.resolve(ticket.token, kind="download")


@pytest.mark.asyncio
async def test_move_out_ambiguous_requires_name(
    slots: UploadDownloadFolders, tmp_path: Path
) -> None:
    ticket = await slots.create_upload()
    (ticket.work_dir / "one.txt").write_text("1")
    (ticket.work_dir / "two.txt").write_text("2")

    with pytest.raises(AmbiguousPayload):
        await slots.move_out(ticket.token, tmp_path / "out", kind="upload")

    dest = tmp_path / "chosen.txt"
    result = await slots.move_out(
        ticket.token, dest, kind="upload", name="two.txt"
    )
    assert result.read_text() == "2"


@pytest.mark.asyncio
async def test_unsafe_zip_and_safe_unzip(tmp_path: Path, clock: _Clock) -> None:
    mgr = UploadDownloadFolders(
        tmp_path / "scratch",
        auto_unzip=True,
        now=clock,
    )
    await mgr.startup(sweep=False)
    try:
        # Unsafe: path traversal
        bad = await mgr.create_upload(auto_unzip=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../outside.txt", "evil")
        (bad.work_dir / "bad.zip").write_bytes(buf.getvalue())
        outside = bad.work_dir.parent / "outside.txt"
        with pytest.raises(UnsafeArchive):
            await mgr.seal(bad.token, kind="upload")
        assert not outside.exists()
        assert not (bad.work_dir / "unpacked").exists()

        # Safe: single zip expands under unpacked/ and zip is removed
        good = await mgr.create_upload(auto_unzip=True)
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as zf:
            zf.writestr("inner/hello.txt", "hello")
        zip_path = good.work_dir / "good.zip"
        zip_path.write_bytes(buf2.getvalue())
        sealed = await mgr.seal(good.token, kind="upload")
        assert not zip_path.exists()
        unpacked = sealed.work_dir / "unpacked" / "inner" / "hello.txt"
        assert unpacked.read_text() == "hello"
    finally:
        mgr.close()


@pytest.mark.asyncio
async def test_discard_removes_inside_minimum_retention(
    tmp_path: Path, clock: _Clock
) -> None:
    mgr = UploadDownloadFolders(
        tmp_path / "scratch",
        minimum_retention=timedelta(hours=2),
        available_for=timedelta(hours=2),
        now=clock,
    )
    await mgr.startup(sweep=False)
    try:
        ticket = await mgr.create_upload()
        (ticket.work_dir / "x.txt").write_text("x")
        await mgr.discard(ticket.token, kind="upload")
        with pytest.raises(TicketUnavailable):
            await mgr.resolve(ticket.token, kind="upload")
        assert mgr._cache.find_file(
            f"{ticket.token}/ticket.sentinel", ("upload",)
        ) is None
    finally:
        mgr.close()
