# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The file-drop transport: where requests are written and results are read.

This is the *writer* half of the mailbox, and it is deliberately manager-free.
Submitting a request and reading its result are pure filesystem operations, and
a client process has no business constructing a fleet coordinator to perform
them. The half that *executes* requests — draining intake, calling the manager,
publishing results — is the signaling adapter, and it holds one of these.

Splitting them this way is what lets the transport be swapped (or tested) with
no manager in sight, and what keeps a submitting client from importing the
scheduling layer at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.advance_result_serializable import (
    AdvanceResultSerializable,
)
from totodev_pub.case_manager_support.adopt import AdoptResult
from totodev_pub.case_manager_support.constants import RESULTS_SUBDIR
from totodev_pub.case_manager_support.shutdown import (
    ShutdownAck,
    shutdown_intake_dir,
    write_shutdown_request,
)

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)

MAILBOX_PROTOCOL_VERSION = 1


def arrival_order(path: Path) -> tuple[float, str]:
    """Sort key for intake drains: FIFO by file mtime, filename as tiebreak.

    Filenames are correlation ids (random UUIDs), so a bare ``sorted()`` would be
    UUID-lexicographic — effectively random relative to submission order. A file
    that vanishes mid-scan (crash cleanup, manual removal) sorts first and is
    skipped by the per-file load guard."""
    try:
        return (path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)


class FireRequest(BaseModel, FileMappedPydanticMixin):
    protocol_version: int = MAILBOX_PROTOCOL_VERSION
    correlation_id: str
    requested_at: str
    case_id: str | None = None
    case_folder: str | None = None
    trigger: str | None = None
    trigger_kwargs: dict[str, Any] = Field(default_factory=dict)


class AdoptRequest(BaseModel, FileMappedPydanticMixin):
    protocol_version: int = MAILBOX_PROTOCOL_VERSION
    correlation_id: str
    requested_at: str
    source_folder: str


class ReclassifyRequest(BaseModel, FileMappedPydanticMixin):
    """Ask the manager to switch a live pooled case to a different registered case type
    (``FolderBackedCase.case_reclassify_to`` executed inside the manager process).

    Addressing mirrors ``FireRequest``: exactly one of ``case_id`` / ``case_folder``.
    ``target_type`` is the bare registered class name (``case_object_type`` vocabulary) —
    class objects cannot travel through a file protocol."""

    protocol_version: int = MAILBOX_PROTOCOL_VERSION
    correlation_id: str
    requested_at: str
    case_id: str | None = None
    case_folder: str | None = None
    target_type: str = ""


class ReclassifyResult(BaseModel, FileMappedPydanticMixin):
    kind: str = "reclassify"
    status: str = "completed"
    correlation_id: str
    case_id: str | None = None
    from_type: str | None = None
    to_type: str | None = None
    case_state: str | None = None
    error: str | None = None
    completed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


@dataclass(frozen=True)
class RequestHandle:
    correlation_id: str
    result_path: Path


#: Every result type carries a ``kind``. Reading it is how a result is
#: identified — the alternative, sniffing for substrings in the raw text, works
#: right up until a case_id or a folder name happens to contain one.
_RESULT_TYPES = {
    "advance": AdvanceResultSerializable,
    "adopt": AdoptResult,
    "reclassify": ReclassifyResult,
    "shutdown": ShutdownAck,
}

MailboxResult = AdvanceResultSerializable | AdoptResult | ReclassifyResult | ShutdownAck


