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
import uuid
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


class UUIDCaseIDGenerator(CaseIDGenerator):
    """UUID4-based generator: collision-resistant across processes and machines.

    A good choice when cases are minted from multiple processes (or hosts)
    into a shared tree and id collisions are a concern — random UUIDs need no
    coordination, unlike ``TimeSlugCaseIDGenerator``'s per-instance monotonic
    clock. The trade-offs: ids take a bit more effort to generate, are longer
    (32 hex characters), and are NOT lexically sortable by creation time.
    Stateless, so one instance is safe to share across threads and case types.

    Ignores ``case_cls`` — uniqueness only.

    Future: a UUIDv7 variant would add time-ordered (lexically monotonic) ids
    on top of the same collision resistance; ``uuid.uuid7`` lands in the
    stdlib in Python 3.14, so upgrade this (or add a sibling) once the
    project's minimum Python allows it.
    """

    def generate(self, case_cls: type[FolderBackedCase] | None = None) -> str:
        return uuid.uuid4().hex


DEFAULT_CASE_ID_GENERATOR = TimeSlugCaseIDGenerator()
