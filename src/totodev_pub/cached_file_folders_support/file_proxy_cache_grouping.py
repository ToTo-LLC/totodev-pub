# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
CacheGroupingFileProxy and Factory

This module provides:
- CacheGroupingFileProxy: A proxy that represents a file (and its per-file slave_dir)
  already present inside a source CacheGrouping, intended to be cloned into a
  destination CacheGrouping. It performs rapid equality check via size+mtime and
  deploys by copying the source bytes with shutil.copy2 to preserve mtime.
- CacheGroupingFileProxyFactory: Produces a sequence of CacheGroupingFileProxy
  instances from a source CacheGrouping, applying a caller-provided ref_path
  transformation to derive the destination ref_path. The factory offers:
  - scan_files(...) and scan_files_batched(...)
  - make_change_receiver(...) to copy per-file slave_dir contents after upsert
  - copy_grouping_slave_dir(...) static method as a convenience for grouping-level
    slave_dir copying (note: grouping-level slave_dir is NOT copied by scan methods)

IMPORTANT NOTES
- Factory scan methods DO NOT transfer grouping-level slave_dir. If that is desired,
  call CacheGroupingFileProxyFactory.copy_grouping_slave_dir(...) separately.
- The destination filename comes from the destination ref_path's final segment,
  consistent with FileProxyBase.file_name() behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Generator, Iterator, List, Optional
import os
import shutil

from .file_proxy_base import FileProxyBase, OriginMetadata
from .cached_file_ref import CachedFileRef  # type: ignore
from .cache_operations_protocol import ChangeNotice  # type: ignore
from .cache_grouping import CacheGrouping


