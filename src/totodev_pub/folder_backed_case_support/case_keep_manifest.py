# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseKeepManifest: case-root retention manifest and purge engine."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from totodev_pub.folder_backed_case_support.case_logging import (
    LogRetention, get_case_log_retention,
)
from totodev_pub.folder_backed_case_support.constants import (
    CASE_LOG_KEEP_RULE,
    FRAMEWORK_KEEP_RULES,
    KEEP_LIST_NAME,
    LEASE_NAME,
)
from totodev_pub.folder_backed_case_support.helpers import _norm_rel

logger = logging.getLogger(__name__)

# Hard-coded paths never deleted by purge, regardless of manifest contents.
_PURGE_SKIP_NAMES = frozenset({KEEP_LIST_NAME, LEASE_NAME})


class CaseKeepManifest:
    """Owns `<case_folder>/_keep.txt` and case-wide ephemeral purge.

    Rules are case-relative exact paths or globs. Blank lines and ``#`` comments are
    ignored. Purge walks every file under the case folder, deletes anything matching
    no rule (except hard skip paths), then prunes empty subdirectories."""

    def __init__(self, case_folder: Path) -> None:
        self._case_folder = Path(case_folder)

    @property
    def path(self) -> Path:
        """The retention manifest file (<case_folder>/_keep.txt)."""
        return self._case_folder / KEEP_LIST_NAME

    def list_rules(self) -> list[str]:
        """Retention rules in first-added order, de-duplicated (case-relative)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            rule = self._norm_rule(s)
            if rule not in seen:
                seen.add(rule)
                out.append(rule)
        return out

    def rule_set(self) -> set[str]:
        return set(self.list_rules())

    def is_kept(self, relative_path: str | Path) -> bool:
        rel = self._norm_rule(relative_path)
        return any(self._rule_matches(rel, rule) for rule in self.list_rules())

    def add_rules(self, *rules: str | Path) -> None:
        """Append case-relative keep rules. Idempotent; first-add order preserved."""
        current = self.list_rules()
        seen = set(current)
        added = [
            rule
            for raw in rules
            if (rule := self._norm_rule(raw)) not in seen and not seen.add(rule)
        ]
        if added:
            self._write_rules(current + added)

    def remove_rules(self, *rules: str | Path) -> None:
        """Remove rules from the manifest. Does not delete files on disk."""
        drop = {self._norm_rule(rule) for rule in rules}
        current = self.list_rules()
        kept = [r for r in current if r not in drop]
        if len(kept) != len(current):
            self._write_rules(kept)

    def ensure_framework_rules(self) -> None:
        """Idempotently seed baseline framework keep rules.

        When the process-global log-retention policy is ``LogRetention.RETAIN``,
        also seeds ``CASE_LOG_KEEP_RULE`` so purge keeps ``logs/case.log``.
        """
        rules: list[str] = list(FRAMEWORK_KEEP_RULES)
        if get_case_log_retention() is LogRetention.RETAIN:
            rules.append(CASE_LOG_KEEP_RULE)
        self.add_rules(*rules)

    def purge(self) -> list[str]:
        """Delete every case file matching no keep rule; prune empty dirs.

        ONE purge process for ALL case ephemera — this is the single chokepoint every
        caller (termination, the CaseManager's redundant purge sweep, ad hoc test/ops
        cleanup) goes through. Returns sorted case-relative paths of files DELETED.

        Hard skip paths and the case root itself are never deleted. Log files are not
        special: they survive only when a keep rule matches them."""
        root = self._case_folder
        if not root.exists():
            return []
        rules = self.list_rules()
        purged: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel in _PURGE_SKIP_NAMES:
                continue
            if any(self._rule_matches(rel, rule) for rule in rules):
                continue
            path.unlink(missing_ok=True)
            logger.debug("purged ephemeral file: %s", rel)
            purged.append(rel)
        self._prune_empty_dirs(root)
        return sorted(purged)

    def _write_rules(self, entries: list[str]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{e}\n" for e in entries), encoding="utf-8")

    def _norm_rule(self, rule: str | Path) -> str:
        """Normalize a keep rule to a manifest-safe case-relative expression."""
        p = Path(rule)
        if p.is_absolute():
            case_root = self._case_folder.resolve()
            absolute = p.resolve()
            try:
                rel = absolute.relative_to(case_root)
            except ValueError:
                raise ValueError(
                    f"path {p!r} is not inside case folder {case_root!r}"
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
                d.rmdir()
            except OSError:
                pass
