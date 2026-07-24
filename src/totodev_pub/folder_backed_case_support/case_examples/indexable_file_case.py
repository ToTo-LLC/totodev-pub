# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Index lifecycle for one file in an organization MCP file-access server.

Organizations often expose a read-only MCP server over a client (or project /
work-order / department) file store. That server understands the client's folder
conventions, maps casual names to the right subtree, and searches by filename,
type, and content — typically via vector or graph indexing. Each recognized
source path gets an ``IndexableFileCase`` whose job is to create, refresh, and
eventually remove that file's index entries in the shared store.

The case's external key is the native path; there is no "move" (delete +
create). Index bookkeeping uses ``index_active/`` (live delete keys) and
``index_retire/`` (superseded keys still searchable until
``cleanup_retired_indexes`` drains them). Textual content held during
processing is ephemeral and must not outlive a successful index commit.

IMPORTANT: This module is a rough first draft. Expect substantial follow-on work
before production use — refining data structures, implementing trigger/guard/
assertion bodies, tuning ``fsm_state_chains``, and hardening asset contracts.
See ``fsm_state_chains`` on the case class for the lifecycle declaration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from transitions.core import EventData

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case import FolderBackedCase
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.case_type_registry import case_type_registry

# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


class FileFingerprint(BaseModel, FileMappedPydanticMixin):
    """Identity snapshot of the source file at examine time.

    TODO: replace this placeholder attribute layout with the real fingerprint
    fields the indexer will persist (path/size/mtime/hash are a starting guess).
    """

    source_path: str = ""
    size_bytes: int = 0
    mtime_iso: str = ""
    content_hash: str = ""


class FileSummary(BaseModel, FileMappedPydanticMixin):
    """LLM summary of the file, informed by name/type/location rules.

    TODO: replace this placeholder attribute layout with the summary schema
    the product actually needs (fields below are a starting guess).
    """

    text: str = ""
    source_path: str = ""
    file_type: str = ""
    rules_applied: list[str] = Field(default_factory=list)


class IndexEntryRef(BaseModel, FileMappedPydanticMixin):
    """Enough identity to delete one index entry when it becomes invalid.

    One yaml per ref under ``index_active/`` or ``index_retire/``. Written after
    a successful upsert into the shared index; moved (renamed) to retire when
    superseded or when the source file is deleted; removed from retire after an
    idempotent DB delete in ``cleanup_retired_indexes``.

    TODO: replace this placeholder attribute layout with whatever the index
    backend needs to delete precisely (id, collection, namespace, vector key, …).
    """

    entry_id: str = ""
    kind: str = ""  # e.g. "chunk", "summary"


