# Security hazards

> A set of line rules flags code patterns that commonly lead to vulnerabilities: shell and SQL injection, unsafe
> deserialisation, eval, disabled TLS, hard-coded secrets and more. They run over the whole codebase at ingest, and
> over the added lines of every diff.

## Why it matters

An AI assistant asked to "just make it work" can introduce the textbook vulnerabilities: `shell=True` with user input,
an f-string SQL query, `verify=False`. These are easy to spot by pattern, and expensive to miss.

## How a line rule works

Each rule is a **regular expression** (a text pattern), sometimes with a **context check** on neighbouring lines.
The scanner applies some common rules to every rule:
- It skips comment lines and docstrings, because examples in documentation aren't executable code.
- It grades matches in test files and documentation files as **info**.
- Some rules have a stricter severity when they appear in a new diff than in existing code (`diff_severity`).

## The rules

| Rule | Catches | Severity |
|---|---|---|
| `sec.shell_true` | `subprocess.*(…, shell=True)` | **block** |
| `sec.os_system` | `os.system(…)`, `os.popen(…)` | warn |
| `sec.eval` | `eval(` / `exec(` of dynamic code | warn (**block** in a new diff) |
| `sec.deserialize` | `pickle` / `marshal` / `shelve` `.load(s)` | warn |
| `sec.yaml_load` | `yaml.load` without `SafeLoader` | **block** |
| `sec.sql_format` | SQL built with f-strings, `%` or `.format()` | **block** |
| `sec.tls_off` | `verify=False`, `CERT_NONE`, unverified SSL context | warn |
| `sec.autoescape_off` | `autoescape=False` | warn |
| `sec.markup_format` | `Markup(f"…")`, which marks formatted text as safe HTML | warn |
| `sec.weak_hash` | `hashlib.md5` / `sha1` | info |
| `sec.secret` | Hard-coded passwords, API keys, tokens and private keys (8+ character literal values) | warn |
| `sec.mktemp` | `tempfile.mktemp` (racy) | warn |
| `sec.debug_run` | `app.run(debug=True)` | warn |
| `sec.path_from_request` | File paths built from request data | warn |
| `sec.ssrf` | Outgoing requests to URLs taken from request data | warn |
| `sec.jwt_none` | JWT `algorithms=["none"]`, signature checking disabled | **block** |
| `sec.js_eval` | `eval(` / `new Function(` (JS/TS) | **block** |
| `sec.js_html` | `innerHTML =`, `dangerouslySetInnerHTML`, `document.write` | warn |
| `sec.js_exec` | `child_process`, `exec(`, `spawn(…shell: true)` | warn |
| `sec.js_sql` | `.query(\`… ${x}\`)`, SQL in a template string | **block** |

## Example

The safety fixture plants three hazards:
- `subprocess.run(cmd, shell=True)` in `app/core.py`, which gives `sec.shell_true` (**block**)
- an f-string SQL query in `app/db.py`, which gives `sec.sql_format` (**block**)
- `el.innerHTML = …` in `web/client.ts`, which gives `sec.js_html` (warn)

## Where in the code

`afd_oracle_ingest/safety/rules.py`: `RULES` and `scan`. Code is read through `safety/source.py`, which knows each
file's language, comments and docstrings.

## Limits

These are patterns, not data-flow analysis. Oracle doesn't prove that input reaches a sink. It flags the sink, and
the severity reflects how rarely the pattern is safe. For deep analysis, pair Oracle with a dedicated security
scanner.

Next: [Races and shared state](07-races.md)
