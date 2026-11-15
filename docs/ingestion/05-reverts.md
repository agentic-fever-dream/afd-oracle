# Finding reverts

> A revert is a commit that undoes an earlier one. Oracle finds both kinds: **explicit** reverts that say so in their
> message, and **silent** reverts that quietly remove exactly what an earlier commit added.

## Why it matters

A reverted change is the clearest record of an idea that was tried and rejected. If Claude is about to add the same
thing, it should know that it was tried, when, and (ideally) why it failed. Reverts are the main input to the
**graveyard** miner.

## How it works

### Explicit reverts

`git revert` writes a message like `Revert "Add caching to dispatch"` followed by `This reverts commit 1a2b3c4d…`.
Oracle matches:
1. `This reverts commit <hash>`: the hash is resolved to a full commit in the store.
2. Otherwise, a subject of the form `Revert "<subject>"`, matched against the subject of an earlier commit.

### Silent reverts

Some people undo changes by hand. For each commit, Oracle builds a **line signature**: the set of
`(file, +/-, line text)` for every non-blank changed line. The signature of a perfect undo is the original with every
`+` turned into `-` and every `-` into `+`.

For a later commit Y and an earlier commit X:
- **Full revert:** Y's signature equals X's inverted signature.
- **Partial revert:** at least `revert_partial_overlap` (60%) of X's inverted lines appear in Y, *and* they make up at
  least half of Y's own lines. The second rule stops a large commit that happens to include an undo from counting.

### Filters that prevent false matches

Silent detection only compares commits that are:
- on the **main line**, not merges and not bulk commits
- not made by bots or formatters: authors or subjects containing `[bot]`, `dependabot`, `renovate` or
  `pre-commit.ci`, and subjects like "reformat", "lint", "black", "prettier", "ruff" and similar
- small, with at most `revert_max_files` (20) files changed
- touching overlapping paths
- within `revert_window_days` (180) of each other

Each revert is stored with its `method` (`message` or `signature`), whether it is `partial`, and the `overlap` ratio.
The reverting commit is flagged `is_revert`.

## Example

The fixture repository plants one of each:
- **Explicit:** "Add caching layer" (c06) is undone by `git revert` (c07). The message says
  `This reverts commit …`.
- **Silent:** "Clean up client" (c11) removes exactly the `with_retry` lines that "Add retry decorator to client"
  (c08) added. Their signatures are exact inverses, so Oracle records a silent full revert, even though no message
  mentions it.

## Where in the code

`afd_oracle_ingest/git/reverts.py`: `line_signature`, `invert`, `detect_reverts`. A `LOGIC_VERSION` setting forces a
full recompute when the rules change.

## Limits

- A revert that also reworks the code (an undo plus a rewrite) may fall below the 60% overlap threshold.
- A revert of a revert means the change came back. The graveyard packet builder drops these chains, because the code
  ended up shipping.

Next: [Classifying branches](06-branches.md)
