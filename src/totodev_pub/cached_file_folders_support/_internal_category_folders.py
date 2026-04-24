# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

r"""
InternalCategoryFolders - Internal folder layout helper for CachedFileFolders.

This module is an internal implementation detail used by CachedFileFolders storage.
It mirrors the behavior needed by CachedFileStorageManager without exposing a public,
long-term API. It is not intended for use outside this library.

Key capabilities (subset needed by storage):
- Pattern-defined hierarchy with `{field}` segments and constants
- Building a grouping folder from a key (list/dict/model)
- Enumerating existing grouping folders with filters
- Inferring a key from a filesystem path
- Purging folders and cleaning up empty parents
- Exposing `root_dir`, `pattern`, and `key_names()`

Differences from public CategoryFolders:
- No deprecation warnings
- Docstrings emphasize internal use only
"""

import re
from pathlib import Path
from typing import Type, TypeVar, Union, Dict, Any, Optional, List, Iterator, Tuple, Sequence
from pydantic import BaseModel, create_model

KeyModel = TypeVar('KeyModel', bound=BaseModel)


class InternalCategoryFolders:
    """
    Internal pattern-driven folder manager with optional Pydantic validation.
    Not part of the public API. Subject to change without notice.
    """
    def __init__(self,
                 pattern: str,
                 root_dir: Union[str, Path],
                 key_class: Optional[Type[KeyModel]] = None):
        normalized_pattern = pattern.strip().strip('/').strip('\\')
        self.pattern = normalized_pattern
        self.key_class = self._resolve_key_class(pattern, key_class)
        self.root_dir = Path(root_dir)
        if not self.root_dir.exists():
            raise FileNotFoundError(f"Root directory '{self.root_dir}' does not exist")
        self._validate_pattern_matches_model()
        self._save_key_order()
        self._regex_pattern, self._field_capture_positions = self._build_regex_for_pattern(self.pattern)

    def _resolve_key_class(self, pattern: str, key_class: Optional[Type[KeyModel]]) -> Type[KeyModel]:
        if key_class is not None:
            return key_class
        field_names = re.findall(r'\{([^}]+)\}', pattern)
        if not field_names:
            return create_model('GenericKey', __base__=BaseModel)
        field_definitions = {name: (str, ...) for name in field_names}
        return create_model('GenericKey', **field_definitions, __base__=BaseModel)

    def _save_key_order(self) -> None:
        field_matches = re.findall(r'\{([^}]+)\}', self.pattern)
        self._key_order = field_matches

    def key_names(self) -> List[str]:
        return list(self._key_order)

    def _validate_pattern_matches_model(self) -> None:
        pattern_fields = set(re.findall(r'\{([^}]+)\}', self.pattern))
        model_fields = set(self.key_class.model_fields.keys())
        missing_in_model = pattern_fields - model_fields
        if missing_in_model:
            raise ValueError(
                f"Pattern parameterized fields must exist on key class. "
                f"Missing in key_class: {missing_in_model}. "
                f"Pattern fields: {sorted(pattern_fields)}. "
                f"Key class: {self.key_class.__name__}. Available fields: {sorted(model_fields)}."
            )

    @staticmethod
    def _build_regex_for_pattern(pattern: str) -> Tuple[re.Pattern, Dict[str, int]]:
        import os
        pattern_parts = pattern.split("/")
        regex_parts: List[str] = []
        field_capture_positions: Dict[str, int] = {}
        capture_count = 2
        for part in pattern_parts:
            if part.startswith("{") and part.endswith("}"):
                field_name = part[1:-1]
                regex_parts.append(r"([^/]+)")
                capture_count += 1
                field_capture_positions[field_name] = capture_count
            else:
                escaped_part = re.escape(part)
                regex_parts.append(escaped_part)
        dir_sep = re.escape(os.sep)
        full_regex_pattern = f"(^|{dir_sep})(" + dir_sep.join(regex_parts) + f")($|{dir_sep})"
        compiled = re.compile(full_regex_pattern)
        return compiled, field_capture_positions

    def _validate_path_component(self, value: str) -> None:
        if not value:
            raise ValueError("Directory name cannot be empty")
        windows_illegal = '<>:"|?*\\/'
        for char in windows_illegal:
            if char in value:
                raise ValueError(f"Directory name contains illegal character '{char}': '{value}'")
        if value.startswith(' ') or value.endswith(' '):
            raise ValueError(f"Directory name cannot start or end with spaces: '{value}'")
        if value.startswith('.') and value in ['.', '..']:
            raise ValueError(f"Directory name cannot be '{value}' (reserved)")
        for char in value:
            if ord(char) < 32:
                raise ValueError(f"Directory name contains control character (ASCII {ord(char)}): '{value}'")
        if len(value) > 255:
            raise ValueError(f"Directory name too long (max 255 characters): '{value}' (length: {len(value)})")
        self._validate_additional_filesystem_restrictions(value)

    def _validate_additional_filesystem_restrictions(self, value: str) -> None:
        if '/' in value or '\\' in value:
            raise ValueError(f"Directory name contains path separator: '{value}'")
        import unicodedata
        normalized = unicodedata.normalize('NFC', value)
        if normalized != value:
            raise ValueError(f"Directory name contains non-normalized Unicode characters: '{value}' (normalized: '{normalized}')")
        problematic_chars = ['`', '$', '!', '#', '%', '^', '&', '(', ')', '+', '=', '[', ']', '{', '}', ';', "'", '"', ',', '~']
        for char in problematic_chars:
            if char in value:
                raise ValueError(f"Directory name contains potentially problematic character '{char}': '{value}'")
        if value.startswith('.') or value.endswith('.'):
            raise ValueError(f"Directory name cannot start or end with dots: '{value}'")
        if '  ' in value:
            raise ValueError(f"Directory name contains consecutive spaces: '{value}'")

    def _sanitize_path_component(self, value: str) -> str:
        self._validate_path_component(value)
        return value

    def _is_sequence_type(self, key: Any) -> bool:
        return isinstance(key, (list, tuple)) or (hasattr(key, '__iter__') and not isinstance(key, (str, dict)))

    def _normalize_key_dict(self, key: Union[KeyModel, Dict[str, Any], List[str]], validate: bool = False) -> Dict[str, Any]:
        if isinstance(key, self.key_class):
            return key.model_dump()
        if isinstance(key, dict):
            if validate:
                model = self.key_class(**key)
                return model.model_dump()
            return dict(key)
        if self._is_sequence_type(key):
            key_list = list(key)
            if len(key_list) != len(self._key_order):
                raise ValueError(
                    f"Sequence length {len(key_list)} doesn't match number of parameterized fields {len(self._key_order)}. "
                    f"Received: {key_list}, Expected {len(self._key_order)} values in order: {self._key_order}"
                )
            data = dict(zip(self._key_order, key_list))
            if validate:
                model = self.key_class(**data)
                return model.model_dump()
            return data
        raise TypeError(
            f"Key must be a {self.key_class.__name__} instance, dict, or sequence of strings. Got {type(key).__name__}: {key}"
        )

    def _build_path_from_key(self, key: Union[KeyModel, Dict[str, Any], Sequence[str]], validate: bool = True) -> Path:
        data = self._normalize_key_dict(key, validate=validate)
        path_parts: List[str] = []
        pattern_parts = self.pattern.split("/")
        for part in pattern_parts:
            if part.startswith("{") and part.endswith("}") and part.count("{") == 1 and part.count("}") == 1:
                field_name = part[1:-1]
                value = data[field_name]
                str_value = value if isinstance(value, str) else str(value)
                self._validate_path_component(str_value)
                path_parts.append(str_value)
            else:
                try:
                    placeholders = re.findall(r'\{([^}]+)\}', part)
                    for placeholder in placeholders:
                        if placeholder not in data:
                            raise KeyError(f"Placeholder '{placeholder}' not found in data: {list(data.keys())}")
                    formatted_part = part.format(**{k: v if isinstance(v, str) else str(v) for k, v in data.items()})
                    self._validate_path_component(formatted_part)
                    path_parts.append(formatted_part)
                except (KeyError, ValueError) as e:
                    raise ValueError(f"Failed to format pattern segment '{part}' with data {data}: {e}")
        return self.root_dir / Path(*path_parts)

    def folder(self, key: Union[KeyModel, Dict[str, Any], Sequence[str]], create: bool = False) -> Path:
        folder_path = self._build_path_from_key(key, validate=True)
        if create:
            folder_path.mkdir(parents=True, exist_ok=True)
        return folder_path

    def infer_key(self, path: Union[str, Path]) -> KeyModel:
        path_str = str(path)
        if not hasattr(self, "_regex_pattern") or self._regex_pattern is None:
            self._regex_pattern, self._field_capture_positions = self._build_regex_for_pattern(self.pattern)
        match = self._regex_pattern.search(path_str)
        if not match:
            raise ValueError(
                f"Path does not match expected pattern. Path: '{path_str}', Pattern: '{self.pattern}'"
            )
        extracted: Dict[str, Any] = {}
        for field_name, group_index in self._field_capture_positions.items():
            extracted[field_name] = match.group(group_index)
        model_instance = self.key_class(**extracted)
        return model_instance

    def _normalize_filters(self, filters: Optional[Union[Dict[str, str], List[str]]]) -> Dict[str, str]:
        if filters is None:
            return {}
        if isinstance(filters, dict):
            extra = [f for f in filters.keys() if f not in self._key_order]
            if extra:
                raise ValueError(
                    f"Filter field(s) not found in pattern: {extra}. Available fields: {self._key_order}"
                )
            normalized: Dict[str, str] = {}
            for field_name in self._key_order:
                if field_name in filters:
                    pattern = filters[field_name]
                    normalized[field_name] = pattern if pattern != "" else "*"
                else:
                    normalized[field_name] = "*"
            return normalized
        if not isinstance(filters, list):
            raise TypeError("filters must be a dict[str,str], list[str], or None")
        if len(filters) != len(self._key_order):
            raise ValueError(
                f"List filter length {len(filters)} doesn't match number of parameterized fields {len(self._key_order)}. "
                f"Expected order: {self._key_order}"
            )
        normalized = {}
        for field_name, pattern in zip(self._key_order, filters):
            if pattern is None:
                normalized[field_name] = "*"
                continue
            pattern_str = str(pattern)
            normalized[field_name] = pattern_str if pattern_str != "" else "*"
        return normalized

    def _matches_filters(self, field_values: List[str], filters: Dict[str, str]) -> bool:
        import fnmatch
        field_to_value = dict(zip(self._key_order, field_values))
        for field_name, pattern in filters.items():
            if field_name not in field_to_value:
                return False
            if not fnmatch.fnmatch(field_to_value[field_name], pattern):
                return False
        return True

    def existing_folders(self,
                         filters: Optional[Union[Dict[str, str], List[str]]] = None,
                         reverse: bool = False) -> Iterator[Path]:
        import fnmatch  # noqa: F401  (kept for parity; used indirectly via _matches_filters)
        normalized_filters = self._normalize_filters(filters)
        parts = self.pattern.split("/")
        def _walk(base: Path, idx: int, collected_field_values: List[str]) -> Iterator[Path]:
            if idx >= len(parts):
                if self._matches_filters(collected_field_values, normalized_filters):
                    yield base
                return
            part = parts[idx]
            if part.startswith("{") and part.endswith("}"):
                field_name = part[1:-1]
                if not base.exists() or not base.is_dir():
                    return
                try:
                    entries = [p for p in base.iterdir() if p.is_dir()]
                except (OSError, PermissionError):
                    return
                entries.sort()
                if reverse:
                    entries.reverse()
                for sub in entries:
                    value = sub.name
                    next_values = collected_field_values + [value]
                    if field_name in normalized_filters:
                        import fnmatch as _fn  # local alias to avoid shadow
                        if not _fn.fnmatch(value, normalized_filters[field_name]):
                            continue
                    yield from _walk(sub, idx + 1, next_values)
            else:
                nxt = base / part
                yield from _walk(nxt, idx + 1, collected_field_values)
        return _walk(self.root_dir, 0, [])

    def purge_folders(self,
                      filters: Optional[Union[Dict[str, str], List[str]]] = None,
                      dry_run: bool = False) -> List[Path]:
        import shutil
        targets = list(self.existing_folders(filters=filters))
        if dry_run:
            return targets
        deleted: List[Path] = []
        affected_parents: set[Path] = set()
        for folder in targets:
            current = folder
            while current != self.root_dir and current.parent != current:
                affected_parents.add(current.parent)
                current = current.parent
            try:
                if folder.exists():
                    shutil.rmtree(folder)
                    deleted.append(folder)
            except (OSError, PermissionError):
                pass
        for parent in sorted(affected_parents, key=lambda p: len(p.parts), reverse=True):
            try:
                if parent.exists() and parent.is_dir() and parent != self.root_dir and not any(parent.iterdir()):
                    parent.rmdir()
            except (OSError, PermissionError):
                pass
        return deleted


