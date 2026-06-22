#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Zoho WorkDrive Sync -- CachedFileFolders TRUNCATE / summarize / index demo.

This example mirrors a Zoho WorkDrive folder tree into a CachedFileFolders cache and,
crucially, demonstrates the library's *truncated-entries* capability: it can mirror a
remote tree far larger than local disk by keeping only metadata (a zero-byte body + a
sidecar) for most files while still feeding everything through a downstream summary +
index pipeline.

It joins the family of example sync programs (SharePoint, Gmail, Outlook) and follows
the same shape: a FileProxy + Factory, a Simple{Service}Sync wrapper, env-var config,
and a Click CLI. What is new here is the retention policy and the async change-receiver
(see `summary_indexer.py` and `retention_policy.py`).

## What it demonstrates

1. Recursive descent: mirror a WorkDrive folder (and subfolders) from a root folder id.
2. Size-based TRUNCATE policy: files over 50 KB are stored as metadata-only entries.
3. Summary + index during change handling: a `summary.md` + `index.json` are written
   into each file's slave directory and the summary is pushed into a (stub) vector
   index -- while the cache keeps respecting truncation. For a truncated-but-fetchable
   text/doc the receiver briefly materializes the body, summarizes, and discards it.
4. Audio/video and pathological files: audio/video (any size) and ANYTHING over a hard
   100 MB ceiling are never materialized -- they are summarized from filename/path/size
   only (with a small/medium/large media class).
5. Zoho-native docs (Writer/Sheet/Show): these report `size_in_bytes: 0` in the listing
   (a native doc has no canonical file) and expose no version/etag/content-hash, so size
   is a useless key. They are detected cheaply from the listing (`service_type` zw/zs/zp,
   with `extn`/`type` as fallbacks) and handled uniformly: ALWAYS truncated (the local
   body would only ever be a derived export, not the source of truth), change-tracked by
   modified-time ALONE, and summarized from filename only. The upshot: a native doc is
   never downloaded -- not even to measure it. (If real native-doc summaries are wanted
   later, flip specific service types back to an export+summarize path.)

The summarizer (dumb pandoc/text -> first 2 KB, filepath fallback, year hint) and the
vector index are deliberately simplistic, documented stubs -- this demo is about the
caching/truncation pattern, not summarization or search quality.

## API and Authentication

Uses the **Zoho WorkDrive REST API v1**
(`https://www.zohoapis.<dc>/workdrive/api/v1`; downloads from
`https://download.zoho.<dc>/v1/workdrive`). Region/data-center (`dc`) is one of
`com`, `eu`, `in`, `com.au`, `jp`, etc., and MUST match your account.

### Authentication scheme
OAuth 2.0 using a **Self Client** with the **authorization-code grant** -- ideal for an
unattended, single-account backend job. You obtain a permanent **refresh token** once,
and the program mints 1-hour access tokens from it on demand. WorkDrive calls use the
header `Authorization: Zoho-oauthtoken <access_token>` (note: NOT `Bearer`).

### One-time credential acquisition (Self Client)
1. Go to the Zoho API Console (`https://api-console.zoho.<dc>`). GET STARTED ->
   Self Client -> CREATE NOW. Copy the **Client ID** and **Client Secret**.
2. Open the **Generate Code** tab. Enter scopes (comma-separated), e.g.
   `WorkDrive.files.READ, WorkDrive.team.READ, WorkDrive.workspace.READ`, a
   description, and an expiry; generate and copy the one-time **grant code**.
3. Exchange the grant code for a refresh token (within its expiry window):
       POST https://accounts.zoho.<dc>/oauth/v2/token
            ?grant_type=authorization_code
            &client_id=<ID>&client_secret=<SECRET>&code=<GRANT_CODE>
   Save the `refresh_token` from the response (it does not expire).
   (The bundled `zoho_workdrive_token_bootstrap.py` automates this step.)

See: https://www.zoho.com/accounts/protocol/oauth/self-client/overview.html

