"""History-mining builders: fix chains, SZZ, reverts/relands, churn spikes, co-change gaps, clones."""
from __future__ import annotations


class Plan:
    """Collect labelled commits, then replay them in chronological order."""

    def __init__(self, repo):
        self.r, self.items, self.h = repo, [], {}

    def add(self, label, days, files, msg, author="alice"):
        self.items.append((days, len(self.items), label, files, msg, author))

    def run(self):
        for days, _, label, files, msg, author in sorted(self.items, key=lambda t: (t[0], t[1])):
            if callable(msg):
                msg = msg(self.h)
            self.h[label] = self.r.commit(files, msg, days=days, author=author)
        return self.h


def parse_v(n):
    bodies = [
        '    parts = value.split(",")\n    cleaned = [p.strip() for p in parts]\n    return cleaned\n',
        '    if not value:\n        return []\n    parts = value.split(",")\n'
        '    cleaned = [p.strip() for p in parts if p]\n    return cleaned\n',
        '    if not value:\n        return []\n    parts = value.split(",") if value else []\n'
        '    cleaned = [p.strip() for p in parts if p.strip()]\n    return cleaned\n',
        '    if not value:\n        return []\n    parts = str(value).split(",")\n'
        '    cleaned = [p.strip() for p in parts if p.strip() != ""]\n    return cleaned\n',
        '    if not value:\n        return []\n    parts = str(value).split(";")\n'
        '    cleaned = [p.strip() for p in parts if p.strip() != "" and p]\n    return cleaned\n',
        '    if not value:\n        return []\n    parts = str(value).split(";")\n'
        '    cleaned = [p.strip().lower() for p in parts if p.strip() != "" and p]\n    return cleaned\n',
    ]
    return "def parse(value):\n" + bodies[n]


CALC0 = "def calc(items):\n    total = 0\n    for item in items:\n        total += item\n    return total\n"
CALC1 = """def calc(items):
    subtotal = sum(price_of(item) for item in items)
    discount = subtotal * discount_rate(items)
    shipping = shipping_cost(items, subtotal)
    taxable = subtotal - discount + shipping
    tax = taxable * tax_rate_for(items)
    rounded_tax = round(tax, 2)
    rounded_total = round(taxable + rounded_tax, 2)
    ledger_entry = build_ledger_entry(items, rounded_total)
    audit_trail = record_audit_trail(ledger_entry)
    summary_text = summarise_totals(rounded_total, rounded_tax)
    publish_total_event(summary_text, audit_trail)
    return rounded_total
"""
CALC2 = CALC1.replace("    taxable = subtotal - discount + shipping\n    tax = taxable * tax_rate_for(items)\n",
                      "    tax = subtotal * tax_rate_for(items)\n    taxable = subtotal - discount + shipping + tax\n"
                      ).replace("    rounded_total = round(taxable + rounded_tax, 2)\n",
                                "    rounded_total = round(taxable, 2)\n")

GUARD0 = "def load(x):\n    return x.data\n"
GUARD1 = 'def load(x):\n    if x is None:\n        raise ValueError("missing input value")\n    return x.data\n'


