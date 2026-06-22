#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Size- and type-driven retention policy for the Zoho WorkDrive TRUNCATE demo.

This is the single place that decides, for each file, the two related-but-distinct
questions the demo cares about:

1. **What does the cache retain?** -> `recommend()` returns a
   `LocalRetentionRecommendation` (KEEP = full body on disk, TRUNCATE = zero-byte
   body + metadata sidecar). A `ZohoWorkDriveFileProxy` delegates its
   `local_retention_recommendation()` to this, so the policy *is* the cache's
   truncation policy.

2. **May the summary step ever touch the body?** -> `may_materialize()`. Some
   entries are TRUNCATE yet still allow a transient body fetch (text/docs between the
   truncate threshold and the hard ceiling); others are TRUNCATE and must NEVER be
   fetched (audio/video of any size, or *anything* over the 100 MB ceiling).

The two are deliberately separate functions because "what we keep" and "what we may
briefly download to summarize" are different decisions.

Rules (defaults; all tunable via constructor args):
- Audio/video (by extension): always TRUNCATE, never materialize -- summaries come
  from filename/path/size only, with a small/medium/large size class.
- Opaque binaries (archives/installers/disk images: .zip, .dmg, .iso, ...): never
  materialize -- there is nothing a text/pandoc summarizer can extract, so fetching the
  body is pure wasted egress. They are summarized from filename/path/size only. (Their
  retention is still size-based: a small archive is KEPT as a normal mirrored file; a
  large one TRUNCATEs -- we just never download it merely to "summarize" it.)
- Files over `truncate_over_bytes` (50 KB): TRUNCATE.
- Files over `never_materialize_over_bytes` (100 MB): never materialized, for ANY
  type. This is a hard pathological-size backstop protecting the filesystem, token /
  egress cost, and runtime. It sits above the per-type rules.
- Everything else (small text/docs): KEEP the body on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from totodev_pub.cached_file_folders_support.file_proxy_base import (
    LocalRetentionRecommendation,
)

KIB = 1024
MIB = 1024 * 1024

DEFAULT_TRUNCATE_OVER_BYTES = 50 * KIB
DEFAULT_NEVER_MATERIALIZE_OVER_BYTES = 100 * MIB
DEFAULT_MEDIA_SMALL_MAX_BYTES = 25 * MIB
DEFAULT_MEDIA_MEDIUM_MAX_BYTES = 250 * MIB

VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".mpg", ".mpeg", ".wmv", ".flv", ".3gp", ".ts",
})
AUDIO_EXTS = frozenset({
    ".mp3", ".wav", ".m4a", ".aac", ".flac",
    ".ogg", ".oga", ".opus", ".wma", ".aiff", ".alac",
})
AUDIO_VIDEO_EXTS = VIDEO_EXTS | AUDIO_EXTS

# Opaque binaries: archives, installers, and disk images. A text/pandoc summarizer can
# extract nothing from these, so the body must never be fetched just to summarize it --
# they are summarized from filename/path/size only (like media). Note that container
# formats like .docx/.xlsx/.pptx/.epub are technically zip-based but ARE summarizable, so
# they are deliberately NOT listed here.
ARCHIVE_EXTS = frozenset({
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".7z", ".rar",
    ".z", ".lz", ".lzma", ".zst", ".cab", ".arj", ".ace",
})
DISK_IMAGE_EXTS = frozenset({".dmg", ".iso", ".img", ".vmdk", ".vdi", ".vhd", ".vhdx"})
INSTALLER_BINARY_EXTS = frozenset({
    ".exe", ".msi", ".pkg", ".deb", ".rpm", ".apk", ".dll", ".so", ".dylib", ".bin",
})
OPAQUE_BINARY_EXTS = ARCHIVE_EXTS | DISK_IMAGE_EXTS | INSTALLER_BINARY_EXTS


@dataclass(frozen=True)
class RetentionPolicy:
    """Decides cache retention and body-fetch permission per file.

    All thresholds are constructor arguments so the demo "reads like a policy" and is
    trivially tunable from the CLI.
    """

    truncate_over_bytes: int = DEFAULT_TRUNCATE_OVER_BYTES
    never_materialize_over_bytes: int = DEFAULT_NEVER_MATERIALIZE_OVER_BYTES
    media_small_max_bytes: int = DEFAULT_MEDIA_SMALL_MAX_BYTES
    media_medium_max_bytes: int = DEFAULT_MEDIA_MEDIUM_MAX_BYTES

    @staticmethod
    def _ext(name: str) -> str:
        return Path(name).suffix.lower()

    def is_audio_video(self, name: str) -> bool:
        return self._ext(name) in AUDIO_VIDEO_EXTS

    def is_opaque_binary(self, name: str) -> bool:
        """True for archives/installers/disk images -- nothing to summarize from content."""
        return self._ext(name) in OPAQUE_BINARY_EXTS

    def summarize_by_filename_only(self, name: str) -> bool:
        """True when the body must NEVER be inspected for summarization (filename-only).

        Covers audio/video and opaque binaries alike: for both, a transient body fetch
        would download bytes a text/pandoc summarizer can do nothing with.
        """
        return self.is_audio_video(name) or self.is_opaque_binary(name)

    def media_kind(self, name: str) -> str | None:
        """Return "video", "audio", or None for the given filename."""
        ext = self._ext(name)
        if ext in VIDEO_EXTS:
            return "video"
        if ext in AUDIO_EXTS:
            return "audio"
        return None

    def recommend(self, name: str, size: int | None) -> LocalRetentionRecommendation:
        """What the cache should retain locally for this file."""
        if self.is_audio_video(name):
            return LocalRetentionRecommendation.TRUNCATE
        if size is not None and size > self.truncate_over_bytes:
            return LocalRetentionRecommendation.TRUNCATE
        return LocalRetentionRecommendation.KEEP

    def may_materialize(self, name: str, size: int | None) -> bool:
        """Whether the summary step may fetch (transiently) the body of a TRUNCATE entry.

        Conservative by construction:
        - audio/video and opaque binaries -> never (filename-only by design);
        - size over the hard ceiling -> never (objective 5), regardless of type;
        - unknown size -> never (don't risk a pathologically large fetch);
        - otherwise -> yes.

        KEEP entries never consult this (their small body is already on disk).
        """
        if self.summarize_by_filename_only(name):
            return False
        if size is None:
            return False
        if size > self.never_materialize_over_bytes:
            return False
        return True

    def classify_media_size(self, size: int | None) -> str | None:
        """Rough small/medium/large class for media, from size alone."""
        if size is None:
            return None
        if size < self.media_small_max_bytes:
            return "small"
        if size < self.media_medium_max_bytes:
            return "medium"
        return "large"
