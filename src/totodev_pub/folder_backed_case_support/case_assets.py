# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseAssets: the case's working-file playground plus the retention manifest."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

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
        the playground stays clean. One relative-to-assets exact path or glob per line.

    Retention is recorded as RULES in the manifest (exact relative paths or globs), never
    by file location, so an asset's path never changes when its keep status changes. An
    asset survives close only if at least one keep rule matches it. Blank lines and '#'
    comments in the manifest are ignored, so it stays hand-editable."""

    def __init__(self, case_folder: Path):
        self._case_folder = Path(case_folder)

    # ---- locations ----

    @property
    def folder(self) -> Path:
        """The assets playground (<case_folder>/assets), created on first access."""
        d = self._case_folder / ASSETS_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def keep_list_path(self) -> Path:
        """The retention manifest file (<case_folder>/_keep_assets.txt)."""
        return self._case_folder / KEEP_LIST_NAME

    def asset_path(self, relative_path: str) -> Path:
        """Absolute path of an asset (relative to assets folder). Does not require existence."""
        return self.folder / _norm_rel(relative_path)

    def relative_path(self, path: str | Path) -> str:
        """Manifest-safe relative asset path for `path`.

        Accepts either:
          * a relative path (normalized + validated), or
          * an absolute path that must be inside the assets folder.
        """
        p = Path(path)
        if p.is_absolute():
            assets_folder = self.folder.resolve()
            absolute = p.resolve()
            try:
                rel = absolute.relative_to(assets_folder)
            except ValueError:
                raise ValueError(
                    f"path {p!r} is not inside assets folder {assets_folder!r}"
                ) from None
            return _norm_rel(rel.as_posix())
        return _norm_rel(p.as_posix())

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
        """Retention rules in first-added order, de-duplicated. Each rule is a manifest-safe
        relative path expression under assets/ (exact path or glob). Blank lines and '#'
        comments are ignored; a missing manifest yields []. Rules are NOT filtered against
        what currently exists on disk — purge_ephemeral() simply ignores unmatched rules."""
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
            rule = self._norm_keep_rule(s)
            if rule not in seen:
                seen.add(rule)
                out.append(rule)
        return out

    def keep_set(self) -> set[str]:
        return set(self.keep_list())

    def is_kept(self, relative_path: str | Path) -> bool:
        rel = self.relative_path(relative_path)
        return any(self._rule_matches(rel, rule) for rule in self.keep_list())

    def add_keep_rules(self, *rules: str | Path) -> None:
        """Add one or more retention rules (exact relative path or glob) under assets/.
        Append-only and idempotent: existing entries are not duplicated, first-add order
        is preserved, and matching files need not exist yet."""
        current = self.keep_list()
        seen = set(current)
        added = [
            rule
            for raw in rules
            if (rule := self._norm_keep_rule(raw)) not in seen and not seen.add(rule)
        ]
        if added:
            self._write_keep_list(current + added)

    def remove_keep_rules(self, *rules: str | Path) -> None:
        """Remove rules from the manifest (rewrites it). Idempotent; unknown rules are
        ignored. Does NOT delete any asset file itself."""
        drop = {self._norm_keep_rule(rule) for rule in rules}
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
        target = self.asset_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if keep:
            self.add_keep_rules(relative_path)
        return target

    def read(self, relative_path: str) -> bytes:
        return self.asset_path(relative_path).read_bytes()

    # ---- purge ----

    def purge_ephemeral(self) -> list[str]:
        """Delete every asset that matches NO keep rule, then prune emptied subdirs.
        Returns the sorted relative paths removed. Kept files, the manifest, and the
        assets root itself are left intact; unmatched rules are ignored."""
        root = self._case_folder / ASSETS_DIR_NAME
        if not root.exists():
            return []
        rules = self.keep_list()
        purged = [
            rel
            for rel in self.list_assets()
            if not any(self._rule_matches(rel, rule) for rule in rules)
        ]
        for rel in purged:
            (root / rel).unlink(missing_ok=True)
        self._prune_empty_dirs(root)
        return sorted(purged)

    def _norm_keep_rule(self, rule: str | Path) -> str:
        """Normalize a keep rule to a manifest-safe relative expression.

        Accepts either:
          * a relative rule (exact path or glob), or
          * an absolute rule rooted in assets/ (converted to relative).
        """
        p = Path(rule)
        if p.is_absolute():
            assets_folder = self.folder.resolve()
            absolute = p.resolve()
            try:
                rel = absolute.relative_to(assets_folder)
            except ValueError:
                raise ValueError(
                    f"path {p!r} is not inside assets folder {assets_folder!r}"
                ) from None
            return _norm_rel(rel.as_posix())
        return _norm_rel(p.as_posix())

    @staticmethod
    def _rule_matches(relative_path: str, rule: str) -> bool:
        has_glob = any(ch in rule for ch in "*?[")
        if not has_glob:
            return relative_path == rule
        return PurePosixPath(relative_path).match(rule)

    @staticmethod
    def _prune_empty_dirs(root: Path) -> None:
        """Remove now-empty subdirectories, deepest first. Never removes `root`."""
        dirs = [p for p in root.rglob("*") if p.is_dir()]
        for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()            # succeeds only if empty
            except OSError:
                pass                 # not empty (or vanished) — leave it
