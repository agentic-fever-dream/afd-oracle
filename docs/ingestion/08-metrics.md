# Structural metrics

> Oracle computes several numbers that point at structural problems or interesting history, and records each match as
> a **candidate** for mining. Candidates are suspicions, not conclusions.

## Why it matters

These are the classic warning signs:
- a class that does everything
- modules that import each other in a circle
- private code used from outside
- code that was written and quickly thrown away

A program can find them cheaply. Judging whether each one is a real problem is left to [mining](../mining/README.md).

## The candidates

### God class
A class that has grown too big. Every living class gets these measurements:

| Measure | Meaning |
|---|---|
| methods | Number of methods |
| loc | Lines of code |
| fan_in | Calls into the class from outside it |
| fan_out | Calls from the class to outside it |
| churn | Distinct commits that touched the class or its members |
| authors | Distinct authors of those commits |

```
god_score = methods/20 + loc/500 + fan_in/20 + fan_out/30 + churn/50 + authors/5
```

A class becomes a candidate when it has **at least 15 methods or at least 400 lines**. The score ranks the
candidates.

### Import cycle
Files that import each other in a loop, such as A → B → C → A. Oracle builds a directed import graph and finds its
*strongly connected components*: groups where every file can reach every other. Any group of two or more files is a
cycle. **Type-only imports are excluded**, because they cannot cause import-order bugs. Without that rule, flask's
type-hint back-edges looked like a 19-module cycle.

### Leaky abstraction
Code that reaches past a boundary:
- a private member (`_name`) called from outside its class, unless the caller is a subclass
- a module-private function called from another module
- anything in an `internal/` package used from outside its parent
- a subclass overriding a private method of its base

### Short-lived symbol and churned-then-deleted
These are graveyard candidates. Only main-line events count, and a moved symbol keeps its original birth date.
- **Short-lived:** deleted within `short_lived_days` (30) or within `short_lived_commits` (10) commits of being
  created.
- **Churned then deleted:** modified at least `churned_deleted_min` (5) times, then removed. Someone kept working on
  it, then gave up.

### Hotspot
For each living file, `churn × log(1 + loc)`: large files that change a lot. The top 25 are kept.

### Idiom cluster
Computed later, during [embedding](09-graph-and-vectors.md): groups of near-identical functions spread over three or
more folders. These are either a deliberate pattern or copy-paste drift.

## Where the numbers go

- Candidates go to the `candidates` table: an id, kind, subject, score and properties.
- The headline numbers are also stored on symbols (`metrics`) and files (`metrics`), so tools can show them directly.

## Where in the code

`afd_oracle_ingest/metrics.py`: `compute_metrics`.

## Limits

- Thresholds are general rules of thumb. A 400-line class can be a perfectly cohesive data type; the
  [verifier](../mining/03-verification.md) is told to refute such cases.
- Fan-in and fan-out depend on how well calls were resolved (see [Snapshot](04-snapshot.md)).

Next: [The graph and the vectors](09-graph-and-vectors.md)
