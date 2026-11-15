# Races and shared state

> A **race** happens when two threads or tasks use the same data at the same time and the result depends on who goes
> first. Oracle looks for state that outlives a single function call and is written from inside functions. That is
> where races live.

## Why it matters

Web servers run requests in parallel. A module-level dict used as a cache works in development, where there is one
request at a time, and corrupts data in production. Race bugs are rare, hard to reproduce and costly. It's much
cheaper to catch the pattern than the bug.

## Structural checks

These are mostly Python; JS/TS has one module-state check.

| Rule | Catches | Severity |
|---|---|---|
| `race.shared_mutation` | A module-level mutable (`[]`, `{}`, `set()`, `dict()`, `defaultdict(` …) that a function mutates (`.append`, `.update`, `x[k] = …`, …) | warn (info if under a lock, or if the module never starts threads or tasks) |
| `race.lazy_init` | `global x; if x is None: x = …` without a lock: two threads can both initialise | warn |
| `race.class_mutable` | A class-level mutable attribute shared by all instances and mutated through them | warn |
| `race.await_check_act` | Check a condition, `await`, then act on it: the world may have changed during the await | warn |
| `race.js_module_state` | A module-level `let` reassigned from functions | info |

**Lock awareness.** A write inside `with lock:`, `with self._mutex:` and similar is downgraded. Oracle looks at the
enclosing indentation blocks above the line.

## Line rules

| Rule | Catches | Severity |
|---|---|---|
| `race.mutable_default` | `def f(x=[])`: the default list is shared between calls | warn |
| `race.global_stmt` | A function rebinds a module global | warn |
| `race.toctou` | `if os.path.exists(p): open(p)`: the file can change between the check and the use ("time of check, time of use") | warn |
| `race.entry_point` | Code that starts threads, processes or tasks: context for the checks above | info |

## How it is used

- At ingest, these produce findings, which feed the [risk score](10-risk-score.md).
- In `check_diff`, they run on the added lines. A new shared-mutation in your change is flagged for review.
- Functions with past race fixes are **race zones** (see [Bug-fix lineage](05-fix-lineage.md)).

## Example

The safety fixture plants:
- a module dict `_cache` written in `store()` in a module that starts threads (warn)
- a lazily initialised global client in `get_client()`
- a class-level `Registry.items = []`

A diff that adds `_SEEN = []` and `def remember(x): _SEEN.append(x)` gets `race.shared_mutation`.

## Where in the code

- `afd_oracle_ingest/safety/concurrency.py`: the structural checks
- `safety/rules.py`: the `race.*` line rules

## Limits

Oracle doesn't know your runtime. A module that is only ever used from a single thread will still get findings,
graded **info** where Oracle can tell. The verdict is "look at this", not "this is a bug".

Next: [Logic bombs and supply chain](08-logic-bombs-and-supply-chain.md)