def test_fix_chains_szz_and_guards(mini):
    r = mini()
    p = Plan(r)
    p.add("init", 0, {"app/core.py": parse_v(0), "app/calc.py": CALC0, "app/guard.py": GUARD0}, "Initial import")
    for i, (d, who) in enumerate([(10, "alice"), (20, "bob"), (30, "carol"), (120, "alice"), (200, "bob")], 1):
        p.add(f"fix{i}", d, {"app/core.py": parse_v(i)}, f"Fix parse crash again {i}", who)
    p.add("origin", 100, {"app/calc.py": CALC1}, "Refactor calc into pricing and discount steps")
    p.add("calcfix", 140, {"app/calc.py": CALC2}, "Fix regression in calc: tax applied before discount")
    p.add("guardfix", 5, {"app/guard.py": GUARD1}, "Fix crash when x is None")
    p.add("guardrm", 40, {"app/guard.py": GUARD0}, "Simplify load")
    p.add("guardfix2", 90, {"app/guard.py": GUARD1.replace("missing input value", "missing value")},
          "Fix crash again when x is None")
    h = p.run()
    r.ingest()
    sym = f"sym:{r.name}:app/core.py:parse:function"

    hits = r.run("fix_on_fix_chain")
    hit = next(x for x in hits if x.subject == sym)
    assert set(hit.commits) >= {h["fix1"], h["fix2"], h["fix3"]}
    assert hit.evidence["lines_removed_that_earlier_fix_added"] >= 1

    hits = r.run("reopened_same_symbol_fix_cadence")
    hit = next(x for x in hits if x.subject == sym)
    assert hit.evidence["fix_count"] == 5 and hit.evidence["authors"] == 3
    assert h["fix5"] in hit.commits

    hits = r.run("szz_bug_introducing_commit")
    hit = next(x for x in hits if x.commits[1] == h["calcfix"])
    assert hit.commits[0] == h["origin"] and hit.evidence["share"] >= 0.5

    hits = r.run("regression_after_refactor")
    hit = next(x for x in hits if x.commits == [h["origin"], h["calcfix"]])
    assert hit.evidence["added_lines_in_refactor"] >= 10

    hits = r.run("removed_guard_then_regression")
    hit = next(x for x in hits if x.paths == ["app/guard.py"])
    assert hit.commits == [h["guardfix"], h["guardrm"], h["guardfix2"]]


