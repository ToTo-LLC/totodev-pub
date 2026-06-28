# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Single-owner expiring lease backed by file mtime.

`HeartbeatLease` stores a "valid-until" timestamp in a content-free lease file's
mtime. `acquire()` claims when absent/expired, `heartbeat()` refreshes while held,
and `release()` drops the claim. The exact mtime last written is also the instance's
ownership token, so a later `heartbeat()` can detect ownership loss.

Because the lease's whole identity is just `(path, token)`, it can be handed off as
dead data: `handoff()` suspends the in-memory holder and emits a `LeaseHandoff`, which
another thread/process turns back into an active lease via `from_handoff()` (or the
original holder re-adopts with `resume()`). The on-disk lease is never released during
a handoff — it stays valid the whole time.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Literal, NamedTuple


class LeaseHandoff(NamedTuple):
    """Portable, serializable proof of lease ownership for a hot handoff.

    Captures everything another thread/process needs to take over a still-valid
    lease: the lease file ``path`` and the ``token`` (the exact mtime that is the
    current ownership proof). Plain scalars on purpose so it can travel as JSON or
    over a queue. The TTL policy (a callable) is NOT carried here; the receiver
    supplies its own when reconstructing.
    """
    path: str
    token: float


class LeaseAlreadyHeldError(Exception):
    """Raised when `acquire()` sees a non-expired lease at `path`."""
    def __init__(self, path: Path, *, expires_in: float):
        super().__init__(
            f"{path} is already held (lease valid for ~{expires_in:.0f}s more). "
            "Wait for the current owner to release() it or for the lease to expire."
        )
        self.path = path
        self.expires_in = expires_in


class LeaseOwnershipLostError(Exception):
    """Raised when `heartbeat()` detects the on-disk token no longer matches ours."""
    def __init__(self, path: Path):
        super().__init__(
            f"Ownership of {path} has been lost: the lease file was overwritten by "
            "another owner. This holder must not continue operating on the resource."
        )
        self.path = path


class LeaseReleasedError(Exception):
    """Raised when `heartbeat()` is called after `release()`."""
    def __init__(self, path: Path):
        super().__init__(
            f"This HeartbeatLease for {path} has been released and holds nothing. "
            "Call acquire() again to re-claim it before heartbeating."
        )
        self.path = path


class LeaseHandedOffError(Exception):
    """Raised when a parked (handed-off) lease is used without resuming it first."""
    def __init__(self, path: Path):
        super().__init__(
            f"This HeartbeatLease for {path} has been handed off and is suspended. "
            "Another holder is responsible for it until you resume() with the token it "
            "hands back; this instance must not heartbeat, release, or hand off again "
            "in the meantime."
        )
        self.path = path


