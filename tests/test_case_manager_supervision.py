# Part of the totodev_pub library.

"""The supervisor contract: exit-code sets, timing floors, and drift guards.

The substance here is the *drift* tests. A deployment cannot be exercised from
inside this library, so the value of publishing floors instead of prose is only
realised if something fails when a floor stops matching the constant it derives
from. ``test_restart_floor_is_the_lease_ttl`` and
``test_stop_floor_tracks_serves_own_default`` are those guards; the rest pin the
exit-code contract documented in ``docs/case-manager-deployment.md``.

The last section pins that document too. Every supervisor snippet it ships names
a restart delay and a kill timeout, and a doc recommending a config its own API
would reject is worse than no doc — so the snippets are parsed and fed back
through ``violations()``. Same idea as the layout-map tests: declaration, code and
document held together rather than trusted to stay in agreement.
"""

from __future__ import annotations

import configparser
import inspect
import re
from pathlib import Path

from totodev_pub.case_manager_support.case_manager_host import (
    DEFAULT_STOP_GRACE_SECS,
    EXIT_RESTART_REQUESTED,
    EXIT_WATCHDOG,
    serve,
)
from totodev_pub.case_manager_support.supervision import (
    EXIT_DELIBERATE_STOP,
    EXIT_STARTUP_REFUSED,
    supervisor_requirements,
)
from totodev_pub.folder_backed_case_support.constants import DEFAULT_LEASE_TTL_SECS


# ---------------------------------------------------------------- drift guards


def test_restart_floor_is_the_lease_ttl():
    """The floor is the lease TTL, pinned to its literal value on purpose.

    Asserting ``floor == DEFAULT_LEASE_TTL_SECS`` would be a tautology — both
    sides move together, so it could never fail. The number is written out here
    instead, because raising the TTL invalidates every restart delay derived from
    it: published examples, this repo's deployment doc, and any consumer's unit
    file. Changing the constant *should* break this test, and fixing it should be
    the moment someone goes and updates those.
    """
    assert supervisor_requirements().restart_delay_floor_secs == 30.0
    assert DEFAULT_LEASE_TTL_SECS == 30.0, "raise the floor above, then the docs"


def test_stop_floor_tracks_serves_own_default():
    """``serve()``'s default and the published floor cannot drift apart.

    Reads the signature rather than trusting the constant, because the failure
    mode is precisely someone editing the default back to a literal.
    """
    signature_default = inspect.signature(serve).parameters["stop_grace_secs"].default
    assert signature_default == DEFAULT_STOP_GRACE_SECS
    assert supervisor_requirements().stop_timeout_floor_secs == DEFAULT_STOP_GRACE_SECS


def test_stop_floor_follows_a_host_that_overrides_the_grace():
    """A host free to widen its grace must get a floor that widens with it."""
    assert supervisor_requirements(stop_grace_secs=90.0).stop_timeout_floor_secs == 90.0


# ---------------------------------------------------------------- exit codes


def test_deliberate_stop_is_never_restarted():
    """Exit 0 is the one code that must leave the process down.

    Restarting it turns a scale-down into a loop and makes ``stop_when_empty``
    self-completion impossible.
    """
    reqs = supervisor_requirements()
    assert EXIT_DELIBERATE_STOP in reqs.no_restart_on
    assert EXIT_DELIBERATE_STOP not in reqs.restart_on


def test_every_documented_failure_code_restarts():
    reqs = supervisor_requirements()
    assert reqs.restart_on == frozenset(
        {EXIT_WATCHDOG, EXIT_RESTART_REQUESTED, EXIT_STARTUP_REFUSED}
    )


def test_the_two_sets_never_overlap():
    reqs = supervisor_requirements()
    assert not (reqs.restart_on & reqs.no_restart_on)


def test_only_a_requested_shutdown_may_restart_promptly():
    """The delay exists to outwait leases, and a clean stop released them.

    Requested shutdown drains and releases the filespace lease, so it alone needs
    no delay. Every other restartable code exits hard and leaves leases held.
    """
    reqs = supervisor_requirements()
    assert reqs.prompt_restart_on == frozenset({EXIT_RESTART_REQUESTED})
    assert reqs.prompt_restart_on <= reqs.restart_on


# ---------------------------------------------------------------- violations()


def test_a_conforming_config_has_no_violations():
    """Derived from the floors, so this stays a test of ``violations()`` alone.

    Hardcoding 35 and 45 here would make it a second, badly-named drift guard —
    it would fail when a constant moved, for reasons nothing in its name explains.
    """
    reqs = supervisor_requirements()
    problems = reqs.violations(
        restart_delay_secs=reqs.restart_delay_floor_secs + 5.0,
        stop_timeout_secs=reqs.stop_timeout_floor_secs + 15.0,
    )
    assert problems == ()


def test_a_value_exactly_at_the_floor_is_not_enough():
    """Both floors are strict; equality is the off-by-one this test exists for."""
    reqs = supervisor_requirements()
    problems = reqs.violations(
        restart_delay_secs=DEFAULT_LEASE_TTL_SECS,
        stop_timeout_secs=DEFAULT_STOP_GRACE_SECS,
    )
    assert len(problems) == 2


def test_each_floor_is_reported_independently():
    reqs = supervisor_requirements()
    good_delay = reqs.restart_delay_floor_secs + 5.0
    good_timeout = reqs.stop_timeout_floor_secs + 15.0
    only_delay = reqs.violations(restart_delay_secs=1.0, stop_timeout_secs=good_timeout)
    only_timeout = reqs.violations(restart_delay_secs=good_delay, stop_timeout_secs=1.0)
    assert len(only_delay) == 1 and "restart delay" in only_delay[0]
    assert len(only_timeout) == 1 and "stop timeout" in only_timeout[0]


