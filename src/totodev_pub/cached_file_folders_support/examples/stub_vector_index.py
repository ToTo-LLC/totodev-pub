#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
STUB vector index for the Zoho WorkDrive demo -- DO NOT use in production.

==============================  IMPORTANT  ==============================
This is a deliberately fake, dependency-free "vector index". The embeddings it
produces are FICTITIOUS: a deterministic hash of the text into a fixed-dimension
vector. There is no semantic meaning whatsoever. It exists only to demonstrate the
*shape* of a sensible pipeline -- "the cheap summary is what gets indexed" -- without
pulling in a real embedding model or vector database.

Replace `StubEmbedder` with a real embedder (e.g. sentence-transformers, an LLM
embedding endpoint, OCR-then-embed) and `StubVectorIndex` with a real local vector DB
(e.g. chromadb, sqlite-vec, faiss) when you want actual semantic search. The public
surface (`upsert` / `remove` / `query`) is intentionally tiny so the swap is easy.
========================================================================

Storage is a single JSON file. Not concurrency-safe on its own; callers that mutate it
from concurrent tasks must serialize writes (the demo's SummaryIndexer holds an
asyncio lock).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DIM = 64


class StubEmbedder:
    """Deterministic, meaningless pseudo-embeddings (a documented stub).

    Hashes each whitespace token into a bucket and accumulates a signed unit, then
    L2-normalizes. Same text -> same vector; that determinism is all the tests rely on.
    """

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in (text or "").lower().split():
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            idx = digest[0] % self.dim
            sign = 1.0 if (digest[1] % 2 == 0) else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class StubVectorIndex:
    """A tiny JSON-backed "vector index" over summary text (a documented stub)."""

    def __init__(self, path: Path | str, embedder: StubEmbedder | None = None) -> None:
        self.path = Path(path)
        self.embedder = embedder or StubEmbedder()
        self._entries: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError:
            # Missing file (or unreadable) is the normal first-run case -- start empty.
            return {}
        except ValueError as exc:
            # Corrupt JSON is NOT expected: surface it (at debug) so accidental data loss
            # is at least traceable, then start empty rather than crash.
            logger.debug("Ignoring corrupt vector index at %s: %s", self.path, exc)
            return {}
        if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
            return raw["entries"]
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stub_warning": "Fictitious embeddings - replace with a real vector DB.",
            "dim": self.embedder.dim,
            "entries": self._entries,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def upsert(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Index (or re-index) a document by id. The document IS the summary text."""
        self._entries[doc_id] = {
            "vector": self.embedder.embed(text),
            "text_preview": (text or "")[:240],
            "metadata": metadata or {},
        }
        self._save()

    def remove(self, doc_id: str) -> bool:
        """Drop a document by id. Returns True if something was removed."""
        existed = self._entries.pop(doc_id, None) is not None
        if existed:
            self._save()
        return existed

    def query(self, text: str, k: int = 5) -> list[tuple[str, float]]:
        """Return up to k (doc_id, cosine_score) pairs, best first (meaningless scores)."""
        q = self.embedder.embed(text)
        scored = [
            (doc_id, _cosine(q, entry.get("vector", [])))
            for doc_id, entry in self._entries.items()
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, doc_id: str) -> bool:
        return doc_id in self._entries
