# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Shutdown-request protocol (§6) — shared by the host (cooperative pickup), the
watchdog (wedged pickup), and CaseManagerClient.submit_shutdown(). One parser,
one precedence rule, zero drift.

Shutdown is process control, not fleet mail, which is why the host owns pickup
and serves it whether or not any request transport exists.

Protocol: ANY non-hidden file in the shutdown mailbox's intake/ dir triggers a
shutdown. Parsed file content is authoritative; the SIGTERM filename token
applies only to unparseable or hand-touched files. The mailbox is a recovery
lever, not a decommission lever — every mailbox-triggered exit is nonzero.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

logger = logging.getLogger(__name__)

SHUTDOWN_PROTOCOL_VERSION = 1
# Case-insensitive filename substring that requests a graceful drain on files
# whose content does not parse as a ShutdownRequest.
GRACEFUL_FILENAME_TOKEN = "sigterm"


class ShutdownRequest(BaseModel, FileMappedPydanticMixin):
    protocol_version: int = SHUTDOWN_PROTOCOL_VERSION
    correlation_id: str
    requested_at: str
    graceful: bool = False
    reason: str | None = None


class ShutdownAck(BaseModel, FileMappedPydanticMixin):
    """Result written (graceful path only) before the drain begins, so
    poll_result/wait_result resolve normally. On the immediate path the handle
    may never resolve — the manifest's stopped_at is the real confirmation."""

    kind: Literal["shutdown"] = "shutdown"
    correlation_id: str
    acknowledged_at: str
    graceful: bool
    reason: str | None = None


@dataclass(frozen=True)
class ShutdownDirective:
    """Normalized pickup result, whichever path (tick or watchdog) saw it."""

    graceful: bool
    reason: str | None
    correlation_id: str | None
    source_path: Path


def shutdown_intake_dir(manager_dir: Path, policy) -> Path:
    return manager_dir / policy.shutdown_mailbox_subdir / "intake"


def scan_shutdown_intake(intake: Path) -> ShutdownDirective | None:
    """Return a directive for the first non-hidden file in intake/, or None.

    "Non-hidden" is load-bearing: the structured API writes a dotfile and
    os.replace()s it into place; counting dotfiles would fire on a
    half-written request."""
    if not intake.exists():
        return None
    entries = sorted(
        p for p in intake.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    if not entries:
        return None
    path = entries[0]
    try:
        req = ShutdownRequest.load(str(path), acquire_lock=False)
        # Precedence rule: parsed content is authoritative; filename ignored.
        return ShutdownDirective(
            graceful=req.graceful,
            reason=req.reason,
            correlation_id=req.correlation_id,
            source_path=path,
        )
    except Exception:
        return ShutdownDirective(
            graceful=GRACEFUL_FILENAME_TOKEN in path.name.lower(),
            reason=None,
            correlation_id=None,
            source_path=path,
        )


def write_shutdown_request(
    intake: Path,
    *,
    graceful: bool = False,
    reason: str | None = None,
    correlation_id: str | None = None,
) -> tuple[str, Path]:
    """Structured writer (used by MailboxTransport.submit_shutdown). Returns
    (correlation_id, final_path)."""
    intake.mkdir(parents=True, exist_ok=True)
    corr = correlation_id or str(uuid.uuid4())
    req = ShutdownRequest(
        correlation_id=corr,
        requested_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        graceful=graceful,
        reason=reason,
    )
    # Content is authoritative, but carry the token in the name anyway so a
    # human ls'ing the intake dir can read the intent.
    name = f"{corr}.SIGTERM.yaml" if graceful else f"{corr}.yaml"
    tmp = intake / f".{name}"
    final = intake / name
    req.save(str(tmp), retain_lock=False)
    os.replace(tmp, final)
    return corr, final


def write_shutdown_ack(results_dir: Path, directive: ShutdownDirective) -> None:
    if directive.correlation_id is None:
        return  # hand-touched file; nobody is polling a handle
    ack = ShutdownAck(
        correlation_id=directive.correlation_id,
        acknowledged_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        graceful=directive.graceful,
        reason=directive.reason,
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    tmp = results_dir / f".{directive.correlation_id}.yaml"
    final = results_dir / f"{directive.correlation_id}.yaml"
    ack.save(str(tmp), retain_lock=False)
    os.replace(tmp, final)


def discard_stale_requests(intake: Path) -> int:
    """Startup hygiene (§6): a request still in intake/ at recover() belongs to
    a previous process and must be logged and discarded, never honored —
    otherwise one stale request induces a restart-immediately loop."""
    if not intake.exists():
        return 0
    count = 0
    for path in sorted(intake.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            logger.warning(
                "Discarding stale shutdown request %s found at recover(); "
                "shutdown requests are never honored across a restart.",
                path.name,
            )
            path.unlink(missing_ok=True)
            count += 1
    return count
