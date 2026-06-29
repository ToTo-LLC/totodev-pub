# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""AdvanceResult: the structured outcome of a single FolderBackedCase.case_advance() call.

case_advance() is a NON-THROWING reporter for the auto-driver: it (almost) always returns one
of these instead of raising, so a blind driver can inspect what happened without wrapping
every call in try/except. Exceptions are carried as DATA in `exceptions` rather than
thrown — including the synthetic AutoAdvanceBlocked when guards provably wall off every
auto path. (A direct/manual `await case.<trigger>()` call, which has no AdvanceResult to
return, still raises through the machine's exception handler.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from totodev_pub.folder_backed_case_support.exceptions import AutoAdvanceBlocked


@dataclass(frozen=True)
class AdvanceResult:
    """One case_advance() outcome.

    Attributes:
        initial_state  the state the case was in when case_advance() began.
        final_state    the state the case is in when case_advance() returned.
        trigger        the auto trigger that fired (or, on a failure, the one attempted);
                       None when nothing was attempted (e.g. already closed / no candidates).
        exceptions     exceptions encountered, carried as DATA (not raised): a transition
                       failure (the original exception, decorated with `.case_context`),
                       and/or an AutoAdvanceBlocked when the case is provably stuck. Usually
                       empty or a single entry.
        alerts         CASE_ALERT messages logged during this step (e.g. from inside a hook
                       or the auto-block detector), harvested by case_advance() so a blind
                       driver sees an alert that neither changed state nor raised. Empty when
                       none.
    """
    initial_state: str
    final_state: str
    trigger: Optional[str] = None
    exceptions: tuple = field(default_factory=tuple)
    alerts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def progressed(self) -> bool:
        """Did the case actually change state this call?"""
        return self.final_state != self.initial_state

    @property
    def blocked(self) -> bool:
        """Is the case provably auto-advance blocked (an AutoAdvanceBlocked was carried)?"""
        return any(isinstance(e, AutoAdvanceBlocked) for e in self.exceptions)

    @property
    def failed(self) -> bool:
        """Did a transition attempt raise (any carried exception that is NOT the blocked
        marker)? A failed attempt typically left the case in its source state to retry."""
        return any(not isinstance(e, AutoAdvanceBlocked) for e in self.exceptions)

    @property
    def alerted(self) -> bool:
        """Did the step log at least one CASE_ALERT (harvested into `alerts`)?"""
        return bool(self.alerts)

    def __bool__(self) -> bool:
        """Truthy iff the case progressed — so `if await case.case_advance():` driver loops
        keep their natural meaning under the richer return type."""
        return self.progressed
