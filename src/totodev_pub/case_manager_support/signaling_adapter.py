# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The signaling adapter: a file-drop transport driving a CaseManager.

Three layers run a managed fleet, and this is the middle one:

1. **Host** (``case_manager_host.serve``) — process ownership: signals, exit
   codes, the watchdog, and shutdown.
2. **Signaling adapter** (here) — translates a file-drop transport into
   ``CaseManager`` method calls: fire, adopt, reclassify, correlation ids,
   result publication, dead-lettering, replay.
3. **`CaseManager`** — the fleet. No transport, no process lifecycle.

The direction of the dependency is the point. The adapter holds a manager and
calls its **public** API; the manager knows only that something registered a
maintenance callback. That forces the public API to actually be complete instead
of letting the transport reach into manager internals — and it makes every
request path testable against a manager that no adapter is attached to.

Correlation ids live here, not on the manager. Idempotency for a re-delivered
request is transport business: the fleet has no opinion about whether two
requests to adopt the same folder came from one client retrying.

**Every request is isolated.** One malformed or exploding request produces an
error result for *that* correlation id and the drain continues. A poisoned batch
would mean the requests behind it are never served and their submitters wait
forever with no way to tell "still queued" from "dropped".
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from totodev_pub.case_manager_support.advance_result_serializable import (
    AdvanceResultSerializable,
)
from totodev_pub.case_manager_support.adopt import AdoptResult
from totodev_pub.case_manager_support.case_store import LIVE
from totodev_pub.case_manager_support.exceptions import LiveCaseNotFoundError
from totodev_pub.case_manager_support.layout import read_case_id_from_folder
from totodev_pub.case_manager_support.mailbox.transport import (
    AdoptRequest,
    FireRequest,
    MailboxTransport,
    ReclassifyRequest,
    ReclassifyResult,
    arrival_order,
)
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager

logger = logging.getLogger(__name__)


@dataclass
class AdapterRecoverReport:
    """What startup found in the transport, separate from the fleet's own recovery."""

    fire_replayed: int = 0
    adopt_settled: int = 0
    reclassify_dead_lettered: int = 0


