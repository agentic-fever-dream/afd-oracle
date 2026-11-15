# Type-aware call graph

> The call graph says who calls each function. Oracle improves the basic name-based graph in two ways: Jedi
> type inference resolves calls the basic graph can't, and **override** links connect a base-class method to every
> subclass method that redefines it.

## Why it matters

The **blast radius** of a change is its callers, their callers, and so on. If the graph misses callers, the blast
radius looks smaller than it is, and Claude may change behaviour that something depends on.

Overrides matter in particular. Change `Shape.area()`'s contract, and every `Square.area()` that overrides it is
affected, even though nothing *calls* `Square.area` by that name.

## How it works

### 1. The base graph (from the snapshot)

The [snapshot](../ingestion/04-snapshot.md) resolves calls by name, imports and `self`. That's fast, but it is blind
to calls through variables, for example `client = make_client(); client.get(...)`.

### 2. Typed calls with Jedi (Python)

**Jedi** is the type-inference engine behind many Python editors' "go to definition".
1. Oracle writes the HEAD tree to a temporary folder (`data/tmp`).
2. For every call site in non-test Python code **whose callee name matches a symbol in the repository**, it asks Jedi
   where the callee is defined.
3. Each resolved call becomes a CALLS edge with confidence `typed`. An existing name-based edge is upgraded.

This runs within a time budget (`safety_typed_calls_budget_s`, 180 seconds), and the share of call sites covered is
reported. On flask it added or upgraded 292 edges. Turn it off with `AFD_ORACLE_SAFETY_TYPED_CALLS=false`.

### 3. OVERRIDES edges

When a subclass method has the same name as a method in one of its base classes (following INHERITS edges), Oracle
adds `subclass.method —OVERRIDES→ base.method`.

## How it is used

`assess_change` and `check_diff` walk the graph **backwards** from the changed symbols:
- following CALLS and OVERRIDES edges, up to `depth` levels (2 for `assess_change`, 1 for `check_diff`)
- also following a base method forward to its overrides

Each caller is reported with how it was reached (`CALLS`, `OVERRIDES` or `OVERRIDDEN_BY`), its depth, its
confidence, and whether it is a test.

## Example

In the safety fixture, `Square(Shape)` overrides `area()`. Assessing `Shape.area` lists `Square.area` as affected,
via `OVERRIDDEN_BY`.

## Where in the code

`afd_oracle_ingest/safety/callgraph.py`: `overrides`, `typed_calls`.

## Limits

- Python only. TypeScript and JavaScript use the name-based graph.
- Calls through `getattr`, plugin registries or dependency injection remain invisible. Some of these show up as
  [string contracts](02-string-contracts.md).

Next: [Public API and breaking changes](04-public-api.md)
