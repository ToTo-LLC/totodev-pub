# Part of the totodev_pub library.

"""The layout drift guard: declaration vs. disk, and declaration vs. document.

Three things could disagree about the manager's namespace — the declaration in
``namespace_map``, the directories a provisioned filespace actually contains, and
the tree printed in ``docs/case-manager-layout.md``. These tests pin all three
together, so a layout change that misses one of them fails here rather than
misleading a reader months later.

This is deliberately a *characterization* suite. It asserts the layout is what it
is, not that it is good. When the layout is intentionally changed, the expected
values below are meant to change with it — the point is that the change becomes
visible and deliberate instead of silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from case_manager_test_utils import (
    TicketCase,
    adopt_into_live,
    attach_adapter,
    provision_manager,
    seed_detached_case,
)
from totodev_pub.case_manager import CaseManager
from totodev_pub.case_manager_support.case_manager_client import CaseManagerClient
from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy
from totodev_pub.case_manager_support.mailbox import MailboxTransport
from totodev_pub.case_manager_support.namespace_map import (
    BEGIN_MARKER,
    END_MARKER,
    EntryKind,
    namespace_dirs_on_disk,
    namespace_entries,
    provisioned_namespace_dirs,
    render_layout_map,
    replace_generated_block,
    storage_entries,
    undeclared_dirs,
)

LAYOUT_DOC = Path(__file__).resolve().parents[1] / "docs" / "case-manager-layout.md"

#: The namespace as it stands today, spelled out rather than derived, so a change
#: to the declaration has to be restated here as well. A test that recomputed
#: this from the same source it is checking would pass no matter what happened.
EXPECTED_NAMESPACE_DIRS = (
    "staging",
    "adopt_drop",
    "fire_mailbox",
    "fire_mailbox/intake",
    "fire_mailbox/malformed",
    "adopt_mailbox",
    "adopt_mailbox/intake",
    "adopt_mailbox/pending",
    "reclassify_mailbox",
    "reclassify_mailbox/intake",
    "reclassify_mailbox/malformed",
    "reclassify_mailbox/executing",
    "shutdown_mailbox",
    "shutdown_mailbox/intake",
    "results",
    "termination",
    "termination/pending",
    "termination/failed",
    "eject",
    "eject/pending",
    "eject/failed",
    "quarantine",
    "quarantine/pending",
    "quarantine/failed",
)


def test_declaration_matches_the_expected_namespace():
    """The declaration is the 24 directories listed above, in parent-before-child order."""
    assert provisioned_namespace_dirs(CaseManagerPolicy()) == EXPECTED_NAMESPACE_DIRS


def test_provisioning_creates_exactly_the_declared_directories(tmp_path):
    """A fresh filespace contains the declared set and nothing else.

    Both directions matter. A missing directory is a provisioning bug; an extra
    one is a directory some other code path created behind the declaration's
    back, which is how a two-dozen-directory namespace becomes untrackable.
    """
    manager = provision_manager(tmp_path)
    on_disk = namespace_dirs_on_disk(manager._manager_dir)

    assert set(on_disk) == set(EXPECTED_NAMESPACE_DIRS), (
        f"undeclared on disk: {sorted(set(on_disk) - set(EXPECTED_NAMESPACE_DIRS))}; "
        f"declared but absent: {sorted(set(EXPECTED_NAMESPACE_DIRS) - set(on_disk))}"
    )


def test_declared_parents_precede_their_children():
    """Order is load-bearing: the renderer derives depth and sibling position from it."""
    seen: set[str] = set()
    for path in provisioned_namespace_dirs(CaseManagerPolicy()):
        parent = "/".join(path.split("/")[:-1])
        assert not parent or parent in seen, f"{path} appears before its parent {parent}"
        seen.add(path)


def test_runtime_dirs_are_absent_from_a_fresh_filespace(tmp_path):
    """``(on demand)`` has to mean something — these must not be pre-created.

    ``fire_mailbox/firing/`` existing on a filespace that has never fired a step
    would make "a file left in firing/ after a restart" unreadable as a signal.
    """
    manager = provision_manager(tmp_path)
    on_disk = set(namespace_dirs_on_disk(manager._manager_dir))
    runtime = [
        e.path for e in namespace_entries(CaseManagerPolicy()) if e.kind is EntryKind.RUNTIME_DIR
    ]

    assert runtime, "no runtime dirs declared; this test would be vacuous"
    for path in runtime:
        assert path not in on_disk, f"{path} is marked (on demand) but was created eagerly"


def test_only_the_live_bucket_is_created_eagerly(tmp_path):
    """Storage buckets are the store's, and only ``live/`` arrives with the filespace."""
    manager = provision_manager(tmp_path)
    root = manager._cache_root
    eager = [e for e in storage_entries(manager._policy) if e.kind is EntryKind.DIR]

    assert [e.path for e in eager] == ["live"]
    assert (root / "live").is_dir()
    assert not (root / "terminated").exists()
    assert not (root / "quarantined").exists()