def test_reverts_https_wip_resurrection_clone_churn(mini):
    r = mini()
    p = Plan(r)
    legacy = "\n".join(f"    step_{i} = compute_step_value({i}) * scale_factor_{i}" for i in range(12))
    export_full = f"def keep(v):\n    return v\n\n\ndef legacy_export(rows):\n{legacy}\n    return rows\n"
    export_keep = "def keep(v):\n    return v\n"
    big = "".join(f"value_{i} = compute_value_number_{i}(argument_{i})\n" for i in range(100))
    block = ("    response = session.get(url, timeout=timeout_seconds)\n"
             "    if response.status_code in RETRYABLE_STATUS_CODES:\n"
             "        time.sleep(backoff_seconds * attempt_number)\n        continue\n"
             '    payload = response.json().get("results", [])\n'
             "    return normalise_results(payload, strict=False)\n")
    p.add("init", 0, {"app/export.py": export_full, "a/handlers.py": "def base_a():\n    return 1\n",
                      "b/views.py": "def base_b():\n    return 2\n", "Dockerfile": "FROM python:3.11\nRUN pip install x\n",
                      "settings.py": "DEBUG = False\nSECURE_SSL_REDIRECT = False\n", "app/big.py": big},
          "Initial import")
    p.add("cloneA", 1, {"a/handlers.py": "def base_a():\n    return 1\n\n\ndef fetch_a(url):\n" + block},
          "Add retry wrapper")
    p.add("cloneB", 120, {"b/views.py": "def base_b():\n    return 2\n\n\ndef fetch_b(url):\n" + block},
          "Add retrying fetch to views")
    p.add("ssl_on", 3, {"settings.py": "DEBUG = False\nSECURE_SSL_REDIRECT = True\n"}, "Enable https redirect")
    p.add("ssl_off", 4, {"settings.py": "DEBUG = False\nSECURE_SSL_REDIRECT = False\n"},
          "Fix redirect loop behind load balancer")
    p.add("docs", 5, {"docs/deploy.md": "Set SECURE_SSL_REDIRECT = True in production\n"}, "Document ssl")
    p.add("d_try", 5, {"Dockerfile": "FROM python:3.12\nRUN pip install x\n"}, "try newer base image")
    p.add("d_again", 6, {"Dockerfile": "FROM python:3.11\nRUN pip install x\n"}, "try again with the old image")
    feat = ("def flags():\n    enabled = load_enabled_flags()\n    names = sorted(enabled)\n"
            "    return {name: True for name in names}\n")
    p.add("featC", 8, {"feat.py": feat}, "Add feature flags endpoint")
    p.add("featR", 9, {"feat.py": None},
          lambda h: f'Revert "Add feature flags endpoint"\n\nThis reverts commit {h["featC"]}.')
    p.add("featC2", 30, {"feat.py": feat + "\n\ndef cached_flags():\n    cache = {}\n    cache['flags'] = flags()\n"
                                          "    cache['stamp'] = 12345\n    return cache\n"},
          "Reapply feature flags endpoint with caching")
    p.add("bC", 12, {"bfile.py": "def bfunc():\n    return 'b value'\n"}, "Add b endpoint")
    p.add("bR", 13, {"bfile.py": None}, lambda h: f'Revert "Add b endpoint"\n\nThis reverts commit {h["bC"]}.')
    p.add("bRR", 14, {"bfile.py": "def bfunc():\n    return 'b value'\n"},
          lambda h: f'Revert "Revert Add b endpoint"\n\nThis reverts commit {h["bR"]}.')
    p.add("legacy_del", 10, {"app/export.py": export_keep}, "Drop legacy export")
    p.add("keepfix", 20, {"app/export.py": "def keep(v):\n    return v or 0\n"}, "Fix keep returning stale value")
    p.add("legacy_back", 250, {"app/export.py": "def keep(v):\n    return v or 0\n\n\ndef legacy_export(rows):\n"
                                              f"{legacy}\n    return rows\n"}, "Bring back legacy export for partners")
    for d in (100, 190, 280):
        p.add(f"small{d}", d, {"app/big.py": big.replace("value_5 =", f"value_5_{d} =", 1)}, f"Tweak big {d}")
    kept = "".join(big.splitlines(True)[:40])
    extra = "".join(f"extra_{i} = brand_new_helper_call_{i}(argument_{i})\n" for i in range(10))
    p.add("spike1", 400, {"app/big.py": kept}, "Remove most of big")
    p.add("spike2", 410, {"app/big.py": kept + extra}, "Rework big module", "bob")
    p.add("spikefix", 430, {"app/big.py": kept + extra.replace("extra_9", "extra_nine")},
          "Fix crash in big after rework")
    h = p.run()
    r.ingest()

    hits = r.run("revert_of_revert_or_reapplied_change")
    assert any(x.commits == [h["featC"], h["featR"], h["featC2"]] and x.evidence["delta_lines"] >= 3 for x in hits)
    assert any(x.commits == [h["bC"], h["bR"], h["bRR"]] for x in hits)

    hits = r.run("https_scheme_redirect_flip")
    assert [x.subject for x in hits] == ["settings.py"]
    assert r.ctx.noise.get("https_scheme_redirect_flip/docs") == 1

    hits = r.run("wip_trial_and_error_sequence")
    hit = next(x for x in hits if x.paths == ["Dockerfile"])
    assert hit.evidence["oscillations"] >= 1 and h["d_again"] in hit.commits

    hits = r.run("resurrected_deleted_code")
    hit = next(x for x in hits if "legacy_export" in x.subject)
    assert hit.commits[:2] == [h["legacy_del"], h["legacy_back"]] and h["keepfix"] in hit.commits

    hits = r.run("clone_late_propagation")
    assert any(x.paths == ["a/handlers.py", "b/views.py"] and x.evidence["shared_rare_lines"] >= 3 for x in hits)

    hits = r.run("relative_churn_spike")
    hit = next(x for x in hits if x.subject == "app/big.py")
    assert h["spikefix"] in hit.commits and hit.evidence["deleted"] >= 60


