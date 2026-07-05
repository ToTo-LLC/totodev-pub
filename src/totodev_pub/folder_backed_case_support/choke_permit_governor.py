# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Counted concurrency permits for named choke resources.

``ChokePermitGovernor`` tracks how many simultaneous holders may draw on each
named resource (e.g. ``"ms-graph-api"``, ``"cpu"``). Pool drivers compose one
instance; this module knows nothing about cases, tiers, or queues.

Two acquire paths:

- ``try_acquire`` — non-blocking, only during an open sweep (beat-quantized budget).
- ``acquire_priority`` — may await; for manual ``fire()`` paths.

All paths are all-or-nothing: a multi-resource request succeeds only when every
name in the set can be granted together, or the caller gets ``None`` / keeps waiting.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class InvalidChokeLimitsError(Exception):
    """Raised when ``ChokePermitGovernor`` is constructed with invalid limits."""

    def __init__(self, resource: str, limit: Any):
        super().__init__(
            f"Choke limit for {resource!r} must be an int >= 1, got {limit!r}."
        )
        self.resource = resource
        self.limit = limit


class ChokeGrantError(Exception):
    """Raised when ``release()`` is called with an unknown or already-released grant."""

    def __init__(self, message: str = "Invalid or already-released ChokeGrant."):
        super().__init__(message)


@dataclass(frozen=True)
class ChokeGrant:
    """Opaque receipt from acquire paths. Pass only to ``release()``."""

    _token: uuid.UUID = field(repr=False)
    _resources: frozenset[str] = field(repr=False)


# Singleton returned for empty acquire requests; release is a no-op.
_EMPTY_GRANT = ChokeGrant(_token=uuid.UUID(int=0), _resources=frozenset())


class ChokePermitGovernor:
    """Semaphore-style concurrency caps over named resources.

    Loop-confined: all methods must run on the same asyncio event loop that owns
    the composing driver.
    """

    def __init__(self, limits: dict[str, int]) -> None:
        self._limits: dict[str, int] = {}
        for name, limit in limits.items():
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise InvalidChokeLimitsError(name, limit)
            self._limits[name] = limit
        self._in_use: dict[str, int] = {name: 0 for name in self._limits}
        self._sweep_available: dict[str, int] | None = None
        self._sweep_open: bool = False
        self._active_grants: set[uuid.UUID] = set()
        self._waiters: deque[tuple[asyncio.Future[ChokeGrant], frozenset[str]]] = deque()

    def begin_sweep(self) -> None:
        """Open a beat-quantized sweep: freeze the try-acquire budget and drain waiters."""
        if self._sweep_open:
            raise RuntimeError("ChokePermitGovernor.begin_sweep(): sweep already open.")
        self._sweep_open = True
        self._sweep_available = {
            name: self._limits[name] - self._in_use.get(name, 0)
            for name in self._limits
        }
        self._drain_priority_waiters()

    def end_sweep(self) -> None:
        """Close the current sweep. Idempotent when no sweep is open."""
        self._sweep_open = False
        self._sweep_available = None

    def try_acquire(self, needed: frozenset[str]) -> ChokeGrant | None:
        """Non-blocking acquire against the sweep budget. Requires an open sweep."""
        if not self._sweep_open:
            raise RuntimeError(
                "ChokePermitGovernor.try_acquire(): no sweep open; call begin_sweep() first."
            )
        if not needed:
            return _EMPTY_GRANT
        self._validate_needed(needed)
        if not self._can_debit_sweep(needed):
            return None
        return self._grant(needed, debit_sweep=True)

    async def acquire_priority(self, needed: frozenset[str]) -> ChokeGrant:
        """Blocking acquire for manual paths. Waiter holds no permits while queued."""
        if not needed:
            return _EMPTY_GRANT
        self._validate_needed(needed)
        immediate = self._try_priority_grant(needed)
        if immediate is not None:
            return immediate
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ChokeGrant] = loop.create_future()
        self._waiters.append((fut, needed))
        return await fut

    def release(self, grant: ChokeGrant) -> None:
        """Return capacity held by ``grant`` and wake priority waiters (FIFO)."""
        if grant is _EMPTY_GRANT:
            return
        token = grant._token
        if token not in self._active_grants:
            raise ChokeGrantError(
                "ChokeGrant is unknown or was already released."
            )
        self._active_grants.discard(token)
        for name in grant._resources:
            self._in_use[name] -= 1
        self._drain_priority_waiters()

    def resource_usage(self) -> dict[str, dict[str, int]]:
        """Live holds per resource: ``{name: {"limit": L, "in_use": U}}``."""
        return {
            name: {"limit": self._limits[name], "in_use": self._in_use[name]}
            for name in self._limits
        }

    def priority_waiter_count(self) -> int:
        """Number of pending ``acquire_priority`` waiters."""
        return len(self._waiters)

    # -- internals -----------------------------------------------------------

    def _validate_needed(self, needed: frozenset[str]) -> None:
        unknown = needed - self._limits.keys()
        if unknown:
            raise ValueError(
                f"Unknown choke resource name(s): {sorted(unknown)!r}. "
                f"Configured: {sorted(self._limits.keys())!r}."
            )

    def _can_debit_live(self, needed: frozenset[str]) -> bool:
        for name in needed:
            if self._in_use[name] >= self._limits[name]:
                return False
        return True

    def _can_debit_sweep(self, needed: frozenset[str]) -> bool:
        assert self._sweep_available is not None
        for name in needed:
            if self._sweep_available.get(name, 0) <= 0:
                return False
        return True

    def _grant(self, needed: frozenset[str], *, debit_sweep: bool) -> ChokeGrant:
        if debit_sweep:
            assert self._sweep_available is not None
            for name in needed:
                self._sweep_available[name] -= 1
        for name in needed:
            self._in_use[name] += 1
        token = uuid.uuid4()
        grant = ChokeGrant(_token=token, _resources=frozenset(needed))
        self._active_grants.add(token)
        return grant

    def _try_priority_grant(self, needed: frozenset[str]) -> ChokeGrant | None:
        """Immediate priority grant when live capacity allows (see § sweep budget rules)."""
        if not self._can_debit_live(needed):
            return None
        if not self._sweep_open:
            return self._grant(needed, debit_sweep=False)
        if self._can_debit_sweep(needed):
            return self._grant(needed, debit_sweep=True)
        # Sweep budget exhausted but live capacity available (typical after release).
        return self._grant(needed, debit_sweep=False)

    def _drain_priority_waiters(self) -> None:
        """Grant pending priority waiters in FIFO order when whole sets are free."""
        if not self._waiters:
            return
        remaining: deque[tuple[asyncio.Future[ChokeGrant], frozenset[str]]] = deque()
        while self._waiters:
            fut, needed = self._waiters.popleft()
            if fut.done():
                continue
            grant = self._try_priority_grant(needed)
            if grant is None:
                remaining.append((fut, needed))
                continue
            fut.set_result(grant)
        self._waiters = remaining
