# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The filespace's single-owner lease: one manager per cache root, enforced.

The same ``HeartbeatLease`` a case holds on its own folder, applied one scope up.
A case lease says "I am driving this case"; this one says "I am driving this
filespace", and it is the only thing that makes that claim checkable before any
work has been done.

Per-case leases already stop a second manager *stealing* a case somebody is
actively driving, but that protection needs contended cases to fire — two
managers over an idle root would both start, both beat the manifest, and both
drain the same mailboxes, discovering the collision only once work arrived. The
filespace lease closes that window at the front.

**Acquiring waits, because a held lease does not mean a live owner.** A manager
that was SIGKILLed leaves its lease file behind with an expiry still in the
future, and that is indistinguishable from a live owner's until you watch it: a
live owner keeps pushing the expiry forward, while a dead one's is frozen. So
acquisition observes for longer than one beat period, and then:

- the expiry **advanced** — somebody is alive, and no amount of waiting will help.
  Fail immediately rather than idling for a lease that will never lapse.
- the expiry is **frozen** — a shadow of a dead process. Wait for it to lapse,
  bounded, then claim it.

That is the same two-phase gate ``restore_pool_from_journal`` applies to case
folders, and it reuses that module's ``LeaseReclaimTimings`` so the two cannot
drift apart.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from totodev_pub.folder_backed_case_support.constants import DEFAULT_LEASE_TTL_SECS
from totodev_pub.folder_backed_case_support.heartbeat_lease import HeartbeatLease
from totodev_pub.folder_backed_case_support.pool_membership_journal import LeaseReclaimTimings

logger = logging.getLogger(__name__)

#: Lives in the manager namespace, not the cache root: it is the manager's own
#: protocol state, and the store owns everything outside that namespace.
MANAGER_LEASE_NAME = ".manager.lease"


class CompetingManagerError(Exception):
    """Another manager holds this filespace's lease.

    Not a contained failure. Two managers over one cache root corrupt shared state
    in ways no per-case guard can undo, so this always raises rather than
    honouring ``strict_recovery`` — refusing to start *is* the containment.

    ``kind`` says which way the evidence pointed: ``"live"`` means the lease was
    observed being renewed, so an owner is genuinely running; ``"timeout"`` means
    it never lapsed within the deadline, which is the same conclusion reached the
    slow way.
    """

    def __init__(self, lease_path: Path, *, remaining_secs: float, kind: str) -> None:
        self.lease_path = Path(lease_path)
        self.remaining_secs = remaining_secs
        self.kind = kind
        detail = (
            "its lease was renewed while we watched, so the owner is alive"
            if kind == "live"
            else "its lease never lapsed within the deadline"
        )
        super().__init__(
            f"Another CaseManager already owns {self.lease_path.parent.parent}: {detail} "
            f"(~{remaining_secs:.0f}s remaining on {self.lease_path}). Run one manager per "
            "cache root; if the previous owner is genuinely dead, wait for its lease to "
            "lapse or remove the lease file by hand once you have confirmed that."
        )


def manager_lease_path(manager_dir: Path) -> Path:
    return Path(manager_dir) / MANAGER_LEASE_NAME


def build_manager_lease(manager_dir: Path) -> HeartbeatLease:
    """A lease over the filespace, on the same fixed TTL every case lease uses."""
    return HeartbeatLease(
        manager_lease_path(manager_dir),
        ttl_provider=lambda: DEFAULT_LEASE_TTL_SECS,
    )


def _valid_until(lease_path: Path) -> float | None:
    """The lease's valid-until wall clock (its mtime), or None when absent.

    Pure stat, deliberately: the absolute value is what makes "did it advance?"
    answerable, which ``secs_left`` cannot express because it moves with the clock.
    """
    try:
        return Path(lease_path).stat().st_mtime
    except FileNotFoundError:
        return None


async def acquire_manager_lease(
    lease: HeartbeatLease,
    *,
    timings: LeaseReclaimTimings | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Claim the filespace, waiting out a dead owner's lease but not a live one.

    Raises ``CompetingManagerError`` when the lease belongs to someone still
    running. ``clock`` and ``sleep`` are injectable so tests need not spend real
    seconds proving it.
    """
    timings = timings or LeaseReclaimTimings()
    path = lease.path
    started_at = clock()

    baseline = _valid_until(path)
    if baseline is None or baseline <= clock():
        lease.acquire()          # absent or already lapsed: nothing to wait for
        return

    # Held. Watch longer than one beat period: a live owner renews, a corpse cannot.
    logger.info(
        "Filespace lease at %s is held (~%.0fs remaining); observing %.0fs to see whether "
        "an owner is alive or this is a dead process's shadow.",
        path, baseline - clock(), timings.freeze_observe_secs,
    )
    await sleep(timings.freeze_observe_secs)

    current = _valid_until(path)
    if current is not None and current > baseline:
        raise CompetingManagerError(
            path, remaining_secs=max(0.0, current - clock()), kind="live"
        )

    deadline = started_at + timings.max_total_secs
    while clock() < deadline:
        vu = _valid_until(path)
        if vu is None or vu <= clock():
            # Lapsed or removed. acquire() re-checks under the same rule, so a racer
            # that claimed it in this gap still turns into a clean refusal below.
            try:
                lease.acquire()
            except Exception:
                logger.warning("Filespace lease at %s was claimed by a racer; retrying.", path)
                await sleep(timings.poll_secs)
                continue
            logger.warning(
                "Reclaimed the filespace lease at %s after %.0fs — its previous owner left it "
                "behind without releasing it, which means that process died rather than "
                "stopping cleanly.",
                path, clock() - started_at,
            )
            return
        if vu > baseline:
            raise CompetingManagerError(
                path, remaining_secs=vu - clock(), kind="live"
            )
        await sleep(timings.poll_secs)

    remaining = (_valid_until(path) or clock()) - clock()
    raise CompetingManagerError(path, remaining_secs=max(0.0, remaining), kind="timeout")
