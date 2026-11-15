# String contracts

> Many parts of a program are connected only by a shared string: an environment variable name, a config key, an
> event name, a URL. Oracle finds each **producer** (where a string is defined or emitted) and each **consumer**
> (where it is read or listened for), so renaming or removing one side can't silently break the other.

## Why it matters

These links are invisible to imports and call graphs. Rename the event `"user.created"` to `"user.made"` in the
emitter, and the listener in another file never fires again. No error, no failing import. This is the purest form
of action at a distance.

## The contract kinds

| Kind | Producer (defines, sets or serves) | Consumer (reads, calls or listens) |
|---|---|---|
| `env` | `.env` files, compose or CI `environment:` blocks, `os.environ[X] = …` | `os.environ[X]`, `os.getenv(X)`, `process.env.X` |
| `config` | `"KEY":` in a config defaults dict, `config["KEY"] = …`, `from_mapping(KEY=…)`, `config.update(KEY=…)`, upper-case constants in settings modules | `config["KEY"]`, `config.get("KEY")`, `app.config.KEY` |
| `event` | `emit` / `send` / `publish` / `dispatch("name")`, `signal("x")` | `on` / `connect` / `subscribe` / `addEventListener("name")` |
| `route` | `@app.route("/p")`, `router.get("/p")`, `add_url_rule` | `fetch("/p")`, `axios.get("/p")`, `client.get("/p")` |
| `endpoint` | The view function name under a route decorator | `url_for("name")` (also matches `"blueprint.name"`) |
| `sql_table` | `CREATE TABLE t` | `FROM` / `JOIN` / `INTO` / `UPDATE t` inside SQL strings |
| `registry` | `@x.register("k")`, `REGISTRY["k"] = …` | `REGISTRY["k"]`, `registry.get("k")` |
| `flag` | Defaults in config or flag files | `is_enabled("k")`, `feature_flag("k")` |
| `cli_flag` | `add_argument("--x")`, `click.option("--x")` | Scripts and docs that pass `--x` |

Routes are normalised (`/items/<int:id>` and `/items/:id` compare equal). Docstrings are skipped, so examples in
documentation aren't counted.

## What it flags

During ingestion, keys are grouped by `(kind, key)`:

| Rule | When it fires | Severity |
|---|---|---|
| `contract.orphan_consumer.<kind>` | Something reads the key, but nothing in the repository defines it | warn (info for `env` and `route`) |
| `contract.orphan_producer.<kind>` | An event, registry entry or flag is emitted, but nothing listens | info |

Well-known external environment variables (`PATH`, `HOME`, `GITHUB_*`, `PYTEST_*` and so on) are never orphans. A
route a client calls might belong to another service, so it is only info.

In [`check_diff`](12-check-diff.md):

| Rule | When it fires | Severity |
|---|---|---|
| `contract.producer_removed` | Your change removes the only definition of a key that other files still use | **block** |
| `contract.consumer_without_producer` | Your change adds a use of a key that nothing defines | warn (info for `env`) |

## Example

In the safety fixture:
- `app/events.py` emits `"user.created"`, and `web/listener.ts` handles it.
- Renaming the emit to `"user.made"` makes `check_diff` return `contract.producer_removed` (**block**), naming
  `web/listener.ts`.
- `SERVICE_URL` is read but never set, so it shows up as an orphan.

## Where in the code

- `afd_oracle_ingest/safety/contracts.py`: extraction and matching
- `safety/step.py`: orphan findings
- `safety/gate.py`: contract deltas in `check_diff`

## Limits

Contracts are found with patterns, not by running code. Keys built at runtime (`config[prefix + "_URL"]`) aren't
seen, and unusual frameworks may need new patterns.

Next: [Type-aware call graph](03-call-graph.md)
