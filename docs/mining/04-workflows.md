# Workflows: how the agents are orchestrated

> A workflow is a small JavaScript program, run by Claude Code, that starts agents in a planned order: analysts in
> batches, a verifier for each finding, then a report. The control flow is code, so it's predictable and repeatable.

## Why it matters

If one big agent were asked to "mine this repository", it would decide its own plan, skip things silently and run
out of context. A workflow fixes the plan in code:
- which packets go to which analyst
- that every finding gets verified
- that what was skipped or failed is reported

## The workflows

They live in `.claude/workflows/`:

| Workflow | Does |
|---|---|
| `oracle-coupling` | Analyses coupling packets and verifies each finding |
| `oracle-graveyard` | Analyses graveyard packets and verifies each finding |
| `oracle-patterns` | Analyses pattern packets and verifies each finding |
| `oracle-catalog` | Analyses [catalogue](07-catalogue.md) packets of every kind, using each packet's own question, and verifies each finding |
| `oracle-full` | Runs the four above **in parallel**, then a synthesis report and a completeness critic |
| `oracle-verify` | Verifies insights left `proposed` by an interrupted run |

## Inside a miner (for example, `oracle-coupling`)

1. **Batch.** The packet ids are split into batches of 5 (`batch_size`).
2. **Pipeline.** Each batch goes through two stages, with **no waiting between batches**. While batch 3 is still
   being analysed, batch 1's findings are already being verified.
   - **Analyse:** one `oracle-analyst` agent per batch returns `{insights, skipped}`.
   - **Verify:** for each proposed insight, the verifiers described in [Verification](03-verification.md) run in
     parallel.
3. **Report.** The workflow returns counts (proposed, verified, rejected, unverified, duplicates), skipped packets
   with reasons, and the number of failed batches. It also logs these, so a partial run is never mistaken for a
   complete one.

Each insight records a `workflow_run` id, such as `mine-flask-20261101-…/coupling`, so a run's results can be
counted and traced.

## `oracle-full`: the whole pass

1. **Mine:** the four miners run at the same time, as child workflows.
2. **Synthesise:** one Opus agent reads **only verified insights** and writes `reports/<repo>-oracle.md`. The report
   has these sections: an executive summary, hidden coupling, rejected attempts, anti-patterns and patterns,
   cross-links between findings, and recommended next steps. Every claim carries its insight id and short commit
   hashes.
3. **Critique:** a small Sonnet "completeness critic" counts packets still open and lists gaps: kinds never run,
   failed batches, unverified insights.

## Concurrency

Claude Code runs a limited number of agents at once; the rest wait in a queue. A run of 100 packets is therefore
about 20 analyst agents plus one or two verifiers per finding, spread over time.

Next: [Running a mining job](05-running.md)
