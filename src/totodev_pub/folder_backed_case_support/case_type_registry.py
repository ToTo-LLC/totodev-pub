# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The case-type catalog and name-driven resolution surface for the FolderBackedCase family.

`CaseTypeRegistry` owns the global type catalog (name -> subclass), type sniffing and
resolution, and the two strict resolution methods:
  - `rehydrate`  — folder -> live, lease-holding case instance.
  - `peek_class` — folder -> bare type name (str), or with return_class_object=True the
                   registered class object.

Static, registry-free folder reads (peek_case_record / peek_case_events / peek_case_assets)
live on `FolderBackedCase` itself — they are class-agnostic utilities that don't touch the
catalog at all.

A single process-wide instance, `case_type_registry`, is exported for callers to use
directly (e.g. `case_type_registry.rehydrate(folder)`). The registry is deliberately
manager-free: type resolution works without any CaseManager owning the catalog.

When you need it: ONLY for name-driven resolution — `rehydrate` and `peek_class`. If you
already hold the concrete case class you can ignore this entirely: `MyCase.create_case_in_folder()`
and `MyCase(folder)` work registry-free (the construction type gate is a local name check,
not a registry lookup).
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
    """The type catalog plus the folder-peek/rehydrate surface.

    Holds a name -> subclass mapping (keyed by the bare `__name__`, the EXACT value
    stamped into every record's `case_object_type`) and the two name-driven resolution
    methods: `rehydrate` (folder -> live case) and `peek_class` (folder -> type name or
    class object). Static folder reads live on FolderBackedCase, not here.

    A module-level singleton, `case_type_registry`, is the canonical instance; callers
    use it directly. Construct a fresh `CaseTypeRegistry()` only for isolation (tests).
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[FolderBackedCase]] = {}

    # ---- registration ----

    def register_case_types(self, *case_classes: type[FolderBackedCase]) -> None:
        """Explicit, opt-in registration of one or more case types (no auto-register).
        Each class is keyed by its bare __name__ — the EXACT value stamped into every
        record's case_object_type (see create_case_in_folder / case_reclassify_to) and enforced by
        _flush_record's guard — so the class name in code is guaranteed to match the name
        on disk. Single or many: register_case_types(A) or register_case_types(A, B, C)."""
        for case_cls in case_classes:
            self._registry[case_cls.__name__] = case_cls

    def register(self, case_cls: type[FolderBackedCase]) -> type[FolderBackedCase]:
        """Class-decorator sugar for one-line self-registration. Returns the class
        unchanged so it composes cleanly:

            @case_type_registry.register
            class TicketCase(FolderBackedCase):
                ...

        Equivalent to register_case_types(TicketCase) after the definition. Runs at
        class-definition time, when the class object already exists in full."""
        self.register_case_types(case_cls)
        return case_cls

    def resolve_case_type(
        self,
        type_name: str | None,
        *,
        registry: dict[str, type[FolderBackedCase]] | None = None,
    ) -> type[FolderBackedCase] | None:
        """Look up a class by its stored bare name. `registry` overrides the singleton
        (tests / isolation); None means use this instance's catalog."""
        if type_name is None:
            return None
        return (registry if registry is not None else self._registry).get(type_name)

    # ---- type resolution (shared by rehydrate + peek_class) ----

    @staticmethod
    def _sniff_case_type(folder: Path) -> str | None:
        """Cheaply read the record's case_object_type WITHOUT full Pydantic validation.
        case_object_type is the FIRST CaseRecord field and YAML is emitted in definition
        order (sort_keys=False), so it reliably appears at the top of the file."""
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
        """Open the folder as the CORRECT FolderBackedCase subclass (registry lookup on
        the sniffed type) and return a live, lease-holding case. The behavior-bearing
        analog of peek_class(return_class_object=True). RAISES UnregisteredCaseTypeError
        for an unknown type — you cannot build behavior without the class."""
        case_cls = self.peek_class(folder, return_class_object=True, registry=registry)
        return case_cls(folder)

    def peek_class(
        self,
        folder: Path,
        *,
        return_class_object: bool = False,
        registry: dict[str, type[FolderBackedCase]] | None = None,
    ) -> type[FolderBackedCase] | str:
        """Deduce a folder's case type from its record's case_object_type.

        return_class_object=False (default): return the bare type NAME (str) sniffed from
            disk — registry-free.
        return_class_object=True: resolve that name to the registered subclass and return
            the CLASS OBJECT; RAISES UnregisteredCaseTypeError when the name is not
            registered (you cannot obtain a class you never registered).

        RAISES FileNotFoundError when the folder holds no record, and ValueError when the
        record carries no case_object_type — either way the type cannot be deduced."""
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



# Process-wide singleton: the canonical catalog callers use directly. Type resolution
# (rehydrate / peek_class) works manager-free because this lives at module scope,
# not inside any CaseManager.
case_type_registry = CaseTypeRegistry()
