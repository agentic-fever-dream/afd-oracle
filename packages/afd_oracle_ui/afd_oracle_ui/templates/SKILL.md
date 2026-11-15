---
name: afd-oracle
description: Use when working inside a git repository, and ALWAYS before and after non-trivial code changes in a repository that Oracle knows. Before an edit it shows the blast radius, hidden coupling, string contracts, public API, tests to run, hazards and past fixes. After an edit it checks the diff for breaking changes, removed guards, contract breaks, and security, race, logic-bomb and supply-chain hazards. Also use for questions about history and design: why code is the way it is, what was tried before, what changes together. Requires the `afd-oracle` MCP server.
---

# Oracle: change safety and codebase history

The `afd-oracle` MCP server holds a deterministic model of each repository the user added in the Oracle dashboard:
- full git history and symbol lifecycles
- the call graph, including type-resolved calls and overrides
- co-change coupling and string contracts (env, config, events, routes, SQL tables, registries)
- the public API at HEAD and recent tags
- the test-to-code map
- bug-fix lineage: the guards each fix added, and security, race and performance zones
- baseline hazards and a risk score
- **verified insights**, which passed a citation gate and an adversarial review
- optionally, GitHub pull request and issue discussions

## 0. Identify the repository (once per session)

Call `identify_repo(path=<absolute current working directory>)`.
- `found: false`: say in one line that this repository is not in Oracle (it can be added in the dashboard, `uv run
  afd-oracle ui`). Then continue normally without these tools.
- `found: true`: keep `repo_id`, and pass it as `repo` to every tool. If `stale` is true, say the model is
  `commits_not_ingested` commits behind and suggest `/afd oracle update`. Then trust the gate less for recent code.

## 1. Before a non-trivial change: `assess_change`

Call `assess_change(repo, paths=[files you will edit], symbols=[functions or classes you will change])` before you
write the code. Then act on the report:
- **public_api**: keep signatures compatible. Default any new parameter, don't remove or rename, and add a deprecation
  path for anything you replace. External callers are invisible here.
- **blast_radius** (callers and overrides): read the important callers. Update them, or keep the behaviour they rely on.
- **hidden_coupling** (`hidden: true`): those files change with this one without an import link. Check whether they
  need the same change.
- **contracts**: if you change a key (env var, config key, event, route, table), update every counterpart, or don't
  change the key.
- **history**: preserve the guards that earlier fixes added. Treat security, race and performance zones with extra care.
- **hazards**: don't add to existing hazards in code you touch. Fix them if that's in scope.
- **tests**: these are what you must run afterwards. If there are none, write a characterization test first.
- Follow the `must` items, and mention the `should` items to the user.

For small edits (typos, comments, a one-line obvious fix) you can skip this, but still run step 2.

## 2. After editing: `check_diff`

Call `check_diff(repo, working_dir=<absolute repo root>)`, or pass `diff=` with the unified diff.
- **verdict `block`**: fix every block finding, or show it to the user and get explicit acknowledgement before
  continuing. Never silently ignore one. Typical blocks:
  - a breaking public API change
  - removing a guard an earlier fix added
  - removing a contract producer that is still used
  - shell, eval or SQL-format sinks
  - obfuscated `exec`
  - a typosquatted dependency or an install script
- **verdict `warn`**: review each warning. Explain what you checked, or fix it.
- **Tests**: run `tests_to_run` with the project's normal test command. Report the results honestly, and don't claim
  safety if tests were not run.
- **Untested change** (`tests.untested_change`): add tests, or say clearly that the change is unverified.
- For risky logic changes, measure whether the tests would catch a regression. Run
  `uv run --directory "<oracle root>" afd-oracle mutate "<path>::<Qual.name>" --cwd "<repo root>" --python "<project python>" --repo <repo_id>`.
  Surviving mutants show behaviour no test pins down.

Run `check_diff` again after your fixes, until it reports no block (or every block is acknowledged by the user).

## 3. History and design questions

| Question | Tools |
|---|---|
| Why is X like this? | `search_qdrant(collection="architectural_insights")`, `get_symbol_history`, `get_commit_diff`. The diff includes linked PRs and issues; also `search_discussions` |
| Was this tried before? | `list_insights(status="verified")` (RejectedAttempt), `search_discussions`, `search_qdrant(collection="commit_diffs")` |
| What else changes with this file? Who uses this key? | `assess_change`, `get_contracts(key=...)`, read-only `run_cypher` |
| What is risky here already? | `list_findings(path=...)`, `repo_overview` |

## Rules

- Cite what you rely on: insight ids, short commit hashes, paths, PR and issue numbers. Say so when Oracle has no
  evidence.
- Prefer verified insights. `proposed` ones are unconfirmed, and `rejected` ones failed review.
- The gate is deterministic and can miss things or flag false positives. It makes risk visible; it doesn't prove a
  change is safe. Tests that actually ran are the evidence.
- Never call `upsert_insight`, `set_insight_status` or `get_work_packets`. Those belong to the mining pipeline, which
  the user runs from the dashboard.
