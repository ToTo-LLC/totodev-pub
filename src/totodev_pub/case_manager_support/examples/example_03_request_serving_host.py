# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""A host that serves out-of-process requests, and a client that sends them.

    # terminal 1
    uv run python -m totodev_pub.case_manager_support.examples.example_03_request_serving_host serve /tmp/fleet
    # terminal 2
    uv run python -m totodev_pub.case_manager_support.examples.example_03_request_serving_host submit /tmp/fleet

Example 01 hosted a manager with no way in from outside. Attaching a
``SignalingAdapter`` adds one: a file-drop transport for fire, adopt, and
reclassify requests, with results published back by correlation id.

The layering is the point. The adapter holds the manager and calls its public
methods; the manager does not know a transport exists. So the same manager runs
with or without one — and the client half needs neither, because
``CaseManagerClient`` reads the manifest and writes files.

``EscalationCase`` waits on a manual edge, so nothing at all happens until a
request arrives. That is what makes the round trip visible.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.case_manager_host import serve
from totodev_pub.case_manager_support.examples.example_cases import (
    EscalationCase,
    InquiryCase,
)
from totodev_pub.case_manager_support.signaling_adapter import SignalingAdapter

logger = logging.getLogger("request_host")


async def run_host(cache_root: Path) -> None:
    store = CaseManager.open_local_store(cache_root)
    manager = CaseManager(store, register_types=[InquiryCase, EscalationCase])
    # The one line that turns a fleet into a service.
    await serve(manager, adapter=SignalingAdapter(manager))


async def run_client(cache_root: Path) -> str | None:
    """Hand a case to the running process, then ask it to take a step.

    Returns the adopted case_id, so a caller can go look at what happened."""
    client = CaseManagerClient(cache_root)

    staging = cache_root.parent / "outbound" / "escalation-1"
    staging.parent.mkdir(parents=True, exist_ok=True)
    if not staging.exists():
        case = EscalationCase.create_case_in_folder(staging)
        case.case_detach()   # a case being handed over must not hold its lease

    adopt_handle = client.submit_adopt(source_folder=staging)
    adopted = await client.wait_adopt(adopt_handle, timeout=30.0)
    if adopted is None or adopted.status != "completed":
        logger.error("Adopt did not complete: %s", adopted)
        return None
    logger.info("Adopted %s", adopted.case_id)

    # EscalationCase only moves when told, so this is the step that matters.
    fire_handle = client.submit_fire(case_id=adopted.case_id, trigger="approve")
    result = await client.wait_result(fire_handle, timeout=30.0)
    logger.info("Fire result: %s", result)
    return adopted.case_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    root = Path(sys.argv[2] if len(sys.argv) > 2 else "./request_fleet")
    root.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_host(root) if mode == "serve" else run_client(root))
