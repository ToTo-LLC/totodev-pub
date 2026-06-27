# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Single-owner expiring lease backed by file mtime.

`HeartbeatLease` stores a "valid-until" timestamp in a content-free lease file's
mtime. `acquire()` claims when absent/expired, `heartbeat()` refreshes while held,
and `release()` drops the claim. The exact mtime last written is also the instance's
ownership token, so a later `heartbeat()` can detect ownership loss.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable


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


class HeartbeatLease:
    """Single-owner, file-mtime lease with throttled heartbeat refresh."""

    def __init__(self, lease_path: Path, *, ttl_provider: Callable[[], float]) -> None:
        self._path = Path(lease_path)
        self._ttl_provider = ttl_provider
        self._held = False
        self._released = False
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
        if not self.is_active():
            raise LeaseReleasedError(self._path)
        if time.monotonic() - self._last_beat_local < min_update_secs:
            return
        if validate_ownership and self._on_disk_mtime() != self._my_mtime:
            raise LeaseOwnershipLostError(self._path)
        self._beat()

    def release(self) -> None:
        """Drop this lease claim. Idempotent for never-acquired or released instances."""
        if self._released or not self._held:
            return
        self._path.unlink(missing_ok=True)
        self._released = True
        self._held = False

    def is_active(self) -> bool:
        """True while this instance still holds the claim (acquired and not released)."""
        return self._held and not self._released

    @staticmethod
    def is_expired(lease_path: Path) -> bool | None:
        """True if expired, False if held, None if file absent."""
        try:
            return Path(lease_path).stat().st_mtime <= time.time()
        except FileNotFoundError:
            return None
