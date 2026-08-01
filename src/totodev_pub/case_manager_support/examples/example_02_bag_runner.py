# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Run a folder of prepared cases to completion, then exit.

    uv run python -m totodev_pub.case_manager_support.examples.example_02_bag_runner ./my_bag

The batch shape: point at a bag of case folders, drive them all, stop when
nothing is left. ``stop_when_empty=True`` is what makes it a job rather than a
service — the process exits 0 on self-completion, which a supervisor reads as
"finished", not "crashed".

Two properties worth noticing:

- **The bag is copied, not consumed.** Adopt moves a case folder into managed
  storage and running it mutates the case, so a loader that adopted your folders
  directly would destroy the input on first use. Run this twice on the same bag
  and you get the same answer twice.
- **Every case type must be registered.** A case whose type this process was
  never told about is rejected at adopt, named in the report, and left behind
  rather than half-loaded.

Pass ``--seed`` to generate a throwaway bag first, if you have not got one.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from totodev_pub.case_manager_support.bag_loading import load_case_bag
from totodev_pub.case_manager_support.case_manager_host import serve
from totodev_pub.case_manager_support.examples.example_cases import InquiryCase

logger = logging.getLogger("bag_runner")


def seed_bag(bag: Path, count: int = 5) -> Path:
    """Write ``count`` fresh, detached cases into ``bag``."""
    bag.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        case = InquiryCase.create_case_in_folder(bag / f"inquiry-{i:02d}")
        case.case_detach()   # a case being handed over must not hold its lease
    return bag


async def main(bag: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="bag_run_") as work:
        manager, report = await load_case_bag(
            bag,
            Path(work) / "fleet",
            register_types=[InquiryCase],
        )
        logger.info(
            "Loaded %d case(s); %d rejected, %d skipped",
            len(report.adopted), len(report.rejected), len(report.skipped),
        )
        for name, why in report.rejected:
            logger.warning("  rejected %s: %s", name, why)

        if not report.adopted:
            logger.warning("Nothing to run.")
            return

        # The loader already recovered the manager (adoption needs the
        # namespace), and serve() accommodates that rather than insisting on
        # doing it itself. Self-completing: returns as soon as the pool empties.
        await serve(manager, stop_when_empty=True)
        logger.info("All cases reached a terminal state.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = [a for a in sys.argv[1:] if a != "--seed"]
    target = Path(args[0]) if args else Path("./example_bag")
    if "--seed" in sys.argv or not target.exists():
        seed_bag(target)
        logging.info("Seeded a bag at %s", target)
    asyncio.run(main(target))
