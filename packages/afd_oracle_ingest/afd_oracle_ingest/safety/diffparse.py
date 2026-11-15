"""Unified diff parsing (git diff output) and patch application."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" " | "+" | "-", text)


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def status(self) -> str:
        if self.old_path is None:
            return "A"
        if self.new_path is None:
            return "D"
        return "R" if self.old_path != self.new_path else "M"

    def added(self) -> list[tuple[int, str]]:
        out = []
        for h in self.hunks:
            n = h.new_start
            for tag, text in h.lines:
                if tag == "+":
                    out.append((n, text))
                if tag in "+ ":
                    n += 1
        return out

    def removed(self) -> list[tuple[int, str]]:
        out = []
        for h in self.hunks:
            o = h.old_start
            for tag, text in h.lines:
                if tag == "-":
                    out.append((o, text))
                if tag in "- ":
                    o += 1
        return out


def _strip(p: str) -> str | None:
    p = p.strip().split("\t", 1)[0]
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r'diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$', raw)
            cur = FileDiff(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            hunk = None
            continue
        if cur is None and raw.startswith("--- "):
            cur = FileDiff(None, None)
            files.append(cur)
        if cur is None:
            continue
        if raw.startswith("--- ") and hunk is None:
            cur.old_path = _strip(raw[4:])
        elif raw.startswith("+++ ") and hunk is None:
            cur.new_path = _strip(raw[4:])
        elif raw.startswith("new file mode"):
            cur.old_path = None
        elif raw.startswith("deleted file mode"):
            cur.new_path = None
        elif raw.startswith("rename from "):
            cur.old_path = raw[len("rename from "):]
        elif raw.startswith("rename to "):
            cur.new_path = raw[len("rename to "):]
        elif raw.startswith("Binary files"):
            cur.binary = True
        else:
            m = HUNK.match(raw)
            if m:
                hunk = Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1))
                cur.hunks.append(hunk)
            elif hunk is not None and raw[:1] in (" ", "+", "-"):
                hunk.lines.append((raw[0], raw[1:]))
            elif hunk is not None and raw == "":
                hunk.lines.append((" ", ""))
    return files


def apply(old_text: str | None, fd: FileDiff) -> str:
    """New file content from old content + hunks (best effort; used when no working copy is available)."""
    if fd.status == "A" or old_text is None:
        return "\n".join(t for h in fd.hunks for tag, t in h.lines if tag in "+ ")
    old = old_text.splitlines()
    out, pos = [], 0
    for h in fd.hunks:
        start = max(0, h.old_start - 1)
        out.extend(old[pos:start])
        pos = start
        for tag, t in h.lines:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else t)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(t)
    out.extend(old[pos:])
    return "\n".join(out)
