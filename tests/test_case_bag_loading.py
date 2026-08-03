# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Loading a bag of case folders into a running fleet, without consuming the bag.

Adopt *moves* a case folder into managed storage, and running a case mutates it.
So a loader that adopted the caller's folders directly would destroy the fixture
on first use and make the second run of any experiment meaningless. Copying is
the whole reason this exists rather than a three-line adopt loop.
"""

from pathlib import Path

import pytest

from case_manager_test_utils import TerminalCase, TicketCase, seed_detached_case
from totodev_pub.case_manager_support.bag_loading import (
    case_folders_in,
    load_case_bag,
    make_case_bag_fixture,
)
from totodev_pub.folder_backed_case_support.case_type_registry import (
    CaseTypeRegistry,
    case_type_registry,
)
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    case_type_registry._registry.clear()
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


case_bag = make_case_bag_fixture(
    case_types=[TicketCase, TerminalCase], maintenance_interval_secs=0.01
)


def _bag_of(tmp_path: Path, count: int, name: str = "bag") -> Path:
    bag = tmp_path / name
    bag.mkdir(parents=True)
    for i in range(count):
        seed_detached_case(TicketCase, bag / f"case-{i}")
    return bag


# ------------------------------------------------------------------ discovery


def test_only_direct_children_holding_a_record_are_cases(tmp_path):
    """Shallow on purpose: a nested search walks into a case's own assets."""
    bag = _bag_of(tmp_path, 2)
    (bag / "notes").mkdir()                       # not a case
    (bag / "README.txt").write_text("hi")         # not even a directory
    nested = bag / "case-0" / "sub"
    nested.mkdir()
    (nested / RECORD_NAME).write_text("case_id: decoy\n")

    found = [p.name for p in case_folders_in(bag)]
    assert found == ["case-0", "case-1"]


def test_a_missing_bag_yields_nothing_rather_than_raising(tmp_path):
    assert case_folders_in(tmp_path / "nope") == []


# -------------------------------------------------------------------- loading


@pytest.mark.asyncio
async def test_a_bag_becomes_a_recovered_manager_holding_every_case(tmp_path):
    bag = _bag_of(tmp_path, 3)

    case_type_registry.register_case_types(TicketCase)
    manager, report = await load_case_bag(bag, tmp_path / "cache")

    assert len(report.adopted) == 3
    assert report.all_adopted
    assert manager.is_recovered, "adoption needs the namespace, so recovery already ran"
    assert not manager.is_running, "how to run it is the caller's decision"
    assert len(manager._driver) == 3


@pytest.mark.asyncio
async def test_the_source_bag_is_left_untouched(tmp_path):
    """The property that makes a repeated run repeatable."""
    bag = _bag_of(tmp_path, 2)
    before = {p.name: sorted(q.name for q in p.iterdir()) for p in case_folders_in(bag)}

    case_type_registry.register_case_types(TicketCase)
    await load_case_bag(bag, tmp_path / "cache")

    after = {p.name: sorted(q.name for q in p.iterdir()) for p in case_folders_in(bag)}
    assert after == before, "adopt moves; the loader must copy first"


@pytest.mark.asyncio
async def test_loading_the_same_bag_twice_gives_two_independent_fleets(tmp_path):
    """The point of leaving the bag alone: the experiment can be run again."""
    bag = _bag_of(tmp_path, 2)

    case_type_registry.register_case_types(TicketCase)
    first, first_report = await load_case_bag(bag, tmp_path / "run1")
    second, second_report = await load_case_bag(bag, tmp_path / "run2")

    assert sorted(first_report.adopted) == sorted(second_report.adopted)
    assert first._store.root_dir != second._store.root_dir


@pytest.mark.asyncio
async def test_non_cases_are_reported_not_silently_dropped(tmp_path):
    bag = _bag_of(tmp_path, 1)
    (bag / "scratch").mkdir()
    (bag / "logs").mkdir()

    case_type_registry.register_case_types(TicketCase)
    _manager, report = await load_case_bag(bag, tmp_path / "cache")

    assert len(report.adopted) == 1
    assert sorted(report.skipped) == ["logs", "scratch"]
    assert not report.all_adopted, "a partly-loaded bag must not look like a clean one"


@pytest.mark.asyncio
async def test_a_rejected_case_names_itself_and_the_reason(tmp_path):
    """One bad case does not abort the load, and does not vanish quietly."""
    bag = _bag_of(tmp_path, 2)
    (bag / "unregistered").mkdir()
    seed_detached_case(TerminalCase, bag / "unregistered" / "inner")
    # A case folder whose type this manager was never told about.
    (bag / "unregistered" / "inner" / RECORD_NAME).replace(
        bag / "unregistered" / RECORD_NAME
    )

    # Private catalog: only TicketCase is known, so TerminalCase must be rejected
    # even if the process-global registry has been polluted by other tests.
    registry = CaseTypeRegistry()
    registry.register_case_types(TicketCase)
    _manager, report = await load_case_bag(bag, tmp_path / "cache", registry=registry)

    assert len(report.adopted) == 2
    assert [name for name, _why in report.rejected] == ["unregistered"]
    assert report.rejected[0][1], "the reason travels with the rejection"


@pytest.mark.asyncio
async def test_an_empty_bag_is_a_clean_load_of_nothing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    case_type_registry.register_case_types(TicketCase)
    manager, report = await load_case_bag(empty, tmp_path / "cache")
    assert report.adopted == [] and report.all_adopted
    assert len(manager._driver) == 0


# -------------------------------------------------------------------- fixture


@pytest.mark.asyncio
async def test_the_fixture_loads_and_the_fleet_runs(case_bag, tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    seed_detached_case(TerminalCase, bag / "c1")

    manager, report = await case_bag(bag)
    assert report.all_adopted

    await manager.start()
    case = manager.get_live(report.adopted[0])
    result = await manager.fire(case_id=case.case_id, trigger="finish")
    assert result.progressed


@pytest.mark.asyncio
async def test_the_fixture_isolates_repeated_loads(case_bag, tmp_path):
    """Several bags in one test must not land on the same root."""
    bag = _bag_of(tmp_path, 1)
    first, _ = await case_bag(bag)
    second, _ = await case_bag(bag)
    assert first._store.root_dir != second._store.root_dir


@pytest.mark.asyncio
async def test_the_fixture_accepts_per_load_overrides(case_bag, tmp_path):
    bag = _bag_of(tmp_path, 1)
    manager, _ = await case_bag(bag, concurrency_ceiling=7)
    assert manager._policy.concurrency_ceiling == 7
