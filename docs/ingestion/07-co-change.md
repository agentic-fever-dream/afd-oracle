# Co-change: files that move together

> If two files keep being changed in the same commits, they probably depend on each other. When nothing in the code
> connects them, that dependency is hidden: changing one without the other is a classic way to break something far
> away.

## Why it matters

This is the main tool against **action at a distance**. Suppose `forms.py` and `templates/signup.html` always change
together because they share a list of field names. Nothing imports anything, but adding a field to one and not the
other breaks the signup page. Co-change reveals that pair.

## How it works

### 1. Pick eligible commits

Only commits that are **main line**, **non-merge** and **non-bulk**, and that touch **2 to `cochange_max_files` (30)**
non-ignored files, count. Very large commits connect everything to everything and carry no signal. Files are keyed by
[rename lineage](01-git-history.md), so renames don't split a pair's history.

### 2. Count

- `n(A)`: eligible commits that touch A
- `n(A,B)`: eligible commits that touch both

### 3. Measure similarity

```
jaccard = n(A,B) / (n(A) + n(B) − n(A,B))
conf_ab = n(A,B) / n(A)          "when A changes, how often does B?"
```

A pair is kept when `n(A,B) ≥ cochange_min_count` (3) and `jaccard ≥ cochange_min_jaccard` (0.15).

### 4. Score

```
score = jaccard
      × (1.0 if in different top-level folders else 0.4)
      × (0.3 if an import/call path of ≤ 2 hops connects them else 1.0)
      × (0.5 + 0.5 × recency)          recency = e^(−age_in_days / 365)
      × (0.3 if either file is deleted today)
      × (0.5 if neither file is source code)
```

- **Different folders** score higher, because coupling across modules is more surprising.
- An **explicit link** (`structural_link`) means the code already explains the pair, so it scores lower.
- **Recent** co-change matters more than co-change from ten years ago.

Each pair also stores up to 20 of the shared commits, newest first, as evidence.

## Example

In the fixture, `api/schema.py` (Python validation) and `web/forms.ts` (the TypeScript form) both list the signup
fields. Every "Add … field to signup" commit (email, phone, address, zip code, country) changes both. They sit in
different top-level folders and nothing imports anything, so the pair gets the top score. That is the planted
hidden coupling the tests check for.

## Where in the code

`afd_oracle_ingest/cochange.py`: `compute_cochange`, `structural_graph`.

## Limits

- Co-change is correlation. A test file changing with its subject, or a changelog changing with everything, is
  real co-change but not interesting. The [noise filter](10-work-packets.md) and the mining
  [verifier](../mining/03-verification.md) remove these.
- Only file pairs are scored. A rule shared by three files appears as three pairs.

Next: [Structural metrics](08-metrics.md)
