# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The smallest complete manager process.

    uv run python -m totodev_pub.case_manager_support.examples.example_01_minimal_host /tmp/inquiries

Everything a host does — recovery, start, signal wiring, exit codes, the
watchdog, shutdown — is inside ``serve()``. What is left is naming a storage root
and the case types this process knows how to drive.

Note what is *absent*: no adapter. This process serves no fire/adopt/reclassify
requests, which is a supported shape rather than a limitation — it is what an
embedded host looks like. Shutdown still works, because the host owns it, not the
mailbox. See example 03 for the request-serving version.

Stop it with Ctrl-C (exit 0), or from another terminal:

    totodev-manager-health /tmp/inquiries      # 0 = healthy
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_host import serve
from totodev_pub.case_manager_support.examples.example_cases import (
    EscalationCase,
    InquiryCase,
)


async def main(cache_root: Path) -> None:
    store = CaseManager.open_local_store(cache_root)
    manager = CaseManager(store, register_types=[InquiryCase, EscalationCase])
    # serve() wants a manager that has not been recovered or started: it owns
    # that sequencing, and says so rather than quietly re-running it.
    await serve(manager)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "./inquiry_fleet")
    root.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(root))
