# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for FolderBackedCase.case_invoke_process."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from totodev_pub.folder_backed_case import FolderBackedCase, CaseInvokedProcessError
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import EV_INVOKED_PROCESS_FAILED


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


class SimpleCase(FolderBackedCase):
    asset_aliases = []
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^new==begin-->open==finish-->done^"]


def test_interface_alignment_includes_invoke_helpers():
    FolderBackedCase._assert_interface_alignment()
    assert hasattr(FolderBackedCase, "case_invoke_process")
    assert hasattr(FolderBackedCase, "case_invoke_threaded")


def test_invoke_process_happy_path_and_default_cwd(tmp_path):
    folder = tmp_path / "case-proc-ok"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        result = asyncio.run(
            case.case_invoke_process(
                sys.executable,
                "-c",
                "import os; print(os.getcwd(), end='')",
                env={},
            )
        )
        assert result.returncode == 0
        assert result.stdout == str(case.case_assets.folder)
        assert result.stderr == ""
    finally:
        case.case_detach()


def test_invoke_process_nonzero_raises_and_journals(tmp_path):
    folder = tmp_path / "case-proc-fail"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        with pytest.raises(CaseInvokedProcessError) as ei:
            asyncio.run(
                case.case_invoke_process(
                    sys.executable,
                    "-c",
                    "import sys; print('oops', file=sys.stderr); sys.exit(7)",
                    env={},
                )
            )
        err = ei.value
        assert err.returncode == 7
        assert err.program == sys.executable
        assert "oops" in err.stderr

        events = list(
            case._journal.primitive.events(
                label_glob=EV_INVOKED_PROCESS_FAILED, recent_first=True,
            )
        )
        assert len(events) == 1
        assert events[0].value == Path(sys.executable).name
        data = events[0].contents().as_dict()
        assert data["returncode"] == 7
        assert "oops" in data["stderr"]
        # Security: event must not carry argv or env
        assert "args" not in data
        assert "env" not in data
        assert "-c" not in str(data)
    finally:
        case.case_detach()


def test_invoke_process_allow_nonzero_retval(tmp_path):
    folder = tmp_path / "case-proc-allow"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        result = asyncio.run(
            case.case_invoke_process(
                sys.executable,
                "-c",
                "import sys; sys.exit(3)",
                env={},
                allow_nonzero_retval=True,
            )
        )
        assert result.returncode == 3
        events = list(
            case._journal.primitive.events(label_glob=EV_INVOKED_PROCESS_FAILED)
        )
        assert len(events) == 1
    finally:
        case.case_detach()


def test_invoke_process_rejects_none_env(tmp_path):
    folder = tmp_path / "case-proc-env"
    case = SimpleCase.create_case_in_folder(folder)
    try:
        with pytest.raises(TypeError, match="env is required"):
            asyncio.run(
                case.case_invoke_process(sys.executable, "-c", "pass", env=None)  # type: ignore[arg-type]
            )
    finally:
        case.case_detach()


def test_invoke_process_cancel_kills_child(tmp_path):
    folder = tmp_path / "case-proc-kill"
    case = SimpleCase.create_case_in_folder(folder)
    pid_path = case.case_assets.folder / "child.pid"

    async def _run_and_cancel():
        task = asyncio.create_task(
            case.case_invoke_process(
                sys.executable,
                "-c",
                (
                    "import os, pathlib, time;"
                    f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
                    "time.sleep(60)"
                ),
                env={},
                kill_grace_secs=1.0,
            )
        )
        for _ in range(50):
            if pid_path.exists() and pid_path.read_text().strip():
                break
            await asyncio.sleep(0.05)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pytest.fail("child never wrote pid file")

        pid = int(pid_path.read_text().strip())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Child should be gone shortly after kill-on-cancel.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            await asyncio.sleep(0.05)
        pytest.fail(f"child pid {pid} still alive after cancel")

    try:
        asyncio.run(_run_and_cancel())
    finally:
        case.case_detach()
