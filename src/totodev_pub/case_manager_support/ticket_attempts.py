# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Counting attempts at work items whose own retry counter cannot be trusted.

Every ticket carries a ``retry_count``, and that is the right place for it right
up until the ticket is the thing that is broken. A ticket that will not parse can
never record that it was tried, so it is retried forever: the case it describes
stays removed from the pool, detached, and un-enqueueable, while the manager
notices about it once per tick until someone restarts the process. The general
shape is worth naming, because it recurs — **the counter that would eventually
give up on a bad item lives inside the very file that failed to parse.**

The fix is to count somewhere the item cannot reach. In memory is enough, and
deliberately so:

- The failure is deterministic, so the threshold is reached within a handful of
  ticks of one process run — no persistence needed to converge.
- The *action* taken on exhaustion is durable: the file moves to ``failed/`` and
  the case is quarantined. A restart before the threshold starts the count over
  and converges again; a restart after finds the work already done.
- A durable counter would be another file that can itself corrupt, which is the
  problem this exists to escape.
"""

from __future__ import annotations

from pathlib import Path


class TicketAttemptLedger:
    """In-memory attempt counts, keyed by the item's path.

    Keyed by path rather than by ticket contents for the same reason the ledger
    exists at all: the path is readable when the contents are not.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, int] = {}

    def record_failure(self, item: Path) -> int:
        """Count one failed attempt at ``item`` and return the running total."""
        key = str(item)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        return self._attempts[key]

    def attempts(self, item: Path) -> int:
        return self._attempts.get(str(item), 0)

    def forget(self, item: Path) -> None:
        """Drop ``item``'s history — it succeeded, or it has been retired.

        Without this the ledger grows for the lifetime of the process, and a
        ticket path reused by a later case would inherit a stranger's failures.
        """
        self._attempts.pop(str(item), None)

    def __len__(self) -> int:
        return len(self._attempts)
