# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Case-type catalog and name-driven resolution for the FolderBackedCase family.

``CaseTypeRegistry`` maps ``case_object_type`` name → subclass and provides:

  - ``rehydrate(folder)`` — open as the correct class (acquires lease via ``__init__``)
  - ``peek_class(folder)`` — sniff type name or resolve registered class

Static lock-free peeks (``peek_case_record``, ``peek_case_events``, etc.) live on
``FolderBackedCase``, not here.

Use the module singleton ``case_type_registry`` in application code. Registry-free
construction works when you already know the class: ``MyCase(folder)`` or
``MyCase.create_case_in_folder(...)``.

Planned ``CaseManager`` (draft: notebooks/DEVDAVE/case_manager_classes/CaseManager
Model.md) may own registration policy; the registry itself stays manager-free.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from totodev_pub.folder_backed_case_support.constants import RECORD_NAME
from totodev_pub.folder_backed_case_support.exceptions import UnregisteredCaseTypeError

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase


class CaseTypeRegistry:
    """Type catalog plus ``peek_class`` / ``rehydrate``.

    Keyed by bare ``__name__`` (the ``case_object_type`` stamped on each record).
    Static folder peeks live on ``FolderBackedCase``, not here.
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[FolderBackedCase]] = {}

    def register_case_types(self, *case_classes: type[FolderBackedCase]) -> None:
        """Register one or more case types, keyed by class ``__name__``.

        That name must match ``case_object_type`` on disk (enforced at bind/flush).
        """
        for case_cls in case_classes:
            self._registry[case_cls.__name__] = case_cls

    def register(self, case_cls: type[FolderBackedCase]) -> type[FolderBackedCase]:
        """Decorator sugar for ``register_case_types``."""
        self.register_case_types(case_cls)
        return case_cls

    def resolve_case_type(
        self,
        type_name: str | None,
        *,
        registry: dict[str, type[FolderBackedCase]] | None = None,
    ) -> type[FolderBackedCase] | None:
        """Look up a class by stored bare name. ``registry`` overrides for tests."""
        if type_name is None:
            return None
        return (registry if registry is not None else self._registry).get(type_name)

    @staticmethod
    def _sniff_case_type(folder: Path) -> str | None:
        """Read ``case_object_type`` from the record file without full validation."""
        record_path = Path(folder) / RECORD_NAME
        try:
            text = record_path.read_text()
        except FileNotFoundError:
            return None
        m = re.search(r'^case_object_type:\s*["\']?([^"\'\s]+)', text, re.M)
        return m.group(1) if m else None

    def rehydrate(
        self,
        folder: Path,
        *,
        registry: dict[str, type[FolderBackedCase]] | None = None,
    ) -> FolderBackedCase:
        """Resolve class from disk and construct a live, lease-holding case.

        Call ``case_detach()`` on the returned instance when you are done with it.
        Raises ``UnregisteredCaseTypeError`` when the sniffed type is not registered.
        """
        case_cls = self.peek_class(folder, return_class_object=True, registry=registry)
        return case_cls(folder)

    def peek_class(
        self,
        folder: Path,
        *,
        return_class_object: bool = False,
        registry: dict[str, type[FolderBackedCase]] | None = None,
    ) -> type[FolderBackedCase] | str:
        """Deduce case type from ``case_object_type`` on disk.

        ``return_class_object=False``: bare name (str), registry-free.
        ``return_class_object=True``: registered class; raises if unknown.

        Raises ``FileNotFoundError`` when no record; ``ValueError`` when type missing.
        """
        record_path = Path(folder) / RECORD_NAME
        if not record_path.exists():
            raise FileNotFoundError(
                f"No case record at {record_path}: cannot deduce a case type for a folder "
                "that is not an initialized case."
            )
        tname = self._sniff_case_type(folder)
        if tname is None:
            raise ValueError(
                f"{record_path} carries no case_object_type; it is not a valid case record."
            )
        if not return_class_object:
            return tname
        case_cls = self.resolve_case_type(tname, registry=registry)
        if case_cls is None:
            raise UnregisteredCaseTypeError(tname)
        return case_cls


case_type_registry = CaseTypeRegistry()
