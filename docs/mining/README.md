# Mining overview

> Mining turns candidates into **verified insights**: short, cited explanations such as "these two files share a
> hidden field list" or "caching was tried in 2019 and reverted because it broke streaming". AI agents do the
> judging; programs and a second, skeptical agent check every claim.

## Why AI at all?

Ingestion can say *that* two files change together. It can't say *why*, or whether the reason matters. Reading
diffs and commit messages and deciding "this is a shared contract" versus "this is a test changing with its subject"
is judgement. That's what language models are good at, and what programs are bad at.

## Why guard rails?

Language models can produce confident, plausible, wrong statements, such as an invented commit hash or a "reason"
that the evidence doesn't support. A knowledge base full of those would be worse than none. So mining has three
layers of defence:

1. **Evidence comes first.** Agents read [work packets](../ingestion/10-work-packets.md) that a program assembled.
   They are told that skipping a packet is a good outcome.
2. **The [citation gate](02-citation-gate.md).** A program refuses to store any insight whose commits, files or
   symbols don't check out against git.
3. **[Adversarial verification](03-verification.md).** A separate agent, which never sees the analyst's reasoning,
   tries to prove the insight wrong. If it is uncertain, the insight is rejected.

## The flow

```mermaid
sequenceDiagram
  participant U as You (dashboard)
  participant API as Dashboard server
  participant C as claude -p (headless)
  participant W as oracle-full workflow
  participant A as Analyst agents (Sonnet)
  participant G as Citation gate
  participant V as Verifier agents
  U->>API: Mine (optional cap)
  API->>API: claim packets, write run args file
  API->>C: start headless Claude Code
  C->>W: run oracle-full with the run's packet ids
  W->>A: batches of 5 packets (coupling / graveyard / pattern in parallel)
  A->>G: upsert_insight(claim + citations)
  G-->>A: stored as proposed, or invalid + reasons
  W->>V: one verifier per proposed insight (2 for high severity)
  V->>V: re-derive from git; try to refute
  V->>G: set_insight_status(verified | rejected); gate re-run
  W->>W: Opus synthesis report + completeness critic
  C-->>API: done
  API->>API: mark packets done; count insights for this run
```

## Pages

1. [Analysts](01-analysts.md): who reads the packets and what they produce
2. [The citation gate](02-citation-gate.md): the program that checks every citation
3. [Adversarial verification](03-verification.md): how insights are attacked before they're trusted
4. [Workflows](04-workflows.md): how the agents are orchestrated
5. [Running a mining job](05-running.md): the dashboard, headless Claude, cost and recovery
6. [Insights](06-insights.md): what's stored and how Claude uses it
7. [The candidate catalogue](07-catalogue.md): about 190 extra candidate kinds and the generic miner
8. [Enrichment and the Docker sandbox](08-enrichment-and-sandbox.md): exact history facts, measured coverage and mutation

Next: [Analysts](01-analysts.md)
