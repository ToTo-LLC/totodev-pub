# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""One manager per cache root, and the wait that tells a corpse from an owner.

A held lease is not evidence of a live owner: a SIGKILLed manager leaves one
behind with its expiry still in the future. The difference is only visible over
time — a live owner keeps pushing the expiry forward, a dead one cannot — so
acquisition watches before it decides.
"""

import asyncio
import os
import time

import pytest

from case_manager_test_utils import provision_manager
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.manager_lease import (
    CompetingManagerError,
    acquire_manager_lease,
    build_manager_lease,
    manager_lease_path,
)
from totodev_pub.folder_backed_case_support.heartbeat_lease import LeaseOwnershipLostError
from totodev_pub.folder_backed_case_support.pool_membership_journal import LeaseReclaimTimings

# Real timings, scaled down: the code paths are identical, they just resolve in
# under a second instead of a minute.
BRISK = LeaseReclaimTimings(freeze_observe_secs=0.15, poll_secs=0.02, max_total_secs=3.0)


def _hold(path, *, secs_from_now: float) -> None:
    """Plant a lease expiring `secs_from_now`, the way a real holder would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    expiry_ns = int((time.time() + secs_from_now) * 1_000_000_000)
    os.utime(path, ns=(expiry_ns, expiry_ns))


@pytest.mark.asyncio
async def test_an_unheld_filespace_is_claimed_without_waiting(tmp_path):
    lease = build_manager_lease(tmp_path)
    started = time.monotonic()
    await acquire_manager_lease(lease, timings=BRISK)
    assert lease.is_active()
    assert time.monotonic() - started < BRISK.freeze_observe_secs, "nothing to wait for"


@pytest.mark.asyncio
async def test_a_dead_owners_lease_is_waited_out_then_reclaimed(tmp_path):
    """The shadow case: expiry in the future, but frozen because nobody is beating."""
    path = manager_lease_path(tmp_path)
    _hold(path, secs_from_now=0.4)

    lease = build_manager_lease(tmp_path)
    await acquire_manager_lease(lease, timings=BRISK)

    assert lease.is_active(), "a frozen lease lapses and is then ours"


@pytest.mark.asyncio
async def test_a_live_owner_is_refused_without_waiting_out_the_ttl(tmp_path):
    """The expiry advancing is proof of life, and no wait will outlast it."""
    path = manager_lease_path(tmp_path)
    _hold(path, secs_from_now=30.0)

    async def owner_keeps_beating(secs: float) -> None:
        # Stand in for the live owner's pulse landing during our observation.
        _hold(path, secs_from_now=30.0 + secs)
        await asyncio.sleep(0)

    lease = build_manager_lease(tmp_path)
    with pytest.raises(CompetingManagerError) as excinfo:
        await acquire_manager_lease(lease, timings=BRISK, sleep=owner_keeps_beating)

    assert excinfo.value.kind == "live"
    assert not lease.is_active()
    assert "Run one manager per cache root" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_lease_that_never_lapses_times_out_rather_than_hanging(tmp_path):
    path = manager_lease_path(tmp_path)
    _hold(path, secs_from_now=600.0)   # far beyond max_total_secs

    lease = build_manager_lease(tmp_path)
    with pytest.raises(CompetingManagerError) as excinfo:
        await acquire_manager_lease(lease, timings=BRISK)
    assert excinfo.value.kind == "timeout"


@pytest.mark.asyncio
async def test_recover_claims_the_filespace_and_a_second_manager_is_refused(tmp_path):
    """The gap this closes: an idle root, where no contended case would reveal it."""
    first = provision_manager(tmp_path)
    await first.recover()
    assert first._filespace_lease.is_active()

    second = provision_manager(tmp_path)
    with pytest.raises(CompetingManagerError):
        await acquire_manager_lease(second._filespace_lease, timings=BRISK)


@pytest.mark.asyncio
async def test_re_recovering_does_not_trip_over_our_own_lease(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.recover()
    assert manager._filespace_lease.is_active()


@pytest.mark.asyncio
async def test_a_clean_stop_releases_the_filespace_for_the_next_owner(tmp_path):
    """A clean handover must not cost the successor a lease-expiry wait."""
    first = provision_manager(tmp_path)
    await first.recover()
    await first.start()
    await first.stop()

    assert not manager_lease_path(first._manager_dir).exists(), "released, not left to lapse"

    second = provision_manager(tmp_path)
    started = time.monotonic()
    await second.recover()
    assert second._filespace_lease.is_active()
    assert time.monotonic() - started < 5.0, "a released lease is taken immediately"
    await second.stop()


@pytest.mark.asyncio
async def test_a_read_only_client_does_not_contend_for_the_filespace(tmp_path):
    """Ownership is taken by recover(), not construction — which is what lets an
    out-of-process reader attach to a fleet that is running right now."""
    owner = provision_manager(tmp_path)
    await owner.recover()

    client = CaseManagerClient(tmp_path / "cache")

    assert not client._manager._filespace_lease.is_active(), "the client claims nothing"
    assert owner._filespace_lease.is_active(), "and the owner keeps what it holds"


@pytest.mark.asyncio
async def test_losing_the_lease_mid_run_stands_the_manager_down(tmp_path):
    """Being stomped is not a hiccup to log and carry on from."""
    manager = provision_manager(tmp_path)
    await manager.recover()
    await manager.start()

    failures = []
    manager.on_loop_failure(failures.append)

    # Another manager takes the lease: same file, a token that is not ours.
    _hold(manager_lease_path(manager._manager_dir), secs_from_now=30.0)
    # The beat self-throttles; clear the throttle so the next pulse validates.
    manager._filespace_lease._last_beat_local = 0.0

    for _ in range(40):
        if failures:
            break
        await asyncio.sleep(0.05)

    assert failures, "the pulse must notice the theft"
    assert isinstance(failures[0], LeaseOwnershipLostError)
    assert not manager.is_running, "and stop driving rather than compete"
