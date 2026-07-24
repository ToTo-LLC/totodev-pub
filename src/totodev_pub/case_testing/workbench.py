# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseWorkbench — interactive single-case testing helper for FolderBackedCase.

Design-time / validation tooling for exploring one FolderBackedCase at a time:
construct or clone into a scratch folder, drive the FSM, inspect state, and
freeze-dry useful snapshots onto a fixtures shelf. Aimed at medium-sized
business apps that need careful testing of reliability-sensitive case
lifecycles. Prefer using it from inside a project that depends on totodev-pub
(a repo with ``pyproject.toml``), not as a fully stand-alone sandbox.

**Unstable API.** CaseWorkbench is usable for interactive exploration, but
this interface should not yet be treated as stable. Changes are planned to
improve workbench + marimo notebook use for regression testing.

Mental model
------------
* **Fixtures shelf** — durable freeze-dried examples (default
  ``tests/case-fixtures`` via ``for_project``).
* **Scratch pool** — working case folders (default
  ``volatile/case-workbench/scratch``).
* **Focus** — ``wb.case``, the live case most commands act on.

**Intended usage pattern.** CaseWorkbench copies cases into the project's
scratch area and views/manipulates them there. Useful snapshots are later
copied onto the project's fixtures shelf (``freeze_dry``) or to an absolute
destination. By design it is **not** an in-place editor of arbitrary case
folders — prefer ``clone`` (including absolute paths) over focusing a live
folder elsewhere on disk.

Project-first setup (marimo, IPython, or a script)::

    from totodev_pub.case_testing import CaseWorkbench

    wb = CaseWorkbench.for_project()  # finds pyproject.toml; prints doctor()
    print(wb.help())

``for_project`` also caches a class index under ``volatile/case-workbench/``.
(Advanced: pass absolute ``fixtures_root`` / ``scratch_root`` instead.)

Happy path
----------
Create (or clone a freeze-dried example), inspect, drive (async), freeze-dry::

    wb.create("MyCase", nickname="demo")          # or wb.create(MyCase)
    # optional: wb.clone("MyCase/some_group/sample")   # when the shelf has one
    # optional: wb.clone(Path("/abs/path/to/case_folder"))
    print(wb.status()); print(wb.probe())
    await wb.advance()   # or await wb.run() / await wb.trigger(...)
    wb.freeze_dry(nickname="group/sample", description="...")
    # or: wb.freeze_dry(dest=Path("/abs/exports/snapshot"))

Driving methods are async. In marimo or IPython use ``await``; elsewhere use
``asyncio.run(...)`` or run under ``python -m asyncio``. Methods return
narrative report objects that print cleanly; use ``wb.case`` for the live
instance. To inspect a case folder elsewhere on disk, ``clone`` an absolute
path into scratch (do not focus the original in place).

Discovery and cleanup
---------------------
* ``wb.help()`` / ``wb.help("create")`` — API catalog and per-method docs.
* ``wb.list_examples()``, ``wb.doctor()`` — shelf browse and environment check.
* ``wb.refresh_class_index()`` — rescan if a new
  ``@case_type_registry.register`` class is not found yet.
* Scratch is durable by default. Call ``wb.cleanup()``, or use
  ``with CaseWorkbench.for_project() as wb:``, to detach live cases and clear
  the scratch pool.

Beyond marimo
-------------
The same API works in IPython, ``python -m asyncio``, and plain scripts. See
``notebooks/case_workbench_template.py`` for a pasteable tour. Tighter
regression-harness integration is planned; until then treat this as
exploration-first tooling.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import random
import re
import shutil
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from totodev_pub.case_testing.class_index import (
    ClassIndex,
    default_index_path,
    ensure_dir,
    find_project_root,
)
from totodev_pub.case_testing.errors import WorkbenchError
from totodev_pub.case_testing.results import (
    AdvanceReport,
    CasesReport,
    CloneReport,
    CreateReport,
    DoctorFinding,
    DoctorReport,
    FocusReport,
    FreezeDryReport,
    HeadReport,
    HistoryReport,
    HistoryStep,
    IncomingAssetsReport,
    ListExamplesReport,
    PathReport,
    ProbeEdge,
    ProbeReport,
    ProblemsReport,
    RunReport,
    StatusReport,
    TreeReport,
    HelpReport,
)
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import (
    EV_ALERTED,
    EV_ASSERT_FAILED,
    EV_ENTRY_EXCEPTION,
    EV_STATE_ENTERED,
    EV_TRANSITION_FAILED,
    EV_TRIGGER_STARTED,
    EV_TRIGGER_TIMED_OUT,
    LEASE_NAME,
    LOGS_DIR_NAME,
    RECORD_NAME,
    WORKBENCH_DIR_NAME,
)
from totodev_pub.folder_backed_case_support.case_briefing import _fmt_fact_guard
from totodev_pub.folder_backed_case_support.state_chain_parser import _is_method_guard

_ASSET_LIKE_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff",
    ".doc", ".docx", ".xls", ".xlsx", ".csv", ".tsv", ".zip", ".gz",
    ".json", ".xml", ".yaml", ".yml", ".txt", ".md", ".bin",
})

_HELP_SECTIONS: list[tuple[str, frozenset[str]]] = [
    ("Construct / shelf", frozenset({
        "for_project", "list_examples", "clone", "create", "freeze_dry",
    })),
    ("Scratch / focus", frozenset({
        "cases", "focus", "case", "cleanup", "path", "tree", "head",
        "incoming_assets",
    })),
    ("Drive", frozenset({"advance", "trigger", "run"})),
    ("Inspect", frozenset({
        "status", "probe", "problems", "history", "doctor",
    })),
    ("Meta", frozenset({
        "help", "refresh_class_index", "fixtures_root", "scratch_root",
        "project_root",
    })),
]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand4() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "case"


def _folder_has_contents(path: Path) -> bool:
    """True if *path* is a directory containing any entry."""
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


