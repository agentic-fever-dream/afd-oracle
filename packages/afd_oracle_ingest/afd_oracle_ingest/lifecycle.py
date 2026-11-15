"""Symbol lifecycle events from consecutive versions of files (created / modified / deleted / moved)."""
from __future__ import annotations

from dataclasses import dataclass

from afd_oracle_core.ids import symbol_uid

from .ast.extractor import FileParse, SymbolRecord


@dataclass
class SymbolEvent:
    uid: str
    path: str
    qualname: str
    kind: str
    change: str  # created | modified | deleted | moved
    body_hash_before: str | None
    body_hash_after: str | None
    old_uid: str | None = None
    record: SymbolRecord | None = None  # the "after" record (None for deletions)


def _index(fp: FileParse | None) -> dict[tuple[str, str], SymbolRecord]:
    return {(s.qualname, s.kind): s for s in fp.symbols} if fp else {}


def file_events(repo: str, old_path: str | None, new_path: str | None, old: FileParse | None,
                new: FileParse | None) -> list[SymbolEvent]:
    """Events for one file change. ``old_path`` differs from ``new_path`` on renames."""
    a, b = _index(old), _index(new)
    events: list[SymbolEvent] = []
    renamed = old_path is not None and new_path is not None and old_path != new_path
    for key, rec in b.items():
        uid = symbol_uid(repo, new_path, rec.qualname, rec.kind)
        prev = a.get(key)
        if prev is None:
            events.append(SymbolEvent(uid, new_path, rec.qualname, rec.kind, "created", None, rec.body_hash, record=rec))
        elif renamed:
            events.append(SymbolEvent(uid, new_path, rec.qualname, rec.kind, "moved", prev.body_hash, rec.body_hash,
                                      old_uid=symbol_uid(repo, old_path, prev.qualname, prev.kind), record=rec))
        elif prev.body_hash != rec.body_hash:
            events.append(SymbolEvent(uid, new_path, rec.qualname, rec.kind, "modified", prev.body_hash, rec.body_hash,
                                      record=rec))
    for key, rec in a.items():
        if key not in b:
            path = old_path or new_path
            events.append(SymbolEvent(symbol_uid(repo, path, rec.qualname, rec.kind), path, rec.qualname, rec.kind,
                                      "deleted", rec.body_hash, None))
    return events


MOVE_WINDOW = 3  # main-line commits between a deletion and the matching re-creation


def link_cross_commit_moves(store, window: int = MOVE_WINDOW) -> dict:
    """Link a main-line deletion to a re-creation of the same (qualname, kind) a few commits later in another file.

    Restructurings are often split over commits, e.g. flask deleting ``flask.py`` in one commit and adding the
    ``flask/`` package in the next. Per-commit move detection sees a deletion and an unrelated creation, so symbols
    looked short-lived and their history appeared to start at the restructure. A match must be unique on both
    sides within the window. The creation becomes ``moved`` (with ``old_uid``), and the deletion becomes
    ``moved_away``, so graveyard metrics stop treating it as removed. Idempotent: already-linked events no longer match.
    """
    main = [r["hash"] for r in store.q("SELECT hash FROM commits WHERE on_main=1 ORDER BY seq")]
    pos = {h: i for i, h in enumerate(main)}
    rows = store.q("SELECT hash, uid, path, qualname, kind, change, seq FROM symbol_events "
                   "WHERE change IN ('created', 'deleted') AND hash IN (SELECT hash FROM commits WHERE on_main=1)")
    dels: dict[tuple[str, str], list] = {}
    creates: dict[tuple[str, str], list] = {}
    for r in rows:
        (dels if r["change"] == "deleted" else creates).setdefault((r["qualname"], r["kind"]), []).append(r)
    links = []
    for key, ds in dels.items():
        cs = creates.get(key)
        if not cs:
            continue
        for d in ds:
            near = [c for c in cs if c["path"] != d["path"] and 0 < pos[c["hash"]] - pos[d["hash"]] <= window]
            if len(near) != 1:
                continue
            c = near[0]
            rivals = [x for x in ds if x["path"] != c["path"] and 0 < pos[c["hash"]] - pos[x["hash"]] <= window]
            if len(rivals) == 1:
                links.append((d, c))
    with store.tx() as conn:
        for d, c in links:
            conn.execute("UPDATE symbol_events SET change='moved', old_uid=? WHERE hash=? AND uid=?",
                         (d["uid"], c["hash"], c["uid"]))
            conn.execute("UPDATE symbol_events SET change='moved_away' WHERE hash=? AND uid=?", (d["hash"], d["uid"]))
            conn.execute("UPDATE symbols SET old_uid=COALESCE(old_uid, ?) WHERE uid=?", (d["uid"], c["uid"]))
    return {"cross_commit_moves": len(links)}


def detect_moves(events: list[SymbolEvent]) -> list[SymbolEvent]:
    """Within one commit: a deletion + creation with identical body hash, kind and name in different files -> moved."""
    deleted = {}
    for e in events:
        if e.change == "deleted":
            deleted.setdefault((e.body_hash_before, e.kind, e.qualname.rsplit(".", 1)[-1]), []).append(e)
    consumed: set[int] = set()
    out: list[SymbolEvent] = []
    for e in events:
        if e.change == "created":
            cands = deleted.get((e.body_hash_after, e.kind, e.qualname.rsplit(".", 1)[-1]), [])
            match = next((d for d in cands if id(d) not in consumed and d.path != e.path), None)
            if match is not None:
                consumed.add(id(match))
                out.append(SymbolEvent(e.uid, e.path, e.qualname, e.kind, "moved", match.body_hash_before,
                                       e.body_hash_after, old_uid=match.uid, record=e.record))
                continue
        out.append(e)
    return [e for e in out if id(e) not in consumed]