@case_type_registry.register
class IndexableFileCase(FolderBackedCase):
    """Manage the index lifecycle for one source file: when the system
    recognizes that a file exists, a case is created for it; the file is
    examined, made textual, summarized, and indexed, then typically rests in
    ``indexed`` for a long time. A later change re-enters that pipeline and
    returns to ``indexed``. Deletion moves the case through ``deleted``
    (draining retired index shards) to terminal ``purged``, after which the case
    can be discarded. The case keeps enough index identity under
    ``index_active/`` and ``index_retire/`` to remove superseded or obsolete
    entries from the shared store.
    """

    # =======================================================================
    # Declarations
    # =======================================================================

    fsm_state_chains = """
        %% Intake: inert new; examine_file carries the readable filename in kwargs.
        [*] --> new == examine_file ==> examined
        %% Optional OCR vs direct extract — both land at textual_content_available.
        examined -- apply_ocr [needs_ocr] --> textual_content_available
        examined -- extract_text [no_ocr_needed] --> textual_content_available
        textual_content_available -- summarize --> summarized
        %% commit_index: rename active→retire, write new active refs, purge text.
        summarized -- commit_index --> indexed
        %% Change parking, then re-enter the same pipeline via examine_file.
        indexed == mark_changed ==> changed
        changed == examine_file ==> examined
        %% Retire drain at rest points only (not mid-pipeline).
        indexed -- cleanup_retired_indexes [has_retired_indexes] --> indexed
        deleted -- cleanup_retired_indexes [has_retired_indexes] --> deleted
        %% Delete: move active→retire, drain, then seal (deleted cannot be terminal
        %% while cleanup still needs a self-loop).
        * == mark_deleted ==> deleted
        deleted -- complete_deletion [no_retired_indexes] --> purged --> [*]
    """

    asset_aliases = {
        "fingerprint": AssetSpec(
            relative_path="fingerprint.yaml",
            loader=FileFingerprint,
            trust_states=frozenset({
                "examined",
                "textual_content_available",
                "summarized",
                "indexed",
                "changed",
            }),
            keep=False,
        ),
        "textual_content": AssetSpec(
            relative_path="textual_content.txt",
            loader=Path,
            trust_states=frozenset({
                "textual_content_available",
                "summarized",
            }),
            keep=False,
        ),
        "summary": AssetSpec(
            relative_path="summary.yaml",
            loader=FileSummary,
            trust_states=frozenset({
                "summarized",
                "indexed",
                "changed",
            }),
            keep=False,
        ),
        "index_active": AssetSpec(
            relative_path="index_active/*.yaml",
            loader=IndexEntryRef,
            trust_states=frozenset({
                "indexed",
                "changed",
            }),
            keep=False,
            many=True,
        ),
        "index_retire": AssetSpec(
            relative_path="index_retire/*.yaml",
            loader=IndexEntryRef,
            trust_states=frozenset({
                "indexed",
                "changed",
                "deleted",
            }),
            keep=False,
            many=True,
        ),
    }

    fsm_trigger_chokes = {
        "apply_ocr": {"cpu"},
        "extract_text": {"cpu"},
        "summarize": {"llm"},
        "commit_index": {"db"},
        "cleanup_retired_indexes": {"db"},
    }

    def _not_implemented(self, retval: Any = None) -> Any:
        """Scaffolding helper for stub hooks; remove with the stubs that call it."""
        caller = sys._getframe(1).f_code.co_qualname
        self.log.warning("STUB not implemented: %s", caller)
        return retval

    # =======================================================================
    # perform_<trigger>
    # =======================================================================

    async def perform_examine_file(
        self, tctx: EventData, *, path: str,
    ) -> None:
        """TODO(responsibility): read the source file named by `path` and
        write/update the `fingerprint` asset. No OCR, text extract, LLM, index
        write, or active→retire rename here — supersede happens in
        `commit_index` / `mark_deleted`.
        """
        return self._not_implemented(None)

    async def perform_apply_ocr(self, tctx: EventData) -> None:
        """TODO(responsibility): run OCR on the examined source (threaded/
        process as needed), write the resulting text into `textual_content`.
        Contends for the `cpu` choke.
        """
        return self._not_implemented(None)

    async def perform_extract_text(self, tctx: EventData) -> None:
        """TODO(responsibility): convert the examined source to a text-friendly
        form (e.g. pandoc / native PDF text layer) and write `textual_content`.
        Contends for the `cpu` choke.
        """
        return self._not_implemented(None)

    async def perform_summarize(self, tctx: EventData) -> None:
        """TODO(responsibility): generate the `summary` asset from
        `textual_content` plus filename/type/location rules. Contends for
        the `llm` choke.
        """
        return self._not_implemented(None)

    async def perform_commit_index(self, tctx: EventData) -> None:
        """TODO(responsibility): rename any `index_active/*.yaml` into
        `index_retire/` (unique names OK), chunk `textual_content` and
        `summary`, upsert into the shared index, write one `IndexEntryRef` yaml
        per successful upsert under `index_active/`, and purge `textual_content`.
        Does not wait for retire DB deletion — that is
        `cleanup_retired_indexes`. Contends for the `db` choke.
        """
        return self._not_implemented(None)

    async def perform_cleanup_retired_indexes(self, tctx: EventData) -> None:
        """TODO(responsibility): for each `index_retire/*.yaml`, idempotently
        delete that row/node from the shared index (tolerate already-gone),
        then remove that yaml; leave leftovers for the next run. Contends for
        the `db` choke. Fired from resting states (`indexed`, `deleted`) when
        retire shards are present.
        """
        return self._not_implemented(None)

    async def perform_mark_changed(self, tctx: EventData) -> None:
        """TODO(responsibility): accept an external change signal and park in
        `changed` until a subsequent `examine_file` re-enters the pipeline.
        Does not move index shards; prior `index_active` stays searchable until
        the next `commit_index` renames them to retire.
        """
        return self._not_implemented(None)

    async def perform_mark_deleted(self, tctx: EventData) -> None:
        """TODO(responsibility): rename all `index_active/*.yaml` into
        `index_retire/` so `cleanup_retired_indexes` can drain them. Folder
        purge / full resource cleanup stays outside the case.
        """
        return self._not_implemented(None)

    async def perform_complete_deletion(self, tctx: EventData) -> None:
        """TODO(responsibility): seal deletion once `index_retire/` is empty
        (no further case-owned index bookkeeping). Folder purge stays outside.
        """
        return self._not_implemented(None)

    # =======================================================================
    # guard_<guard>
    # =======================================================================

    async def guard_needs_ocr(self, tctx: EventData) -> bool:
        """TODO(responsibility): return True iff the examined file requires OCR
        before text is available (e.g. scanned PDF / image). Complementary to
        `no_ocr_needed`. Stub defaults to False so simulation takes extract_text.
        """
        return self._not_implemented(False)

    async def guard_no_ocr_needed(self, tctx: EventData) -> bool:
        """TODO(responsibility): return True iff text can be extracted without
        OCR. Complementary to `needs_ocr`. Stub defaults to True for simulation.
        """
        return self._not_implemented(True)

    async def guard_has_retired_indexes(self, tctx: EventData) -> bool:
        """TODO(responsibility): return True iff any `index_retire/*.yaml`
        exists. Gates `cleanup_retired_indexes`. Stub defaults to False so
        simulation does not spin on the cleanup self-loop.
        """
        return self._not_implemented(False)

    async def guard_no_retired_indexes(self, tctx: EventData) -> bool:
        """TODO(responsibility): return True iff `index_retire/` has no yaml
        shards. Gates `complete_deletion`. Complementary to
        `has_retired_indexes`. Stub defaults to True so simulation can seal.
        """
        return self._not_implemented(True)

    # =======================================================================
    # case_assert_<state>_<slug>
    # =======================================================================

    def case_assert_new_is_inert(self, ltx) -> None | str:
        """TODO(responsibility): inert parking — no fingerprint / text /
        summary / index_active / index_retire yet."""
        return self._not_implemented(None)

    def case_assert_examined_has_fingerprint(self, ltx) -> None | str:
        """TODO(responsibility): `fingerprint` must exist and identify the
        source file just examined."""
        return self._not_implemented(None)

    def case_assert_textual_content_available_has_text(self, ltx) -> None | str:
        """TODO(responsibility): `textual_content` must exist and be non-empty."""
        return self._not_implemented(None)

    def case_assert_summarized_has_summary(self, ltx) -> None | str:
        """TODO(responsibility): `summary` must exist; `textual_content` still
        present until commit_index purges it."""
        return self._not_implemented(None)

    def case_assert_indexed_has_entries_without_text(self, ltx) -> None | str:
        """TODO(responsibility): `index_active` and `summary` present;
        `textual_content` purged; `index_retire` may still exist until cleanup."""
        return self._not_implemented(None)

    def case_assert_changed_is_parked(self, ltx) -> None | str:
        """TODO(responsibility): change signal accepted; prior fingerprint /
        summary / index_active may still exist until re-analyze + commit."""
        return self._not_implemented(None)

    def case_assert_deleted_awaiting_retire_drain(self, ltx) -> None | str:
        """TODO(responsibility): active shards already moved to retire (or none
        existed); cleanup and/or complete_deletion still outstanding."""
        return self._not_implemented(None)

    def case_assert_purged_indexes_cleared(self, ltx) -> None | str:
        """TODO(responsibility): no `index_active` or `index_retire` shards
        remain (full folder purge is outside the case)."""
        return self._not_implemented(None)
