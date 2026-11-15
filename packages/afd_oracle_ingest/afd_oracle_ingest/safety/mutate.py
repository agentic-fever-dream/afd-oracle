"""Targeted mutation testing: would the existing tests catch a regression in this function?

Applies small semantic mutations (flipped comparisons, negated conditions, swapped booleans, off-by-one, dropped
raises, `return None`) to one function at a time, runs the selected tests with the project's own Python after each,
and restores the file. A mutant that every test survives marks behaviour no test pins down. On demand only.
"""
from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..ast.extractor import language_for, parse_source

SWAPS = [
    (r"(?<![=!<>])==(?!=)", "!=", "== -> !="),
    (r"!=", "==", "!= -> =="),
    (r"<=", ">", "<= -> >"),
    (r">=", "<", ">= -> <"),
    (r"(?<![<>=-])<(?![<=])", ">=", "< -> >="),
    (r"(?<![<>=-])>(?![>=])", "<=", "> -> <="),
    (r"\bis not None\b", "is None", "is not None -> is None"),
    (r"\bis None\b", "is not None", "is None -> is not None"),
    (r"\band\b", "or", "and -> or"),
    (r"\bor\b", "and", "or -> and"),
    (r"\bTrue\b", "False", "True -> False"),
    (r"\bFalse\b", "True", "False -> True"),
    (r"\+ 1\b", "- 1", "+ 1 -> - 1"),
    (r"- 1\b", "+ 1", "- 1 -> + 1"),
]


@dataclass
class Mutant:
    line: int
    original: str
    mutated: str
    op: str


def _code_part(line: str) -> tuple[str, str]:
    """Split off a trailing comment (naive, but string-aware enough for one-liners)."""
    in_str = None
    for i, ch in enumerate(line):
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == "#":
            return line[:i], line[i:]
    return line, ""


def _string_block_lines(lines: list[str], start: int, end: int) -> set[int]:
    """Line numbers inside triple-quoted strings (docstrings, multi-line text): prose, not code, so never mutated."""
    inside, quote, out = False, "", set()
    for n in range(start, min(end, len(lines)) + 1):
        text = lines[n - 1]
        i = 0
        line_in = inside
        while i < len(text):
            if not inside:
                m = re.search(r'("""|\'\'\')', text[i:])
                if not m:
                    break
                inside, quote, line_in = True, m.group(1), True
                i += m.end()
            else:
                j = text.find(quote, i)
                if j < 0:
                    break
                inside = False
                i = j + 3
        if line_in or inside:
            out.add(n)
    return out


def mutants(lines: list[str], start: int, end: int, limit: int = 20) -> list[Mutant]:
    out: list[Mutant] = []
    prose = _string_block_lines(lines, start, end)
    for n in range(start + 1, min(end, len(lines)) + 1):  # skip the def line itself
        if n in prose:
            continue
        raw = lines[n - 1]
        code, comment = _code_part(raw)
        stripped = code.strip()
        if not stripped or stripped.startswith(("\"\"\"", "'''", "@", "def ", "class ", "import ", "from ")):
            continue
        if stripped.count('"') % 2 or stripped.count("'") % 2:
            continue
        for rx, rep, op in SWAPS:
            for m in re.finditer(rx, code):
                if code[:m.start()].count('"') % 2 or code[:m.start()].count("'") % 2:
                    continue  # inside a string literal
                out.append(Mutant(n, raw, code[:m.start()] + rep + code[m.end():] + comment, op))
                break
        m = re.match(r"^(\s*)(if|elif|while)\s+(.+):\s*$", code)
        if m:
            out.append(Mutant(n, raw, f"{m.group(1)}{m.group(2)} not ({m.group(3)}):{comment}", "negate condition"))
        m = re.match(r"^(\s*)return\s+(?!None\b)(.+)$", code)
        if m:
            out.append(Mutant(n, raw, f"{m.group(1)}return None{comment}", "return None"))
        m = re.match(r"^(\s*)raise\b", code)
        if m:
            out.append(Mutant(n, raw, f"{m.group(1)}pass  # mutant: raise removed", "drop raise"))
    # spread across the function rather than taking the first N on early lines
    if len(out) > limit:
        step = len(out) / limit
        out = [out[int(i * step)] for i in range(limit)]
    return out


def find_function(path: Path, qualname: str) -> tuple[int, int]:
    lang = language_for(path.name)
    fp = parse_source(lang, path.read_bytes()) if lang else None
    for s in fp.symbols if fp else []:
        if s.qualname == qualname and s.kind == "function":
            return s.start_line, s.end_line
    raise LookupError(f"function {qualname} not found in {path}")


def run(cwd: Path, target: str, python: str | list[str] | None = None, tests: list[str] | None = None,
        limit: int = 15, timeout: float = 300.0, log=print) -> dict:
    """target: 'path/to/file.py::Qual.name' (path relative to cwd). ``python`` is an interpreter path or a command
    prefix (e.g. ``["docker", "exec", "-w", "/src", "<container>", "/venv/bin/python"]`` to run tests in a sandbox)."""
    rel, _, qual = target.partition("::")
    file = (cwd / rel).resolve()
    start, end = find_function(file, qual)
    original = file.read_text(encoding="utf-8")
    lines = original.splitlines()
    newline = "\r\n" if "\r\n" in original else "\n"
    py = list(python) if isinstance(python, list) else [python or sys.executable]
    cmd = [*py, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *(tests or [])]
    backup = file.with_suffix(file.suffix + ".afd-oracle-mutate.bak")
    shutil.copy2(file, backup)

    def restore() -> None:
        if backup.exists():
            shutil.copy2(backup, file)
            backup.unlink()
    atexit.register(restore)

    def run_tests() -> tuple[bool, float]:
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return r.returncode == 0, time.time() - t0
        except subprocess.TimeoutExpired:
            return False, time.time() - t0

    try:
        ok, base_t = run_tests()
        if not ok:
            return {"error": "the selected tests fail before any mutation; fix them first", "command": cmd}
        results = []
        for m in mutants(lines, start, end, limit):
            mutated = lines[:]
            mutated[m.line - 1] = m.mutated
            file.write_text(newline.join(mutated) + (newline if original.endswith(("\n", "\r\n")) else ""),
                            encoding="utf-8", newline="")  # newline="": keep the file's own line endings
            passed, dt = run_tests()
            results.append({"line": m.line, "op": m.op, "mutated": m.mutated.strip()[:160], "killed": not passed,
                            "seconds": round(dt, 1)})
            log(f"  {'killed  ' if not passed else 'SURVIVED'} line {m.line}: {m.op}")
            shutil.copy2(backup, file)  # byte-exact restore between mutants
    finally:
        restore()
    killed = sum(r["killed"] for r in results)
    return {"target": target, "tests": tests or ["(whole suite)"], "baseline_seconds": round(base_t, 1),
            "mutants": len(results), "killed": killed,
            "score": round(killed / len(results), 3) if results else None,
            "survived": [r for r in results if not r["killed"]],
            "verdict": ("no mutants generated" if not results else
                        "well tested" if killed == len(results) else
                        "weakly tested: surviving mutants show behaviour no test pins down")}