class HeartbeatLease:
    """Single-owner, file-mtime lease with throttled heartbeat refresh."""

    def __init__(self, lease_path: Path, *, ttl_provider: Callable[[], float]) -> None:
        self._path = Path(lease_path)
        self._ttl_provider = ttl_provider
        self._held = False
        self._released = False
        self._parked = False
        self._stray_policy: Literal["raise", "ignore"] = "raise"
        self._my_mtime: float | None = None
        self._last_beat_local: float = 0.0

    @property
    def path(self) -> Path:
        """The lease file this instance manages."""
        return self._path

    def _on_disk_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _beat(self) -> None:
        expiry = time.time() + self._ttl_provider()
        self._path.touch(exist_ok=True)
        os.utime(self._path, (expiry, expiry))
        self._my_mtime = self._on_disk_mtime()
        self._last_beat_local = time.monotonic()
        self._held = True
        self._released = False

    def acquire(self) -> None:
        """Claim the lease or raise `LeaseAlreadyHeldError` when still valid."""
        m = self._on_disk_mtime()
        if m is not None and m > time.time():
            raise LeaseAlreadyHeldError(self._path, expires_in=m - time.time())
        self._beat()

    def heartbeat(
        self,
        *,
        min_update_secs: float = 15.0,
        validate_ownership: bool = True,
    ) -> None:
        """Refresh expiry, optionally validating our ownership token before rewriting."""
        if self._parked:
            if self._stray_policy == "ignore":
                return
            raise LeaseHandedOffError(self._path)
        if not self.is_active():
            raise LeaseReleasedError(self._path)
        if time.monotonic() - self._last_beat_local < min_update_secs:
            return
        if validate_ownership and self._on_disk_mtime() != self._my_mtime:
            raise LeaseOwnershipLostError(self._path)
        self._beat()

    def release(self) -> None:
        """Drop this lease claim. Idempotent for never-acquired or released instances."""
        if self._parked:
            raise LeaseHandedOffError(self._path)
        if self._released or not self._held:
            return
        self._path.unlink(missing_ok=True)
        self._released = True
        self._held = False

    def handoff(
        self, *, on_stray_heartbeat: Literal["raise", "ignore"] = "raise"
    ) -> LeaseHandoff:
        """Suspend this in-memory holder and emit a portable token for the next one.

        Beats once to refresh the window (so the receiver gets a full TTL to take
        over), then parks this instance: the on-disk lease is NOT released, it stays
        valid. While parked, `heartbeat()` (and `release()`) either raise
        `LeaseHandedOffError` (default) or `heartbeat()` becomes a no-op, per
        `on_stray_heartbeat` — a stray local beat during a handoff is a logic error.

        Symmetric: the same call is used to delegate the lease away and to hand it
        back. Resume ownership later with `resume()` (or `from_handoff()` in a fresh
        process) using the returned token.
        """
        if on_stray_heartbeat not in ("raise", "ignore"):
            raise ValueError(
                "on_stray_heartbeat must be 'raise' or 'ignore', "
                f"got {on_stray_heartbeat!r}"
            )
        if self._parked:
            raise LeaseHandedOffError(self._path)
        if not self.is_active():
            raise LeaseReleasedError(self._path)
        if self._on_disk_mtime() != self._my_mtime:
            raise LeaseOwnershipLostError(self._path)
        self._beat()
        self._parked = True
        self._stray_policy = on_stray_heartbeat
        assert self._my_mtime is not None
        return LeaseHandoff(path=str(self._path), token=self._my_mtime)

    def resume(self, handoff: LeaseHandoff) -> None:
        """Re-adopt a handed-off lease from its token, becoming the active beater.

        Validates that nothing was lost during the handoff window: the on-disk token
        must still match the one being handed back and must not have expired. If it
        was stolen or lapsed, raises `LeaseOwnershipLostError`. The throttle clock is
        reset so the resumer may beat immediately (monotonic clocks do not survive a
        cross-process handoff).
        """
        if Path(handoff.path) != self._path:
            raise ValueError(
                f"Handoff token is for {handoff.path!r}, not this lease's "
                f"{str(self._path)!r}."
            )
        on_disk = self._on_disk_mtime()
        if on_disk is None or on_disk != handoff.token or on_disk <= time.time():
            raise LeaseOwnershipLostError(self._path)
        self._my_mtime = handoff.token
        self._last_beat_local = 0.0
        self._held = True
        self._released = False
        self._parked = False
        self._stray_policy = "raise"

    @classmethod
    def from_handoff(
        cls, handoff: LeaseHandoff, *, ttl_provider: Callable[[], float]
    ) -> "HeartbeatLease":
        """Reconstruct an active lease from a handoff token in another thread/process.

        `ttl_provider` is a local policy and is supplied fresh here (it cannot ride
        along in the token). Validates the token like `resume()`: raises
        `LeaseOwnershipLostError` if the lease was stolen or expired in transit.
        """
        inst = cls(Path(handoff.path), ttl_provider=ttl_provider)
        inst.resume(handoff)
        return inst

    def is_active(self) -> bool:
        """True while this instance is the live beater (acquired, not released, not parked)."""
        return self._held and not self._released and not self._parked

    def is_parked(self) -> bool:
        """True while this instance has handed the lease off and is suspended."""
        return self._parked

    @staticmethod
    def is_expired(lease_path: Path) -> bool | None:
        """True if expired, False if held, None if file absent."""
        try:
            return Path(lease_path).stat().st_mtime <= time.time()
        except FileNotFoundError:
            return None
