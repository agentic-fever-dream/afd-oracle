# GitHub discussions

> Commit messages are short. The real "why" behind a change often lives in its pull request or issue discussion.
> Oracle can optionally fetch PRs and issues from GitHub and link them to commits, so Claude can read the reasoning.

## Why it matters

"Why does this use a lock here?" may be answered by a 40-comment PR thread with the original bug report. Linking that
thread to the commit lets Claude answer from the source instead of guessing. The [verifier](../mining/03-verification.md)
also benefits: a "why" backed by a PR discussion is stronger evidence.

## How it works

1. **Opt in** per repository: the dashboard toggle, or `afd-oracle discussions <repo>`. It is off by default.
2. **Authenticate** with a token from `GITHUB_TOKEN` or `GH_TOKEN`, or from `gh auth token` if the GitHub CLI is
   logged in. Without a token, Oracle still tries anonymous requests, at GitHub's much lower rate limit. The sync
   reports whether it was authenticated and whether it was rate-limited.
   Repositories whose origin isn't on GitHub are skipped.
3. **Fetch** pull requests and issues through the GitHub REST API: title, body, state, labels, dates and comments.
   Syncs are **incremental**. Only items updated since the last sync (`discussions_synced_at`) are fetched.
4. **Link to commits:**
   - a merged PR's **merge commit**
   - main-line commits whose subject ends with `(#N)`: **squash merges**
   - commits whose message references `#N`, such as "fixes #12" or "closes #34"
5. **Store** in the `discussions` and `discussion_commits` tables.

## How it is used

- `get_commit_diff` includes the linked discussions alongside the diff.
- `search_discussions(repo, query)` searches titles and bodies.
- [Fix lineage](05-fix-lineage.md) and the verifier can read the reasoning behind a fix.

## Privacy

This is one of Oracle's few network features. It sends the repository's owner and name to the GitHub API, and stores
the discussion text locally. It never sends your code.

## Where in the code

`afd_oracle_ingest/safety/discussions.py`: `sync`, `link_commits`, `for_commit`, `search`.

Next: [Vulnerability scans](16-vulnerability-scans.md)
