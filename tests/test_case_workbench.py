# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for CaseWorkbench (totodev_pub.case_testing)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from totodev_pub.case_testing import CaseWorkbench, WorkbenchError
from totodev_pub.case_testing.results import WorkbenchReport
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import (
    CASE_RESERVED_ARTIFACT_NAMES,
    WORKBENCH_DIR_NAME,
)


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@case_type_registry.register
class WbTicketCase(FolderBackedCase):
    asset_aliases = []
    fsm_trigger_chokes = {}
    fsm_state_chains = [
        "^created--triage-->triaged--price-->priced==approve-->approved^",
        "priced==reject-->rejected^",
    ]

    async def perform_triage(self, tctx):
        pass

    async def perform_price(self, tctx):
        pass

    async def perform_approve(self, tctx):
        pass

    async def perform_reject(self, tctx):
        pass


@case_type_registry.register
class WbGuardedCase(FolderBackedCase):
    asset_aliases = []
    fsm_trigger_chokes = {"go": frozenset({"slot_a"})}
    fsm_state_chains = ["^start--ready#go-->done^"]

    async def guard_ready(self, tctx):
        return True

    async def perform_go(self, tctx):
        pass


def _wb(tmp_path: Path, **kwargs) -> CaseWorkbench:
    fx = tmp_path / "fixtures"
    sc = tmp_path / "scratch"
    fx.mkdir()
    sc.mkdir()
    return CaseWorkbench(
        fixtures_root=fx,
        scratch_root=sc,
        project_root=tmp_path,
        case_modules=[],
        source_roots=[],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Core reservation + report base
# ---------------------------------------------------------------------------

def test_workbench_dir_is_reserved():
    assert WORKBENCH_DIR_NAME == "workbench"
    assert WORKBENCH_DIR_NAME in CASE_RESERVED_ARTIFACT_NAMES


def test_create_rejects_preseeded_workbench(tmp_path):
    folder = tmp_path / "casedir"
    folder.mkdir()
    (folder / WORKBENCH_DIR_NAME).mkdir()
    with pytest.raises(FileExistsError):
        WbTicketCase.create_case_in_folder(folder)


def test_workbench_report_narrative():
    r = WorkbenchReport(narrative="hello")
    assert "WorkbenchReport" in repr(r)
    assert "hello" in str(r)
    assert "WorkbenchReport" in r._repr_markdown_()


# ---------------------------------------------------------------------------
# for_project / path resolution
# ---------------------------------------------------------------------------

def test_for_project_creates_fixtures_when_parent_exists(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "volatile").mkdir()
    monkeypatch.chdir(proj)
    wb = CaseWorkbench.for_project(quiet=True, case_modules=[])
    assert wb.fixtures_root.is_dir()
    assert wb.scratch_root.is_dir()
    assert wb.project_root == proj.resolve()
    assert wb.fixtures_root == (proj / "tests" / "case-fixtures").resolve()


def test_for_project_parent_missing_hard_fail(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    with pytest.raises(WorkbenchError, match="parent directory does not exist"):
        CaseWorkbench.for_project(
            fixtures_relpath="missing_parent/case-fixtures",
            quiet=True,
            case_modules=[],
        )


def test_for_project_no_pyproject_hard_fail(tmp_path, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(WorkbenchError, match="No pyproject.toml"):
        CaseWorkbench.for_project(quiet=True)


def test_for_project_absolute_both_without_project(tmp_path, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    fx = tmp_path / "fx"
    sc = tmp_path / "sc"
    fx.mkdir()
    sc.mkdir()
    wb = CaseWorkbench.for_project(
        fixtures_root=fx, scratch_root=sc, quiet=True, case_modules=[],
    )
    assert wb.project_root is None
    assert wb.fixtures_root == fx.resolve()


def test_for_project_rejects_relpath_and_absolute(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (proj / "tests").mkdir()
    monkeypatch.chdir(proj)
    with pytest.raises(WorkbenchError, match="only one of fixtures_root"):
        CaseWorkbench.for_project(
            fixtures_root=tmp_path / "fx",
            fixtures_relpath="tests/case-fixtures",
            quiet=True,
        )


def test_roots_immutable(tmp_path):
    wb = _wb(tmp_path)
    with pytest.raises(AttributeError):
        wb.fixtures_root = tmp_path / "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# create / clone / freeze_dry / focus / cleanup
# ---------------------------------------------------------------------------

def test_create_and_focus_and_cases(tmp_path):
    wb = _wb(tmp_path)
    report = wb.create(WbTicketCase, nickname="demo")
    assert report.case is wb.case
    assert wb.case.case_id.startswith("demo--")
    assert (wb.scratch_root / wb.case.case_id).is_dir()
    listed = wb.cases()
    assert any(r["id"] == wb.case.case_id for r in listed.rows)
    cid = wb.case.case_id
    wb.case.case_detach()
    wb._live.clear()
    wb._case = None
    focused = wb.focus(cid)
    assert focused.case.case_id == cid


def test_clone_fixture_remints_and_provenance(tmp_path):
    wb = _wb(tmp_path)
    created = wb.create(WbTicketCase)
    freeze = wb.freeze_dry(nickname="newly_created/minimal", description="seed")
    assert freeze.nickname == "WbTicketCase/newly_created/minimal"
    assert (wb.fixtures_root / "WbTicketCase" / "newly_created" / "minimal" / "case_record.yaml").is_file()
    # leave focus, clone from shelf
    old_id = wb.case.case_id
    wb.case.case_detach()
    cloned = wb.clone("WbTicketCase/newly_created/minimal")
    assert cloned.case.case_id != old_id
    assert cloned.case.case_id == cloned.case.case_folder.name
    prov = yaml.safe_load(
        (cloned.case.case_folder / "workbench" / "provenance.yaml").read_text(encoding="utf-8")
    )
    assert prov["immediate_source"].startswith("fixture:")
    assert prov["original_fixture_nickname"] == "WbTicketCase/newly_created/minimal"
    # provenance stripped from fixture
    assert not (
        wb.fixtures_root / "WbTicketCase" / "newly_created" / "minimal"
        / "workbench" / "provenance.yaml"
    ).exists()


def test_clone_same_nickname_replaces(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    wb.freeze_dry(nickname="newly_created/minimal")
    first = wb.clone("WbTicketCase/newly_created/minimal")
    first_id = first.case.case_id
    second = wb.clone("WbTicketCase/newly_created/minimal")
    assert second.case.case_id != first_id
    assert not (wb.scratch_root / first_id).exists()
    assert first.case.case_is_detached


def test_clone_parallel_keeps_both(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    wb.freeze_dry(nickname="newly_created/minimal")
    a = wb.clone("WbTicketCase/newly_created/minimal")
    b = wb.clone("WbTicketCase/newly_created/minimal", parallel=True)
    assert (wb.scratch_root / a.case.case_id).exists()
    assert (wb.scratch_root / b.case.case_id).exists()


def test_clone_keep_identity_collision(tmp_path):
    wb = _wb(tmp_path)
    c = wb.create(WbTicketCase)
    # keep_identity wants dest folder named like source id  still present ? collision
    with pytest.raises(WorkbenchError, match="already exists"):
        wb.clone(keep_identity=True)
    assert c.case is wb.case


def test_clone_absolute_path_copies_into_scratch_with_path_provenance(tmp_path):
    wb = _wb(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "live-case"
    elsewhere.parent.mkdir(parents=True)
    source = WbTicketCase.create_case_in_folder(elsewhere, case_id="ext-001", nickname="external")
    source.case_detach()

    report = wb.clone(elsewhere.resolve())
    assert report.case is wb.case
    assert report.case.case_id != "ext-001"
    assert (wb.scratch_root / report.case.case_id).is_dir()
    assert elsewhere.is_dir()  # original untouched
    assert (elsewhere / "case_record.yaml").is_file()

    prov = yaml.safe_load(
        (Path(report.case.case_folder) / "workbench" / "provenance.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert prov["immediate_source"] == f"path:{elsewhere.resolve()}"
    assert Path(prov["source_path"]) == elsewhere.resolve()
    assert prov["original_case_id"] == "ext-001"


def test_clone_absolute_path_string(tmp_path):
    wb = _wb(tmp_path)
    elsewhere = tmp_path / "other" / "case-a"
    elsewhere.parent.mkdir(parents=True)
    WbTicketCase.create_case_in_folder(elsewhere, case_id="str-path").case_detach()
    report = wb.clone(str(elsewhere.resolve()))
    assert report.case.case_id != "str-path"
    prov = yaml.safe_load(
        (Path(report.case.case_folder) / "workbench" / "provenance.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert prov["immediate_source"].startswith("path:")


def test_clone_absolute_path_missing_record_errors(tmp_path):
    wb = _wb(tmp_path)
    empty = tmp_path / "not-a-case"
    empty.mkdir()
    with pytest.raises(WorkbenchError, match="case_record"):
        wb.clone(empty.resolve())


def test_clone_relative_three_segment_still_fixture_nickname(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    wb.freeze_dry(nickname="newly_created/minimal")
    wb.case.case_detach()
    wb._live.clear()
    wb._case = None
    # Relative Class/group/sample must not be treated as a filesystem path
    report = wb.clone("WbTicketCase/newly_created/minimal")
    prov = yaml.safe_load(
        (Path(report.case.case_folder) / "workbench" / "provenance.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert prov["immediate_source"].startswith("fixture:")


def test_ambiguous_focus_glob(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase, nickname="alpha")
    wb.create(WbTicketCase, nickname="alpha2")
    with pytest.raises(WorkbenchError, match="Ambiguous"):
        wb.focus("alpha*")


def test_context_manager_cleanup(tmp_path):
    fx = tmp_path / "fx"
    sc = tmp_path / "sc"
    fx.mkdir()
    sc.mkdir()
    with CaseWorkbench(
        fixtures_root=fx,
        scratch_root=sc,
        project_root=tmp_path,
        case_modules=[],
        source_roots=[],
    ) as wb:
        wb.create(WbTicketCase)
        cid = wb.case.case_id
        assert (wb.scratch_root / cid).exists()
    assert not (sc / cid).exists()


def test_incoming_assets_paths(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    group = Path(wb.case.case_folder) / "workbench" / "incoming_assets" / "attachments"
    group.mkdir(parents=True)
    pdf = group / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    report = wb.incoming_assets("attachments")
    assert report.paths[0] == pdf.resolve()
    # workbench must not write into assets/
    assert not (Path(wb.case.case_folder) / "assets" / "a.pdf").exists()


def test_list_examples_prefix(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    wb.freeze_dry(nickname="newly_created/minimal", description="seed")
    wb.freeze_dry(nickname="awaiting_approval/vip", description="vip", overwrite=False)
    # second freeze from same case ok
    all_ex = wb.list_examples()
    assert len(all_ex.examples) == 2
    filtered = wb.list_examples("WbTicketCase/newly_created")
    assert len(filtered.examples) == 1


def test_freeze_dry_empty_dest_ok_nonempty_errors(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    dest = wb.fixtures_root / "WbTicketCase" / "newly_created" / "minimal"
    dest.mkdir(parents=True)
    # empty pre-existing folder is fine
    report = wb.freeze_dry(nickname="newly_created/minimal")
    assert report.dest == dest.resolve()
    assert (dest / "case_record.yaml").is_file()
    with pytest.raises(WorkbenchError, match="not empty|overwrite"):
        wb.freeze_dry(nickname="newly_created/minimal")
    report2 = wb.freeze_dry(nickname="newly_created/minimal", overwrite=True)
    assert report2.dest == dest.resolve()


def test_freeze_dry_absolute_dest(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    out = tmp_path / "exports" / "snapshot"
    out.parent.mkdir(parents=True)
    report = wb.freeze_dry(dest=out.resolve())
    assert report.dest == out.resolve()
    assert report.nickname is None
    assert (out / "case_record.yaml").is_file()
    assert not (out / "workbench" / "fixture.yaml").is_file()
    # nonempty guard
    with pytest.raises(WorkbenchError, match="not empty|overwrite"):
        wb.freeze_dry(dest=out.resolve())
    wb.freeze_dry(dest=out.resolve(), overwrite=True)
    assert (out / "case_record.yaml").is_file()


def test_freeze_dry_dest_and_nickname_mutex(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    with pytest.raises(WorkbenchError, match="nickname|dest"):
        wb.freeze_dry(nickname="g/s", dest=(tmp_path / "x").resolve())
    with pytest.raises(WorkbenchError, match="nickname|dest"):
        wb.freeze_dry()


def test_freeze_dry_relative_dest_rejected(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    with pytest.raises(WorkbenchError, match="absolute"):
        wb.freeze_dry(dest="relative/snapshot")


# ---------------------------------------------------------------------------
# drive / probe / history / problems
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_and_trigger_and_history(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    await wb.run()
    assert wb.case.case_state == "priced"
    await wb.trigger("approve")
    assert wb.case.case_is_terminal
    hist = wb.history()
    assert len(hist.steps) >= 2
    st = wb.status()
    assert st.terminal is True


@pytest.mark.asyncio
async def test_run_stop_before(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    await wb.run(stop_before="priced")
    assert wb.case.case_state == "triaged"


@pytest.mark.asyncio
async def test_probe_guard_verdict(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbGuardedCase)
    report = wb.probe()
    assert report.edges
    auto = [e for e in report.edges if e.kind == "auto"]
    assert auto
    assert auto[0].chokes == ["slot_a"]
    # Inside a running asyncio loop, sync probe cannot await async guards ? error
    assert auto[0].guards


def test_probe_async_guard_outside_loop(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbGuardedCase)
    report = wb.probe()
    auto = [e for e in report.edges if e.kind == "auto"]
    assert auto
    assert auto[0].guards
    assert any(v is True for _, v in auto[0].guards)


def test_help_catalog_and_prefix(tmp_path):
    wb = _wb(tmp_path)
    cat = wb.help()
    assert "clone" in cat.narrative
    detail = wb.help("clone")
    assert "clone" in detail.narrative.lower() or "Clone" in detail.narrative
    hist = wb.help("hist")
    assert hist.name == "history"


def test_doctor_never_raises(tmp_path):
    wb = _wb(tmp_path)
    report = wb.doctor()
    assert report.findings
    assert all(f.level in ("info", "warning") for f in report.findings)


def test_path_short_and_full(tmp_path):
    wb = _wb(tmp_path)
    wb.create(WbTicketCase)
    short = wb.path()
    assert ".." not in short.narrative
    assert short.folder.is_absolute()
    full = wb.path(full=True)
    assert str(full.folder) in full.narrative or full.narrative == str(full.folder)


def test_class_index_case_modules(tmp_path, monkeypatch):
    # Write a tiny module under tmp and import via case_modules
    mod_dir = tmp_path / "pkg"
    mod_dir.mkdir()
    (mod_dir / "__init__.py").write_text("", encoding="utf-8")
    (mod_dir / "mini_case.py").write_text(
        textwrap.dedent("""\
            from totodev_pub.folder_backed_case import FolderBackedCase
            from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry

            @case_type_registry.register
            class IndexedMiniCase(FolderBackedCase):
                asset_aliases = []
                fsm_trigger_chokes = {}
                fsm_state_chains = ["^a--go-->b^"]
                async def perform_go(self, tctx):
                    pass
        """),
        encoding="utf-8",
    )
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        wb = CaseWorkbench(
            fixtures_root=tmp_path / "fx",
            scratch_root=tmp_path / "sc",
            project_root=tmp_path,
            case_modules=["pkg.mini_case"],
            source_roots=[],
        )
        (tmp_path / "fx").mkdir()
        (tmp_path / "sc").mkdir()
        # class should be registered via eager import
        assert case_type_registry.resolve_case_type("IndexedMiniCase") is not None
        wb.create(case_type_registry.resolve_case_type("IndexedMiniCase"))
        assert wb.case is not None
        first_id = wb.case.case_id
        # string form uses the same ClassIndex / registry path
        wb.create("IndexedMiniCase", nickname="by-name")
        assert wb.case.case_id != first_id
        assert wb.case.case_id.startswith("by-name--")
        with pytest.raises(WorkbenchError, match="not registered"):
            wb.create("DefinitelyMissingCaseClass")
    finally:
        sys.path.remove(str(tmp_path))


def test_pytest_bridge_iter_scenarios(tmp_path):
    from totodev_pub.case_testing.pytest_bridge import iter_scenario_paths, make_scenario_parametrization

    root = tmp_path / "case-scenarios" / "demo"
    root.mkdir(parents=True)
    (root / "scenario_one.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (root / "other.py").write_text("pass\n", encoding="utf-8")
    paths = list(iter_scenario_paths(tmp_path / "case-scenarios"))
    assert len(paths) == 1
    assert paths[0].name == "scenario_one.py"
    name, vals, ids = make_scenario_parametrization(tmp_path / "case-scenarios")
    assert name == "scenario_path"
    assert len(vals) == 1
    assert ids == ["scenario_one"]


@pytest.mark.asyncio
async def test_run_stop_on_problems(tmp_path):
    wb = _wb(tmp_path)

    @case_type_registry.register
    class ProblemCase(FolderBackedCase):
        asset_aliases = []
        fsm_trigger_chokes = {}
        fsm_state_chains = ["^a--go-->b--go2-->c^"]

        async def perform_go(self, tctx):
            self.case_emit_alert_event("boom")

        async def perform_go2(self, tctx):
            pass

    wb.create(ProblemCase)
    report = await wb.run(stop_on_problems=True, max_steps=10)
    assert len(report.new_problems) >= 1
    # Should have stopped after first step that introduced a problem (before go2)
    assert wb.case.case_state == "b"


def test_keep_all_idempotent(tmp_path):
    wb = _wb(tmp_path, keep_assets=True)
    wb.create(WbTicketCase)
    keep = Path(wb.case.case_folder) / "_keep.txt"
    text1 = keep.read_text(encoding="utf-8") if keep.exists() else ""
    wb.focus(wb.case)
    text2 = keep.read_text(encoding="utf-8") if keep.exists() else ""
    assert "**" in text2 or "**" in text1
