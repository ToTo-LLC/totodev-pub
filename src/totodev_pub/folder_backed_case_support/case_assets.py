# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseAssets: the case's working-file playground plus the retention manifest."""

from __future__ import annotations

from pathlib import Path

from totodev_pub.folder_backed_case_support.constants import ASSETS_DIR_NAME, KEEP_LIST_NAME
from totodev_pub.folder_backed_case_support.helpers import _norm_rel


class CaseAssets:
    """Owns the case's working files and the retention decision, grouping all asset
    operations OUT of the FolderBackedCase namespace. Reach it via `case.assets`.

    Two deliberately separated locations:
      * <case_folder>/assets/          — the PLAYGROUND. Entirely under downstream code's
        control: this class never moves, renames, or silently injects files there, so a
        caller can organize subfolders however it likes and trust what it put there.
      * <case_folder>/_keep_assets.txt — the retention MANIFEST, kept OUTSIDE assets/ so
        the playground stays clean. One relative-to-assets path per line.

    Retention is recorded by NAME in the manifest, never by file location, so an asset's
    path never changes when its keep status changes. Membership is explicit: an asset
    survives close only if keep_asset() has listed it. Blank lines and '#' comments in
    the manifest are ignored, so it stays hand-editable."""

    def __init__(self, case_folder: Path):
        self._case_folder = Path(case_folder)

    # ---- locations ----

    @property
    def root(self) -> Path:
        """The assets playground (<case_folder>/assets), created on first access."""
        d = self._case_folder / ASSETS_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def keep_list_path(self) -> Path:
        """The retention manifest file (<case_folder>/_keep_assets.txt)."""
        return self._case_folder / KEEP_LIST_NAME

    def path_for(self, relative_path: str) -> Path:
        """Absolute path of an asset (relative to root). Does not require existence."""
        return self.root / _norm_rel(relative_path)

    # ---- asset enumeration ----

    def list_assets(self) -> list[str]:
        """Every FILE under assets/, as a sorted list of relative (posix) paths. The
        manifest lives outside assets/, so it never appears here."""
        root = self._case_folder / ASSETS_DIR_NAME
        if not root.exists():
            return []
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file()
        )

    # ---- retention manifest ----

    def keep_list(self) -> list[str]:
        """Retained relative paths in first-added order, de-duplicated. Blank lines and
        '#' comments are ignored; a missing manifest yields []. Entries are NOT filtered
        against what currently exists on disk — purge_ephemeral() simply ignores entries
        with no matching file."""
        try:
            raw = self.keep_list_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            rel = _norm_rel(s)
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
        return out

    def keep_set(self) -> set[str]:
        return set(self.keep_list())

    def is_kept(self, relative_path: str) -> bool:
        return _norm_rel(relative_path) in self.keep_set()

    def keep_asset(self, *relative_paths: str) -> None:
        """Mark one or more assets (relative to root) for retention. Append-only and
        idempotent: existing entries are not duplicated, first-add order is preserved,
        and the file need not exist yet (you may declare intent before writing)."""
        current = self.keep_list()
        seen = set(current)
        added = [
            rel for rp in relative_paths
            if (rel := _norm_rel(rp)) not in seen and not seen.add(rel)
        ]
        if added:
            self._write_keep_list(current + added)

    def unkeep_asset(self, *relative_paths: str) -> None:
        """Remove entries from the manifest (rewrites it). Idempotent; unknown entries
        are ignored. Does NOT delete the asset file itself."""
        drop = {_norm_rel(rp) for rp in relative_paths}
        current = self.keep_list()
        kept = [r for r in current if r not in drop]
        if len(kept) != len(current):
            self._write_keep_list(kept)

    def _write_keep_list(self, entries: list[str]) -> None:
        path = self.keep_list_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{e}\n" for e in entries), encoding="utf-8")

    # ---- convenience I/O (thin; the assets dir is the caller's playground) ----

    def write(self, relative_path: str, data: bytes, *, keep: bool = False) -> Path:
        """Write bytes to an asset path, creating parent dirs. keep=True also records it
        in the retention manifest. Returns the absolute path."""
        target = self.path_for(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if keep:
            self.keep_asset(relative_path)
        return target

    def read(self, relative_path: str) -> bytes:
        return self.path_for(relative_path).read_bytes()

    # ---- purge ----

    def purge_ephemeral(self) -> list[str]:
        """Delete every asset NOT named in the manifest, then prune emptied subdirs.
        Returns the sorted relative paths removed. Kept files, the manifest, and the
        assets root itself are left intact; manifest entries with no file are ignored."""
        root = self._case_folder / ASSETS_DIR_NAME
        if not root.exists():
            return []
        keep = self.keep_set()
        purged = [rel for rel in self.list_assets() if rel not in keep]
        for rel in purged:
            (root / rel).unlink(missing_ok=True)
        self._prune_empty_dirs(root)
        return sorted(purged)

    @staticmethod
    def _prune_empty_dirs(root: Path) -> None:
        """Remove now-empty subdirectories, deepest first. Never removes `root`."""
        dirs = [p for p in root.rglob("*") if p.is_dir()]
        for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()            # succeeds only if empty
            except OSError:
                pass                 # not empty (or vanished) — leave it
