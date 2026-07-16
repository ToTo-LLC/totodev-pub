# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Pluggable case-id generation for FolderBackedCase.

Case IDs are opaque unique strings. FolderBackedCase needs uniqueness (and
ideally lexical monotonicity) but does not own the generation policy. Callers
may share one generator across many case types, run several namespaces in
parallel, or encode limited case-type info into the id.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from totodev_pub.folder_backed_case_support.helpers import _new_time_slug

if TYPE_CHECKING:
    from totodev_pub.folder_backed_case import FolderBackedCase


class CaseIDGenerator(ABC):
    """Abstract factory for minting case_id strings at case inception.

    ``generate`` is called by ``FolderBackedCase.create_case_in_folder`` when
    ``case_id`` is omitted (or is itself a ``CaseIDGenerator`` — see that
    method). ``case_cls`` — the concrete case class being created — is the
    sole context passed in: free, already-known information a generator may
    use to encode limited case-type info into the id (e.g. a three-letter
    prefix), or simply ignore.
    """

    @abstractmethod
    def generate(self, case_cls: type[FolderBackedCase] | None = None) -> str:
        """Return a new case_id string."""


class TimeSlugCaseIDGenerator(CaseIDGenerator):
    """Default generator: short, sortable, base-36 millisecond time slug.

    For in-process collision resistance, generation is monotonic per generator
    instance: if two calls land in the same millisecond, the latter is bumped
    to ``(previous + 1ms)`` before encoding. This does not guarantee uniqueness
    across multiple processes or machines.

    Ignores ``case_cls`` — uniqueness and lexical order only.
    """

    def __init__(self) -> None:
        self._last_ms: int = -1

    def generate(self, case_cls: type[FolderBackedCase] | None = None) -> str:
        now_ms = int(time.time() * 1000)
        mint_ms = now_ms if now_ms > self._last_ms else self._last_ms + 1
        self._last_ms = mint_ms
        return _new_time_slug(mint_ms)


DEFAULT_CASE_ID_GENERATOR = TimeSlugCaseIDGenerator()
