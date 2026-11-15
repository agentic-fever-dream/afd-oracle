"""Topic map: a collection's vectors grouped into labelled topic islands for the Explore view.

1. Spherical k-means on the full-dimensional vectors finds topics, so grouping isn't an artefact of projecting to 2D.
2. Topic centres are placed by a PCA of the centroids, so related topics sit near each other.
3. Each topic's members are spread inside a circle by a local PCA, sized by member count; overlapping circles are
   pushed apart.
4. Each topic is labelled by its most distinctive terms (TF-IDF over symbol names or commit messages) plus the folder
   most of its members live in.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from typing import Any, Callable

import numpy as np
from qdrant_client import QdrantClient, models

from afd_oracle_core.embedding import tokenize

MAX_POINTS = 4000
STOP = set("""
a an and are as at be by for from in into is it its of on or that the this to was were will with not no use used using
add added adds fix fixed fixes fixing update updated updates remove removed make made move moved change changed changes
support new old more less also when than then so all any can may should via per test tests py js ts self cls init get
set none true false return def class function commit merge pull request branch master main version bump release docs
doc readme changelog rst md txt file files src lib typo typos patch com github http https www org now just
minor small some like into after before still only first
""".split())


def tokens(text: str, extra: frozenset = frozenset()) -> list[str]:
    return [t for t in tokenize(text) if len(t) > 2 and t not in STOP and t not in extra and not t.isdigit()]


def spherical_kmeans(X: np.ndarray, k: int, iters: int = 25) -> tuple[np.ndarray, np.ndarray]:
    """Cosine k-means over unit rows, with deterministic k-means++ seeding."""
    rng = np.random.default_rng(0)
    n = X.shape[0]
    centers = [X[rng.integers(n)]]
    for _ in range(1, k):
        d = np.clip(1 - np.max(X @ np.stack(centers).T, axis=1), 0, None) ** 2
        centers.append(X[rng.choice(n, p=d / d.sum())] if d.sum() > 0 else X[rng.integers(n)])
    C = np.stack(centers).astype(np.float32)
    labels = np.full(n, -1)
    for _ in range(iters):
        new = np.argmax(X @ C.T, axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            m = X[labels == j]
            if len(m):
                c = m.mean(axis=0)
                C[j] = c / (np.linalg.norm(c) + 1e-9)
    return labels, C


def common_dir(paths: list[str]) -> str:
    """The deepest folder holding at least 45% of the paths ('' when they're scattered)."""
    best = ""
    paths = [p for p in paths if p]
    for depth in (1, 2, 3):
        cnt = Counter("/".join(p.split("/")[:depth]) for p in paths if p.count("/") >= depth)
        if not cnt:
            break
        top, n = cnt.most_common(1)[0]
        if n / len(paths) < 0.45:
            break
        best = top
    return best


def _doc(coll: str, pl: dict) -> tuple[str, str]:
    text = str(pl.get("text", ""))
    if coll == "commit_diffs":
        msg = [ln for ln in text.splitlines()[1:4] if not ln.startswith(("Merge pull request", "Merge branch", "Files:"))]
        return " ".join(msg), (pl.get("paths") or [""])[0]
    if coll == "code_symbols":
        return f"{pl.get('qualname', '')} {pl.get('path', '')}", pl.get("path") or ""
    return f"{pl.get('title', '')} {pl.get('kind', '')}", (pl.get("paths") or [""])[0]


def build(client: QdrantClient, repo: str, coll: str, describe: Callable[[dict], dict]) -> dict[str, Any]:
    """``describe(payload)`` returns the display fields (uid, title, subtitle) for one point."""
    flt = models.Filter(must=[models.FieldCondition(key="repo", match=models.MatchValue(value=repo))])
    vecs, meta, docs, paths, offset, skipped = [], [], [], [], None, 0
    own = frozenset(tokenize(repo))  # the repo's own name is in everything
    while len(vecs) < MAX_POINTS:
        pts, offset = client.scroll(coll, scroll_filter=flt, limit=512, offset=offset, with_payload=True,
                                    with_vectors=["dense"])
        for p in pts:
            v = p.vector.get("dense") if isinstance(p.vector, dict) else p.vector
            if v is None or not np.any(v):
                skipped += 1  # unusable vector (e.g. not re-embedded yet)
                continue
            pl = p.payload or {}
            doc, path = _doc(coll, pl)
            vecs.append(v)
            docs.append(set(tokens(doc, own)))
            paths.append(path)
            d = describe(pl)
            meta.append({"uid": d["uid"], "title": d["title"], "subtitle": d["subtitle"],
                         "kind": pl.get("kind") or pl.get("label") or "", "alive": pl.get("alive")})
        if offset is None:
            break
    base = {"total": len(vecs), "sampled": offset is not None, "skipped": skipped, "built": time.time()}
    if len(vecs) < 3:
        return {**base, "points": [], "clusters": [],
                "note": "No usable vectors yet. If a re-embed is running, try again when it finishes."}

    X = np.asarray(vecs, dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    n = len(X)
    k = int(max(2, min(24, round(math.sqrt(n / 6)), n // 3)))
    labels, C = spherical_kmeans(X, k)

    cm = C.mean(axis=0)
    _, _, vt = np.linalg.svd(C - cm, full_matrices=False)
    centers = (C - cm) @ vt[:2].T
    centers /= np.abs(centers).max() + 1e-9
    sizes = np.bincount(labels, minlength=k)
    radius = 0.05 + 0.5 * np.sqrt(sizes / n)
    for _ in range(400):  # push overlapping islands apart
        moved = False
        for i in range(k):
            for j in range(i + 1, k):
                d = centers[j] - centers[i]
                dist = float(np.linalg.norm(d)) or 1e-6
                need = radius[i] + radius[j] + 0.08
                if dist < need:
                    push = (need - dist) / 2 * d / dist
                    centers[i] -= push
                    centers[j] += push
                    moved = True
        if not moved:
            break

    df = Counter(t for d in docs for t in d)
    clusters, local = [], {}
    xy = np.zeros((n, 2), dtype=np.float32)
    for j in range(k):
        idx = np.where(labels == j)[0]
        if not len(idx):
            continue
        Xi = X[idx]
        mean = Xi.mean(axis=0)
        comps = np.linalg.svd(Xi - mean, full_matrices=False)[2][:2] if len(idx) >= 3 else np.zeros((2, X.shape[1]))
        loc = (Xi - mean) @ comps.T
        # spread by rank-preserving radius so islands look evenly filled instead of a dense core + outliers
        r = np.linalg.norm(loc, axis=1)
        order = np.argsort(np.argsort(r))
        scale_r = np.sqrt((order + 1) / len(idx)) / (r + 1e-9)
        loc = loc * scale_r[:, None]
        xy[idx] = centers[j] + loc * radius[j] * 0.9
        local[j] = (mean, comps, float(np.median(r) or 1.0))
        tf = Counter(t for i in idx for t in docs[i])
        floor = max(2, len(idx) * 0.06)
        terms = [t for t, c in sorted(tf.items(), key=lambda kv: -(kv[1] / len(idx)) * math.log(n / df[kv[0]]))
                 if c >= floor][:3]
        folder = common_dir([paths[i] for i in idx])
        clusters.append({"id": j, "count": int(len(idx)), "terms": terms, "folder": folder,
                         "label": " · ".join(terms) or (folder.rsplit("/", 1)[-1] if folder else f"topic {j + 1}"),
                         "x": float(centers[j][0]), "y": float(centers[j][1]), "r": float(radius[j])})
    span = float(max(np.abs(xy).max(), max(abs(c["x"]) + c["r"] for c in clusters),
                     max(abs(c["y"]) + c["r"] for c in clusters))) or 1.0
    xy /= span
    for c in clusters:
        c["x"], c["y"], c["r"] = round(c["x"] / span, 4), round(c["y"] / span, 4), round(c["r"] / span, 4)
    for m, (x, y), lab in zip(meta, xy.tolist(), labels.tolist()):
        m["x"], m["y"], m["c"] = round(x, 4), round(y, 4), int(lab)
    clusters.sort(key=lambda c: -c["count"])
    return {**base, "points": meta, "clusters": clusters, "C": C, "centers": centers, "radius": radius,
            "local": local, "span": span}


def place_query(m: dict, v: np.ndarray) -> dict:
    """Where a query vector lands: inside its nearest topic island."""
    v = v / (np.linalg.norm(v) + 1e-9)
    j = int(np.argmax(m["C"] @ v))
    mean, comps, med = m["local"][j]
    loc = (v - mean) @ comps.T
    loc = loc / (np.linalg.norm(loc) + 1e-9) * min(1.0, float(np.linalg.norm(loc)) / (2 * med))
    x, y = ((m["centers"][j] + loc * m["radius"][j] * 0.9) / m["span"]).tolist()
    return {"x": round(x, 4), "y": round(y, 4), "cluster": j}
