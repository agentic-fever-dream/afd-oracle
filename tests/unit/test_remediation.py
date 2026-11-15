"""Fixes found on the first real-repository pass (flask): type-only imports, cross-commit moves, landing dates."""
from __future__ import annotations

import pygit2

from afd_oracle_core.store import Store
from afd_oracle_ingest.ast.extractor import language_for, parse_source
from afd_oracle_ingest.lifecycle import link_cross_commit_moves
from afd_oracle_ingest.structure import landing_points


def _imports(path: str, src: str) -> dict[str, bool]:
    fp = parse_source(language_for(path), src.encode())
    assert fp.error is None
    return {i.module: i.type_only for i in fp.imports}


def test_python_type_checking_imports_are_type_only():
    src = ("import os\n"
           "import typing as t\n"
           "from typing import TYPE_CHECKING\n"
           "if TYPE_CHECKING:\n"
           "    from .app import Flask\n"
           "if t.TYPE_CHECKING:\n"
           "    from .ctx import AppContext\n"
           "else:\n"
           "    from .fallback import Thing\n"
           "try:\n"
           "    import ujson\n"
           "except ImportError:\n"
           "    import json\n")
    got = _imports("pkg/mod.py", src)
    assert got[".app"] is True and got[".ctx"] is True
    assert got["os"] is False and got["typing"] is False and got[".fallback"] is False
    assert got["ujson"] is False and got["json"] is False


def test_typescript_import_type_is_type_only():
    got = _imports("web/a.ts", "import type { A } from './a'\nimport { B } from './b'\n")
    assert got == {"./a": True, "./b": False}


def _store(tmp_path, events, n_commits=6) -> Store:
    s = Store(tmp_path / "store.sqlite", "t")
    with s.tx() as conn:
        for i in range(n_commits):
            conn.execute("INSERT INTO commits(hash, seq, ts, is_merge, is_bulk, files_changed, on_main) "
                         "VALUES (?,?,?,?,?,?,1)", (f"c{i}", i, 1000 + i, 0, 0, 1))
        for h, uid, path, qual, change in events:
            conn.execute("INSERT INTO symbol_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (h, uid, path, qual, "class", change, None, None, None, 1000, int(h[1:])))
    return s


def test_cross_commit_move_is_linked_and_idempotent(tmp_path):
    s = _store(tmp_path, [("c1", "sym:t:flask.py:Module:class", "flask.py", "Module", "deleted"),
                          ("c2", "sym:t:flask/module.py:Module:class", "flask/module.py", "Module", "created")])
    assert link_cross_commit_moves(s) == {"cross_commit_moves": 1}
    rows = {r["hash"]: (r["change"], r["old_uid"]) for r in s.q("SELECT hash, change, old_uid FROM symbol_events")}
    assert rows == {"c1": ("moved_away", None), "c2": ("moved", "sym:t:flask.py:Module:class")}
    assert link_cross_commit_moves(s) == {"cross_commit_moves": 0}


def test_cross_commit_move_requires_unique_match_inside_window(tmp_path):
    s = _store(tmp_path, [
        # ambiguous: two re-creations of the same name inside the window
        ("c0", "sym:t:a.py:Thing:class", "a.py", "Thing", "deleted"),
        ("c1", "sym:t:b.py:Thing:class", "b.py", "Thing", "created"),
        ("c2", "sym:t:c.py:Thing:class", "c.py", "Thing", "created"),
        # too far apart (5 main-line commits > window of 3)
        ("c0", "sym:t:x.py:Far:class", "x.py", "Far", "deleted"),
        ("c5", "sym:t:y.py:Far:class", "y.py", "Far", "created"),
    ])
    assert link_cross_commit_moves(s) == {"cross_commit_moves": 0}


def test_landing_points_attribute_branch_commits_to_their_merge(fixture_repo):
    path, facts = fixture_repo
    repo = pygit2.Repository(str(path))
    head = str(repo.head.target)
    landed = landing_points(repo, head)
    merge = repo.get(facts["commits"]["m1_merge_login"])
    assert landed[str(merge.id)][0] == str(merge.id)
    side = str(merge.parents[1].id)  # tip of feature/login
    assert landed[side] == (str(merge.id), merge.commit_time)
    assert landed[head] == (head, repo.get(head).commit_time)
    abandoned = repo.references[f"refs/heads/{facts['abandoned_branch']}"].target
    assert str(abandoned) not in landed  # never reached the main line


def test_citation_paths_keep_leading_dots():
    from afd_oracle_core.citations import normalize_path
    assert normalize_path(".github/workflows/tests.yaml") == ".github/workflows/tests.yaml"
    assert normalize_path("./.pre-commit-config.yaml") == ".pre-commit-config.yaml"
    assert normalize_path(".\\src\\flask\\app.py") == "src/flask/app.py"
    assert normalize_path("/docs/index.rst") == "docs/index.rst"


def test_noise_filter_rules():
    from afd_oracle_ingest.packets import _change_noise, _is_changelog, _is_docs, _stem
    assert _is_docs("docs/patterns/fileuploads.rst") and _is_docs("README.md") and _is_docs("CHANGES.rst")
    assert not _is_docs("requirements/dev.txt") and not _is_docs("src/flask/app.py")
    assert _is_changelog("CHANGES.rst") and _is_changelog("docs/changelog.md") and not _is_changelog("src/flask/app.py")
    assert _stem("tests/test_cli.py") == _stem("src/flask/cli.py") == "cli"
    assert _stem("web/forms.test.ts") == "forms"
    assert _change_noise(["docs/index.rst", "README.md"], 40, "Improve docs") == "docs_only"
    assert _change_noise(["tests/test_basic.py"], 40, "Fix test") == "tests_only"
    assert _change_noise(["src/flask/app.py"], 1, "typo") == "trivial_change"
    assert _change_noise(["src/flask/__init__.py"], 2 + 2, "Bump version to 2.0.1") == "version_bump"
    assert _change_noise(["requirements/dev.txt", "requirements/docs.txt"], 60, "Bump the python-requirements group") \
        == "dependency_bump"
    assert _change_noise(["src/flask/sessions.py", "tests/test_basic.py"], 30, "reject str SECRET_KEY_FALLBACKS") is None
    assert _change_noise(["tox.ini", "requirements/dev.txt"], 40, "fix min python for pip-compile") is None
