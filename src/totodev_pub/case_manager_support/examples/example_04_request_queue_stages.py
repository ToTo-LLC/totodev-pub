# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Watch requests move through the queue stages, driven entirely by the adapter.

    uv run python -m totodev_pub.case_manager_support.examples.example_04_request_queue_stages

Every action travels as one message through one queue, and the stage it sits in
*is* its state::

    requests/queued/          submitted; the manager has not claimed it yet
    requests/claimed/{id}/    claimed, waiting for a slot or a choke permit
    requests/running/{id}/    executing right now
    requests/results/         the answer, by correlation id

The file is never rewritten as it moves — only renamed, which is atomic, so a
crash can never leave a half-changed state. That property is pinned by
``tests/test_signaling_adapter.py``; this script exists to make it *visible*.

**Why the stages are normally invisible.** A request is claimed, run, answered
and deleted inside a single tick, faster than anything can observe. The fix is
not an artificial pause — it is giving the fleet real reasons to hold a request
in each stage, so what you watch is the queue doing its actual job:

- ``queued/``  — ``maintenance_interval_secs`` is raised, so a submitted request
  waits for the next drain rather than being picked up instantly.
- ``claimed/`` — a choke of one permit. This is the stage's *purpose*: accepted
  but waiting for capacity. Three fires against one permit means two of them sit
  in ``claimed/`` while the first runs.
- ``running/`` — the trigger's ``perform_`` hook awaits, so the step genuinely
  takes seconds.

``asyncio.sleep`` in the hook is load-bearing: the observer below shares the
event loop, so a blocking ``time.sleep`` would freeze the watcher too and you
would see nothing.

Nothing here moves a file by hand. Every transition is the adapter's own —
``_drain_queue`` claims, and the fire's ``on_launch`` promotes to ``running/``
when the pool sweep actually starts the step.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.mailbox import MailboxTransport
from totodev_pub.case_manager_support.signaling_adapter import SignalingAdapter
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry

logger = logging.getLogger("queue_stages")

#: How long the step takes. Long enough to see ``running/`` between polls, short
#: enough that the whole example finishes in well under a minute.
STEP_SECS = 3.0

#: One permit, so two of three fires must wait in ``claimed/``.
APPROVAL_CHOKE = "approval_desk"


class SlowApprovalCase(FolderBackedCase):
    """A manual edge with a deliberately slow step.

    ``==`` is a manual edge: nothing moves until a fire asks for it, so every
    transition you see was caused by a request rather than by the fleet ticking
    along on its own. ``asset_aliases`` is empty for the same reason as the other
    examples — the subject here is the queue, not data modelling.
    """

    asset_aliases = {}
    #: trigger -> the set of choke names it needs a permit from. The name must
    #: match a key in the manager's ``choke_limits``.
    fsm_trigger_chokes = {"approve": {APPROVAL_CHOKE}}
    fsm_state_chains = ["[*] --> waiting == approve ==> approved --> [*]"]

    async def perform_approve(self, tctx) -> None:
        await asyncio.sleep(STEP_SECS)


def stage_counts(transport: MailboxTransport) -> dict[str, int]:
    """How many request files sit in each stage right now.

    ``rglob`` for the per-case stages: ``claimed/`` and ``running/`` hold one
    subdirectory per case, so a flat glob would always report zero.
    """
    return {
        "queued": len(list(transport.queued().glob("*.yaml"))),
        "claimed": len(list(transport.claimed().rglob("*.yaml"))),
        "running": len(list(transport.running().rglob("*.yaml"))),
        "results": len(list(transport.results_dir().glob("*.yaml"))),
    }


async def watch(transport: MailboxTransport, *, until_results: int, timeout: float) -> None:
    """Print a stage table until every request has been answered.

    Polls rather than hooks, deliberately: the point is that the stages are
    legible from the *filesystem* alone, which is what an operator has.
    """
    print(f"\n{'elapsed':>8}  {'queued':>7} {'claimed':>8} {'running':>8} {'results':>8}")
    print(f"{'-' * 8}  {'-' * 7} {'-' * 8} {'-' * 8} {'-' * 8}")
    loop = asyncio.get_running_loop()
    started = loop.time()
    previous: dict[str, int] | None = None
    while loop.time() - started < timeout:
        counts = stage_counts(transport)
        if counts != previous:  # only print on change, so the table stays readable
            elapsed = loop.time() - started
            print(
                f"{elapsed:7.1f}s  {counts['queued']:>7} {counts['claimed']:>8} "
                f"{counts['running']:>8} {counts['results']:>8}"
            )
            previous = counts
        if counts["results"] >= until_results:
            return
        await asyncio.sleep(0.25)
    print(f"\n(timed out after {timeout}s — the fleet did not finish)")


async def main(cache_root: Path, *, case_count: int = 3) -> None:
    case_type_registry.register_case_types(SlowApprovalCase)
    store = CaseManager.open_local_store(
        cache_root,
        # Each knob makes one stage observable. See the module docstring.
        maintenance_interval_secs=2.0,
        concurrency_ceiling=1,
        choke_limits={APPROVAL_CHOKE: 1},
    )
    manager = CaseManager(store)
    adapter = SignalingAdapter(manager)
    adapter.attach()
    await manager.recover()

    # Admit the cases first, so the run below is only about firing them.
    case_ids = []
    for index in range(case_count):
        staged = manager.allocate_incoming_folder()
        built = SlowApprovalCase.create_case_in_folder(staged, external_key=f"APPROVAL-{index}")
        result = await manager.adopt_case(built.case_detach())
        assert result.status == "completed", result.rejection_reason
        case_ids.append(result.case_id)
    print(f"admitted {len(case_ids)} cases: {', '.join(case_ids)}")

    await manager.start()
    try:
        for case_id in case_ids:
            adapter.transport.submit_fire(case_id=case_id, trigger="approve")
        print(
            f"submitted {len(case_ids)} fires against a choke of 1 permit "
            f"and a {STEP_SECS:.0f}s step"
        )
        await watch(
            adapter.transport,
            until_results=len(case_ids),
            timeout=STEP_SECS * case_count + 30.0,
        )
    finally:
        await manager.stop()

    print("\nWhat you just saw:")
    print("  queued  — waiting for the next drain (maintenance_interval_secs=2.0)")
    print(f"  claimed — accepted, waiting for the single {APPROVAL_CHOKE} permit")
    print("  running — the step is executing, one at a time")
    print("  results — answered; the request file is deleted after the answer lands")
    print("\nEvery move above was the adapter's own. Nothing here renamed a file.")


if __name__ == "__main__":
    # WARNING, not INFO: the subject of this example is its own printed table,
    # and the library narrates every case attach/detach at INFO.
    #
    # basicConfig's level alone does not achieve that here. Per-case loggers are
    # built directly with their own DEBUG level and propagate upward, and a
    # propagated record skips every ancestor *logger* level check — only the
    # originating logger and the *handler* filter it. basicConfig sets the root
    # logger's level, leaving its handler at NOTSET, which accepts everything. So
    # the handler is what has to be raised.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
        root.parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(main(root))
    else:
        # No argument: a throwaway directory, so the example is safe to re-run.
        with tempfile.TemporaryDirectory(prefix="queue_stages_") as scratch:
            asyncio.run(main(Path(scratch) / "fleet"))
