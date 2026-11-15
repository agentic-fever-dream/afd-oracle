# The citation gate

> A program, not an AI, checks every citation in an insight against git and the store. If any citation is wrong,
> the insight is refused and is never stored.

## Why it matters

The most dangerous AI error is a fabricated reference: a commit hash that looks real but isn't, or a file that
didn't exist at that commit. A claim backed by fake citations looks well-sourced and isn't. The gate makes this
impossible to store.

## The rules

An insight passes only if **all** of these hold:

1. **At least one commit**, each with a valid role (`introduced`, `reverted`, `evidence` or `fixed`).
2. **Every commit hash resolves.** It is a hex string of 7–40 characters, it matches exactly one commit in the store
   (a unique prefix), **and** that commit exists in the git object database.
3. **At least one file**, and every `{path, at_commit}` is real. The path exists in that commit's tree, or appears in
   that commit's diff (deletions and renames).
4. **Every symbol uid exists** in the store.
5. **The citations agree with each other.** At least one cited commit actually touched at least one cited file or
   symbol. This blocks the trick of citing a real commit next to an unrelated real file.

Paths are normalised before checking: backslashes become `/`, and a leading `./` or `/` is removed. Leading dots are
kept, because `.github/...` is a real path.

## When the gate runs

- **When an insight is created** (`upsert_insight`). If it fails, the agent gets
  `status: "invalid"` and a list of `{field, value, reason}` errors, and can correct and retry once.
- **When an insight is verified** (`set_insight_status(verified)`). The gate runs again. If reality has changed since
  then, for example because history was rewritten, a "verified" verdict becomes "rejected".
- **On demand**, through the `verify_citations` MCP tool, which the verifier calls first.

## Duplicates

Each insight gets a **dedupe key**: its kind plus the sorted set of cited file paths. If an insight with the same key
already exists, it isn't created again. The agent gets the existing id with `duplicate: true`, so re-running mining
doesn't fill the store with copies.

## Where in the code

- `afd_oracle_core/citations.py`: the rules
- `afd_oracle_core/insights.py`: `upsert`, `set_status` and dedupe

## Limits

The gate proves that citations are **real and connected**. It does not prove that the claim is **true**. That's the
job of [adversarial verification](03-verification.md).

Next: [Adversarial verification](03-verification.md)
