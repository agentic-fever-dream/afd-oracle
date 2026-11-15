# Enrichment and the Docker sandbox

> Many catalogue builders started as approximations because Oracle didn't keep some facts: a function's old
> signatures, which function a changed line belonged to, which commit wrote a line a fix removed, real test coverage.
> **Enrichment** now derives the git facts on every ingest. The **sandbox** measures coverage and mutation results by
> running the repository's own tests inside a locked-down Docker container.

## Why it matters

An approximation such as "same file" instead of "same function" lets noise through. An analyst can throw noise away,
but every wrong packet costs tokens. Exact data makes the candidates sharper before any model sees them.

## Enrichment (git only, every ingest)

Enrichment runs inside the catalogue step. It never uses the network and never runs repository code. Every part is
incremental: a re-run only processes new commits, and on flask a re-run takes about a second.

| Fact | Table | What builders use it for |
|---|---|---|
| Release tags | `tags` | "Was it like this at the last release?", post-release hotfix clusters |
| File mode changes | `file_modes` | Files made executable, symlink flips |
| Every version of every function | `symbol_versions` | Signature history (parameters added, multi-line signatures), complexity at each version |
| The function each changed line belongs to | `diff_lines.symbol` | "Same function", not "same file" |
| Who wrote the lines a bug fix removed | `line_origins` | `git blame` at the fix's parent, for SZZ-style "which commit introduced this bug" |
| The test map at past releases | `test_map_at` | "Was this tested when the bug was fixed?" |
| Lockfile and vendored-file history | `diff_lines_aux` | Dependency versions over time, local patches to vendored code |
| Patch ids | `commit_patch_ids` | The same change applied again (re-applied reverts, cherry-picks), exactly as `git patch-id` sees it |
| Exceptions each function version raises | `symbol_versions.raises` | Interface changes that only change what a function raises |
| Call graph and string contracts at past releases | `calls_at`, `contracts_at` | "Gained a new caller since the last release", config keys that disappeared and came back |

The parse cache is keyed by extractor version, so history parsed by an older Oracle is re-parsed on demand. On flask,
the first full enrichment parsed the missing file versions, recorded 56,139 symbol versions, mapped 199,465 changed
lines to their function, and blamed 1,957 lines removed by fixes.

The historical test map is approximate: it uses each tag's code but today's import and call graph, so builders
discard links to tests that didn't exist yet at the tag.

## The sandbox (Docker, on demand)

A repository's tests are its own code. Oracle never runs them on your machine directly, only in a throwaway container:

1. **A clean copy.** The commit is exported with `git archive` into `data/sandbox/<repo>/src`. Your working copy is
   never used.
2. **Install, with network.** A first container installs pytest, coverage and the project's test dependencies into a
   cached virtualenv. It only runs `pip`.
3. **Test, without network.** A second container runs the tests with `--network none`, all Linux capabilities
   dropped, `no-new-privileges`, and memory, CPU and process limits (`sandbox_memory`, `sandbox_cpus`). Only the source
   copy, the virtualenv and an output folder are mounted.

### Coverage

`afd-oracle sandbox coverage <repo>` runs the whole suite under coverage.py with per-test contexts and stores:

- `coverage_files` and `covered_lines`: which lines ran at HEAD, and under how many tests
- `test_map` rows with source `coverage`: measured links from each test to the functions it ran

On flask: 482 tests passed and 1 failed (a failing test doesn't stop coverage), 95.2% of lines ran, giving 26,546
measured test-to-function links, in about 3 minutes including the one-time install.

Coverage also records **which tests ran each line** (`covered_line_tests`), so builders can ask "does the test that mocks
this function ever run it?" or "do the tests that reach this guard ever feed it an empty value?".

### Helm charts

`afd-oracle sandbox helm <repo>` renders every Helm chart with `helm template` in a container with no network, at
HEAD and at recent commits that changed it (`helm_renders`). Resource-limit builders then compare real manifests
instead of templated text.

### Mutation testing

`afd-oracle sandbox mutate <repo> --target path/to/file.py::Qual.name` (or `--top N` to let the catalogue pick
fix-prone, tested functions) runs [mutation testing](../change-safety/13-mutation-testing.md) with each mutant's tests
executed inside one long-lived sandbox container. Survivors are stored in `mutation_results`. Mutants are only made
in code, never in docstrings or comments.

## Opt-in GitHub data

The GitHub sync (`afd-oracle discussions <repo>`, off by default) also stores the repository's visibility and fork
facts and its release list. Builders use them for "private repository without forks" and "release published, then its
tag deleted", and read linked PR titles and closed issues. Without a sync, those builders use their git-only path.

## How builders use it

Builders read the new facts through the catalogue context (`ctx.symbol_versions`, `ctx.line_origin`,
`ctx.covered`, `ctx.symbol_coverage`, `ctx.mutations`, ...). Each upgraded builder keeps its old proxy as a fallback,
so it still works when a table is empty, for example before the sandbox has run. Its docstring says which path it
used.

Some approximations remain, because they are judgements rather than missing data: "does this scheduled job touch
the database", "is this default safe", "is this endpoint public". Those are left to the analyst.

## Limits

- Python projects only (pytest and coverage.py). Other ecosystems are skipped with a reason.
- Dependency installation is best-effort. It reads test requirement files and `[test]`/`[dev]` extras. Projects that
  need services (databases, message queues) to run their tests will report failures, and coverage reflects only what
  ran.
- Coverage is measured on one operating system, so platform-specific branches for other systems always look
  unexecuted.

## Where in the code

- `afd_oracle_ingest/catalog/enrich.py`: the enrichment tables
- `afd_oracle_ingest/sandbox.py`: export, install, coverage, mutation, result ingestion
- `afd_oracle_ingest/safety/mutate.py`: the mutation engine (it accepts a container command as its test runner)

Next: [Change safety overview](../change-safety/README.md)