def test_cochange_hub_and_quiet_file(mini):
    r = mini()
    p = Plan(r)
    files = {"app/models.py": "X = 1\n", "app/serializers.py": "Y = 1\n",
             "quiet/quiet.py": "def total(v):\n    return v\n",
             "quiet/partner.py": "def compute_partner_total(v):\n    return v\n",
             "core/hub.py": "def hub(value):\n    return value + 1\n"}
    for k in range(4):
        for i in range(5):
            files[f"c{k}/m{i}.py"] = f"from core.hub import hub\n\n\ndef use_{k}_{i}():\n    return hub({i})\n"
    p.add("init", 0, files, "Initial import")
    for d in range(1, 7):
        p.add(f"co{d}", d, {"app/models.py": f"X = {d + 1}\n", "app/serializers.py": f"Y = {d + 1}\n"},
              f"Update schema {d}")
    p.add("m1", 20, {"app/models.py": "X = 20\n"}, "Add field x")
    p.add("s1", 21, {"app/serializers.py": "Y = 20\n"}, "Add field x to serializer")
    p.add("m2", 40, {"app/models.py": "X = 40\n"}, "Add field y")
    p.add("s2", 41, {"app/serializers.py": "Y = 40\n"}, "Add field y to serializer")
    for d in (101, 102, 103):
        p.add(f"q{d}", d, {"quiet/quiet.py": f"def total(v):\n    return v + {d}\n",
                           "quiet/partner.py": f"def compute_partner_total(v):\n    return v + {d}\n"},
              f"Tune totals {d}")
    for i, d in enumerate((200, 210, 220), 1):
        p.add(f"hubfix{i}", d, {"core/hub.py": f"def hub(value):\n    return value + {i + 1}\n"},
              f"Fix hub off by one {i}")
    p.add("ripple", 215, {"c1/m0.py": "from core.hub import hub\n\n\ndef use_1_0():\n    return hub(0) + 0\n",
                          "c2/m4.py": "from core.hub import hub\n\n\ndef use_2_4():\n    return hub(4) + 0\n"},
          "Adapt callers to hub change")
    p.add("partner_change", 500, {"quiet/partner.py": "def compute_partner_total(v):\n    return v * 2\n"},
          "Update partner defaults")
    p.add("quietfix", 520, {"quiet/quiet.py": "from quiet import partner\n\n\ndef total(v):\n"
                                              "    result = partner.compute_partner_total(v)\n"
                                              "    if result is None:\n        return 0\n    return result\n"},
          "Fix crash when partner returns None")
    h = p.run()
    r.ingest()

    hits = r.run("missing_cochange_partner")
    hit = next(x for x in hits if x.subject in ("app/models.py=>app/serializers.py",
                                                "app/serializers.py=>app/models.py"))
    assert len(hit.evidence["violations"]) >= 2

    hits = r.run("fix_prone_high_fan_in_hub")
    assert [x.subject for x in hits] == [f"sym:{r.name}:core/hub.py:hub:function"]
    assert hits[0].evidence["fan_in"] == 20

    hits = r.run("surprise_defect_fix_in_quiet_file")
    hit = next(x for x in hits if x.subject.startswith(h["quietfix"]))
    assert hit.evidence["partners"][0]["path"] == "quiet/partner.py"
    assert hit.evidence["partners"][0]["changed_in"] == h["partner_change"]


def _block(tag):
    return "".join(f"    value_{tag}_{i} = transform_input_value(payload, {i}, strict=True)\n" for i in range(4))


def test_szz_uses_blame_not_latest_identical_line(mini):
    r = mini()
    p = Plan(r)
    head = "VERSION_LABEL = 'one'\n\n\n"
    a_old = "def alpha(payload):\n" + _block("x") + "    return payload\n"
    a_fixed = "def alpha(payload):\n    return payload\n"
    beta = "\n\ndef beta(payload):\n" + _block("x") + "    return payload\n"
    # beta (added later) carries identical lines; a text match would blame beta's commit, blame blames alpha's.
    p.add("init", 0, {"app/mod.py": "VERSION_LABEL = 'one'\n"}, "Initial import")
    p.add("origin", 5, {"app/mod.py": head + a_old}, "Add alpha transform")
    p.add("later", 60, {"app/mod.py": head + a_old + beta}, "Add beta transform")
    p.add("fix", 100, {"app/mod.py": head + a_fixed + beta}, "Fix regression in alpha: drop the strict transform")
    h = p.run()
    r.ingest()
    hits = r.run("szz_bug_introducing_commit")
    hit = next(x for x in hits if x.commits[1] == h["fix"])
    assert hit.commits[0] == h["origin"] and hit.evidence["method"] == "blame"


def test_szz_falls_back_to_text_match_without_blame(mini):
    r = mini()
    p = Plan(r)
    body = "def calc(items):\n" + _block("y") + "    return items\n"
    p.add("init", 0, {"app/calc.py": "X = 1\n"}, "Initial import")
    p.add("origin", 5, {"app/calc.py": "X = 1\n\n\n" + body}, "Add calc")
    p.add("fix", 60, {"app/calc.py": "X = 1\n\n\ndef calc(items):\n    return items\n"},
          "Fix regression in calc: drop transform")
    h = p.run()
    r.ingest()
    r.ctx.store.conn.execute("DELETE FROM line_origins")
    r.ctx.store.conn.commit()
    hit = next(x for x in r.run("szz_bug_introducing_commit") if x.commits[1] == h["fix"])
    assert hit.commits[0] == h["origin"] and hit.evidence["method"] == "text-match"


