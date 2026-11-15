# Using Oracle

> There are three ways in. The **dashboard** is for adding and managing repositories. **Claude Code** uses Oracle
> automatically through the skill and MCP tools, or explicitly through `/afd oracle <action>`. The **CLI** is for
> scripting and CI.

## One-time setup

1. **Start the databases:** `docker compose -f infra/docker-compose.yml up -d`, or use the button on the dashboard's
   health strip. A Neo4j password and a Qdrant API key are generated into `infra/.env` on first run.
2. **Open the dashboard:** `uv run afd-oracle ui`, then go to http://127.0.0.1:8765.
3. **Connect Claude Code:** use the health strip's **Install** buttons, or run `uv run afd-oracle setup`. This:
   - registers the `afd-oracle` MCP server for your user, so it works in every folder
   - installs the `afd-oracle` skill
   - installs the `/afd` command router
   - installs the dashboard as a login service (below)

### Keeping the dashboard running

A dashboard started with `afd-oracle ui` lives only as long as the terminal (or tool) that started it. Install it as
a service instead, with the health strip's **Run at login** button or `uv run afd-oracle service install`:

| Layer | What it does |
|---|---|
| OS service | Windows Task Scheduler task, macOS launchd agent or Linux systemd user unit. Starts the supervisor at login. |
| Watchdog (Windows) | The task re-runs every minute. That's a no-op while the supervisor is alive and brings it back within a minute if it was killed. launchd and systemd restart it themselves. |
| Supervisor (`afd_oracle_ui.serve`) | Runs the server as a child process and restarts it within seconds whenever it exits. If another dashboard is already answering on the port, it only watches, and takes over if that one stops. |

Logs are in `data/logs/dashboard.log`. `afd-oracle service status | start | stop | uninstall` manage it.

The databases run in Docker with `restart: unless-stopped`, so they come back whenever Docker Desktop is running. If
Docker Desktop isn't set to start when you sign in, the health strip warns you.

## The dashboard

- **Home:**
  - A health strip covering Docker, Neo4j, Qdrant, the Claude CLI, the MCP server, the skill and commands, each with a
    one-click fix.
  - A card for each repository.
- **Add repository:** a git URL or a local folder. Oracle clones it if needed, ingests it, and shows each
  [ingestion step](ingestion/README.md) live.
- **Repository page:**

  | Tab | What it's for |
  |---|---|
  | Overview | Counts, candidates by kind, noise-filter drops, and the **Update** button |
  | Risk | Findings by severity, riskiest files, an assess form, and the GitHub discussions sync |
  | Mine | Open candidates, the token estimate, a cap, **Start**, live counters, **Cancel** and **Release unfinished** ([details](mining/05-running.md)) |
  | Insights | Search and filter insights; a detail drawer shows citations, linked diffs and verifier notes |
  | Reports | The Markdown reports written by mining |
  | Activity | Job history and logs |

The dashboard listens on `127.0.0.1` only. Requests that change anything must carry the `X-AFD-Oracle-UI` header,
which protects against other websites triggering actions (cross-site request forgery).

## In Claude Code

### Automatically: the skill

When you work in a git repository, the `afd-oracle` skill tells Claude to:
1. Call `identify_repo` with the current folder. Oracle matches the repository by its root commit or remote URL.
   - If it isn't in the knowledge base, Claude tells you how to add it and stops using Oracle.
   - If the knowledge base is behind (`commits_behind > 0`), Claude says so.
2. **Before editing:** call `assess_change` and respect its `must` items.
3. **After editing:** call `check_diff`. Claude fixes or explains every **block**, reviews every **warn**, and runs
   `tests_to_run`.
4. For "why" and "was this tried before" questions, use insights, symbol history and commit diffs, and cite them.

### Explicitly: `/afd oracle <action>`

| Action | Does |
|---|---|
| `assess <files or symbols>` | Run `assess_change` and summarise the risks |
| `check` | Run `check_diff` on your working copy and report the verdict |
| `mutate <file::function>` | Run [mutation testing](change-safety/13-mutation-testing.md) on one function |
| `discussions` | Sync GitHub PRs and issues for this repository |
| `ask <question>` | Answer from insights, history and the graph, with citations |
| `status` | Is this repository in Oracle, and how fresh is it? |
| `add [url-or-path] [name]` | Add a repository (starts ingestion) |
| `update` | Pull new commits and re-ingest incrementally |
| `mine [N]` | Start mining (optionally capped at N packets) |
| `ui` | Open the dashboard |

### The MCP tools

| Tool | Purpose |
|---|---|
| `identify_repo` | Is this folder a known repository? How far behind is it? |
| `repo_overview` | Counts, top verified insights and hotspots |
| `assess_change` | [Before an edit](change-safety/11-assess-change.md) |
| `check_diff` | [After an edit](change-safety/12-check-diff.md) |
| `list_findings` | Change-safety findings, filtered by category, severity and path |
| `list_vulnerabilities` | [Scanner findings](change-safety/16-vulnerability-scans.md) (CVEs, code issues, misconfigurations, secrets), filtered or searched by meaning |
| `get_contracts` | Producers and consumers of a string key |
| `search_discussions` | Search linked PRs and issues |
| `list_insights` | Insights, filtered by status, kind and repository |
| `search_qdrant` | Search code, commits or insights by meaning |
| `run_cypher` | Read-only graph queries |
| `get_commit_diff` | A commit's diff (optionally restricted to paths), with linked discussions |
| `get_symbol_history` | A symbol's diary, with its body at each version |
| `get_work_packets`, `upsert_insight`, `verify_citations`, `set_insight_status` | Used by the mining agents |

## The CLI

Run `uv run afd-oracle <command> --help` for every option.

| Command | Does |
|---|---|
| `ingest <url-or-path>` | Full ingestion ([overview](ingestion/README.md)) |
| `update <repo>` | Incremental update |
| `repos`, `status` | List repositories; show a repository's counts |
| `mine <repo> [--max N] [--estimate]` | Mining through headless Claude Code |
| `safety <repo> [--osv]` | Re-run the change-safety step |
| `scan <repo> [--osv] [--snyk]` | Run [vulnerability scanners](change-safety/16-vulnerability-scans.md) in Docker and store the findings |
| `catalog <repo> [--only id] [--list]` | Re-run the [candidate catalogue](mining/07-catalogue.md) |
| `sandbox coverage <repo>`, `sandbox mutate <repo> --top N` | Run the repository's tests in a [Docker sandbox](mining/08-enrichment-and-sandbox.md) to measure coverage or mutation results |
| `assess <repo> --path …`, `check <repo> --workdir …` | The gate (`check` exits with code 2 on block) |
| `coverage`, `discussions`, `mutate` | Optional precision tools |
| `project`, `embed`, `packets`, `sync-insights` | Rebuild one projection |
| `ui`, `setup`, `doctor`, `init-db` | Dashboard, Claude Code integration, health check, database schema |
| `service install \| status \| stop \| uninstall` | Run the dashboard at login, supervised ([details](#keeping-the-dashboard-running)) |

Back to the [documentation index](README.md).
