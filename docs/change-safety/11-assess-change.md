# `assess_change`: before an edit

> Before Claude edits code, it calls `assess_change` with the files or symbols it plans to touch. Oracle returns
> everything the change could affect, and a short list of what Claude **must** and **should** do.

## Why it matters

The cheapest time to avoid a mistake is before making it. If Claude knows up front that a function is public API,
that a file has a hidden partner, and that a fix once added a guard here, it plans the edit around them instead of
discovering them in review.

## How to call it

- **MCP:** `assess_change(paths=[...], symbols=[...], repo=..., working_dir=..., depth=2)`. Give `repo`, or
  `working_dir` so Oracle can identify the repository.
- **CLI:** `afd-oracle assess <repo> --path app/api.py` (repeat `--path` / `--symbol` as needed)
- **Dashboard:** the repository's **Risk** tab has an assess form

Paths are relative to the repository root. Symbols can be full uids, `path::Qual.name`, or qualified names.

## What it returns

| Section | Contents | From |
|---|---|---|
| `risk_level`, `max_risk_score` | low / medium / high | [Risk score](10-risk-score.md) |
| `targets` | The resolved symbols, each marked `public` and `deprecated` | [Public API](04-public-api.md) |
| `blast_radius` | Callers up to `depth` levels, production versus tests | [Call graph](03-call-graph.md) |
| `hidden_coupling` | Co-change partners (Jaccard ≥ 0.25) not linked by imports | [Co-change](../ingestion/07-co-change.md) |
| `contracts` | Keys this file produces or consumes, and the files on the other side | [String contracts](02-string-contracts.md) |
| `public_api` | Public symbols in the change | [Public API](04-public-api.md) |
| `tests` | Tests to run, ranked, with how each was linked | [Test map](01-test-map.md) |
| `hazards` | Existing block and warn findings in these files | Detectors 6–9 |
| `history` | Past fixes, zones, guards and regression tests per symbol | [Fix lineage](05-fix-lineage.md) |
| `insights` | Verified or proposed insights that cite these files or symbols | [Insights](../mining/06-insights.md) |
| `must` / `should` | Plain-language obligations | All of the above |

### Typical must and should items

- **Must:**
  - "Keep the public API compatible: fetch, Client.get"
  - "event `user.created` (producer here) is shared with: web/listener.ts"
  - "`render` is a security zone (1 past fixes): review against <fix hash> 'Fix XSS in render: escape user input'"
  - "Run the affected tests (3): start with tests/test_api.py::test_fetch_none"
  - "No test exercises this code: add tests that pin the current behaviour before changing it"
- **Should:**
  - "`web/forms.ts` changes with `app/schema.py` 3 times…"
  - "`render` is fragile (4 past fixes)"

## Example

In the safety fixture, `assess_change(paths=["app/api.py"])` returns:
- `risk_level: "high"`, because `fetch` and `Client.get` are public
- `tests/test_api.py::test_fetch_none` as a test to run
- `fetch`'s history: 1 fix, with `test_fetch_none` as its regression test

Assessing `app/schema.py` lists `web/forms.ts` as a hidden partner.

## Where in the code

`afd_oracle_ingest/safety/gate.py`: `Gate.assess_change` and its helpers (`callers`, `tests_for`, `hidden_partners`,
`contracts_for`, `history`, `insights`).

Next: [`check_diff`: after an edit](12-check-diff.md)
