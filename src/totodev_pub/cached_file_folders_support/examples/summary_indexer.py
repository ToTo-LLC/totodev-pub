#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SummaryIndexer -- the source-agnostic heart of the Zoho WorkDrive TRUNCATE demo.

It is a `change_receiver` for `CachedFileFolders.resync_bulk()` / `resync_sweep()` that,
for every INSERT/UPDATE, writes a `summary.md` and an `index.json` into the file's slave
directory and upserts the summary into a (stub) vector index; for DELETE it removes the
index row. It is deliberately decoupled from Zoho so it can be unit-tested against the
library's `MockNetworkFileProxy`.

The interesting bit is HOW it gets at the content while respecting the truncation
policy. It NEVER assumes a TRUNCATE entry's body is fetchable; it asks
`RetentionPolicy.may_materialize()` first. Three body-access strategies result:

1. Body forbidden (audio/video of any size, OR anything over the 100 MB ceiling):
   never materialize. Summarize from filename/path/size only (with a media size class).
2. Truncated but fetchable text/doc (between the truncate threshold and the ceiling):
   transiently `materialize()` + `deploy()` the pristine proxy into the cache's
   throwaway temp dir, summarize, then discard the body. The cache entry stays a
   zero-byte truncated file -- we only borrowed the bytes.
3. KEEP entry (small): the body is already on disk at `cur.file_path`; read it directly.

MUST be used as an ASYNC receiver: fetching a truncated entry's body requires
`await proxy.materialize(...)`, which a synchronous receiver cannot do.

The summarizer itself is intentionally dumb (we are not demonstrating summarization):
pandoc/text -> first ~2 KB, with a filepath-sentence fallback and an optional
recent-year hint. The vector index is a documented stub. Both are clearly marked seams
for real implementations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from totodev_pub.cached_file_folders_support import ChangeNotice, ChangeType
from totodev_pub.cached_file_folders_support.file_proxy_base import FileProxyBase

from .retention_policy import RetentionPolicy
from .stub_vector_index import StubVectorIndex

logger = logging.getLogger(__name__)

SUMMARY_FILENAME = "summary.md"
INDEX_FILENAME = "index.json"
SUMMARY_MAX_CHARS = 2048

# Text-like extensions we can read directly without pandoc.
_TEXT_EXTS = frozenset({
    ".txt", ".text", ".md", ".markdown", ".rst", ".log",
    ".csv", ".tsv", ".json", ".yaml", ".yml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".html", ".htm", ".xml",
})

# Plausible recent year inside a name/path: 1950-2049.
_YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")


def _human_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class _CacheLike(Protocol):
    """The minimal CachedFileFolders surface SummaryIndexer actually uses.

    Declaring it as a Protocol (instead of typing the cache as ``Any``) keeps the indexer
    decoupled from the concrete cache class while making the dependency explicit: the only
    things needed are the grouping-level slave dir and the swept temp-directory root.
    """

    def get_slave_dir(self, grouping_key: Any) -> str: ...

    def get_temp_directory_root(self) -> str: ...


