# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Direct coverage of case_manager_support/aberrant.py."""

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.aberrant import move_case_to_aberrant
from totodev_pub.case_manager_support.layout import (
    aberrant_grouping_key,
    live_grouping_key,
    ref_path_for_case,
)
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


@pytest.fixture(autouse=True)
def _isolate_case_registry():
    saved = dict(case_type_registry._registry)
    try:
        yield
    finally:
        case_type_registry._registry.clear()
        case_type_registry._registry.update(saved)


@pytest.mark.asyncio
async def test_move_relocates_a_managed_case_into_the_aberrant_bucket(tmp_path):
    """Branch A: a live cache entry exists, so the folder is relocated by the cache."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    staging = tmp_path / "inbound"
    seed_detached_case(TicketCase, staging)
    case = await adopt_into_live(manager, staging)
    case_id = case.case_id
    live_folder = case.case_folder
    case.case_detach()
    (live_folder / "payload.txt").write_text("keep me", encoding="utf-8")

    dest = await move_case_to_aberrant(
        manager._cache, manager._policy, case_id, live_folder, "verification failed"
    )

    ref_path = ref_path_for_case(manager._policy, case_id)
    assert manager._cache.find_file(ref_path, live_grouping_key(manager._policy)) is None
    assert manager._cache.find_file(ref_path, aberrant_grouping_key(manager._policy)) is not None
    assert dest.exists()
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "keep me"


@pytest.mark.asyncio
async def test_move_rescues_an_orphan_folder_with_no_cache_entry(tmp_path):
    """Branch B: no cache entry exists, so the folder is copied in and registered.

    This is the path reached when termination fails after the live entry is already
    gone. It must leave a real cache entry behind, not just bytes on disk, or the
    case becomes invisible to iter_aberrant() and every other cache-driven query.
    """
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()

    orphan = tmp_path / "orphan"
    case = seed_detached_case(TicketCase, orphan)
    case_id = case.case_id

    ref_path = ref_path_for_case(manager._policy, case_id)
    assert manager._cache.find_file(ref_path, live_grouping_key(manager._policy)) is None

    dest = await move_case_to_aberrant(
        manager._cache, manager._policy, case_id, orphan, "orphaned by a failed termination"
    )

    assert dest.exists()
    assert (dest / RECORD_NAME).exists()
    ref = manager._cache.find_file(ref_path, aberrant_grouping_key(manager._policy))
    assert ref is not None, "orphan rescue must register a cache entry, not just copy bytes"
    assert ref.slave_dir_path == dest


@pytest.mark.asyncio
async def test_orphan_rescue_is_discoverable_via_iter_aberrant(tmp_path):
    manager = provision_manager(tmp_path)
    manager._ensure_namespace()

    orphan = tmp_path / "orphan2"
    case = seed_detached_case(TicketCase, orphan)

    await move_case_to_aberrant(
        manager._cache, manager._policy, case.case_id, orphan, "mechanical failure"
    )

    found = [reader.case_id for reader in manager.iter_aberrant()]
    assert case.case_id in found
