# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Case assertions: state-tied, observe-only sanity checks for FolderBackedCase.

Spec: volatile/specs/2026-07-17-case-assertions-mini-spec.md. Two authoring
channels share one naming convention (``case_assert_<state>_<slug>``) and one
return contract (falsy = pass; truthy = fail with ``str(value)`` as message;
raise = fail with error detail):

  * CLASS methods on the case subclass — ``def case_assert_<state>_<slug>(self, ltx)``.
  * PER-CASE files in ``assertions/*.py`` inside the case folder —
    ``def case_assert_<state>_<slug>(case_reader, ltx)`` where ``case_reader``
    is a read-only FolderBackedCaseReader (observe-only by construction).

``ltx`` is the sweep's CaseLastTransition snapshot. Assertions are SYNCHRONOUS
by contract (the ``on_terminating`` precedent): they run at the transition
boundary with no keepalive spanning them, so heavy work belongs in a
``perform_`` step, not here.

A failing assertion is a recorded fact, not control flow: CASE_ASSERT_FAILED
per failure + one CASE_ASSERTED summary per sweep, a ``self.log`` error line,
and the overridable ``on_assertion_failed`` hook. The machine is unaffected —
nothing here counts toward @FAIL or raises into dispatch.

This module owns all assertion mechanics; FolderBackedCase only wires them in
(the ``_CaseAdvancer`` / ``_CaseMachineFactory`` pattern).
"""

from __future__ import annotations

import enum
import importlib.util
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from totodev_pub.folder_backed_case_support.case_journal import (
    CaseEventJournal, CaseLastTransition,
)
from totodev_pub.folder_backed_case_support.constants import ASSERTS_DIR_NAME
from totodev_pub.folder_backed_case_support.exceptions import FsmBindingError

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase
    from totodev_pub.folder_backed_case_support.state_chain_parser import FsmChainSpec

# The assertion hook-name convention. Deliberately NOT in the parser's
# _HOOK_METHOD_PREFIXES registry: those prefixes demand an EXACT suffix match
# against one FSM name kind, while an assertion suffix is <state>_<slug> and
# needs the greedy state match below. Validation therefore lives here
# (validate_case_assertion_methods), called from the same bind-time block as
# validate_object_compatibility.
ASSERT_METHOD_PREFIX = "case_assert_"


class AssertionMode(enum.Enum):
    """Process-global assertion policy (the LogRetention pattern).

    FULL runs both channels. CLASS_ONLY never imports folder code — for fleets
    ingesting case folders of doubtful provenance (the assertions/ channel is,
    by design, "execute code that arrived as data"). SKIP runs nothing but
    still writes the CASE_ASSERTED summary (mode="skip") so observers can
    distinguish "all passed" from "never ran".
    """

    FULL = "full"
    CLASS_ONLY = "class_only"
    SKIP = "skip"


_MODE: AssertionMode = AssertionMode.FULL


def set_case_assertion_mode(mode: AssertionMode) -> None:
    """Set the process-global assertion mode (default FULL). A coarse startup
    knob, consulted afresh at every sweep — a long-lived process may flip it."""
    global _MODE
    if not isinstance(mode, AssertionMode):
        raise TypeError(f"mode must be an AssertionMode, got {type(mode).__name__}")
    _MODE = mode


def get_case_assertion_mode() -> AssertionMode:
    """The current process-global assertion mode."""
    return _MODE


def match_assertion_name(
    name: str, states: list[str] | set[str] | frozenset[str],
) -> Optional[tuple[str, str]]:
    """Split ``case_assert_<state>_<slug>`` into (state, slug), or None.

    State names contain underscores, so ``<state>`` is matched GREEDILY: the
    longest known state name that prefixes the suffix wins (state 'open_ticket'
    beats 'open' for 'case_assert_open_ticket_check'). Returns None when the
    prefix is absent, no known state matches, or the slug is empty.
    """
    if not name.startswith(ASSERT_METHOD_PREFIX):
        return None
    rest = name[len(ASSERT_METHOD_PREFIX):]
    for state in sorted(states, key=len, reverse=True):
        if rest.startswith(state + "_"):
            slug = rest[len(state) + 1:]
            if slug:
                return state, slug
    return None


def _accepts_one_positional(fn) -> bool:
    """True if the (unbound) function can be called with (self, ltx) — i.e. it
    binds two positionals. Mirrors the parser's _accepts_tctx probe; assume True
    when the signature cannot be introspected."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    try:
        sig.bind(object(), object())
    except TypeError:
        return False
    return True


def validate_case_assertion_methods(case_cls: type, states: list[str]) -> None:
    """Bind-time gate for the class assertion channel: every ``case_assert_*``
    method must name a known state (greedy match), carry a non-empty slug, be
    SYNCHRONOUS, and accept the single ``ltx`` argument after ``self``. Raises
    a teaching FsmBindingError listing all offenders at once. Runs once per
    concrete class, alongside validate_object_compatibility."""
    convention = (
        f"assertions are named `{ASSERT_METHOD_PREFIX}<state>_<slug>` where <state> "
        "is a state of this class's FSM (matched greedily — the longest state name "
        "wins) and <slug> is a non-empty label"
    )
    bad: list[tuple[str, str]] = []
    for name in sorted(dir(case_cls)):
        if not name.startswith(ASSERT_METHOD_PREFIX):
            continue
        fn = getattr(case_cls, name, None)
        if not callable(fn):
            continue
        parsed = match_assertion_name(name, states)
        if parsed is None:
            bad.append((
                name,
                f"does not match any known state; {convention}. "
                f"Known states: {sorted(states)}",
            ))
            continue
        if inspect.iscoroutinefunction(fn):
            bad.append((
                name,
                "assertions are synchronous by contract — declare it with plain "
                "`def` (heavy work belongs in a perform_ step, not an assertion)",
            ))
        if not _accepts_one_positional(fn):
            bad.append((
                name,
                "must accept the last-transition snapshot: declare it "
                f"`def {name}(self, ltx)` (use `ltx` even if unused)",
            ))
    if bad:
        raise FsmBindingError(case_cls.__name__, bad_assertions=bad)


def discover_class_assertions(
    case_cls: type, states: list[str],
) -> dict[str, list[tuple[str, str]]]:
    """Map each state to its class assertions as (slug, method_name), sorted by
    method name (the sweep's deterministic order). Assumes
    validate_case_assertion_methods already passed for this class."""
    found: dict[str, list[tuple[str, str]]] = {}
    for name in sorted(dir(case_cls)):
        if not name.startswith(ASSERT_METHOD_PREFIX):
            continue
        if not callable(getattr(case_cls, name, None)):
            continue
        parsed = match_assertion_name(name, states)
        if parsed is None:
            continue
        state, slug = parsed
        found.setdefault(state, []).append((slug, name))
    return found


class _CaseAssertionRunner:
    """Executes one assertion sweep per state entry on behalf of a bound case.

    Internal by convention (leading underscore), like _CaseAdvancer and
    _CaseMachineFactory: not part of any public surface. FolderBackedCase
    constructs one per bound instance and calls ``sweep(state)`` from
    ``_on_state_changed``. Per-assertion isolation is absolute: an assertion
    that fails, returns garbage, or raises — and a misbehaving
    ``on_assertion_failed`` hook — never disturbs the sweep, let alone the
    dispatch that triggered it.
    """

    def __init__(
        self, case: "FolderBackedCase", fsm: "FsmChainSpec", journal: CaseEventJournal,
    ) -> None:
        self._case = case
        self._journal = journal
        self._states = list(fsm.states)
        self._class_assertions = discover_class_assertions(type(case), self._states)
        # Per-file module cache for the assertions/ channel: path -> (mtime, module).
        self._module_cache: dict[Path, tuple[float, object]] = {}
        # Unknown-state file functions already warned about (once per instance).
        self._warned_unknown: set[str] = set()

    def sweep(self, state: str) -> None:
        """Run every assertion tied to ``state`` (class methods first, name-sorted;
        then file assertions) and close with one CASE_ASSERTED summary. Consults
        the process-global AssertionMode afresh on every call."""
        mode = get_case_assertion_mode()
        if mode is AssertionMode.SKIP:
            self._journal.log_asserted(state, ran=0, failed=0, mode=mode.value)
            return
        ltx = self._journal.last_transition()
        ran = 0
        failed = 0
        for slug, method_name in self._class_assertions.get(state, []):
            ran += 1
            fn = getattr(self._case, method_name)
            failed += self._run_one(state, slug, "method", fn, (ltx,))
        if mode is AssertionMode.FULL:
            file_ran, file_failed = self._sweep_files(state, ltx)
            ran += file_ran
            failed += file_failed
        self._journal.log_asserted(state, ran=ran, failed=failed, mode=mode.value)

    # ---- single-assertion execution (isolation boundary) ----

    def _run_one(
        self, state: str, slug: str, source: str, fn: Callable, args: tuple,
    ) -> int:
        """Run one assertion; return 1 on failure, 0 on pass. NEVER raises."""
        error: Optional[str] = None
        try:
            result = fn(*args)
        except Exception as exc:               # raise inside an assertion = FAIL
            result, error = str(exc) or type(exc).__name__, type(exc).__name__
        if not result:                          # falsy = PASS (None, "", False, 0)
            return 0
        msg = result if isinstance(result, str) else str(result)
        self._record_failure(state, slug, source, msg, error)
        return 1

    def _record_failure(
        self, state: str, name: Optional[str], source: str, msg: str,
        error: Optional[str],
    ) -> None:
        value = f"{state}.{name}" if name else msg_basename(source)
        self._journal.log_assert_failed(
            value, state=state, name=name, source=source, msg=msg, error=error,
        )
        self._case.log.error(
            "assertion %s (%s) FAILED in state %r: %s",
            name or source, source, state, msg,
        )
        hook = getattr(self._case, "on_assertion_failed", None)
        if hook is None:
            return
        try:
            hook(state, name or source, msg)
        except Exception:                       # a misbehaving hook must not mask/disturb
            self._case.log.exception(
                "on_assertion_failed hook raised for case %s", self._case.case_id,
            )

    # ---- file channel (Task 5 fills this in) ----

    def _sweep_files(
        self, state: str, ltx: Optional[CaseLastTransition],
    ) -> tuple[int, int]:
        return 0, 0


def msg_basename(source: str) -> str:
    """Event VALUE for a failure with no state.slug identity (an assertion file
    that failed to import): the file's basename from a 'file:<filename>' source."""
    return source.partition(":")[2] or source