class SummaryIndexer:
    """Async change-receiver that derives a dumb summary + stub index per file."""

    def __init__(
        self,
        cache: _CacheLike,
        grouping_key: Any | None = None,
        policy: RetentionPolicy | None = None,
        vector_index: StubVectorIndex | None = None,
        source: str = "cachedfilefolders",
        materialize_blocking_secs: float = 30.0,
    ) -> None:
        """
        Args:
            cache: The CachedFileFolders instance (for temp dir + grouping slave dir).
            grouping_key: Grouping key the sync runs under (for the grouping-level index).
            policy: RetentionPolicy; defaults to the standard 50 KB / 100 MB policy.
            vector_index: Stub vector index; defaults to one stored in the grouping
                slave dir as `vector_index.json`.
            source: Label recorded in index.json (e.g. "zoho_workdrive").
            materialize_blocking_secs: Max wait passed to proxy.materialize().
        """
        self.cache = cache
        self.grouping_key = grouping_key
        self.policy = policy or RetentionPolicy()
        self.source = source
        self.materialize_blocking_secs = materialize_blocking_secs

        if vector_index is None:
            slave_dir = Path(cache.get_slave_dir(grouping_key))
            vector_index = StubVectorIndex(slave_dir / "vector_index.json")
        self.vector_index = vector_index

        # Serialize stub-index mutations across concurrent upserts.
        self._index_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # change_receiver entry point                                         #
    # ------------------------------------------------------------------ #

    async def on_change(self, notice: ChangeNotice, proxy: FileProxyBase | None) -> None:
        if notice.change_type == ChangeType.DELETE:
            await self._on_delete(notice)
            return
        await self._on_upsert(notice, proxy)

    async def _on_delete(self, notice: ChangeNotice) -> None:
        old = notice.old
        if old is not None:
            async with self._index_lock:
                self.vector_index.remove(old.ref_path)

    async def _on_upsert(self, notice: ChangeNotice, proxy: FileProxyBase | None) -> None:
        cur = notice.cur
        if cur is None:
            return

        ref_path = cur.ref_path
        name = cur.file_path.name
        ext = cur.file_path.suffix.lower()
        size = await self._effective_size(notice, proxy)
        is_truncated = cur.is_truncated()
        media_kind = self.policy.media_kind(name)
        is_native_doc = self._is_native_doc(proxy)
        # Filename-only summarization (never touch the body): native docs (derived export,
        # not the source), plus media and opaque binaries (nothing a text/pandoc
        # summarizer can use).
        filename_only = is_native_doc or self.policy.summarize_by_filename_only(name)

        # Decide how (or whether) we get at the body.
        if filename_only:
            summary, method = self._metadata_only_summary(
                ref_path, name, size, media_kind, is_native_doc
            )
            body_inspected = False
        elif not is_truncated:
            # KEEP: full body already on disk; read it directly, never re-drive proxy.
            summary, method = self._summarize_body(cur.file_path, ref_path, name, size, media_kind)
            body_inspected = True
        elif proxy is not None and self.policy.may_materialize(name, size):
            # TRUNCATE but fetchable: borrow the bytes transiently, then discard.
            summary, method, body_inspected = await self._summarize_via_transient_body(
                proxy, ref_path, name, size, media_kind
            )
        else:
            # Body forbidden (over-ceiling, unknown size, or no proxy).
            summary, method = self._metadata_only_summary(ref_path, name, size, media_kind)
            body_inspected = False

        year_hint = self._year_hint(f"{name} {ref_path}")
        record: dict[str, Any] = {
            "ref_path": ref_path,
            "file_name": name,
            "ext": ext,
            "size_bytes": size,
            "retention": "truncate" if is_truncated else "keep",
            "body_inspected": body_inspected,
            "summary_method": method,
            "is_native_doc": is_native_doc,
            "media_kind": media_kind,
            "size_class": self.policy.classify_media_size(size) if media_kind else None,
            "year_hint": year_hint,
            "summary": summary,
            "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": self.source,
        }

        self._write_artifacts(cur.slave_dir_path, record)
        self._update_metadata(notice, record)

        async with self._index_lock:
            self.vector_index.upsert(ref_path, summary, metadata={
                "ref_path": ref_path,
                "ext": ext,
                "size_bytes": size,
                "retention": record["retention"],
                "body_inspected": body_inspected,
                "media_kind": media_kind,
                "size_class": record["size_class"],
                "source": self.source,
            })

    # ------------------------------------------------------------------ #
    # Body access strategies                                              #
    # ------------------------------------------------------------------ #

    async def _summarize_via_transient_body(
        self,
        proxy: FileProxyBase,
        ref_path: str,
        name: str,
        size: int | None,
        media_kind: str | None,
    ) -> tuple[str, str, bool]:
        """Materialize+deploy the pristine proxy into the cache's temp area, summarize, discard.

        Falls back to a metadata-only summary if the transient fetch fails for any reason
        -- "no body available" is an ordinary outcome, never an exception out of here.

        TEMP-FILE HYGIENE (worth copying into your own receivers): the borrowed body is a
        loose file inside the cache's *designated, swept* temp area, and it is removed in a
        ``finally`` on EVERY exit path. That gives two independent guarantees -- (1) the
        ``finally`` deletes it in-process even on error/cancellation, and (2) because it is a
        plain file in the swept temp area, the library's periodic temp sweep reaps it as a
        backstop even if the process is hard-killed before the ``finally`` runs. (This mirrors
        how the core library stages its own transient materializations.)
        """
        temp_root = Path(self.cache.get_temp_directory_root())
        temp_root.mkdir(parents=True, exist_ok=True)
        body_path: Path | None = None
        try:
            ready = await proxy.materialize(self.materialize_blocking_secs, temp_root)
            if not ready:
                summary, method = self._metadata_only_summary(ref_path, name, size, media_kind)
                return summary, method, False
            # deploy() lands the body at <temp_root>/<file_name()> (the same flat-temp
            # pattern the core library uses for its own transient materialization).
            proxy.deploy(str(temp_root))
            body_path = temp_root / Path(proxy.file_name()).name
            if not body_path.is_file():
                summary, method = self._metadata_only_summary(ref_path, name, size, media_kind)
                return summary, method, False
            summary, method = self._summarize_body(body_path, ref_path, name, size, media_kind)
            return summary, method, True
        except Exception as exc:  # transient fetch is best-effort; degrade gracefully
            logger.warning("Transient body fetch failed for %s: %s", ref_path, exc)
            summary, method = self._metadata_only_summary(ref_path, name, size, media_kind)
            return summary, method, False
        finally:
            # Always discard the borrowed body; the cache entry stays a zero-byte truncated
            # file. We only ever borrowed the bytes.
            if body_path is not None:
                try:
                    body_path.unlink()
                except OSError:
                    pass

    def _summarize_body(
        self,
        body_path: Path,
        ref_path: str,
        name: str,
        size: int | None,
        media_kind: str | None,
    ) -> tuple[str, str]:
        """Dumb summary from a body on disk: text/pandoc -> first ~2 KB, else filepath sentence."""
        text, method = self._extract_text(body_path)
        if text is not None:
            summary = text[:SUMMARY_MAX_CHARS].strip()
            if not summary:
                summary = self._filepath_sentence(ref_path, name)
                method = "filepath_fallback"
        else:
            summary = self._filepath_sentence(ref_path, name)
            method = "filepath_fallback"

        year = self._year_hint(f"{name} {ref_path} {text or ''}")
        if year is not None:
            summary = f"{summary}\n\nMay relate to events or information from {year}."
        return summary, method

    def _metadata_only_summary(
        self,
        ref_path: str,
        name: str,
        size: int | None,
        media_kind: str | None,
        is_native_doc: bool = False,
    ) -> tuple[str, str]:
        """Summary from filename/path/size only -- no body access at all."""
        parts = [self._filepath_sentence(ref_path, name)]
        if media_kind:
            size_class = self.policy.classify_media_size(size)
            klass = size_class.capitalize() if size_class else "Unknown-size"
            parts.append(f"{klass} {media_kind} file ({_human_size(size)}); not transcribed.")
        elif is_native_doc:
            parts.append("Zoho-native document; summarized from filename only "
                         "(body is a derived export, not inspected).")
        elif self.policy.is_opaque_binary(name):
            ext = Path(name).suffix.lower().lstrip(".") or "binary"
            parts.append(f"Opaque {ext} file ({_human_size(size)}); not summarized from content.")
        else:
            parts.append(f"Large file ({_human_size(size)}); body not inspected.")
        year = self._year_hint(f"{name} {ref_path}")
        if year is not None:
            parts.append(f"May relate to events or information from {year}.")
        return " ".join(parts), "metadata_only"

    # ------------------------------------------------------------------ #
    # Dumb extraction helpers                                            #
    # ------------------------------------------------------------------ #

    def _extract_text(self, body_path: Path) -> tuple[str | None, str | None]:
        """Return (text, method). Text files read directly; others via pandoc; None if neither works."""
        ext = body_path.suffix.lower()
        if ext in _TEXT_EXTS:
            try:
                return body_path.read_text(encoding="utf-8", errors="replace"), "text_first_2k"
            except OSError:
                return None, None

        # Non-text: try pandoc (optional system dependency) via pypandoc.
        try:
            import pypandoc  # lazy: optional dependency
        except Exception:
            return None, None
        try:
            converted = pypandoc.convert_file(str(body_path), to="plain")
            return converted, "pandoc_first_2k"
        except Exception as exc:
            logger.debug("pandoc could not convert %s: %s", body_path, exc)
            return None, None

    @staticmethod
    def _is_native_doc(proxy: FileProxyBase | None) -> bool:
        """Duck-typed: does the proxy advertise itself as a native doc (Zoho Writer/Sheet/Show)?

        Kept generic so the summarizer stays source-agnostic -- a proxy may expose an
        ``is_native_doc`` attribute but is not required to.
        """
        return bool(getattr(proxy, "is_native_doc", False)) if proxy is not None else False

    @staticmethod
    def _filepath_sentence(ref_path: str, name: str) -> str:
        return f"File {name} is found on the server at {ref_path}."

    @staticmethod
    def _year_hint(text: str) -> int | None:
        match = _YEAR_RE.search(text or "")
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------------ #
    # Artifact writers                                                   #
    # ------------------------------------------------------------------ #

    def _write_artifacts(self, slave_dir: Path, record: dict[str, Any]) -> None:
        slave_dir.mkdir(parents=True, exist_ok=True)
        (slave_dir / INDEX_FILENAME).write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
        (slave_dir / SUMMARY_FILENAME).write_text(self._render_summary_md(record), encoding="utf-8")

    @staticmethod
    def _render_summary_md(record: dict[str, Any]) -> str:
        lines = [
            f"# Summary: {record['file_name']}",
            "",
            f"- ref_path: `{record['ref_path']}`",
            f"- size: {_human_size(record['size_bytes'])}",
            f"- retention: {record['retention']}",
            f"- body_inspected: {record['body_inspected']}",
            f"- summary_method: {record['summary_method']}",
        ]
        if record.get("media_kind"):
            lines.append(f"- media: {record['media_kind']} ({record.get('size_class')})")
        if record.get("year_hint"):
            lines.append(f"- year_hint: {record['year_hint']}")
        lines += ["", "## Summary", "", record["summary"], ""]
        return "\n".join(lines)

    def _update_metadata(self, notice: ChangeNotice, record: dict[str, Any]) -> None:
        meta = notice.metadata()
        if meta is None:
            return
        try:
            data = dict(meta.as_dict(mutable=True))
            data.update({
                "processing_state": "summarized",
                "summarized_at": record["indexed_at"],
                "summary_method": record["summary_method"],
                "body_inspected": record["body_inspected"],
                "retention": record["retention"],
                "source": self.source,
            })
            meta.overwrite_source_file(data)
        except Exception as exc:  # metadata is a convenience, never fatal
            logger.debug("Could not update metadata for %s: %s", record["ref_path"], exc)

    # ------------------------------------------------------------------ #
    # Size resolution                                                    #
    # ------------------------------------------------------------------ #

    async def _effective_size(
        self, notice: ChangeNotice, proxy: FileProxyBase | None
    ) -> int | None:
        """Cheapest reliable size: proxy peek -> truncation sidecar -> on-disk stat."""
        if proxy is not None:
            try:
                origin = await proxy.peek_metadata()
                if origin is not None and origin.size is not None:
                    return origin.size
            except Exception as exc:
                logger.debug("peek_metadata failed; falling back to sidecar/stat: %s", exc)
        cur = notice.cur
        if cur is not None:
            info = cur.truncation_info()
            if info is not None and info.size is not None:
                return info.size
            try:
                on_disk = cur.file_path.stat().st_size
                if on_disk > 0:
                    return on_disk
            except OSError:
                pass
        return None
