# Tracking each symbol's life

> By comparing a file's symbols before and after each commit, Oracle writes a diary for every symbol: when it was
> **created**, each time it was **modified**, whether it **moved** to another file, and when it was **deleted**.

## Why it matters

- "When and why was this function added?" needs its creation commit.
- "Was this tried before?" needs to know about functions that existed and were later deleted. These deleted functions
  form the **graveyard**.
- "How fragile is this?" needs to know how often it was modified.

If moves were not detected, a refactor that relocated a module would look like hundreds of deletions and creations,
and wipe out every symbol's history.

## How it works

### Per commit: compare before and after

For each changed source file in a non-merge commit, Oracle loads the parse of the old version and the new version.
It then matches symbols by `(qualname, kind)`:

| Before | After | Event |
|---|---|---|
| Missing | Present | **created** |
| Present | Present, different `body_hash` | **modified** |
| Present | Present, file was renamed | **moved** (records `old_uid`) |
| Present | Missing | **deleted** |

### Same commit: moves between files

When one commit deletes a symbol in file A and creates a symbol with the **same body fingerprint, kind and name** in
file B, the pair becomes a single **moved** event. This catches "cut from here, paste there" refactors.

### Across commits: delayed moves

Restructuring is often split over several commits. For example, flask deleted `flask.py` in one commit and added the
`flask/` package in the next. Oracle links a main-line deletion to a re-creation of the same `(qualname, kind)` in a
different file **within 3 main-line commits**, but only when the match is unique on both sides. The creation becomes
`moved`, and the deletion becomes `moved_away`, so it is not counted as a removal. This step runs after the snapshot,
because it needs to know which commits are on the main line.

### The symbols table

Each symbol row stores:
- `first_seen` and `last_seen` commits and timestamps
- `modifications`, a count of edits
- `alive`, set by the [snapshot](04-snapshot.md)
- the latest body, signature and docstring
- `old_uid` when it was moved

## Example

In the fixture repository:
- `legacy_parse` is created (c01), modified (c04) and deleted (c13). Its diary is
  `created → modified → deleted`.
- `experimental_hash` is created (c14) and deleted (c15) three days later. That makes it a **short-lived symbol**
  candidate for the graveyard miner (see [Metrics](08-metrics.md)).

## Where in the code

- `afd_oracle_ingest/lifecycle.py`: `file_events`, `detect_moves`, `link_cross_commit_moves`
- `afd_oracle_ingest/history.py`: `_write_event` writes events and updates the symbols table

## Limits

- A symbol that is renamed *and* moved, or renamed in place, looks like a deletion plus a creation. The noise filter
  later drops "deleted" symbols whose name is still alive elsewhere (see [Work packets](10-work-packets.md)).
- Merge commits are skipped for symbol events, because their changes are already counted in the commits they merge.

Next: [The snapshot of today's code](04-snapshot.md)
