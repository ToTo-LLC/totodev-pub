# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Best-effort discovery of @case_type_registry.register case classes."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

import yaml

from totodev_pub.case_testing.errors import WorkbenchError

# Matches common forms:
#   @case_type_registry.register
#   @case_type_registry.register()
# followed (soon) by `class ClassName`
_REGISTER_DECORATOR_RE = re.compile(
    r"@case_type_registry\.register(?:\s*\([^)]*\))?\s*\n"
    r"(?:@[^\n]+\n)*"  # other decorators
    r"class\s+(\w+)\s*[:(]",
    re.MULTILINE,
)


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward from *start* (default cwd) looking for ``pyproject.toml``."""
    cur = (start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def ensure_dir(path: Path, *, label: str) -> Path:
    """Create *path* if missing and parent exists; else raise WorkbenchError."""
    path = path.resolve()
    if path.exists():
        if not path.is_dir():
            raise WorkbenchError(
                f"{label} path exists but is not a directory: {path}. "
                "Remove or rename it, then retry."
            )
        return path
    parent = path.parent
    if not parent.exists():
        raise WorkbenchError(
            f"Cannot create {label} at {path}: parent directory does not exist "
            f"({parent}). Create the parent first, or pass an absolute path whose "
            "parent already exists."
        )
    path.mkdir(parents=False, exist_ok=True)
    return path


def scan_source_roots(source_roots: Iterable[Path]) -> dict[str, str]:
    """Map class name -> module file path (as string). Best-effort text scan."""
    index: dict[str, str] = {}
    for root in source_roots:
        root = root.resolve()
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if py.name.startswith(".") or "site-packages" in py.parts:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in _REGISTER_DECORATOR_RE.finditer(text):
                index[match.group(1)] = str(py)
    return index


def default_index_path(project_root: Path | None, scratch_parent: Path | None = None) -> Path:
    if project_root is not None:
        return project_root / "volatile" / "case-workbench" / "class_index.yaml"
    if scratch_parent is not None:
        return scratch_parent / "class_index.yaml"
    return Path.cwd() / "volatile" / "case-workbench" / "class_index.yaml"


class ClassIndex:
    """Lazy class-name → module loader backed by an optional on-disk cache."""

    def __init__(
        self,
        *,
        source_roots: list[Path] | None = None,
        case_modules: list[str] | None = None,
        cache_path: Path | None = None,
        project_root: Path | None = None,
    ):
        self.source_roots = [Path(p).resolve() for p in (source_roots or [])]
        self.case_modules = list(case_modules or [])
        self.cache_path = cache_path
        self.project_root = project_root
        self._name_to_file: dict[str, str] = {}
        self._explicit_only = bool(self.case_modules)

    def refresh(self) -> dict[str, str]:
        if self._explicit_only:
            for mod in self.case_modules:
                importlib.import_module(mod)
            # Record stub for doctor visibility
            self._name_to_file = {f"module:{m}": m for m in self.case_modules}
            self._write_cache(mode="explicit_modules", modules=self.case_modules)
            return dict(self._name_to_file)

        self._name_to_file = scan_source_roots(self.source_roots)
        self._write_cache(mode="scan", mapping=self._name_to_file)
        return dict(self._name_to_file)

    def load_cache_or_refresh(self) -> dict[str, str]:
        if self._explicit_only:
            return self.refresh()
        cached = self._read_cache()
        if cached is not None:
            self._name_to_file = cached
            return dict(self._name_to_file)
        return self.refresh()

    def ensure_registered(self, class_name: str, registry) -> type:
        """Import indexed module if *class_name* is not yet on *registry*."""
        existing = registry.resolve_case_type(class_name)
        if existing is not None:
            return existing

        if self._explicit_only:
            for mod in self.case_modules:
                importlib.import_module(mod)
            existing = registry.resolve_case_type(class_name)
            if existing is not None:
                return existing
            raise WorkbenchError(
                f"Case class {class_name!r} is not registered after importing "
                f"case_modules={self.case_modules!r}. Check the class name and "
                "that those modules call @case_type_registry.register."
            )

        path = self._name_to_file.get(class_name)
        if path is None:
            # Refresh once on miss
            self.refresh()
            path = self._name_to_file.get(class_name)
        if path is None:
            raise WorkbenchError(
                f"Case class {class_name!r} is not registered and was not found "
                "by the class-index scan. Import the module yourself, pass "
                "case_modules=[...] to for_project(), or call "
                "wb.refresh_class_index() after adding the file."
            )
        self._import_file(Path(path))
        existing = registry.resolve_case_type(class_name)
        if existing is None:
            raise WorkbenchError(
                f"Imported {path} but {class_name!r} is still unregistered. "
                "Confirm the @case_type_registry.register decorator is on that class."
            )
        return existing

    def _import_file(self, path: Path) -> None:
        path = path.resolve()
        # Prefer package import when under a known source root
        for root in self.source_roots:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if rel.suffix != ".py":
                continue
            parts = list(rel.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mod_name = ".".join(parts)
            if mod_name:
                importlib.import_module(mod_name)
                return
        # Fallback: load by file path under a synthetic module name
        mod_name = f"_workbench_case_{path.stem}_{abs(hash(str(path))) & 0xfffffff}"
        if mod_name in sys.modules:
            return
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise WorkbenchError(f"Cannot import case module from {path}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

    def _write_cache(self, *, mode: str, mapping: dict | None = None, modules: list | None = None) -> None:
        if self.cache_path is None:
            return
        payload = {
            "mode": mode,
            "modules": modules or [],
            "classes": mapping or {},
            "mtimes": {},
        }
        if mapping:
            for name, fpath in mapping.items():
                try:
                    payload["mtimes"][name] = Path(fpath).stat().st_mtime
                except OSError:
                    pass
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _read_cache(self) -> Optional[dict[str, str]]:
        if self.cache_path is None or not self.cache_path.is_file():
            return None
        try:
            data = yaml.safe_load(self.cache_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        if data.get("mode") != "scan":
            return None
        classes = data.get("classes") or {}
        mtimes = data.get("mtimes") or {}
        for name, fpath in classes.items():
            try:
                if Path(fpath).stat().st_mtime != mtimes.get(name):
                    return None
            except OSError:
                return None
        return {str(k): str(v) for k, v in classes.items()}
