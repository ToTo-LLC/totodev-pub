# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from __future__ import annotations

from typing import Optional

from .cache_operations_protocol import CacheOperations
from .sync_types import UpsertFailure
from .change_notice import ChangeNotice


class AsyncUpsertOperation:
    """Handles async upsert operations."""

    def __init__(
        self,
        cache: CacheOperations,
        source_file,
        ref_path: str,
        grouping_key,
        target_file_path: str,
        target_slave_dir_path: str,
        existing_file_ref,
        session,
        force: bool = False,
    ):
        self.cache = cache
        self.source_file = source_file
        self.ref_path = ref_path
        self.grouping_key = grouping_key
        self.target_file_path = target_file_path
        self.target_slave_dir_path = target_slave_dir_path
        self.existing_file_ref = existing_file_ref
        self.session = session
        self.force = force

    async def execute(self) -> Optional[ChangeNotice]:
        try:
            notice = await self.cache._upsert_file_core(
                self.source_file,
                self.ref_path,
                self.grouping_key,
                self.target_file_path,
                self.target_slave_dir_path,
                self.existing_file_ref,
                self.force,
            )
            return await self.cache._finalize_change_notice(
                notice,
                self.session.change_receiver,
                self.source_file,
            )
        except Exception as e:
            # Handle failure according to session policy
            failure = UpsertFailure(
                grouping_key=self.grouping_key,
                file_proxy=self.source_file,
                exception=e
            )
            self.session._upsert_failures_buffer.append(failure)
            
            if self.session.upsert_fail_policy == "FAIL_FAST":
                raise
            else:
                # Log the failure and return None (failure is tracked in buffer)
                import logging
                logger = logging.getLogger(__name__)
                logger.warning("Upsert failed for %s in %s: %s", 
                              self.source_file.ref_path(), self.grouping_key, e)
                return None


class AsyncDeleteOperation:
    """Handles async delete operations."""

    def __init__(self, cache: CacheOperations, ref_path, grouping_key, existing_file_ref, session):
        self.cache = cache
        self.ref_path = ref_path
        self.grouping_key = grouping_key
        self.existing_file_ref = existing_file_ref
        self.session = session

    async def execute(self) -> Optional[ChangeNotice]:
        notice = await self.cache._delete_file_core(
            self.ref_path,
            self.grouping_key,
            self.existing_file_ref,
        )
        return await self.cache._finalize_change_notice(
            notice,
            self.session.change_receiver,
            None,
        )



