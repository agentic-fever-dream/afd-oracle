"""``.oracleignore`` matching (glob patterns; patterns without '/' match the basename anywhere)."""
from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

DEFAULT_PATTERNS = ["*.lock", "package-lock.json", "*.min.js", "node_modules/**", "vendor/**", "dist/**"]


class IgnoreRules:
    def __init__(self, patterns: list[str]):
        self.patterns = [p.strip() for p in patterns if p.strip() and not p.strip().startswith("#")]

    @classmethod
    def load(cls, *candidates: Path) -> "IgnoreRules":
        for c in candidates:
            if c and Path(c).is_file():
                return cls(Path(c).read_text().splitlines())
        return cls(DEFAULT_PATTERNS)

    def match(self, path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        for p in self.patterns:
            if "/" not in p:
                if fnmatchcase(base, p):
                    return True
                continue
            pat = p.replace("**/", "*/").replace("/**", "/*")
            if fnmatchcase(path, pat) or fnmatchcase(path, p.replace("**/", "")) or \
                    fnmatchcase("/" + path, "*/" + pat.lstrip("*/")):
                return True
        return False