def test_fix_chain_counts_lines_per_symbol(mini):
    r = mini()
    p = Plan(r)
    src = "def tiny(x):\n    return x + {a}\n\n\ndef big(x):\n{body}    return x\n"
    p.add("init", 0, {"app/two.py": src.format(a=0, body="    y = 0\n")}, "Initial import")
    for i in range(1, 4):
        body = "".join(f"    y{i}_{j} = x * {i + j}\n" for j in range(5))
        p.add(f"fix{i}", i * 6, {"app/two.py": src.format(a=i, body=body)}, f"Fix crash in tiny and big {i}")
    p.add("tail", 400, {"README.md": "x\n"}, "Docs")
    p.run()
    r.ingest()
    subj = {x.subject.split(":")[-2] for x in r.run("fix_on_fix_chain")}
    # a whole-file count (11+ lines per commit) would flag 'tiny' too; per symbol it changes 2 lines per fix
    assert "big" in subj and "tiny" not in subj
    assert r.ctx.noise.get("fix_on_fix_chain/tiny_fix", 0) >= 3


def test_resurrection_similarity_is_per_symbol(mini):
    r = mini()
    p = Plan(r)
    legacy = "\n".join(f"    step_{i} = compute_step_value({i}) * scale_factor_{i}" for i in range(12))
    full = f"def keep(v):\n    return v\n\n\ndef legacy_export(rows):\n{legacy}\n    return rows\n"
    noise = "".join(f"unrelated_{i} = build_unrelated_value_{i}(config_{i})\n" for i in range(80))
    p.add("init", 0, {"app/export.py": full}, "Initial import")
    p.add("del", 10, {"app/export.py": "def keep(v):\n    return v\n"}, "Drop legacy export")
    p.add("mid", 50, {"app/export.py": "def keep(v):\n    return v or 0\n"}, "Fix keep returning stale value")
    back = ("def keep(v):\n    return v or 0\n\n\ndef legacy_export(rows):\n    # restored for partners\n"
            f"{legacy}\n    return rows\n\n\n" + noise)
    p.add("back", 250, {"app/export.py": back}, "Bring back legacy export for partners")
    h = p.run()
    r.ingest()
    hit = next(x for x in r.run("resurrected_deleted_code") if "legacy_export" in x.subject)
    # the 80 unrelated lines added in the same commit would sink a file-level Jaccard
    assert hit.commits[:2] == [h["del"], h["back"]] and hit.evidence["similarity"] >= 0.85


def test_wip_overlap_uses_symbols_not_line_ranges(mini):
    r = mini()
    p = Plan(r)

    def mod(a, b, c):
        return (f"def alpha(x):\n    return x + {a}\n\n\ndef beta(x):\n    return x * {b}\n\n\n"
                f"def gamma(x):\n    return x - {c}\n")
    p.add("init", 0, {"app/trio.py": mod(0, 0, 0)}, "Initial import")
    # c1 touches alpha+gamma (range spans beta), c2 only beta, c3 alpha+gamma again
    p.add("c1", 1, {"app/trio.py": mod(1, 0, 1)}, "try a new offset")
    p.add("c2", 1.1, {"app/trio.py": mod(1, 1, 1)}, "try again with scale")
    p.add("c3", 1.2, {"app/trio.py": mod(2, 1, 2)}, "try once more")
    p.run()
    r.ingest()
    assert [x for x in r.run("wip_trial_and_error_sequence") if x.paths == ["app/trio.py"]] == []
    assert r.ctx.noise.get("wip_trial_and_error_sequence/no_overlapping_hunks", 0) >= 1


