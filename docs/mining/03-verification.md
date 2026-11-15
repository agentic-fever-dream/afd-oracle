# Adversarial verification

> Every proposed insight goes to a separate skeptical agent whose only job is to prove it wrong. It sees the claim
> and the citations, not the analyst's reasoning. If it can't confirm the claim from git itself, the insight is
> rejected.

## Why it matters

If you ask a model "is this right?", it tends to agree. If you ask it to *refute* a claim, and make refutation the
default, the bias works in your favour. Separating the verifier from the analyst also means a mistake in the
analyst's reasoning isn't simply repeated.

## The verifier

The agent definition `.claude/agents/oracle-verifier.md`:
- **Tools:** read-only Oracle tools, `verify_citations` and `set_insight_status`. It **cannot** create insights.
- **Procedure:**
  1. Load the insight, reading only its claim and citations.
  2. Run `verify_citations`. Any error means the insight is refuted.
  3. Open **every** cited commit (`get_commit_diff`) and the cited symbols' histories, and re-derive the claim
     independently.
  4. Refute if:
     - a citation doesn't show what the summary says
     - the claim generalises beyond the evidence (for example, "always" based on two commits)
     - a mundane explanation fits better
     - the stated "why" isn't supported by commit messages or follow-up commits
  5. **If uncertain, refute.** The default is `refuted = true`.

## What counts as a "mundane explanation"

Each workflow lists them for its kind:

| Kind | Refute if… |
|---|---|
| Coupling | A cited commit doesn't change both files as claimed; the pair is explained by an import or call, a test changing with its subject, generated or vendored code, lockfiles or a bulk format; or the "hidden contract" is asserted without diff evidence |
| Graveyard | The "introduced" commit didn't add what's claimed; the "reverted" commit doesn't undo it; the failure reason is invented; the work was later re-applied and shipped; or the symbol was merely renamed or moved |
| Pattern | The metrics don't match the graph; the god class is a cohesive data type or generated code; the cycle is type-only; the "leak" is sanctioned test access; or the idiom cluster is boilerplate |

## Votes and models

Verification is scaled to how much a wrong verdict would cost:

| Severity | Verifiers | Model |
|---|---|---|
| info, low | 1 | Sonnet |
| medium | 1 | Opus |
| high | 2 independent skeptics, both must fail to refute | Opus |

- **One verifier:** it is the *deciding* verifier, and calls `set_insight_status` itself.
- **Two verifiers:** each just returns a verdict. A small Sonnet "recorder" then writes the combined result. The
  insight is verified only if **every** vote says "not refuted".

`set_insight_status` stores the verdict, the verifier's notes and the votes (model, refuted, reason). It then:
- re-runs the [citation gate](02-citation-gate.md)
- updates the graph
- embeds **verified** insights into the `architectural_insights` vector collection, or removes rejected ones

## If a verifier fails

If an agent errors or is cancelled, the insight stays **proposed**. The `oracle-verify` workflow sweeps up such
leftovers later. Nothing is marked verified by default.

Next: [Workflows](04-workflows.md)
