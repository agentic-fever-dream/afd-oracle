# Classifying branches

> Every branch and pull-request ref is classified as **merged**, **unmerged** or **abandoned**, with a list of the
> commits that exist only on it.

## Why it matters

An abandoned branch is an idea that was started and then dropped. Like a revert, it is evidence of a rejected attempt.
Oracle needs to tell abandoned work apart from work that is merged, or merely in progress.

## How it works

1. **Unique commits.** For each ref, Oracle walks from its tip while hiding everything reachable from the default
   branch. What remains are the commits that exist only on that branch.
2. **Merged?** A branch with no unique commits is merged.
3. **Squash-merged?** GitHub's squash-merge creates a new commit on `main`, so the PR's original commits stay
   "unique" even though the work landed. Oracle collects every PR number mentioned in main-line subjects, in forms
   like `(#123)`, `pull request #123` and `!123`. A `refs/pull/123/head` whose number appears there is marked
   **merged** with source `squash_merge`.
4. **Abandoned?** An unmerged branch whose tip is older than `abandoned_days` (90) is **abandoned**. "Older" is
   measured against the newest commit in the repository, not today's date, so an old mirror still classifies
   correctly.
5. **Branches recovered from merge messages.** Deleted branches leave traces in merge messages such as
   `Merge branch 'feature-x'` or `Merge pull request #12 from user/feature`. These are recorded as merged branches
   with source `merge_message`, so the graph shows where work came from even after the branch is gone.

## Example

In the fixture repository:
- `experiment/graphql` has 2 unique commits ("Prototype GraphQL schema" and "Add GraphQL resolvers"), and its tip
  is more than 90 days older than the newest commit. It is classified **abandoned** and becomes an
  `abandoned_branch` graveyard packet.
- `feature/login` was merged with `--no-ff`, so it has no unique commits and is **merged**.

## Where in the code

`afd_oracle_ingest/git/branches.py`: `classify_branches`.

## Limits

- A branch merged by cherry-picking or a manual rebase, without a `(#N)` subject, still looks unmerged.
- Without `--fetch-pulls`, a mirror has only the branches pushed to the remote, so most pull-request history is
  missing.

Next: [Co-change: files that move together](07-co-change.md)
