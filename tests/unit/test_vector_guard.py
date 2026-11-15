"""upsert_texts must never store a NaN/zero dense vector (Qdrant silently turns those into all-zero cosine vectors)."""
import math

import pytest

from afd_oracle_core.embedding import HashEmbedder
from afd_oracle_core.vectors import _usable, upsert_texts


class FlakyEmbedder(HashEmbedder):
    """Returns NaN for the whole batch, but a good vector when asked for one text."""

    def dense(self, texts):
        if len(texts) > 1:
            return [[math.nan] * self.dim for _ in texts]
        return super().dense(texts)


class BrokenEmbedder(HashEmbedder):
    def dense(self, texts):
        return [[0.0] * self.dim for _ in texts]


class FakeClient:
    def __init__(self):
        self.points = []

    def upsert(self, collection, points, wait=True):
        self.points += points


def test_usable():
    assert _usable([0.1, 0.2])
    assert not _usable([0.0, 0.0])
    assert not _usable([math.nan, 1.0])
    assert not _usable([])


def test_bad_batch_is_retried_one_by_one():
    client = FakeClient()
    items = [(f"sym:r:a.py:f{i}:function", f"def f{i}(): return {i}", {"repo": "r"}) for i in range(3)]
    assert upsert_texts(client, FlakyEmbedder(16), "code_symbols", items, batch=3) == 3
    assert all(_usable(p.vector["dense"]) for p in client.points)


def test_unusable_vector_is_refused():
    with pytest.raises(ValueError, match="unusable vector"):
        upsert_texts(FakeClient(), BrokenEmbedder(16), "code_symbols", [("sym:r:a.py:f:function", "x", {})])
