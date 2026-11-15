# Test map

> The test map records which tests exercise which code. When a function changes, Oracle can name the tests to run,
> or warn that none exist.

## Why it matters

"Run the tests" is too vague for a large project, where a full run can take far too long. "Run these five tests" is
actionable. Knowing that *no* test covers some code is just as important: a change there has no safety net.

## How it works

Five independent sources each link a **test** (a pytest id like `tests/test_api.py::test_fetch_none`, or a test file)
to a **target** (a symbol uid or a file path). Each link has a **weight** for how strong the evidence is:

| Source | Evidence | Weight |
|---|---|---|
| `static` | A test function calls the target (from the [call graph](03-call-graph.md)) | 1.0 |
| `name` | Naming convention: `tests/test_foo.py` ↔ `foo.py`, and `foo.test.ts` ↔ `foo.ts` | 0.6 |
| `import` | A test file imports the target file | 0.5 |
| `history` | The test file and the target change together (main-line co-change) | The pair's Jaccard |
| `coverage` | Real per-test coverage, imported with `afd-oracle coverage` | Measured |

Some details:
- **Test files** are recognised by their path: `tests/`, `test_*.py`, `*_test.py`, `*.test.ts`, `*.spec.ts` and
  similar.
- **Name matching** ignores generic file names (`__init__`, `index`, `main`, `conftest`), which would match
  everything.
- **Calls from test helpers and fixtures** are attributed to the test file.

### Coverage (optional, most precise)

Run your tests under coverage.py with per-test contexts:

```
pytest --cov --cov-context=test          # or: coverage run --context=test -m pytest
afd-oracle coverage <repo> --data .coverage --root path/to/working-copy
```

Oracle then knows exactly which lines each test executed.

## How it is used

When ranking tests for a change, each link's weight is multiplied by how directly the test reaches the change:

| How the test reaches the change | Multiplier |
|---|---|
| Directly, through the changed symbol | × 1.0 |
| Through the changed file | × 0.7 |
| Through a caller of the changed code | × 0.5 |

The best score per test wins.

## Example

In the safety fixture, `tests/test_api.py::test_fetch_none` calls `fetch`, which gives a static link with weight 1.0.
`assess_change(paths=["app/api.py"])` lists it first among the tests to run.

## Where in the code

`afd_oracle_ingest/safety/testmap.py`: `build`, `import_coverage`.

## Limits

Without coverage data, the static and import links are approximations. A test that reaches code through dependency
injection or HTTP may be missed. [Mutation testing](13-mutation-testing.md) measures whether the tests that do exist
are strong enough.

Next: [String contracts](02-string-contracts.md)
