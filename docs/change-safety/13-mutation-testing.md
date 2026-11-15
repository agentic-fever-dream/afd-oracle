# Mutation testing

> To check whether the tests would really catch a bug in a function, Oracle deliberately introduces small bugs
> (**mutants**) one at a time and runs the tests. A mutant the tests don't catch marks behaviour that no test pins
> down.

## Why it matters

"There is a test for this" doesn't mean "the test would catch a regression". A test that calls a function and checks
nothing passes whatever the function does. Mutation testing measures how strong the tests are, not just whether they
exist. It's useful before a risky change: if the tests can't catch a flipped `<`, they won't catch Claude's mistake
either.

## How it works

1. **Pick a target:** one function, `path/to/file.py::Qual.name`.
2. **Check the baseline.** The selected tests must pass on the unmodified code; otherwise the results would mean
   nothing.
3. **Generate mutants** inside that function only:

   | Mutant | Example |
   |---|---|
   | Flip a comparison | `==` → `!=`, `<=` → `>`, `<` → `>=` |
   | Flip a None check | `is None` ↔ `is not None` |
   | Swap logic | `and` ↔ `or` |
   | Swap booleans and off-by-one | `True` ↔ `False`, `+ 1` → `- 1` |
   | Negate a condition | `if x:` → `if not (x):` |
   | Drop a raise | `raise ValueError(...)` → `pass` |
   | Return nothing | `return value` → `return None` |

4. **For each mutant** (up to `--max`, default 15):
   - write it to the file
   - run `python -m pytest -x -q` with the selected tests, using **the project's own Python**
   - restore the file
   A failing test **kills** the mutant; a mutant that passes **survives**.
5. **Restore safely.** A backup copy is made first, and the original bytes are restored exactly (including line
   endings) after every mutant and at exit, even after a crash.

The result lists killed and surviving mutants, with each survivor's line and change. The **mutation score** is
killed ÷ total.

## How to run it

```
afd-oracle mutate src/pkg/api.py::fetch --cwd path/to/working-copy --python .venv/bin/python \
    --test tests/test_api.py --max 15
```

Or ask Claude: `/afd oracle mutate fetch`. It runs on demand only, never during ingestion, because it runs your tests
many times. To run the tests inside a locked-down container instead of on your machine, use
`afd-oracle sandbox mutate <repo> --target ...` ([the sandbox](../mining/08-enrichment-and-sandbox.md)).

## Example

Mutating `fetch` in the safety fixture with `tests/test_api.py`: the **drop raise** mutant (removing
`raise ValueError("key is required")`) is killed, because `test_fetch_none` expects that error. Any mutant that
survives names a line whose behaviour no test checks, which is exactly the test to add next.

## Where in the code

`afd_oracle_ingest/safety/mutate.py`: `SWAPS`, `find_function`, `run`.

## Limits

- Python and pytest only.
- Each mutant costs a test run, so slow test suites make this slow. Narrow the tests with `--test`.
- Some mutants are *equivalent*: they don't actually change behaviour, and can never be killed.

Next: [GitHub discussions](14-github-discussions.md)
