# Incremental updates

> Running ingestion again processes only what changed since last time. The first ingest of a large repository takes
> a while; every update after it is quick.

## Why it matters

A knowledge base that is expensive to refresh goes stale, and stale facts are worse than none. Oracle reports how far
behind it is: `identify_repo` returns `commits_behind`. Catching up should be cheap enough that you just do it.

## How each step avoids repeated work

| Step | What makes it incremental |
|---|---|
| Clone | `git fetch` into the existing mirror |
| History | Skips known commits and stops at the ref tips processed last time (`processed_tips`) |
| Parsing | The parse cache is keyed by blob, so unchanged file versions are never re-parsed |
| Snapshot, moves, branches, co-change, metrics, safety | Fully recomputed from the store. Fast, because they read SQLite, not git |
| Reverts | Only commits newer than the previous run are checked as reverters (a full recompute happens if `LOGIC_VERSION` changed) |
| Neo4j | Commits are projected once; derived layers are rewritten |
| Qdrant | Re-embeds only symbols whose body fingerprint or alive status changed, and only new commits |
| Packets | Rebuilt, keeping each packet's status |
| Insights | Never touched: verified knowledge survives updates |

## How to update

- **Dashboard:** **Update** on the repository page.
- **CLI:** `afd-oracle update <repo>`, which uses the source recorded at ingest.
- **Full rebuild:** `--full` deletes the history tables and re-ingests from scratch. Insights are kept. Use it after
  changing ignore rules or detection thresholds.

## Where in the code

- `afd_oracle_ingest/pipeline.py`: `run_deterministic`, including `full`
- `afd_oracle_ingest/history.py`: `processed_tips`
- `afd_oracle_ingest/embed.py`: `embedded_hash`

Next: [Mining overview](../mining/README.md)
