# `check_diff`: after an edit

> After Claude edits code, it calls `check_diff`. Oracle reads the actual change, runs every detector on it,
> compares it with history, and returns a verdict: **pass**, **warn** or **block**. It also returns the tests to run
> and the next steps.

## Why it matters

A plan is not the change. `check_diff` looks at what was really written, catching what an assessment can't: a new
`shell=True`, a deleted guard, a renamed event, a misspelled dependency.

## How to call it

- **MCP:** `check_diff(working_dir="<repo folder>")` reads the working copy's changes (staged, unstaged and
  untracked). `check_diff(repo="<id>", diff="<unified diff>")` checks a diff passed as text against the
  knowledge base's HEAD.
- **CLI:** `afd-oracle check <repo> --workdir <folder>`, or `--diff change.patch`. It exits with code 2 on
  **block**, so it can be used as a pre-commit or CI gate.
- **Slash command:** `/afd oracle check`.

## How it works

1. **Build the diff.** In working-copy mode, the diff is `git diff HEAD` plus untracked files. In text mode, the given
   diff is applied to the HEAD version of each file. Either way, Oracle gets each file's old and new text and the
   exact added and removed lines.
2. **Scan the added lines** with every line rule ([security](06-security.md), [race](07-races.md),
   [logic bomb](08-logic-bombs-and-supply-chain.md), [error and performance](09-error-handling-and-performance.md)),
   using the stricter diff severities. Run the structural [race checks](07-races.md) on the new file.
3. **Supply chain:** check changed dependency and build files ([details](08-logic-bombs-and-supply-chain.md)).
4. **Breaking changes:** compare old and new public signatures ([Public API](04-public-api.md)).
5. **Contract deltas:** removed producers that others still use, and new consumers with no producer
   ([String contracts](02-string-contracts.md)).
6. **Removed guards:** compare removed lines with guards that past fixes added to the same function
   ([Fix lineage](05-fix-lineage.md)).
7. **Sensitive zones:** warn when changed functions have security, race, performance or regression fix history.
8. **Hidden partners:** a co-change partner with Jaccard ≥ 0.3, no import link, and not part of this change gives
   `coupling.partner_not_changed` (warn).
9. **Tests:**
   - list the tests mapped to the changed code and its callers (`tests_to_run`)
   - warn with `tests.untested_change` when a changed function has no mapped test and no test file in the change
     mentions its module
10. **Verdict:**
    - **block** if any finding is block
    - **warn** if any finding is warn
    - **pass** otherwise

## What it returns

| Field | Contents |
|---|---|
| `verdict`, `counts` | pass / warn / block, and the number of findings per severity |
| `files`, `changed_symbols` | What changed |
| `findings` | Each finding's rule, severity, file and line, message, symbol and detail, sorted by severity |
| `tests_to_run`, `tests_in_change` | Ranked tests, and the test files you touched |
| `callers_affected` | Production callers one level up |
| `next_steps` | E.g. "Resolve every 'block' finding, or get the user's explicit acknowledgement", "Breaking public API change: keep a compatible signature…", "Run: pytest …" |

## The rule for Claude

The `afd-oracle` skill tells Claude to:
- fix every **block**, or explain it and ask you to accept it explicitly
- review every **warn**
- run the listed tests
- never present a block as done

## Example

In the safety fixture, a single edit to `app/api.py` changes `fetch(key, timeout=10)` to `fetch(key, timeout)` and
deletes the `if key is None: raise …` guard. `check_diff` returns:
- `verdict: "block"`
- `api.param_now_required` (block)
- `regression.guard_removed` (block), citing "Fix crash when key is None (#12)"
- `tests/test_api.py::test_fetch_none` in `tests_to_run`

Changing `Shape.area`'s return value from `0` to `0.0` passes with no blocks.

On flask, a test edit to `src/flask/helpers.py` was blocked on a breaking change to `get_flashed_messages`'s
signature, warned about `SESSION_COOKIE_DOMAINS`, and named 5 tests to run. Through headless Claude Code, the `/afd oracle check`
run took 14 seconds.

## Where in the code

- `afd_oracle_ingest/safety/gate.py`: `Gate.check_diff`, `_next_steps`
- `safety/diffparse.py`: unified diff parsing and application

Next: [Mutation testing](13-mutation-testing.md)
