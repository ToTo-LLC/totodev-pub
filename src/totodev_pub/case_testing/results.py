# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Narrative result objects returned by CaseWorkbench methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class WorkbenchReport:
    """Base display carrier: narrative in ``__str__``/``__repr__``/markdown."""

    narrative: str = ""

    def _body(self) -> str:
        return self.narrative.strip()

    def __str__(self) -> str:
        body = self._body()
        name = type(self).__name__
        return f"{name}\n{body}" if body else name

    def __repr__(self) -> str:
        return str(self)

    def _repr_markdown_(self) -> str:
        body = self._body()
        name = type(self).__name__
        if not body:
            return f"**{name}**"
        # Escape nothing aggressive — narratives are plain prose / simple tables.
        return f"**{name}**\n\n```\n{body}\n```"


@dataclass
class CloneReport(WorkbenchReport):
    case: Any = None


@dataclass
class CreateReport(WorkbenchReport):
    case: Any = None


@dataclass
class FocusReport(WorkbenchReport):
    case: Any = None


@dataclass
class AdvanceReport(WorkbenchReport):
    result: Any = None
    new_problems: list[str] = field(default_factory=list)


@dataclass
class RunReport(WorkbenchReport):
    steps: list[Any] = field(default_factory=list)
    new_problems: list[str] = field(default_factory=list)


@dataclass
class StatusReport(WorkbenchReport):
    state: Optional[str] = None
    dwell_secs: Optional[float] = None
    terminal: bool = False
    advanceable: bool = False
    problem_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ProbeEdge:
    trigger: str
    dest: str
    kind: str  # "auto" | "manual"
    guards: list[tuple[str, Any]] = field(default_factory=list)  # (name, True|False|"error")
    fact_guards: list[str] = field(default_factory=list)
    chokes: list[str] = field(default_factory=list)


@dataclass
class ProbeReport(WorkbenchReport):
    edges: list[ProbeEdge] = field(default_factory=list)


@dataclass
class ProblemsReport(WorkbenchReport):
    alerts: list[Any] = field(default_factory=list)
    assert_failures: list[Any] = field(default_factory=list)
    transition_failures: list[Any] = field(default_factory=list)
    entry_exceptions: list[Any] = field(default_factory=list)
    trigger_timeouts: list[Any] = field(default_factory=list)


@dataclass
class HistoryStep:
    index: int
    from_state: Optional[str]
    to_state: str
    trigger: Optional[str]
    duration_ms: Optional[int]


@dataclass
class HistoryReport(WorkbenchReport):
    steps: list[HistoryStep] = field(default_factory=list)


@dataclass
class PathReport(WorkbenchReport):
    folder: Path = field(default_factory=Path)


@dataclass
class TreeReport(WorkbenchReport):
    root: Path = field(default_factory=Path)
    entries: list[str] = field(default_factory=list)


@dataclass
class HeadReport(WorkbenchReport):
    path: Optional[Path] = None
    text: Optional[str] = None
    binary_bytes: Optional[int] = None


@dataclass
class IncomingAssetsReport(WorkbenchReport):
    paths: list[Path] = field(default_factory=list)
    group: Optional[str] = None


@dataclass
class CasesReport(WorkbenchReport):
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DoctorFinding:
    level: str  # "info" | "warning"
    message: str


@dataclass
class DoctorReport(WorkbenchReport):
    findings: list[DoctorFinding] = field(default_factory=list)


@dataclass
class HelpReport(WorkbenchReport):
    name: Optional[str] = None


@dataclass
class FreezeDryReport(WorkbenchReport):
    nickname: Optional[str] = None
    dest: Optional[Path] = None


@dataclass
class ListExamplesReport(WorkbenchReport):
    examples: list[tuple[str, str]] = field(default_factory=list)  # (nickname, description)
