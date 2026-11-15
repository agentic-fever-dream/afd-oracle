# Risk score

> Every source file and every function or class gets a score from 0 to 100: how careful you should be when changing
> it. The score combines how often the code changes, how complex it is, whether tests cover it, how often it was
> fixed, how many hazards it has, and how many people know it.

## Why it matters

The detectors produce many facts. A single number helps decide where to spend attention: the dashboard's Risk tab
ranks files by it, and `assess_change` uses it to set the overall risk level.

## How it works

### 1. Measure the factors

**Per file:**

| Factor | Measured as | Weight |
|---|---|---|
| churn | Lines added and deleted over history | 0.20 |
| complexity | Branch keywords (`if`, `for`, `while`, `except`, `and`, `or`, `?`…) | 0.15 |
| test_gap | 1 if no test is mapped to the file or its symbols, else 0 | 0.20 |
| fragility | Past fixes (from [lineage](05-fix-lineage.md)) | 0.20 |
| hazards | warn findings count 1 each, block findings count 3 | 0.15 |
| ownership | Share of commits by the top author (only with ≥ 5 commits): knowledge held by one person | 0.10 |

**Per function and class:**

| Factor | Weight |
|---|---|
| fan_in (callers) | 0.25 |
| complexity | 0.15 |
| test_gap | 0.20 |
| fragility | 0.25 |
| hazards | 0.15 |

### 2. Convert to percentiles

Raw values aren't comparable: churn might be 5,000 while fixes are 3. Each factor except `test_gap` is therefore
turned into a **percentile rank** within the repository. 0 is the calmest file, and 1 is the most extreme.

### 3. Weigh and add

```
score = 100 × Σ weight × rank          (test_gap uses its 0/1 value directly)
```

## How it is used

In `assess_change`, the risk level is:
- **high:** the max score is ≥ 70, or there is a block hazard, or public API is touched
- **medium:** the max score is ≥ 40, or there are production callers, or hidden partners
- **low:** otherwise

## Where in the code

`afd_oracle_ingest/safety/risk.py`: `WEIGHTS_FILE`, `WEIGHTS_SYM`, `build`.

## Limits

Percentiles are relative. In a very calm repository, *something* still scores high. Read the factors, not just the
number.

Next: [`assess_change`: before an edit](11-assess-change.md)
