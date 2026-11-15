# The candidate catalogue

> Beyond coupling, graveyard and pattern candidates, Oracle runs about 190 small, deterministic **catalogue
> builders**. Each one looks for one well-documented pitfall of long-lived software: a configuration default that
> flipped, a migration edited after it shipped, a bug fix with no regression test, a retry without backoff, and so on.
> Every hit becomes a work packet that carries its own question for the analyst.

## Why it matters

Coupling and reverts are only two of the ways history records a lesson. Outage studies and repository-mining research
describe many more: misconfiguration is one of the biggest causes of production incidents, fix-on-fix chains predict
future defects, and fixes that never got a test tend to break again. Each catalogue entry turns one of those findings
into a cheap check over data Oracle already has, so an analyst only spends tokens where history points.

## How it works

1. **The line index.** A table, `diff_lines`, holds every added and removed line of every commit (first-parent diff,
   no context). It's built once and then updated incrementally, and it skips merges, bulk commits, ignored and binary
   files, and files with more than 1,500 changed lines. On flask it holds about 395,000 lines and takes about 25
   seconds to build. Builders search it with regular expressions instead of re-diffing history.
2. **Builders.** Each catalogue entry is a function registered with `@candidate("<id>")`. It reads the store (commits,
   symbols, test map, fix lineage, findings, contracts, co-change), the line index and file text at any commit, and
   yields **hits**: a stable subject, a score, a one-line title, compact evidence, and the commits, diffs, paths and
   symbols to cite. Builders never call a model.
3. **Noise filters.** Every reason a builder rejects something is counted under a name such as
   `fix_without_regression_test/test_in_same_commit`, the same way the [packet noise
   filter](../ingestion/10-work-packets.md) reports its drops.
4. **Ranking and caps.** Each kind keeps its top `catalog_max_per_kind` hits (default 40) by score. Hits are stored as
   candidates with ids `cat:<kind>:<hash>` and rebuilt on every ingest.
5. **Packets.** Each hit becomes a `catalog` work packet. The packet carries the entry's name, the pitfall, the
   **question** the analyst must answer, the **dismiss_if** conditions, the insight label to use, commit briefs and
   trimmed diffs. Packet priority comes from the entry's priority (P1 to P3) plus its score.
6. **Mining.** One generic workflow, `oracle-catalog`, judges every kind, because each packet says what to judge.
   Verification works exactly as for other insights: a separate skeptic tries to refute each one. `oracle-full` runs
   it alongside the other three miners, and a dashboard mining run with a cap interleaves catalogue kinds so it
   samples many different pitfalls.

## The families

| Family | Examples |
|---|---|
| Testing and coverage | Fix without a regression test, test deleted while the behaviour stayed, skip markers never removed, weakened assertions |
| Reliability and operations | Retry without backoff, emergency commit chains, swallowed exceptions that later hid a bug, timeouts added after hangs |
| Configuration | Default value flips, dead config keys, environment variable contract breaks, sibling environment configs that drifted |
| Infrastructure impact | Kubernetes resource and availability changes, probe drift, Terraform replacements, removed infrastructure guardrails |
| Data and schema | Locking DDL on hot tables, migrations edited after shipping, rollback-unsafe changes, ORM and migration drift |
| API compatibility | Breaking public API changes, deprecations never removed, exception hierarchy changes, stale callers |
| History mining | Fix-on-fix chains, SZZ bug-introducing commits, resurrected code, missing co-change partners |
| Legacy evolution | Lingering TODO/FIXME in hot code, clone siblings left unfixed, half-finished migrations, parameter accretion |
| Dependencies and build | Pins with a reason, dependency downgrades, CI gate weakening, hand-edited generated code |
| Security history | CORS and CSP changes, partial route-guard rollouts, CI privilege widening, PII in logs |
| Team and knowledge | Chesterton's-fence comments, keep-in-sync partners left unchanged, orphaned ownership |
| Concurrency, performance, observability | Lock churn, N+1 fixes, unbounded caches, log level flips, metric label cardinality |

The full list, with each entry's pitfall, signal, packet contents, question, noise filter and sources, is in
`afd_oracle_ingest/catalog/spec.json`.

## Example

On flask, `fix_without_regression_test` finds about 30 fixes that changed a function no strong test reaches, with no
test added in the same commit or by the same author within a week. Its noise filters dropped about 1,000 fixes to code
that no longer exists, 36 where the author added a test within a week, 13 already covered by a strong test link, and
12 that shipped a test in the same commit.

## Running it

- It runs on every ingest and update (setting `catalog`, on by default).
- `afd-oracle catalog <repo>` re-runs it and rebuilds packets. `--only <id>` runs single builders, `--list` shows every
  id and whether it's built, and `--rebuild-index` rebuilds the line index.

## Where in the code

- `afd_oracle_ingest/catalog/base.py`: `Hit`, `Ctx` (the shared read-only view), `@candidate`, path and commit
  classifiers
- `afd_oracle_ingest/catalog/difflines.py`: the line index
- `afd_oracle_ingest/catalog/runner.py`: runs builders, ranks, caps, stores candidates
- `afd_oracle_ingest/catalog/packets.py`: self-describing packets
- `afd_oracle_ingest/catalog/<family>.py`: the builders, one module per family
- `.claude/workflows/oracle-catalog.js`: the generic miner
- `tests/catalog/`: a planted mini-repository for every builder

## Limits

- Builders work from repository contents, plus measured coverage and mutation results when the
  [sandbox](08-enrichment-and-sandbox.md) has run. Signals that need production data (traffic, live configuration)
  use proxies; each builder's docstring says when.
- Many entries target ecosystems a given repository doesn't use (Kubernetes, Terraform, SQL migrations). They simply
  find nothing there.
- Some research sources for the entries were gathered by research agents and haven't been individually re-checked.

Next: [Enrichment and the Docker sandbox](08-enrichment-and-sandbox.md)
