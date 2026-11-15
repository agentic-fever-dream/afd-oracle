"""Read-only Cypher guard (defence in depth on top of READ access-mode transactions)."""
from __future__ import annotations

import re

_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`")
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
# (?<![.\w$]) keeps property names like n.set / r.delete from tripping the clause check.
_WRITE = re.compile(
    r"(?<![.\w$])(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|FOREACH|LOAD\s+CSV|GRANT|DENY|REVOKE)\b"
    r"|\bIN\s+TRANSACTIONS\b"
    r"|\bCALL\s+(dbms|db\.(create|index\.fulltext\.create|clearQueryCaches)|gds|apoc\.(create|merge|refactor|periodic"
    r"|load|import|export|cypher\.(run|do)|do|trigger|nodes\.(delete|link)|schema|atomic|lock|refactor|systemdb))",
    re.I,
)
MAX_QUERY_CHARS = 8000


class CypherRejected(ValueError):
    pass


def check_read_only(query: str) -> None:
    if not query or not query.strip():
        raise CypherRejected("empty query")
    if len(query) > MAX_QUERY_CHARS:
        raise CypherRejected(f"query longer than {MAX_QUERY_CHARS} characters")
    stripped = _COMMENTS.sub(" ", _STRINGS.sub("''", query))
    m = _WRITE.search(stripped)
    if m:
        raise CypherRejected(f"write or admin clause '{m.group(0).strip()}' is not allowed; run_cypher is read-only. "
                             "Use upsert_insight / set_insight_status to record findings.")
    if re.search(r"\bUSE\s+system\b", stripped, re.I):
        raise CypherRejected("the system database is not accessible")
