# The snapshot of today's code

> The snapshot looks at the tip of the default branch (HEAD) and records which symbols exist today, how files and
> symbols connect (imports, calls, inheritance), which commits are on the main line, and when each commit actually
> landed there.

## Why it matters

History describes the past; the snapshot describes the present. Questions like "who calls this?" or "what does this
file import?" are about today's code. Many later steps also need to know which commits are on the main line, because
work in side branches is weaker evidence.

## How it works

1. **Find the default branch.** Oracle uses `HEAD`'s target, falling back to `main` or `master`. It reads every
   non-ignored file at that commit and parses the source files (from the cache).
2. **Mark living symbols.** Every symbol present at HEAD gets `alive = 1` and its current lines, body and signature;
   everything else gets `alive = 0`.
3. **Build structural edges.** A resolver connects names to the symbols they refer to:
   - **IMPORTS** (file → file). Python dotted and relative imports, and JS/TS relative specifiers, are resolved to
     files in the repository. `type_only` imports are flagged.
   - **CALLS** (symbol → symbol). A call to `foo()` is resolved through the file's imports and definitions. A call to
     `self.bar()` is resolved through the class and its bases. Each edge carries a `confidence`.
   - **INHERITS** and **IMPLEMENTS** (class → class or interface).
4. **Mark the main line.** Every commit reachable from HEAD gets `on_main = 1`.
5. **Compute landing points.** For each commit, Oracle records which first-parent commit on the default branch brought
   it in, and when (`landed_in`, `landed_ts`).
   - A commit authored in 2019 but merged in 2021 *landed* in 2021.
   - Using author dates would make long-lived pull requests look as if they shipped years before they did.
6. **Build the files table.** For every path ever seen, Oracle records:
   - `alive` (whether it exists at HEAD)
   - `loc` (lines of code)
   - `churn` (total lines added and deleted, merges excluded)
   - the number of commits, the first and last commit, and the lineage

## Example

In flask, `src/flask/app.py` imports `src/flask/sansio/app.py`, giving an IMPORTS edge. `Flask.wsgi_app` calls
`self.request_context(...)`, which resolves to `Flask.request_context`, giving a CALLS edge. `class Flask(App)` gives
an INHERITS edge to `sansio/app.py:App`.

## Where in the code

- `afd_oracle_ingest/structure.py`: `build_snapshot`, `landing_points`
- `afd_oracle_ingest/ast/resolve.py`: import, call and inheritance resolution

## Limits

- Name-based call resolution can't follow calls through variables of unknown type, such as
  `handler = get(); handler.run()`. The change-safety step adds **typed** call edges using Jedi to close part of this
  gap (see [Type-aware call graph](../change-safety/03-call-graph.md)).
- The snapshot is rebuilt in full on every ingest. That is cheap, because parses are cached.

Next: [Finding reverts](05-reverts.md)
