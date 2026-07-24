"""Standalone tests for HeartbeatLease — the general file-mtime expiring lease.

These exercise the lease entirely on its own (no FolderBackedCase), proving it is a
reusable capability: a lease file path plus a ttl_provider callback is all it needs."""

import os
import time

import pytest

from totodev_pub.folder_backed_case_support.heartbeat_lease import (
    HeartbeatLease,
    LeaseAlreadyHeldError,
    LeaseHandedOffError,
    LeaseHandoff,
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


def test_secs_left_reports_remaining_held_expired_and_absent(tmp_path):
    lease = _lease(tmp_path, ttl=300.0)

    # Absent file => None.
    assert HeartbeatLease.secs_left(lease.path) is None

    # Held (future token) => positive seconds, bounded by the TTL just written.
    lease.acquire()
    remaining = HeartbeatLease.secs_left(lease.path)
    assert remaining is not None
    assert 0 < remaining <= 300.0 + 1.0

    # Past token => negative seconds (lapsed; file still present).
    past = time.time() - 60
    os.utime(lease.path, (past, past))
    lapsed = HeartbeatLease.secs_left(lease.path)
    assert lapsed is not None
    assert lapsed < 0
    assert lapsed == pytest.approx(-60.0, abs=1.0)


def test_secs_left_never_zero_when_lease_file_exists_at_clock_edge(tmp_path, monkeypatch):
    lease = _lease(tmp_path)
    lease.acquire()
    frozen = 1_700_000_000.0
    os.utime(lease.path, (frozen, frozen))
    monkeypatch.setattr(time, "time", lambda: frozen)
    remaining = HeartbeatLease.secs_left(lease.path)
    assert remaining is not None
    assert remaining > 0
    assert bool(remaining)


def test_secs_left_tracks_the_written_expiry(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    # Pin the on-disk expiry to a known future instant and confirm the reported
    # remaining seconds line up with it (independent of the ttl_provider).
    future = time.time() + 120
    os.utime(lease.path, (future, future))
    remaining = HeartbeatLease.secs_left(lease.path)
    assert remaining is not None
    assert remaining == pytest.approx(future - time.time(), abs=1.0)


def test_ttl_provider_is_consulted_per_beat(tmp_path):
    ttls = iter([100.0, 5_000.0])
    lease = HeartbeatLease(tmp_path / "res.lease", ttl_provider=lambda: next(ttls))

    lease.acquire()                 # consumes 100.0
    first = lease.path.stat().st_mtime
    lease.heartbeat(min_update_secs=0)   # consumes 5_000.0 => much later expiry
    assert lease.path.stat().st_mtime > first


# ---------------------------------------------------------------------------
# Hot handoff: suspend this in-memory holder and pass the lease as dead data to
# another thread/process, then resume ownership later — without ever releasing.
# ---------------------------------------------------------------------------


def test_handoff_keeps_lease_on_disk_and_parks(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    before = lease.path.stat().st_mtime_ns
    time.sleep(0.01)

    token = lease.handoff()

    # The on-disk lease persists (never released) and the window was refreshed.
    assert lease.path.exists()
    assert isinstance(token, LeaseHandoff)
    assert token.path == str(lease.path)
    assert token.token == lease.path.stat().st_mtime_ns
    assert token.token > before
    # The parked holder is no longer the active beater.
    assert not lease.is_active()
    assert lease.is_parked()


def test_handoff_then_heartbeat_raises_by_default(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    lease.handoff()

    with pytest.raises(LeaseHandedOffError) as ei:
        lease.heartbeat(min_update_secs=0)
    assert ei.value.path == lease.path


def test_handoff_ignore_policy_makes_heartbeat_a_noop(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    lease.handoff(on_stray_heartbeat="ignore")
    frozen = lease.path.stat().st_mtime_ns

    lease.heartbeat(min_update_secs=0)   # no raise, and the token is untouched
    assert lease.path.stat().st_mtime_ns == frozen


def test_handoff_rejects_unknown_stray_policy(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    with pytest.raises(ValueError):
        lease.handoff(on_stray_heartbeat="explode")


def test_double_handoff_raises(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    lease.handoff()
    with pytest.raises(LeaseHandedOffError):
        lease.handoff()


def test_release_while_parked_raises_and_keeps_file(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    lease.handoff()

    with pytest.raises(LeaseHandedOffError):
        lease.release()
    assert lease.path.exists()


def test_handoff_detects_lost_ownership(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire()
    stolen = time.time() + 9_999
    os.utime(lease.path, (stolen, stolen))

    with pytest.raises(LeaseOwnershipLostError):
        lease.handoff()


def test_from_handoff_reconstructs_an_active_lease(tmp_path):
    owner = _lease(tmp_path)
    owner.acquire()
    token = owner.handoff()

    delegate = HeartbeatLease.from_handoff(token, ttl_provider=lambda: 300.0)
    assert delegate.is_active()
    assert delegate.path == owner.path


def test_delegate_beats_immediately_despite_default_throttle(tmp_path):
    # The throttle clock is process-local monotonic; a freshly reconstructed
    # delegate must be able to beat right away rather than wait out min_update_secs.
    owner = _lease(tmp_path)
    owner.acquire()
    token = owner.handoff()

    delegate = HeartbeatLease.from_handoff(token, ttl_provider=lambda: 5_000.0)
    time.sleep(0.01)
    delegate.heartbeat()   # default min_update_secs=15, must still beat
    assert delegate.path.stat().st_mtime_ns > token.token


def test_heartbeat_retries_when_filesystem_rounds_token(tmp_path, monkeypatch):
    lease = _lease(tmp_path)
    lease.acquire()
    existing = lease._my_token
    assert existing is not None

    sequence = [existing, existing, existing, existing + 1]

    def fake_on_disk_token():
        if sequence:
            return sequence.pop(0)
        return existing + 1

    utime_calls = 0
    real_utime = os.utime

    def counting_utime(*args, **kwargs):
        nonlocal utime_calls
        utime_calls += 1
        return real_utime(*args, **kwargs)

    monkeypatch.setattr(lease, "_on_disk_token", fake_on_disk_token)
    monkeypatch.setattr(os, "utime", counting_utime)

    lease.heartbeat(min_update_secs=0)
    assert utime_calls >= 2
    assert lease._my_token == existing + 1


def test_round_trip_handoff_and_resume(tmp_path):
    owner = _lease(tmp_path)
    owner.acquire()

    out = owner.handoff()
    delegate = HeartbeatLease.from_handoff(out, ttl_provider=lambda: 5_000.0)
    delegate.heartbeat(min_update_secs=0)            # delegate moves the token
    back = delegate.handoff()                        # symmetric: hand it back

    owner.resume(back)
    assert owner.is_active()
    assert not owner.is_parked()
    # Owner can beat again without a false ownership-loss.
    owner.heartbeat(min_update_secs=0)
    assert owner.is_active()


def test_resume_detects_theft_during_handoff(tmp_path):
    owner = _lease(tmp_path)
    owner.acquire()
    token = owner.handoff()

    # Someone else reclaims the file while it was handed off.
    stolen = time.time() + 9_999
    os.utime(owner.path, (stolen, stolen))

    with pytest.raises(LeaseOwnershipLostError):
        owner.resume(token)


def test_resume_detects_expiry_during_handoff(tmp_path):
    owner = _lease(tmp_path)
    owner.acquire()
    owner.handoff()

    # The handoff window lapsed: on-disk token matches what we hold but is in the
    # past, so the lease is reclaimable and must not be silently resumed.
    past = time.time() - 60
    os.utime(owner.path, (past, past))
    expired = LeaseHandoff(path=str(owner.path), token=owner.path.stat().st_mtime_ns)

    with pytest.raises(LeaseOwnershipLostError):
        owner.resume(expired)


def test_from_handoff_rejects_a_stolen_token(tmp_path):
    owner = _lease(tmp_path)
    owner.acquire()
    token = owner.handoff()

    stolen = time.time() + 9_999
    os.utime(owner.path, (stolen, stolen))

    with pytest.raises(LeaseOwnershipLostError):
        HeartbeatLease.from_handoff(token, ttl_provider=lambda: 300.0)


def test_resume_rejects_token_for_a_different_path(tmp_path):
    owner = _lease(tmp_path)
    owner.acquire()
    owner.handoff()
    foreign = LeaseHandoff(
        path=str(tmp_path / "somewhere-else.lease"),
        token=int((time.time() + 100) * 1_000_000_000),
    )

    with pytest.raises(ValueError):
        owner.resume(foreign)
