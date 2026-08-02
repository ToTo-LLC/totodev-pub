# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""From "a folder of case folders" to "a manager driving them", in one call.

The common starting shape is a bag of prepared case folders — a fixture
directory, a batch handed over by an upstream system, a set of cases exported
from somewhere else — and the goal is a manager running them. Getting there by
hand means provisioning a root, registering types, and writing an adopt loop,
which is enough ceremony that most callers write it once badly and copy it.

**The bag is copied, never consumed.** Adopt *moves* a case folder into managed
storage, and running a case mutates it — record, events, logs, lease. So a
loader that adopted the caller's folders directly would destroy the fixture on
first use and make the second run of any experiment meaningless. Copying costs
one pass over the bag and buys repeatability.

This is deliberately not a second storage model. It is construction ceremony,
removed; the manager owns its filespace exactly as it always does.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from totodev_pub.case_manager_support.layout import read_case_id_from_folder
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME

if TYPE_CHECKING:
    from totodev_pub.case_manager import CaseManager
    from totodev_pub.folder_backed_case import FolderBackedCase

logger = logging.getLogger(__name__)


@dataclass
class BagLoadReport:
    """What the bag contained and what became of it."""

    adopted: list[str] = field(default_factory=list)
    #: ``(folder name, why)`` for candidates the manager declined.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    #: Children that were not case folders at all — no ``case_record.yaml``.
    skipped: list[str] = field(default_factory=list)

    @property
    def all_adopted(self) -> bool:
        return not self.rejected and not self.skipped


def case_folders_in(bag: Path) -> list[Path]:
    """Direct children of ``bag`` that look like case folders.

    Deliberately shallow: a nested search would eventually walk into a case's own
    assets and find something that looks like a second case.
    """
    if not bag.is_dir():
        return []
    return sorted(
        child for child in bag.iterdir()
        if child.is_dir() and (child / RECORD_NAME).exists()
    )


async def load_case_bag(
    source_bag: str | Path,
    cache_root: str | Path,
    *,
    register_types: Sequence[type["FolderBackedCase"]],
    **overrides: Any,
) -> tuple["CaseManager", BagLoadReport]:
    """Open a filespace, copy every case in ``source_bag`` into it, adopt each.

    Returns a **recovered but not started** manager, so the caller decides how to
    run it: ``serve(manager)`` for a process, or ``start()`` / ``fire()`` for a
    test or an experiment. Recovery has already happened because adoption needs
    the manager namespace to exist.

    ``source_bag`` is left exactly as it was found.
    """
    from totodev_pub.case_manager import CaseManager

    bag = Path(source_bag)
    store = CaseManager.open_local_store(cache_root, **overrides)
    manager = CaseManager(store, register_types=list(register_types))
    await manager.recover()

    report = BagLoadReport()
    for child in sorted(p for p in bag.iterdir() if p.is_dir()) if bag.is_dir() else []:
        if not (child / RECORD_NAME).exists():
            report.skipped.append(child.name)
            continue
        staged = manager.allocate_staging_folder()
        # copytree needs a non-existent destination; staging hands us an empty one.
        shutil.rmtree(staged)
        shutil.copytree(child, staged)
        result = await manager.adopt_case(staged)
        if result.status == "completed":
            report.adopted.append(result.case_id)
        else:
            report.rejected.append((child.name, result.rejection_reason or result.status))
            logger.warning(
                "Case bag: %s was not adopted (%s)", child.name, result.rejection_reason
            )
    return manager, report


def make_case_bag_fixture(
    *,
    register_types: Sequence[type["FolderBackedCase"]],
    **default_overrides: Any,
):
    """Build a pytest fixture that loads case bags and cleans up after itself.

    Yields an async loader rather than a manager, because the bag differs per
    test while the wiring does not::

        from myapp.cases import InquiryCase
        case_bag = make_case_bag_fixture(register_types=[InquiryCase])

        async def test_the_batch_completes(case_bag, tmp_path):
            manager, report = await case_bag(FIXTURES / "inquiries")
            assert report.all_adopted
            await manager.start()
            ...

    Every manager the loader creates is stopped at teardown, so a test that
    raises mid-run does not leave a live pool holding leases on ``tmp_path``.
    Each load gets its own root under ``tmp_path``, so one test may load several
    bags without them colliding.
    """
    # pytest-asyncio runs in strict mode, where an async fixture declared with a
    # bare @pytest.fixture is silently not awaited. Imported here rather than at
    # module scope so the loader itself carries no test-time dependency.
    import pytest_asyncio

    @pytest_asyncio.fixture
    async def case_bag(tmp_path: Path):
        created: list["CaseManager"] = []

        async def _load(
            source_bag: str | Path, **overrides: Any
        ) -> tuple["CaseManager", BagLoadReport]:
            root = tmp_path / f"case_bag_{len(created)}"
            manager, report = await load_case_bag(
                source_bag,
                root,
                register_types=register_types,
                **{**default_overrides, **overrides},
            )
            created.append(manager)
            return manager, report

        try:
            yield _load
        finally:
            for manager in created:
                try:
                    await manager.stop()
                except Exception:
                    logger.exception("Case bag fixture: stopping a manager failed")

    return case_bag
