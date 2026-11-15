# What Oracle is

> Oracle reads a repository's entire history and code structure, remembers it in a local database, and gives that
> memory to Claude Code. Claude can then understand *why* code is the way it is, and check that a change won't break
> something far away.

## The problem

When a developer, or an AI assistant like Claude, changes code, the code on screen tells only part of the story.
Important facts live elsewhere:

- **History.** Why does this odd `if` exist? It might be there because a bug was fixed three years ago. Delete it and
  the bug comes back.
- **Hidden links.** Two files in different folders always change together because they share an unwritten rule, such
  as the same list of form fields. Nothing in the code connects them, so changing one silently breaks the other.
- **Past attempts.** Someone already tried this exact idea, and it was reverted for a good reason.
- **Risk.** Some code is fragile, security-sensitive, run by many threads at once, or used by external callers.

A human expert holds these facts in their head after years on a project. An AI assistant starts every session from
scratch.

## The idea

Oracle computes these facts once, stores them, and lets Claude query them while it works. It has two halves.

1. **Deterministic analysis, which uses no AI.** Programs read every commit and parse every version of every file, and
   compute facts from them: which functions existed when, which files change together, which commits were reverted,
   which code is public API, which tests exercise what, where risky patterns are. This is fast, repeatable and free.
   See [Ingestion](ingestion/README.md) and [Change safety](change-safety/README.md).
2. **AI judgement, with guard rails.** Some questions need judgement. Is this pair of files *really* coupled, or is it
   a coincidence? Why was this feature abandoned? Claude agents examine small, pre-assembled bundles of evidence and
   write down findings. A program checks that every finding's citations point at real commits, files and symbols, and
   a second agent tries hard to prove each finding wrong. Only findings that survive are kept, as *verified insights*.
   See [Mining](mining/README.md).

A guiding rule runs through the design: **anything a program can compute is never sent to an AI.** Programs do the
counting, diffing and pattern-matching. AI is used only where judgement is genuinely needed, and even then its output
is checked.

## What you get

- **Answers about history and design:** "why is this like this?", "was this tried before?". Claude cites commits,
  files and verified insights instead of guessing.
- **A change-safety gate.**
  - Before Claude edits code, `assess_change` lists what the change could affect.
  - After the edit, `check_diff` inspects the actual diff for breaking changes, removed safety checks, broken
    cross-file contracts, and security, race and suspicious-code hazards, and names the tests to run.
- **A dashboard** for adding repositories, watching ingestion, running mining and browsing findings.

Everything runs on your machine. The only network traffic is optional:
- cloning repositories you choose
- syncing GitHub discussions, if you turn it on
- the Claude agents used for mining

Next: [Key ideas](02-key-ideas.md)
