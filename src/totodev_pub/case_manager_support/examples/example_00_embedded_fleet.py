# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The manager by hand: provision, adopt, run, inspect, stop — no host at all.

    uv run python -m totodev_pub.case_manager_support.examples.example_00_embedded_fleet

Read this one first. The other examples are *process* shapes and hand the
lifecycle to ``serve()``; this one drives the same lifecycle explicitly, so the
steps ``serve()`` performs on your behalf are visible before they are hidden.

**Where a manager should live.** This script embeds the manager in the calling
process, which is supported and is what a fleet inside a larger program looks
like. But the design target is a manager with a process (or container) of its
own: the pool advances cases on the event loop, so a busy fleet competes with a
UI for it; a crash on either side takes the other down; and restart-and-recover
is a much simpler story for a process whose only job is the fleet. When you
split them, the manager side becomes ``serve()`` (example 01) and the UI side
becomes ``CaseManagerClient`` over the file-drop protocol (example 03). Starting
embedded is fine — the split is a hosting change, not an API change.

Two things worth watching for, because both are easy to get wrong and neither
announces itself:

- **Detach before adopting.** Adopt rejects a case folder whose lease the
  builder still holds, rather than wrestling it away from a live owner.
- **Finished is not yet filed.** A case leaves the pool the moment it reaches a
  terminal state, and is archived into terminal storage on a later tick. Code
  that waits for the archive must wait for the archive.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.examples.example_cases import (
    EscalationCase,
    InquiryCase,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


async def main(work_dir: Path) -> None:
    # 1. Storage. The only thing that creates a working directory, and
    #    idempotent — so this is equally how you reopen an existing fleet.
    store = CaseManager.open_local_store(work_dir)

    # 2. The manager. Register case types *before* construction: cases are
    #    rehydrated off disk by type name, including during recover(), and an
    #    unregistered type is rejected rather than guessed at.
    #    InquiryCase advances itself ("--" edges are automatic); EscalationCase
    #    waits to be told ("==" edges need a fire()). See example_cases.py.
    case_type_registry.register_case_types(InquiryCase, EscalationCase)
    manager = CaseManager(store)

    # 3. Claim the directory and reconcile what is already in it, then enter run
    #    mode. start() without recover() raises: driving an empty pool over a
    #    directory that may still hold live cases is never what you meant.
    await manager.recover()
    await manager.start()

    # 4/5. Build each case in staging, then adopt it. Staging is on the same
    #      filesystem as managed storage, so adopt renames rather than copies.
    #      case_detach() matters: adopt refuses a folder that is still leased.
    #      It returns that folder, so the handoff reads as one phrase — build,
    #      let go, adopt. Adopt then consumes the folder and pools the case.
    ids: dict[str, str] = {}
    for case_cls, external_key in ((InquiryCase, "INQ-1"), (EscalationCase, "ESC-1")):
        staged = manager.allocate_staging_folder()
        result = await manager.adopt_case(
            case_cls.create_case_in_folder(staged, external_key=external_key).case_detach()
        )
        assert result.status == "completed", result.rejection_reason
        ids[case_cls.__name__] = result.case_id
    print("adopted:", ids)

    # 6. Let the pool work. The inquiry walks itself to `closed` over a few
    #    maintenance ticks; the escalation sits in `waiting` indefinitely.
    #    Waiting on the archive (not on terminal state) is the honest condition:
    #    a finished case leaves the pool a tick before it is filed.
    while not any(manager.iter_terminal()):
        await asyncio.sleep(0.25)

    # 7. Inspect. Readers are read-only snapshots that take no lease and never
    #    rehydrate, so they are safe to take while the pool is running. (For the
    #    live case *object* there is get_live(), but the pool may advance it at
    #    any await boundary — prefer readers, and fire() to make it move.)
    for reader in manager.iter_live():
        print("live    ", reader.case_id, reader.case_object_type, reader.case_state)
    for reader in manager.iter_terminal():
        print("terminal", reader.case_id, reader.case_object_type, reader.case_terminal_state)

    #    Manual actions go through the pool's queue rather than around it, so
    #    they share its concurrency ceiling and choke-resource budgets.
    await manager.fire(case_id=ids["EscalationCase"], trigger="approve")
    print("fired   ", ids["EscalationCase"], "->", manager.reader(case_id=ids["EscalationCase"]).case_state)

    # 8. Stop. Drains advances already in flight, settles the fleet, and releases
    #    the working directory so the next manager need not wait out a lease.
    await manager.stop()


if __name__ == "__main__":
    # WARNING, not INFO: this example's subject is its own printed output, and
    # the library narrates every lock it takes at INFO. Raise it when debugging.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
        root.parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(main(root))
    else:
        # No argument: run in a throwaway directory, so the example is safe to
        # run repeatedly without leaving a fleet behind.
        with tempfile.TemporaryDirectory(prefix="embedded_fleet_") as scratch:
            asyncio.run(main(Path(scratch) / "fleet"))
