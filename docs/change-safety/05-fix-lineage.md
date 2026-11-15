# Bug-fix lineage

> Oracle finds every commit that fixed a bug, and records which functions it changed, what **guard** lines it added
> (a None check, a raise, a lock, escaping…) and which tests came with it. Removing such a guard later is flagged as a
> likely **regression**.

## Why it matters

The most common regression is undoing a past fix. Someone sees an odd-looking `if key is None: raise …`, thinks it's
clutter, and removes it, and the crash from three years ago is back. The guard's purpose lives in the history, not in
the code.

## How it works

### 1. Find fix commits

A fix commit is a main-line, non-merge commit whose message matches the fix pattern, or that reverts a regression.

| Pattern | Examples |
|---|---|
| Fix | "fix", "fixes", "bug", "regression", "crash", "broken", "hotfix", "issue #12", "closes #34", "workaround", "corrects the handling" |
| Not a fix | "fix typo", "docs", "spelling", "lint", "formatting", "changelog", "readme" |

Up to `safety_max_fix_commits` (4,000) of the most recent are analysed. On flask, 356 fix commits were found.

### 2. Classify each fix

| Kind | Message mentions |
|---|---|
| `security` | security, CVE, XSS, CSRF, injection, sanitise, escape, traversal, auth bypass… |
| `race` | race, deadlock, thread-safe, concurrency, atomic, lock… |
| `performance` | performance, faster, slow, optimise, memory leak, N+1… |
| `regression` | regression, regressed |
| `bug` | Anything else |

### 3. Record the guards

For each added line in the fix's diff, Oracle finds the smallest enclosing function and checks the line against the
guard patterns:

| Guard | Looks like |
|---|---|
| `none_check` | `is None`, `is not None`, `=== null`, `!== undefined` |
| `raise` | `raise …`, `throw …` |
| `type_check` | `isinstance(`, `typeof`, `instanceof` |
| `lock` | `with … lock`, `.acquire(`, `Lock(` |
| `bounds` | `len(x) < …`, `min(` / `max(`, `.length >` |
| `empty_check`, `exception_handling`, `sanitize`, `assert` | Emptiness checks, try/except, escaping and quoting, assertions |

Test functions added in the same commit are recorded as that fix's **regression tests**.

### 4. Zones and fragility

- **Fragility** of a function is its number of past fixes.
- A function with security, race, performance or regression fixes sits in a **zone** of that kind. Changes there
  deserve extra review.

## How it is used

- **`assess_change`:** for each target, it lists the past fixes, zones, guards and regression tests. For zone
  functions it adds a **must**: "review against fix X".
- **`check_diff`:**
  - `regression.guard_removed` (**block**): a removed line exactly matches a guard that a fix added to that
    function.
  - `history.sensitive_zone` (warn): you changed a function in a security, race, performance or regression zone.

## Example

In the safety fixture, the commit "Fix crash when key is None (#12)" adds `if key is None: raise ValueError(...)` to
`fetch`, along with `test_fetch_none`. A later diff that deletes those two lines gets `regression.guard_removed`
(**block**), citing the fix commit, and `check_diff` tells Claude to run `test_fetch_none`.

## Where in the code

`afd_oracle_ingest/safety/lineage.py`: `FIX`, `NOT_FIX`, `KINDS`, `GUARDS`, `build`, `zones`.

## Limits

- It depends on commit messages. Fixes with messages like "update" are missed.
- A guard that is moved or reworded, rather than deleted, doesn't match exactly, so it isn't flagged.

Next: [Security hazards](06-security.md)