## Environment variables (credentials/endpoints -- never CLI args)

    export ZOHO_WD_CLIENT_ID="1000.xxxxxxxx"
    export ZOHO_WD_CLIENT_SECRET="xxxxxxxxxxxxxxxx"
    export ZOHO_WD_REFRESH_TOKEN="1000.yyyy.zzzz"
    export ZOHO_WD_DC="com"                       # data center / region
    # optional: the folder to mirror (else pass --root-folder-id on the CLI)
    export ZOHO_WD_ROOT_FOLDER_ID="abcd1234..."
    # optional explicit overrides (otherwise derived from ZOHO_WD_DC / token api_domain)
    export ZOHO_WD_API_HOST="https://www.zohoapis.com/workdrive/api/v1"
    export ZOHO_WD_DOWNLOAD_HOST="https://download.zoho.com/v1/workdrive"

## Installation / dependencies

    pip install "totodev-pub[connectors]"      # requests
    # optional, for nicer summaries of non-text files:
    pip install pypandoc        # plus the `pandoc` system binary

## Usage

    python zoho_workdrive_sync.py \
        --cache-root volatile/zoho_wd_sync/ \
        --dir-key demo \
        --root-folder-id <folder_id> \
        --max-files 5

    python zoho_workdrive_sync.py \
        --cache-root volatile/zoho_wd_sync/ --dir-key fulltree \
        --root-folder-id <folder_id> \
        --truncate-over-kb 50 --never-materialize-over-mb 100

## File organization

Files are cached under `<cache-root>/key-<dir_key>/` mirroring the WorkDrive ref_path
`zohowd://<workspace>/<folder>/<name>`. Each file gets a slave directory holding the
standardized `metadata.yaml`, plus this demo's `summary.md` and `index.json`. Truncated
files additionally have a `_truncation_info.yaml` sidecar and a zero-byte body. The
grouping-level slave directory holds the stub `vector_index.json`.

## Known limitations

- Pull-side re-derivation is out of scope: the cache never re-fetches on its own.
- `use_xxhash=False` is required at scale (xxhash would re-download every entry).
- The summarizer and vector index are stubs; replace with OCR/LLM + a real vector DB.
- Native docs (Writer/Sheet/Show) are summarized from filename only; materializing their
  exports for richer summaries is possible future work -- see the seam at `_is_native_doc`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

# Add src to path ONLY for direct execution (`python zoho_workdrive_sync.py`). When this
# module is imported as part of the package, src is already importable and we must not
# mutate sys.path. `__name__` is already "__main__" at module-load time during direct
# execution, so this guard runs the insert before the package imports below need it, while
# skipping it entirely on package import. (Mirrors the other examples' intent.)
if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support import ChangeNotice
from totodev_pub.cached_file_folders_support.file_proxy_base import (
    FileProxyBase,
    LocalRetentionRecommendation,
    OriginMetadata,
)
from totodev_pub.cached_file_folders_support.sync_types import ResyncBulkResult, UpsertFailure

# Absolute imports (not relative) so this module runs both directly
# (`python zoho_workdrive_sync.py`, via the sys.path insert above) and as a package
# module (`python -m ...`), matching the sibling examples.
from totodev_pub.cached_file_folders_support.examples.retention_policy import RetentionPolicy
from totodev_pub.cached_file_folders_support.examples.summary_indexer import SummaryIndexer

logger = logging.getLogger(__name__)

GROUPING_PATTERN = "key-{dir_key}/"

# Environment variable names -- a single source of truth that doubles as the key set for
# the config dict returned by validate_environment() (no silent name transformation).
_ENV_CLIENT_ID = "ZOHO_WD_CLIENT_ID"
_ENV_CLIENT_SECRET = "ZOHO_WD_CLIENT_SECRET"
_ENV_REFRESH_TOKEN = "ZOHO_WD_REFRESH_TOKEN"
_ENV_DC = "ZOHO_WD_DC"
_ENV_API_HOST = "ZOHO_WD_API_HOST"
_ENV_DOWNLOAD_HOST = "ZOHO_WD_DOWNLOAD_HOST"
_ENV_ROOT_FOLDER_ID = "ZOHO_WD_ROOT_FOLDER_ID"


