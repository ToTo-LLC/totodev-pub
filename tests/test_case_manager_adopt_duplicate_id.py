# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""One case_id, one case: adopt must refuse an identity the store already holds.

``case_id`` is the identity every other surface addresses a case by — the store's
folder mapping, the driver's index, ticket filenames, mailbox correlation. Two
cases sharing one would not degrade gracefully; it would corrupt addressing in a
way nothing downstream can detect. Adopt guards it twice, and both layers are
exercised here:

1. ``validate_adopt_source`` rejects before anything moves, so a duplicate leaves
   the source folder untouched and re-submittable under a fresh id.
2. A ``DuplicateCaseIdError`` backstop after ``create_location``, which exists
   because ``create_location`` is idempotent and would otherwise hand back a
   folder the incumbent already occupies.

The default case-id generator is only monotonic *within a process*
(``TimeSlugCaseIDGenerator``), so two intake processes minting in the same
millisecond is exactly how a duplicate reaches adopt in practice. That makes
these paths load-bearing rather than theoretical.
"""

from pathlib import Path

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager_support.adopt import (
    AdoptRejectReason,
    adopt_case_folder,
    validate_adopt_source,
)
from totodev_pub.case_manager_support.case_store import QUARANTINED
from totodev_pub.folder_backed_case_support.constants import RECORD_NAME


def _stage_with_case_id(folder: Path, case_id: str) -> None:
    """A detached staging folder deliberately carrying an already-taken id."""
    TicketCase.create_case_in_folder(folder, case_id=case_id).case_detach()


@pytest.mark.asyncio
async def test_adopt_rejects_a_case_id_that_is_already_live(tmp_path):
    manager = provision_manager(tmp_path)
    await manager.recover()

    incumbent = await adopt_into_live(manager, seed_detached_case(
        TicketCase, tmp_path / "first"
    ).case_folder)
    taken = incumbent.case_id

    notices = []
    manager.subscribe_notices(notices.append)

    collider = tmp_path / "second"
    _stage_with_case_id(collider, taken)
    result = await manager.adopt_case(collider)

    assert result.status == "rejected"
    assert AdoptRejectReason.DUPLICATE_CASE_ID.value in result.rejection_reason
    assert [n.kind for n in notices] == ["ADOPT_REJECTED"]

    # Validation runs against the source, so nothing was moved: the caller can
    # re-mint an id and resubmit the very same tree.
    assert (collider / RECORD_NAME).exists(), "a rejected source must not be consumed"


@pytest.mark.asyncio
async def test_a_departed_case_id_is_still_taken(tmp_path):
    """An id belonging to a quarantined case is not free for reuse.

    ``store.contains()`` deliberately spans every status, not just the live
    bucket: reusing a departed id would give the archive and the new case one
    identity, and the archive is what an operator inspects after the fact.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()

    departed = await adopt_into_live(manager, seed_detached_case(
        TicketCase, tmp_path / "first"
    ).case_folder)
    taken = departed.case_id
    departed.case_detach()          # a held lease legitimately blocks relocation
    await manager._store.set_status(taken, QUARANTINED)

    collider = tmp_path / "second"
    _stage_with_case_id(collider, taken)
    result = await manager.adopt_case(collider)

    assert result.status == "rejected"
    assert AdoptRejectReason.DUPLICATE_CASE_ID.value in result.rejection_reason


@pytest.mark.asyncio
async def test_validate_names_the_duplicate_before_any_transfer(tmp_path):
    """The rejection is decided by validation, not discovered during the move."""
    manager = provision_manager(tmp_path)
    await manager.recover()

    collider = tmp_path / "staged"
    _stage_with_case_id(collider, "already-mine")

    case_id, reason, detail = validate_adopt_source(
        collider,
        store=manager._store,
        policy=manager._policy,
        manager_dir=manager._manager_dir,
        registry=manager._registry,
        case_id_exists=lambda cid: cid == "already-mine",
    )

    assert reason is AdoptRejectReason.DUPLICATE_CASE_ID
    assert detail == "duplicate case_id"
    assert case_id is None, "a rejected source yields no adoptable id"


@pytest.mark.asyncio
async def test_backstop_catches_a_duplicate_the_precheck_missed(tmp_path):
    """The post-``create_location`` guard, and that it spares the incumbent.

    Reached only when the pre-check is wrong about what the store holds — a lost
    race between two adopts of the same id, which the pre-check cannot exclude
    because nothing locks between it and ``create_location``. Simulated here by
    forcing ``case_id_exists`` to lie.
    """
    manager = provision_manager(tmp_path)
    await manager.recover()

    incumbent = await adopt_into_live(manager, seed_detached_case(
        TicketCase, tmp_path / "first"
    ).case_folder)
    taken = incumbent.case_id
    incumbent_folder = incumbent.case_folder

    collider = tmp_path / "second"
    _stage_with_case_id(collider, taken)

    result = await adopt_case_folder(
        collider,
        store=manager._store,
        policy=manager._policy,
        manager_dir=manager._manager_dir,
        registry=manager._registry,
        driver_add=manager._driver.add,
        case_id_exists=lambda cid: False,      # the race the backstop exists for
        quarantine=manager._quarantine,
    )

    assert result.status == "error"
    assert taken in result.rejection_reason
    assert "already exists" in result.rejection_reason

    # The incumbent is what the store already promised to keep. A duplicate
    # arriving late must not cost it its folder.
    assert (incumbent_folder / RECORD_NAME).exists()
    assert manager.get_live(taken).case_folder == incumbent_folder
