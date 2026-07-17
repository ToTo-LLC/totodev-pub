# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""CaseAssets: the case's working-file playground (everything under assets/)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.constants import ASSETS_DIR_NAME
from totodev_pub.folder_backed_case_support.exceptions import AssetSchemaError
from totodev_pub.folder_backed_case_support.helpers import _norm_rel


class CaseAssets:
    """Owns the case's working-file PLAYGROUND, grouping asset I/O out of the
    FolderBackedCase namespace. Reach it via `case.case_assets`.

    Retention (deciding what survives the termination purge) is entirely outside
    this class's scope — that's a case-level policy decision, not an assets-
    playground one, and this class knows nothing about it. To keep an asset,
    prefer the declarative ``AssetSpec(keep=True)`` at class-definition time; for
    a one-off, runtime decision use ``case.case_keep_files()`` on the case object
    (with the full ``assets/...``-prefixed path)."""

    def __init__(self, case_folder: Path, *,
                 asset_specs: dict[str, AssetSpec] | None = None,
                 flexible_asset_alias_loading: bool = False):
        self._case_folder = Path(case_folder)
        self._asset_specs: dict[str, AssetSpec] = dict(asset_specs) if asset_specs else {}
        self._flexible = flexible_asset_alias_loading

    # ---- locations ----

    @property
    def folder(self) -> Path:
        """The assets playground (<case_folder>/assets), created on first access."""
        d = self._case_folder / ASSETS_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def asset_path(self, relative_path: str) -> Path:
        """Absolute path of an asset (relative to assets folder). Does not require existence."""
        return self.folder / _norm_rel(relative_path)

    def relative_path(self, path: str | Path) -> str:
        """Normalized assets-relative path for `path`.

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
        """Every FILE under assets/, as a sorted list of relative (posix) paths."""
        root = self._case_folder / ASSETS_DIR_NAME
        if not root.exists():
            return []
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file()
        )

    # ---- convenience I/O (thin; the assets dir is the caller's playground) ----

    def write(self, relative_path: str, data: bytes) -> Path:
        """Write bytes to an asset path, creating parent dirs. Returns the absolute path.

        To retain the file across the termination purge, prefer declaring
        ``AssetSpec(keep=True)`` (the class-level, declarative mechanism); for a
        runtime decision, call ``case.case_keep_files(f"assets/{relative_path}")``."""
        target = self.asset_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def read(self, relative_path: str) -> bytes:
        return self.asset_path(relative_path).read_bytes()

    # ---- declared data objects (asset schema) ----

    def registered_aliases(self) -> list[str]:
        """Declared aliases, in declaration order."""
        return list(self._asset_specs)

    def _require_spec(self, alias: str) -> AssetSpec:
        try:
            return self._asset_specs[alias]
        except KeyError:
            known = ", ".join(self._asset_specs) or "(none)"
            raise KeyError(
                f"No asset alias {alias!r} is registered. Known aliases: {known}."
            ) from None

    def dataclass_paths(self, alias: str) -> list[Path]:
        """Absolute paths of EXISTING files matching the alias's pattern (glob-aware),
        sorted; empty if none match."""
        spec = self._require_spec(alias)
        pattern = spec.relative_path
        if any(ch in pattern for ch in "*?["):
            return [
                self.asset_path(rel)
                for rel in self.list_assets()
                if self._rule_matches(rel, pattern)
            ]
        p = self.asset_path(pattern)
        return [p] if p.exists() else []

    def dataclass_path(self, alias: str) -> Path:
        """The single existing file for an alias. FileNotFoundError if none,
        ValueError if a glob matched more than one (use dataclass_paths /
        load_dataclasses / load_dataclass_file)."""
        spec = self._require_spec(alias)
        if spec.many:
            raise ValueError(
                f"Asset {alias!r} is many=True; use dataclass_paths() or "
                "load_dataclasses()."
            )
        paths = self.dataclass_paths(alias)
        if not paths:
            raise FileNotFoundError(
                f"Asset {alias!r} ({spec.relative_path!r}) not found under {self.folder}."
            )
        if len(paths) > 1:
            raise ValueError(
                f"Asset {alias!r} matched {len(paths)} files; declare many=True and "
                "use load_dataclasses(), or use dataclass_paths() + "
                "load_dataclass_file() to load each."
            )
        return paths[0]

    def load_dataclass(self, alias: str):
        """Resolve a singular alias to exactly one file and deserialize it.

        Raises ``ValueError`` when the alias is ``many=True`` (use
        ``load_dataclasses``)."""
        spec = self._require_spec(alias)
        if spec.many:
            raise ValueError(
                f"Asset {alias!r} is many=True; use load_dataclasses()."
            )
        return self._load(spec, self.dataclass_path(alias))

    def load_dataclasses(self, alias: str) -> list:
        """Resolve a ``many=True`` alias to zero or more files and deserialize each.

        Returns an empty list when nothing matches. Raises ``ValueError`` when
        the alias is not ``many=True`` (use ``load_dataclass``)."""
        spec = self._require_spec(alias)
        if not spec.many:
            raise ValueError(
                f"Asset {alias!r} is not many=True; use load_dataclass()."
            )
        return [self._load(spec, path) for path in self.dataclass_paths(alias)]

    def load_dataclass_file(self, relative_path: str | Path):
        """Deserialize ONE explicit file, using the loader of the spec whose pattern
        matches it. ValueError if no/ambiguous spec; FileNotFoundError if absent."""
        rel = self.relative_path(relative_path)
        matches = [
            s for s in self._asset_specs.values()
            if rel == s.relative_path
            or (any(ch in s.relative_path for ch in "*?[")
                and self._rule_matches(rel, s.relative_path))
        ]
        if not matches:
            raise ValueError(f"No registered asset spec matches {rel!r}.")
        if len(matches) > 1:
            raise ValueError(
                f"{rel!r} matches multiple asset specs: {[m.alias for m in matches]}."
            )
        path = self.asset_path(rel)
        if not path.exists():
            raise FileNotFoundError(f"Asset file {path} does not exist.")
        return self._load(matches[0], path)

    def _load(self, spec: AssetSpec, path: Path):
        if not path.exists():
            raise FileNotFoundError(
                f"Asset file {path} (alias {spec.alias!r}) does not exist."
            )
        loader = spec.loader
        if loader is None:
            if not self._flexible:
                raise AssetSchemaError(
                    f"alias {spec.alias!r} has no loader and flexible loading is off."
                )
            return LazyLoadedFileData(str(path))
        if loader is Path:
            return path
        if isinstance(loader, type) and issubclass(loader, FileMappedPydanticMixin):
            return loader.load(str(path), acquire_lock=False)
        return loader(path)

    # ---- internal helpers ----

    @staticmethod
    def _rule_matches(relative_path: str, rule: str) -> bool:
        has_glob = any(ch in rule for ch in "*?[")
        if not has_glob:
            return relative_path == rule
        return PurePosixPath(relative_path).match(rule)
