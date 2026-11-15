# Oracle documentation

These pages explain how Oracle works from the ground up. You don't need any background: every page defines its terms
or links to where they are defined, and each covers one idea.

## Reading order

Start with the foundations, then read whichever part you care about. Every page ends with a link to the next.

### 1. Foundations
1. [What Oracle is](01-what-oracle-is.md): the problem, and the idea in one page
2. [Key ideas](02-key-ideas.md): a glossary of git and code-analysis terms used everywhere else
3. [Architecture](03-architecture.md): the moving parts and how data flows between them

### 2. Ingestion: turning a repository into knowledge (no AI)
[Overview](ingestion/README.md), then:
1. [Reading git history](ingestion/01-git-history.md)
2. [Parsing code into symbols](ingestion/02-parsing-code.md)
3. [Tracking each symbol's life](ingestion/03-symbol-lifecycle.md)
4. [The snapshot of today's code](ingestion/04-snapshot.md)
5. [Finding reverts](ingestion/05-reverts.md)
6. [Classifying branches](ingestion/06-branches.md)
7. [Co-change: files that move together](ingestion/07-co-change.md)
8. [Structural metrics](ingestion/08-metrics.md)
9. [The graph and the vectors](ingestion/09-graph-and-vectors.md)
10. [Work packets and the noise filter](ingestion/10-work-packets.md)
11. [Incremental updates](ingestion/11-incremental.md)

### 3. Mining: turning candidates into verified insights (AI, with guard rails)
[Overview](mining/README.md), then:
1. [Analysts](mining/01-analysts.md)
2. [The citation gate](mining/02-citation-gate.md)
3. [Adversarial verification](mining/03-verification.md)
4. [Workflows: how the agents are orchestrated](mining/04-workflows.md)
5. [Running a mining job](mining/05-running.md)
6. [Insights: storage and lifecycle](mining/06-insights.md)
7. [The candidate catalogue](mining/07-catalogue.md)
8. [Enrichment and the Docker sandbox](mining/08-enrichment-and-sandbox.md)

### 4. Change safety: protecting a change before and after it is made (no AI)
[Overview](change-safety/README.md), then the detectors:
1. [Test map](change-safety/01-test-map.md)
2. [String contracts](change-safety/02-string-contracts.md)
3. [Type-aware call graph](change-safety/03-call-graph.md)
4. [Public API and breaking changes](change-safety/04-public-api.md)
5. [Bug-fix lineage](change-safety/05-fix-lineage.md)
6. [Security hazards](change-safety/06-security.md)
7. [Races and shared state](change-safety/07-races.md)
8. [Logic bombs and supply chain](change-safety/08-logic-bombs-and-supply-chain.md)
9. [Error handling and performance](change-safety/09-error-handling-and-performance.md)
10. [Risk score](change-safety/10-risk-score.md)

Then how they are applied:
11. [`assess_change`: before an edit](change-safety/11-assess-change.md)
12. [`check_diff`: after an edit](change-safety/12-check-diff.md)
13. [Mutation testing](change-safety/13-mutation-testing.md)
14. [GitHub discussions](change-safety/14-github-discussions.md)
15. [Vulnerability scans](change-safety/16-vulnerability-scans.md)
16. [Limits and honest expectations](change-safety/15-limits.md)

### 5. Using it
- [Using Oracle](using-oracle.md): the dashboard, Claude Code (skill, `/afd oracle`, MCP tools) and the CLI
