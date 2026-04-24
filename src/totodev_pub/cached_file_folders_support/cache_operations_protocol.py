# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from __future__ import annotations

from typing import Protocol, Optional, Any, Iterable, Tuple, Sequence, Callable

from .change_notice import ChangeNotice


class CacheOperations(Protocol):
    pattern: str
    _requires_grouping: bool

    def find_file(self, ref_path: str, grouping_key: Tuple[Any, ...]) -> Optional[Any]: ...

    async def end_updates(self, handle: dict, delete_notices_only: bool = False) -> Iterable[ChangeNotice]: ...

    def _upsert_file_core(
        self,
        source_file: Any,
        ref_path: str,
        grouping_key: Tuple[Any, ...],
        target_file_path: str,
        target_slave_dir_path: str,
        existing_file_ref: Optional[Any],
    ) -> Optional[ChangeNotice]: ...

    def _delete_file_core(
        self,
        ref_path: str,
        grouping_key: Tuple[Any, ...],
        existing_file_ref: Any,
    ) -> Optional[ChangeNotice]: ...

    def _finalize_change_notice(
        self,
        notice: Optional[ChangeNotice],
        change_receiver: Optional[Callable[[ChangeNotice, Optional[Any]], None]],
        source_file: Optional[Any],
    ) -> Optional[ChangeNotice]: ...

    @property
    def _storage(self) -> Any: ...



