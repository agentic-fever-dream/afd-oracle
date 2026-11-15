"""Catalogue family: data and schema (migrations, constraints, persisted names, money and key formats).

Most builders read one shared, cached view: every added line of every migration file parsed into *DDL events*
(``create_table``, ``add``/``alter``/``drop`` column, ``create_index``/``drop_index`` ...). The parser understands raw
SQL, Alembic ``op.*``, Django ``migrations.*`` and Rails ``add_column`` style migrations. It works on ``diff_lines``
(added lines only, bulk and merge commits are not indexed) so it is an approximation: statements are joined by
parenthesis balance and multi-line SQL by an ``ALTER TABLE`` carry-over; exotic forms are lost.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .base import (DAY, MIGRATION_RX, Ctx, Hit, candidate, is_code, is_docs, is_generated, is_test, is_vendored,
                   subject_of)

# ---------------------------------------------------------------------------- shared helpers
_SCHEMA_DUMP_RX = re.compile(r"(?i)(^|/)(schema|structure|dump|seed|seeds|fixtures?|data|test[_-]?data|queries)[^/]*\.sql$")
_BASELINE_RX = re.compile(r"(?i)baseline|initial|squash|(^|/)0*1[_-]|(^|/)v?1__|(^|/)0001")
MONEY = (r"(price|amount|total|balance|cost|fee|tax|money|currency|salary|wage|charge|payment|invoice|discount|"
         r"revenue|cents?|subtotal|refund|payout|premium|budget)")
_DJANGO_HEAD = re.compile(r"^\s*(?:migrations|operations)\.(AddField|AlterField|RemoveField|RenameField|CreateModel|"
                          r"DeleteModel|AddIndex|RemoveIndex|AddConstraint|RemoveConstraint|RenameModel|"
                          r"AlterUniqueTogether|RunSQL|RunPython)\(")
_ALEMBIC_HEAD = re.compile(r"^\s*(?:batch_)?op\.(add_column|alter_column|drop_column|create_table|drop_table|"
                           r"create_index|drop_index|create_foreign_key|create_unique_constraint|drop_constraint|"
                           r"rename_table|execute)\(")
_QUOTE = re.compile(r"""["'`\[\]:]""")
_COMMENT = re.compile(r"^\s*(#|--|//|/\*|\*)")


def _n(s: str | None) -> str:
    return _QUOTE.sub("", s or "").strip().lower()


def _cols(s: str) -> tuple[str, ...]:
    out = []
    for piece in (s or "").split(","):
        p = _QUOTE.sub("", piece).strip().lower()
        p = re.sub(r"\s+asc$", "", p)
        p = re.sub(r"\s+(text_pattern_ops|varchar_pattern_ops)", "", p)
        if p:
            out.append(p)
    return tuple(out)