def test_clone_propagation_is_function_level(mini):
    r = mini()
    p = Plan(r)
    lines = [f"    outcome_{i} = reconcile_ledger_entry(entry, {i}, strict=True)\n" for i in range(4)]
    src = "def base():\n    return 1\n\n\ndef {name}(entry):\n" + "".join(lines) + "    return entry\n"
    p.add("init", 0, {"a/led.py": "def base():\n    return 1\n", "b/one.py": "def base():\n    return 2\n",
                      "c/two.py": "def base():\n    return 3\n"}, "Initial import")
    p.add("A", 1, {"a/led.py": src.format(name="reconcile_a")}, "Add reconcile a")
    p.add("B", 100, {"b/one.py": src.format(name="reconcile_b")}, "Add reconcile b")
    # C spreads the same four rare lines over four functions: file-level sharing, but no function-level clone
    spread = "".join(f"\n\ndef part_{i}(entry):\n{lines[i]}    pad_{i} = prepare_extra_padding_{i}(entry)\n"
                     f"    pad2_{i} = prepare_more_padding_{i}(entry)\n    pad3_{i} = prepare_last_padding_{i}(entry)\n"
                     f"    return entry\n" for i in range(4))
    p.add("C", 110, {"c/two.py": "def base():\n    return 3\n" + spread}, "Add spread parts")
    h = p.run()
    r.ingest()
    hits = r.run("clone_late_propagation")
    ab = next(x for x in hits if x.paths == ["a/led.py", "b/one.py"])
    assert ab.evidence["first_symbol"] == "reconcile_a" and ab.evidence["second_symbol"] == "reconcile_b"
    assert len(ab.symbols) == 2
    assert not any("c/two.py" in x.paths and h["C"] in x.commits for x in hits)




def test_reland_exact_patch_id_and_line_set_fallback(mini):
    r = mini()

    def body(n):
        return "".join(f"def metric_{n}_{i}(value):\n    return value * {i + 3} + offset_{i}\n\n\n" for i in range(4))

    init = r.commit({"app/base.py": "OFFSET = 1\n"}, "Init", days=0)
    r.commit({"app/other.py": "OTHER = 1\n"}, "Other work", days=0.5)
    c = r.commit({"app/m1.py": body(1), "app/m2.py": body(2)}, "Add metrics modules", days=1)
    rv = r.commit({"app/m1.py": None, "app/m2.py": None},
                  f'Revert "Add metrics modules"\n\nThis reverts commit {c}.', days=5)
    # the same change re-applied elsewhere (cherry-pick onto a branch cut before C): same patch-id, other base
    r.git("checkout", "-q", "-b", "hotfix", init)
    r.git("cherry-pick", c, days=20, author="bob")
    c2 = r.git("rev-parse", "HEAD")
    r.checkout("main")
    r.ingest()
    assert r.ctx.patch_ids[c] == r.ctx.patch_ids[c2]
    hits = r.run("revert_of_revert_or_reapplied_change")
    hit = next(x for x in hits if x.commits == [c, rv, c2])
    assert hit.evidence["exact_patch_id"] is True and hit.evidence["delta_lines"] == 0

    # fallback: without patch ids the identical re-application is not reported (line sets only catch changed ones)
    r.ctx.__dict__["patch_ids"] = {}
    r.ctx.__dict__["commits_by_patch_id"] = {}
    assert not [x for x in r.run("revert_of_revert_or_reapplied_change") if x.commits == [c, rv, c2]]


def test_wip_sequence_grouped_by_branch_not_author_window(mini):
    r = mini()
    r.commit({"app/svc.py": "def build():\n    return 'a'\n"}, "Init", days=0)
    r.branch("feature/flaky")
    states = ["b", "c", "b"]
    authors = ["alice", "bob", "alice"]
    msgs = ["Try b", "Try c again", "Back to b, still failing"]
    hs = [r.commit({"app/svc.py": f"def build():\n    return '{v}'\n"}, m, days=10 + 9 * k, author=a)
          for k, (v, a, m) in enumerate(zip(states, authors, msgs))]
    r.checkout("main")
    r.ingest()
    assert all(r.ctx.branch_of.get(h) == "feature/flaky" for h in hs)
    hit = next(x for x in r.run("wip_trial_and_error_sequence") if x.paths == ["app/svc.py"])
    assert hit.evidence["branch"] == "feature/flaky" and set(hs) <= set(hit.commits)

    # fallback: branch unknown -> authors differ and the commits are >7 days apart, so no sequence
    r.ctx.__dict__["branch_of"] = {}
    assert not [x for x in r.run("wip_trial_and_error_sequence") if x.paths == ["app/svc.py"]]
