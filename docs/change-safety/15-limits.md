# Limits and honest expectations

> Change safety makes regressions, breaking changes and hidden-coupling mistakes much less likely. It cannot make
> them impossible. Here is what it can and can't promise.

## What it is good at

- **Mechanical, high-confidence checks.** Removed public parameters, deleted fix guards, removed contract producers,
  injection sinks and typosquats are caught reliably, and graded block.
- **Surfacing context.** Hidden partners, past fixes, zones, tests to run and relevant insights are facts a careful
  senior developer would check, delivered in seconds.
- **Precision over volume.** After tuning on flask, the baseline showed 7 warnings, all genuine. Tests and docs are
  graded info, and docstrings are skipped. Noisy tools get ignored.

## What it cannot do

| Gap | Why | What helps |
|---|---|---|
| Prove a change correct | Oracle doesn't execute your code (except during mutation testing) | Tests, [mutation testing](13-mutation-testing.md) |
| Follow data flow | Rules match patterns; they don't trace input to sink | A dedicated security scanner |
| See dynamic links | `getattr`, plugin registries, keys built at runtime, reflection | String contracts catch some; co-change catches others |
| Understand behaviour changes with the same signature | API checks compare signatures, not semantics | Tests; the test map tells you which to run |
| Know your runtime | Single-threaded modules still get race findings | Severity is lowered where it can tell |
| Cover other languages | Only Python and TypeScript/JavaScript are parsed | History and co-change still work for every file |
| Stay current by itself | Facts are as fresh as the last ingest | `identify_repo` reports `commits_behind`; **Update** is incremental |

## How to read a verdict

- **pass:** none of the known hazards were detected. It does *not* mean bug-free. Still run the listed tests.
- **warn:** a human should look. Each finding explains why.
- **block:** stop. Fix it, or accept it explicitly with a reason ("this API break is intended for v3.0").

## Tuning

Thresholds live in `afd_oracle_core/config.py` and can be overridden with `AFD_ORACLE_*` environment variables. For
example:
- `AFD_ORACLE_SAFETY=false` disables the safety step
- `AFD_ORACLE_SAFETY_TYPED_CALLS=false` skips Jedi
- `AFD_ORACLE_SAFETY_TYPED_CALLS_BUDGET_S=60` shortens its time budget

Back to the [documentation index](../README.md), or read [Using Oracle](../using-oracle.md).
