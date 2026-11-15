Oracle (afd-oracle): change-safety checks (assess before, check after), history and design answers, status, add or update a repository, mine verified insights, mutation-test a function, sync GitHub discussions, dashboard.

# /afd oracle <action>

Oracle keeps a knowledge base of each added repository: its full git history, symbol lifecycles, hidden coupling,
reverts, abandoned branches and **verified insights**. A verified insight has been checked against git and survived an
adversarial reviewer. It is installed at `{{ROOT}}`. The dashboard is http://127.0.0.1:8765, and `/afd oracle ui`
starts it.

| Action | Use |
|---|---|
| `assess <files or symbols>` | Before a change: blast radius, coupling, contracts, API, tests, hazards and past fixes |
| `check` | After a change: block / warn / pass verdict for the working copy's diff, plus the tests to run |
| `mutate <file::function>` | Would the tests catch a regression in this function? (runs the tests several times) |
| `discussions` | Sync GitHub pull requests and issues for this repository (the "why" behind commits) |
| `ask <question>` | Answer from the knowledge base for the current repository |
| `status` | Is this repository in Oracle, how current is it, and what has been found |
| `add [url-or-path] [name]` | Add the current repository, or the given one (deterministic, no AI tokens) |
| `update` | Pull in commits added since the last ingest (no AI tokens) |
| `mine [N]` | Mine up to N candidates for verified insights (uses Claude; asks before large runs) |
| `ui` | Start the dashboard |

**Shared step, REPO:** call the MCP tool `identify_repo` (server `afd-oracle`) with `path` set to the absolute current
working directory. Use the returned `repo_id` as REPO. Shell commands below run Oracle's CLI as
`uv run --directory "{{ROOT}}" afd-oracle ...`.

## assess <files or symbols>
1. Resolve REPO. ARGS are repo-relative file paths, or symbols such as `path::Class.method` or qualified names. If
   there are none, use the files changed in the working copy (`git diff --name-only HEAD`).
2. Call `assess_change(repo=REPO, paths=[...], symbols=[...])`.
3. Present a short report, in this order:
   - the risk level and the `must` items
   - public API at stake
   - the top callers
   - hidden-coupling partners and contracts
   - zones and fragile code from history, with the guards to preserve
   - existing hazards
   - the tests to run

## check
1. Resolve REPO. Call `check_diff(repo=REPO, working_dir=<absolute git top-level>)`.
2. Show the verdict and every `block` and `warn` finding (rule, file:line, message), then the tests to run.
3. If the verdict is `block`, propose concrete fixes. Don't proceed with further changes until each block is fixed or
   the user explicitly accepts it. Offer to run the tests.

## mutate <file::function>
1. Resolve REPO. Find the project's Python (its virtual environment, or `python` on PATH).
2. Run `uv run --directory "{{ROOT}}" afd-oracle mutate "<file::Qual.name>" --cwd "<git top-level>" --python "<python>" --repo <REPO>`.
   This temporarily edits the file and always restores it; tell the user not to edit that file meanwhile.
3. Report the score and each surviving mutant (line and change), then suggest the tests that would kill them.

## discussions
1. Resolve REPO. Run `uv run --directory "{{ROOT}}" afd-oracle discussions <REPO>`. It uses `gh auth` or
   GITHUB_TOKEN and only fetches updates since the last sync.
2. Report how many pull requests and issues were synced and linked to commits. Then `search_discussions` and
   `get_commit_diff` include them.

## ask <question>
1. Resolve REPO. If it is not found, say so and suggest `/afd oracle add`.
2. Follow the `afd-oracle` skill:
   - Search verified insights first: `search_qdrant(collection="architectural_insights", query, filter={"repo": REPO})`.
   - Then use `get_symbol_history`, `get_commit_diff`, and read-only `run_cypher` for co-change and callers.
3. Answer concisely. Cite insight ids, short commit hashes and paths. Say plainly what the knowledge base has no
   evidence for.

## status
1. Resolve REPO. If it is not found, say that this repository is not in Oracle and offer `/afd oracle add`.
2. Call `repo_overview(repo=REPO)`.
3. Report:
   - when it was ingested, and how many commits it is behind (`commits_not_ingested`), suggesting `/afd oracle update`
     if stale
   - its size and insight counts
   - the five most important verified insights (id and title)
   - the dashboard link `http://127.0.0.1:8765/repos/<REPO>`

## add [url-or-path] [name]
1. SOURCE is the argument if given. Otherwise it is the git top-level of the current directory
   (`git rev-parse --show-toplevel`).
2. NAME is the second argument if given. Otherwise it is the last path segment without `.git`, using letters, digits,
   `.`, `_` and `-` only.
3. If `identify_repo` already finds this repository, say so and suggest `update` instead.
4. Run `uv run --directory "{{ROOT}}" afd-oracle ingest "<SOURCE>" --repo-id <NAME> --packet-limit 0`, in the
   background. For a URL, add `--fetch-pulls`. A first ingest can take from minutes to an hour; most of that is CPU
   embeddings. Tell the user they can watch it in the dashboard.
5. When it finishes, summarise commits, reverts, branches and candidates found. Mention that `/afd oracle mine` turns
   candidates into verified insights.

## update
1. Resolve REPO (it must be found).
2. Run `uv run --directory "{{ROOT}}" afd-oracle update <REPO>`, in the background if the repository is large.
3. Report how many new commits were ingested and how many new candidates appeared.

## mine [N]
1. Resolve REPO.
2. Run `uv run --directory "{{ROOT}}" afd-oracle mine <REPO> --estimate`, adding `--max N` if N was given. Show the
   candidates per kind and the estimated tokens.
3. If the estimate is over 2,000,000 tokens and the user did not give N, ask with AskUserQuestion: run all, cap at a
   number, or cancel. Stop on cancel.
4. Run `uv run --directory "{{ROOT}}" afd-oracle mine <REPO>` with the chosen `--max`, in the background. It runs the
   analyst and reviewer pipeline through a headless Claude Code session, and you can follow it in the dashboard.
5. When it finishes, report proposed, verified and rejected counts. Show the newly verified insights using
   `list_insights(repo=REPO, status="verified")`, filtered to this run's `workflow_run` id.

## ui
Run `uv run --directory "{{ROOT}}" afd-oracle ui` in the background, then give the link http://127.0.0.1:8765.
