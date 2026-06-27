"""Standalone tests for HeartbeatLease — the general file-mtime expiring lease.

These exercise the lease entirely on its own (no FolderBackedCase), proving it is a
reusable capability: a lease file path plus a ttl_provider callback is all it needs."""

import os
import time

import pytest

from totodev_pub.folder_backed_case_support.heartbeat_lease import (
    HeartbeatLease,
    LeaseAlreadyHeldError,
    LeaseOwnershipLostError,
    LeaseReleasedError,
)


def _lease(tmp_path, ttl=300.0):
    return HeartbeatLease(tmp_path / "res.lease", ttl_provider=lambda: ttl)


def test_acquire_creates_future_mtime_and_is_active(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    assert lease.is_active()
    assert lease.path.exists()
    # mtime is a "valid-until" token => comfortably in the future after a beat.
    assert lease.path.stat().st_mtime > time.time()


def test_second_acquire_on_held_path_raises(tmp_path):
    first = _lease(tmp_path)
    first.acquire()

    second = _lease(tmp_path)
    with pytest.raises(LeaseAlreadyHeldError) as ei:
        second.acquire()
    assert ei.value.expires_in > 0
    assert ei.value.path == first.path


def test_acquire_reclaims_expired_lease(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    # Force the on-disk token into the past => the lease is now reclaimable.
    past = time.time() - 60
    os.utime(lease.path, (past, past))

    other = _lease(tmp_path)
    other.acquire()                 # absent/expired => claim succeeds, no raise
    assert other.is_active()
    assert other.path.stat().st_mtime > time.time()


def test_heartbeat_is_throttled_then_forced(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    first_mtime = lease.path.stat().st_mtime

    # Within the throttle window: a no-op, the on-disk token is untouched.
    lease.heartbeat(min_update_secs=10_000)
    assert lease.path.stat().st_mtime == first_mtime

    # Forced beat (min_update_secs=0) rewrites the token; it should not go backwards.
    time.sleep(0.01)
    lease.heartbeat(min_update_secs=0)
    assert lease.path.stat().st_mtime >= first_mtime


def test_heartbeat_detects_lost_ownership(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    # Simulate another owner reclaiming the file past our TTL: the on-disk mtime no
    # longer matches the token we last wrote.
    stolen = time.time() + 9_999
    os.utime(lease.path, (stolen, stolen))

    with pytest.raises(LeaseOwnershipLostError) as ei:
        lease.heartbeat(min_update_secs=0)
    assert ei.value.path == lease.path


def test_heartbeat_can_skip_ownership_validation(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    stolen = time.time() + 9_999
    os.utime(lease.path, (stolen, stolen))

    # With validation off, the beat overwrites the token without complaint.
    lease.heartbeat(min_update_secs=0, validate_ownership=False)
    assert lease.is_active()


def test_release_removes_file_and_is_idempotent(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    assert lease.path.exists()

    lease.release()
    assert not lease.path.exists()
    assert not lease.is_active()

    lease.release()                 # idempotent: a second release is a no-op
    assert not lease.path.exists()


def test_release_on_never_acquired_lease_is_noop(tmp_path):
    lease = _lease(tmp_path)
    lease.release()                 # never acquired => deletes nothing, no raise
    assert not lease.path.exists()
    assert not lease.is_active()


def test_heartbeat_after_release_raises(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    lease.release()
    with pytest.raises(LeaseReleasedError):
        lease.heartbeat(min_update_secs=0)


def test_is_expired_reports_held_expired_and_absent(tmp_path):
    lease = _lease(tmp_path)

    # Absent file => None.
    assert HeartbeatLease.is_expired(lease.path) is None

    # Held (future token) => False.
    lease.acquire()
    assert HeartbeatLease.is_expired(lease.path) is False

    # Past token => True (reclaimable).
    past = time.time() - 60
    os.utime(lease.path, (past, past))
    assert HeartbeatLease.is_expired(lease.path) is True


def test_ttl_provider_is_consulted_per_beat(tmp_path):
    ttls = iter([100.0, 5_000.0])
    lease = HeartbeatLease(tmp_path / "res.lease", ttl_provider=lambda: next(ttls))

    lease.acquire()                 # consumes 100.0
    first = lease.path.stat().st_mtime
    lease.heartbeat(min_update_secs=0)   # consumes 5_000.0 => much later expiry
    assert lease.path.stat().st_mtime > first
