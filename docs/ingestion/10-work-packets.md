# Work packets and the noise filter

> A work packet is one candidate plus all the evidence needed to judge it, trimmed to about 6,000 tokens. A noise
> filter first removes candidates a program can already recognise as uninteresting, so no AI time is spent on them.

## Why it matters

AI agents are expensive and slow at fetching evidence. Each tool call costs a round trip and tokens. If the program
gathers the evidence in advance, agents spend their effort on **judgement**, and every agent sees the same evidence.
That makes runs repeatable.

## Packet kinds

| Kind | Subkinds | Built from | Main evidence |
|---|---|---|---|
| `coupling` | – | Co-change pairs (cross-folder, unlinked first) | Metrics, both files' top-level symbols, shared commits with diffs of *both* files |
| `graveyard` | `revert`, `abandoned_branch`, `unmerged_branch`, `short_lived_symbol`, `churned_deleted` | Reverts, branches and graveyard candidates | Reverted and reverting commits with full messages, the reverted diff, follow-up commits; branch commits and diffstat; a symbol's event diary with its creation and deletion diffs |
| `pattern` | `god_class`, `cycle`, `leaky_abstraction`, `idiom_cluster` | Metric candidates | Metrics, member signatures, the cycle's edges, callers, recent history |
| `catalog` | One per [catalogue](../mining/07-catalogue.md) entry, about 190 | Catalogue builders | The entry's question and dismissal rules, the builder's evidence, commit briefs, trimmed diffs |

## How a packet is fitted to size

Each packet is built with diffs of up to 80 lines per file. If it exceeds `packet_max_tokens` (6,000, estimated as
characters ÷ 4), it is rebuilt with 40, then 20, then 8, then 0 diff lines. If it's still too big, it gets a note:
"trimmed to fit; use get_commit_diff for full patches".

## The noise filter

Every drop is counted under a named reason and reported, so a filtered pool is never mistaken for a complete one.
Turn the filter off with `AFD_ORACLE_PACKET_NOISE_FILTER=false`.

### Coupling pairs dropped

| Reason | Meaning |
|---|---|
| `explicit_link` | An import or call path of ≤ 2 hops already explains it |
| `test_with_subject` | `test_foo.py` changing with `foo.py` |
| `docs_lockstep` | Both are documentation |
| `changelog_lockstep` | One is CHANGES, CHANGELOG, HISTORY or NEWS |
| `rename_artifact` | Same file name, with one side deleted: it was a rename |
| `dependency_bumps` | Half or more of the shared commits are dependency or version bumps |

### Reverts and branches dropped

| Reason | Meaning |
|---|---|
| `no_tracked_files` | Only ignored files changed |
| `docs_only` | Only documentation changed |
| `tests_only` | Only tests changed |
| `trivial_change` | 2 lines or fewer changed |
| `version_bump` | A version bump of 8 lines or fewer |
| `dependency_bump` | A dependency bump |
| `duplicate_ref` | Another ref carries the same commits |
| `no_source_change` | Only config, CI or assets changed |

Two more rules apply to reverts:
- Chains where a revert was itself reverted are skipped, because the change shipped in the end.
- Rebased copies of the same change are kept only once.

### Deleted symbols dropped

| Reason | Meaning |
|---|---|
| `test_code` | The symbol was in a test file |
| `moved_not_removed` | A symbol with the same name and kind is still alive elsewhere |

## Priority, caps and status

- Each packet has a **priority** used for ordering.
- `--packet-limit` caps packets per kind: 200 by default, and `0` means no cap (the dashboard uses 0).
- A packet's **status** moves `open → claimed → done`. Rebuilding packets keeps each one's status. Packets whose
  candidate disappeared are deleted.

## Where in the code

`afd_oracle_ingest/packets.py`: `build_packets`, the per-kind builders, `fit`, and the noise rules.

Next: [Incremental updates](11-incremental.md)
