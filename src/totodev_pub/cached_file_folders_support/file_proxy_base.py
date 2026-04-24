# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
`FileProxyBase` is the placeholder interface that lets `CachedFileFolders` track
remote or generated files without downloading them immediately.

Before caching data, define what the proxy represents and design a `ref_path`
schema that uniquely identifies each item. The `ref_path` should read like a
filesystem tree (URLs, mailbox/message-id pairs, etc.) because cache storage
mirrors that structure; good schemas make navigation easy, poor ones force
content inspection.

Proxies can represent SFTP directory entries, SharePoint documents, Google
Drive files, inbox messages, archive members, or API outputs—anything that can
ultimately be materialized as a file.

A proxy typically follows this lifecycle: instantiate it with a meaningful
`ref_path`; optionally call `looks_same()` for a cheap change check; call
`materialize()` once to fetch bytes into temporary storage; call `deploy()` to
place those bytes where the caller needs them. Implementations may allow repeat
materialization, but callers should assume single-use. `get_context_info()`
returns safe metadata for logging. `nested_proxies()` exposes child proxies
(e.g., email attachments, ZIP members) so container content can be traversed on
demand.

Derived classes should emit deterministic bytes when they serialize data, honor
caller-provided temp directories, and rely on `CachedFileFolders` to fall back
to hash-based comparisons when cheap checks are unavailable.

See concrete implementations such as `LocalFileProxy` in `file_proxy_local_file.py`,
`SharepointFileProxy` in `file_proxy_sharepoint.py`, `GmailEmailProxy` in
`file_proxy_gmail.py`, and `OutlookEmailProxy` in
`file_proxy_outlook_email.py`, along with the broader `file_proxy_*.py` family,
for reference patterns when authoring new proxies.
"""

from typing import Optional, Dict, Any, Generator
from abc import ABC, abstractmethod
from pathlib import Path


class FileProxyBase(ABC):
    """
    Abstraction for a file that may live remotely or be generated on-demand.
    This is a core abstraction used by CachedFileFolders to represent files that may live remotely or be generated on-demand.
    
    ⚠️  CRITICAL: The modification time (mtime) of files created by deploy() is the cornerstone of cache correctness—
    it drives sweep safety, change detection, and optimistic concurrency across the entire system. Preserve original 
    mtimes when possible or use deterministic timestamps for generated content; incorrect mtime handling will cause 
    cache operations to delete recently updated files or trigger unnecessary re-downloads.
    
    IMPLEMENTATION NOTE: When implementing derived classes that serialize data, deterministic serialization is 
    strongly encouraged to enable reliable change detection via simple file comparison tools.
    
    See SerializableDataProxy for an example implementation approach.
    """

    @abstractmethod
    def ref_path(self) -> str:
        raise NotImplementedError("Not implemented")

    def file_name(self) -> str:
        ref_path = self.ref_path()
        ref_lower = ref_path.lower()
        is_url = ref_lower.startswith("http://") or ref_lower.startswith("https://")
        separators = ['\\', '/']
        if is_url:
            separators.append('?')
        for i in range(len(ref_path) - 1, -1, -1):
            if ref_path[i] in separators:
                return ref_path[i + 1:]
        return ref_path

    @abstractmethod
    def deploy(self, target_dir: str) -> None:
        """Deploy the materialized file to the target directory.
        
        ⚠️  CRITICAL: Preserve or set the file modification time (mtime) appropriately!
        
        The mtime of the deployed file is used extensively by the cache for:
        - Change detection (has the file actually changed?)
        - Optimistic concurrency control (preventing deletion of modified files)
        - Sweep safety (detecting concurrent updates during sync operations)
        
        Implementation guidelines:
        - If copying from a remote source: preserve the remote file's mtime
        - If generating/serializing content: set mtime based on source data timestamp
        - Use os.utime() or Path.touch() to set mtime explicitly after writing
        - Do NOT use current time (time.time()) unless the content is truly "just created"
        
        Example:
            target_path = Path(target_dir) / self.file_name()
            target_path.write_bytes(content)
            # Set mtime to match source
            os.utime(target_path, (source_mtime, source_mtime))
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def looks_same(self, other_fpath: str) -> Optional[bool]:
        """
        Provides a quick (but not perfect) comparison of the file proxy with another file.  
        For example, it may look at the size and modify time of the file and conclude they are the same.

        Derived classes are free to implement this method in whatever way makes sense for their
        use case. Some implementations may compare mtime, others may use size, hashes, ETags,
        version numbers, or any other available metadata. The implementation is entirely up to
        the proxy designer.
        
        In an ideal world, this method provides a less expensive test for changes to
        files that are already in the cache to determine if retrieval is worthwhile.

        For example, many remote file storage services (e.g. Sharepoint, Dropbox, etc.) 
        can cheaply retrieve the file size and modify date without needing to pull down large files.

        If this value is not trustworthy or consistently available, users of the
        CachedFileFolders class can configure it to rely on xxhash comparison or exact comparison
        instead.
        
        IMPORTANT: Even if your looks_same() implementation doesn't use mtime, you should still
        preserve mtime in deploy() because the cache system relies on it for other operations
        (sweep safety, change detection, concurrency control, etc.).
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """Materialize the file proxy.
        
        Args:
            blocking_secs: Maximum time to wait for materialization
            temp_dir: Optional directory for temporary files during materialization.
                     If provided, use this directory and ensure filename uniqueness
                     using standard library functions like tempfile.mkstemp().
                     Derived classes are free to implement their own temp file strategy
                     and ignore this parameter if desired.

        **CRITICAL NOTE**: If a temp_dir is provided, the caller is responsible for
        cleaning up any orphaned temporary files created in that directory. Any
        derived class that does not implement its own orphaned file cleanup
        strategy should require the caller to supply a temp_dir.  Although, the
        deploy process should result in the temp file being deleted.
        On this subject, it's worth mentioning that CachedFileFolders always
        offers a temporary directory for materialization files.
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def get_context_info(self) -> Dict[str, Any]:
        """Return safe context information for logging/debugging.
        
        Should return a dictionary of key-value pairs that are safe to log.
        Avoid sensitive information like passwords, tokens, etc.
        """
        raise NotImplementedError("Not implemented")
    
    def nested_proxies(self) -> Generator['FileProxyBase', None, None]:
        """
        Yield nested file proxies (e.g., email attachments, archive contents).
        
        Default implementation yields nothing. Override in derived classes that represent
        container files with nested content (emails with attachments, ZIP archives, etc.).
        
        This enables lazy loading of nested content - the container is processed first,
        and nested items are only fetched/parsed when this method is called.
        
        The method is a generator and is NOT replayable - calling it multiple times may
        trigger repeated expensive operations (API calls, file parsing, etc.). If callers
        need to iterate over nested proxies multiple times, they should materialize the
        results into a list.
        
        Can be called recursively to traverse arbitrarily deep hierarchies.
        
        Yields:
            FileProxyBase: Nested file proxies
        
        Example - Flat traversal:
            for email_proxy in factory.scan_messages(...):
                process(email_proxy)
                for nested in email_proxy.nested_proxies():
                    process(nested)
        
        Example - Recursive traversal:
            def traverse(proxy: FileProxyBase):
                yield proxy
                for nested in proxy.nested_proxies():
                    yield from traverse(nested)
            
            all_proxies = list(traverse(root_proxy))
        """
        return
        yield  # Make this a generator that yields nothing



