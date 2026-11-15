# Logic bombs and supply chain

> A **logic bomb** is code that behaves normally until some condition is met (a date, a hostname) and then does
> something harmful. **Supply-chain** attacks hide harm in dependencies or build scripts instead of the code itself.
> Oracle flags the tell-tale signs of both.

## Why it matters

These are rare, but the cost is catastrophic, and they are designed not to be noticed in review. Unlike most rules,
logic-bomb rules **also scan comments and docstrings**, because hiding things in plain sight is the point.

## Logic-bomb indicators

| Rule | Catches | Severity |
|---|---|---|
| `lb.encoded_exec` | Executing decoded content, such as `exec(b64decode(…))` or `eval(atob(…))` | **block** |
| `lb.date_gate` | A condition that compares the current date or time to a fixed date | warn (**block** in a new diff) |
| `lb.host_gate` | A condition on the hostname or user name (`gethostname()`, `getuser()`, `$USER`…) | warn (**block** in a new diff) |
| `lb.blob` | A long base64 or hex string literal (160+ characters) | info |
| `lb.dynamic_import` | `__import__(x)` or `import_module(x)` with a non-literal name | info |
| `lb.disabled_check` | `if False:`, `# nosec`, `eslint-disable`, `@ts-ignore` | info |
| `lb.test_skipped` | `@pytest.mark.skip`, `it.skip(`, `xit(` | info |
| `lb.destructive` | `rmtree`, `os.remove`, `rm -rf`, `DROP TABLE` and the like | info (warn in a new diff) |
| `lb.network` | Raw sockets, `urlopen`, `requests.get`, `fetch("https://…")` | info (warn in a new diff) |

Most of these are info on their own. The value comes from their **combination** in a diff: a date check next to an
`rmtree` is very different from either alone.

## Supply chain

These run in `check_diff` on changed dependency and build files.

| Rule | Catches | Severity |
|---|---|---|
| `sc.typosquat` | A new dependency whose name is one edit away from a popular package (`reqeusts` vs `requests`), the same name with dashes removed, or a popular name plus a suffix like `-js` | **block** |
| `sc.install_script` | A package install script added to `package.json` (`preinstall`, `install`, `postinstall`): it runs arbitrary code on every install | **block** |
| `sc.new_dependency` | Any new dependency | warn |
| `sc.hook_changed` | A change to files that run automatically: `setup.py`, `pyproject.toml`, `package.json`, `Makefile`, `Dockerfile`, `conftest.py`, CI workflows, git hooks, shell scripts | warn |

"One edit away" uses a Damerau-Levenshtein distance, so transpositions like `reqeusts` count as one edit.

**Optional vulnerability lookup.** `afd-oracle safety <repo> --osv` checks pinned dependency versions against the
public OSV vulnerability database (osv.dev). It's off by default, because it sends package names over the network.

## Example

The safety fixture plants `exec(b64decode(...))` and a date-gated `rmtree` in `app/maint.py`. In a test diff, adding
`reqeusts==2.31.0` to `requirements.txt` gives `sc.typosquat` (**block**) and `sc.new_dependency` (warn).

## Where in the code

- `afd_oracle_ingest/safety/rules.py`: the `lb.*` rules
- `safety/deps.py`: `lookalike`, `diff_findings`, `osv_check`

## Limits

A determined attacker can evade any pattern list. These checks raise the cost of the obvious attacks and make
suspicious changes visible to a human. They are not a guarantee.

Next: [Error handling and performance](09-error-handling-and-performance.md)
