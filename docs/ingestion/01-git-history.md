# Reading git history

> Oracle walks every commit reachable from any branch, tag or pull-request ref. For each one it records who changed
> which files, by how many lines, and where each file came from if it was renamed.

## Why it matters

Everything else is built on this record. Co-change counts commits, reverts compare commits, and the symbol lifecycle
follows a function through commits. If a rename broke a file's history in two, a file renamed five years ago would
look brand new.

## How it works

1. **Clone (URL sources only).** Oracle makes a *bare mirror*: a copy of the repository's database with no working
   files. With `--fetch-pulls` it also fetches GitHub's `refs/pull/*/head`, so unmerged pull requests can be studied
   as well.
2. **Find new commits.** Oracle starts from every ref tip (branches, remote branches, tags, pull requests) and walks
   backwards. It stops at commits already in the store, and at tips it already processed last time (kept in the
   store's `processed_tips` setting). A second run therefore only sees what's new.
3. **Read each commit** with `pygit2`, a Python binding to libgit2. Oracle records:
   - the hash, parents, author name and email, timestamp and message
   - whether it is a **merge** (two or more parents)
   - whether it is **bulk**: more than `bulk_files` (50) non-ignored files. Bulk commits are usually reformatting or
     mass renames, and are excluded from co-change and revert detection.
   - a `seq` number, the order Oracle processed it in, used to sort history cheaply
4. **Record file changes.** For each changed file Oracle stores:
   - the status: `A`dded, `M`odified, `D`eleted or `R`enamed
   - lines added and deleted
   - the old and new blob ids (a *blob* is git's stored copy of one file version)
5. **Track rename lineage.** Every path gets a **lineage** id. It is the path the file was first created with, and
   it is carried through every rename. If `flask.py` is renamed to `src/flask/app.py`, both paths share one lineage,
   so history and co-change follow the file rather than its name.
6. **Work in batches** of 400 commits. Each batch is parsed in parallel (see [Parsing code](02-parsing-code.md)) and
   written in one database transaction.

## Example

The fixture repository used by the tests (`tests/fixtures/build_fixture_repo.py`) renames `util/strings.py` to
`util/text.py` in commit c10. Both paths carry the lineage `util/strings.py`, so `slugify` keeps its creation date,
and the file keeps its co-change history, across the rename.

## Where in the code

- `afd_oracle_ingest/git/walker.py`: mirroring, ref tips, commit extraction, patches
- `afd_oracle_ingest/history.py`: the batch loop, lineage, and writing commits and file changes

## Limits

- Oracle reads history as git stores it. A force-pushed or rewritten history produces new commits; old ones stay in
  the store until a `--full` rebuild.
- Renames are detected by git's similarity heuristic. A file that is rewritten heavily while it is moved can show up as
  a delete plus an add.

Next: [Parsing code into symbols](02-parsing-code.md)