def test_violation_messages_name_the_floor_they_failed():
    """A message without the number is useless in a failing config test."""
    reqs = supervisor_requirements()
    (message,) = reqs.violations(
        restart_delay_secs=1.0, stop_timeout_secs=reqs.stop_timeout_floor_secs + 15.0
    )
    assert str(DEFAULT_LEASE_TTL_SECS) in message


# ---------------------------------------------------------------- public surface


def test_the_contract_is_importable_from_the_package_root():
    """The point of the export: a consumer never hardcodes 70 or 75.

    Before this, the codes were reachable only through
    ``case_manager_support.case_manager_host`` — a path that reads as private, and
    which nothing outside the test suite imported.
    """
    import totodev_pub.case_manager_support as pkg

    assert pkg.EXIT_WATCHDOG == EXIT_WATCHDOG
    assert pkg.EXIT_RESTART_REQUESTED == EXIT_RESTART_REQUESTED
    assert pkg.supervisor_requirements().restart_on == supervisor_requirements().restart_on
    for name in ("EXIT_WATCHDOG", "EXIT_RESTART_REQUESTED", "supervisor_requirements"):
        assert name in pkg.__all__


def test_the_package_root_does_not_eagerly_import_the_host():
    """The lazy-export idiom exists so unused surface costs nothing.

    ``case_manager_support/__init__.py`` explains at length why nothing may be
    bound at package-import time. Routing the exit codes through
    ``case_manager_host`` would quietly undo that if it were done eagerly, since
    every ``CaseManager`` import executes this package's ``__init__``.
    """
    import subprocess
    import sys

    probe = (
        "import totodev_pub.case_manager_support, sys; "
        "print('totodev_pub.case_manager_support.case_manager_host' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stdout + out.stderr


# ------------------------------------------------------- the deployment document

DEPLOYMENT_DOC = Path(__file__).resolve().parents[1] / "docs" / "case-manager-deployment.md"


def _blocks(language: str) -> list[str]:
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    return re.findall(rf"```{language}\n(.*?)```", text, re.S)


def _ini_section(block: str, section: str) -> configparser.SectionProxy:
    parser = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"), strict=True)
    parser.optionxform = str  # systemd and supervisord keys are case-sensitive
    parser.read_string(block)
    return parser[section]


def test_the_systemd_unit_satisfies_the_contract_it_documents():
    service = _ini_section(_blocks("ini")[0], "Service")
    reqs = supervisor_requirements()
    assert (
        reqs.violations(
            restart_delay_secs=float(service["RestartSec"]),
            stop_timeout_secs=float(service["TimeoutStopSec"]),
        )
        == ()
    )


def test_the_systemd_unit_is_installable():
    """Without ``[Install]`` the unit cannot be enabled, so it never starts at boot.

    A regression test: the section was dropped once while the rest of the snippet
    stayed plausible, and nothing about the unit looks wrong without it.
    """
    assert _ini_section(_blocks("ini")[0], "Install")["WantedBy"] == "multi-user.target"


def test_the_supervisord_program_satisfies_the_contract():
    program = _ini_section(_blocks("ini")[1], "program:fleet")
    reqs = supervisor_requirements()
    assert (
        reqs.violations(
            restart_delay_secs=float(program["startsecs"]),
            stop_timeout_secs=float(program["stopwaitsecs"]),
        )
        == ()
    )
    # `exitcodes` is what keeps a deliberate stop from being restarted.
    assert {int(c) for c in program["exitcodes"].split(",")} == set(reqs.no_restart_on)


def test_the_compose_grace_period_clears_the_floor():
    grace = re.search(r"stop_grace_period:\s*(\d+)s", _blocks("yaml")[0])
    assert grace is not None, "the compose snippet lost its stop_grace_period"
    reqs = supervisor_requirements()
    assert float(grace.group(1)) > reqs.stop_timeout_floor_secs


def test_the_shell_loop_handles_every_exit_code_the_way_the_contract_says():
    """The fallback loop is the one snippet that spells the whole policy out.

    Parsing it back guards the case where a code is added to the contract and the
    example silently stops covering it — the arm would simply never match, and the
    loop would treat a known code as an unexpected bug.
    """
    loop = _blocks("sh")[0]
    reqs = supervisor_requirements()

    no_restart = {int(c) for c in re.findall(r"^\s*(\d+)\)\s*exit 0", loop, re.M)}
    prompt = {int(c) for c in re.findall(r"^\s*(\d+)\)\s*fails=0; delay=1", loop, re.M)}
    delayed_arm = re.search(r"^\s*([\d|]+)\)\s*fails=\$\(\(fails\+1\)\); delay=(\d+)", loop, re.M)
    assert delayed_arm is not None, "the delayed-restart arm changed shape"
    delayed = {int(c) for c in delayed_arm.group(1).split("|")}

    assert no_restart == set(reqs.no_restart_on)
    assert prompt == set(reqs.prompt_restart_on)
    assert delayed == set(reqs.restart_on - reqs.prompt_restart_on)
    assert prompt | delayed == set(reqs.restart_on), "a restartable code is unhandled"
    assert float(delayed_arm.group(2)) > reqs.restart_delay_floor_secs


def test_the_shell_loop_forwards_sigterm():
    """Without the trap the loop dies and orphans the manager mid-drain."""
    loop = _blocks("sh")[0]
    assert re.search(r"trap\s+'kill -TERM", loop), "the SIGTERM trap is gone"
    assert "TERM INT" in loop