def configure_logging(debug_enabled: bool = False) -> None:
    # Configure the root logger FIRST: basicConfig is a no-op once handlers exist, so it
    # must run before we tune individual library loggers below.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    level = logging.DEBUG if debug_enabled else logging.WARNING
    for logger_name in ["urllib3", "requests", "asyncio"]:
        logging.getLogger(logger_name).setLevel(level)


def _require_requests():
    try:
        import requests  # noqa: F401
        return requests
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "The 'requests' package is required for Zoho WorkDrive sync. "
            "Install it with: pip install \"totodev-pub[connectors]\""
        ) from exc


# =============================================================================
# OAUTH
# =============================================================================

class ZohoOAuth:
    """Self-Client OAuth token provider: refresh token -> cached 1-hour access token.

    Mints a new access token on first use and whenever the cached one is near expiry.
    Exposes `api_domain` (returned by Zoho) so callers can use the correct regioned
    `zohoapis` host without guessing.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        dc: str = "com",
        accounts_host: str | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.dc = dc
        self.accounts_host = (accounts_host or f"https://accounts.zoho.{dc}").rstrip("/")
        self._access_token: str | None = None
        # 0.0 is in the past, so the first get_access_token() call always refreshes.
        self._expiry_epoch: float = 0.0
        self.api_domain: str | None = None

    def get_access_token(self) -> str:
        if self._access_token is not None and time.time() < (self._expiry_epoch - 60):
            return self._access_token
        self._refresh()
        assert self._access_token is not None
        return self._access_token

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Zoho-oauthtoken {self.get_access_token()}"}

    def _refresh(self) -> None:
        requests = _require_requests()
        resp = requests.post(
            f"{self.accounts_host}/oauth/v2/token",
            params={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"Token refresh returned non-JSON (HTTP {resp.status_code})")
        if "access_token" not in data:
            raise RuntimeError(
                f"Token refresh failed: {data.get('error', data)} (HTTP {resp.status_code})"
            )
        self._access_token = data["access_token"]
        self._expiry_epoch = time.time() + int(data.get("expires_in", 3600))
        self.api_domain = data.get("api_domain") or self.api_domain


# =============================================================================
# API CLIENT
# =============================================================================

class _ZohoWorkDriveApiClient:
    """All HTTP with the WorkDrive API. One method per call for easy mocking."""

    def __init__(self, token_provider: ZohoOAuth, api_host: str, download_host: str) -> None:
        self.token_provider = token_provider
        self.api_host = api_host.rstrip("/")
        self.download_host = download_host.rstrip("/")

    def get_folder_children(self, folder_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """GET /files/{folder_id}/files -- one JSON:API page of child records."""
        requests = _require_requests()
        url = f"{self.api_host}/files/{folder_id}/files"
        headers = {**self.token_provider.auth_header(), "Accept": "application/vnd.api+json"}
        resp = requests.get(
            url,
            headers=headers,
            params={"page[limit]": limit, "page[offset]": offset},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"WorkDrive list failed (HTTP {resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def download_file(self, file_id: str, out_path: str, download_url: str | None = None) -> None:
        """Stream a file body to out_path (blocking).

        Prefers the per-file ``download_url`` returned in the listing (it points at the
        correct, possibly account-specific, download host); falls back to constructing
        ``{download_host}/download/{file_id}``.
        """
        requests = _require_requests()
        url = download_url or f"{self.download_host}/download/{file_id}"
        headers = self.token_provider.auth_header()
        with requests.get(url, headers=headers, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                raise RuntimeError(
                    f"WorkDrive download failed (HTTP {resp.status_code}): {resp.text[:300]}"
                )
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)


def _parse_modified(attrs: dict[str, Any]) -> datetime | None:
    """Extract a modified-time datetime from WorkDrive JSON:API attributes.

    WorkDrive returns the numeric epoch under ``modified_time_in_millisecond`` (the
    ``modified_time`` field is a human string like "Jun 21, 10:28 PM", not ISO).
    """
    for key in ("modified_time_in_millisecond", "modified_time_in_millis"):
        millis = attrs.get(key)
        if isinstance(millis, (int, float)) and millis > 0:
            return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    raw = attrs.get("modified_time")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_SIZE_UNITS = {"byte": 1, "bytes": 1, "b": 1, "kb": 1024, "mb": 1024**2,
               "gb": 1024**3, "tb": 1024**4}


def _parse_size(attrs: dict[str, Any]) -> int | None:
    """Extract a byte count from WorkDrive attributes.

    Prefers the numeric ``storage_info.size_in_bytes``; falls back to parsing the
    human ``size`` string ("0 byte", "12.3 KB") if the numeric field is absent.

    NOTE: for Zoho *native* docs (Writer/Sheet/Show) this reports ``0`` -- a native
    doc has no canonical file, so its byte size only exists once exported. Callers that
    care (the factory) must detect native docs via :func:`_is_native_doc` and treat the
    reported size as unknown rather than trusting the false ``0``.
    """
    storage = attrs.get("storage_info") or {}
    numeric = storage.get("size_in_bytes")
    if isinstance(numeric, (int, float)):
        return int(numeric)
    text = storage.get("size") or attrs.get("size")
    if isinstance(text, str):
        parts = text.split()
        try:
            value = float(parts[0])
            unit = _SIZE_UNITS.get(parts[1].lower(), 1) if len(parts) > 1 else 1
            return int(value * unit)
        except (ValueError, IndexError):
            return None
    return None


# Zoho-native document markers (Writer/Sheet/Show). Native docs report `size_in_bytes: 0`
# (they have no canonical file) and there is no version/etag/content-hash in the listing,
# so we treat them uniformly: always TRUNCATE, track changes by modified-time only, and
# summarize from filename only -- their body is a derived export we never download.
# Detection is cheap (these fields come straight from the listing). `service_type` is the
# cleanest signal (zw/zs/zp), with extn/type as corroborating fallbacks.
_NATIVE_EXTN = frozenset({"zwriter", "zsheet", "zshow", "zdoc"})
_NATIVE_TYPES = frozenset({"writer", "zohosheet", "zohoshow", "zohowriter"})
_NATIVE_SERVICE_TYPES = frozenset({"zw", "zs", "zp"})


def _is_native_doc(attrs: dict[str, Any]) -> bool:
    """True if the record is a Zoho-native doc (Writer/Sheet/Show)."""
    extn = (attrs.get("extn") or "").lower()
    dtype = (attrs.get("type") or "").lower()
    service = (attrs.get("service_type") or "").lower()
    return extn in _NATIVE_EXTN or dtype in _NATIVE_TYPES or service in _NATIVE_SERVICE_TYPES


def extract_root_folder_id(value: str) -> str:
    """Accept either a bare WorkDrive folder id or a folder URL and return the id.

    Be tolerant of what users paste. A bare id (no slashes) is returned unchanged. From a
    URL we prefer the segment right after ``folders/`` (the folder id); if that is the
    ``files`` landing page (no specific folder), we fall back to the workspace id after
    ``ws/``; finally we fall back to the last meaningful path segment. Query strings and
    fragments are ignored.

    Examples:
        "q8hgxe014..."                                  -> "q8hgxe014..."
        ".../ws/<ws>/folders/<folder>"                  -> "<folder>"
        ".../ws/<ws>/folders/files"                     -> "<ws>"
        "https://workdrive.zoho.com/folder/<id>?x=1"    -> "<id>"
    """
    value = (value or "").strip()
    if not value:
        return value
    # A bare id has no path separators; return it as-is.
    if "://" not in value and "/" not in value:
        return value

    from urllib.parse import urlparse

    path = urlparse(value).path if "://" in value else value
    segments = [seg for seg in path.split("/") if seg]

    def _after(marker: str) -> str | None:
        if marker in segments:
            idx = segments.index(marker)
            if idx + 1 < len(segments):
                return segments[idx + 1]
        return None

    folder = _after("folders") or _after("folder")
    if folder and folder != "files":
        return folder
    workspace = _after("ws")
    if workspace:
        return workspace
    meaningful = [seg for seg in segments if seg != "files"]
    return meaningful[-1] if meaningful else value


# =============================================================================
# PROXY
# =============================================================================

class ZohoWorkDriveFileProxy(FileProxyBase):
    """Lazy proxy for a single Zoho WorkDrive file."""

    def __init__(
        self,
        api_client: _ZohoWorkDriveApiClient,
        file_id: str,
        name: str,
        folder_path: str,
        size: int | None,
        modified_time: datetime | None,
        policy: RetentionPolicy,
        workspace_name: str = "workdrive",
        download_url: str | None = None,
        is_native_doc: bool = False,
    ) -> None:
        self._api_client = api_client
        self._file_id = file_id
        self._name = name
        self._folder_path = folder_path.strip("/")
        self._size = size
        self._modified_time = modified_time
        self._policy = policy
        self._workspace_name = workspace_name
        self._download_url = download_url
        # Native docs are handled specially throughout this class; see the _NATIVE_*
        # constants block for the rationale.
        self._is_native_doc = is_native_doc

        self._temp_path: str | None = None
        self._was_deployed = False
        self._materialization_started = False
        self._materialization_completed = False

    def ref_path(self) -> str:
        if self._folder_path:
            return f"zohowd://{self._workspace_name}/{self._folder_path}/{self._name}"
        return f"zohowd://{self._workspace_name}/{self._name}"

    def file_name(self) -> str:
        return self._name

    async def peek_metadata(self) -> OriginMetadata | None:
        if self._size is None and self._modified_time is None:
            return None
        mtime = self._modified_time.timestamp() if self._modified_time is not None else None
        return OriginMetadata(size=self._size, mtime=mtime)

    def looks_same(self, other_fpath: str, override_byte_count: int | None = None) -> bool | None:
        if self._modified_time is None:
            return None
        try:
            st = os.stat(other_fpath)
        except OSError:
            return None
        if self._is_native_doc:
            # Native docs have no meaningful size (API reports 0) and no version/etag, so
            # modified-time is the only change signal. Compare it alone, ignore size.
            return self._modified_time.timestamp() == st.st_mtime
        if self._size is None:
            return None
        other_size = st.st_size if override_byte_count is None else override_byte_count
        return self._size == other_size and self._modified_time.timestamp() == st.st_mtime

    async def materialize(self, blocking_secs: float, temp_dir: Path | None = None) -> bool:
        if temp_dir is None or not str(temp_dir).strip():
            raise ValueError("temp_dir must be provided and non-blank for ZohoWorkDriveFileProxy")
        if self._materialization_completed:
            return True
        if self._materialization_started:
            if blocking_secs > 0:
                await asyncio.sleep(min(0.1, blocking_secs))
            return self._materialization_completed
        self._materialization_started = True
        # Create the scratch file FIRST, then guard the failure-prone network download in a
        # try so a raised error or a partial download can never orphan it -- and so a retry
        # never accumulates a fresh orphan each time. (BaseException so cancellation is
        # covered too.) Copy this care into any proxy you write: the temp file must die on
        # every failure path, not only on success.
        fd, temp_path = tempfile.mkstemp(suffix=Path(self._name).suffix, dir=str(temp_dir))
        os.close(fd)
        try:
            # Offload blocking network I/O so the event loop keeps serving other upserts.
            await asyncio.to_thread(
                self._api_client.download_file, self._file_id, temp_path, self._download_url
            )
        except BaseException:
            try:
                os.remove(temp_path)  # best-effort; never mask the original error
            except OSError:
                pass
            self._materialization_started = False
            raise
        self._temp_path = temp_path
        self._materialization_completed = True
        return True

    def deploy(self, target_dir: str) -> None:
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
        if not self._materialization_completed or self._temp_path is None:
            raise RuntimeError("File must be materialized before deployment")
        # "/dev/null" is the shared "materialize-then-discard" sentinel used across the
        # proxy family (sharepoint/local_file/data_struct/dummy/mock_network): the caller
        # wants the body fetched but not kept, so we drop the temp file and report deployed.
        if target_dir == "/dev/null":
            if os.path.exists(self._temp_path):
                os.remove(self._temp_path)
            self._was_deployed = True
            return
        if not os.path.isdir(target_dir):
            raise RuntimeError(f"Target directory does not exist: {target_dir}")
        target_path = os.path.join(target_dir, self._name)
        shutil.move(self._temp_path, target_path)
        if self._modified_time is not None:
            ts = self._modified_time.timestamp()
            os.utime(target_path, (ts, ts))
        self._was_deployed = True

    def local_retention_recommendation(self) -> LocalRetentionRecommendation:
        if self._is_native_doc:  # native docs are always truncated (see _NATIVE_* block)
            return LocalRetentionRecommendation.TRUNCATE
        return self._policy.recommend(self._name, self._size)

    @property
    def is_native_doc(self) -> bool:
        return self._is_native_doc

    def retrieval_hint(self) -> dict[str, Any]:
        hint = {
            "source": "zoho_workdrive",
            "file_id": self._file_id,
            "api_host": self._api_client.api_host,
            "ref_path": self.ref_path(),
        }
        if self._is_native_doc:
            hint["is_native_doc"] = True
        return hint

    def get_context_info(self) -> dict[str, Any]:
        return {
            "proxy_type": "ZohoWorkDriveFileProxy",
            "file_id": self._file_id,
            "name": self._name,
            "folder_path": self._folder_path,
            "size": self._size,
            "modified_time": self._modified_time.isoformat() if self._modified_time else None,
            "is_native_doc": self._is_native_doc,
        }


# =============================================================================
# FACTORY
# =============================================================================

class ZohoWorkDriveFileProxyFactory:
    """Discovers WorkDrive files by recursive descent and yields proxies."""

    def __init__(
        self,
        token_provider: ZohoOAuth,
        api_host: str,
        download_host: str,
        policy: RetentionPolicy,
        workspace_name: str = "workdrive",
        page_limit: int = 50,
    ) -> None:
        self.policy = policy
        self.workspace_name = workspace_name
        self.page_limit = page_limit
        self._api_client = _ZohoWorkDriveApiClient(token_provider, api_host, download_host)

    def scan_files(
        self,
        folder_id: str,
        include_subfolders: bool = True,
        max_files: int | None = None,
        file_extensions: set[str] | None = None,
    ) -> Generator[ZohoWorkDriveFileProxy, None, None]:
        """Breadth-first descent from folder_id, yielding a proxy per file."""
        queue: deque = deque([(folder_id, "")])
        yielded = 0
        while queue:
            current_id, display_path = queue.popleft()
            offset = 0
            while True:
                payload = self._api_client.get_folder_children(current_id, offset, self.page_limit)
                records = payload.get("data", []) or []
                if not records:
                    break
                for record in records:
                    attrs = record.get("attributes", {}) or {}
                    name = attrs.get("name")
                    if not name:
                        continue
                    is_folder = bool(attrs.get("is_folder"))
                    child_path = f"{display_path}/{name}" if display_path else name
                    if is_folder:
                        if include_subfolders:
                            queue.append((record.get("id"), child_path))
                        continue
                    if file_extensions and Path(name).suffix.lower() not in file_extensions:
                        continue
                    native = _is_native_doc(attrs)
                    proxy = ZohoWorkDriveFileProxy(
                        api_client=self._api_client,
                        file_id=record.get("id"),
                        name=name,
                        folder_path=display_path,
                        # Native docs report a meaningless size (0); drop it so they are
                        # mtime-tracked only. Real files use the parsed size.
                        size=None if native else _parse_size(attrs),
                        modified_time=_parse_modified(attrs),
                        policy=self.policy,
                        workspace_name=self.workspace_name,
                        download_url=attrs.get("download_url"),
                        is_native_doc=native,
                    )
                    yield proxy
                    yielded += 1
                    if max_files is not None and yielded >= max_files:
                        return
                if len(records) < self.page_limit:
                    break
                offset += self.page_limit


# =============================================================================
# SIMPLE SYNC WRAPPER
# =============================================================================

@dataclass
class SyncResult:
    insert_count: int
    update_count: int
    delete_count: int
    changes: list[ChangeNotice]
    failures: list[UpsertFailure]
    files_changed_or_failed: int


class SimpleZohoWorkDriveSync:
    """Encapsulates auth + discovery + cached sync with the SummaryIndexer receiver."""

    def __init__(
        self,
        cache: CachedFileFolders,
        dir_key: str,
        oauth: ZohoOAuth,
        api_host: str,
        download_host: str,
        policy: RetentionPolicy,
        root_folder_id: str,
        workspace_name: str = "workdrive",
        max_concurrent_requests: int = 4,
    ) -> None:
        self.cache = cache
        self.dir_key = dir_key
        self.oauth = oauth
        self.api_host = api_host
        self.download_host = download_host
        self.policy = policy
        self.root_folder_id = root_folder_id
        self.workspace_name = workspace_name
        self.max_concurrent_requests = max_concurrent_requests

    async def sync(self, max_files: int | None = None) -> SyncResult:
        # Use a tuple grouping key: CachedFileFolders.files() treats a list as a pattern
        # filter (which silently breaks mark-and-sweep deletes), but an exact tuple key
        # takes the correct lookup path.
        grouping_key = (self.dir_key,)
        grouping = self.cache.grouping(grouping_key)

        factory = ZohoWorkDriveFileProxyFactory(
            token_provider=self.oauth,
            api_host=self.api_host,
            download_host=self.download_host,
            policy=self.policy,
            workspace_name=self.workspace_name,
        )
        indexer = SummaryIndexer(
            cache=self.cache,
            grouping_key=grouping_key,
            policy=self.policy,
            source="zoho_workdrive",
        )

        result: ResyncBulkResult = await grouping.resync_bulk(
            file_proxies=factory.scan_files(self.root_folder_id, max_files=max_files),
            upsert_fail_policy="RETAIN_OLD",
            max_concurrent_requests=self.max_concurrent_requests,
            change_receiver=indexer.on_change,
        )

        stats = {"insert": 0, "update": 0, "delete": 0}
        for change in result.changes:
            stats[change.change_type.value.lower()] += 1
        return SyncResult(
            insert_count=stats["insert"],
            update_count=stats["update"],
            delete_count=stats["delete"],
            changes=result.changes,
            failures=result.failures,
            files_changed_or_failed=len(result.changes) + len(result.failures),
        )


# =============================================================================
# ENV VALIDATION + HOST RESOLUTION
# =============================================================================

def validate_environment() -> dict[str, str]:
    required = [_ENV_CLIENT_ID, _ENV_CLIENT_SECRET, _ENV_REFRESH_TOKEN, _ENV_DC]
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        click.echo(f"Missing required environment variables: {', '.join(missing)}", err=True)
        click.echo("\nRequired:", err=True)
        for var in required:
            click.echo(f"  export {var}='your-value-here'", err=True)
        click.echo("\nSee the module docstring for Self Client credential setup.", err=True)
        sys.exit(1)
    # Keys are the env var names themselves -- no silent lowercasing transformation.
    config = {var: os.getenv(var) for var in required}
    config[_ENV_API_HOST] = os.getenv(_ENV_API_HOST, "")
    config[_ENV_DOWNLOAD_HOST] = os.getenv(_ENV_DOWNLOAD_HOST, "")
    return config


def resolve_hosts(oauth: ZohoOAuth, config: dict[str, str]) -> tuple[str, str]:
    dc = config[_ENV_DC]
    api_host = config.get(_ENV_API_HOST) or ""
    if not api_host:
        # Prefer the api_domain Zoho returns with the token; fall back to the dc default.
        oauth.get_access_token()  # populates api_domain
        if oauth.api_domain:
            api_host = f"{oauth.api_domain.rstrip('/')}/workdrive/api/v1"
        else:
            api_host = f"https://www.zohoapis.{dc}/workdrive/api/v1"
    download_host = config.get(_ENV_DOWNLOAD_HOST) or f"https://download.zoho.{dc}/v1/workdrive"
    return api_host, download_host


# =============================================================================
# CLI
# =============================================================================

async def _run(
    cache_root: str,
    dir_key: str,
    root_folder_id: str,
    max_files: int | None,
    truncate_over_kb: int,
    never_materialize_over_mb: int,
    config: dict[str, str],
) -> None:
    oauth = ZohoOAuth(
        client_id=config[_ENV_CLIENT_ID],
        client_secret=config[_ENV_CLIENT_SECRET],
        refresh_token=config[_ENV_REFRESH_TOKEN],
        dc=config[_ENV_DC],
    )
    api_host, download_host = resolve_hosts(oauth, config)
    policy = RetentionPolicy(
        truncate_over_bytes=truncate_over_kb * 1024,
        never_materialize_over_bytes=never_materialize_over_mb * 1024 * 1024,
    )
    cache = CachedFileFolders(
        grouping_pattern=GROUPING_PATTERN,
        root_dir=os.path.abspath(cache_root),
        use_xxhash=False,
    )
    sync = SimpleZohoWorkDriveSync(
        cache=cache,
        dir_key=dir_key,
        oauth=oauth,
        api_host=api_host,
        download_host=download_host,
        policy=policy,
        root_folder_id=root_folder_id,
    )
    result = await sync.sync(max_files=max_files)
    click.echo(
        f"Sync complete: {result.insert_count} inserted, "
        f"{result.update_count} updated, {result.delete_count} deleted"
    )
    if result.failures:
        click.echo(f"{len(result.failures)} downloads failed")
    click.echo(f"Cached to: {cache_root}/key-{dir_key}/")


@click.command()
@click.option("--cache-root", required=True, help="Root directory for the CachedFileFolders cache")
@click.option("--dir-key", required=True, help="Grouping key for organizing files in the cache")
@click.option("--root-folder-id", default=None,
              help="Zoho WorkDrive folder id to descend from "
                   "(defaults to the ZOHO_WD_ROOT_FOLDER_ID env var if set)")
@click.option("--max-files", type=int, default=None, help="Limit files processed (for testing)")
@click.option("--truncate-over-kb", type=int, default=50, show_default=True,
              help="Files larger than this are stored metadata-only (truncated)")
@click.option("--never-materialize-over-mb", type=int, default=100, show_default=True,
              help="Hard ceiling: never download a file larger than this, for any reason")
@click.option("--debug", is_flag=True, help="Enable debug logging from external libraries")
def main(
    cache_root: str,
    dir_key: str,
    root_folder_id: str,
    max_files: int | None,
    truncate_over_kb: int,
    never_materialize_over_mb: int,
    debug: bool,
) -> None:
    """Zoho WorkDrive Sync - CachedFileFolders TRUNCATE / summarize / index demo."""
    configure_logging(debug)
    config = validate_environment()
    # Root folder is config, not a secret: accept it from --root-folder-id OR the
    # ZOHO_WD_ROOT_FOLDER_ID env var (so it can live in the same sourced shell script).
    raw_root = root_folder_id or os.getenv(_ENV_ROOT_FOLDER_ID)
    if not raw_root:
        click.echo(
            "Missing root folder id: pass --root-folder-id <id-or-url> or set "
            "ZOHO_WD_ROOT_FOLDER_ID.\nYou can paste a WorkDrive folder URL -- the id is "
            "extracted automatically (see the connectivity guide).",
            err=True,
        )
        sys.exit(1)
    # Tolerant input: accept a pasted folder URL and pull the id out of it.
    root_folder_id = extract_root_folder_id(raw_root)
    if root_folder_id != raw_root:
        click.echo(f"Resolved root folder id '{root_folder_id}' from URL.", err=True)
    asyncio.run(_run(
        cache_root=cache_root,
        dir_key=dir_key,
        root_folder_id=root_folder_id,
        max_files=max_files,
        truncate_over_kb=truncate_over_kb,
        never_materialize_over_mb=never_materialize_over_mb,
        config=config,
    ))


if __name__ == "__main__":
    main()