class CaseWorkbench:
    """Single-session helper for constructing, cloning, driving, and freeze-drying one case at a time.

    See the module docstring for a usage tour (project setup, happy path, async notes).
    """

    _HELP_EXCLUDE: frozenset[str] = frozenset()

    def __init__(
        self,
        fixtures_root: Path,
        scratch_root: Path,
        *,
        project_root: Path | None = None,
        keep_assets: bool = True,
        log_to_case: bool = True,
        echo: bool = False,
        case_modules: list[str] | None = None,
        source_roots: list[Path] | None = None,
        class_index: ClassIndex | None = None,
    ):
        self._fixtures_root = Path(fixtures_root).resolve()
        self._scratch_root = Path(scratch_root).resolve()
        self._project_root = Path(project_root).resolve() if project_root else None
        self._keep_assets = keep_assets
        self._log_to_case = log_to_case
        self._echo = echo
        self._case: FolderBackedCase | None = None
        self._live: dict[str, FolderBackedCase] = {}
        self._replace_map: dict[str, str] = {}  # fixture nickname -> scratch case_id
        roots = source_roots
        if roots is None and self._project_root is not None:
            src = self._project_root / "src"
            roots = [src] if src.is_dir() else []
        self._class_index = class_index or ClassIndex(
            source_roots=list(roots or []),
            case_modules=case_modules,
            cache_path=default_index_path(self._project_root, self._scratch_root.parent),
            project_root=self._project_root,
        )
        if class_index is None:
            self._class_index.load_cache_or_refresh()

    # --- read-only roots -------------------------------------------------

    @property
    def fixtures_root(self) -> Path:
        return self._fixtures_root

    @property
    def scratch_root(self) -> Path:
        return self._scratch_root

    @property
    def project_root(self) -> Path | None:
        return self._project_root

    @property
    def case(self) -> FolderBackedCase | None:
        return self._case

    # --- construction ----------------------------------------------------

    @classmethod
    def for_project(
        cls,
        fixtures_root: Path | str | None = None,
        scratch_root: Path | str | None = None,
        *,
        fixtures_relpath: str | None = None,
        scratch_relpath: str | None = None,
        project_root: Path | str | None = None,
        case_modules: list[str] | None = None,
        source_roots: list[Path] | None = None,
        quiet: bool = False,
        **kwargs,
    ) -> CaseWorkbench:
        """Locate the project and build a workbench with default roots."""
        explicit_project = Path(project_root).resolve() if project_root else None
        found = find_project_root()
        proj = explicit_project or found

        if fixtures_root is not None and fixtures_relpath is not None:
            raise WorkbenchError(
                "Pass only one of fixtures_root= (absolute) or fixtures_relpath= "
                "(project-relative), not both."
            )
        if scratch_root is not None and scratch_relpath is not None:
            raise WorkbenchError(
                "Pass only one of scratch_root= (absolute) or scratch_relpath= "
                "(project-relative), not both."
            )

        need_project_for_rel = (
            fixtures_root is None or scratch_root is None
            or fixtures_relpath is not None or scratch_relpath is not None
        )
        # Absolute-both escape: both absolute roots supplied, no relpaths
        absolute_both = (
            fixtures_root is not None
            and scratch_root is not None
            and fixtures_relpath is None
            and scratch_relpath is None
        )
        if not absolute_both and proj is None and need_project_for_rel:
            raise WorkbenchError(
                "No pyproject.toml found walking up from the current directory. "
                "cd into a project, pass project_root=, or pass absolute "
                "fixtures_root= and scratch_root= (constructor / for_project)."
            )

        if fixtures_root is not None:
            fx = Path(fixtures_root).resolve()
        else:
            rel = fixtures_relpath if fixtures_relpath is not None else "tests/case-fixtures"
            if proj is None:
                raise WorkbenchError(
                    "fixtures_relpath requires a project_root (pyproject.toml). "
                    "Pass absolute fixtures_root= instead."
                )
            fx = (proj / rel).resolve()

        if scratch_root is not None:
            sc = Path(scratch_root).resolve()
        else:
            rel = scratch_relpath if scratch_relpath is not None else "volatile/case-workbench/scratch"
            if proj is None:
                raise WorkbenchError(
                    "scratch_relpath requires a project_root (pyproject.toml). "
                    "Pass absolute scratch_root= instead."
                )
            sc = (proj / rel).resolve()

        ensure_dir(fx, label="fixtures_root")
        # Scratch default nests under volatile/case-workbench/ — create parents when
        # they sit under a known project_root; otherwise require the immediate parent.
        if not sc.exists():
            if not sc.parent.exists():
                if proj is not None:
                    try:
                        sc.parent.relative_to(proj.resolve())
                        sc.parent.mkdir(parents=True, exist_ok=True)
                    except ValueError:
                        ensure_dir(sc.parent, label="scratch parent")
                else:
                    ensure_dir(sc.parent, label="scratch parent")
            ensure_dir(sc, label="scratch_root")
        else:
            ensure_dir(sc, label="scratch_root")

        wb = cls(
            fixtures_root=fx,
            scratch_root=sc,
            project_root=proj,
            case_modules=case_modules,
            source_roots=source_roots,
            **kwargs,
        )
        if not quiet:
            report = wb.doctor()
            print(report)
        return wb

    def __enter__(self) -> CaseWorkbench:
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # --- path display ----------------------------------------------------

    def _display_path(self, path: Path, *, full: bool = False) -> str:
        path = Path(path).resolve()
        if full or self._project_root is None:
            return str(path)
        try:
            rel = path.relative_to(self._project_root)
            text = str(rel)
            if ".." in Path(text).parts:
                return str(path)
            return text
        except ValueError:
            return str(path)

    def _resolve_case(self, case: FolderBackedCase | None = None) -> FolderBackedCase:
        if case is not None:
            return case
        if self._case is None:
            raise WorkbenchError(
                "No focus case. clone(...) / create(...) a case first, or "
                "focus(...) an existing scratch folder."
            )
        return self._case

    def _log(self, case: FolderBackedCase, msg: str) -> None:
        line = f"[workbench] {msg}"
        if self._log_to_case and not case.case_is_detached:
            case.log.info(line)
        if self._echo:
            print(line)

    def _apply_keep(self, case: FolderBackedCase) -> None:
        if self._keep_assets:
            case.case_keep_files("**")

    def _adopt(self, case: FolderBackedCase) -> None:
        self._live[case.case_id] = case
        self._case = case

    # --- doctor / help / index -------------------------------------------

    def refresh_class_index(self) -> ListExamplesReport:
        mapping = self._class_index.refresh()
        return ListExamplesReport(
            narrative=f"Class index refreshed ({len(mapping)} entries).",
            examples=[(k, v) for k, v in sorted(mapping.items())],
        )

    def doctor(self) -> DoctorReport:
        findings: list[DoctorFinding] = []
        if self._project_root is None:
            findings.append(DoctorFinding(
                "info",
                "No project_root — path narratives use absolute paths.",
            ))
        else:
            findings.append(DoctorFinding(
                "info",
                f"Project root: {self._display_path(self._project_root)}",
            ))
        if not self._fixtures_root.is_dir():
            findings.append(DoctorFinding(
                "warning",
                f"fixtures_root missing: {self._display_path(self._fixtures_root)}",
            ))
        else:
            findings.append(DoctorFinding(
                "info",
                f"Fixtures shelf: {self._display_path(self._fixtures_root)}",
            ))
        if not self._scratch_root.is_dir():
            findings.append(DoctorFinding(
                "warning",
                f"scratch_root missing: {self._display_path(self._scratch_root)}",
            ))
        else:
            n = sum(1 for p in self._scratch_root.iterdir() if p.is_dir())
            if n:
                term = 0
                for p in self._scratch_root.iterdir():
                    if not p.is_dir():
                        continue
                    try:
                        rec = FolderBackedCase.peek_case_record(p)
                        if rec.terminal is not None:
                            term += 1
                    except Exception:
                        pass
                findings.append(DoctorFinding(
                    "info",
                    f"Scratch contains {n} case folder(s) from earlier sessions "
                    f"({term} terminal). wb.cases() to list, wb.focus('<name>') "
                    "to resume, wb.cleanup() to clear.",
                ))
            else:
                findings.append(DoctorFinding(
                    "info",
                    f"Scratch pool (empty): {self._display_path(self._scratch_root)}",
                ))

        # Fixture layout / class mismatches / loose assets
        for nick, desc, folder in self._iter_fixture_dirs():
            try:
                rec = FolderBackedCase.peek_case_record(folder)
                class_seg = nick.split("/")[0]
                if rec.case_object_type != class_seg:
                    findings.append(DoctorFinding(
                        "warning",
                        f"Fixture {nick}: class segment {class_seg!r} != "
                        f"case_record case_object_type {rec.case_object_type!r}.",
                    ))
            except Exception as exc:
                findings.append(DoctorFinding(
                    "warning", f"Fixture {nick}: cannot read case_record ({exc}).",
                ))
            wb_dir = folder / WORKBENCH_DIR_NAME
            if wb_dir.is_dir():
                for child in wb_dir.iterdir():
                    if child.is_file() and child.suffix.lower() in _ASSET_LIKE_SUFFIXES:
                        findings.append(DoctorFinding(
                            "warning",
                            f"Fixture {nick}: asset-like file loose in workbench/ "
                            f"({child.name}); prefer workbench/incoming_assets/<group>/.",
                        ))
            if not (folder / WORKBENCH_DIR_NAME / "fixture.yaml").is_file():
                findings.append(DoctorFinding(
                    "info",
                    f"Fixture {nick}: missing workbench/fixture.yaml "
                    f"(description empty{'; description' if not desc else ''}).",
                ))

        if not self._class_index._name_to_file:
            findings.append(DoctorFinding(
                "warning",
                "Class index is empty — pass case_modules= or refresh_class_index().",
            ))
        findings.append(DoctorFinding(
            "info",
            "wb.help() for the API catalog. Driving is async — use python -m asyncio, "
            "IPython, or marimo; or asyncio.run(wb.advance()) in a plain REPL.",
        ))
        lines = [f"[{f.level}] {f.message}" for f in findings]
        return DoctorReport(narrative="\n".join(lines), findings=findings)

    def help(self, name: str | None = None) -> HelpReport:
        if name is None:
            return HelpReport(narrative=self._help_catalog(), name=None)
        # unique prefix
        public = self._public_help_names()
        matches = [n for n in public if n.startswith(name)]
        if len(matches) == 1:
            return HelpReport(narrative=self._help_detail(matches[0]), name=matches[0])
        if name in public:
            return HelpReport(narrative=self._help_detail(name), name=name)
        if matches:
            return HelpReport(
                narrative=f"Ambiguous prefix {name!r}. Did you mean: {', '.join(sorted(matches))}?",
                name=name,
            )
        return HelpReport(
            narrative=f"No help topic matching {name!r}. Try wb.help().",
            name=name,
        )

    def _public_help_names(self) -> list[str]:
        names: list[str] = []
        for attr, obj in inspect.getmembers(type(self)):
            if attr.startswith("_") or attr in self._HELP_EXCLUDE:
                continue
            if inspect.isroutine(obj) or isinstance(obj, (property, classmethod)):
                names.append(attr)
            elif isinstance(getattr(type(self), attr, None), property):
                names.append(attr)
        # de-dupe preserving order
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def _help_catalog(self) -> str:
        names = self._public_help_names()
        mapped: set[str] = set()
        lines = [
            "CaseWorkbench API. Driving is async — use python -m asyncio, IPython, "
            "or marimo; or asyncio.run(wb.advance()) in a plain REPL.",
            "",
        ]
        for section, members in _HELP_SECTIONS:
            present = [n for n in names if n in members]
            if not present:
                continue
            mapped.update(present)
            lines.append(f"## {section}")
            for n in sorted(present):
                doc = inspect.getdoc(getattr(type(self), n)) or ""
                first = doc.splitlines()[0] if doc else ""
                lines.append(f"  {n} — {first}" if first else f"  {n}")
            lines.append("")
        other = [n for n in names if n not in mapped]
        if other:
            lines.append("## Other")
            for n in sorted(other):
                lines.append(f"  {n}")
        return "\n".join(lines).rstrip()

    def _help_detail(self, name: str) -> str:
        obj = getattr(type(self), name)
        if isinstance(obj, property):
            target = obj.fget
        elif isinstance(obj, classmethod):
            target = obj.__func__
        else:
            target = obj
        sig = ""
        try:
            sig = str(inspect.signature(target)) if target else ""
        except (TypeError, ValueError):
            pass
        doc = inspect.getdoc(obj) or inspect.getdoc(target) or "(no docstring)"
        async_note = ""
        if target and inspect.iscoroutinefunction(target):
            async_note = "\n(async — await this method)\n"
        return f"{name}{sig}{async_note}\n\n{doc}"

    # --- fixtures shelf --------------------------------------------------

    def _iter_fixture_dirs(self):
        root = self._fixtures_root
        if not root.is_dir():
            return
        for record in root.rglob(RECORD_NAME):
            folder = record.parent
            try:
                rel = folder.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) != 3:
                continue
            nick = "/".join(parts)
            desc = ""
            fx = folder / WORKBENCH_DIR_NAME / "fixture.yaml"
            if fx.is_file():
                try:
                    data = yaml.safe_load(fx.read_text(encoding="utf-8")) or {}
                    desc = str(data.get("description") or "")
                except (OSError, yaml.YAMLError):
                    pass
            yield nick, desc, folder

    def list_examples(self, prefix: str | None = None) -> ListExamplesReport:
        """List freeze-dried fixture nicknames (optional prefix filter)."""
        examples: list[tuple[str, str]] = []
        for nick, desc, folder in self._iter_fixture_dirs():
            if prefix and not nick.startswith(prefix):
                continue
            class_seg = nick.split("/")[0]
            try:
                rec = FolderBackedCase.peek_case_record(folder)
                if rec.case_object_type != class_seg:
                    # pedagogical warning via narrative note, not raise
                    desc = (desc + " " if desc else "") + (
                        f"[warn: class segment != {rec.case_object_type}]"
                    )
            except Exception:
                pass
            examples.append((nick, desc))
        examples.sort()
        if not examples:
            narrative = "No fixtures found" + (f" under prefix {prefix!r}." if prefix else ".")
        else:
            lines = [f"{n}  {d}" if d else n for n, d in examples]
            narrative = "\n".join(lines)
        return ListExamplesReport(narrative=narrative, examples=examples)

    # --- scratch listing / focus -----------------------------------------

    def cases(self, pattern: str | None = None, full: bool = False) -> CasesReport:
        """List scratch cases (glob-filterable); lock-free peeks only."""
        rows: list[dict[str, Any]] = []
        if not self._scratch_root.is_dir():
            return CasesReport(narrative="Scratch pool is empty / missing.", rows=[])
        for folder in sorted(self._scratch_root.iterdir()):
            if not folder.is_dir():
                continue
            name = folder.name
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            label = "—"
            cls_name = "?"
            state = "?"
            terminal = False
            try:
                rec = FolderBackedCase.peek_case_record(folder)
                cls_name = rec.case_object_type
                if rec.nickname:
                    label = rec.nickname
                terminal = rec.terminal is not None
                # state: prefer journal peek
                try:
                    journal = FolderBackedCase.peek_case_event_journal(folder)
                    state = journal.current_state or "?"
                except Exception:
                    state = rec.terminal_state or "?"
            except Exception:
                pass
            if label == "—":
                prov = folder / WORKBENCH_DIR_NAME / "provenance.yaml"
                if prov.is_file():
                    try:
                        data = yaml.safe_load(prov.read_text(encoding="utf-8")) or {}
                        if data.get("original_fixture_nickname"):
                            label = str(data["original_fixture_nickname"])
                    except (OSError, yaml.YAMLError):
                        pass
            rows.append({
                "id": name,
                "label": label,
                "class": cls_name,
                "state": state,
                "terminal": terminal,
                "path": folder.resolve(),
            })
        lines = ["id  label  class  state  terminal  path"]
        for r in rows:
            mark = "T" if r["terminal"] else ""
            lines.append(
                f"{r['id']}  {r['label']}  {r['class']}  {r['state']}  {mark}  "
                f"{self._display_path(r['path'], full=full)}"
            )
        return CasesReport(
            narrative="\n".join(lines) if rows else "No scratch cases.",
            rows=rows,
        )

    def focus(self, case_or_name: FolderBackedCase | str) -> FocusReport:
        """Make a case the focus (live object or scratch name/glob)."""
        if isinstance(case_or_name, FolderBackedCase):
            case = case_or_name
            self._adopt(case)
            if self._keep_assets and not case.case_is_detached:
                self._apply_keep(case)
            return FocusReport(
                narrative=f"Focus → {case.case_id} ({case.__class__.__name__} @ {case.case_state})",
                case=case,
            )
        folder = self._resolve_scratch_name(case_or_name)
        rec = FolderBackedCase.peek_case_record(folder)
        self._class_index.ensure_registered(rec.case_object_type, case_type_registry)
        case = case_type_registry.rehydrate(folder)
        self._apply_keep(case)
        self._adopt(case)
        self._log(case, f"focused scratch case {case.case_id}")
        return FocusReport(
            narrative=f"Focus → {case.case_id} ({case.__class__.__name__} @ {case.case_state})",
            case=case,
        )

    def _resolve_scratch_name(self, name_or_glob: str) -> Path:
        if "/" in name_or_glob:
            # might be absolute-ish mistake
            raise WorkbenchError(
                f"{name_or_glob!r} looks like a fixture nickname (contains '/'). "
                "Use clone(...) for fixtures; focus() takes a scratch id/glob."
            )
        matches = [
            p for p in self._scratch_root.iterdir()
            if p.is_dir() and fnmatch.fnmatch(p.name, name_or_glob)
        ] if self._scratch_root.is_dir() else []
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise WorkbenchError(
                f"No scratch case matching {name_or_glob!r} under "
                f"{self._display_path(self._scratch_root)}. wb.cases() to list."
            )
        names = ", ".join(sorted(p.name for p in matches))
        raise WorkbenchError(
            f"Ambiguous scratch pattern {name_or_glob!r} matches: {names}. "
            "Narrow the glob."
        )

    def cleanup(self) -> StatusReport:
        """Detach every live registry case, then clear the scratch pool."""
        for case in list(self._live.values()):
            try:
                if not case.case_is_detached:
                    case.case_detach()
            except Exception:
                pass
        self._live.clear()
        self._case = None
        self._replace_map.clear()
        if self._scratch_root.is_dir():
            for child in list(self._scratch_root.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        return StatusReport(narrative="Cleanup complete: live registry detached; scratch cleared.")

    # --- create / clone / freeze_dry -------------------------------------

    def create(
        self,
        case_cls: type | str,
        *,
        nickname: str | None = None,
        **fields,
    ) -> CreateReport:
        """Construct a fresh case in the scratch pool.

        *case_cls* may be a ``FolderBackedCase`` subclass or a class-name string
        resolved through the workbench class index (same discovery path as
        ``clone`` / ``focus``). Call ``refresh_class_index()`` if a newly added
        module is not found yet.
        """
        if isinstance(case_cls, str):
            case_cls = self._class_index.ensure_registered(case_cls, case_type_registry)
        if not isinstance(case_cls, type) or not issubclass(case_cls, FolderBackedCase):
            raise WorkbenchError(
                f"create() expects a FolderBackedCase subclass or class-name "
                f"string; got {case_cls!r}."
            )
        case_type_registry.register_case_types(case_cls)
        stem = _slugify(nickname) if nickname else _slugify(case_cls.__name__)
        case_id = f"{stem}--{_utc_stamp()}--{_rand4()}"
        dest = self._scratch_root / case_id
        if dest.exists():
            raise WorkbenchError(f"Scratch folder already exists: {dest.name}. Retry create().")
        dest.mkdir(parents=True, exist_ok=False)
        case = case_cls.create_case_in_folder(
            dest, case_id=case_id, nickname=nickname, **fields,
        )
        self._apply_keep(case)
        self._log(case, f"created {case_cls.__name__} as {case_id}")
        self._adopt(case)
        return CreateReport(
            narrative=f"Created {case_cls.__name__} → {case_id}",
            case=case,
        )

    def clone(
        self,
        source: Any = None,
        *,
        keep_identity: bool = False,
        case_id: str | None = None,
        parallel: bool = False,
    ) -> CloneReport:
        """Clone a fixture, scratch case, absolute path, live case, or focus into scratch.

        Absolute filesystem paths (``str`` or ``Path``) copy any case folder into
        the scratch pool — prefer this over focusing an in-place folder. Relative
        ``Class/group/sample`` strings remain fixture nicknames.
        """
        if keep_identity and case_id is not None:
            raise WorkbenchError(
                "Pass keep_identity=True or case_id=..., not both."
            )

        src_folder, meta = self._resolve_clone_source(source)
        fixture_nick = meta.get("fixture_nickname")
        source_path = meta.get("source_path")
        original_id = meta.get("original_case_id")
        chained_nick = meta.get("original_fixture_nickname")

        # Replace semantics for fixture nicknames
        if fixture_nick and not parallel and fixture_nick in self._replace_map:
            old_id = self._replace_map[fixture_nick]
            old = self._live.pop(old_id, None)
            if old is not None and not old.case_is_detached:
                try:
                    old.case_detach()
                except Exception:
                    pass
            old_folder = self._scratch_root / old_id
            if old_folder.is_dir():
                shutil.rmtree(old_folder, ignore_errors=True)
            if self._case is not None and self._case.case_id == old_id:
                self._case = None

        if keep_identity:
            new_id = original_id or Path(src_folder).name
        elif case_id is not None:
            new_id = case_id
        else:
            slug_src = chained_nick or fixture_nick or meta.get("class_name") or "case"
            new_id = f"{_slugify(slug_src)}--{_utc_stamp()}--{_rand4()}"

        dest = self._scratch_root / new_id
        if dest.exists():
            raise WorkbenchError(
                f"Scratch folder {new_id!r} already exists. Choose another case_id, "
                "cleanup(), or use a different mint."
            )

        shutil.copytree(src_folder, dest)
        lease = dest / LEASE_NAME
        if lease.exists():
            lease.unlink()

        # Remint case_id in YAML before open (raw rewrite — no first-class remint API)
        record_path = dest / RECORD_NAME
        data = yaml.safe_load(record_path.read_text(encoding="utf-8")) or {}
        if not keep_identity:
            data["case_id"] = new_id
            record_path.write_text(
                yaml.safe_dump(data, sort_keys=False), encoding="utf-8",
            )
        class_name = data.get("case_object_type") or meta.get("class_name")

        # Provenance
        if fixture_nick:
            immediate = f"fixture:{fixture_nick}"
        elif source_path:
            immediate = f"path:{source_path}"
        else:
            immediate = f"scratch:{original_id or src_folder.name}"
        prov = {
            "immediate_source": immediate,
            "original_case_id": original_id or src_folder.name,
            "original_fixture_nickname": chained_nick or fixture_nick,
            "cloned_at": _utc_z(),
        }
        if source_path:
            prov["source_path"] = str(source_path)
        wb_dir = dest / WORKBENCH_DIR_NAME
        wb_dir.mkdir(exist_ok=True)
        (wb_dir / "provenance.yaml").write_text(
            yaml.safe_dump(prov, sort_keys=False), encoding="utf-8",
        )

        if not class_name:
            raise WorkbenchError(
                f"Clone destination missing case_object_type in {record_path}."
            )
        self._class_index.ensure_registered(class_name, case_type_registry)
        case = case_type_registry.rehydrate(dest)
        self._apply_keep(case)
        self._log(case, f"cloned from {immediate} → {case.case_id}")
        self._adopt(case)
        if fixture_nick and not parallel:
            self._replace_map[fixture_nick] = case.case_id
        return CloneReport(
            narrative=f"Cloned {immediate} → {case.case_id}",
            case=case,
        )

    def _resolve_clone_source(self, source: Any) -> tuple[Path, dict]:
        meta: dict[str, Any] = {}
        if source is None:
            if self._case is None:
                raise WorkbenchError(
                    "clone() with no source needs a focus case. "
                    "clone('Class/group/sample'), clone(/abs/path), or create(...) first."
                )
            source = self._case

        if isinstance(source, FolderBackedCase):
            folder = Path(source.case_folder).resolve()
            meta["original_case_id"] = source.case_id
            meta["class_name"] = source.__class__.__name__
            meta.update(self._read_provenance_chain(folder))
            return folder, meta

        if isinstance(source, Path):
            return self._resolve_absolute_case_folder(source)

        if not isinstance(source, str):
            raise WorkbenchError(
                f"clone source must be a fixture nickname, absolute path, "
                f"scratch id/glob, case object, or None; got {type(source).__name__}."
            )

        as_path = Path(source)
        if as_path.is_absolute():
            return self._resolve_absolute_case_folder(as_path)

        if "/" in source:
            parts = source.split("/")
            if len(parts) != 3:
                raise WorkbenchError(
                    f"Fixture nickname must be exactly three segments "
                    f"Class/group/sample; got {source!r}."
                )
            folder = self._fixtures_root / parts[0] / parts[1] / parts[2]
            if not (folder / RECORD_NAME).is_file():
                raise WorkbenchError(
                    f"No fixture at {self._display_path(folder)}. "
                    "wb.list_examples() to browse."
                )
            try:
                rec = FolderBackedCase.peek_case_record(folder)
                if rec.case_object_type != parts[0]:
                    # pedagogical warning only — still clone
                    pass
                meta["original_case_id"] = rec.case_id
                meta["class_name"] = rec.case_object_type
            except Exception:
                pass
            meta["fixture_nickname"] = source
            meta["original_fixture_nickname"] = source
            return folder.resolve(), meta

        # scratch id / glob
        folder = self._resolve_scratch_name(source)
        try:
            rec = FolderBackedCase.peek_case_record(folder)
            meta["original_case_id"] = rec.case_id
            meta["class_name"] = rec.case_object_type
        except Exception:
            meta["original_case_id"] = folder.name
        meta.update(self._read_provenance_chain(folder))
        return folder.resolve(), meta

    def _resolve_absolute_case_folder(self, folder: Path) -> tuple[Path, dict]:
        """Resolve an absolute filesystem path to a case folder for clone()."""
        folder = Path(folder)
        if not folder.is_absolute():
            raise WorkbenchError(
                f"clone(path) requires an absolute path; got relative {folder!s}. "
                "Use Class/group/sample for fixtures, or Path(...).resolve()."
            )
        folder = folder.resolve()
        if not (folder / RECORD_NAME).is_file():
            raise WorkbenchError(
                f"No case_record.yaml at {self._display_path(folder, full=True)}. "
                "clone(path) expects an absolute path to a case folder."
            )
        meta: dict[str, Any] = {
            "source_path": str(folder),
            "original_case_id": folder.name,
        }
        try:
            rec = FolderBackedCase.peek_case_record(folder)
            meta["original_case_id"] = rec.case_id
            meta["class_name"] = rec.case_object_type
        except Exception:
            pass
        meta.update(self._read_provenance_chain(folder))
        return folder, meta

    def _read_provenance_chain(self, folder: Path) -> dict:
        prov_path = folder / WORKBENCH_DIR_NAME / "provenance.yaml"
        out: dict[str, Any] = {}
        if not prov_path.is_file():
            return out
        try:
            data = yaml.safe_load(prov_path.read_text(encoding="utf-8")) or {}
            if data.get("original_fixture_nickname"):
                out["original_fixture_nickname"] = data["original_fixture_nickname"]
        except (OSError, yaml.YAMLError):
            pass
        return out

    def freeze_dry(
        self,
        case: FolderBackedCase | None = None,
        nickname: str | None = None,
        description: str = "",
        overwrite: bool = False,
        *,
        dest: Path | str | None = None,
    ) -> FreezeDryReport:
        """Copy focus (or *case*) onto the fixtures shelf or an absolute path.

        Pass *nickname* (``group/sample`` or ``Class/group/sample``) for the
        project fixtures shelf, or absolute *dest* for an arbitrary snapshot
        directory. A pre-existing **empty** destination is reused; a
        non-empty destination raises unless ``overwrite=True`` (which purges
        it first). Absolute *dest* snapshots omit shelf ``fixture.yaml``.
        """
        case = self._resolve_case(case)
        if nickname is not None and dest is not None:
            raise WorkbenchError(
                "Pass nickname=... (fixtures shelf) or dest=... (absolute path), "
                "not both."
            )
        if nickname is None and dest is None:
            raise WorkbenchError(
                "freeze_dry requires nickname='group/sample' (or "
                "'Class/group/sample') or absolute dest=..."
            )

        full_nick: str | None = None
        write_fixture_meta = False
        if dest is not None:
            dest_path = Path(dest)
            if not dest_path.is_absolute():
                raise WorkbenchError(
                    f"freeze_dry(dest=...) requires an absolute path; got "
                    f"{dest_path!s}. Use nickname= for the fixtures shelf."
                )
            dest_path = dest_path.resolve()
        else:
            parts = nickname.strip("/").split("/")
            class_name = case.__class__.__name__
            if len(parts) == 2:
                group, sample = parts
                full_nick = f"{class_name}/{group}/{sample}"
            elif len(parts) == 3:
                if parts[0] != class_name:
                    raise WorkbenchError(
                        f"Nickname class segment {parts[0]!r} != case class "
                        f"{class_name!r}."
                    )
                full_nick = "/".join(parts)
                group, sample = parts[1], parts[2]
            else:
                raise WorkbenchError(
                    f"nickname must be group/sample or Class/group/sample; "
                    f"got {nickname!r}."
                )
            dest_path = self._fixtures_root / class_name / group / sample
            write_fixture_meta = True

        self._prepare_freeze_dest(dest_path, overwrite=overwrite)

        def _ignore(dirpath, names):
            drop = set()
            base = Path(dirpath)
            rel = base.relative_to(case.case_folder) if base != case.case_folder else Path(".")
            for n in names:
                if n == LEASE_NAME:
                    drop.add(n)
                if n == LOGS_DIR_NAME and rel == Path("."):
                    drop.add(n)
                if n == "provenance.yaml" and rel == Path(WORKBENCH_DIR_NAME):
                    drop.add(n)
            return drop

        shutil.copytree(case.case_folder, dest_path, ignore=_ignore)
        # Strip lease if copied
        lease = dest_path / LEASE_NAME
        if lease.exists():
            lease.unlink()
        if write_fixture_meta:
            wb_dir = dest_path / WORKBENCH_DIR_NAME
            wb_dir.mkdir(exist_ok=True)
            fixture_yaml = {
                "nickname": full_nick,
                "description": description,
                "case_class": case.__class__.__name__,
                "state_at_freeze": case.case_state,
                "frozen_at": _utc_z(),
            }
            (wb_dir / "fixture.yaml").write_text(
                yaml.safe_dump(fixture_yaml, sort_keys=False), encoding="utf-8",
            )
            self._log(case, f"freeze_dry → {full_nick}")
            narrative = f"Freeze-dried → {full_nick}\n{self._display_path(dest_path)}"
        else:
            self._log(case, f"freeze_dry → path:{dest_path}")
            narrative = (
                f"Freeze-dried → path:{dest_path}\n"
                f"{self._display_path(dest_path, full=True)}"
            )
        return FreezeDryReport(
            narrative=narrative,
            nickname=full_nick,
            dest=dest_path.resolve(),
        )

    def _prepare_freeze_dest(self, dest: Path, *, overwrite: bool) -> None:
        """Ensure *dest* is absent so copytree can create it.

        Empty directories are removed quietly. Non-empty destinations require
        ``overwrite=True`` (purge then proceed).
        """
        if dest.exists() and dest.is_file():
            raise WorkbenchError(
                f"freeze_dry destination is a file, not a folder: "
                f"{self._display_path(dest, full=True)}."
            )
        if dest.exists() and dest.is_dir() and _folder_has_contents(dest):
            if not overwrite:
                raise WorkbenchError(
                    f"Destination is not empty: {self._display_path(dest)}. "
                    "Pass overwrite=True to replace."
                )
            shutil.rmtree(dest)
        elif dest.exists() and dest.is_dir():
            dest.rmdir()
        dest.parent.mkdir(parents=True, exist_ok=True)

    # --- inspect ---------------------------------------------------------

    def path(self, case: FolderBackedCase | None = None, full: bool = False) -> PathReport:
        case = self._resolve_case(case)
        folder = Path(case.case_folder).resolve()
        return PathReport(
            narrative=self._display_path(folder, full=full),
            folder=folder,
        )

    def tree(
        self,
        case: FolderBackedCase | None = None,
        subpath: str | None = "assets",
        full: bool = False,
    ) -> TreeReport:
        case = self._resolve_case(case)
        root = Path(case.case_folder).resolve()
        if subpath is None:
            target = root
        else:
            target = (root / subpath).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise WorkbenchError(f"subpath escapes case folder: {subpath!r}") from None
        if not target.exists():
            return TreeReport(
                narrative=f"(empty — {self._display_path(target, full=full)} missing)",
                root=target,
                entries=[],
            )
        entries: list[str] = []
        for p in sorted(target.rglob("*")):
            if p.is_file():
                try:
                    rel = p.relative_to(target)
                except ValueError:
                    continue
                entries.append(str(rel))
        body = "\n".join(entries) if entries else "(empty)"
        header = self._display_path(target, full=full)
        return TreeReport(narrative=f"{header}\n{body}", root=target, entries=entries)

    def head(
        self,
        name: str,
        n: int = 10,
        case: FolderBackedCase | None = None,
    ) -> HeadReport:
        case = self._resolve_case(case)
        root = Path(case.case_folder)
        # alias first
        target: Path | None = None
        try:
            aliases = case.case_record().asset_aliases or {}
            if name in aliases:
                rel = aliases[name].get("path")
                if rel:
                    # may be glob — take first match
                    matches = list((root / "assets").glob(rel)) if not Path(rel).is_absolute() else [Path(rel)]
                    # also try relative to case root
                    if not matches:
                        matches = list(root.glob(rel))
                    if matches:
                        target = matches[0]
        except Exception:
            pass
        if target is None:
            candidate = root / name
            if candidate.is_file():
                target = candidate
        if target is None or not target.is_file():
            raise WorkbenchError(
                f"head({name!r}): no asset alias or case-relative file found. "
                "Pass an alias or a path relative to the case folder."
            )
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise WorkbenchError(f"Cannot read {target}: {exc}") from exc
        # binary heuristic
        if b"\x00" in raw[:1024]:
            return HeadReport(
                narrative=f"binary, {len(raw)} bytes ({target.name})",
                path=target.resolve(),
                binary_bytes=len(raw),
            )
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()[:n]
        return HeadReport(
            narrative="\n".join(lines),
            path=target.resolve(),
            text="\n".join(lines),
        )

    def incoming_assets(
        self,
        group: str | None = None,
        case: FolderBackedCase | None = None,
        full: bool = False,
    ) -> IncomingAssetsReport:
        """List provisioned inputs under workbench/incoming_assets/ (never copies into assets/)."""
        case = self._resolve_case(case)
        base = Path(case.case_folder) / WORKBENCH_DIR_NAME / "incoming_assets"
        if group is None:
            if not base.is_dir():
                return IncomingAssetsReport(
                    narrative="No incoming_assets/ groups.",
                    paths=[],
                )
            lines = []
            for g in sorted(p for p in base.iterdir() if p.is_dir()):
                count = sum(1 for _ in g.rglob("*") if _.is_file())
                lines.append(f"{g.name}/  ({count} files)")
            return IncomingAssetsReport(
                narrative="\n".join(lines) if lines else "No incoming_assets/ groups.",
                paths=[],
            )
        folder = base / group
        if not folder.is_dir():
            return IncomingAssetsReport(
                narrative=f"No incoming_assets/{group}/.",
                paths=[],
                group=group,
            )
        paths = sorted(p.resolve() for p in folder.rglob("*") if p.is_file())
        lines = [self._display_path(p, full=full) for p in paths]
        return IncomingAssetsReport(
            narrative="\n".join(lines) if lines else f"(empty group {group})",
            paths=paths,
            group=group,
        )

    def status(self, case: FolderBackedCase | None = None) -> StatusReport:
        case = self._resolve_case(case)
        problems = self.problems(case)
        counts = {
            "alerts": len(problems.alerts),
            "assert_failures": len(problems.assert_failures),
            "transition_failures": len(problems.transition_failures),
            "entry_exceptions": len(problems.entry_exceptions),
            "trigger_timeouts": len(problems.trigger_timeouts),
        }
        dwell = None
        try:
            dwell = case.case_dwell_secs()
        except Exception:
            pass
        narrative = (
            f"state={case.case_state}  dwell={dwell!s}  "
            f"terminal={case.case_is_terminal}  advanceable={case.case_is_advanceable}\n"
            f"problems: {counts}"
        )
        return StatusReport(
            narrative=narrative,
            state=case.case_state,
            dwell_secs=dwell,
            terminal=case.case_is_terminal,
            advanceable=case.case_is_advanceable,
            problem_counts=counts,
        )

    def probe(self, case: FolderBackedCase | None = None) -> ProbeReport:
        """List auto/manual edges from the current state with bound-method guard verdicts.

        Relies on the guard-purity contract on FolderBackedCaseInterface: guards are
        side-effect-free predicates and may be called repeatedly for inspection.
        """
        case = self._resolve_case(case)
        fsm = case.case_type_spec().fsm
        state = case.case_state
        edges: list[ProbeEdge] = []

        auto = fsm.auto_edges_from(state)
        auto_triggers = {t for t, _ in auto}

        def _eval_guards(trigger: str, dest: str, kind: str) -> ProbeEdge:
            guards: list[tuple[str, Any]] = []
            # find matching transition dict(s)
            for td in fsm.transitions:
                src = td.get("source")
                sources = src if isinstance(src, (list, tuple)) else [src]
                if state not in sources and src != "*":
                    continue
                if td.get("trigger") != trigger:
                    continue
                if td.get("dest") != dest:
                    continue
                for item in td.get("_guards") or []:
                    if _is_method_guard(item):
                        gname = item
                        meth = getattr(case, gname, None)
                        if meth is None:
                            guards.append((gname, "error"))
                            continue
                        try:
                            if inspect.iscoroutinefunction(meth):
                                try:
                                    asyncio.get_running_loop()
                                except RuntimeError:
                                    verdict = asyncio.run(meth(None))
                                    guards.append((gname, bool(verdict)))
                                else:
                                    # Sync probe inside a running loop cannot await safely
                                    guards.append((gname, "error"))
                            else:
                                try:
                                    result = meth()
                                except TypeError:
                                    result = meth(None)
                                if inspect.iscoroutine(result):
                                    result.close()
                                    guards.append((gname, "error"))
                                else:
                                    guards.append((gname, bool(result)))
                        except Exception as exc:
                            guards.append((gname, f"error:{exc}"))
                    else:
                        guards.append((_fmt_fact_guard(item), "fact"))
            chokes = sorted(fsm.trigger_chokes.get(trigger, frozenset()))
            return ProbeEdge(
                trigger=trigger, dest=dest, kind=kind,
                guards=guards, chokes=chokes,
            )

        for trigger, dest in auto:
            edges.append(_eval_guards(trigger, dest, "auto"))

        # manual edges
        seen_manual: set[tuple[str, str]] = set()
        for td in fsm.transitions:
            src = td.get("source")
            sources = src if isinstance(src, (list, tuple)) else [src]
            if state not in sources and src != "*":
                continue
            trigger = td.get("trigger")
            dest = td.get("dest")
            if trigger is None or dest is None:
                continue
            if fsm.is_auto(state, trigger):
                continue
            if trigger in auto_triggers:
                continue
            key = (trigger, dest)
            if key in seen_manual:
                continue
            seen_manual.add(key)
            edges.append(_eval_guards(trigger, dest, "manual"))

        lines = [f"state={state}"]
        for e in edges:
            gtxt = ", ".join(f"{n}={v}" for n, v in e.guards) or "—"
            ctxt = f" chokes={e.chokes}" if e.chokes else ""
            lines.append(f"  [{e.kind}] {e.trigger} → {e.dest}  guards=[{gtxt}]{ctxt}")
        return ProbeReport(narrative="\n".join(lines), edges=edges)

    def problems(self, case: FolderBackedCase | None = None) -> ProblemsReport:
        case = self._resolve_case(case)
        journal = case.case_event_journal
        prim = journal.primitive

        def _collect(label: str) -> list:
            return list(prim.events(label_glob=label, recent_first=True))

        alerts = _collect(EV_ALERTED)
        asserts = list(journal.assert_failures())
        transitions = _collect(EV_TRANSITION_FAILED)
        entries = _collect(EV_ENTRY_EXCEPTION)
        timeouts = _collect(EV_TRIGGER_TIMED_OUT)
        lines = [
            f"alerts: {len(alerts)}",
            f"assert_failures: {len(asserts)}",
            f"transition_failures: {len(transitions)}",
            f"entry_exceptions: {len(entries)}",
            f"trigger_timeouts: {len(timeouts)}",
        ]
        return ProblemsReport(
            narrative="\n".join(lines),
            alerts=alerts,
            assert_failures=asserts,
            transition_failures=transitions,
            entry_exceptions=entries,
            trigger_timeouts=timeouts,
        )

    def _problem_fingerprint(self, case: FolderBackedCase) -> frozenset[str]:
        p = self.problems(case)
        keys: set[str] = set()
        for label, items in (
            ("alert", p.alerts),
            ("assert", p.assert_failures),
            ("transition", p.transition_failures),
            ("entry", p.entry_exceptions),
            ("timeout", p.trigger_timeouts),
        ):
            for ev in items:
                keys.add(f"{label}:{getattr(ev, 'id', None) or id(ev)}")
        return frozenset(keys)

    def history(self, case: FolderBackedCase | None = None) -> HistoryReport:
        case = self._resolve_case(case)
        journal = case.case_event_journal
        transitions = journal.transitions()
        prim = journal.primitive
        started = list(prim.events(label_glob=EV_TRIGGER_STARTED, recent_first=False))
        entered = list(prim.events(label_glob=EV_STATE_ENTERED, recent_first=False))

        steps: list[HistoryStep] = []
        lines = ["#  from       → to         trigger            duration"]
        for i, tr in enumerate(transitions, start=1):
            duration_ms: int | None = None
            # Match START whose trigger matches, immediately before this ENTERED
            if tr.trigger and i <= len(entered):
                ent = entered[i - 1]
                # find latest START before this ENTERED with matching trigger value
                for st in reversed(started):
                    if st.mtime <= ent.mtime and (
                        st.value == tr.trigger or st.contents().as_dict().get("trigger") == tr.trigger
                        or str(st.value) == tr.trigger
                    ):
                        # ensure no earlier matching for previous steps — use first match walking back
                        delta = (ent.mtime - st.mtime).total_seconds()
                        duration_ms = int(delta * 1000)
                        break
            steps.append(HistoryStep(
                index=i,
                from_state=tr.from_state,
                to_state=tr.to_state,
                trigger=tr.trigger,
                duration_ms=duration_ms,
            ))
            fr = tr.from_state or "(inception)"
            trig = tr.trigger or "—"
            dur = f"{duration_ms} ms" if duration_ms is not None else "—"
            lines.append(f"{i}  {fr} → {tr.to_state}   {trig}            {dur}")
        return HistoryReport(narrative="\n".join(lines), steps=steps)

    # --- drive -----------------------------------------------------------

    async def advance(
        self,
        case: FolderBackedCase | None = None,
        trigger: str | None = None,
        trigger_kwargs: dict | None = None,
    ) -> AdvanceReport:
        """Thin wrap of case_advance with narrative + problem deltas."""
        case = self._resolve_case(case)
        before = self._problem_fingerprint(case)
        result = await case.case_advance(trigger=trigger, trigger_kwargs=trigger_kwargs)
        after = self._problem_fingerprint(case)
        new = sorted(after - before)
        if result.progressed:
            body = (
                f"Trigger {result.trigger!r} fired: "
                f"{result.initial_state} → {result.final_state}."
            )
        elif result.blocked:
            body = f"Advance blocked in {result.initial_state} (auto edge declined)."
        elif result.failed:
            body = f"Advance failed in {result.initial_state}: {result.exceptions!r}."
        else:
            body = f"No progress from {result.initial_state}."
        if new:
            body += f"\nNew this step: {', '.join(new)}"
        return AdvanceReport(narrative=body, result=result, new_problems=new)

    async def trigger(
        self,
        name: str,
        case: FolderBackedCase | None = None,
        **kwargs,
    ) -> AdvanceReport:
        """Fire await case.<name>(**kwargs); narrative + deltas."""
        case = self._resolve_case(case)
        before = self._problem_fingerprint(case)
        meth = getattr(case, name, None)
        if meth is None or not callable(meth):
            raise WorkbenchError(
                f"Case {case.__class__.__name__} has no trigger method {name!r}."
            )
        initial = case.case_state
        await meth(**kwargs)
        final = case.case_state
        after = self._problem_fingerprint(case)
        new = sorted(after - before)
        body = f"Trigger {name!r} fired: {initial} → {final}."
        if new:
            body += f"\nNew this step: {', '.join(new)}"
        return AdvanceReport(narrative=body, result=None, new_problems=new)

    async def run(
        self,
        case: FolderBackedCase | None = None,
        stop_before: str | None = None,
        max_steps: int = 50,
        stop_on_problems: bool = False,
    ) -> RunReport:
        """Drive AUTO edges until terminal / no progress / max_steps / stop_before / problems."""
        case = self._resolve_case(case)
        before = self._problem_fingerprint(case)
        steps: list[Any] = []
        cumulative_new: list[str] = []
        while case.case_is_live and len(steps) < max_steps:
            candidates = case.case_type_spec().fsm.auto_edges_from(case.case_state)
            if not candidates:
                break
            if stop_before and any(dest == stop_before for _, dest in candidates):
                break
            step_before = self._problem_fingerprint(case)
            result = await case.case_advance()
            step_after = self._problem_fingerprint(case)
            step_new = sorted(step_after - step_before)
            steps.append(result)
            cumulative_new.extend(step_new)
            if stop_on_problems and step_new:
                break
            if not result.progressed:
                break
        after = self._problem_fingerprint(case)
        all_new = sorted(after - before)
        last = steps[-1] if steps else None
        body = f"Ran {len(steps)} step(s)."
        if last is not None:
            body += f" Last: {last.initial_state} → {last.final_state}."
        body += f" Now at {case.case_state}."
        if all_new:
            body += f"\nNew this run: {', '.join(all_new)}"
        return RunReport(narrative=body, steps=steps, new_problems=all_new)
