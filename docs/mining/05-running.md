# Running a mining job

> You press **Mine** in the dashboard, or run `afd-oracle mine <repo>`. Oracle reserves the packets, starts Claude
> Code in the background with the `oracle-full` workflow, shows live progress, and marks the packets done when it
> finishes.

## Before you start: the estimate

The **Mine** tab shows how many open candidates there are of each kind, and an estimated token cost:

```
estimated tokens = packets × 13,000
```

The 13,000 figure was measured on flask. It covers the analyst, the verifiers and a share of the synthesis, counting
all agent tokens. You can **cap** the run at N packets. The cap is shared **round-robin** across kinds (one coupling,
one graveyard, one pattern, repeat, highest priority first), so a small run still samples every kind.

`afd-oracle mine <repo> --estimate` prints the same numbers without starting anything.

## What happens when you press Mine

1. **Checks.** No other job may be running for this repository, and the `claude` command must be installed.
2. **Claim.** The chosen packets move from `open` to `claimed`, so a second run can't take them.
3. **Run file.** Oracle writes `data/runs/<run_id>.json` containing the repository, the run id, the batch size and
   the packet ids per kind.
4. **Headless Claude Code.** The dashboard starts:
   ```
   claude -p "<prompt: read the run file, run the oracle-full workflow with exactly those args>"
          --model sonnet --output-format stream-json --verbose
          --allowedTools "Workflow Read mcp__afd-oracle__get_work_packets mcp__afd-oracle__list_insights"
   ```
   - The main session only orchestrates, so it runs on Sonnet. The workflow's agents use their own models.
   - `--allowedTools` limits the main session to running the workflow and reading. It can't edit files or run
     commands.
   - The command runs from the Oracle project folder, so it finds the agents, the workflows and the MCP server.
5. **Live progress.** Claude's output is streamed to the job log. The dashboard counts this run's insights by status
   (proposed, verified, rejected) as they appear, and shows them over a live connection (server-sent events).
6. **Finish.** On success, the run's packets become `done`. The job summary records insight counts for the run,
   including its child workflows (`run_id/coupling` and so on).

## When something goes wrong

- **Cancel** stops the process tree.
- **Release unfinished** returns *this run's* still-claimed packets to `open`, so they can be mined again. Other
  runs' packets are untouched.
- Insights left `proposed` by an interrupted verifier can be finished by the `oracle-verify` workflow.

## Billing note

Headless Claude Code uses your normal Claude Code login and settings. If `ANTHROPIC_BASE_URL` or `ANTHROPIC_API_KEY`
is set globally, mining will go through that endpoint or key. The dashboard's health strip warns about this.

## Where in the code

- `afd_oracle_ui/app.py`: `get_estimate`, `post_mine`, `_pick`, `post_release`
- `afd_oracle_ui/jobs.py`: the subprocess job runner, logs and event stream

Next: [Insights](06-insights.md)
