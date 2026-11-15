# Parsing code into symbols

> Every version of every Python, TypeScript and JavaScript file is parsed into a list of **symbols** (classes,
> functions, methods, interfaces, module variables), each with its location, signature, docstring and a fingerprint
> of its body.

## Why it matters

Git sees lines. Developers think in functions and classes. Parsing lets Oracle say "`Flask.dispatch_request` was
changed 14 times" instead of "lines 700–760 of a file changed", and lets it tell when a function was created, edited,
moved or removed.

## How it works

1. **Choose a parser by file extension.** Oracle uses **tree-sitter**, a fast parser that produces a syntax tree even
   for code with errors. It supports Python, TypeScript, TSX and JavaScript. Other files are recorded in history but not
   parsed.
2. **Walk the syntax tree.** For each definition, the extractor records:
   - `qualname` (e.g. `Flask.wsgi_app`), `name` and `kind` (`class`, `interface`, `function` or `variable`)
   - start and end line
   - the `signature`: the definition line, with parameters
   - the `docstring`
   - the `body` (up to 6,000 characters) and a `body_hash`: a fingerprint of the body, used to tell whether it
     changed
   - `parent`: the enclosing class, for methods
   - `calls`: the names it calls, such as `foo(...)` or `.bar(...)`
   - `bases` and `implements`: what a class inherits from
   - imports, flagged `type_only` when they sit under `if TYPE_CHECKING:` (Python) or are written `import type` (TS).
     These only exist for type checking and have no runtime effect.
3. **Cache by blob.** Each parse is stored in `data/parse_cache.sqlite`, keyed by `(blob id, language)`. Many commits
   share the same file version, so each version is parsed only once, across every commit and every repository. The
   `EXTRACTOR_VERSION` setting is bumped whenever extraction logic changes, which invalidates the cache.
4. **Parse in parallel.** New blobs are parsed by a pool of worker processes (CPUs − 2 by default; `--workers`).
   Binary files are skipped: a NUL byte in the first 8 KB marks a file as binary.

## Example

```python
class Flask(App):
    def wsgi_app(self, environ, start_response):
        """The actual WSGI application."""
        ctx = self.request_context(environ)
        ...
```

This produces a `class` symbol `Flask` (bases `["App"]`) and a `function` symbol `Flask.wsgi_app`, with parent
`Flask`, its signature, its docstring and calls including `.request_context`. Its uid is
`sym:flask:src/flask/app.py:Flask.wsgi_app:function`.

## Where in the code

- `afd_oracle_ingest/ast/extractor.py`: records and the language registry
- `afd_oracle_ingest/ast/languages/`: one extractor per language
- `afd_oracle_ingest/ast/parse_cache.py`: the cache and the worker pool

## Limits

- Only Python and TypeScript/JavaScript are understood structurally. Other languages appear in history and co-change
  but have no symbols.
- Dynamically created functions (made with `setattr` or by decorators that generate code) are invisible to a parser.

Next: [Tracking each symbol's life](03-symbol-lifecycle.md)