class CacheGroupingFileProxy(FileProxyBase):
    """
    Proxy representing an existing file inside a source CacheGrouping, intended to be
    copied into a destination CacheGrouping (potentially with a transformed ref_path).
    
    This proxy does not fetch or generate content; it references a local on-disk file
    (from the source grouping) and deploys via shutil.copy2 to preserve the original mtime.
    """

    def __init__(
        self,
        source_file_path: Path,
        destination_ref_path: str,
        *,
        source_slave_dir_path: Optional[Path] = None,
    ) -> None:
        self._source_file_path = Path(source_file_path)
        self._destination_ref_path = destination_ref_path
        self._source_slave_dir_path = Path(source_slave_dir_path) if source_slave_dir_path else None
        self._was_deployed = False

    def ref_path(self) -> str:
        return self._destination_ref_path

    def deploy(self, target_dir: str) -> None:
        """
        Copy the source file into the target directory using shutil.copy2 to preserve mtime.
        """
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
        if not os.path.isdir(target_dir):
            raise FileNotFoundError(f"Target directory does not exist: {target_dir}")
        target_path = Path(target_dir) / self.file_name()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._source_file_path, target_path)
        self._was_deployed = True

    def looks_same(self, other_fpath: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
        """
        Rapid comparison using size and mtime between source file and the provided path.
        """
        try:
            src_stat = os.stat(self._source_file_path)
            dst_stat = os.stat(other_fpath)
            # For a truncated entry the on-disk size is zero but the mtime is still
            # authoritative; use the recorded size when supplied.
            dst_size = dst_stat.st_size if override_byte_count is None else override_byte_count
            return (src_stat.st_size == dst_size) and (src_stat.st_mtime == dst_stat.st_mtime)
        except (OSError, IOError):
            return None

    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        # Already local; nothing to fetch.
        return True

    async def peek_metadata(self) -> Optional[OriginMetadata]:
        # The source is an on-disk file in another grouping; stat it cheaply.
        try:
            st = os.stat(self._source_file_path)
            return OriginMetadata(size=st.st_size, mtime=st.st_mtime)
        except (OSError, IOError):
            return None

    def retrieval_hint(self) -> dict:
        # Record the source grouping file location for potential re-cloning.
        return {"source": "cache_grouping", "source_file_path": str(self._source_file_path)}

    def get_context_info(self) -> dict:
        return {
            "proxy_type": "CacheGroupingFileProxy",
            "source_file_path": str(self._source_file_path),
            "destination_ref_path": self._destination_ref_path,
            "has_source_slave_dir": bool(self._source_slave_dir_path),
            "was_deployed": self._was_deployed,
        }

    # Accessor used by the factory-provided change receiver
    @property
    def source_slave_dir_path(self) -> Optional[Path]:
        return self._source_slave_dir_path


class CacheGroupingFileProxyFactory:
    """
    Factory for producing CacheGroupingFileProxy instances from a source CacheGrouping.
    
    This factory DOES NOT transfer grouping-level slave_dir contents as part of scanning.
    If needed, call copy_grouping_slave_dir(source_grouping, dest_grouping) separately.
    """

    def __init__(self, source_grouping: CacheGrouping) -> None:
        self._source_grouping = source_grouping

    def scan_files(
        self,
        ref_path_glob: Optional[str],
        ref_path_transform: Callable[[str], Optional[str]],
    ) -> Generator[CacheGroupingFileProxy, None, None]:
        """
        Iterate files in the source grouping, generating proxies for cloning.
        
        Args:
            ref_path_glob: Optional glob filter applied to source ref_paths.
            ref_path_transform: Callable that receives the source ref_path and returns
                                the destination ref_path (or None to skip the item).
        
        Yields:
            CacheGroupingFileProxy instances ready for upsert into a destination grouping.
        """
        for file_ref in self._source_grouping.files(ref_path_glob=ref_path_glob):
            # file_ref is CachedFileRef with .ref_path, .file_path, .slave_dir_path
            dest_ref = ref_path_transform(file_ref.ref_path)
            if dest_ref is None:
                continue
            yield CacheGroupingFileProxy(
                source_file_path=file_ref.file_path,
                destination_ref_path=dest_ref,
                source_slave_dir_path=file_ref.slave_dir_path,
            )

    def scan_files_batched(
        self,
        ref_path_glob: Optional[str],
        ref_path_transform: Callable[[str], Optional[str]],
        *,
        batch_size: int = 100,
    ) -> Generator[List[CacheGroupingFileProxy], None, None]:
        """
        Batched variant of scan_files for memory efficiency.
        """
        batch: List[CacheGroupingFileProxy] = []
        for proxy in self.scan_files(ref_path_glob, ref_path_transform):
            batch.append(proxy)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    @staticmethod
    def _copy_slave_dir_contents(source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            dest = target / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    def make_change_receiver(self, copy_slave_dir: bool = True) -> Callable[[ChangeNotice, Optional[FileProxyBase]], None]:
        """
        Create a change_receiver suitable for CachedFileFolders.upsert_file(...).
        
        If copy_slave_dir is True and the proxy passed back to the receiver is a
        CacheGroupingFileProxy with a source slave_dir, the contents are copied
        into the new/current file's slave_dir (notice.cur.slave_dir_path).
        """
        def _receiver(notice: ChangeNotice, proxy: Optional[FileProxyBase]) -> None:
            if not copy_slave_dir or proxy is None:
                return
            if not isinstance(proxy, CacheGroupingFileProxy):
                return
            source_slave = proxy.source_slave_dir_path
            cur_ref: Optional[CachedFileRef] = getattr(notice, "cur", None)  # type: ignore
            if source_slave is None or cur_ref is None:
                return
            if not source_slave.exists():
                return
            self._copy_slave_dir_contents(source_slave, cur_ref.slave_dir_path)
        return _receiver

    @staticmethod
    def copy_grouping_slave_dir(source_grouping: CacheGrouping, dest_grouping: CacheGrouping) -> None:
        """
        Convenience utility to copy the grouping-level slave_dir contents from source to destination.
        Note: Not used by scan methods; call explicitly if needed.
        """
        src_dir = source_grouping.get_slave_dir()
        dst_dir = dest_grouping.get_slave_dir()
        if not src_dir.exists():
            return
        dst_dir.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            dest = dst_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)


