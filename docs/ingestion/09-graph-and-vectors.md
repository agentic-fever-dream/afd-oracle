# The graph and the vectors

> Two extra copies of the store make different questions fast. **Neo4j** answers "what is connected to what".
> **Qdrant** answers "what is similar in meaning to this".

## Why it matters

The SQLite store is complete but flat. Some questions are natural on a graph, such as "files within two hops of this
one that changed in a revert". Others need search by meaning, such as "where is session expiry handled?" when you
don't know the function's name.

## The graph (Neo4j)

### What is projected

- **Nodes:** Repository, Directory, File, Commit, Author, Symbol (labelled Class / Function / Interface / Variable),
  Branch and Insight.
- **Edges:** CONTAINS, AUTHORED, PARENT, TOUCHED (commit → file or symbol), DEFINES, IMPORTS, CALLS, INHERITS,
  IMPLEMENTS, REVERTS, POINTS_TO, UNIQUE_TO, CO_COMMITTED, and the insight edges EVIDENCED_BY and CONCERNS.

### How it is written

- Writes go in batches (`UNWIND … MERGE`).
- Commits, authors and TOUCHED edges are written **incrementally**: only new commits.
- Derived layers (symbols, edges, reverts, branches and co-change) are deleted and rewritten for the repository on
  each ingest. They are cheap and always recomputed.
- Every node carries a `repo` property, so many repositories can share one database.

Claude queries the graph through the read-only `run_cypher` MCP tool.

## The vectors (Qdrant)

An **embedding** turns text into a list of numbers, so that texts with similar meaning get similar numbers. Oracle
embeds locally with `fastembed`: no API calls and no AI tokens. It uses two models:
- **Dense:** `jinaai/jina-embeddings-v2-base-code` (768 numbers), trained on code.
- **Sparse:** `Qdrant/bm25`, a keyword model, so exact names still match.

Search uses both (*hybrid* search).

| Collection | Contents |
|---|---|
| `code_symbols` | Living functions, classes and interfaces (kind, name, path, signature, docstring, body), plus deleted symbols that are graveyard candidates |
| `commit_diffs` | Each commit: message, files changed, symbol events |
| `architectural_insights` | Verified insights, added during mining |

Each point carries filters such as repo, path prefixes, kind, `alive` and commit hash.

Embedding is incremental. A symbol is re-embedded only when its body fingerprint or alive status changes, and a
commit only once. The first ingest of a large repository is the slowest step: flask's embedding took most of an hour
on a laptop CPU. Later updates take seconds.

### Idiom clusters

After embedding, Oracle checks every living function of five lines or more (up to 2,000) for near-duplicates, with
similarity ≥ 0.9. Functions that are near-duplicates of each other are grouped. A group of three or more spread across
three or more folders becomes an `idiom_cluster` candidate.

## Where in the code

- `afd_oracle_ingest/writers/neo4j_writer.py`: the projection
- `afd_oracle_ingest/embed.py`: embedding and idiom clusters
- `afd_oracle_core/vectors.py` and `embedding.py`: collections and models

## Limits

- Both databases need Docker running. Without them, `--no-graph --no-vectors` still gives a working store, and the
  change-safety gate does not need them.
- Embedding quality on very short or generated functions is low; idiom clusters are verified by mining before anyone
  relies on them.

Next: [Work packets and the noise filter](10-work-packets.md)
