# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Capture-and-reapply helper for change receiver workflows.

When CachedFileFolders emits a ChangeNotice during delete/update operations, the `old`
file is staged only while the change_receiver callback runs. As soon as the callback
returns, the cache performs its normal cleanup and permanently removes that staged copy.

`SavedUpsert` preserves the staged artifact by immediately copying it into the cache's
temp area, letting callers re-upsert the content later (for example, to move a file that
would otherwise be deleted during sweep cleanup).

Although you *could* queue up several SavedUpserts and apply them in bulk, the intended
use-case is rescuing an old file/slave directory from automatic deletion and re-inserting
it elsewhere once the change receiver has finished.

This class is necessary because attempting to upsert inside of a change receiver callback 
would result in implementation difficulties related to async methods and race condiions.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .change_notice import ChangeNotice
from .file_proxy_local_file import LocalFileProxy

GroupingKey = Optional[Sequence[str]]


@dataclass
class SavedUpsert:
    """
    Captures a staged file/slave directory so it can be re-upserted later.
    """

    _file_path: Path
    _slave_dir_path: Optional[Path]
    _ref_path: str
    _grouping_key: GroupingKey
    _preserve_slave_dir: bool
    _consumed: bool = False

    @classmethod
    def from_change_notice(
        cls,
        cache: "CachedFileFolders",
        notice: ChangeNotice,
        *,
        new_ref_path: Optional[str] = None,
        grouping_key_override: GroupingKey = None,
        preserve_slave_dir: bool = True,
    ) -> "SavedUpsert":
        """
        Capture the staged artifact referenced by `notice.old`.

        Args:
            cache: CachedFileFolders instance (used for temp directory access).
            notice: ChangeNotice whose `old` artifact should be preserved.
            new_ref_path: Optional ref_path to use when re-upserting (defaults to notice.old.ref_path).
            grouping_key_override: Optional grouping key override (defaults to notice.old.grouping_key).
            preserve_slave_dir: Copy slave directory contents when available.
        """
        if notice.old is None:
            raise ValueError("SavedUpsert requires a ChangeNotice with an 'old' reference (UPDATE or DELETE).")

        source_file_path = notice.old.file_path
        if source_file_path is None or not source_file_path.exists():
            raise FileNotFoundError("Staged file is no longer available for capture.")

        temp_root = cache.get_temp_directory_root()
        unique_dir = temp_root / f"saved_upsert_{uuid.uuid4().hex}"
        unique_dir.mkdir(parents=True, exist_ok=True)

        target_file = unique_dir / source_file_path.name
        shutil.copy2(source_file_path, target_file)

        slave_path: Optional[Path] = None
        if preserve_slave_dir and notice.old.slave_dir_path and notice.old.slave_dir_path.exists():
            slave_path = unique_dir / notice.old.slave_dir_path.name
            shutil.copytree(notice.old.slave_dir_path, slave_path)

        ref_path = new_ref_path if new_ref_path is not None else notice.old.ref_path
        grouping_key = grouping_key_override if grouping_key_override is not None else notice.old.grouping_key

        return cls(
            _file_path=target_file,
            _slave_dir_path=slave_path,
            _ref_path=ref_path,
            _grouping_key=grouping_key,
            _preserve_slave_dir=preserve_slave_dir and slave_path is not None,
        )

    @property
    def ref_path(self) -> str:
        return self._ref_path

    @property
    def grouping_key(self) -> GroupingKey:
        return self._grouping_key

    def was_cleaned_up(self) -> bool:
        """
        Return True if the saved artifacts have been cleaned up by cache maintenance.
        """
        return not self._file_path.exists()

    def discard(self) -> None:
        """
        Delete any saved artifacts without re-upserting.
        """
        if self._file_path.exists():
            try:
                self._file_path.unlink()
            except FileNotFoundError:
                pass
        if self._slave_dir_path and self._slave_dir_path.exists():
            shutil.rmtree(self._slave_dir_path, ignore_errors=True)
        if self._file_path.parent.exists():
            try:
                self._file_path.parent.rmdir()
            except OSError:
                pass
        self._consumed = True

    async def upsert(
        self,
        cache: "CachedFileFolders",
        *,
        grouping_key: GroupingKey = None,
        force: bool = False,
        change_receiver=None,
        preserve_slave_dir: Optional[bool] = None,
        delete_after: bool = True,
    ) -> Optional[ChangeNotice]:
        """
        Upsert the saved artifact back into the cache.

        Args:
            cache: CachedFileFolders instance.
            grouping_key: Grouping key for the upsert (defaults to captured grouping).
            force: Whether to bypass change detection.
            change_receiver: Optional callback identical to cache.upsert_file.
            preserve_slave_dir: Override whether slave contents are copied (defaults to capture-time flag).
            delete_after: Delete saved artifacts after upsert (defaults to True).
        """
        if self._consumed:
            raise RuntimeError("SavedUpsert has already been consumed or discarded.")

        if not self._file_path.exists():
            raise RuntimeError(
                "SavedUpsert artifact missing. CachedFileFolders cleans SavedUpsert temp files "
                "after a short grace period (typically a few minutes). Capture a fresh SavedUpsert "
                "if you need to reapply later."
            )

        effective_grouping = grouping_key if grouping_key is not None else self._grouping_key
        effective_slave_copy = self._preserve_slave_dir if preserve_slave_dir is None else preserve_slave_dir

        proxy = LocalFileProxy(str(self._file_path), ref_path=self._ref_path, delete_after_deploy=delete_after)
        notice = await cache.upsert_file(
            proxy,
            grouping_key=effective_grouping,
            force=force,
            change_receiver=change_receiver,
        )

        if notice and effective_slave_copy and self._slave_dir_path and self._slave_dir_path.exists():
            self._copy_slave_dir(self._slave_dir_path, notice.cur.slave_dir_path if notice.cur else None)

        if delete_after:
            self.discard()
        else:
            self._consumed = True

        return notice

    @staticmethod
    def _copy_slave_dir(source: Path, target: Optional[Path]) -> None:
        if target is None:
            return
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            dest = target / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)


# Avoid circular imports
from totodev_pub.cached_file_folders import CachedFileFolders  # noqa: E402

