# Error handling and performance

> Two smaller rule families. One catches errors that are silently swallowed. The other catches database or network
> calls made inside loops.

## Why it matters

- A swallowed exception turns a loud, easy-to-fix crash into silent wrong behaviour, which is much harder to debug.
- A query inside a loop (the "N+1 problem") works on test data and becomes very slow on real data.

## Error handling

| Rule | Catches | Severity |
|---|---|---|
| `err.bare_except` | `except:` with no type, which also catches Ctrl-C and `SystemExit` | warn |
| `err.swallow` | `except Exception:` whose body does nothing useful (`pass`, `...`) | warn |
| `err.js_swallow` | An empty `catch {}` | warn |
| `err.retry` | `for … in range(…)` / `while True:` loops that look like retry loops: check them for back-off and a limit | info |
| `err.open_no_with` | `open(…)` outside a `with` block, so the file may never be closed | info |

## Performance

| Rule | Catches | Severity |
|---|---|---|
| `perf.io_in_loop` | `.execute(`, `.query(`, `.filter(`, `requests.get(`, `open(`, `await fetch(` inside a `for` or `while` loop | info |

Performance rules are **skipped in test files**: I/O in a test loop isn't a production problem.

Functions with past performance fixes are **performance zones** (see [Bug-fix lineage](05-fix-lineage.md)). Changing
them gives `history.sensitive_zone` in `check_diff`.

## Where in the code

`afd_oracle_ingest/safety/rules.py`: the `err.*` and `perf.*` rules.

Next: [Risk score](10-risk-score.md)
