"""Line rules: security sinks, race patterns, logic-bomb indicators, error handling and performance smells.

Each rule is a regex plus an optional context check over neighbouring lines. Rules run on every text file at HEAD
(baseline) and on the added lines of a diff (`check_diff`), where some are graded more strictly (`diff_severity`).
Severity: "block" (must be resolved or explicitly acknowledged), "warn" (review), "info" (context).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from afd_oracle_core.ids import short_hash

from .source import SourceFile, is_test_path

PY = {"python"}
JS = {"typescript", "tsx", "javascript"}
CODE = PY | JS


@dataclass
class Finding:
    category: str  # security | race | logic_bomb | error_handling | performance | supply_chain | contract | api | ...
    rule: str
    severity: str  # block | warn | info
    path: str
    line: int
    message: str
    symbol: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return "f:" + short_hash(self.rule, self.path, self.symbol or "", self.detail.get("text", ""), str(self.line))

    def as_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "rule": self.rule, "severity": self.severity,
                "path": self.path, "line": self.line, "symbol": self.symbol, "message": self.message,
                "detail": self.detail}


Context = Callable[[list[str], int, "re.Match"], bool]


@dataclass
class Rule:
    id: str
    category: str
    severity: str
    pattern: str
    message: str
    langs: set[str] | None = None  # None = any text file
    context: Context | None = None
    diff_severity: str | None = None  # stricter grade when the line is newly added by a change
    keep_in_tests: bool = False  # by default findings in test files are downgraded to info
    flags: int = 0
    rx: re.Pattern = field(init=False)

    def __post_init__(self) -> None:
        self.rx = re.compile(self.pattern, self.flags)


# ---------------------------------------------------------------------------- context helpers
def _indent(s: str) -> int:
    return len(s) - len(s.lstrip())


def _window(lines: list[str], i: int, after: int) -> list[str]:
    return lines[i + 1: i + 1 + after]


def _in_loop(lines: list[str], i: int, _m=None) -> bool:
    ind = _indent(lines[i])
    for j in range(i - 1, max(-1, i - 12), -1):
        s = lines[j]
        if not s.strip():
            continue
        if _indent(s) < ind and re.match(r"\s*(for|while)\b|\s*(for|while)\s*\(|.*\.(forEach|map)\(", s):
            return True
        if _indent(s) < ind and re.match(r"\s*(def|class|async def|function)\b", s):
            return False
    return False


def _swallowed(lines: list[str], i: int, _m=None) -> bool:
    for s in _window(lines, i, 3):
        t = s.strip()
        if not t or t.startswith("#"):
            continue
        return t in ("pass", "continue", "...", "return", "return None", "return False")
    return False


def _toctou(lines: list[str], i: int, m: "re.Match") -> bool:
    var = m.group(2)
    if not var:
        return False
    after = "\n".join(_window(lines, i, 4))
    return bool(re.search(rf"(open|os\.remove|os\.unlink|os\.rename|os\.makedirs|os\.mkdir|shutil\.\w+)\(\s*{re.escape(var)}\b",
                          after))


def _retry(lines: list[str], i: int, _m=None) -> bool:
    body = "\n".join(_window(lines, i, 8))
    return "except" in body and re.search(r"\bsleep\(", body) is not None


def _gate_condition(lines: list[str], i: int, _m=None) -> bool:
    return bool(re.match(r"\s*(if|elif|while)\b|.*\bif\s*\(|.*\?\s*$|.*\band\b|.*\bor\b", lines[i]))


def _not_literal_secret(lines: list[str], i: int, m: "re.Match") -> bool:
    v = m.group(3).lower()
    return not any(w in v for w in ("example", "change", "dummy", "test", "xxx", "placeholder", "your", "secret-key",
                                    "<", "{", "${", "%(", "redacted", "fake", "sample", "default", "development"))


def _sql_string(lines: list[str], i: int, _m=None) -> bool:
    return re.search(r"(?i)\b(select|insert|update|delete|create|drop|alter)\b", lines[i]) is not None


def _weak_hash_security(lines: list[str], i: int, _m=None) -> bool:
    return "usedforsecurity=False" not in lines[i]


RULES: list[Rule] = [
    # ---------------------------------------------------------------- security
    Rule("sec.shell_true", "security", "block", r"\bsubprocess\.\w+\(.*shell\s*=\s*True",
         "subprocess with shell=True: command injection if any argument is user-controlled", PY),
    Rule("sec.os_system", "security", "warn", r"\bos\.(system|popen)\(", "os.system/os.popen runs a shell", PY),
    Rule("sec.eval", "security", "warn", r"(?<![\w.])(eval|exec)\(", "eval/exec of dynamic code", PY,
         diff_severity="block"),
    Rule("sec.deserialize", "security", "warn", r"\b(pickle|cPickle|dill|marshal|shelve)\.(loads?|open)\(",
         "unsafe deserialisation: never load untrusted data", PY),
    Rule("sec.yaml_load", "security", "block", r"\byaml\.(load|load_all)\((?![^)]*Loader\s*=\s*(yaml\.)?C?SafeLoader)",
         "yaml.load without SafeLoader can construct arbitrary objects", PY),
    Rule("sec.sql_format", "security", "block",
         r"\.(execute|executemany|executescript|raw|text)\(\s*(f[\"']|[\"'][^\"']*[\"']\s*(%|\+|\.format\())",
         "SQL built by string formatting: use parameters", PY, context=_sql_string),
    Rule("sec.tls_off", "security", "warn", r"\bverify\s*=\s*False\b|ssl\._create_unverified_context|CERT_NONE",
         "TLS certificate verification disabled", PY),
    Rule("sec.autoescape_off", "security", "warn", r"autoescape\s*=\s*False", "template autoescaping disabled", PY),
    Rule("sec.markup_format", "security", "warn", r"\bMarkup\(\s*(f[\"']|[^)]*%|[^)]*\.format\()",
         "Markup() around formatted text bypasses escaping (XSS)", PY),
    Rule("sec.weak_hash", "security", "info", r"\bhashlib\.(md5|sha1)\(", "weak hash (fine for checksums, not for "
         "passwords/signatures)", PY, context=_weak_hash_security),
    Rule("sec.secret", "security", "warn",
         r"(?i)\b(secret|password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\w*\s*[:=]\s*([rbuf]?)[\"']([^\"'\s]{8,})[\"']",
         "hard-coded credential", context=_not_literal_secret),
    Rule("sec.mktemp", "security", "warn", r"\btempfile\.mktemp\(", "tempfile.mktemp is racy: use mkstemp", PY),
    Rule("sec.debug_run", "security", "warn", r"\.run\([^)]*debug\s*=\s*True", "app run with debug=True", PY),
    Rule("sec.path_from_request", "security", "warn",
         r"\b(open|send_file|os\.path\.join|Path)\([^)]*\brequest\.(args|form|values|json|files|view_args)",
         "file path built from request data (path traversal)", PY),
    Rule("sec.ssrf", "security", "warn",
         r"\b(requests|httpx|urllib\.request)\.\w+\([^)]*\brequest\.(args|form|values|json)",
         "outbound request to a user-supplied URL (SSRF)", PY),
    Rule("sec.jwt_none", "security", "block", r"(?i)algorithms\s*=\s*\[?\s*[\"']none[\"']|verify_signature[\"']?\s*:\s*False",
         "JWT signature verification disabled", PY),
    Rule("sec.js_eval", "security", "block", r"(?<![\w.])eval\(|\bnew Function\(", "eval/new Function of dynamic code", JS),
    Rule("sec.js_html", "security", "warn", r"\.innerHTML\s*=|\.outerHTML\s*=|dangerouslySetInnerHTML|document\.write\(",
         "raw HTML injection sink (XSS)", JS),
    Rule("sec.js_exec", "security", "warn", r"\bchild_process\b|\bexecSync?\(|\bspawn\([^)]*shell\s*:\s*true",
         "process execution", JS),
    Rule("sec.js_sql", "security", "block", r"\.(query|execute|raw)\(\s*`[^`]*\$\{", "SQL built from a template string", JS),
    # ---------------------------------------------------------------- race (line-level; structural rules live in concurrency.py)
    Rule("race.mutable_default", "race", "warn",
         r"^\s*(async\s+)?def\s+\w+\(.*=\s*(\[\]|\{\}|set\(\)|dict\(\)|list\(\))",
         "mutable default argument is shared between calls", PY),
    Rule("race.global_stmt", "race", "warn", r"^\s+global\s+\w+", "function rebinds a module global (shared state)", PY),
    Rule("race.toctou", "race", "warn", r"\bos\.path\.(exists|isfile|isdir|lexists)\(\s*(\w+)",
         "check-then-act on the filesystem (TOCTOU race)", PY, context=_toctou),
    Rule("race.entry_point", "race", "info",
         r"\bthreading\.Thread\(|\bThreadPoolExecutor\(|\bProcessPoolExecutor\(|\.submit\(|\basyncio\.(create_task|gather|ensure_future)\(|\brun_in_executor\(|new Worker\(",
         "concurrency entry point: code reachable from here may run in parallel", CODE),
    # ---------------------------------------------------------------- logic bombs / suspicious code
    Rule("lb.date_gate", "logic_bomb", "warn",
         r"\b(datetime|date)(\.datetime)?\(\s*(19|20)\d\d\s*,|(datetime\.(now|utcnow|today)|date\.today|time\.time)\(\)\s*[<>]=?|Date\.now\(\)\s*[<>]=?|new Date\(\)\s*[<>]=?|getFullYear\(\)\s*[<>=]",
         "behaviour gated on a date/time", CODE, context=_gate_condition, diff_severity="block", keep_in_tests=False),
    Rule("lb.host_gate", "logic_bomb", "warn",
         r"(socket\.gethostname|getpass\.getuser|os\.getlogin|platform\.node|os\.uname)\(\)|os\.environ\.get\([\"'](USER|USERNAME|HOSTNAME|COMPUTERNAME|LOGNAME)[\"']|os\.hostname\(\)|userInfo\(\)",
         "behaviour gated on host or user identity", CODE, context=_gate_condition, diff_severity="block"),
    Rule("lb.encoded_exec", "logic_bomb", "block",
         r"(exec|eval|compile|__import__|Function)\(.*(b64decode|b32decode|fromhex|unhexlify|codecs\.decode|zlib\.decompress|marshal\.loads|atob\(|Buffer\.from\()",
         "executes decoded/obfuscated content", CODE, keep_in_tests=True),
    Rule("lb.blob", "logic_bomb", "info", r"[\"'`]([A-Za-z0-9+/]{160,}={0,2}|[0-9a-fA-F]{160,})[\"'`]",
         "long encoded literal", CODE, diff_severity="warn"),
    Rule("lb.dynamic_import", "logic_bomb", "info", r"\b(__import__|importlib\.import_module)\(\s*(?![\"'])",
         "import of a computed module name", PY, diff_severity="warn"),
    Rule("lb.disabled_check", "logic_bomb", "info", r"^\s*if\s+(False|0)\s*:|#\s*nosec\b|//\s*eslint-disable(?!-next-line\s+\S)|@ts-ignore",
         "check disabled or suppressed", CODE, diff_severity="warn", keep_in_tests=True),
    Rule("lb.test_skipped", "logic_bomb", "info", r"@pytest\.mark\.(skip|xfail)\b|\bunittest\.skip\b|\b(it|test|describe)\.skip\(|\bxit\(",
         "test skipped", CODE, diff_severity="warn", keep_in_tests=True),
    Rule("lb.destructive", "logic_bomb", "info",
         r"\bshutil\.rmtree\(|\bos\.(remove|unlink|rmdir)\(|(?i:\bdrop\s+(table|database)\b|\btruncate\s+table\b)|\brm\s+-rf\b|\bfs\.(rmSync|rm|unlinkSync)\(",
         "destructive operation", None, diff_severity="warn"),
    Rule("lb.network", "logic_bomb", "info",
         r"\bsocket\.socket\(|\burllib\.request\.urlopen\(|\brequests\.(get|post|put)\(|\bhttp\.client\.|\bfetch\(\s*[\"'`]https?://|\bXMLHttpRequest\(",
         "outbound network call", CODE, diff_severity="warn"),
    # ---------------------------------------------------------------- error handling
    Rule("err.bare_except", "error_handling", "warn", r"^\s*except\s*:", "bare except also catches KeyboardInterrupt/SystemExit", PY),
    Rule("err.swallow", "error_handling", "warn", r"^\s*except\s+\(?\s*(Exception|BaseException)\b[^:]*:\s*$",
         "broad except that swallows the error", PY, context=_swallowed),
    Rule("err.js_swallow", "error_handling", "warn", r"catch\s*(\(\s*\w*\s*\))?\s*\{\s*\}", "empty catch block", JS),
    Rule("err.retry", "error_handling", "info", r"^\s*(for\s+\w+\s+in\s+range\(|while\s+True\s*:)",
         "retry loop: make sure the retried operation is idempotent", PY, context=_retry),
    Rule("err.open_no_with", "error_handling", "info", r"=\s*open\([^)]*\)\s*$|(?<!with )\bopen\([^)]*\)\.(read|write)",
         "file opened without a context manager", PY),
    # ---------------------------------------------------------------- performance
    Rule("perf.io_in_loop", "performance", "info",
         r"\.(execute|query|filter|get|all|first)\(|\brequests\.\w+\(|\bopen\(|\bsession\.query\(|\bawait\s+fetch\(",
         "I/O or query inside a loop", CODE, context=lambda ls, i, m: _in_loop(ls, i)),
]

_COMMENT = {"python": "#", "typescript": "//", "tsx": "//", "javascript": "//"}


def _is_comment(line: str, lang: str | None) -> bool:
    t = line.lstrip()
    mark = _COMMENT.get(lang or "")
    return bool(mark and t.startswith(mark)) or t.startswith(("*", "/*"))


def scan(src: SourceFile, repo_id: str, only_lines: set[int] | None = None, diff_mode: bool = False,
         rules: list[Rule] | None = None) -> list[Finding]:
    """Apply line rules to one file. `only_lines` restricts to those 1-based lines (added lines of a diff)."""
    out: list[Finding] = []
    test = is_test_path(src.path)
    lines = src.lines
    doc = src.docstring_lines()
    for rule in rules or RULES:
        if rule.langs is not None and src.lang not in rule.langs:
            continue
        if test and rule.category == "performance":  # test-loop I/O is not a production performance hazard
            continue
        for i, line in enumerate(lines):
            n = i + 1
            if only_lines is not None and n not in only_lines:
                continue
            if len(line) > 2000 or (_is_comment(line, src.lang) and rule.category != "logic_bomb"):
                continue
            if n in doc and rule.category != "logic_bomb":  # docstring examples are not executable code
                continue
            m = rule.rx.search(line)
            if not m or (rule.context and not rule.context(lines, i, m)):
                continue
            sev = (rule.diff_severity or rule.severity) if diff_mode else rule.severity
            if (test and not rule.keep_in_tests and rule.category != "logic_bomb") or src.is_doc_file:
                sev = "info"  # tests and documentation examples are context, not hazards
            out.append(Finding(rule.category, rule.id, sev, src.path, n, rule.message, src.uid_at(repo_id, n),
                               {"text": line.strip()[:200]}))
    return out
