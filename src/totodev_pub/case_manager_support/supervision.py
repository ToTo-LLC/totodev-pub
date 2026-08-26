# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The supervisor contract as importable values rather than prose.

A hosted ``CaseManager`` never restarts itself. The watchdog detects, diagnoses
and dies — remediation belongs to whatever runs the process. That decision rests
on two timing floors and a handful of exit codes, and both floors are *derived*
from constants elsewhere in this library rather than chosen:

- **Restart delay** must exceed the case lease TTL. A hard exit leaves its cases'
  heartbeat leases held, and a replacement cannot reclaim them until they lapse;
  restarting sooner spends every cycle waiting on leases and never reaches steady
  state.
- **Kill timeout** must exceed ``serve()``'s stop grace, or SIGTERM is followed by
  SIGKILL mid-drain and the clean exit — along with the lease release that makes
  the *next* start fast — is forfeited.

Those two relationships previously lived only in ``docs/case-manager-deployment.md``,
where nothing could check them and nothing failed when the constants moved. This
library cannot unit-test somebody's unit file, but it can hand out the numbers that
unit file has to respect, so a consumer's own config test asserts against
``supervisor_requirements()`` instead of copying ``30`` and ``45`` into YAML and
trusting they stay true.

Use this when writing or testing supervisor configuration (systemd, Compose,
Kubernetes, or a shell loop). It is not needed to *run* a manager — ``serve()``
requires nothing from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from totodev_pub.case_manager_support.case_manager_host import (
    DEFAULT_STOP_GRACE_SECS,
    EXIT_RESTART_REQUESTED,
    EXIT_WATCHDOG,
)
from totodev_pub.folder_backed_case_support.constants import DEFAULT_LEASE_TTL_SECS

#: A deliberate, final stop — an OS-signaled stop or a ``stop_when`` predicate that
#: returned True. The one code a supervisor must **not** restart.
EXIT_DELIBERATE_STOP = 0

#: Python's own code for an exception that escaped ``serve()``, not one the host
#: chooses. Named here because it is reachable in normal operation — almost always
#: ``CompetingManagerError``, where the incumbent may be a corpse whose lease has
#: yet to lapse — so a supervisor must treat it as retryable rather than fatal.
EXIT_STARTUP_REFUSED = 1


@dataclass(frozen=True)
class SupervisorRequirements:
    """What a supervisor configuration must satisfy to host a ``CaseManager``.

    Both floors are **strict**: a value equal to the floor does not satisfy it.
    Prefer ``violations()`` over comparing the fields by hand — each comparison is
    easy to invert, and an inverted one yields a config that looks right and fails
    only under crash-restart load, which is exactly when it is needed.

    Attributes:
        restart_delay_floor_secs: A restart delay must be strictly greater. Derived
            from the case lease TTL, so it is not a deployment choice.
        stop_timeout_floor_secs: A kill/termination timeout must be strictly
            greater. Follows the host's own ``serve(stop_grace_secs=...)``.
        restart_on: Exit codes a supervisor must restart.
        no_restart_on: Exit codes a supervisor must leave stopped.
        prompt_restart_on: The subset of ``restart_on`` needing no delay at all,
            because that path shuts down cleanly and releases the filespace lease.
            Everything else in ``restart_on`` leaves a lease to lapse.
    """

    restart_delay_floor_secs: float
    stop_timeout_floor_secs: float
    restart_on: frozenset[int]
    no_restart_on: frozenset[int]
    prompt_restart_on: frozenset[int]

    def violations(self, *, restart_delay_secs: float, stop_timeout_secs: float) -> tuple[str, ...]:
        """Every way a supervisor config breaks this contract; empty means it holds.

        Args:
            restart_delay_secs: The configured delay between restarts (systemd
                ``RestartSec``, a shell loop's ``sleep``).
            stop_timeout_secs: The configured grace before SIGKILL (systemd
                ``TimeoutStopSec``, Docker ``stop_grace_period``, Kubernetes
                ``terminationGracePeriodSeconds``).

        Returns:
            Human-readable violation messages, each naming the offending value and
            the floor it failed to clear. Assert this is empty in a config test.
        """
        problems: list[str] = []
        if restart_delay_secs <= self.restart_delay_floor_secs:
            problems.append(
                f"restart delay {restart_delay_secs}s must exceed the case lease TTL "
                f"({self.restart_delay_floor_secs}s), or each restart is spent waiting "
                f"for the previous owner's leases to lapse"
            )
        if stop_timeout_secs <= self.stop_timeout_floor_secs:
            problems.append(
                f"stop timeout {stop_timeout_secs}s must exceed the host's stop grace "
                f"({self.stop_timeout_floor_secs}s), or the process is SIGKILLed "
                f"mid-drain and forfeits both the clean exit and the lease release"
            )
        return tuple(problems)


def supervisor_requirements(
    *, stop_grace_secs: float = DEFAULT_STOP_GRACE_SECS
) -> SupervisorRequirements:
    """The floors and exit-code sets for a host running ``serve()``.

    Args:
        stop_grace_secs: The same value the host passes to
            ``serve(stop_grace_secs=...)``. Defaults to ``serve()``'s own default,
            so a host that does not override it needs no argument here.

    Returns:
        A ``SupervisorRequirements`` whose floors track the library constants they
        derive from, so a config test fails when those constants change rather
        than silently drifting out of agreement with them.

    There is deliberately no ``restart_delay_secs`` argument: the case lease TTL is
    a library constant, and picking an actual delay above it is deployment policy
    this library has no business choosing.
    """
    return SupervisorRequirements(
        restart_delay_floor_secs=DEFAULT_LEASE_TTL_SECS,
        stop_timeout_floor_secs=stop_grace_secs,
        restart_on=frozenset({EXIT_WATCHDOG, EXIT_RESTART_REQUESTED, EXIT_STARTUP_REFUSED}),
        no_restart_on=frozenset({EXIT_DELIBERATE_STOP}),
        prompt_restart_on=frozenset({EXIT_RESTART_REQUESTED}),
    )