def test_layout_document_matches_the_renderer():
    """``docs/case-manager-layout.md`` is generated; a hand edit fails here.

    The failure message names the exact command that fixes it, because the right
    response to this test failing is almost always to regenerate rather than to
    reason about the diff.
    """
    document = LAYOUT_DOC.read_text(encoding="utf-8")
    expected = replace_generated_block(document, render_layout_map(CaseManagerPolicy()))

    assert document == expected, (
        "docs/case-manager-layout.md is out of date. Regenerate it with:\n"
        "  uv run python -m totodev_pub.case_manager_support.namespace_map "
        "docs/case-manager-layout.md"
    )


def test_layout_document_carries_both_markers():
    """Without both markers the regeneration command cannot find its block."""
    document = LAYOUT_DOC.read_text(encoding="utf-8")
    assert document.count(BEGIN_MARKER) == 1
    assert document.count(END_MARKER) == 1
    assert document.index(BEGIN_MARKER) < document.index(END_MARKER)


def test_replace_generated_block_is_idempotent():
    """Regenerating twice must not accumulate blocks or drift whitespace."""
    tree = render_layout_map(CaseManagerPolicy())
    once = replace_generated_block(f"before\n{BEGIN_MARKER}\nstale\n{END_MARKER}\nafter\n", tree)
    twice = replace_generated_block(once, tree)

    assert once == twice
    assert once.count(BEGIN_MARKER) == 1
    assert once.startswith("before\n") and once.endswith("after\n")
    assert "stale" not in once


def test_replace_generated_block_refuses_a_document_without_markers():
    """A missing marker is a broken document, not a reason to append a second tree."""
    with pytest.raises(ValueError, match="missing"):
        replace_generated_block("no markers here\n", "tree")


def test_renaming_a_layout_field_moves_the_whole_subtree(tmp_path):
    """Layout fields are policy, not literals — a renamed mailbox must follow.

    This is the property Change 1 depends on: if the tree were built from
    hardcoded names anywhere, a rename would provision one tree and document
    another.
    """
    manager = provision_manager(tmp_path, fire_mailbox_subdir="requests_fire")
    on_disk = set(namespace_dirs_on_disk(manager._manager_dir))

    assert "requests_fire/intake" in on_disk
    assert not any(p.startswith("fire_mailbox") for p in on_disk)
    assert provisioned_namespace_dirs(manager._policy).count("requests_fire/intake") == 1

    # The tree prints one basename per line, so the renamed root appears on its
    # own line and the old name must be gone from the document entirely.
    tree = render_layout_map(manager._policy)
    assert "requests_fire/" in tree
    assert "fire_mailbox" not in tree


def test_transport_ensure_dirs_creates_only_declared_directories(tmp_path):
    """``MailboxTransport.ensure_dirs`` is a *second* list of the same directories.

    Provisioning is not the only thing that creates them — the transport creates
    its own subset on every maintenance tick and every submit. That second list is
    hand-written and independent, so without this test the two can drift and the
    map silently stops describing what a running manager has on disk.

    Change 1 rewrites both lists, which is exactly when drift would happen.
    """
    manager = provision_manager(tmp_path)
    transport = MailboxTransport(manager._manager_dir, manager._policy)

    transport.ensure_dirs()

    assert undeclared_dirs(manager._manager_dir, manager._policy) == ()


@pytest.mark.asyncio
async def test_a_real_run_creates_no_undeclared_directories(tmp_path):
    """The end-to-end check: adopt, attach, fire, and still nothing undeclared.

    Provisioning-time assertions cannot see directories created by the per-case
    stage moves (``pending/{case_id}``, ``firing/{case_id}``), so this drives a
    real fire to completion and then audits the whole namespace. It is also what
    proves the ``(on demand)`` annotations describe reality rather than intent.
    """
    manager = provision_manager(tmp_path, enable_mailbox=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    seed_detached_case(TicketCase, seed / "c1")
    await manager.recover()
    case = await adopt_into_live(manager, seed / "c1")
    attach_adapter(manager)
    await manager.start()
    try:
        client = CaseManagerClient(tmp_path / "cache")
        handle = client.submit_fire(case_id=case.case_id, trigger="work", only_if_fresh=False)
        result = await client.wait_result(handle, timeout=5.0)
        assert result is not None, "the fire never returned a result; the audit would be vacuous"

        on_disk = set(namespace_dirs_on_disk(manager._manager_dir))
        assert undeclared_dirs(manager._manager_dir, manager._policy) == ()
        # The per-case stage dirs must actually have appeared, or this test would
        # pass just as happily against a manager that never ran anything.
        assert any(p.startswith("fire_mailbox/firing/") for p in on_disk)
    finally:
        await manager.stop()


def test_ensure_namespace_dirs_is_idempotent(tmp_path):
    """Called on every construction, so a second call must be a no-op."""
    manager = provision_manager(tmp_path)
    before = namespace_dirs_on_disk(manager._manager_dir)

    CaseManager._ensure_namespace_dirs(manager._manager_dir, manager._policy)

    assert namespace_dirs_on_disk(manager._manager_dir) == before
