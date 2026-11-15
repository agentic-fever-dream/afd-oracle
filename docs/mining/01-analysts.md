# Analysts

> An analyst is a Claude agent (Sonnet) that reads a batch of five work packets and, for each one, either records a
> **proposed insight** with exact citations, or skips the packet and says why.

## Why it matters

The analyst is where judgement happens. Its instructions push hard toward caution. A wrong insight costs far more
than a missing one, because Claude will later rely on it when changing code.

## What an analyst is

The agent definition `.claude/agents/oracle-analyst.md`:
- **Model:** Sonnet. The packets already contain the evidence, so the work is reading and judging, not searching.
- **Tools:** read-only Oracle tools (`get_work_packets`, `get_commit_diff`, `get_symbol_history`, `run_cypher`,
  `search_qdrant`, `list_insights`), plus `upsert_insight` to record findings. It can't edit files or run commands.

## What it is asked to decide

| Packet kind | The question | Insight it may record |
|---|---|---|
| coupling | Is there a hidden contract between these files: a shared field list, enum, wire format or config key, logic duplicated by hand, or a required ordering? | `AntiPattern` / `hidden_coupling` |
| graveyard | What was tried, and does the evidence say *why* it was abandoned? | `RejectedAttempt` / `reverted_feature`, `abandoned_branch`, … |
| pattern | Is this god class, cycle, leak or cluster a real problem, or a deliberate idiom? | `AntiPattern` / `god_class`, `cycle`, `leaky_abstraction`, `duplicated_logic`; or `Pattern` / `idiom` |

## The rules it follows

- **Evidence over speculation.** If the packet doesn't support a conclusion, skip it.
- **Cite exactly.** Copy hashes, paths and symbol uids verbatim from the packet or a tool result. Never reconstruct
  them.
  - `commits`: each with a **role** (`introduced`, `reverted`, `evidence` or `fixed`)
  - `files`: each as `{path, at_commit}`
  - `symbols`: each as `{uid}`
- **Fix once, then give up.** If the [citation gate](02-citation-gate.md) rejects an insight, read the errors, fix
  the citations from real tool output and retry once. If it fails again, skip.
- **Plain words.** Titles of 120 characters or fewer. Summaries of 2,000 characters or fewer, saying what happens,
  why it matters and what the evidence shows.
- **Severity:**
  - `high`: likely to cause bugs or block change
  - `medium`: a recurring cost
  - `low`: worth knowing
  - `info`: a neutral pattern

## What it returns

A structured list of `{insight_id, severity, duplicate}` for each recorded insight, and `{packet_id, reason}` for
each skipped packet. The workflow uses this list to start verification.

## Example

For the fixture's `api/schema.py` ↔ `web/forms.ts` packet, the analyst sees five shared commits, each adding the
same field name to both files. It records the following:
- **Title:** "Signup fields are defined twice: Python validation and TS form must be edited together"
- **Label and kind:** `AntiPattern` / `hidden_coupling`
- **Severity:** `medium`
- **Citations:** the shared commits as `evidence`, both files at a commit, and `validate_schema`'s symbol uid

Next: [The citation gate](02-citation-gate.md)
