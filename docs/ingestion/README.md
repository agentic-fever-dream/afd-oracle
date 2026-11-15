# Ingestion overview

> Ingestion reads a repository's full git history and today's code, and computes everything a program can know about
> it. It uses no AI, gives the same result every time, and only processes what is new on later runs.

## What goes in, what comes out

- **In:** a git repository, either a URL (Oracle clones a mirror into `repos/<id>.git`) or a folder on disk.
- **Out:** a per-repository store (`data/repos/<id>/store.sqlite`) filled with:
  - commits and file changes
  - every symbol's life story
  - today's structure
  - reverts, branches, co-change pairs and metric candidates
  - the change-safety tables
  - a graph copy, vector embeddings and work packets for mining

## The steps, in order

Ingestion is run by `afd-oracle ingest` (or the dashboard's **Add repository** and **Update** buttons). The steps
run in this order because each one uses the results of the ones before it:

| # | Step | What it produces | Page |
|---|---|---|---|
| 0 | Clone (URL sources only) | A bare mirror in `repos/` | [Reading git history](01-git-history.md) |
| 1 | History | Commits, file changes, rename lineage | [Reading git history](01-git-history.md) |
| 2 | Parse | Symbols for every version of every source file | [Parsing code](02-parsing-code.md) |
| 3 | Symbol events | Created / modified / moved / deleted per symbol per commit | [Symbol lifecycle](03-symbol-lifecycle.md) |
| 4 | Snapshot | Which symbols are alive today; IMPORTS / CALLS / INHERITS edges; main-line flags | [Snapshot](04-snapshot.md) |
| 5 | Moves | Symbols moved between files across commits | [Symbol lifecycle](03-symbol-lifecycle.md) |
| 6 | Reverts | Which commits undid which | [Reverts](05-reverts.md) |
| 7 | Branches | Merged, unmerged or abandoned | [Branches](06-branches.md) |
| 8 | Co-change | File pairs that change together | [Co-change](07-co-change.md) |
| 9 | Metrics | God classes, import cycles, leaky abstractions, short-lived code, hotspots | [Metrics](08-metrics.md) |
| 10 | Safety | Every change-safety detector | [Change safety](../change-safety/README.md) |
| 10b | Catalogue | The line index, then about 190 catalogue builders | [The candidate catalogue](../mining/07-catalogue.md) |
| 11 | Graph | Neo4j projection | [Graph and vectors](09-graph-and-vectors.md) |
| 12 | Vectors | Qdrant embeddings, plus idiom clusters | [Graph and vectors](09-graph-and-vectors.md) |
| 13 | Packets | Evidence bundles for mining, after the noise filter | [Work packets](10-work-packets.md) |

Steps 1–10 write only to the SQLite store (`pipeline.run_deterministic`). Steps 11–13 build from the store and can
each be skipped (`--no-graph`, `--no-vectors`, `--no-packets`) or re-run on their own (`afd-oracle project`, `embed`,
`packets`).

## Ignoring files

Lock files, minified bundles and vendored code would drown out the signal. An `.oracleignore` file, in the repository
or the Oracle folder, lists glob patterns to skip. The default is:

```
*.lock  package-lock.json  *.min.js  node_modules/**  vendor/**  dist/**
```

A pattern without `/` matches a file name anywhere. Ignored files are still recorded (flagged `ignored`) but are never
parsed, counted or analysed.

## Progress reporting

With `--progress-json`, each step prints a line such as
`{"event":"progress","step":"history","done":800,"total":5400}`. The dashboard reads these lines to draw its progress
checklist and the percentage bar for embedding.

Next: [Reading git history](01-git-history.md)