class MailboxTransport:
    """Paths, request writes, and result reads for one manager's mailbox."""

    def __init__(self, manager_dir: Path, policy: "CaseManagerPolicy") -> None:
        self._mgr_dir = Path(manager_dir)
        self._policy = policy

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def results_dir(self) -> Path:
        return self._mgr_dir / RESULTS_SUBDIR

    def fire_intake(self) -> Path:
        return self._fire_dir() / "intake"

    def adopt_intake(self) -> Path:
        return self._adopt_dir() / "intake"

    def reclassify_intake(self) -> Path:
        return self._reclassify_dir() / "intake"

    def shutdown_intake(self) -> Path:
        return shutdown_intake_dir(self._mgr_dir, self._policy)

    def request_intakes(self) -> tuple[Path, ...]:
        """The intakes a signaling adapter is responsible for draining.

        Shutdown is absent on purpose: it is process control, owned by the host,
        and it is served whether or not any adapter exists."""
        return (self.fire_intake(), self.adopt_intake(), self.reclassify_intake())

    def _fire_dir(self) -> Path:
        return self._mgr_dir / self._policy.fire_mailbox_subdir

    def _adopt_dir(self) -> Path:
        return self._mgr_dir / self._policy.adopt_mailbox_subdir

    def _reclassify_dir(self) -> Path:
        return self._mgr_dir / self._policy.reclassify_mailbox_subdir

    def fire_stage(self, stage: str, case_key: str = "") -> Path:
        """``intake`` / ``pending`` / ``firing`` / ``malformed`` for the fire mailbox."""
        base = self._fire_dir() / stage
        return base / case_key if case_key else base

    def adopt_stage(self, stage: str) -> Path:
        return self._adopt_dir() / stage

    def reclassify_stage(self, stage: str, case_key: str = "") -> Path:
        base = self._reclassify_dir() / stage
        return base / case_key if case_key else base

    def ensure_dirs(self) -> None:
        for p in (
            self.fire_intake(),
            self.fire_stage("malformed"),
            self.adopt_intake(),
            self.adopt_stage("pending"),
            self.reclassify_intake(),
            self.reclassify_stage("malformed"),
            self.reclassify_stage("executing"),
            self.shutdown_intake(),
            self.results_dir(),
        ):
            p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Submitting
    # ------------------------------------------------------------------

    def submit_fire(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        trigger: str | None = None,
        trigger_kwargs: dict | None = None,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        corr = correlation_id or str(uuid.uuid4())
        return self._publish_request(
            FireRequest(
                correlation_id=corr,
                requested_at=_utc_stamp(),
                case_id=case_id,
                case_folder=str(case_folder) if case_folder else None,
                trigger=trigger,
                trigger_kwargs=trigger_kwargs or {},
            ),
            self.fire_intake(),
            corr,
        )

    def submit_adopt(
        self,
        *,
        source_folder: Path,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        corr = correlation_id or str(uuid.uuid4())
        return self._publish_request(
            AdoptRequest(
                correlation_id=corr,
                requested_at=_utc_stamp(),
                source_folder=str(Path(source_folder).resolve()),
            ),
            self.adopt_intake(),
            corr,
        )

    def submit_reclassify(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        target_type: str,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        corr = correlation_id or str(uuid.uuid4())
        return self._publish_request(
            ReclassifyRequest(
                correlation_id=corr,
                requested_at=_utc_stamp(),
                case_id=case_id,
                case_folder=str(case_folder) if case_folder else None,
                target_type=target_type,
            ),
            self.reclassify_intake(),
            corr,
        )

    def submit_shutdown(
        self,
        *,
        graceful: bool = False,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        self.ensure_dirs()
        corr, _path = write_shutdown_request(
            self.shutdown_intake(),
            graceful=graceful,
            reason=reason,
            correlation_id=correlation_id,
        )
        return RequestHandle(corr, self.result_path(corr))

    def _publish_request(self, request: Any, intake: Path, corr: str) -> RequestHandle:
        """Write a request into intake so it can never be seen half-written.

        The dotfile-then-rename is the whole protocol: a drain skips hidden
        files, so a reader either sees a complete request or nothing at all.
        """
        self.ensure_dirs()
        tmp = intake / f".{corr}.yaml"
        request.save(str(tmp), retain_lock=False)
        os.replace(tmp, intake / f"{corr}.yaml")
        return RequestHandle(corr, self.result_path(corr))

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def result_path(self, correlation_id: str) -> Path:
        return self.results_dir() / f"{correlation_id}.yaml"

    def publish_result(self, correlation_id: str, result: Any) -> None:
        path = self.result_path(correlation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        result.save(str(path), retain_lock=False)

    def poll_result(self, handle: RequestHandle) -> MailboxResult | None:
        """Load a result, identified by its own ``kind`` field.

        Returns None while the result is absent, unreadable, or half-written —
        all three are "not ready yet" from a caller's point of view, and a
        partial file is indistinguishable from a missing one on a poll.
        """
        try:
            text = handle.result_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        if not isinstance(payload, dict):
            return None
        result_type = _RESULT_TYPES.get(payload.get("kind"))
        if result_type is None:
            logger.warning(
                "Result %s has kind %r, which this version does not know how to read",
                handle.result_path.name,
                payload.get("kind"),
            )
            return None
        try:
            return result_type.model_validate(payload)
        except Exception:
            logger.warning(
                "Result %s claims kind %r but does not validate as one",
                handle.result_path.name,
                payload.get("kind"),
                exc_info=True,
            )
            return None

    async def wait_result(
        self,
        handle: RequestHandle,
        *,
        timeout: float = 30.0,
        poll_secs: float = 0.05,
    ) -> MailboxResult | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            result = self.poll_result(handle)
            if result is not None:
                return result
            await asyncio.sleep(poll_secs)
        return None

    def sweep_old_results(self) -> None:
        ttl = self._policy.result_ttl_secs
        now = time.time()
        for path in self.results_dir().glob("*.yaml"):
            try:
                if now - path.stat().st_mtime > ttl:
                    path.unlink()
            except OSError:
                pass

    def oldest_request_age_secs(self) -> float | None:
        """Age of the oldest unserved request, or None when nothing is waiting.

        The liveness signal for "requests are arriving but nobody is draining
        them". Shutdown is excluded — it has its own pickup path and would
        otherwise report neglect the instant it is submitted."""
        now = time.time()
        oldest: float | None = None
        for intake in self.request_intakes():
            if not intake.exists():
                continue
            for path in intake.glob("*.yaml"):
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if oldest is None or age > oldest:
                    oldest = age
        return oldest


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
