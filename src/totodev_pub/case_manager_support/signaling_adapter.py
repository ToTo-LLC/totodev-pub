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

**One queue, dispatched on ``op``.** There were three drains, one per mailbox,
and their order within a tick was load-bearing: fire ran before adopt, so a fire
co-submitted with an adopt was processed before the adopt that would have pooled
its case. Now a single FIFO drain serves the queue in submission order, which is
both simpler and closer to what a submitter expects. Ops that need different
handling differ in their *executor*, not in their queue.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import yaml

from totodev_pub.case_manager_support.advance_result_serializable import (
    AdvanceResultSerializable,
)
from totodev_pub.case_manager_support.adopt import AdoptResult
from totodev_pub.case_manager_support.case_store import LIVE
from totodev_pub.case_manager_support.exceptions import LiveCaseNotFoundError
from totodev_pub.case_manager_support.layout import read_case_id_from_folder
from totodev_pub.case_manager_support.mailbox.transport import (
    ADOPT_OP,
    FIRE_OP,
    RECLASSIFY_OP,
    AdoptPayload,
    FirePayload,
    MailboxTransport,
    ReclassifyPayload,
    ReclassifyResult,
    RequestEnvelope,
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
    requeued: int = 0


class SignalingAdapter:
    """Drives a ``CaseManager`` from a ``MailboxTransport``."""

    def __init__(self, manager: "CaseManager", transport: MailboxTransport | None = None) -> None:
        self._manager = manager
        self._transport = transport or MailboxTransport(manager._manager_dir, manager._policy)

    @property
    def transport(self) -> MailboxTransport:
        return self._transport

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Register with the manager's maintenance tick.

        The queue is drained at the *head* of a tick, before the pool sweep, so a
        fire submitted between ticks is attached in time to be stepped by the
        very next sweep rather than the one after it.
        """
        self._manager.on_maintenance(self.maintenance_tick)

    def recover(self) -> AdapterRecoverReport:
        """Reconcile the transport after a crash. Call before ``manager.start()``.

        Recovery is a two-party sequence: the fleet recovers its own storage and
        pool, and the adapter separately settles requests that were in flight when
        the process died. Neither knows how to do the other's half.

        Call order matters: the fleet must have rebuilt its pool first, because
        settling an interrupted adopt means asking the manager whether the case
        is there.

        Two stages, two policies. A request in ``claimed/`` was accepted but never
        started, so it is safe to requeue verbatim. One in ``running/`` was
        executing, and what that means depends on the op — which is why the
        per-op settle logic below is not collapsible into a single rule.

        Every in-flight request leaves here with a published result or a requeue.
        Nothing is left for a later tick to notice, because nothing later looks.
        """
        self._transport.ensure_dirs()
        report = AdapterRecoverReport(requeued=self._requeue_claimed())
        for path in _yaml_files(self._transport.running()):
            self._settle_interrupted(path, report)
        return report

    @property
    def is_idle(self) -> bool:
        """True when no request is waiting to be served.

        Half of the system-wide idle question; the manager answers the other
        half about its pool. Neither is sufficient alone, which is why the host
        composes them."""
        queued = self._transport.queued()
        return not queued.exists() or not any(queued.glob("*.yaml"))

    async def maintenance_tick(self) -> None:
        """Drain the queue once. Registered on the manager's tick."""
        self._transport.ensure_dirs()
        await self._drain_queue()
        self._transport.sweep_old_results()

    # ------------------------------------------------------------------
    # Draining
    # ------------------------------------------------------------------

    async def _drain_queue(self) -> None:
        """Serve every queued request, FIFO by arrival, dispatching on ``op``.

        The queue directory *is* the queue — there is no in-memory one. Each
        request is claimed by an atomic rename into ``claimed/{case_key}/``, so
        even if two drainers ever ran only one could win it.
        """
        for path in sorted(self._transport.queued().glob("*.yaml"), key=arrival_order):
            corr = path.stem
            try:
                envelope = RequestEnvelope.load(str(path), acquire_lock=False)
                payload = envelope.typed_payload()
            except Exception as exc:
                # The correlation id is in the filename even when the body is
                # unreadable, so a waiting client gets an error rather than
                # timing out with no way to tell what happened. The op may be
                # unreadable too — then the submitter gets the generic shape,
                # which is the honest answer when we cannot tell what they asked.
                # Read the op *before* dead-lettering: that moves the file.
                op = _op_hint(path)
                self._transport.dead_letter(path)
                self._publish_failure(op, corr, f"malformed request: {exc}")
                continue
            # Isolation carries the real op, so an exploding adopt is answered
            # with an AdoptResult rather than an advance-shaped one — a submitter
            # polling for its own result type would otherwise see nothing it
            # recognises and wait out the full timeout.
            with self._isolated_request(envelope.op, path, corr):
                await self._execute(envelope, payload, path)

    async def _execute(self, envelope: RequestEnvelope, payload: Any, path: Path) -> None:
        """Claim the request, then run the executor its ``op`` selects."""
        claimed = self._transport.move_to_stage(path, self._transport.claimed(envelope.case_key))
        if envelope.op == FIRE_OP:
            await self._attach_fire(envelope, payload, claimed)
        elif envelope.op == RECLASSIFY_OP:
            await self._execute_reclassify(envelope, payload, claimed)
        elif envelope.op == ADOPT_OP:
            await self._execute_adopt(envelope, payload, claimed)
        else:  # pragma: no cover - typed_payload already rejected unknown ops
            raise ValueError(f"unknown request op {envelope.op!r}")

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
            logger.exception("Signaling adapter: %s %s failed", kind, corr)
            try:
                self._publish_failure(kind, corr, str(exc))
            except Exception:
                logger.exception("Could not publish a failure result for %s", corr)
            path.unlink(missing_ok=True)

    def _publish_failure(self, op: str, corr: str, message: str) -> None:
        if op == RECLASSIFY_OP:
            result: Any = ReclassifyResult(status="error", correlation_id=corr, error=message)
        elif op == ADOPT_OP:
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

    async def _attach_fire(
        self, envelope: RequestEnvelope, payload: FirePayload, claimed: Path
    ) -> None:
        """Hand the step to the pool without awaiting it.

        ``fire(..., wait=False)`` means the drain does not block on the step.
        ``on_launch`` moves ``claimed/ → running/`` when the sweep actually starts
        it and ``on_complete`` writes the result, which keeps a slow or
        choke-starved fire from stalling the whole tick.
        """
        corr = envelope.id
        running = self._transport.running(envelope.case_key) / claimed.name
        try:
            loc = self._locate_for(envelope.case_id, payload.case_folder)
            if loc is None or not loc.in_pool:
                raise LiveCaseNotFoundError(envelope.case_id or str(payload.case_folder))

            def on_launch() -> None:
                running.parent.mkdir(parents=True, exist_ok=True)
                if claimed.exists():
                    self._transport.move_to_stage(claimed, running.parent)

            def on_complete(result: AdvanceResult | None, error: BaseException | None) -> None:
                try:
                    self._transport.publish_result(corr, _fire_result(result, error))
                finally:
                    running.unlink(missing_ok=True)
                    claimed.unlink(missing_ok=True)

            await self._manager.fire(
                case_folder=loc.case_folder,
                trigger=payload.trigger,
                wait=False,
                on_launch=on_launch,
                on_complete=on_complete,
                **(payload.trigger_kwargs or {}),
            )
        except Exception as exc:
            self._publish_failure(FIRE_OP, corr, str(exc))
            claimed.unlink(missing_ok=True)
            running.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Reclassify
    # ------------------------------------------------------------------

    async def _execute_reclassify(
        self, envelope: RequestEnvelope, payload: ReclassifyPayload, claimed: Path
    ) -> None:
        running = self._transport.move_to_stage(claimed, self._transport.running(envelope.case_key))
        addr = self._address_of(envelope.case_id, payload.case_folder)
        from_type: str | None = None
        try:
            from_type = self._manager.reader(**addr).case_object_type
        except Exception:
            pass  # best-effort provenance; never worth failing the request over
        try:
            fresh = await self._manager.reclassify_case(**addr, target_type=payload.target_type)
            result = ReclassifyResult(
                status="completed",
                correlation_id=envelope.id,
                case_id=fresh.case_id,
                from_type=from_type,
                to_type=type(fresh).__name__,
                case_state=fresh.case_state,
            )
        except Exception as exc:
            result = ReclassifyResult(
                status="error",
                correlation_id=envelope.id,
                case_id=envelope.case_id,
                from_type=from_type,
                to_type=payload.target_type,
                error=str(exc),
            )
        self._transport.publish_result(envelope.id, result)
        running.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Adopt
    # ------------------------------------------------------------------

    async def _execute_adopt(
        self, envelope: RequestEnvelope, payload: AdoptPayload, claimed: Path
    ) -> None:
        if self._already_answered(envelope.id) is not None:
            # A re-delivered request: answer from the result we already
            # published rather than adopting the same folder twice.
            claimed.unlink(missing_ok=True)
            return

        stamped = self._stamp_source_case_id(envelope, claimed)
        running = self._transport.move_to_stage(stamped, self._transport.running(envelope.case_key))
        result = await self._manager.adopt_case(Path(payload.source_folder))
        result = result.model_copy(update={"correlation_id": envelope.id})
        self._transport.publish_result(envelope.id, result)
        running.unlink(missing_ok=True)

    def _stamp_source_case_id(self, envelope: RequestEnvelope, claimed: Path) -> Path:
        """Record the source's case id on the claimed request before the transfer.

        The window this closes: adopt moves the source into managed storage and
        removes it, so a process that dies mid-adopt leaves a request naming a
        folder that may no longer exist, with no way to tell a completed adopt
        from one that never started. With the id on file, recovery can ask the
        store which happened.

        Best effort by design. A source whose id is unreadable is one adopt is
        about to reject anyway, and failing to stamp only costs recovery its
        precision — it must never cost the adopt itself.

        Returns the request's path, which changes when a stamp lands: the file is
        keyed by case id, so learning the id moves it out of ``_unknown/``.
        """
        try:
            payload = AdoptPayload.model_validate(envelope.payload)
            case_id = read_case_id_from_folder(Path(payload.source_folder))
            if not case_id:
                return claimed
            envelope.case_id = case_id
            envelope.save(str(claimed), retain_lock=False)
            return self._transport.move_to_stage(claimed, self._transport.claimed(case_id))
        except Exception:
            logger.warning(
                "Could not stamp the case id onto claimed adopt %s; "
                "crash recovery for it will report an unknown outcome",
                claimed,
                exc_info=True,
            )
            return claimed

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

    def _requeue_claimed(self) -> int:
        """Send every claimed-but-unstarted request back to ``queued/``.

        Safe for every op without inspecting it: claiming is a rename that happens
        before any work, so a request caught here provably never touched a case.
        """
        count = 0
        queued = self._transport.queued()
        queued.mkdir(parents=True, exist_ok=True)
        for path in _yaml_files(self._transport.claimed()):
            try:
                self._transport.move_to_stage(path, queued)
                count += 1
            except OSError:
                logger.warning("Discarding unrequeueable request %s", path)
                path.unlink(missing_ok=True)
        return count

    def _settle_interrupted(self, path: Path, report: AdapterRecoverReport) -> None:
        """Settle one request a crash caught mid-execution, by op.

        What each op owes its submitter differs, and the differences are earned:

        - **fire** — the step's outcome is unknown, and re-running it could double
          a side effect. The submitter is told so.
        - **reclassify** — ``case_reclassify_to`` commits in two phases, so the
          *case* is consistent either way, old type or new, never half. Only the
          requester is left hanging.
        - **adopt** — the outcome cannot be assumed. Requeueing would re-run
          against a source the previous attempt may have consumed, and erroring
          blindly would tell a submitter "failed" about a case that is live right
          now. So the store is asked what actually landed.

        What none of them may do is leave the file in ``running/``. A submitter
        polling a result path cannot distinguish "still working" from "the process
        adopting your case died three restarts ago", and that is the one answer no
        timeout ever resolves.
        """
        try:
            envelope = RequestEnvelope.load(str(path), acquire_lock=False)
        except Exception:
            logger.warning("Discarding unreadable in-flight request %s", path)
            path.unlink(missing_ok=True)
            return
        try:
            if envelope.op == ADOPT_OP:
                self._settle_adopt(envelope, report)
            elif envelope.op == RECLASSIFY_OP:
                self._transport.publish_result(
                    envelope.id,
                    ReclassifyResult(
                        status="error",
                        correlation_id=envelope.id,
                        case_id=envelope.case_id,
                        error="dead-letter: recovered from running/",
                    ),
                )
                report.reclassify_dead_lettered += 1
            else:
                self._publish_failure(FIRE_OP, envelope.id, "dead-letter: recovered from running/")
                report.fire_replayed += 1
        except Exception:
            logger.exception("Could not settle in-flight request %s", path)
        path.unlink(missing_ok=True)

    def _settle_adopt(self, envelope: RequestEnvelope, report: AdapterRecoverReport) -> None:
        if self._already_answered(envelope.id) is not None:
            # The crash landed between publishing and cleanup; the published
            # result is authoritative and must not be overwritten.
            return
        self._transport.publish_result(envelope.id, self._recovered_adopt_result(envelope))
        report.adopt_settled += 1

    def _recovered_adopt_result(self, envelope: RequestEnvelope) -> AdoptResult:
        """Reconstruct the outcome of an interrupted adopt from managed storage.

        Uses ``locate()`` rather than the store directly — the adapter drives the
        manager's public API, and by the time adapter recovery runs the fleet has
        already rebuilt its pool, so a readmitted case is found either way.
        """
        source = str(envelope.payload.get("source_folder", ""))
        loc = self._manager.locate(envelope.case_id) if envelope.case_id else None
        if loc is not None and loc.status == LIVE:
            return AdoptResult(
                status="completed",
                case_id=envelope.case_id or "",
                case_folder=str(loc.case_folder),
                source_folder=source,
                correlation_id=envelope.id,
            )
        if loc is not None:
            # It landed, then adopt's own failure path moved it out of live
            # storage. The submitter needs the status, not a bare "error".
            return AdoptResult(
                status="error",
                case_id=envelope.case_id or "",
                case_folder=str(loc.case_folder),
                source_folder=source,
                correlation_id=envelope.id,
                rejection_reason=(
                    "dead-letter: recovered from running/; the case is in managed "
                    f"storage with status {loc.status!r}"
                ),
            )
        detail = (
            "the case never reached managed storage"
            if envelope.case_id
            else "outcome unknown (no case id was recorded before the transfer)"
        )
        return AdoptResult(
            status="error",
            case_id=envelope.case_id or "",
            source_folder=source,
            correlation_id=envelope.id,
            rejection_reason=f"dead-letter: recovered from running/; {detail}",
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


def _fire_result(
    result: AdvanceResult | None, error: BaseException | None
) -> AdvanceResultSerializable:
    if error is not None:
        return AdvanceResultSerializable(
            status="error",
            initial_state="",
            final_state="",
            exception_messages=(str(error),),
        )
    if result is None:
        return AdvanceResultSerializable(
            status="error",
            initial_state="",
            final_state="",
            exception_messages=("fire completed with no result",),
        )
    return AdvanceResultSerializable.from_advance_result(result)


def _op_hint(path: Path) -> str:
    """Best guess at a malformed request's op, for choosing the error's shape.

    A request whose *payload* failed validation still parsed as YAML, so its op is
    readable and the submitter gets the result type it is polling for. One that
    did not parse at all has no readable op, and the generic advance shape is the
    honest answer — inventing an op would answer a question nobody asked.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return FIRE_OP
    if isinstance(loaded, dict) and loaded.get("op") in _RESULT_SHAPES:
        return str(loaded["op"])
    return FIRE_OP


#: The ops ``_publish_failure`` has a distinct result shape for.
_RESULT_SHAPES = frozenset({FIRE_OP, ADOPT_OP, RECLASSIFY_OP})


def _yaml_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.yaml")) if root.exists() else []
