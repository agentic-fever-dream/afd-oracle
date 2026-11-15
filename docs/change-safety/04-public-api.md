# Public API and breaking changes

> Oracle works out which functions and classes are **public**, meaning meant for code outside the project, and
> records their signatures at HEAD and at recent release tags. When a change alters a public signature in a way that
> would break callers, it is a **block**.

## Why it matters

Inside a project, you can update every caller of a function. Outside it, you can't. Users of a library will break
on upgrade if a parameter disappears or becomes required. This is the clearest kind of **breaking change**.

## What counts as public

**Python:**
- The module's path has no `_private` part (`__init__.py` is fine), and isn't under `tests/`, `docs/`, `examples/`,
  `scripts/`, `benchmarks/`, `tools/` or `.github/`.
- Top-level names without a leading underscore. If the module defines `__all__`, only the names it lists.
- Methods of public classes, plus `__init__` and `__call__`.

**TypeScript and JavaScript:** symbols declared with `export`, and their methods.

**Deprecations:** a symbol whose body raises `DeprecationWarning` or `FutureWarning`, or that is marked `@deprecated`,
is recorded as deprecated.

The surface is stored for HEAD and for the newest `safety_api_tags` (6) release tags. On flask that is 314 public
symbols across 7 refs.

## How signatures are compared

Each parameter is parsed into a name, a kind (positional, keyword-only, `*args` or `**kwargs`) and a default value.
Comparing the old and new versions of a public symbol:

| Rule | Example | Severity |
|---|---|---|
| `api.removed` | A public function is deleted or renamed | **block** (warn if it was deprecated) |
| `api.kind_changed` | A function became a class, or the reverse | **block** |
| `api.param_removed` | `fetch(key, timeout)` → `fetch(key)` | **block** |
| `api.param_now_required` | `timeout=10` → `timeout` | **block** |
| `api.param_now_keyword` | `timeout` moved after `*` | **block** |
| `api.param_added_required` | A new parameter without a default | **block** |
| `api.default_changed` | `timeout=10` → `timeout=30` | warn |
| `api.param_reordered` | Positional parameters swapped | warn |
| `api.exception_changed` | The exceptions the function raises changed | warn |
| `api.return_changed` | The return annotation changed | info |

Adding an **optional** parameter at the end is compatible, and is not flagged.

## Example

In the safety fixture, `app/api.py` exports `fetch(key, timeout=10)`. Changing it to `fetch(key, timeout)` gives
`api.param_now_required` (**block**). `check_diff`'s next steps suggest keeping a default, or keeping the old name as
a deprecated alias.

## Where in the code

`afd_oracle_ingest/safety/api.py`: `is_api_path`, `public_symbols`, `deprecations`, `params`, `compare`, `classify`.

## Limits

- "Public" is a convention, not a guarantee. Some projects export private-looking names, and some internal modules
  are imported by users anyway.
- Behaviour changes that keep the same signature, such as returning different data, aren't caught here. Tests and
  [mutation testing](13-mutation-testing.md) cover those.

Next: [Bug-fix lineage](05-fix-lineage.md)
