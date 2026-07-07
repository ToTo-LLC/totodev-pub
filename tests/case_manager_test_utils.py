# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Shared fixtures for CaseManager tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.constants import PLACEHOLDER_HEADER
from totodev_pub.case_manager_support.layout import live_grouping_key, ref_path_for_case
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


class TicketCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^open--work-->done^"]

    async def perform_work(self, tctx):
        pass


class TerminalCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^open--finish-->done^"]

    async def perform_finish(self, tctx):
        pass


class ManualCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["^waiting==push-->done^"]


def run(coro):
    import asyncio
    return asyncio.run(coro)


def provision_manager(tmp_path: Path, **overrides) -> CaseManager:
    return CaseManager.open(
        tmp_path / "cache",
        register_types=[TicketCase, TerminalCase, ManualCase],
        maintenance_interval_secs=0.01,
        **overrides,
    )


def seed_detached_case(
    case_cls: type[FolderBackedCase],
    folder: Path,
    *,
    external_key: str | None = None,
) -> FolderBackedCase:
    case = case_cls.create_case_in_folder(folder, external_key=external_key)
    case.case_detach()
    return case


async def adopt_into_live(manager: CaseManager, staging: Path) -> FolderBackedCase:
    result = await manager.adopt_case(staging)
    assert result.status == "completed", result.rejection_reason
    return manager.get_live(result.case_id)


def write_live_placeholder(cache: CachedFileFolders, policy, case_id: str) -> Path:
    ref = ref_path_for_case(policy, case_id)
    gk = live_grouping_key(policy)
    placeholder = cache.root_dir / gk[0] / ref
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    placeholder.write_text(PLACEHOLDER_HEADER, encoding="utf-8")
    return cache.get_slave_dir(gk, ref)
