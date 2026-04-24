#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CachedFileFolders Tutorial: Microsoft 365 Mailbox Processing Pipeline

Scenario: a scheduled job scans a CachedFileFolders cache that is already
populated with Outlook `.eml` files and attachments (synchronized by a separate
process such as `outlook_email_sync.py`).

Focus:
    1. Iterate cached email files using `CacheGrouping.files(ref_path_glob)`,
       rather than reacting to live change events.
    2. Inspect `PrimitiveEventLog` entries (`CachedFileRef.event_log()`) to
       determine whether an email still requires processing, logging each stage
       of the workflow as we go.
    3. Persist business decisions and audit data with `CachedFileRef.metadata()`
       (remember to call `overwrite_source_file()` to write changes).
    4. Demonstrate attachment access patterns and follow-up flag management
       without exposing production-specific APIs.

Microsoft follow-up flag semantics used here:
    - Blank  → No automated decision yet.
    - To-do  → Requires human review (system determined the message is out of
               scope or encountered an error).
    - Done   → Automated processing completed successfully.

Keep in mind that this is an illustrative tutorial: the “business logic” is
deliberately lightweight and uses placeholder functions with descriptive names.
Swap in the real integrations when adapting this example for production.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Iterable, Optional

# Add src to path for imports when running the script directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping
from totodev_pub.cached_file_folders_support.cached_file_ref import CachedFileRef


# =============================================================================
# CONFIGURATION - Update to match your environment and cache layout
# =============================================================================

CACHE_ROOT = "./email_cache"  # Existing cache populated by outlook_email_sync.py
OWNER_EMAIL = "user@company.com"  # Mailbox owner (matches grouping key)
MAIL_FOLDER = "Inbox"  # Folder within the mailbox we want to monitor
POLL_INTERVAL_SECONDS = 15  # How often the job wakes up to inspect the cache


# Grouping pattern mirrors the layout produced by outlook_email_sync.py
GROUPING_PATTERN = "{owner_email}/{server_folder}/"

# Event log label for this tutorial – keeps the log easy to read
PROCESSING_LABEL = "MAIL-PROCESSING"


class FollowUpState(Enum):
    """Application-facing view of Outlook follow-up statuses."""

    BLANK = auto()
    TO_DO = auto()
    DONE = auto()


class EmailClassification(Enum):
    """Outcome of the tutorial's placeholder classification step."""

    RELEVANT = auto()
    IRRELEVANT = auto()
    ERROR = auto()


@dataclass(slots=True)
class ProcessorContext:
    """Shared configuration for the mail processor."""

    grouping: CacheGrouping
    poll_interval: float = POLL_INTERVAL_SECONDS


