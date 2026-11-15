# Oracle

**Oracle** (`afd-oracle`) is part of [Agentic Fever Dream](https://github.com/agentic-fever-dream). It turns a
repository's full git history and code structure into a local knowledge base that Claude Code uses while you work.
It records why code is the way it is, what was tried and abandoned, what secretly changes together, and where the risky
parts are.

It is also a **change-safety gate**. Before Claude edits code, Oracle shows what the change can break. After the edit,
it checks the actual diff for regressions, breaking changes, unintended action at a distance, security hazards, race
hazards and logic bombs, and names the tests to run. See [Change safety](#change-safety).

It runs entirely on your machine:
- **Ingestion** is deterministic: parsing, diffing, co-change maths, revert detection, metrics and local embeddings. It
  uses no AI tokens.
- **Mining** runs Claude agents. One agent proposes findings with exact citations, a deterministic gate checks every
  cited commit, file and symbol against git, and an independent reviewer tries to refute each finding. Only findings
  that survive become **verified insights**.

```
Dashboard (browser) ──► add / update / mine repositories ──► local knowledge base (SQLite + Neo4j + Qdrant)
Claude Code (any repo) ──► afd-oracle MCP server + skill + /afd oracle ──┘
```

**New here?** The [documentation](docs/README.md) explains how everything works from first principles: ingestion,
mining, and every change-safety detector, in short pages that assume no background.

## Quick start

Prerequisites: Python 3.12, [uv](https://docs.astral.sh/uv/), git, Node.js 18+ (builds the dashboard once),
Docker Desktop, and [Claude Code](https://claude.com/claude-code) for mining and for using the knowledge base.

```bash
git clone https://github.com/agentic-fever-dream/afd-oracle.git
cd afd-oracle
uv sync
uv run afd-oracle ui          # opens http://127.0.0.1:8765
```

In the dashboard:
1. **Health strip.** Click **Start services** to start Neo4j and Qdrant in Docker and initialise them. Then click
   **Register for Claude** and **Install skill + commands**. These register the `afd-oracle` MCP server for your user
   and install the skill and the `/afd oracle` commands into `~/.claude`. Then click **Run at login** so the dashboard starts
   with your session and restarts itself if it ever stops (or run `uv run afd-oracle service install`).
2. **Add repository.** Paste a git URL or pick a local folder. Ingestion shows live progress. A first ingest of a large
   repository can take a while, mostly CPU embeddings. Later updates only read new commits.
3. **Mine.** Choose how many candidates to mine. The dashboard shows a token estimate first, then runs the analysts
   and reviewers through Claude Code in the background, on your Claude subscription.
4. **Insights / Reports.** Browse the verified findings with their linked commits and diffs.

`uv run afd-oracle setup` does step 1's Claude registration from the terminal.

## Using it in Claude Code

Open Claude Code in any repository you have added.

- **Automatically:** the `afd-oracle` skill triggers on questions about history and design ("why is this like this?",
  "was this tried before?", "what else changes with this file?") and before non-trivial edits. Its first step is to
  call `identify_repo`, which recognises the working copy by its root commit or remote URL and reports how stale the
  knowledge base is.
- **Explicitly:** use `/afd oracle <action>`:

| Command | What it does |
|---|---|
| `/afd oracle assess <files or symbols>` | Before a change: blast radius, coupling, contracts, API, tests, hazards, past fixes |
| `/afd oracle check` | After a change: block / warn / pass verdict for the working copy, plus the tests to run |
| `/afd oracle mutate <file::function>` | Would the tests catch a regression in this function? |
| `/afd oracle discussions` | Sync GitHub pull requests and issues for this repository |
| `/afd oracle ask <question>` | Answers from the knowledge base, citing insights, commits and paths |
| `/afd oracle status` | Is this repository in Oracle, how current is it, top findings, dashboard link |
| `/afd oracle add [url-or-path] [name]` | Adds this or another repository (deterministic, no AI tokens) |
| `/afd oracle update` | Ingests commits added since the last update |
| `/afd oracle mine [N]` | Mines up to N candidates for verified insights (asks before large runs) |
| `/afd oracle ui` | Starts the dashboard |

`/afd` is the shared router for Agentic Fever Dream tools. Each tool installs its action guide into `~/.claude/afd/`,
so later tools add `/afd <tool> <action>` without conflicts.

## Change safety

Every ingest runs deterministic detectors over HEAD and the history. They take about 11 seconds on flask, with no AI
tokens.

| Detector | What it knows |
|---|---|
| Test map | Which tests exercise each symbol: static calls, imports, naming, co-change history, and optional per-test coverage (`afd-oracle coverage`) |
| String contracts | Producers and consumers of env vars, config keys, events and signals, HTTP routes and endpoints, SQL tables, registries, feature flags and CLI flags, including orphans |
| Typed call graph | Jedi-resolved calls (for example `bus.emit()` → `Bus.emit`) and `OVERRIDES` edges, so blast radius includes overrides |
| Public API | The exported surface at HEAD and recent tags, deprecations, and a classifier for removed or renamed symbols, new required or removed parameters, changed defaults, keyword-only changes and exception changes |
| Fix lineage | Fix commits, the guards each added (None checks, raises, locks, bounds, escaping) and their regression tests, which give per-symbol fragility plus security, race and performance zones |
| Security | Shell, eval and deserialisation sinks, SQL built from strings, TLS off, autoescape off, path traversal and SSRF from request data, hard-coded secrets, and JS `innerHTML` / `eval` / `child_process` |
| Races | Module-level mutable state written from functions, lazy init without a lock, class-level mutables, check-then-act across `await`, and filesystem TOCTOU |
| Logic bombs and supply chain | Date- or host-gated behaviour, exec of decoded blobs, dynamic imports, disabled checks and skipped tests, destructive operations, new dependencies, typosquats, install scripts, CI and build hook edits, and opt-in OSV advisories |
| Error handling and performance | Swallowed or bare excepts, retry loops, I/O inside loops |
| Risk score | Churn × complexity × test gap × fragility × hazards × ownership concentration, per file and per symbol |

Claude uses them through two MCP tools, which the skill calls automatically:
- **`assess_change(paths, symbols)`** runs before an edit. It returns callers and overrides, hidden co-change partners,
  shared string contracts, the public API at stake, the tests to run, existing hazards, past fixes and their guards,
  related verified insights, a risk level, and `must` / `should` items.
- **`check_diff(working_dir | diff)`** runs after an edit and returns a `block` / `warn` / `pass` verdict. It checks for:
  - breaking API changes and guards removed that earlier fixes added
  - contract producers removed while still used, and new uses of undefined keys
  - new security, race, logic-bomb and supply-chain findings on the added lines
  - co-change partners not updated, and changed code no test reaches

  It also lists the tests to run.

For risky logic changes, `afd-oracle mutate "<file>::<function>" --cwd <repo> --python <project python>` measures
whether the tests would catch a regression. It applies targeted mutations, runs the selected tests, and always
restores the file.

GitHub pull requests and issues can be synced per repository: from the dashboard's Risk tab, or with
`afd-oracle discussions <repo>`. They are linked to commits, returned by `get_commit_diff`, and searchable with
`search_discussions`.

The gate is deterministic. It makes risk visible and points to the tests that provide the evidence; it doesn't prove a
change is safe.

### MCP tools (server `afd-oracle`)

`assess_change`, `check_diff`, `list_findings`, `get_contracts`, `search_discussions`, `identify_repo`,
`repo_overview`, `search_qdrant` (hybrid search over code, commits and verified insights),
`get_commit_diff` (with authored, committed and landed-on-main dates), `get_symbol_history` (follows moves and renames),
`run_cypher` (read-only), `list_insights`. The mining pipeline also uses `get_work_packets`, `upsert_insight`,
`verify_citations` and `set_insight_status`.

## CLI

`uv run afd-oracle <command>`:

| Command | Purpose |
|---|---|
| `ui`, `setup` | Start the dashboard; register the MCP server, skill and commands for Claude |
| `ingest <path-or-url> [--repo-id] [--full] [--fetch-pulls]` | Ingest or update a repository |
| `update <repo>`, `mine <repo> [--max N] [--estimate]` | Same as the dashboard buttons; streams progress |
| `assess <repo> -p <file> -s <symbol>`, `check <repo> --workdir <wc>` / `--diff <file>` | The change-safety gate (`check` exits 2 on block, for CI or hooks) |
| `safety <repo> [--osv]`, `coverage <repo> --data .coverage --root <wc>`, `discussions <repo>`, `mutate <file::fn> --cwd <wc>` | Re-run detectors, import per-test coverage, sync GitHub discussions, targeted mutation testing |
| `status`, `repos`, `doctor`, `init-db` | Inspect and maintain |
| `project`, `embed`, `packets [--limit 0] [--reopen]`, `sync-insights` | Re-run individual pipeline stages |

Thresholds such as co-change minimums, revert window and abandoned-branch age are `AFD_ORACLE_*` settings in
`packages/afd_oracle_core/afd_oracle_core/config.py`. `infra/.env` (created from `infra/.env.example` by **Start
services**) holds the database passwords.

### Without Docker

- `afd-oracle ingest ... --no-graph` skips Neo4j. Everything else, including the MCP insight tools, works from the
  per-repo SQLite store. `run_cypher` reports that the graph is unreachable.
- `AFD_ORACLE_QDRANT_PATH=data/qdrant-local` uses embedded Qdrant. It is single-process only: stop the MCP server while
  `afd-oracle ingest` runs.
- Any Neo4j 5.x server also works, for example the community zip run with `bin/neo4j console` on Java 21.

## How it fits together

```
git repo ──pygit2──► history.py ──► SQLite store (data/repos/<id>/store.sqlite) ──► writers/neo4j_writer.py ──► Neo4j
                       │  ▲                     │                                   embed.py ─────────────► Qdrant
            tree-sitter│  │blob-keyed cache     ├─ structure / moves / reverts / branches / cochange / metrics
                       ▼  │(parse once, ever)   └─ packets.py ──► noise filter ──► work packets ──┐
                  ast/ extractors                                                                ▼
   dashboard (afd_oracle_ui) ── headless Claude Code ── .claude/workflows/oracle-*.js ── oracle-analyst / oracle-verifier
                                                                    │
                                                  FastMCP server (afd_oracle_mcp) ◄── Claude Code in your repositories
```

| Package | Role |
|---|---|
| `afd_oracle_core` | Settings, the SQLite store, Neo4j and Qdrant access, embeddings, the citation gate, insights |
| `afd_oracle_ingest` | The deterministic pipeline and the `afd-oracle` CLI |
| `afd_oracle_mcp` | The MCP server |
| `afd_oracle_ui` | FastAPI backend, job runner, setup, and the React SPA in `web/` |

- **Store first, project second.** The deterministic pipeline writes to a per-repo SQLite store. Neo4j and Qdrant are
  projections of it. That makes incremental runs cheap and lets the citation gate work offline.
- **Incremental.** New commits are found by hiding previously processed ref tips, and each git blob is parsed once,
  ever. Derived layers are recomputed on each run, and an incremental run matches a full rebuild (this is tested).
- **Citation gate** (`afd_oracle_core/citations.py`):
  - every hash must resolve in the store and in git
  - every `{path, at_commit}` must exist in that tree or diff
  - every symbol uid must exist
  - at least one cited commit must have touched a cited file or symbol

  It runs again when an insight is marked verified.
- **Real-history hygiene:**
  - Co-change, reverts and graveyard metrics use main-line commits only.
  - Deletions and re-creations a few commits apart are linked as moves.
  - Type-only imports are left out of cycle detection.
  - Every main-line commit records when it landed.
  - Packets show authored, committed and landed dates separately, and flag pure renames.
- **Noise filter** (`AFD_ORACLE_PACKET_NOISE_FILTER`, on by default). It drops candidates a program can recognise as
  noise:
  - docs-only, trivial, version-bump and dependency-bump changes
  - tests changing alongside the code they test
  - docs or changelog lockstep
  - pairs already linked by an import
  - duplicate PR refs and branches with no source change
  - moved symbols and test code

  Drops are counted by reason. On the flask repository this cut 2,382 candidates to 899.
- **Mining runs headless.** The dashboard (or `afd-oracle mine`) claims candidates, writes a run file, and starts
  `claude -p` in this folder to run the `oracle-full` workflow. Candidates are marked done when the run succeeds; a
  failed run's candidates can be released back to the queue.
- **Insights** go `proposed` → `verified` | `rejected`. They are stored in SQLite, projected to Neo4j
  (`:Insight:AntiPattern` and so on, `EVIDENCED_BY`, `CONCERNS`), and embedded into `architectural_insights` once
  verified. If Neo4j was down, `afd-oracle sync-insights` replays them.

## Design notes

- **Extractors walk tree-sitter nodes directly** instead of using `.scm` query files. The py-tree-sitter query API
  changed across 0.2x releases, while node and field traversal stayed stable.
- **Neo4j Community has no role-based access control**, so `run_cypher` uses READ-mode transactions (enforced by the
  server), a clause guard, and row and time limits.
- **Verification.** Medium- and high-severity findings get an Opus reviewer (two for high severity). Info and low
  findings get a Sonnet reviewer. If a reviewer fails, the insight stays `proposed`.
- **Commit identity is repo-scoped** (`commit:<repo>:<sha>`), so a fork and its upstream can both be ingested.
- **The dashboard binds to 127.0.0.1 only.** Every request that changes something must carry an `X-AFD-Oracle-UI`
  header, which blocks cross-site requests.

## Tests

```bash
uv run pytest tests --ignore=tests/mcp && uv run pytest tests/mcp   # ~320 tests; Neo4j tests skip unless AFD_ORACLE_NEO4J_PASSWORD is set and a server is on :7687
cd packages/afd_oracle_ui/web && npm run build   # type-checks and builds the dashboard
```

The suite builds a fixture repository with planted facts and checks:
- **History:** counts against `git rev-list` and `git show --numstat`, lifecycle events, a full cache hit on re-run,
  and an incremental run matching a full rebuild.
- **Analysis:** Jaccard against `git log`, both kinds of revert, branch classification, the import cycle, the god class,
  the noise filter, cross-commit moves and landing dates.
- **Guardrails:** MCP guards, the citation gate, the insight lifecycle, and `identify_repo` on working copies and clones.
- **Dashboard API:** add, ingest, estimate, mine (with a stand-in for `claude`) and release.

Tests use a hash embedder and in-memory Qdrant, so they never download models.

## CI and releases

- **CI** (`.github/workflows/ci.yml`, every push and pull request): a gitleaks secret scan over the whole history
  (rules in `.gitleaks.toml`), the test suite on Windows, and the dashboard type-check and build.
- **Release** (`.github/workflows/release.yml`): push a tag matching the version, e.g. `v0.1.0`. It re-runs the secret
  scan, builds the dashboard and every package's wheel and sdist (`uv build --all-packages`), signs each file, writes
  `SHA256SUMS`, adds a build-provenance attestation and publishes a GitHub release. Run it by hand for a dry run that
  stops before publishing.
- **Signing:** the release signs with a PKCS#12 certificate held in the `SIGNING_CERT` secret (base64 of the `.pfx`)
  and `SIGNING_CERT_PASSWORD`. Each artifact gets a detached CMS signature (`.p7s`) and the release carries
  `signing-cert.pem`; verify with
  `openssl cms -verify -binary -inform DER -in file.whl.p7s -content file.whl -CAfile <issuer chain>`.
  Without the secret, releases are published unsigned with a warning.
