# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CasePoolDriver: the seam for the case-DRIVING (scheduling) layer.

WHY THIS EXISTS
---------------
A FolderBackedCase models ONE case: its record, files, lease, and FSM. It knows how
to take a SINGLE forward step — ``await case.case_advance()`` — and report the outcome.

It deliberately does NOT know how to drive a fleet. Deciding WHICH case to advance,
in WHAT ORDER, at WHAT CADENCE, with what CONCURRENCY, FAIRNESS, and BACKPRESSURE is a
scheduling concern, and scheduling is a separate responsibility from "what a case is".
Baking a single-case ``run_to_completion`` loop into the case object quietly endorses a
one-case-at-a-time model; the intended deployment is a POOL of open cases advanced
fairly (e.g. round-robin) so no case is starved. That driving policy lives HERE, behind
this seam, never on the case.

The general case is a POOL: you add cases to be driven, whether that pool holds 1 or
500. A driver that only ever handles a single case is the degenerate edge case (a pool
of size one), so the abstraction is named for the pool, not the edge case. What actually
varies between drivers is the SCHEDULING POLICY (round-robin, priority, event-reactive,
sharded), which is the axis concrete subclasses are named for — e.g. a forthcoming
``RoundRobinCasePoolDriver``.

STATUS: PLACEHOLDER
-------------------
This is an intentionally-minimal contract, not a production scheduler. It marks the
home for the real driver and pins the one essential operation (a single fair pass over
the pool). A concrete, adequate-but-not-canonical pool driver is expected to land
shortly; richer concerns are explicitly out of scope for this placeholder:

  * discovery / admission of cases into the pool (e.g. scanning a cases root on disk),
  * concurrency limits and per-tick work budgeting,
  * fairness / round-robin ordering and anti-starvation,
  * cross-case retry / backoff and lease contention handling,
  * stop / resume of the pool and graceful drain,
  * observability (per-tick metrics, stuck-case surfacing).

Note: single-case "run to completion" is, at most, the degenerate pool-of-one case
(handy for tests/scripts) — not the model the framework optimizes for. The test suite's
equivalent lives in tests/case_test_utils.py::drive_to_completion, kept out of the
shipped library on purpose.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class CasePoolDriver(ABC):
    """Abstract base for a driver over a POOL of cases (size 1..N).

    Subclass and implement :meth:`advance_all_once` to define a single fair pass over
    the pool. A run loop then simply calls that repeatedly on whatever cadence the
    deployment wants; the base intentionally prescribes nothing about discovery,
    concurrency, or scheduling beyond the one-pass contract. Concrete subclasses are
    named for their scheduling policy (e.g. ``RoundRobinCasePoolDriver``).
    """

    @abstractmethod
    async def advance_all_once(self) -> int:
        """Advance every currently-drivable case in the pool by AT MOST one auto step
        (one fair pass).

        Returns the number of cases that progressed this pass, so a run loop can decide
        whether to keep spinning, idle/backoff, or stop. Implementations own admission,
        ordering (round-robin / anti-starvation), and concurrency.
        """
        raise NotImplementedError
