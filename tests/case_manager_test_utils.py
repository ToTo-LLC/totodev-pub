# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Shared fixtures for CaseManager tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.mailbox import MailboxTransport
from totodev_pub.case_manager_support.signaling_adapter import SignalingAdapter
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    case_type_registry._registry.clear()
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


class TicketCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> open -- work --> done --> [*]"]

    async def perform_work(self, tctx):
        pass


class TerminalCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> open -- finish --> done --> [*]"]

    async def perform_finish(self, tctx):
        pass


class ManualCase(FolderBackedCase):
    asset_aliases = {}
    fsm_trigger_chokes = {}
    fsm_state_chains = ["[*] --> waiting == push ==> done --> [*]"]


def run(coro):
    import asyncio
    return asyncio.run(coro)


_BINDING_KEYS = {"driver", "registry"}


def provision_manager(tmp_path: Path, **overrides) -> CaseManager:
    """Open a fresh filespace under ``tmp_path`` and return a manager over it.

    Policy overrides go into the record as it is created, bindings go to the
    manager; the caller passes both as one flat set of keywords. Common test
    case types are registered on the effective registry before construction.
    """
    bindings = {k: overrides.pop(k) for k in list(overrides) if k in _BINDING_KEYS}
    overrides.setdefault("maintenance_interval_secs", 0.01)
    store = CaseManager.open_local_store(tmp_path / "cache", **overrides)
    registry = bindings.get("registry")
    target = case_type_registry if registry is None else registry
    target.register_case_types(TicketCase, TerminalCase, ManualCase)
    return CaseManager(store, **bindings)


def seed_detached_case(
    case_cls: type[FolderBackedCase],
    folder: Path,
    *,
    external_key: str | None = None,
) -> FolderBackedCase:
    case = case_cls.create_case_in_folder(folder, external_key=external_key)
    case.case_detach()
    return case


def transport_for(manager: CaseManager) -> MailboxTransport:
    """The mailbox layout for a manager, without attaching an adapter to it."""
    return MailboxTransport(manager._manager_dir, manager._policy)


def attach_adapter(manager: CaseManager) -> SignalingAdapter:
    """Give a manager a request transport, the way serve() does.

    The fleet no longer knows what a mailbox is, so a test that exercises the
    file-drop protocol has to compose the two explicitly — which is the point of
    the extraction, not an inconvenience of it.
    """
    adapter = SignalingAdapter(manager)
    adapter.recover()
    adapter.attach()
    return adapter


async def adopt_into_live(manager: CaseManager, staging: Path) -> FolderBackedCase:
    result = await manager.adopt_case(staging)
    assert result.status == "completed", result.rejection_reason
    return manager.get_live(result.case_id)
