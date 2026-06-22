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

from typing import Optional, Dict, Any, Generator, NamedTuple
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path


class LocalRetentionRecommendation(Enum):
    """A proxy's recommendation for how much of a file to retain locally.

    The three members are points on a single axis -- how much of the file the
    cache keeps locally: the full body, only metadata (truncated), or nothing at
    all. The members are imperative *advice*: the type is a recommendation only,
    so the cache (CachedFileFolders) has the final say and may override it via its
    own policy. Derived classes can implement simple, explicit rules (based on
    size, type, path, etc.) to express intent.

    The members map to resulting entry states: KEEP -> a full entry, TRUNCATE ->
    a truncated entry (zero-byte file plus a metadata sidecar), EXCLUDE -> no
    entry. Here the "body" is the file's content bytes, as distinguished from the
    small sidecar of metadata the cache can keep when the body is truncated away.

    Members:
        KEEP: Fetch the body and keep it on disk in full. This is the default and
            matches the library's long-standing behavior.
        TRUNCATE: Record authoritative metadata (size/mtime, optionally a hash)
            but do not keep the body. The cached file is truncated to zero bytes
            and a sidecar records the details. See the truncated-entries design notes.
        EXCLUDE: Do not bring the file into the cache at all. EXCLUDE is a
            sweep-*membership* signal honored at the driver layer, not inside the
            per-file upsert path: resync_bulk filters EXCLUDE proxies out of the
            stream before upsert (so any previously cached copy falls to the
            sweep's deletion pass), while resync_sweep leaves it to the caller
            (docstring guidance) and it is never acted on inside upsert_file().
            It exists to make an otherwise-implicit "skip this file" decision
            explicit and testable.
    """

    KEEP = "keep"
    TRUNCATE = "truncate"
    EXCLUDE = "exclude"


class OriginMetadata(NamedTuple):
    """Cheap, source-side facts about a file, returned by `peek_metadata()`.

    All fields are optional because different sources can cheaply learn different
    things: a local file knows size and mtime, an object store may only offer an
    ETag, a generated payload may know nothing until it is materialized. A proxy
    returns whatever it can determine cheaply and leaves the rest as None.

    Fields:
        size: Size in bytes, if cheaply known.
        mtime: Source modification time as a POSIX timestamp, if cheaply known.
        origin_version: An opaque, cheaply-available source-side version token such
            as an ETag or version id. Compare it; never interpret it. This is NEVER
            a locally computed hash (computing a hash requires the bytes, which
            defeats the purpose of a cheap peek). Many sources have no such token
            and leave it None.
    """

    size: Optional[int] = None
    mtime: Optional[float] = None
    origin_version: Optional[str] = None


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
    def looks_same(self, other_fpath: str, override_byte_count: Optional[int] = None) -> Optional[bool]:
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

        Args:
            other_fpath: Path to the existing cached file to compare against.
            override_byte_count: Optional substitute for the on-disk size of
                `other_fpath`. When the cache compares against a *truncated* entry,
                the file at `other_fpath` has been shrunk to zero bytes while its
                mtime is preserved as the recorded source mtime. In that case the
                cache passes the recorded (pre-truncation) size here so that
                size-based comparisons remain correct without materializing the
                body -- the mtime read from disk is already authoritative, so size
                is the only input that needs substituting. When None (the default),
                implementations use the actual on-disk size.

                Notes on applicability:
                - Proxies that compare by size (and/or mtime) should honor this and
                  use it in place of `os.stat(other_fpath).st_size`.
                - Proxies that compare by *content* rather than size (e.g. email
                  proxies that parse an injected header) cannot be compared cheaply
                  while the body is truncated; they may ignore this argument and
                  will naturally report a difference, prompting re-materialization.
                - It is irrelevant under `use_xxhash=True`, where comparison hashes
                  bytes that a truncated entry does not have on disk.
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

        TIP ON mtime: The source modification time is often only available here
        (e.g. a remote API returns the file's modified date when you fetch it),
        not later in deploy(). If a meaningful source timestamp is available,
        capture it during materialization (store it on the instance, or stamp
        the temp file) so deploy() can apply it to the deployed file. The
        authoritative mtime guarantee still belongs to deploy() because that is
        the file the cache observes—see deploy() for details.
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def get_context_info(self) -> Dict[str, Any]:
        """Return safe context information for logging/debugging.
        
        Should return a dictionary of key-value pairs that are safe to log.
        Avoid sensitive information like passwords, tokens, etc.
        """
        raise NotImplementedError("Not implemented")
    
    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        """Recommend how much of this file the cache should retain locally.

        Returns one of:
        - `LocalRetentionRecommendation.KEEP` (default): fetch and keep the body.
        - `LocalRetentionRecommendation.TRUNCATE`: record metadata only, no body.
        - `LocalRetentionRecommendation.EXCLUDE`: do not bring the file into the cache.

        This is a recommendation; the cache may override it with its own policy.
        Override this in derived classes to express simple, explicit rules (for
        example: exclude files over a size threshold, or truncate everything under an
        archive path). The default keeps the library's historical behavior.

        IMPORTANT (EXCLUDE semantics): EXCLUDE is a sweep-membership signal honored
        at the driver layer, not inside the per-file upsert path. resync_bulk filters
        EXCLUDE proxies out before upsert, so a previously cached entry that now
        recommends EXCLUDE is left untouched and is swept (deleted) on the next sweep
        just like any untouched entry. resync_sweep does not act on it automatically
        (the caller decides), and it is never acted on inside upsert_file().
        """
        return LocalRetentionRecommendation.KEEP

    async def peek_metadata(self) -> Optional[OriginMetadata]:
        """Cheaply probe source-side metadata (size/mtime/origin_version) without materializing.

        This is the *cheap* metadata tier. It must not download or generate the
        file. Return an `OriginMetadata` with whatever is cheaply known (any field
        may be None), or return `None` to indicate "nothing is cheaply known" - in
        which case the cache falls back to the normal materialize-and-compare path.
        This mirrors the `looks_same() -> Optional[bool]` philosophy where None
        means "I can't tell; do it the expensive way."

        Implementations that perform I/O (for example, a metadata API call) should
        memoize the result: proxies are short-lived, single-use objects and
        `peek_metadata()` may be consulted more than once during a sync.

        The base implementation returns None. It deliberately does NOT materialize
        the file to measure it; when a truncated entry must be produced for a proxy
        that cannot peek cheaply, the cache is responsible for measuring the bytes it
        already materializes to a temporary location.

        Relationship to `looks_same()`: `looks_same()` remains the authoritative
        cheap comparison hook today. A future base-class `looks_same()` expressed in
        terms of `peek_metadata()` is anticipated but not implemented here.
        """
        return None

    def retrieval_hint(self) -> Dict[str, Any]:
        """Return an arbitrary, JSON-serializable blob describing how the original could be re-fetched.

        This is purely informational. It does NOT implement re-materialization; it
        merely *facilitates* it by recording where the file came from so future
        tooling (or a human) could reconstruct a proxy and re-fetch the bytes. It is
        the natural place to stash an origin path, URL, message id, drive id, etc.

        The default returns the proxy's own reference path. Override to provide
        richer, source-specific retrieval information.
        """
        return {"ref_path": self.ref_path()}

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



