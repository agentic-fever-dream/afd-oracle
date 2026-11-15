"""Dense + sparse text embedders.

``FastEmbedEmbedder`` runs local ONNX models (no API calls, no LLM tokens). ``HashEmbedder`` is a deterministic,
dependency-free fallback used by the test suite and for offline smoke runs.
"""
from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass
from typing import Protocol, Sequence

from .config import Settings, get_settings


@dataclass
class Sparse:
    indices: list[int]
    values: list[float]


class Embedder(Protocol):
    dim: int

    def dense(self, texts: Sequence[str]) -> list[list[float]]: ...
    def sparse(self, texts: Sequence[str]) -> list[Sparse]: ...
    def dense_query(self, text: str) -> list[float]: ...
    def sparse_query(self, text: str) -> Sparse: ...


_TOKEN = re.compile(r"[A-Za-z][a-z0-9]*|[A-Z]+(?![a-z])|\d+")


def tokenize(text: str) -> list[str]:
    """Identifier-aware tokens: ``validateFormSchema`` / ``validate_form_schema`` -> validate, form, schema."""
    return [t.lower() for t in _TOKEN.findall(text)]


class HashEmbedder:
    def __init__(self, dim: int = 768):
        self.dim = dim

    def _dense_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize(text):
            h = zlib.crc32(tok.encode())
            vec[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def _sparse_one(self, text: str) -> Sparse:
        counts: dict[int, float] = {}
        for tok in tokenize(text):
            idx = zlib.crc32(tok.encode()) & 0x7FFFFFFF
            counts[idx] = counts.get(idx, 0.0) + 1.0
        items = sorted(counts.items())
        return Sparse([i for i, _ in items], [v for _, v in items])

    def dense(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._dense_one(t) for t in texts]

    def sparse(self, texts: Sequence[str]) -> list[Sparse]:
        return [self._sparse_one(t) for t in texts]

    def dense_query(self, text: str) -> list[float]:
        return self._dense_one(text)

    def sparse_query(self, text: str) -> Sparse:
        return self._sparse_one(text)


def length_buckets(texts: Sequence[str], max_batch: int, budget: float = 64 * 512 ** 2,
                   chars_per_token: float = 2.5) -> list[list[int]]:
    """Group text indices (shortest first) so each group's padded attention cost stays bounded.

    A transformer batch is padded to its longest member and attention memory grows with batch * seq_len**2, so one
    long code body in a batch of 64 can ask ONNX Runtime for many GB. Sorting by length and capping
    ``len(group) * est_tokens(longest)**2`` keeps peak memory near that of 64 x 512-token inputs.
    """
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    groups: list[list[int]] = []
    cur: list[int] = []
    for i in order:
        seq = max(1.0, len(texts[i]) / chars_per_token)
        if cur and (len(cur) + 1 > max_batch or (len(cur) + 1) * seq * seq > budget):
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


class FastEmbedEmbedder:
    def __init__(self, model: str, sparse_model: str, dim: int, batch: int = 64):
        import os

        # The Hugging Face "xet" transfer path stalls at 0 bytes on some networks; plain HTTP downloads work.
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from fastembed import SparseTextEmbedding, TextEmbedding  # heavy import, keep lazy

        self.dim = dim
        self.batch = batch
        self._dense = TextEmbedding(model_name=model)
        self._sparse = SparseTextEmbedding(model_name=sparse_model)

    def dense(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float] | None] = [None] * len(texts)
        for idx in length_buckets(texts, self.batch):
            for i, v in zip(idx, self._dense.embed([texts[i] for i in idx], batch_size=len(idx))):
                out[i] = v.tolist()
        return out  # type: ignore[return-value]

    def sparse(self, texts: Sequence[str]) -> list[Sparse]:
        return [Sparse(e.indices.tolist(), e.values.tolist()) for e in self._sparse.embed(list(texts), batch_size=self.batch)]

    def dense_query(self, text: str) -> list[float]:
        return next(iter(self._dense.query_embed(text))).tolist()

    def sparse_query(self, text: str) -> Sparse:
        e = next(iter(self._sparse.query_embed(text)))
        return Sparse(e.indices.tolist(), e.values.tolist())


_cached: Embedder | None = None


def get_embedder(settings: Settings | None = None) -> Embedder:
    global _cached
    settings = settings or get_settings()
    if _cached is not None and _cached.dim == settings.embed_dim:
        return _cached
    if settings.embedder == "hash":
        _cached = HashEmbedder(settings.embed_dim)
    else:
        _cached = FastEmbedEmbedder(settings.embed_model, settings.sparse_model, settings.embed_dim, settings.embed_batch)
    return _cached


def reset_embedder() -> None:
    global _cached
    _cached = None
