# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The file-drop transport: where requests are written and results are read.

This is the *writer* half of the channel, and it is deliberately manager-free.
Submitting a request and reading its result are pure filesystem operations, and
a client process has no business constructing a fleet coordinator to perform
them. The half that *executes* requests — draining the queue, calling the
manager, publishing results — is the signaling adapter, and it holds one of these.

Splitting them this way is what lets the transport be swapped (or tested) with
no manager in sight, and what keeps a submitting client from importing the
scheduling layer at all.

**One queue, and the action lives inside the message.** Every request is a
``RequestEnvelope`` carrying an ``op`` field, and it travels
``queued/ → claimed/{case_key}/ → running/{case_key}/`` with the bytes unchanged
at every step — only the path moves, by atomic rename. That is the whole reason
state lives in the path: ``os.replace`` is atomic, so if two drainers ever raced
only one could win the claim, whereas rewriting a ``state:`` field inside a file
is not atomic and a crash mid-rewrite leaves a corrupt file.

This replaced four per-action mailboxes with three or four stage folders each.
The action was encoded in the *folder path*, which meant adding one action cost an
edit to four files, and three different words were in use for "in progress"
(``firing``, ``executing``, and adopt's ``pending``) while fire's ``pending``
meant "accepted, not started" — so ``pending`` denoted two opposite things in two
sibling mailboxes.

Maildir's mechanism is kept exactly: write to a hidden dotfile, then rename into
place. A reader skips hidden files, so it sees a complete request or nothing.
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
from totodev_pub.case_manager_support.constants import (
    CLAIMED_STAGE,
    FAILED_STAGE,
    QUEUED_STAGE,
    RESULTS_SUBDIR,
    RUNNING_STAGE,
    UNKNOWN_CASE_KEY,
)
from totodev_pub.case_manager_support.namespace_map import request_channel_dirs
from totodev_pub.case_manager_support.shutdown import (
    ShutdownAck,
    shutdown_intake_dir,
    write_shutdown_request,
)

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

logger = logging.getLogger(__name__)

#: Bumped 1 → 2 by the request-queue cutover. Version 1 messages were per-mailbox
#: request models with no ``op``; there is no reader for them, by decision.
MAILBOX_PROTOCOL_VERSION = 2

FIRE_OP = "fire"
ADOPT_OP = "adopt"
RECLASSIFY_OP = "reclassify"


def arrival_order(path: Path) -> tuple[float, str]:
    """Sort key for queue drains: FIFO by file mtime, filename as tiebreak.

    Filenames are correlation ids (random UUIDs), so a bare ``sorted()`` would be
    UUID-lexicographic — effectively random relative to submission order. A file
    that vanishes mid-scan (crash cleanup, manual removal) sorts first and is
    skipped by the per-file load guard."""
    try:
        return (path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)


def case_key_for(case_id: str | None) -> str:
    """The stage subfolder a request is filed under.

    Grouping by case is what makes a busy case visible in one directory listing,
    but not every request has an id to group by: a fire may address its case by
    folder, and an adopt's id is only read off its source on a best-effort basis.
    Those land in a reserved bucket rather than being dropped from the grouping —
    ``_unknown`` cannot collide with a real id, which is a base-36 time slug or a
    uuid4 hex.
    """
    return case_id or UNKNOWN_CASE_KEY


# ----------------------------------------------------------------------
# Payloads — the per-action part of a message
# ----------------------------------------------------------------------


class FirePayload(BaseModel):
    """Make a pooled case take one step. Addressed by envelope ``case_id`` or by folder."""

    case_folder: str | None = None
    trigger: str | None = None
    trigger_kwargs: dict[str, Any] = Field(default_factory=dict)


class AdoptPayload(BaseModel):
    """Take a detached case folder into managed storage.

    The envelope's ``case_id`` is not submitter-supplied — the adapter stamps it,
    read off the source's own record, before the transfer starts. Adopt *consumes*
    its source, so after a crash the request alone cannot say whether the case
    landed; the stamped id is what lets recovery ask the store instead of guessing.
    """

    source_folder: str


class ReclassifyPayload(BaseModel):
    """Switch a live pooled case to a different registered case type.

    ``target_type`` is the bare registered class name — class objects cannot
    travel through a file protocol.
    """

    case_folder: str | None = None
    target_type: str = ""


#: Every op's payload type. Reading ``op`` and validating against this is how a
#: message is understood; the alternative, sniffing for fields, works right up
#: until two ops share a field name.
_PAYLOAD_TYPES: dict[str, type[BaseModel]] = {
    FIRE_OP: FirePayload,
    ADOPT_OP: AdoptPayload,
    RECLASSIFY_OP: ReclassifyPayload,
}


class RequestEnvelope(BaseModel, FileMappedPydanticMixin):
    """One request, whatever the action. The ``op`` field is what it means.

    ``case_id`` sits on the envelope rather than in the payload because the queue
    keys its stage folders by it and recovery reads it without caring which op it
    is. ``attempts`` is carried for a future retry policy and is not yet
    incremented by anything.
    """

    protocol_version: int = MAILBOX_PROTOCOL_VERSION
    op: str
    id: str
    attempts: int = 0
    requested_at: str
    case_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def case_key(self) -> str:
        return case_key_for(self.case_id)

    def typed_payload(self) -> BaseModel:
        """Validate the payload against this op's model.

        Raises ``ValueError`` on an unknown op, and lets pydantic raise on a
        payload that does not match. Both are malformed-request territory: the
        drain dead-letters and tells the submitter, rather than guessing.
        """
        payload_type = _PAYLOAD_TYPES.get(self.op)
        if payload_type is None:
            raise ValueError(f"unknown request op {self.op!r}")
        return payload_type.model_validate(self.payload)


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
    """Paths, request writes, and result reads for one manager's request channel."""

    def __init__(self, manager_dir: Path, policy: "CaseManagerPolicy") -> None:
        self._mgr_dir = Path(manager_dir)
        self._policy = policy

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def requests_root(self) -> Path:
        return self._mgr_dir / self._policy.requests_subdir

    def queued(self) -> Path:
        return self.requests_root() / QUEUED_STAGE

    def claimed(self, case_key: str = "") -> Path:
        base = self.requests_root() / CLAIMED_STAGE
        return base / case_key if case_key else base

    def running(self, case_key: str = "") -> Path:
        base = self.requests_root() / RUNNING_STAGE
        return base / case_key if case_key else base

    def failed(self) -> Path:
        return self.requests_root() / FAILED_STAGE

    def results_dir(self) -> Path:
        return self.requests_root() / RESULTS_SUBDIR

    def shutdown_intake(self) -> Path:
        return shutdown_intake_dir(self._mgr_dir, self._policy)

    def ensure_dirs(self) -> None:
        """Create the request channel, from the layout declaration.

        A client may submit into a filespace whose manager has never run, so the
        transport cannot assume provisioning happened. The list comes from
        ``namespace_map`` rather than being written here again — two lists of the
        same directories is how a documented layout stops matching the real one.
        """
        for sub in request_channel_dirs(self._policy):
            (self._mgr_dir / sub).mkdir(parents=True, exist_ok=True)

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
        payload = FirePayload(
            case_folder=str(case_folder) if case_folder else None,
            trigger=trigger,
            trigger_kwargs=trigger_kwargs or {},
        )
        return self._publish(FIRE_OP, payload, case_id=case_id, correlation_id=correlation_id)

    def submit_adopt(
        self,
        *,
        source_folder: Path,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        payload = AdoptPayload(source_folder=str(Path(source_folder).resolve()))
        return self._publish(ADOPT_OP, payload, correlation_id=correlation_id)

    def submit_reclassify(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        target_type: str,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        payload = ReclassifyPayload(
            case_folder=str(case_folder) if case_folder else None,
            target_type=target_type,
        )
        return self._publish(RECLASSIFY_OP, payload, case_id=case_id, correlation_id=correlation_id)

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

    def _publish(
        self,
        op: str,
        payload: BaseModel,
        *,
        case_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        """Write a request into ``queued/`` so it can never be seen half-written.

        The dotfile-then-rename is the whole protocol: a drain skips hidden
        files, so a reader either sees a complete request or nothing at all.
        """
        self.ensure_dirs()
        corr = correlation_id or str(uuid.uuid4())
        envelope = RequestEnvelope(
            op=op,
            id=corr,
            requested_at=_utc_stamp(),
            case_id=case_id,
            payload=payload.model_dump(),
        )
        queued = self.queued()
        tmp = queued / f".{corr}.yaml"
        envelope.save(str(tmp), retain_lock=False)
        os.replace(tmp, queued / f"{corr}.yaml")
        return RequestHandle(corr, self.result_path(corr))

    # ------------------------------------------------------------------
    # Stage moves
    # ------------------------------------------------------------------

    def move_to_stage(self, path: Path, destination: Path) -> Path:
        """Move a request file between stages, atomically, and return its new path.

        The bytes are never rewritten — a stage change is a rename and nothing
        else, which is what makes it crash-safe.
        """
        destination.mkdir(parents=True, exist_ok=True)
        moved = destination / path.name
        os.replace(path, moved)
        return moved

    def dead_letter(self, path: Path) -> None:
        """Park a request nothing will retry, for a human to find."""
        try:
            self.move_to_stage(path, self.failed())
        except OSError:
            logger.exception("Could not dead-letter %s; unlinking", path)
            path.unlink(missing_ok=True)

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
        """Age of the oldest unclaimed request, or None when nothing is waiting.

        The liveness signal for "requests are arriving but nobody is draining
        them". Only ``queued/`` counts: a claimed request is already the fleet's
        problem and may legitimately sit waiting for a choke permit, and shutdown
        has its own pickup path and would otherwise report neglect the instant it
        is submitted."""
        queued = self.queued()
        if not queued.exists():
            return None
        now = time.time()
        oldest: float | None = None
        for path in queued.glob("*.yaml"):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if oldest is None or age > oldest:
                oldest = age
        return oldest


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
