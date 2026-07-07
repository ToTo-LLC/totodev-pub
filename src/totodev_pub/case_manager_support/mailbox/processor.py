# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Filesystem mailbox for cross-process fire and adopt requests (§10)."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, Field

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.case_manager_support.advance_result_serializable import AdvanceResultSerializable
from totodev_pub.case_manager_support.adopt import AdoptResult

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)

MAILBOX_PROTOCOL_VERSION = 1


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
    expected_case_id: str | None = None


@dataclass(frozen=True)
class RequestHandle:
    correlation_id: str
    result_path: Path


class MailboxProcessor:
    def __init__(self, manager: "CaseManager") -> None:
        self._manager = manager

    @property
    def _mgr_dir(self) -> Path:
        return self._manager._manager_dir

    @property
    def _policy(self):
        return self._manager._policy

    def results_dir(self) -> Path:
        return self._mgr_dir / "results"

    def fire_intake(self) -> Path:
        return self._mgr_dir / self._policy.fire_mailbox_subdir / "intake"

    def adopt_intake(self) -> Path:
        return self._mgr_dir / self._policy.adopt_mailbox_subdir / "intake"

    def _ensure_dirs(self) -> None:
        for p in (
            self.fire_intake(),
            self._mgr_dir / self._policy.fire_mailbox_subdir / "malformed",
            self._mgr_dir / self._policy.adopt_mailbox_subdir / "pending",
            self.adopt_intake(),
            self.results_dir(),
        ):
            p.mkdir(parents=True, exist_ok=True)

    def submit_fire(
        self,
        *,
        case_id: str | None = None,
        case_folder: Path | None = None,
        trigger: str | None = None,
        trigger_kwargs: dict | None = None,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        self._ensure_dirs()
        corr = correlation_id or str(uuid.uuid4())
        req = FireRequest(
            correlation_id=corr,
            requested_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            case_id=case_id,
            case_folder=str(case_folder) if case_folder else None,
            trigger=trigger,
            trigger_kwargs=trigger_kwargs or {},
        )
        tmp = self.fire_intake() / f".{corr}.yaml"
        final = self.fire_intake() / f"{corr}.yaml"
        req.save(str(tmp), retain_lock=False)
        os.replace(tmp, final)
        return RequestHandle(corr, self.results_dir() / f"{corr}.yaml")

    def submit_adopt(
        self,
        *,
        source_folder: Path,
        expected_case_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RequestHandle:
        self._ensure_dirs()
        corr = correlation_id or str(uuid.uuid4())
        req = AdoptRequest(
            correlation_id=corr,
            requested_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_folder=str(source_folder.resolve()),
            expected_case_id=expected_case_id,
        )
        tmp = self.adopt_intake() / f".{corr}.yaml"
        final = self.adopt_intake() / f"{corr}.yaml"
        req.save(str(tmp), retain_lock=False)
        os.replace(tmp, final)
        return RequestHandle(corr, self.results_dir() / f"{corr}.yaml")

    def poll_result(self, handle: RequestHandle) -> AdvanceResultSerializable | AdoptResult | None:
        if not handle.result_path.exists():
            return None
        try:
            if "adopt" in handle.result_path.read_text()[:200].lower() or handle.result_path.read_text().find("source_folder") >= 0:
                return AdoptResult.load(str(handle.result_path), acquire_lock=False)
        except Exception:
            pass
        try:
            return AdvanceResultSerializable.load(str(handle.result_path), acquire_lock=False)
        except Exception:
            try:
                return AdoptResult.load(str(handle.result_path), acquire_lock=False)
            except Exception:
                return None

    async def wait_result(
        self,
        handle: RequestHandle,
        *,
        timeout: float = 30.0,
        poll_secs: float = 0.05,
    ):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            result = self.poll_result(handle)
            if result is not None:
                return result
            await asyncio.sleep(poll_secs)
        return None

    async def maintenance_tick(self) -> None:
        if not self._policy.enable_mailbox:
            return
        self._ensure_dirs()
        await self._process_fire_intake()
        await self._process_adopt_intake()
        self._sweep_old_results()

    async def _process_fire_intake(self) -> None:
        intake = self.fire_intake()
        for path in sorted(intake.glob("*.yaml")):
            try:
                req = FireRequest.load(str(path), acquire_lock=False)
            except Exception:
                malformed = self._mgr_dir / self._policy.fire_mailbox_subdir / "malformed" / path.name
                path.rename(malformed)
                continue
            pending_dir = self._mgr_dir / self._policy.fire_mailbox_subdir / "pending" / (req.case_id or "unknown")
            pending_dir.mkdir(parents=True, exist_ok=True)
            pending = pending_dir / path.name
            os.replace(path, pending)
            firing_dir = self._mgr_dir / self._policy.fire_mailbox_subdir / "firing" / (req.case_id or "unknown")
            firing_dir.mkdir(parents=True, exist_ok=True)
            firing = firing_dir / path.name
            os.replace(pending, firing)
            try:
                if req.case_id:
                    ar = await self._manager.fire(case_id=req.case_id, trigger=req.trigger, **req.trigger_kwargs)
                elif req.case_folder:
                    ar = await self._manager.fire(
                        case_folder=Path(req.case_folder), trigger=req.trigger, **req.trigger_kwargs
                    )
                else:
                    raise ValueError("fire request missing case_id and case_folder")
                result = AdvanceResultSerializable.from_advance_result(ar)
                result.save(str(self.results_dir() / f"{req.correlation_id}.yaml"), retain_lock=False)
                if req.case_id or req.case_folder:
                    loc = self._manager.locate(
                        case_id=req.case_id,
                        case_folder=Path(req.case_folder) if req.case_folder else None,
                    )
                    if loc:
                        self._manager._driver.boost(loc.case_folder)
                firing.unlink(missing_ok=True)
            except Exception as exc:
                err = AdvanceResultSerializable(
                    status="error",
                    initial_state="",
                    final_state="",
                    exception_messages=(str(exc),),
                )
                err.save(str(self.results_dir() / f"{req.correlation_id}.yaml"), retain_lock=False)
                firing.unlink(missing_ok=True)

    async def _process_adopt_intake(self) -> None:
        intake = self.adopt_intake()
        pending_dir = self._mgr_dir / self._policy.adopt_mailbox_subdir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(intake.glob("*.yaml")):
            try:
                req = AdoptRequest.load(str(path), acquire_lock=False)
            except Exception:
                path.unlink(missing_ok=True)
                continue
            pending = pending_dir / path.name
            os.replace(path, pending)
            result = await self._manager.adopt_case(
                Path(req.source_folder),
                correlation_id=req.correlation_id,
                expected_case_id=req.expected_case_id,
            )
            result.save(str(self.results_dir() / f"{req.correlation_id}.yaml"), retain_lock=False)
            pending.unlink(missing_ok=True)

    def replay_fire_on_recover(self) -> int:
        count = 0
        firing_root = self._mgr_dir / self._policy.fire_mailbox_subdir / "firing"
        if firing_root.exists():
            for path in firing_root.rglob("*.yaml"):
                try:
                    req = FireRequest.load(str(path), acquire_lock=False)
                    err = AdvanceResultSerializable(
                        status="error",
                        initial_state="",
                        final_state="",
                        exception_messages=("dead-letter: recovered from firing/",),
                    )
                    err.save(str(self.results_dir() / f"{req.correlation_id}.yaml"), retain_lock=False)
                    path.unlink(missing_ok=True)
                    count += 1
                except Exception:
                    path.unlink(missing_ok=True)
        return count

    def replay_adopt_on_recover(self) -> int:
        pending = self._mgr_dir / self._policy.adopt_mailbox_subdir / "pending"
        if not pending.exists():
            return 0
        return len(list(pending.glob("*.yaml")))

    def _sweep_old_results(self) -> None:
        import time
        ttl = self._policy.result_ttl_secs
        now = time.time()
        for path in self.results_dir().glob("*.yaml"):
            try:
                if now - path.stat().st_mtime > ttl:
                    path.unlink()
            except OSError:
                pass