class MailProcessor:
    """Encapsulates the recurring cache scan and email processing pipeline."""

    def __init__(self, context: ProcessorContext) -> None:
        self._context = context

    async def run(self) -> None:
        """Continuously process cached emails at the configured interval."""

        while True:
            await self._process_cached_emails()
            await asyncio.sleep(self._context.poll_interval)

    async def _process_cached_emails(self) -> None:
        """Inspect the cache for `.eml` files that require processing."""

        for ref in self._context.grouping.files(ref_path_glob="*.eml"):
            if self._should_skip(ref):
                continue

            try:
                self._record_stage(ref, "INTAKE-START", {"seen_at": datetime.now().isoformat()})

                classification = self._classify_email(ref)
                self._record_stage(ref, "CLASSIFIED", {"classification": classification.name})

                if classification is EmailClassification.IRRELEVANT:
                    self._update_follow_up_flag(ref, FollowUpState.TO_DO)
                    self._write_metadata(ref, status="human-review", classification=classification.name)
                    self._record_stage(ref, "DONE", {"reason": "Not applicable"})
                    continue

                if classification is EmailClassification.ERROR:
                    self._update_follow_up_flag(ref, FollowUpState.TO_DO)
                    self._write_metadata(ref, status="error", classification=classification.name)
                    self._record_stage(ref, "RECOVERY-QUEUED", {"reason": "Classification error"})
                    continue

                attachments = self._collect_attachments(ref)
                self._record_stage(ref, "ACTION-START", {"attachments": len(attachments)})

                self._execute_action(ref, attachments)

                self._update_follow_up_flag(ref, FollowUpState.DONE)
                self._write_metadata(
                    ref,
                    status="done",
                    classification=classification.name,
                    attachments=[str(p) for p in attachments],
                    completed_at=datetime.now().isoformat(),
                )
                self._record_stage(ref, "DONE", {"completed_at": datetime.now().isoformat()})

            except Exception as exc:  # noqa: BLE001 - tutorial intentionally broad
                self._update_follow_up_flag(ref, FollowUpState.TO_DO)
                self._write_metadata(
                    ref,
                    status="error",
                    classification="ERROR",
                    error=str(exc),
                )
                self._record_stage(ref, "ERROR", {"message": str(exc)})

    def _should_skip(self, ref: CachedFileRef) -> bool:
        """Check the event log to avoid reprocessing completed emails."""

        values = ref.event_log().latest_values()
        return values.get(PROCESSING_LABEL) == "DONE"

    def _record_stage(self, ref: CachedFileRef, value: str, payload: Optional[dict] = None) -> None:
        """Helper for writing a stage marker to the per-email event log."""

        ref.event_log().create_event(PROCESSING_LABEL, value, payload)

    def _classify_email(self, ref: CachedFileRef) -> EmailClassification:
        """Placeholder for real classification logic (OCR, NLP, rules, etc.)."""

        # Tutorials often inject deterministic behavior. Here we inspect metadata to
        # simulate idempotency (e.g., skip reclassification if already decided).
        metadata = ref.metadata(default_data={}).as_dict()
        if metadata.get("status") == "done":
            return EmailClassification.RELEVANT

        # Replace with your actual business rules. For now, everything is relevant.
        return EmailClassification.RELEVANT

    def _execute_action(self, ref: CachedFileRef, attachments: Iterable[Path]) -> None:
        """Dummy work to illustrate downstream actions triggered by emails."""

        # Examples of real actions:
        #   - Create/update a record in your system of record
        #   - Send webhooks or kick off background jobs
        #   - Upload attachments for additional processing
        # This tutorial leaves the implementation detail to the reader.
        _ = (ref, list(attachments))

    def _update_follow_up_flag(self, ref: CachedFileRef, state: FollowUpState) -> None:
        """Stand-in for Microsoft Graph / EWS follow-up flag updates."""

        # In production, call the actual API (e.g., Graph `updateMessage`) to set:
        #   - Blank when no decision yet
        #   - To-do for human attention (irrelevant or failed processing)
        #   - Done once automation completes successfully
        # We only emit a log entry here to show intent.
        self._record_stage(ref, "FOLLOW-UP", {"state": state.name})

    def _write_metadata(self, ref: CachedFileRef, **fields: object) -> None:
        """Persist processing context alongside the cached email."""

        meta = ref.metadata(default_data={})
        current = meta.as_dict()
        current.update(fields)
        current.setdefault("last_updated", datetime.now().isoformat())
        meta.overwrite_source_file(current)

    def _collect_attachments(self, ref: CachedFileRef) -> list[Path]:
        """Gather attachment file paths stored alongside the cached email."""

        attachments_dir = ref.file_path.parent / f"{ref.file_path.name}_files"
        if not attachments_dir.exists():
            return []

        return sorted(p for p in attachments_dir.iterdir() if p.is_file())


async def main() -> None:
    """Entry point: build the cache grouping and start the processor."""

    cache = CachedFileFolders(
        grouping_pattern=GROUPING_PATTERN,
        root_dir=os.path.abspath(CACHE_ROOT),
        use_xxhash=False,
    )

    grouping = cache.grouping([OWNER_EMAIL, MAIL_FOLDER])

    # Grouping-level slave directory is a great place for auxiliary state. If you
    # support purge/retention, consult the cache's `purge()` helpers. A production
    # version might clear fully processed prior-day folders during maintenance.

    processor = MailProcessor(ProcessorContext(grouping=grouping))
    await processor.run()


if __name__ == "__main__":
    if OWNER_EMAIL == "user@company.com":
        print("❌ Please customize the configuration constants before running this tutorial")
        sys.exit(1)

    asyncio.run(main())