class SignalingAdapter:
    """Drives a ``CaseManager`` from a ``MailboxTransport``."""

    def __init__(self, manager: "CaseManager", transport: MailboxTransport | None = None) -> None:
        self._manager = manager
        self._transport = transport or MailboxTransport(
            manager._manager_dir, manager._policy
        )

    @property
    def transport(self) -> MailboxTransport:
        return self._transport

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Register with the manager's maintenance tick.

        Intake is drained at the *head* of a tick, before the pool sweep, so a
        fire submitted between ticks is attached in time to be stepped by the
        very next sweep rather than the one after it.
        """
        self._manager.on_maintenance(self.maintenance_tick)

    def recover(self) -> AdapterRecoverReport:
        """Reconcile the transport after a crash. Call before ``manager.start()``.

        Recovery is a two-party sequence now: the fleet recovers its own storage
        and pool, and the adapter separately settles requests that were in flight
        when the process died. Neither knows how to do the other's half.

        Call order matters: the fleet must have rebuilt its pool first, because
        settling an interrupted adopt means asking the manager whether the case
        is there.

        Every in-flight request leaves here with a published result or a requeue.
        Nothing is left for a later tick to notice, because nothing later looks.
        """
        self._transport.ensure_dirs()
        return AdapterRecoverReport(
            fire_replayed=self._replay_fire(),
            adopt_settled=self._settle_adopt_pending(),
            reclassify_dead_lettered=self._dead_letter_reclassify(),
        )

    @property
    def is_idle(self) -> bool:
        """True when no request is waiting to be served.

        Half of the system-wide idle question; the manager answers the other
        half about its pool. Neither is sufficient alone, which is why the host
        composes them."""
        return all(
            not intake.exists() or not any(intake.glob("*.yaml"))
            for intake in self._transport.request_intakes()
        )

    async def maintenance_tick(self) -> None:
        """Drain every request intake once. Registered on the manager's tick.

        Fire first, because attaching a fire before the sweep is what lets it be
        stepped in this same tick rather than the next one."""
        self._transport.ensure_dirs()
        await self._drain_fire()
        await self._drain_reclassify()
        await self._drain_adopt()
        self._transport.sweep_old_results()

    # ------------------------------------------------------------------
    # Per-request isolation
    # ------------------------------------------------------------------

    @contextmanager
    def _isolated_request(self, kind: str, path: Path, corr: str) -> Iterator[None]:
        """Contain one request's failure and tell its submitter what happened.

        Silence is the worst outcome here: a submitter polling for a result
        cannot distinguish "still queued" from "your request exploded and was
        dropped", so a failure that writes nothing leaves them waiting out their
        whole timeout for no reason.
        """
        try:
            yield
        except Exception as exc:
            logger.exception("Signaling adapter: %s request %s failed", kind, corr)
            try:
                self._publish_failure(kind, corr, str(exc))
            except Exception:
                logger.exception("Could not publish a failure result for %s", corr)
            path.unlink(missing_ok=True)

    def _publish_failure(self, kind: str, corr: str, message: str) -> None:
        if kind == "reclassify":
            result: Any = ReclassifyResult(
                status="error", correlation_id=corr, error=message
            )
        elif kind == "adopt":
            result = AdoptResult(
                status="error",
                case_id="",
                source_folder="",
                correlation_id=corr,
                rejection_reason=message,
            )
        else:
            result = AdvanceResultSerializable(
                status="error",
                initial_state="",
                final_state="",
                exception_messages=(message,),
            )
        self._transport.publish_result(corr, result)

    # ------------------------------------------------------------------
    # Fire
    # ------------------------------------------------------------------

    async def _drain_fire(self) -> None:
        """Attach each fire request to its case's scheduling slot.

        The intake directory *is* the queue — there is no in-memory one. Each
        request moves ``intake/ → pending/`` and is handed to
        ``fire(..., wait=False)`` so the drain does **not** await the step.
        ``on_launch`` moves ``pending/ → firing/`` when the sweep actually
        launches it and ``on_complete`` writes the result, which keeps a slow
        or choke-starved fire from stalling the whole tick.
        """
        for path in sorted(self._transport.fire_intake().glob("*.yaml"), key=arrival_order):
            corr = path.stem
            with self._isolated_request("fire", path, corr):
                try:
                    req = FireRequest.load(str(path), acquire_lock=False)
                except Exception as exc:
                    self._dead_letter(path, self._transport.fire_stage("malformed"))
                    self._publish_failure("fire", corr, f"malformed fire request: {exc}")
                    continue
                await self._attach_fire(req, path)

    async def _attach_fire(self, req: FireRequest, path: Path) -> None:
        case_key = req.case_id or "unknown"
        pending = self._transport.fire_stage("pending", case_key) / path.name
        pending.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, pending)
        firing = self._transport.fire_stage("firing", case_key) / path.name
        corr = req.correlation_id

        try:
            loc = self._locate_for(req.case_id, req.case_folder)
            if loc is None or not loc.in_pool:
                raise LiveCaseNotFoundError(req.case_id or str(req.case_folder))

            def on_launch() -> None:
                firing.parent.mkdir(parents=True, exist_ok=True)
                if pending.exists():
                    os.replace(pending, firing)

            def on_complete(
                result: AdvanceResult | None, error: BaseException | None
            ) -> None:
                try:
                    self._transport.publish_result(corr, _fire_result(result, error))
                finally:
                    firing.unlink(missing_ok=True)
                    pending.unlink(missing_ok=True)

            await self._manager.fire(
                case_folder=loc.case_folder,
                trigger=req.trigger,
                wait=False,
                on_launch=on_launch,
                on_complete=on_complete,
                **(req.trigger_kwargs or {}),
            )
        except Exception as exc:
            self._publish_failure("fire", corr, str(exc))
            pending.unlink(missing_ok=True)
            firing.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Reclassify
    # ------------------------------------------------------------------

    async def _drain_reclassify(self) -> None:
        for path in sorted(
            self._transport.reclassify_intake().glob("*.yaml"), key=arrival_order
        ):
            corr = path.stem
            with self._isolated_request("reclassify", path, corr):
                try:
                    req = ReclassifyRequest.load(str(path), acquire_lock=False)
                except Exception as exc:
                    self._dead_letter(path, self._transport.reclassify_stage("malformed"))
                    # The correlation id is in the filename even when the body is
                    # unreadable, so a waiting client gets an error rather than
                    # timing out with no way to tell what happened.
                    self._publish_failure(
                        "reclassify", corr, f"malformed reclassify request: {exc}"
                    )
                    continue
                await self._execute_reclassify(req, path)

    async def _execute_reclassify(self, req: ReclassifyRequest, path: Path) -> None:
        executing = self._transport.reclassify_stage(
            "executing", req.case_id or "unknown"
        ) / path.name
        executing.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, executing)

        addr = self._address_of(req.case_id, req.case_folder)
        from_type: str | None = None
        try:
            from_type = self._manager.reader(**addr).case_object_type
        except Exception:
            pass    # best-effort provenance; never worth failing the request over
        try:
            fresh = await self._manager.reclassify_case(**addr, target_type=req.target_type)
            result = ReclassifyResult(
                status="completed",
                correlation_id=req.correlation_id,
                case_id=fresh.case_id,
                from_type=from_type,
                to_type=type(fresh).__name__,
                case_state=fresh.case_state,
            )
        except Exception as exc:
            result = ReclassifyResult(
                status="error",
                correlation_id=req.correlation_id,
                case_id=req.case_id,
                from_type=from_type,
                to_type=req.target_type,
                error=str(exc),
            )
        self._transport.publish_result(req.correlation_id, result)
        executing.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Adopt
    # ------------------------------------------------------------------

    async def _drain_adopt(self) -> None:
        for path in sorted(self._transport.adopt_intake().glob("*.yaml"), key=arrival_order):
            corr = path.stem
            with self._isolated_request("adopt", path, corr):
                try:
                    req = AdoptRequest.load(str(path), acquire_lock=False)
                except Exception as exc:
                    self._publish_failure("adopt", corr, f"malformed adopt request: {exc}")
                    path.unlink(missing_ok=True)
                    continue
                await self._execute_adopt(req, path)

    async def _execute_adopt(self, req: AdoptRequest, path: Path) -> None:
        pending = self._transport.adopt_stage("pending") / path.name
        pending.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, pending)

        existing = self._already_answered(req.correlation_id)
        if existing is not None:
            # A re-delivered request: answer from the result we already
            # published rather than adopting the same folder twice.
            pending.unlink(missing_ok=True)
            return

        self._stamp_source_case_id(req, pending)
        result = await self._manager.adopt_case(Path(req.source_folder))
        result = result.model_copy(update={"correlation_id": req.correlation_id})
        self._transport.publish_result(req.correlation_id, result)
        pending.unlink(missing_ok=True)

    def _stamp_source_case_id(self, req: AdoptRequest, pending: Path) -> None:
        """Record the source's case id on the pending request before the transfer.

        The window this closes: adopt moves the source into managed storage and
        removes it, so a process that dies mid-adopt leaves a request naming a
        folder that may no longer exist, with no way to tell a completed adopt
        from one that never started. With the id on file, recovery can ask the
        store which happened.

        Best effort by design. A source whose id is unreadable is one adopt is
        about to reject anyway, and failing to stamp only costs recovery its
        precision — it must never cost the adopt itself.
        """
        try:
            case_id = read_case_id_from_folder(Path(req.source_folder))
            if not case_id:
                return
            req.case_id = case_id
            req.save(str(pending), retain_lock=False)
        except Exception:
            logger.warning(
                "Could not stamp the case id onto pending adopt %s; "
                "crash recovery for it will report an unknown outcome",
                pending, exc_info=True,
            )

    def _already_answered(self, correlation_id: str) -> AdoptResult | None:
        path = self._transport.result_path(correlation_id)
        if not path.exists():
            return None
        try:
            return AdoptResult.load(str(path), acquire_lock=False)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def _replay_fire(self) -> int:
        """Dead-letter fires caught mid-flight; requeue those never launched.

        A request in ``firing/`` was executing when the process died, so its
        outcome is unknown and the submitter is told so. One in ``pending/`` was
        attached but never launched, which is safe to simply run again.
        """
        count = 0
        for path in _yaml_files(self._transport.fire_stage("firing")):
            try:
                req = FireRequest.load(str(path), acquire_lock=False)
                self._publish_failure(
                    "fire", req.correlation_id, "dead-letter: recovered from firing/"
                )
                count += 1
            except Exception:
                logger.warning("Discarding unreadable in-flight fire request %s", path)
            path.unlink(missing_ok=True)

        intake = self._transport.fire_intake()
        intake.mkdir(parents=True, exist_ok=True)
        for path in _yaml_files(self._transport.fire_stage("pending")):
            try:
                os.replace(path, intake / path.name)
                count += 1
            except OSError:
                logger.warning("Discarding unrequeueable fire request %s", path)
                path.unlink(missing_ok=True)
        return count

    def _dead_letter_reclassify(self) -> int:
        """Settle reclassify requests a crash caught mid-execution.

        ``case_reclassify_to`` commits in two phases, so the *case* is
        consistent either way — old type or new, never half. Only the requester
        is left hanging, so they get an error telling them to re-check and
        resubmit if they still want it.
        """
        count = 0
        for path in _yaml_files(self._transport.reclassify_stage("executing")):
            try:
                req = ReclassifyRequest.load(str(path), acquire_lock=False)
                self._transport.publish_result(
                    req.correlation_id,
                    ReclassifyResult(
                        status="error",
                        correlation_id=req.correlation_id,
                        case_id=req.case_id,
                        to_type=req.target_type,
                        error="dead-letter: recovered from executing/",
                    ),
                )
                count += 1
            except Exception:
                logger.warning("Discarding unreadable in-flight reclassify request %s", path)
            path.unlink(missing_ok=True)
        return count

    def _settle_adopt_pending(self) -> int:
        """Settle adopt requests a crash caught mid-execution.

        Adopt is the one request type whose outcome recovery cannot assume.
        Requeueing blindly would re-run against a source the previous attempt
        may have already consumed, and erroring blindly would tell a submitter
        "failed" about a case that is live in managed storage right now. So the
        stamped ``case_id`` decides it: the store is asked what actually landed,
        and the submitter is told that.

        What it must never do is leave the request in ``pending/``. A submitter
        polling a result path cannot distinguish "still working" from "the
        process that was adopting your case died three restarts ago", and that
        is the one answer no timeout ever resolves.
        """
        count = 0
        for path in _yaml_files(self._transport.adopt_stage("pending")):
            try:
                req = AdoptRequest.load(str(path), acquire_lock=False)
            except Exception:
                logger.warning("Discarding unreadable in-flight adopt request %s", path)
                path.unlink(missing_ok=True)
                continue
            if self._already_answered(req.correlation_id) is not None:
                # The crash landed between publishing and cleanup; the published
                # result is authoritative and must not be overwritten.
                path.unlink(missing_ok=True)
                continue
            try:
                self._transport.publish_result(
                    req.correlation_id, self._recovered_adopt_result(req)
                )
                count += 1
            except Exception:
                logger.exception("Could not settle in-flight adopt request %s", path)
            path.unlink(missing_ok=True)
        return count

    def _recovered_adopt_result(self, req: AdoptRequest) -> AdoptResult:
        """Reconstruct the outcome of an interrupted adopt from managed storage.

        Uses ``locate()`` rather than the store directly — the adapter drives the
        manager's public API, and by the time adapter recovery runs the fleet has
        already rebuilt its pool, so a readmitted case is found either way.
        """
        loc = self._manager.locate(req.case_id) if req.case_id else None
        if loc is not None and loc.status == LIVE:
            return AdoptResult(
                status="completed",
                case_id=req.case_id or "",
                case_folder=str(loc.case_folder),
                source_folder=req.source_folder,
                correlation_id=req.correlation_id,
            )
        if loc is not None:
            # It landed, then adopt's own failure path moved it out of live
            # storage. The submitter needs the status, not a bare "error".
            return AdoptResult(
                status="error",
                case_id=req.case_id or "",
                case_folder=str(loc.case_folder),
                source_folder=req.source_folder,
                correlation_id=req.correlation_id,
                rejection_reason=(
                    "dead-letter: recovered from pending/; the case is in managed "
                    f"storage with status {loc.status!r}"
                ),
            )
        detail = (
            "the case never reached managed storage"
            if req.case_id
            else "outcome unknown (no case id was recorded before the transfer)"
        )
        return AdoptResult(
            status="error",
            case_id=req.case_id or "",
            source_folder=req.source_folder,
            correlation_id=req.correlation_id,
            rejection_reason=f"dead-letter: recovered from pending/; {detail}",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _address_of(case_id: str | None, case_folder: str | None) -> dict[str, Any]:
        if case_id:
            return {"case_id": case_id}
        return {"case_folder": Path(case_folder) if case_folder else None}

    def _locate_for(self, case_id: str | None, case_folder: str | None):
        if case_id:
            return self._manager.locate(case_id)
        if case_folder:
            return self._manager._resolve_single(case_folder=Path(case_folder))
        raise ValueError("fire request names neither case_id nor case_folder")

    @staticmethod
    def _dead_letter(path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(destination / path.name)
        except OSError:
            logger.exception("Could not dead-letter %s; unlinking", path)
            path.unlink(missing_ok=True)


def _fire_result(
    result: AdvanceResult | None, error: BaseException | None
) -> AdvanceResultSerializable:
    if error is not None:
        return AdvanceResultSerializable(
            status="error", initial_state="", final_state="",
            exception_messages=(str(error),),
        )
    if result is None:
        return AdvanceResultSerializable(
            status="error", initial_state="", final_state="",
            exception_messages=("fire completed with no result",),
        )
    return AdvanceResultSerializable.from_advance_result(result)


def _yaml_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.yaml")) if root.exists() else []
