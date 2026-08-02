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

``ltx`` is the sweep's CaseTransition snapshot. Assertions are SYNCHRONOUS
by contract (the ``on_terminating`` precedent): they run at the transition
boundary with no keepalive spanning them, so heavy work belongs in a
``perform_`` step, not here.

A failing assertion is a recorded fact, not control flow: CASE_ASSERT_FAILED
per failure + one CASE_ASSERTED summary per sweep, a ``self.log`` error line,
and the overridable ``on_assertion_failed`` hook. The machine is unaffected —
nothing here counts toward @FAIL or raises into dispatch.

A third, automatic check rides the same sweep: every asset alias whose
declared ``trust_states`` claims validity in the state just entered gets loaded
(``case_load_asset``/``case_load_assets``) purely to confirm it does not
raise — no hand-written assertion required. This catches drift between an
alias's declared ``trust_states`` and the code that actually populates the file. It
is unconditional (independent of whether the class or the folder define any
``case_assert_*`` for that state) but obeys the same ``AssertionMode`` knob as
everything else in the sweep: ``SKIP`` turns it off along with everything
else, for performance-sensitive callers.

This module owns all assertion mechanics; FolderBackedCase only wires them in
(the ``_CaseAdvancer`` / ``_CaseMachineFactory`` pattern).
"""

from __future__ import annotations

import enum
import importlib.util
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from totodev_pub.folder_backed_case_support.case_journal import (
    CaseEventJournal, CaseTransition,
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


@dataclass(frozen=True)
class AssertionFailure:
    """One failure from a single ``sweep`` call (mirrors CASE_ASSERT_FAILED data)."""

    name: Optional[str]
    source: str
    msg: str
    error: Optional[str] = None


@dataclass(frozen=True)
class AssertionSweepResult:
    """In-process outcome of one ``sweep`` — the mirror of CASE_ASSERTED + failures."""

    state: str
    ran: int
    failed: int
    mode: str
    failures: list[AssertionFailure] = field(default_factory=list)


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

    def sweep(self, state: str) -> AssertionSweepResult:
        """Run every assertion tied to ``state`` (class methods first, name-sorted;
        then the automatic asset-loadability check; then file assertions) and close
        with one CASE_ASSERTED summary. Returns an in-process ``AssertionSweepResult``
        for this sweep only. Consults the process-global AssertionMode afresh on
        every call."""
        mode = get_case_assertion_mode()
        if mode is AssertionMode.SKIP:
            self._journal.log_asserted(state, ran=0, failed=0, mode=mode.value)
            return AssertionSweepResult(
                state=state, ran=0, failed=0, mode=mode.value, failures=[],
            )
        ltx = self._journal.last_transition()
        ran = 0
        failures: list[AssertionFailure] = []
        for slug, method_name in self._class_assertions.get(state, []):
            ran += 1
            fn = getattr(self._case, method_name)
            hit = self._run_one(state, slug, "method", fn, (ltx,))
            if hit is not None:
                failures.append(hit)
        asset_ran, asset_failures = self._check_asset_loadability(state)
        ran += asset_ran
        failures.extend(asset_failures)
        if mode is AssertionMode.FULL:
            file_ran, file_failures = self._sweep_files(state, ltx)
            ran += file_ran
            failures.extend(file_failures)
        self._journal.log_asserted(
            state, ran=ran, failed=len(failures), mode=mode.value,
        )
        return AssertionSweepResult(
            state=state, ran=ran, failed=len(failures), mode=mode.value,
            failures=failures,
        )

    def _check_asset_loadability(
        self, state: str,
    ) -> tuple[int, list[AssertionFailure]]:
        """Confirm every asset alias trusted in ``state`` (per its declared
        ``AssetSpec.trust_states``) can actually be loaded. Unconditional — it runs
        whether or not any hand-written ``case_assert_*`` targets this state; the
        only gate is the mode check already done by the caller (``sweep``)."""
        book = type(self._case)._resolve_asset_book()
        ran = 0
        failures: list[AssertionFailure] = []
        for alias in book.trusted_aliases(state):
            spec = book.spec(alias)
            loader = self._case.case_load_assets if spec.many else self._case.case_load_asset
            ran += 1
            hit = self._run_asset_load(state, alias, loader)
            if hit is not None:
                failures.append(hit)
        return ran, failures

    def _run_asset_load(
        self, state: str, alias: str, loader: Callable,
    ) -> Optional[AssertionFailure]:
        """Load one asset alias purely to confirm it does not raise. Unlike
        ``_run_one``, the loaded value's truthiness is irrelevant — only an
        exception counts as failure here."""
        try:
            loader(alias)
        except Exception as exc:
            return self._record_failure(
                state, f"asset_loadable:{alias}", "asset-load",
                str(exc) or type(exc).__name__, type(exc).__name__,
            )
        return None

    # ---- single-assertion execution (isolation boundary) ----

    def _run_one(
        self, state: str, slug: str, source: str, fn: Callable, args: tuple,
    ) -> Optional[AssertionFailure]:
        """Run one assertion; return the failure record or None on pass. NEVER raises."""
        error: Optional[str] = None
        try:
            result = fn(*args)
        except Exception as exc:               # raise inside an assertion = FAIL
            result, error = str(exc) or type(exc).__name__, type(exc).__name__
        if not result:                          # falsy = PASS (None, "", False, 0)
            return None
        msg = result if isinstance(result, str) else str(result)
        return self._record_failure(state, slug, source, msg, error)

    def _record_failure(
        self, state: str, name: Optional[str], source: str, msg: str,
        error: Optional[str],
    ) -> AssertionFailure:
        value = f"{state}.{name}" if name else _msg_basename(source)
        self._journal.log_assert_failed(
            value, state=state, name=name, source=source, msg=msg, error=error,
        )
        self._case.log.error(
            "assertion %s (%s) FAILED in state %r: %s",
            name or source, source, state, msg,
        )
        hook = getattr(self._case, "on_assertion_failed", None)
        if hook is not None:
            try:
                hook(state, name or source, msg)
            except Exception:                   # a misbehaving hook must not mask/disturb
                self._case.log.exception(
                    "on_assertion_failed hook raised for case %s", self._case.case_id,
                )
        return AssertionFailure(name=name, source=source, msg=msg, error=error)

    def _sweep_files(
        self, state: str, ltx: Optional[CaseTransition],
    ) -> tuple[int, list[AssertionFailure]]:
        """Discover and run assertions/*.py functions tied to ``state``. Files are
        DATA: a broken file logs one import-failure CASE_ASSERT_FAILED and the sweep
        continues; a function naming an unknown state warns once (per instance) and
        is skipped. Returns (ran, failures) — import failures count in ``failures``
        only (nothing ran)."""
        folder = self._case.case_folder / ASSERTS_DIR_NAME
        if not folder.is_dir():
            return 0, []
        from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
        reader = FolderBackedCaseReader(self._case.case_folder)
        ran = 0
        failures: list[AssertionFailure] = []
        for path in sorted(folder.glob("*.py")):
            module, import_failure = self._load_module(path, state)
            if module is None:
                if import_failure is not None:
                    failures.append(import_failure)
                continue
            functions = sorted(
                (name, fn) for name, fn in vars(module).items()
                if callable(fn) and name.startswith(ASSERT_METHOD_PREFIX)
            )
            for name, fn in functions:
                parsed = match_assertion_name(name, self._states)
                if parsed is None:
                    key = f"{path.name}:{name}"
                    if key not in self._warned_unknown:
                        self._warned_unknown.add(key)
                        self._case.log.warning(
                            "assertion file %s defines %r, which matches no known "
                            "state of %s (states: %s); it will never run",
                            path.name, name, type(self._case).__name__,
                            sorted(self._states),
                        )
                    continue
                fn_state, slug = parsed
                if fn_state != state:
                    continue                    # tied to another (known) state
                ran += 1
                hit = self._run_one(
                    state, slug, f"file:{path.name}", fn, (reader, ltx),
                )
                if hit is not None:
                    failures.append(hit)
        return ran, failures

    def _load_module(
        self, path: Path, state: str,
    ) -> tuple[Optional[object], Optional[AssertionFailure]]:
        """Import an assertion file, cached by (path, mtime). Returns
        ``(module, None)`` on success, or ``(None, failure)`` after journaling an
        import failure. Modules are NOT placed in sys.modules — they are private
        to this runner (no global registry growth, matching the case-logger
        philosophy)."""
        cached = self._module_cache.get(path)
        try:
            mtime = path.stat().st_mtime
            if cached is not None and cached[0] == mtime:
                return cached[1], None
            spec = importlib.util.spec_from_file_location(
                f"_case_assertions__{self._case.case_id}__{path.stem}", path,
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            failure = self._record_failure(
                state, None, f"file:{path.name}",
                f"failed to import assertion file: {exc}", type(exc).__name__,
            )
            self._module_cache.pop(path, None)
            return None, failure
        self._module_cache[path] = (mtime, module)
        return module, None


def _msg_basename(source: str) -> str:
    """Event VALUE for a failure with no state.slug identity (an assertion file
    that failed to import): the file's basename from a 'file:<filename>' source."""
    return source.partition(":")[2] or source