def _scratch(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_ds_cache", {})


def _is_migration_file(path: str) -> bool:
    return bool(MIGRATION_RX.search(path)) and not is_test(path) and not is_vendored(path) and not is_docs(path) \
        and not _SCHEMA_DUMP_RX.search(path) and not is_generated(path)


def _ev(op: str, table: str, col: str | None, text: str, **kw) -> dict:
    return {"op": op, "table": _n(table), "col": _n(col) if col else None, "text": text[:260], **kw}


_SQL_ALTER = re.compile(r"(?i)\bALTER\s+TABLE\s+(?:ONLY\s+|IF\s+EXISTS\s+)*[`\"\[]?(?:\w+\.)?(\w+)[`\"\]]?\s*(.*)$")
_SQL_CREATE_T = re.compile(r"(?i)\bCREATE\s+(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                           r"[`\"\[]?(?:\w+\.)?(\w+)")
_SQL_DROP_T = re.compile(r"(?i)\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`\"\[]?(?:\w+\.)?(\w+)")
_SQL_CIDX = re.compile(r"(?i)\bCREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?"
                       r"\s+ON\s+(?:ONLY\s+)?[`\"]?(?:\w+\.)?(\w+)[`\"]?\s*(?:USING\s+\w+\s*)?\(([^)]*)\)(.*)")
_SQL_DIDX = re.compile(r"(?i)\bDROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?[`\"]?(?:\w+\.)?(\w+)")
_CL_UNIQ = re.compile(r"(?i)\bADD\s+(?:CONSTRAINT\s+[`\"]?(\w+)[`\"]?\s+)?UNIQUE\s*(?:KEY\s+\w+\s*)?\(([^)]*)\)(.*)")
_CL_CONS = re.compile(r"(?i)\bADD\s+CONSTRAINT\s+[`\"]?(\w+)[`\"]?\s+(FOREIGN\s+KEY|CHECK|PRIMARY\s+KEY)\b")
_CL_FK = re.compile(r"(?i)\bADD\s+FOREIGN\s+KEY\b")
_CL_ADD = re.compile(r"(?i)\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?\s+(.*)")
_CL_ALT = re.compile(r"(?i)\bALTER\s+(?:COLUMN\s+)?[`\"]?(\w+)[`\"]?\s+((?:SET\s+DATA\s+)?(?:TYPE|SET|DROP)\b.*)")
_CL_MOD = re.compile(r"(?i)\b(?:MODIFY|CHANGE)\s+(?:COLUMN\s+)?[`\"]?(\w+)[`\"]?\s+(.*)")
_CL_DROPC = re.compile(r"(?i)\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?[`\"]?(\w+)")
_CL_DROPK = re.compile(r"(?i)\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?[`\"]?(\w+)")
_CL_REN = re.compile(r"(?i)\bRENAME\s+(?:COLUMN\s+)?[`\"]?(\w+)[`\"]?\s+TO\s+[`\"]?(\w+)")
_ADD_SKIP = {"constraint", "unique", "primary", "foreign", "check", "index", "key", "exclude", "fulltext"}
_WHERE = re.compile(r"(?i)\bwhere\b")


def _pred(tail: str) -> str:
    parts = _WHERE.split(tail or "", 1)
    return parts[1].strip(" ;").lower() if len(parts) == 2 else ""


def _sql_events(line: str, cur: list) -> list[dict]:
    """``cur`` is a one-element carry list holding the table of an unfinished ALTER TABLE."""
    out: list[dict] = []
    s = line.strip()
    if not s or s.startswith(("--", "#", "//")):
        return out
    rest = None
    m = _SQL_ALTER.search(s)
    if m:
        cur[0] = m.group(1)
        rest = m.group(2)
    elif cur[0] and re.match(r"(?i)(ADD|ALTER|DROP|RENAME|MODIFY|CHANGE)\b", s):
        rest = s
    if rest:
        t = cur[0]
        mu = _CL_UNIQ.search(rest)
        mc = _CL_CONS.search(rest)
        if mu:
            out.append(_ev("create_index", t, None, rest, idx=_n(mu.group(1)), cols=_cols(mu.group(2)), unique=True,
                           pred=_pred(mu.group(3)), concurrently=False))
        elif mc:
            out.append(_ev("add_constraint", t, None, rest, ctype=re.sub(r"\s+", " ", mc.group(2)).lower()))
        elif _CL_FK.search(rest):
            out.append(_ev("add_constraint", t, None, rest, ctype="foreign key"))
        else:
            ma = _CL_ADD.search(rest)
            if ma and ma.group(1).lower() not in _ADD_SKIP:
                out.append(_ev("add", t, ma.group(1), rest, ftype=ma.group(2)[:60]))
        for ma in _CL_ALT.finditer(rest):
            out.append(_ev("alter", t, ma.group(1), rest, ftype=ma.group(2)[:60]))
        ma = _CL_MOD.search(rest)
        if ma:
            out.append(_ev("alter", t, ma.group(1), rest, ftype=ma.group(2)[:60]))
        for ma in _CL_DROPC.finditer(rest):
            out.append(_ev("drop", t, ma.group(1), rest))
        for ma in _CL_DROPK.finditer(rest):
            out.append(_ev("drop_constraint", t, ma.group(1), rest))
        ma = _CL_REN.search(rest)
        if ma:
            out.append(_ev("rename", t, ma.group(1), rest, new=_n(ma.group(2))))
    if s.rstrip().endswith(";"):
        cur[0] = None
    m = _SQL_CREATE_T.search(s)
    if m:
        out.append(_ev("create_table", m.group(1), None, s))
    m = _SQL_DROP_T.search(s)
    if m:
        out.append(_ev("drop_table", m.group(1), None, s))
    m = _SQL_CIDX.search(s)
    if m:
        out.append(_ev("create_index", m.group(4), None, s, idx=_n(m.group(3)), cols=_cols(m.group(5)),
                       unique=bool(m.group(1)), pred=_pred(m.group(6)), concurrently=bool(m.group(2))))
    else:
        m = _SQL_DIDX.search(s)
        if m:
            out.append(_ev("drop_index", "", None, s, idx=_n(m.group(1))))
    return out


def _kw(stmt: str, key: str) -> str | None:
    m = re.search(rf"\b{key}\s*=\s*['\"](\w+)['\"]", stmt)
    return m.group(1) if m else None


def _first(rx: str, s: str, default: str = "") -> str:
    m = re.search(rx, s)
    return m.group(1) if m else default


def _stmt_events(stmt: str, batch_table: str | None) -> list[dict]:
    out: list[dict] = []
    s = re.sub(r"\s+", " ", stmt).strip()
    q = r"['\"](\w+)['\"]"
    m = _ALEMBIC_HEAD.match(s)
    if m:
        op = m.group(1)
        if op == "add_column":
            mm = re.search(rf"add_column\(\s*{q}\s*,\s*(?:sa\.|sqlalchemy\.)?Column\(\s*{q}", s)
            if mm:
                out.append(_ev("add", mm.group(1), mm.group(2), s, ftype=s[mm.end():mm.end() + 80]))
            else:
                mm = re.search(rf"add_column\(\s*(?:sa\.|sqlalchemy\.)?Column\(\s*{q}", s)
                if mm and batch_table:
                    out.append(_ev("add", batch_table, mm.group(1), s, ftype=s[mm.end():mm.end() + 80]))
        elif op == "alter_column":
            ft = _first(r"type_\s*=\s*([\w.()]+)", s)
            mm = re.search(rf"alter_column\(\s*{q}\s*,\s*{q}", s)
            if mm:
                out.append(_ev("alter", mm.group(1), mm.group(2), s, ftype=ft))
            else:
                mm = re.search(rf"alter_column\(\s*{q}", s)
                if mm and batch_table:
                    out.append(_ev("alter", batch_table, mm.group(1), s, ftype=ft))
        elif op == "drop_column":
            mm = re.search(rf"drop_column\(\s*{q}\s*,\s*{q}", s)
            if mm:
                out.append(_ev("drop", mm.group(1), mm.group(2), s))
            else:
                mm = re.search(rf"drop_column\(\s*{q}", s)
                if mm and batch_table:
                    out.append(_ev("drop", batch_table, mm.group(1), s))
        elif op == "create_table":
            mm = re.search(rf"create_table\(\s*{q}", s)
            if mm:
                out.append(_ev("create_table", mm.group(1), None, s))
        elif op == "drop_table":
            mm = re.search(rf"drop_table\(\s*{q}", s)
            if mm:
                out.append(_ev("drop_table", mm.group(1), None, s))
        elif op == "create_index":
            mm = re.search(rf"create_index\(\s*(?:op\.f\()?{q}\)?\s*,\s*{q}\s*,\s*\[([^\]]*)\]", s)
            if mm:
                out.append(_ev("create_index", mm.group(2), None, s, idx=_n(mm.group(1)), cols=_cols(mm.group(3)),
                               unique=bool(re.search(r"unique\s*=\s*True", s)),
                               pred=_first(r"postgresql_where\s*=\s*(?:sa\.text\()?['\"]([^'\"]*)", s).lower(),
                               concurrently=bool(re.search(r"concurrently\s*=\s*True", s))))
        elif op == "drop_index":
            mm = re.search(rf"drop_index\(\s*(?:op\.f\()?{q}", s)
            if mm:
                out.append(_ev("drop_index", _kw(s, "table_name") or "", None, s, idx=_n(mm.group(1))))
        elif op == "create_unique_constraint":
            mm = re.search(rf"create_unique_constraint\(\s*(?:op\.f\()?['\"]?(\w+)['\"]?\)?\s*,\s*{q}\s*,\s*\[([^\]]*)\]", s)
            if mm:
                out.append(_ev("create_index", mm.group(2), None, s, idx=_n(mm.group(1)), cols=_cols(mm.group(3)),
                               unique=True, pred="", concurrently=False))
        elif op == "drop_constraint":
            mm = re.search(rf"drop_constraint\(\s*(?:op\.f\()?['\"]?(\w+)['\"]?\)?\s*,\s*{q}", s)
            if mm:
                if re.search(r"type_\s*=\s*['\"]unique", s):
                    out.append(_ev("drop_index", mm.group(2), None, s, idx=_n(mm.group(1))))
                else:
                    out.append(_ev("drop_constraint", mm.group(2), mm.group(1), s))
        elif op == "create_foreign_key":
            mm = re.search(rf"create_foreign_key\(\s*['\"]?\w*['\"]?\s*,\s*{q}", s)
            if mm:
                out.append(_ev("add_constraint", mm.group(1), None, s, ctype="foreign key"))
        elif op == "execute":
            body = re.sub(r"^\s*(?:op\.)?execute\(\s*(?:sa\.text\()?[rf]?['\"]{1,3}", "", s)
            out.extend(_sql_events(body, [None]))
        return out
    m = _DJANGO_HEAD.match(s)
    if m:
        op = m.group(1)
        model, name = _kw(s, "model_name"), _kw(s, "name")
        if op in ("AddField", "AlterField", "RemoveField") and model and name:
            kind = {"AddField": "add", "AlterField": "alter", "RemoveField": "drop"}[op]
            out.append(_ev(kind, model, name, s, ftype=_first(r"field\s*=\s*([\w.]+\([^)]*\)?)", s)[:80]))
        elif op == "RenameField" and model:
            out.append(_ev("rename", model, _kw(s, "old_name"), s, new=_n(_kw(s, "new_name"))))
        elif op == "CreateModel" and name:
            out.append(_ev("create_table", name, None, s))
        elif op == "DeleteModel" and name:
            out.append(_ev("drop_table", name, None, s))
        elif op == "AddIndex" and model:
            fields = _first(r"fields\s*=\s*\[([^\]]*)\]", s)
            cond = _first(r"condition\s*=\s*(.*?)(?:,\s*name|\)\)|$)", s)
            out.append(_ev("create_index", model, None, s, idx=_n(name), cols=_cols(fields), unique=False,
                           pred=cond.lower()[:80], concurrently=False))
        elif op == "RemoveIndex" and model:
            out.append(_ev("drop_index", model, None, s, idx=_n(name)))
        elif op == "AddConstraint" and model:
            fields = _first(r"UniqueConstraint\(.*?fields\s*=\s*\[([^\]]*)\]", s)
            if fields:
                cond = _first(r"condition\s*=\s*(.*?)(?:,\s*name|\)\)|$)", s)
                out.append(_ev("create_index", model, None, s, idx=_n(name), cols=_cols(fields), unique=True,
                               pred=cond.lower()[:80], concurrently=False))
            else:
                out.append(_ev("add_constraint", model, None, s, ctype="check"))
        elif op == "RemoveConstraint" and model:
            out.append(_ev("drop_index", model, None, s, idx=_n(name)))
        elif op == "RunSQL":
            out.extend(_sql_events(re.sub(r"^\s*migrations\.RunSQL\(\s*[rf]?['\"]{1,3}", "", s), [None]))
        return out
    return out


_RAILS = re.compile(r"^\s*(add_column|change_column|remove_column|rename_column|create_table|drop_table|add_index|"
                    r"remove_index|add_foreign_key|change_column_null|add_reference)\s+:?['\"]?(\w+)['\"]?\s*,?\s*(.*)$")


def _rails_event(line: str) -> list[dict]:
    m = _RAILS.match(line)
    if not m:
        return []
    op, table, rest = m.groups()
    col = _first(r":?['\"]?(\w+)", rest) or None
    if op == "add_column":
        return [_ev("add", table, col, line, ftype=rest[:60])]
    if op in ("change_column", "change_column_null"):
        return [_ev("alter", table, col, line, ftype=rest[:60])]
    if op == "remove_column":
        return [_ev("drop", table, col, line)]
    if op == "rename_column":
        return [_ev("rename", table, col, line, new=_n(_first(r":?['\"]?\w+['\"]?\s*,\s*:?['\"]?(\w+)", rest)))]
    if op == "create_table":
        return [_ev("create_table", table, None, line)]
    if op == "drop_table":
        return [_ev("drop_table", table, None, line)]
    if op == "add_index":
        cl = _first(r"\[([^\]]*)\]", rest)
        return [_ev("create_index", table, None, line, idx=_n(_first(r"name:\s*:?['\"]?(\w+)", rest)),
                    cols=_cols(cl) if cl else ((_n(col),) if col else ()),
                    unique="unique: true" in rest or "unique => true" in rest,
                    pred=_first(r"where:\s*['\"]([^'\"]*)", rest).lower(), concurrently="concurrently" in rest)]
    if op == "remove_index":
        return [_ev("drop_index", table, None, line, idx=_n(_first(r"name:\s*:?['\"]?(\w+)", rest)),
                    cols=_cols(_first(r"\[([^\]]*)\]", rest)))]
    if op == "add_foreign_key":
        return [_ev("add_constraint", table, None, line, ctype="foreign key")]
    return []


def _file_events(rows: list) -> list[dict]:
    out: list[dict] = []
    cur = [None]
    stmt: list[str] = []
    depth = 0
    batch_table = None

    def flush():
        nonlocal stmt, depth
        if stmt:
            out.extend(_stmt_events(" ".join(stmt), batch_table))
        stmt, depth = [], 0

    for r in rows:
        line = r["text"]
        mb = re.search(r"batch_alter_table\(\s*['\"](\w+)['\"]", line)
        if mb:
            batch_table = mb.group(1)
        starter = bool(_ALEMBIC_HEAD.match(line) or _DJANGO_HEAD.match(line))
        if starter and stmt:
            flush()
        if starter or stmt:
            stmt.append(line.strip())
            depth += line.count("(") - line.count(")")
            if depth <= 0 or len(stmt) >= 14:
                flush()
            continue
        out.extend(_rails_event(line))
        out.extend(_sql_events(line, cur))
    flush()
    return out


def _mig_events(ctx: Ctx) -> list[dict]:
    """All DDL events from added lines of migration files, oldest first (hash, seq, ts, path + op fields)."""
    sc = _scratch(ctx)
    if "events" in sc:
        return sc["events"]
    rows = ctx.q("SELECT hash, seq, ts, path, line, text FROM diff_lines WHERE sign='+' AND path REGEXP ? "
                 "ORDER BY seq, path, line", (MIGRATION_RX.pattern,))
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if _is_migration_file(r["path"]):
            groups[(r["seq"], r["hash"], r["ts"], r["path"])].append(r)
    events: list[dict] = []
    for (seq, h, ts, path), rs in groups.items():
        for e in _file_events(rs):
            e.update(hash=h, seq=seq, ts=ts, path=path)
            events.append(e)
    events.sort(key=lambda e: e["seq"])
    sc["events"] = events
    return events


def _mentions(ctx: Ctx, name: str) -> list[tuple[str, str, int]]:
    """Distinct (hash, path, seq) of changed non-migration, non-test lines mentioning ``name`` (case-insensitive)."""
    sc = _scratch(ctx).setdefault("mentions", {})
    key = name.lower()
    if key not in sc:
        rows = ctx.q("SELECT DISTINCT hash, path, seq FROM diff_lines WHERE instr(lower(text), ?) > 0", (key,))
        sc[key] = [(r["hash"], r["path"], r["seq"]) for r in rows
                   if not MIGRATION_RX.search(r["path"]) and not is_test(r["path"]) and not is_docs(r["path"])]
    return sc[key]


def _creates(ctx: Ctx) -> dict[str, dict]:
    sc = _scratch(ctx)
    if "creates" not in sc:
        out: dict[str, dict] = {}
        for e in _mig_events(ctx):
            if e["op"] == "create_table" and e["table"] not in out:
                out[e["table"]] = e
        sc["creates"] = out
    return sc["creates"]


def _sym_uid(ctx: Ctx, path: str, name: str) -> list[str]:
    return [s["uid"] for s in ctx.symbols_by_path.get(path, []) if s["name"] == name][:1]


def _later_fixes(ctx: Ctx, events: list[dict], after_seq: int, table: str, days: float = 60) -> list[dict]:
    first = next((e for e in events if e["seq"] == after_seq), None)
    ts0 = first["ts"] if first else 0
    out = []
    for e in events:
        if e["seq"] <= after_seq or e["table"] != table or e["ts"] > ts0 + days * DAY:
            continue
        c = ctx.by_hash.get(e["hash"])
        if c is not None and (ctx.is_fix(c) or c["is_revert"] or re.search(r"(?i)lock_timeout|concurrently|not valid", e["text"])):
            out.append(e)
    return out


# ---------------------------------------------------------------------------- 1. locking DDL
_SAFE_RX = re.compile(r"(?i)concurrently|not\s+valid|lock_timeout")


def _blocking_kind(e: dict) -> str | None:
    t, op = e["text"], e["op"]
    if op == "add" and re.search(r"(?i)\bNOT\s+NULL\b|nullable\s*=\s*False|null:\s*false", t):
        return "add_not_null_column"
    if op == "alter" and re.search(r"(?i)\bTYPE\b|SET\s+NOT\s+NULL|type_\s*=|AlterField|change_column|MODIFY|field=models", t):
        return "alter_column_type_or_not_null"
    if op == "create_index":
        return "create_index_blocking"
    if op == "add_constraint":
        return f"add_{e.get('ctype', 'constraint').replace(' ', '_')}_constraint"
    return None


@candidate("locking_ddl_on_hot_table")
def locking_ddl_on_hot_table(ctx: Ctx):
    """Blocking DDL (ADD COLUMN NOT NULL, ALTER TYPE/SET NOT NULL, non-concurrent CREATE INDEX, ADD FK/CHECK without
    NOT VALID) on a table created by an earlier migration. Heat proxy = distinct non-migration commits mentioning the
    table plus migrations touching it. Statements are judged line by line (``lock_timeout`` is honoured per file),
    so multi-line SQL may be misjudged. A hit needs a later lock fix/revert/lock_timeout on the table, or heat >= 10."""
    events = _mig_events(ctx)
    creates = _creates(ctx)
    touches: Counter = Counter(e["table"] for e in events if e["table"])
    lock_files = {(e["hash"], e["path"]) for e in events if re.search(r"(?i)lock_timeout", e["text"])}
    cands = []
    for e in events:
        kind = _blocking_kind(e)
        if not kind or not e["table"]:
            continue
        if "sqlite" in e["path"].lower() or "sqlite" in e["text"].lower():
            ctx.drop("sqlite")
            continue
        if _SAFE_RX.search(e["text"]) or e.get("concurrently") or (e["hash"], e["path"]) in lock_files:
            ctx.drop("safe_form")
            continue
        cr = creates.get(e["table"])
        if cr is None or cr["hash"] == e["hash"] or cr["seq"] > e["seq"]:
            ctx.drop("same_migration_create_or_unknown_table")
            continue
        cands.append((e, kind))
    heat_cache: dict[str, int] = {}
    seen = set()
    for e, kind in cands[:2000]:
        key = (e["hash"], e["table"], kind)
        if key in seen:
            continue
        seen.add(key)
        t = e["table"]
        if t not in heat_cache:
            heat_cache[t] = (len({h for h, _, _ in _mentions(ctx, t)}) if len(heat_cache) < 150 else 0) + touches[t]
        heat = heat_cache[t]
        fixes = _later_fixes(ctx, events, e["seq"], t)
        if not fixes and heat < 10:
            ctx.drop("cold_table_no_followup")
            continue
        yield Hit(
            subject=f"{e['hash']}|{t}|{kind}",
            score=heat * 0.3 + 6 * len(fixes) + (2 if "not_null" in kind else 1),
            title=f"{kind.replace('_', ' ')} on {t} in {e['path'].rsplit('/', 1)[-1]} (heat {heat}, {len(fixes)} later lock fixes)",
            evidence={"table": t, "form": kind, "statement": e["text"][:160], "heat": heat,
                      "table_created": creates[t]["hash"][:10],
                      "later_fixes": [{"hash": f["hash"][:10], "path": f["path"],
                                       "subject": subject_of(ctx.by_hash[f["hash"]]["message"])} for f in fixes[:4]]},
            commits=[e["hash"], *[f["hash"] for f in fixes[:3]]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])


# ---------------------------------------------------------------------------- 2. migration edited after commit
_MIG_EDIT_MSG = re.compile(r"(?i)checksum|drift|already applied|edit(ed|ing)?\s+(the\s+)?migration|modify\s+migration|"
                           r"fix\s+migration|out of sync")


def _meaningful(lines: list[str]) -> list[str]:
    return [re.sub(r"\s+", "", ln) for ln in lines if ln.strip() and not _COMMENT.match(ln)]


@candidate("migration_edited_after_commit")
def migration_edited_after_commit(ctx: Ctx):
    """A migration file modified (non-whitespace, non-comment change) more than 24h after it was created on the
    mainline. Release-tag boundaries are not used; "on mainline" is ``commits.on_main``. Boosted by checksum/drift
    wording and by a later migration touching the same tables."""
    events = _mig_events(ctx)
    by_path_tables: dict[str, set[str]] = defaultdict(set)
    for e in events:
        if e["table"]:
            by_path_tables[e["path"]].add(e["table"])
    for path, rows in ctx.changes_by_path.items():
        if not _is_migration_file(path) or len(rows) < 2:
            continue
        first = rows[0]
        c0 = ctx.by_hash.get(first["hash"])
        if c0 is None or first["status"] != "A":
            continue
        if not c0["on_main"]:
            ctx.drop("creator_not_mainline")
            continue
        for r in rows[1:]:
            c = ctx.by_hash.get(r["hash"])
            if c is None or r["status"] == "D":
                continue
            if c["ts"] - c0["ts"] <= DAY:
                ctx.drop("edited_within_24h")
                continue
            if not c["on_main"]:
                ctx.drop("same_branch_edit")
                continue
            if c["is_bulk"] or c["is_merge"] or re.search(r"(?i)squash", c["message"] or ""):
                ctx.drop("squash_or_bulk")
                continue
            added, removed = ctx.added_removed(r["hash"], path)
            ma, mr = _meaningful(added), _meaningful(removed)
            if not (ma or mr) or Counter(ma) == Counter(mr):
                ctx.drop("whitespace_or_comment_only")
                continue
            msg = c["message"] or ""
            tables = by_path_tables.get(path, set())
            follow = [e for e in events if e["path"] != path and e["seq"] > c["seq"] and e["table"] in tables
                      and e["ts"] <= c["ts"] + 60 * DAY][:3]
            boost = bool(_MIG_EDIT_MSG.search(msg))
            yield Hit(
                subject=f"{path}|{r['hash']}",
                score=2 + 3 * boost + 2 * bool(follow) + min(3, math.log1p(len(added) + len(removed))),
                title=f"{path.rsplit('/', 1)[-1]} edited {round((c['ts'] - c0['ts']) / DAY)} days after creation: {subject_of(msg)[:80]}",
                evidence={"migration": path, "created": first["hash"][:10], "edited": r["hash"][:10],
                          "days_after_creation": round((c["ts"] - c0["ts"]) / DAY, 1), "tables": sorted(tables)[:6],
                          "drift_wording": boost, "followup_migrations": sorted({f["path"] for f in follow}),
                          "lines_added": len(ma), "lines_removed": len(mr)},
                commits=[first["hash"], r["hash"]], diffs=[(r["hash"], path)], paths=[path])


# ---------------------------------------------------------------------------- 3. backfill rerun scars
_JOB_RX = re.compile(r"(?i)(backfill|reindex|data[_-]?migrat|migrate[_-]?data|fix[_-]?data|repair|rebuild|reprocess|"
                     r"one[_-]?off|re[_-]?sync|recompute|rehash|bulk[_-]?update)")
_GUARDS = {
    "batch": re.compile(r"(?i)\bbatch|chunk|page_size|\bLIMIT\s+\d|\.iterator\(|paginat"),
    "cursor": re.compile(r"(?i)cursor|last_id|checkpoint|\bresume|after_id|min_id|start_id|\bid\s*>\s|offset"),
    "idempotent": re.compile(r"(?i)idempot|on\s+conflict|get_or_create|update_or_create|upsert|if\s+not\s+exists|"
                             r"already\s+(done|processed|migrated)|\bIS\s+NULL|skip_existing"),
    "throttle": re.compile(r"(?i)sleep\(|throttl|rate[_ -]?limit|\bpause\b|\bdelay\b"),
    "dry_run": re.compile(r"(?i)dry[_-]?run|--dry|\bnoop\b|--commit\b|--apply\b"),
    "timeouts": re.compile(r"(?i)timeout|deadline|statement_timeout|lock_timeout"),
}
_DATA_STMT = re.compile(r"(?i)\bUPDATE\b.*\bSET\b|\bDELETE\s+FROM\b|\.update\(|\.delete\(|\.save\(|bulk_(update|create)")


@candidate("backfill_job_rerun_scars")
def backfill_job_rerun_scars(ctx: Ctx):
    """Backfill/repair/reindex job files with >=3 commits where >=2 distinct guard categories (batch, cursor,
    idempotent, throttle, dry_run, timeouts) were added in separate later commits (the first commit is the baseline).
    Sub-signal: the job runs a data statement but still lacks a category at HEAD (``unguarded_at_head``, small boost)."""
    for path, rows in ctx.changes_by_path.items():
        if not _JOB_RX.search(path):
            continue
        if is_test(path) or not (is_code(path) or path.endswith((".sql", ".sh"))):
            ctx.drop("tests_or_not_code")
            continue
        if len({r["hash"] for r in rows}) < 3:
            ctx.drop("single_commit_job")
            continue
        added_by: dict[str, set[str]] = defaultdict(set)
        used = set()
        first = rows[0]["hash"]
        for r in rows:
            if r["hash"] == first:
                continue
            added, _ = ctx.added_removed(r["hash"], path)
            code = [ln for ln in added if not re.match(r"^\s*(import|from)\s", ln) and not _COMMENT.match(ln)]
            for cat, rx in _GUARDS.items():
                if any(rx.search(ln) for ln in code):
                    added_by[cat].add(r["hash"])
                    used.add(r["hash"])
        if len(added_by) < 2 or len(used) < 2:
            ctx.drop("fewer_than_two_guard_categories")
            continue
        head = ctx.text_at_head(path) or ""
        unguarded = [c for c in _GUARDS if not _GUARDS[c].search(head)] if _DATA_STMT.search(head) else []
        hashes = sorted(used, key=lambda h: ctx.by_hash[h]["seq"])
        yield Hit(
            subject=path, score=len(added_by) * 2 + len(used) + len(unguarded) * 0.5,
            title=f"{path.rsplit('/', 1)[-1]}: {len(added_by)} guard categories added over {len(used)} commits",
            evidence={"guards_added": {k: [h[:10] for h in sorted(v)] for k, v in added_by.items()},
                      "commits_total": len({r['hash'] for r in rows}), "unguarded_at_head": unguarded},
            commits=hashes[:8], diffs=[(h, path) for h in hashes[:4]], paths=[path])


# ---------------------------------------------------------------------------- 4. legacy persisted name pinned
_PERSIST = [
    re.compile(r"db_column\s*=\s*['\"](\w+)['\"]"), re.compile(r"@Column\([^)]*?name\s*=\s*\"(\w+)\""),
    re.compile(r"@Column\(\s*\"(\w+)\"\s*\)"), re.compile(r"serialized_name\s*=\s*['\"](\w+)['\"]"),
    re.compile(r"\balias\s*=\s*['\"](\w+)['\"]"), re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]"),
    re.compile(r"@SerializedName\(\s*\"(\w+)\""), re.compile(r"@JsonProperty\(\s*\"(\w+)\""),
    re.compile(r"`[^`]*(?:json|db|bson|yaml):\"(\w+)"), re.compile(r"@Table\([^)]*name\s*=\s*\"(\w+)\""),
    re.compile(r"\bColumn\(\s*['\"](\w+)['\"]"), re.compile(r"mapped_column\(\s*['\"](\w+)['\"]"),
    re.compile(r"db_table\s*=\s*['\"](\w+)['\"]"),
]
_DECL = [re.compile(r"^\s*(\w+)\s*(?::[^=]+)?=\s*(.*)$"),
         re.compile(r"^\s*(?:(?:public|private|protected|static|final|readonly)\s+)*[\w<>\[\],.?]+\s+(\w+)\s*(;|=.*)$"),
         re.compile(r"^\s*(\w+)\s+([\w.*\[\]]+\s+`.*`)\s*$")]
_NOT_NAMES = {"return", "self", "cls", "def", "class", "if", "for", "else", "import", "from"}


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _decl(line: str) -> tuple[str, str] | None:
    for rx in _DECL:
        m = rx.match(line)
        if m and m.group(1) not in _NOT_NAMES:
            return m.group(1), re.sub(rf"\b{re.escape(m.group(1))}\b", "@", line.strip())
    return None


def _persist_literal(line: str) -> str | None:
    for rx in _PERSIST:
        m = rx.search(line)
        if m:
            return m.group(1)
    return None


def _field_renames(ctx: Ctx, h: str, path: str) -> list[tuple[str, str]]:
    """(old, new) class-field renames of ``path`` in commit ``h`` read from the symbol diary: a ``variable`` symbol
    deleted and another created under the same class parent, or a ``moved`` variable event."""
    deleted: dict[str, list[str]] = defaultdict(list)
    created: dict[str, list[str]] = defaultdict(list)
    out: list[tuple[str, str]] = []
    for ev in ctx.events_by_commit.get(h, []):
        if ev["path"] != path or ev["kind"] != "variable":
            continue
        sym = ctx.symbols.get(ev["uid"])
        parent = (sym["parent"] if sym else None) or (ev["qualname"] or "").rsplit(".", 1)[0]
        name = (ev["qualname"] or "").rsplit(".", 1)[-1]
        if ev["change"] == "deleted":
            deleted[parent].append(name)
        elif ev["change"] == "created":
            created[parent].append(name)
        elif ev["change"] == "moved" and ev["old_uid"]:
            old = ctx.symbols.get(ev["old_uid"])
            if old and old["name"] and old["name"] != name:
                out.append((old["name"], name))
    for parent, news in created.items():
        olds = deleted.get(parent, [])
        if len(olds) == len(news):
            out.extend(zip(sorted(olds), sorted(news)))
    return out


@candidate("legacy_persisted_name_pinned")
def legacy_persisted_name_pinned(ctx: Ctx):
    """A field/column/class renamed in code while a persistence literal (db_column, @Column(name), serialized_name,
    alias, __tablename__, json/db struct tag ...) keeps (or is newly given) the old name. Renames come from the symbol
    diary first: class fields are ``variable`` symbols, so a field deleted and another created under the same class in
    one commit (or a ``moved`` variable event) is a rename (``detected_via: symbol``). When a changed file has no such
    symbol rename, it falls back to pairing removed and added declaration lines of that file in that commit (same line
    shape, different identifier; ``detected_via: lines``). Literals on neighbouring lines (Java annotations) are not
    seen."""
    rows = ctx.grep(r"db_column|@Column|serialized_name|\balias\s*=|__tablename__|SerializedName|JsonProperty|"
                    r"(json|db|bson):\"|@Table\(|\bColumn\(|mapped_column|db_table", None)
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in rows:
        if r["sign"] == "+":
            by_file[(r["hash"], r["path"])]["+"].append(r)
    for (h, path), d in by_file.items():
        if is_test(path) or is_generated(path) or is_vendored(path) or re.search(r"(?i)fixtures?|\.json$|\.md$", path):
            ctx.drop("tests_generated_fixtures")
            continue
        d["-"] = [x for x in ctx.lines_of(h, path) if x["sign"] == "-"]
        removed = [(x, _decl(x["text"])) for x in d["-"]]
        removed = [(x, dd) for x, dd in removed if dd]
        renames = _field_renames(ctx, h, path)
        via = "symbol" if renames else "lines"
        for a in d["+"]:
            da = _decl(a["text"])
            lit = _persist_literal(a["text"])
            if not da or not lit:
                continue
            if renames:
                cands = []
                for old, new in renames:
                    if new != da[0]:
                        continue
                    dr = next((dd for _, dd in removed if dd[0] == old), None)
                    cands.append(dr or (old, ""))
            else:
                cands = [dr for _, dr in removed]
            for dr in cands:
                if dr[0] == da[0]:
                    continue
                old, new = dr[0], da[0]
                if _norm_name(old) == _norm_name(new):
                    ctx.drop("case_or_underscore_only")
                    continue
                unchanged_shape = bool(dr[1]) and dr[1] == da[1]
                pinned_old = _norm_name(lit) == _norm_name(old)
                if not unchanged_shape and not pinned_old:
                    continue
                if _norm_name(lit) == _norm_name(new):
                    ctx.drop("literal_matches_new_name")
                    continue
                mode = "literal unchanged" if unchanged_shape else "literal newly added"
                yield Hit(
                    subject=f"{h}|{path}|{old}->{new}",
                    score=3 + 3 * pinned_old + min(3, math.log1p(len(ctx.path_commits(path)))),
                    title=f"{old} renamed to {new} but persisted name stays '{lit}' ({mode})",
                    evidence={"old_name": old, "new_name": new, "persisted_literal": lit, "mode": mode,
                              "pinned_to_old_name": pinned_old, "line": a["text"].strip()[:140],
                              "detected_via": via},
                    commits=[h], diffs=[(h, path)], paths=[path], symbols=_sym_uid(ctx, path, new))
                break


# ---------------------------------------------------------------------------- 5. rollback unsafe
_DESTRUCTIVE = {"drop": "drops column", "drop_table": "drops table"}


def _irreversible(ctx: Ctx, h: str, path: str) -> str | None:
    text = ctx.text_at(h, path) or ""
    if not text:
        return None
    if re.search(r"def\s+upgrade\b", text):
        m = re.search(r"def\s+downgrade\b[^\n]*\n((?:[ \t]+[^\n]*\n?|\n)*)", text)
        if not m:
            return "alembic migration has no downgrade()"
        body = [ln.strip() for ln in m.group(1).splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not body or body == ["pass"] or any("NotImplementedError" in ln for ln in body):
            return "alembic downgrade() is empty or raises NotImplementedError"
    if re.search(r"RunPython\(", text) and not re.search(r"reverse_code|RunPython\.noop|reverse_sql", text):
        return "Django RunPython without reverse_code"
    if re.search(r"RunSQL\(", text) and not re.search(r"reverse_sql", text):
        return "Django RunSQL without reverse_sql"
    if "IrreversibleMigration" in text:
        return "Rails migration raises IrreversibleMigration"
    return None


def _head_symbol_refs(ctx: Ctx, name: str, after_seq: int) -> list[str]:
    """uids of live non-test functions/classes that gained a line naming ``name`` after ``after_seq`` and whose HEAD
    body still names it (symbol attribution of changed lines instead of file-level mentions)."""
    key = name.lower()
    rows = ctx.q("SELECT DISTINCT symbol FROM diff_lines WHERE sign='+' AND seq > ? AND symbol != '' "
                 "AND instr(lower(text), ?) > 0 LIMIT 200", (after_seq, key))
    out = []
    for r in rows:
        sym = ctx.symbols.get(r["symbol"])
        if sym is None or not sym["alive"] or is_test(sym["path"]) or MIGRATION_RX.search(sym["path"]):
            continue
        if key in (sym["body"] or "").lower():
            out.append(sym["uid"])
    return out


@candidate("rollback_unsafe_change")
def rollback_unsafe_change(ctx: Ctx):
    """Migrations that destroy data (DROP COLUMN/TABLE, RemoveField, DeleteModel) on a table older than 14 days, or
    are irreversible (missing/empty downgrade, RunPython without reverse_code). Ranked by same-commit application
    edits naming the column/table, later non-migration mentions of it, later fix/revert commits, and the spec's
    "references at HEAD" factor: functions/classes that gained a line naming it after the migration (attributed through
    ``diff_lines.symbol``) and whose body at HEAD still mentions it (``head_symbol_references``). Later diff mentions
    remain as the wider, history-wide count. Needs ``diff_lines.symbol``; without it the HEAD factor is 0."""
    events = _mig_events(ctx)
    creates = _creates(ctx)
    n = 0
    for e in events:
        if e["op"] not in _DESTRUCTIVE or not e["table"]:
            continue
        cr = creates.get(e["table"])
        if cr is not None and (cr["hash"] == e["hash"] or e["ts"] - cr["ts"] < 14 * DAY):
            ctx.drop("same_release_or_new_table")
            continue
        name = e["col"] or e["table"]
        same = [r for r in ctx.lines_of(e["hash"]) if not MIGRATION_RX.search(r["path"]) and not is_test(r["path"])
                and name in r["text"].lower()]
        later = [m for m in _mentions(ctx, name) if m[2] > e["seq"]] if n < 120 else []
        head_refs = _head_symbol_refs(ctx, name, e["seq"]) if n < 120 else []
        n += 1
        fixes = _later_fixes(ctx, events, e["seq"], e["table"], 90)
        yield Hit(
            subject=f"{e['hash']}|{e['table']}|{e['col'] or ''}|{e['op']}",
            score=3 + 2 * min(len(same), 5) + min(len(later), 10) * 0.4 + 5 * len(fixes) + (2 if e["op"] == "drop_table" else 0)
            + min(len(head_refs), 6) * 1.0,
            title=f"{_DESTRUCTIVE[e['op']]} {e['table']}{'.' + e['col'] if e['col'] else ''} in {e['path'].rsplit('/', 1)[-1]}",
            evidence={"table": e["table"], "column": e["col"], "statement": e["text"][:140],
                      "same_commit_app_lines": len(same), "later_mentions": len(later),
                      "head_symbol_references": head_refs[:6],
                      "later_fixes": [{"hash": f["hash"][:10], "path": f["path"]} for f in fixes[:3]]},
            commits=[e["hash"], *[f["hash"] for f in fixes[:2]]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])
    for path, rows in ctx.changes_by_path.items():
        if not _is_migration_file(path) or not rows or rows[0]["status"] != "A":
            continue
        reason = _irreversible(ctx, rows[0]["hash"], path)
        if not reason:
            continue
        tabs = sorted({e["table"] for e in events if e["path"] == path and e["table"]})
        if tabs and all(creates.get(t, {}).get("hash") == rows[0]["hash"] for t in tabs):
            ctx.drop("creates_only_new_tables")
            continue
        yield Hit(subject=f"{rows[0]['hash']}|{path}|irreversible", score=3 + len(tabs),
                  title=f"{path.rsplit('/', 1)[-1]}: {reason}", evidence={"reason": reason, "tables": tabs[:6]},
                  commits=[rows[0]["hash"]], diffs=[(rows[0]["hash"], path)], paths=[path])


# ---------------------------------------------------------------------------- 6. cascade / FK rule change
_RULE = re.compile(r"(?i)on_delete\s*=|ondelete\s*=|\bON\s+DELETE\b|dependent\s*(:|=>)|orphanRemoval|\bcascade\s*=|"
                   r"CascadeType|cascade:")
_RULE_VAL = [re.compile(r"on_delete\s*=\s*(?:models\.)?(\w+)"), re.compile(r"(?i)ondelete\s*=\s*['\"]([\w ]+)['\"]"),
             re.compile(r"(?i)ON\s+DELETE\s+(CASCADE|SET\s+NULL|RESTRICT|NO\s+ACTION|SET\s+DEFAULT)"),
             re.compile(r"dependent\s*(?::|=>)\s*:?['\"]?(\w+)"), re.compile(r"orphanRemoval\s*=\s*(\w+)"),
             re.compile(r"\bcascade\s*=\s*([^,)]+)"), re.compile(r"cascade:\s*([^,)]+)")]
_REL_KEY = [re.compile(r"^\s*(\w+)\s*=\s*(?:models\.|db\.|sa\.)?(?:ForeignKey|OneToOneField|relationship|Column)\b"),
            re.compile(r"(?:belongs_to|has_many|has_one)\s+:(\w+)"), re.compile(r"(?i)REFERENCES\s+(\w+)"),
            re.compile(r"(?:ForeignKey|OneToOneField|relationship)\(\s*['\"]?([\w.]+)"),
            re.compile(r"private\s+[\w<>]+\s+(\w+)\s*;")]


def _rule_value(t: str) -> str | None:
    for rx in _RULE_VAL:
        m = rx.search(t)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip().upper().replace("_", " ")
    return None


def _rel_key(t: str) -> str | None:
    for rx in _REL_KEY:
        m = rx.search(t)
        if m:
            return m.group(1).lower()
    return None


@candidate("cascade_and_fk_rule_change")
def cascade_and_fk_rule_change(ctx: Ctx):
    """Delete-rule changed on an existing relationship (Django on_delete, SQL ON DELETE, SQLAlchemy ondelete/cascade,
    Rails dependent, JPA orphanRemoval). Removed/added rule lines are paired within one file+commit by relationship
    key (field name or referenced table), or directly when each side has exactly one rule line. CASCADE additions and
    PROTECT/RESTRICT removals rank first."""
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(_RULE.pattern, None, limit=60000):
        by_file[(r["hash"], r["path"])][r["sign"]].append(r)
    for (h, path), d in by_file.items():
        if is_test(path) or is_generated(path) or is_vendored(path) or is_docs(path):
            ctx.drop("test_or_generated_model")
            continue
        fc = next((x for x in ctx.changes(h) if x["path"] == path), None)
        if fc is not None and fc["status"] == "A":
            ctx.drop("initial_creation")
            continue
        rem = [t for t in ((x, _rule_value(x["text"]), _rel_key(x["text"])) for x in d["-"]) if t[1]]
        add = [t for t in ((x, _rule_value(x["text"]), _rel_key(x["text"])) for x in d["+"]) if t[1]]
        for a in add:
            m = next((r for r in rem if r[2] and r[2] == a[2]), None)
            if m is None and len(add) == 1 and len(rem) == 1:
                m = rem[0]
            if not m:
                continue
            (ro, rv, rk), (ao, av, ak) = m, a
            if rv == av:
                ctx.drop("same_rule_reordered_args")
                continue
            sc = 3 + (4 if av == "CASCADE" else 0) + (3 if rv in ("PROTECT", "RESTRICT") else 0) + \
                (1 if av in ("SET NULL", "NO ACTION") else 0)
            others = len(_RULE.findall(ctx.text_at_head(path) or ""))
            yield Hit(
                subject=f"{h}|{path}|{rk or ak}",
                score=sc + min(others, 6) * 0.2,
                title=f"{(rk or ak or 'relationship')} delete rule {rv} -> {av} in {path.rsplit('/', 1)[-1]}",
                evidence={"relationship": rk or ak, "old_rule": rv, "new_rule": av, "old_line": ro["text"].strip()[:140],
                          "new_line": ao["text"].strip()[:140], "other_rules_in_file_at_head": others},
                commits=[h], diffs=[(h, path)], paths=[path], symbols=_sym_uid(ctx, path, ak or rk or ""))


# ---------------------------------------------------------------------------- 7. index add/drop/re-add cycle
@candidate("index_add_drop_readd_cycle")
def index_add_drop_readd_cycle(ctx: Ctx):
    """Index lineage per (table, set of columns) from migrations: >=3 create/drop events over >=2 commits that include
    a re-create, or a drop followed by a create with a different predicate / uniqueness / column order. Drops that
    name only the index are resolved through earlier create events. Baseline/initial migrations, same-commit churn
    and pure renames (same definition, different name) are dropped."""
    by_name: dict[str, tuple] = {}
    lineage: dict[tuple, list[dict]] = defaultdict(list)
    for e in _mig_events(ctx):
        if e["op"] == "create_index" and e.get("cols"):
            key = (e["table"], frozenset(e["cols"]))
            if e.get("idx"):
                by_name[e["idx"]] = key
        elif e["op"] == "drop_index":
            key = by_name.get(e.get("idx", ""))
            if key is None:
                continue
        else:
            continue
        if _BASELINE_RX.search(e["path"]):
            ctx.drop("baseline_migration")
            continue
        lineage[key].append(e)
    for key, evs in lineage.items():
        if len({e["hash"] for e in evs}) < 2:
            ctx.drop("same_pr_churn")
            continue
        recreate = any(evs[i]["op"] == "drop_index" and any(x["op"] == "create_index" for x in evs[i + 1:])
                       for i in range(len(evs)))
        changed = False
        for i, e in enumerate(evs):
            if e["op"] != "drop_index":
                continue
            nxt = next((x for x in evs[i + 1:] if x["op"] == "create_index"), None)
            prev = next((x for x in reversed(evs[:i]) if x["op"] == "create_index"), None)
            if nxt and prev and (tuple(nxt["cols"]), nxt["unique"], nxt["pred"]) != \
                    (tuple(prev["cols"]), prev["unique"], prev["pred"]):
                changed = True
        if not ((len(evs) >= 3 and recreate) or changed):
            continue
        if len(evs) == 2 and not changed:
            ctx.drop("rename_only")
            continue
        yield Hit(
            subject=f"{key[0]}|{','.join(sorted(key[1]))}",
            score=len(evs) * 2 + 4 * changed + len({e["hash"] for e in evs}),
            title=f"index on {key[0]}({', '.join(sorted(key[1]))}) created/dropped {len(evs)}x"
                  f"{' with changed definition' if changed else ''}",
            evidence={"table": key[0], "columns": sorted(key[1]), "definition_changed": changed,
                      "events": [{"op": e["op"], "hash": e["hash"][:10], "name": e.get("idx"), "unique": e.get("unique"),
                                  "predicate": e.get("pred"), "order": list(e.get("cols") or [])} for e in evs[:8]]},
            commits=list(dict.fromkeys(e["hash"] for e in evs))[:8],
            diffs=[(e["hash"], e["path"]) for e in evs[:4]], paths=list(dict.fromkeys(e["path"] for e in evs))[:6])


# ---------------------------------------------------------------------------- 8. migration followed by hotfix migration
@candidate("migration_followed_by_hotfix_migration")
def migration_followed_by_hotfix_migration(ctx: Ctx):
    """>=2 add/alter/rename events on the same (table, column) from different migration files within 14 days of the
    first. Initial/baseline migrations are skipped, so are slower chains (planned expand/contract). Fix-style commit
    messages on the follow-ups boost the score."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in _mig_events(ctx):
        if e["op"] in ("add", "alter", "rename") and e["table"] and e["col"]:
            if _BASELINE_RX.search(e["path"]):
                ctx.drop("initial_migration")
                continue
            groups[(e["table"], e["col"])].append(e)
    for (t, c), evs in groups.items():
        chain = [evs[0]]
        for e in evs[1:]:
            if e["ts"] - evs[0]["ts"] <= 14 * DAY:
                chain.append(e)
            else:
                ctx.drop("slower_than_14_days")
        if len(chain) < 2 or len({e["path"] for e in chain}) < 2:
            continue
        fixy = [e for e in chain[1:] if ctx.is_fix(ctx.by_hash[e["hash"]])]
        yield Hit(
            subject=f"{t}.{c}|{chain[0]['hash']}",
            score=len(chain) * 2 + 4 * len(fixy) + (1 if chain[-1]["ts"] - chain[0]["ts"] < 2 * DAY else 0),
            title=f"{t}.{c} altered {len(chain)}x by separate migrations within {round((chain[-1]['ts'] - chain[0]['ts']) / DAY, 1)} days",
            evidence={"table": t, "column": c,
                      "events": [{"op": e["op"], "hash": e["hash"][:10], "path": e["path"],
                                  "subject": subject_of(ctx.by_hash[e["hash"]]["message"])[:80]} for e in chain[:6]],
                      "fix_style_followups": len(fixy)},
            commits=list(dict.fromkeys(e["hash"] for e in chain))[:6],
            diffs=[(e["hash"], e["path"]) for e in chain[:4]], paths=list(dict.fromkeys(e["path"] for e in chain)))


# ---------------------------------------------------------------------------- 9. enum persisted value change
_ENUM_SIG = re.compile(r"\b(Enum|IntEnum|StrEnum|IntFlag|Flag|IntegerChoices|TextChoices|Choices)\b")
_ENUM_START = re.compile(r"(export\s+)?(const\s+)?enum\b|public\s+enum\b")


def _enum_members(text: str, name: str) -> list[tuple[str, str | None]] | None:
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(rf"^(\s*)class\s+{re.escape(name)}\b.*:\s*$", ln)
        if m:
            indent = len(m.group(1))
            out: list[tuple[str, str | None]] = []
            member_indent = None
            for body in lines[i + 1:]:
                st = body.strip()
                if not st or st.startswith(("#", '"""', "'''")):
                    continue
                ind = len(body) - len(body.lstrip())
                if ind <= indent:
                    break
                member_indent = ind if member_indent is None else member_indent
                mm = re.match(r"^\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s*(#.*)?$", body)
                if mm and ind == member_indent and not mm.group(1).startswith("_"):
                    val = mm.group(2).strip()
                    if "auto()" in val:
                        val = "auto()"
                    elif val.startswith("(") or "," in val:
                        val = val.lstrip("(").split(",")[0].strip()
                    out.append((mm.group(1), val))
            return out
        if re.search(rf"\benum\s+(?:class\s+)?{re.escape(name)}\b", ln):
            blob = "\n".join(lines[i:i + 80])
            m2 = re.search(r"\{(.*?)\}", blob, re.S)
            if m2:
                out = []
                for part in m2.group(1).split(","):
                    part = re.sub(r"//[^\n]*", "", part)
                    mm = re.match(r"\s*([A-Za-z_]\w*)\s*(?:=\s*([^,;}\n]+))?", part)
                    if mm:
                        out.append((mm.group(1), mm.group(2).strip() if mm.group(2) else None))
                return out
    return None


@candidate("enum_value_persisted_change")
def enum_value_persisted_change(ctx: Ctx):
    """Enum classes (Python Enum/IntEnum/Choices, TS/Java ``enum``) whose members changed in a persisted-unsafe way:
    a member inserted mid-body in an implicit (auto/ordinal) enum, an explicit value changed or reused, or a member
    removed. Pure appends are dropped. Usage in model/migration/serializer-like paths comes from the diff index
    (``used_in``); enums with no such usage are dropped as non-persisted. Only each enum's 25 latest modifications
    are inspected (file text is parsed before/after)."""
    persist_path = re.compile(r"(?i)model|migrat|serializ|schema|entity|dto|orm|db|proto|column|field")
    for s in ctx.alive_symbols:
        if s["kind"] not in ("class", "enum", "interface"):
            continue
        first = (s["body"] or "").lstrip()[:80]
        if not (_ENUM_SIG.search(s["signature"] or "") or _ENUM_START.match(first)):
            continue
        if is_test(s["path"]) or is_vendored(s["path"]) or is_generated(s["path"]):
            continue
        name = s["qualname"].rsplit(".", 1)[-1]
        evs = [e for e in ctx.symbol_events(s["uid"]) if e["change"] == "modified"
               and not ctx.by_hash[e["hash"]]["is_bulk"]][-25:]
        if not evs:
            continue
        used = sorted({p for _, p, _ in _mentions(ctx, name) if persist_path.search(p) and p != s["path"]})[:6]
        for e in evs:
            old, new = ctx.text_before(e["hash"], s["path"]), ctx.text_at(e["hash"], s["path"])
            if not old or not new:
                continue
            mo, mn = _enum_members(old, name), _enum_members(new, name)
            if mo is None or mn is None or mo == mn:
                continue
            old_names, new_names = [m[0] for m in mo], [m[0] for m in mn]
            removed = [x for x in old_names if x not in new_names]
            added = [x for x in new_names if x not in old_names]
            dv, ov = dict(mn), dict(mo)
            changed_vals = [x for x in old_names if x in dv and dv[x] != ov[x] and ov[x] is not None]
            implicit = any(v in (None, "auto()") for _, v in mo)
            mid = [x for x in added if new_names.index(x) < len(new_names) - len(added)] if implicit else []
            old_vals = {ov[x] for x in removed if ov.get(x) is not None}
            reused = [x for x in added if dv.get(x) in old_vals]
            if not (removed or changed_vals or mid or reused):
                ctx.drop("pure_append")
                continue
            if not used:
                ctx.drop("not_persisted")
                continue
            kinds = [k for k, v in (("member_removed", removed), ("value_changed", changed_vals),
                                    ("inserted_mid_implicit", mid), ("value_reused", reused)) if v]
            yield Hit(
                subject=f"{e['hash']}|{s['uid']}",
                score=2 + 3 * bool(changed_vals or reused) + 2 * bool(mid) + 2 * bool(removed) + min(len(used), 4),
                title=f"enum {name}: {', '.join(kinds)} ({', '.join((removed + changed_vals + mid + reused)[:3])})",
                evidence={"enum": name, "changes": kinds, "removed": removed[:5], "value_changed": changed_vals[:5],
                          "inserted_mid": mid[:5], "reused_values": reused[:5], "used_in": used},
                commits=[e["hash"]], diffs=[(e["hash"], s["path"])], paths=[s["path"], *used[:2]], symbols=[s["uid"]])


# ---------------------------------------------------------------------------- 10. read-after-write replica fix
_RAW_MSG = re.compile(r"(?i)replica\s+lag|replication\s+lag|read[- ]your[- ]writes?|stale\s+reads?|read[- ]after[- ]write|"
                      r"reads?\s+from\s+(the\s+)?(replica|secondary)|lagging\s+replica")
_RAW_TOK = re.compile(r"""using\(\s*['"]default['"]\s*\)|\buse_primary\b|DATABASE_ROUTERS|read_from_primary|force_primary|"""
                      r"""with_primary|\.on_primary\b|use_master|from_master|prefer_primary""")


@candidate("read_after_write_replica_fix")
def read_after_write_replica_fix(ctx: Ctx):
    """Commits whose message mentions replica lag / read-your-writes / stale reads, or fix commits that add
    primary-routing tokens (using('default'), use_primary, DATABASE_ROUTERS ...) to application code. Test files and
    analytics/report paths are ignored; single-DB projects cannot be detected (no data)."""
    tok_by_commit: dict[str, list] = defaultdict(list)
    for r in ctx.grep(_RAW_TOK.pattern, "+", limit=40000):
        if is_test(r["path"]) or re.search(r"(?i)test", r["path"].rsplit("/", 1)[-1]):
            ctx.drop("test_settings")
            continue
        if re.search(r"(?i)analytic|report|warehouse|bi/|metrics", r["path"]):
            ctx.drop("analytics_read")
            continue
        if is_docs(r["path"]):
            continue
        tok_by_commit[r["hash"]].append(r)
    for c in ctx.commits:
        if ctx.is_bot(c) or c["is_merge"]:
            continue
        msg = c["message"] or ""
        by_msg = bool(_RAW_MSG.search(msg))
        toks = tok_by_commit.get(c["hash"], [])
        if not (by_msg or (toks and ctx.is_fix(c))):
            continue
        paths = sorted({r["path"] for r in toks} or {fc["path"] for fc in ctx.changes(c["hash"])
                                                      if is_code(fc["path"]) and not is_test(fc["path"])})[:6]
        if not paths:
            ctx.drop("no_application_code")
            continue
        yield Hit(
            subject=c["hash"], score=2 + 4 * by_msg + 2 * min(len(toks), 3) + (1 if ctx.is_fix(c) else 0),
            title=subject_of(msg)[:100],
            evidence={"message_match": by_msg,
                      "routing_tokens": sorted({_RAW_TOK.search(r["text"]).group(0) for r in toks})[:5], "paths": paths},
            commits=[c["hash"]], diffs=[(c["hash"], p) for p in paths[:3]], paths=paths)


# ---------------------------------------------------------------------------- 11. unique constraint semantics
_UC_LINE = re.compile(r"(?i)UniqueConstraint\(|unique_together|UNIQUE\s*(INDEX|KEY|\()|UniqueIndex|"
                      r"@Table\(.*uniqueConstraints")


def _uc_def(t: str) -> tuple[str | None, tuple, str] | None:
    """-> (name, ordered cols, predicate) from a model/DDL line, or None."""
    name = _kw(t, "name")
    m = re.search(r"fields\s*=\s*\[([^\]]*)\]", t) or re.search(r"unique_together\s*=\s*[\[(]*\(?([^)\]]*)", t) or \
        re.search(r"(?i)UNIQUE\s*(?:INDEX|KEY)?\s*(?:\w+\s*)?\(([^)]*)\)", t)
    if not m:
        return None
    pred = _first(r"condition\s*=\s*(.*?)(?:,\s*name|\)\)|$)", t) or _pred(t)
    return name, _cols(m.group(1)), pred.strip().lower()[:80]


@candidate("unique_constraint_semantics_change")
def unique_constraint_semantics_change(ctx: Ctx):
    """A unique constraint/index whose definition changed: predicate (partial), UNIQUE flag or column order. Sources:
    (a) migration index/constraint events paired by name or column set across time; (b) model lines (UniqueConstraint,
    unique_together) paired by name/column set within a commit. Pure renames, creation migrations and tables never
    referenced outside migrations are dropped. Conflict-handling evidence is attached only when an
    IntegrityError/UniqueViolation/ON CONFLICT line mentions the table or model."""
    conflict = ctx.grep(r"IntegrityError|UniqueViolation|ON CONFLICT|DuplicateKey|get_or_create", "+", limit=5000)
    seen: dict = {}
    found = []
    for e in _mig_events(ctx):
        if e["op"] != "create_index" or not e.get("cols"):
            continue
        for key in (("n", e["table"], e.get("idx")), ("c", e["table"], frozenset(e["cols"]))):
            if not key[2]:
                continue
            prev = seen.get(key)
            seen[key] = e
            if prev is None or prev["hash"] == e["hash"]:
                continue
            old = (tuple(prev["cols"]), prev["unique"], prev["pred"])
            new = (tuple(e["cols"]), e["unique"], e["pred"])
            if old != new and (prev["unique"] or e["unique"]):
                found.append((prev, e, old, new))
    done = set()
    for prev, e, old, new in found:
        k = (e["hash"], e["table"], e.get("idx"))
        if k in done:
            continue
        done.add(k)
        if not _mentions(ctx, e["table"]):
            ctx.drop("unreferenced_table")
            continue
        what = [w for w, a, b in (("predicate", old[2], new[2]), ("unique_flag", old[1], new[1]),
                                  ("column_order", old[0], new[0])) if a != b]
        if not what:
            ctx.drop("rename_only")
            continue
        ev = [r for r in conflict if e["table"] in r["text"].lower() or e["table"] in r["path"].lower()][:3]
        yield Hit(
            subject=f"{e['hash']}|{e['table']}|{e.get('idx') or ','.join(sorted(e['cols']))}",
            score=3 + 2 * ("predicate" in what) + 2 * ("unique_flag" in what) + 3 * len(ev),
            title=f"unique definition on {e['table']}({', '.join(e['cols'])}) changed: {', '.join(what)}",
            evidence={"table": e["table"], "changed": what,
                      "old": {"cols": list(old[0]), "unique": old[1], "predicate": old[2]},
                      "new": {"cols": list(new[0]), "unique": new[1], "predicate": new[2]},
                      "previous_commit": prev["hash"][:10],
                      "conflict_handling_lines": [{"path": r["path"], "hash": r["hash"][:10]} for r in ev]},
            commits=[e["hash"], prev["hash"]], diffs=[(e["hash"], e["path"]), (prev["hash"], prev["path"])],
            paths=[e["path"], *[r["path"] for r in ev]])
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(_UC_LINE.pattern, None, limit=40000):
        if not MIGRATION_RX.search(r["path"]) and not is_test(r["path"]) and not is_generated(r["path"]):
            by_file[(r["hash"], r["path"])][r["sign"]].append(r)
    for (h, path), d in by_file.items():
        fc = next((x for x in ctx.changes(h) if x["path"] == path), None)
        if fc is not None and fc["status"] == "A":
            continue
        rem = [_uc_def(x["text"]) for x in d["-"]]
        for a in d["+"]:
            da = _uc_def(a["text"])
            if not da:
                continue
            for dr in rem:
                if not dr or not ((da[0] and da[0] == dr[0]) or set(da[1]) == set(dr[1])):
                    continue
                if (da[1], da[2]) == (dr[1], dr[2]):
                    ctx.drop("rename_only")
                    continue
                what = [w for w, x, y in (("predicate", dr[2], da[2]), ("column_order", dr[1], da[1])) if x != y]
                stem = path.rsplit("/", 1)[-1].split(".")[0].lower()
                ev = [x for x in conflict if stem in x["path"].lower()][:2]
                yield Hit(
                    subject=f"{h}|{path}|{da[0] or ','.join(sorted(da[1]))}",
                    score=3 + 2 * len(what) + 3 * len(ev),
                    title=f"unique definition in {path.rsplit('/', 1)[-1]} changed: {', '.join(what) or 'columns'}",
                    evidence={"changed": what, "old": {"cols": list(dr[1]), "predicate": dr[2]},
                              "new": {"cols": list(da[1]), "predicate": da[2]},
                              "conflict_handling_lines": [{"path": x["path"], "hash": x["hash"][:10]} for x in ev]},
                    commits=[h], diffs=[(h, path)], paths=[path])
                break


# ---------------------------------------------------------------------------- 12. ORM model <-> migration drift
_FIELD_LINE = re.compile(r"^\s*(\w+)\s*(?::[^=]+)?=\s*(?:models\.|fields\.|db\.|sa\.|sqlalchemy\.)?"
                         r"(\w*(?:Field|Key|Column|mapped_column|relationship)\w*)\(")
_MISSING_MIG = re.compile(r"(?i)missing\s+migrations?|forgot\s+(to\s+)?(add\s+)?(the\s+)?migrations?|makemigrations|"
                          r"add(ed)?\s+(the\s+)?migration\s+for|migration\s+for")


def _same_pr_migration(ctx: Ctx, h: str, c, my: int, seq_of: dict, migs: list[dict]) -> str | None:
    """How a migration commit belongs to the same PR as model commit ``h`` (or None): same branch (``branch_of``),
    same linked PR (``discussions_for``), else the previous-two-commits-by-the-same-author proxy. A known, different
    branch on both sides rules the proxy out."""
    my_branch = ctx.branch_of.get(h)
    my_prs = {d["number"] for d in ctx.discussions_for(h)} if ctx.discussions else set()
    for m in migs:
        mh = m["hash"]
        if mh == h or mh not in ctx.by_hash:
            continue
        if my_branch and ctx.branch_of.get(mh) == my_branch and seq_of.get(mh, -1) > my - 50                 and ctx.by_hash[mh]["ts"] <= c["ts"] + 3 * DAY:
            return "branch"
        if my_prs and my_prs & {d["number"] for d in ctx.discussions_for(mh)}:
            return "pr"
    for m in migs:
        mh = m["hash"]
        if 0 < my - seq_of.get(mh, -99) <= 2 and ctx.by_hash[mh]["author_email"] == c["author_email"]:
            mb = ctx.branch_of.get(mh)
            if my_branch and mb and mb != my_branch:
                continue
            return "author_proxy"
    return None


@candidate("orm_model_migration_drift")
def orm_model_migration_drift(ctx: Ctx):
    """A commit adds/removes/retypes a model field (Django field, SQLAlchemy Column/mapped_column) in a package with a
    sibling ``migrations/`` directory (or any alembic versions dir) without a migration in the same commit or the same
    PR, followed within 45 days by a
    migration with a 'missing migration' message or naming the changed field. "Same PR" = a migration on the same
    branch (``ctx.branch_of``) within 3 days, or a commit linked to the same PR/issue (``ctx.discussions_for``);
    fallback when neither is known: a migration in the previous two commits by the same author (skipped when both
    commits are known to be on different branches). Abstract/unmanaged models are dropped from the file text."""
    mig_dirs: dict[str, list[dict]] = defaultdict(list)
    for p, rows in ctx.changes_by_path.items():
        if _is_migration_file(p) and not p.endswith(".sql"):
            d = p.rsplit("/", 1)[0]
            if d.endswith("/versions") or "/versions" in d:
                d = "alembic:" + d
            for r in rows:
                if r["status"] in ("A", "M"):
                    mig_dirs[d].append({"hash": r["hash"], "path": p})
    alembic_any = [d for d in mig_dirs if d.startswith("alembic:")]
    seq_of = {c["hash"]: c["seq"] for c in ctx.commits}
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(r"^\s*\w+\s*(:[^=]+)?=\s*[\w.]*(Field|ForeignKey|OneToOneField|Column|mapped_column|relationship)\w*\(",
                      None, path_like="%.py", limit=60000):
        if not MIGRATION_RX.search(r["path"]) and not is_test(r["path"]):
            by_file[(r["hash"], r["path"])][r["sign"]].append(r)
    for (h, path), d in by_file.items():
        c = ctx.by_hash[h]
        pkg = path.rsplit("/", 1)[0] if "/" in path else ""
        want = (pkg + "/migrations") if pkg else "migrations"
        dirs = [x for x in mig_dirs if x == want] or alembic_any[:1]
        if not dirs:
            continue
        fc = next((x for x in ctx.changes(h) if x["path"] == path), None)
        if fc is not None and fc["status"] == "A":
            ctx.drop("new_model_file")
            continue
        rem = {m.group(1): m.group(2) for x in d["-"] if (m := _FIELD_LINE.match(x["text"]))}
        add = {m.group(1): m.group(2) for x in d["+"] if (m := _FIELD_LINE.match(x["text"]))}
        changes = [f"+{n}" for n in add if n not in rem] + [f"-{n}" for n in rem if n not in add] + \
                  [f"~{n}" for n in add if n in rem and add[n] != rem[n]]
        if not changes:
            continue
        if re.search(r"abstract\s*=\s*True|managed\s*=\s*False|__abstract__\s*=\s*True", ctx.text_at(h, path) or ""):
            ctx.drop("abstract_or_unmanaged")
            continue
        if any(m["hash"] == h for dd in dirs for m in mig_dirs[dd]):
            ctx.drop("migration_in_same_commit")
            continue
        my = seq_of[h]
        same_pr = _same_pr_migration(ctx, h, c, my, seq_of, [m for dd in dirs for m in mig_dirs[dd]])
        if same_pr:
            ctx.drop("same_pr_chain")
            continue
        names = [x[1:] for x in changes]
        later = sorted((m for dd in dirs for m in mig_dirs[dd]
                        if seq_of.get(m["hash"], -1) > my and ctx.by_hash[m["hash"]]["ts"] <= c["ts"] + 45 * DAY),
                       key=lambda m: seq_of[m["hash"]])
        follow = next((m for m in later if _MISSING_MIG.search(ctx.by_hash[m["hash"]]["message"] or "")
                       or any(n in (ctx.text_at(m["hash"], m["path"]) or "") for n in names)), None)
        if follow is None:
            ctx.drop("no_followup_migration")
            continue
        fc2 = ctx.by_hash[follow["hash"]]
        yield Hit(
            subject=f"{h}|{path}",
            score=4 + 3 * bool(_MISSING_MIG.search(fc2["message"] or "")) + min(len(changes), 4),
            title=f"model fields {', '.join(changes[:4])} in {path.rsplit('/', 1)[-1]} had no migration until "
                  f"{follow['path'].rsplit('/', 1)[-1]}",
            evidence={"field_changes": changes[:8], "migration_commit": follow["hash"][:10], "migration": follow["path"],
                      "days_later": round((fc2["ts"] - c["ts"]) / DAY, 1), "followup_subject": subject_of(fc2["message"])},
            commits=[h, follow["hash"]], diffs=[(h, path), (follow["hash"], follow["path"])],
            paths=[path, follow["path"]])


# ---------------------------------------------------------------------------- 13. integer width downcast fix
_WIDE = re.compile(r"(?i)\b(bigint|biginteger|bigautofield|bigserial|int8|int64)\b|BigIntegerField|BigAutoField")
_NARROW = re.compile(r"(?i)\b(int|integer|serial|smallint|int4|int32|mediumint|autofield|integerfield|smallinteger)\b")
_OVERFLOW = re.compile(r"(?i)integer\s+overflow|out\s+of\s+range|\bint4\b|2147483647|\boverflow|exceeds?\s+(the\s+)?(max|int)|"
                       r"32[- ]bit|wrap\s*around|bigint|big\s*int")
_LOOKUP = re.compile(r"(?i)lookup|_types?$|statuses|status$|roles?$|countr|currenc|enum|choice|_kinds?$")


@candidate("integer_width_downcast_fix")
def integer_width_downcast_fix(ctx: Ctx):
    """Id columns widened to bigint (ALTER ... TYPE bigint, AlterField(BigAutoField), alter_column(BigInteger)), or a
    fix commit with an overflow message adding bigint DDL. Siblings = ``<table>_id`` columns declared with a narrower
    type in migration/SQL lines and never later widened. Lookup tables are dropped; a widening with neither siblings
    nor overflow wording is dropped as having no inbound references."""
    events = _mig_events(ctx)
    wide_later: dict[str, int] = {}
    for e in events:
        if e["op"] in ("alter", "add") and e["col"] and _WIDE.search(e.get("ftype", "") + " " + e["text"]):
            wide_later[e["col"]] = max(wide_later.get(e["col"], 0), e["seq"])
    decls: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e["op"] in ("add", "alter") and e["col"] and e["col"].endswith("_id") and \
                _NARROW.search(e.get("ftype", "")) and not _WIDE.search(e.get("ftype", "")):
            decls[e["col"]].append(e)
    reported = set()
    for e in events:
        if e["op"] != "alter" or not e["col"] or not (e["col"] == "id" or e["col"].endswith("_id")):
            continue
        if not _WIDE.search(e.get("ftype", "") + " " + e["text"]):
            continue
        t = e["table"]
        if _LOOKUP.search(t):
            ctx.drop("lookup_table")
            continue
        c = ctx.by_hash[e["hash"]]
        base = t[:-1] if t.endswith("s") else t
        sib_cols = {f"{t}_id", f"{base}_id"} if e["col"] == "id" else {e["col"]}
        sibs = [d for col in sib_cols for d in decls.get(col, [])
                if d["seq"] < e["seq"] and d["table"] != t and wide_later.get(col, 0) <= d["seq"]]
        overflow = bool(_OVERFLOW.search(c["message"] or "")) and ctx.is_fix(c)
        if not sibs and not overflow:
            ctx.drop("no_inbound_refs")
            continue
        reported.add(e["hash"])
        yield Hit(
            subject=f"{e['hash']}|{t}.{e['col']}",
            score=3 + 3 * overflow + min(len(sibs), 6) * 1.5,
            title=f"{t}.{e['col']} widened to bigint; {len(sibs)} narrower sibling column(s) remain",
            evidence={"table": t, "column": e["col"], "overflow_message": overflow,
                      "narrow_siblings": [{"table": d["table"], "column": d["col"], "path": d["path"]} for d in sibs[:6]]},
            commits=[e["hash"], *[d["hash"] for d in sibs[:2]]],
            diffs=[(e["hash"], e["path"]), *[(d["hash"], d["path"]) for d in sibs[:2]]], paths=[e["path"]])
    for c in ctx.commits:
        if c["hash"] in reported or c["is_merge"] or not ctx.is_fix(c) or \
                not re.search(r"(?i)integer\s+overflow|out\s+of\s+range|2147483647|int4", c["message"] or ""):
            continue
        rows = [r for r in ctx.lines_of(c["hash"]) if r["sign"] == "+" and _WIDE.search(r["text"]) and not is_test(r["path"])]
        if rows:
            yield Hit(subject=f"{c['hash']}|overflow", score=4, title=subject_of(c["message"])[:110],
                      evidence={"widening_lines": [r["text"].strip()[:100] for r in rows[:3]]},
                      commits=[c["hash"]], diffs=[(c["hash"], rows[0]["path"])],
                      paths=sorted({r["path"] for r in rows})[:4])


# ---------------------------------------------------------------------------- 14. float money / rounding
_MONEY_LINE = re.compile(rf"(?i)\b\w*{MONEY}\w*\b")
_NUM_TOK = re.compile(r"(?i)\b(Float(?:Field)?|Double|float|double|REAL|Decimal(?:Field)?|NUMERIC\(\d+\s*,\s*\d+\)|"
                      r"Numeric\(\d+\s*,\s*\d+\)|BigDecimal|MONEY|ROUND_(?:HALF_\w+|UP|DOWN|CEILING|FLOOR)|"
                      r"RoundingMode\.\w+|setScale|DECIMAL\(\d+\s*,\s*\d+\))(?!\w)")
_UI_PATH = re.compile(r"(?i)\.(tsx|jsx|vue|svelte|html|erb|hbs)$|(^|/)(components?|views?|templates?|ui|frontend|web)/|format")
_PENNY_MSG = re.compile(r"(?i)\b(penny|pennies|cents?|rounding|round(ed|s)?\s+(up|down|half)|reconcil\w*)\b")


@candidate("float_money_or_rounding_change")
def float_money_or_rounding_change(ctx: Ctx):
    """Money-named identifiers whose numeric type or rounding mode changed (Float->Decimal, NUMERIC(p,s),
    ROUND_HALF_*, RoundingMode, setScale): removed/added lines of one file+commit carrying a money word and a
    type/rounding token whose token sets differ; or fix commits whose message mentions penny/cent/rounding/reconcil
    and that touch code. Plain round()/toFixed are not tokens. UI/format paths and non-money floats are dropped."""
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(_NUM_TOK.pattern, None, limit=60000):
        if _MONEY_LINE.search(r["text"]):
            by_file[(r["hash"], r["path"])][r["sign"]].append(r)
    seen_commits = set()
    for (h, path), d in by_file.items():
        if is_test(path) or is_generated(path) or is_vendored(path) or is_docs(path):
            continue
        if _UI_PATH.search(path) and not path.endswith((".py", ".java", ".go", ".rb", ".kt")):
            ctx.drop("ui_formatting")
            continue
        tm = {t.lower() for r in d["-"] for t in _NUM_TOK.findall(r["text"])}
        tp = {t.lower() for r in d["+"] for t in _NUM_TOK.findall(r["text"])}
        if tm == tp:
            ctx.drop("no_type_change")
            continue
        c = ctx.by_hash[h]
        seen_commits.add(h)
        msg_match = bool(_PENNY_MSG.search(c["message"] or ""))
        yield Hit(
            subject=f"{h}|{path}",
            score=3 + 3 * msg_match + 2 * bool(tm and tp) + (1 if ctx.is_fix(c) else 0),
            title=f"money type/rounding {sorted(tm) or '-'} -> {sorted(tp) or '-'} in {path.rsplit('/', 1)[-1]}",
            evidence={"removed_tokens": sorted(tm), "added_tokens": sorted(tp), "message_match": msg_match,
                      "line": ((d["+"] or d["-"])[0]["text"]).strip()[:140]},
            commits=[h], diffs=[(h, path)], paths=[path])
    for c in ctx.commits:
        if c["hash"] in seen_commits or c["is_merge"] or ctx.is_bot(c) or not ctx.is_fix(c) or \
                not _PENNY_MSG.search(c["message"] or ""):
            continue
        code = [fc["path"] for fc in ctx.changes(c["hash"]) if is_code(fc["path"]) and not is_test(fc["path"])
                and not _UI_PATH.search(fc["path"])]
        if not code:
            ctx.drop("ui_formatting")
            continue
        yield Hit(subject=f"{c['hash']}|message", score=3, title=subject_of(c["message"])[:110],
                  evidence={"message_match": True, "paths": code[:5]},
                  commits=[c["hash"]], diffs=[(c["hash"], p) for p in code[:2]], paths=code[:5])


# ---------------------------------------------------------------------------- 15. signing key / session format
_SIGN_SETTING = re.compile(r"\b(SECRET_KEY_FALLBACKS|SECRET_KEY|SESSION_SERIALIZER|SESSION_ENGINE|SESSION_COOKIE_SALT|"
                           r"JWT_ALGORITHM|JWT_ALGORITHMS|JWT_ISSUER|JWT_AUDIENCE|JWT_SECRET(?:_KEY)?|JWT_AUTH|"
                           r"SIGNING_KEY|SIMPLE_JWT)\b\s*[:=]")
_JWT_CALL = re.compile(r"(?i)jwt\.(encode|decode|sign|verify)|jose\.|create_access_token|encode_token|decode_token|"
                       r"URLSafeTimedSerializer|TimestampSigner|signing\.dumps")
_ENV_SRC = re.compile(r"environ|getenv|env\(|config\(|process\.env")


@candidate("signing_key_session_format_change")
def signing_key_session_format_change(ctx: Ctx):
    """Changes to SECRET_KEY(_FALLBACKS), SESSION_SERIALIZER/ENGINE, JWT-prefixed algorithm/issuer/audience settings,
    an explicit algorithm= kwarg in a jwt call, or claim keys renamed within a file that signs/verifies tokens.
    Values are never emitted (setting names and booleans only). Tests/examples and environment-sourced SECRET_KEY
    edits are dropped; generic salt/aud/iss are not matched."""
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(r"SECRET_KEY|SESSION_SERIALIZER|SESSION_ENGINE|SESSION_COOKIE_SALT|JWT_|SIGNING_KEY|SIMPLE_JWT|"
                      r"algorithms?\s*=|jwt\.|URLSafeTimedSerializer|TimestampSigner|[\"']\w+[\"']\s*:", None, limit=80000):
        by_file[(r["hash"], r["path"])][r["sign"]].append(r)
    kp = re.compile(r"""["'](\w+)["']\s*:""")
    for (h, path), d in by_file.items():
        both = d["+"] + d["-"]
        if is_test(path) or is_docs(path) or is_vendored(path) or re.search(r"(?i)test|example|sample|\.lock$", path):
            if any(_SIGN_SETTING.search(x["text"]) for x in both):
                ctx.drop("tests_or_examples")
            continue
        fcs = next((x for x in ctx.changes(h) if x["path"] == path), None)
        if fcs is not None and fcs["status"] == "A":
            ctx.drop("initial_creation")
            continue
        names, claim = set(), []
        for x in d["+"] + d["-"]:
            m = _SIGN_SETTING.search(x["text"])
            if m:
                names.add(m.group(1))
            elif re.search(r"(?i)algorithms?\s*=", x["text"]) and _JWT_CALL.search(x["text"]):
                names.add("jwt algorithm")
        if "SECRET_KEY" in names and any(_ENV_SRC.search(x["text"]) for x in both
                                         if re.match(r"\s*SECRET_KEY\s*[:=]", x["text"])):
            names.discard("SECRET_KEY")
            ctx.drop("env_source_only")
        if any(_JWT_CALL.search(x["text"]) for x in both):
            old = {k for x in d["-"] for k in kp.findall(x["text"])}
            new = {k for x in d["+"] for k in kp.findall(x["text"])}
            claim = sorted(old ^ new) if old and new else []
        if not names and not claim:
            continue
        c = ctx.by_hash[h]
        yield Hit(
            subject=f"{h}|{path}",
            score=3 + 3 * bool(names) + 2 * bool(claim) + (2 if names & {"SECRET_KEY", "SESSION_SERIALIZER", "SESSION_ENGINE"} else 0),
            title=f"{', '.join(sorted(names)) or 'claim keys'} changed in {path.rsplit('/', 1)[-1]}: {subject_of(c['message'])[:60]}",
            evidence={"settings_changed": sorted(names), "claim_keys_changed": claim[:8], "values": "withheld"},
            commits=[h], diffs=[(h, path)], paths=[path])


# ---------------------------------------------------------------------------- 16. soft delete sibling drift
_SOFT_PRED = re.compile(r"(?i)deleted_at\s*(__isnull\s*=\s*True|\s+IS\s+NULL|\s*=\s*None|\s*:\s*nil|\.is_\(None\))|"
                        r"\bis_deleted\s*(=|==|:)\s*(False|false|0)|\bdeleted\s*(=|==)\s*(False|false|0)|"
                        r"not_deleted\(|where\(deleted_at:\s*nil|isDeleted\s*(=|:)\s*false|WHERE\s+deleted")
_QUERY_SITE = re.compile(r"(\w+)\.(?:objects|query)\b|\bFROM\s+(\w+)|(\w+)\.(?:find|where|filter)\(|"
                         r"(?:select|query|session\.query)\(\s*(\w+)\s*\)")
_SOFT_SKIP = re.compile(r"(?i)admin|audit|restore|undelete|trash|archiv|purge|with_deleted|all_objects|unscoped")
_DEFAULT_SCOPE = re.compile(r"(?i)default_scope|SoftDelete|paranoid|@Where\(|@SQLRestriction|acts_as_paranoid")


@candidate("soft_delete_filter_sibling_drift")
def soft_delete_filter_sibling_drift(ctx: Ctx):
    """A commit adds a soft-delete predicate (deleted_at IS NULL, is_deleted=False, ...) to an existing query (proxy:
    a modified file where the commit also removed lines, the added line being a query site). The model comes from the
    query line (``Model.objects``, ``FROM table``, ``query(Model)``). Sibling evidence: other query sites naming the
    same model in added lines of the same file extension without a predicate; needs >=5 sites. Each sibling site is attributed to its enclosing function
    (``diff_lines.symbol``): sites whose function no longer exists or no longer holds that line at HEAD are dropped
    (``site_gone_at_head``) and so are sites whose function filters soft-deleted rows on another line
    (``predicate_elsewhere_in_function``). Module-level sites keep the line-only test. Models with a
    default-scope/manager marker and admin/audit/restore paths are dropped."""
    adds = ctx.grep(_SOFT_PRED.pattern, "+", limit=20000)
    scoped_txt = " ".join(r["text"].lower() for r in ctx.grep(_DEFAULT_SCOPE.pattern, "+", limit=5000))
    sites_cache: dict[tuple, list] = {}
    done = set()
    for r in adds:
        path = r["path"]
        if not is_code(path) or is_test(path):
            continue
        if _SOFT_SKIP.search(path) or _SOFT_SKIP.search(r["text"]):
            ctx.drop("admin_audit_restore")
            continue
        fc = next((x for x in ctx.changes(r["hash"]) if x["path"] == path), None)
        if fc is None or fc["status"] == "A" or not fc["deleted"]:
            ctx.drop("new_query_not_a_change")
            continue
        model = None
        for x in [r, *[y for y in ctx.lines_of(r["hash"], path) if y["line"] and abs(y["line"] - r["line"]) <= 6]]:
            mm = _QUERY_SITE.search(x["text"])
            model = next((g for g in (mm.groups() if mm else ()) if g), None)
            if model and len(model) >= 3:
                break
            model = None
        if not model:
            ctx.drop("model_unknown")
            continue
        if model.lower() in scoped_txt:
            ctx.drop("default_scope_model")
            continue
        if (r["hash"], model) in done:
            continue
        done.add((r["hash"], model))
        ext = path.rsplit(".", 1)[-1]
        if (model, ext) not in sites_cache:
            rows = ctx.q("SELECT hash, path, line, text, symbol FROM diff_lines WHERE sign='+' AND instr(text, ?) > 0 "
                         "AND path LIKE ?", (model, f"%.{ext}"))
            sites_cache[(model, ext)] = [x for x in rows if _QUERY_SITE.search(x["text"]) and not is_test(x["path"])]
        sites = sites_cache[(model, ext)]
        distinct = {(x["path"], x["text"].strip()) for x in sites}
        without = []
        for x in sites:
            if _SOFT_PRED.search(x["text"]) or x["hash"] == r["hash"] or _SOFT_SKIP.search(x["path"]):
                continue
            if x["symbol"]:
                host = ctx.symbols.get(x["symbol"])
                if host is None or not host["alive"] or x["text"].strip() not in (host["body"] or ""):
                    ctx.drop("site_gone_at_head")
                    continue
                if _SOFT_PRED.search(host["body"] or ""):
                    ctx.drop("predicate_elsewhere_in_function")
                    continue
            without.append(x)
        if len(distinct) < 5:
            ctx.drop("fewer_than_5_sites")
            continue
        if not without:
            continue
        c = ctx.by_hash[r["hash"]]
        yield Hit(
            subject=f"{r['hash']}|{model}",
            score=2 + len(without) * 0.5 + 2 * ctx.is_fix(c),
            title=f"soft-delete filter added to a {model} query; {len(without)} sibling sites lack it",
            evidence={"model": model, "predicate_line": r["text"].strip()[:120],
                      "sibling_sites_without_predicate": len(without), "total_sites": len(distinct),
                      "examples": [{"path": x["path"], "hash": x["hash"][:10]} for x in without[:4]]},
            commits=[r["hash"]], diffs=[(r["hash"], path)], paths=[path, *sorted({x["path"] for x in without})[:3]])
