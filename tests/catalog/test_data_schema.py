"""Tests for the data/schema catalogue family (builders in catalog/data_schema.py). Two planted repos, module-scoped."""
from __future__ import annotations

import pytest

from .conftest import MiniRepo


def _repo(tmp_path_factory, name):
    base = tmp_path_factory.mktemp(name)
    return MiniRepo(base / "repo", base / "data", name)


@pytest.fixture(scope="module")
def sql_repo(tmp_path_factory):
    r = _repo(tmp_path_factory, "sqlrepo")
    r.commit({"migrations/2023_01_create_orders.sql": "CREATE TABLE orders (\n  id integer primary key,\n  user_id integer\n);\n",
              "migrations/2023_01_create_items.sql": "CREATE TABLE items (\n  id integer primary key,\n  sku text\n);\n",
              "app/orders.py": "def load():\n    return 'select * from orders'\n",
              "app/items.py": "def load():\n    return 'select * from items'\n"}, "Add schema", days=0)
    r.commit({"migrations/2023_01_items_order.sql": "ALTER TABLE items ADD COLUMN order_id integer;\n"},
             "Link items to orders", days=2)
    r.commit({"migrations/2023_01_items_note.sql": "ALTER TABLE items ADD COLUMN note text;\n",
              "app/items.py": "def load():\n    return 'select note from items'\n"}, "Add note", days=3)
    r.commit({"migrations/2023_01_items_drop_note.sql": "ALTER TABLE items DROP COLUMN note;\n"}, "Drop note", days=5)
    r.commit({"migrations/2023_02_not_null.sql": "ALTER TABLE orders ADD COLUMN status text NOT NULL DEFAULT 'new';\n"},
             "Add status to orders", days=40)
    r.commit({"migrations/2023_02_lock_fix.sql": "ALTER TABLE orders ADD COLUMN note text;\n"},
             "Fix lock outage on orders migration", days=41)
    r.commit({"migrations/2023_02_safe_idx.sql": "CREATE INDEX CONCURRENTLY idx_o_status ON orders (status);\n"},
             "Index status safely", days=45)
    r.commit({"migrations/2023_03_drop_legacy.sql": "ALTER TABLE orders DROP COLUMN legacy_code;\n",
              "app/orders.py": "def load():\n    return 'select id from orders'  # legacy_code removed\n"},
             "Drop legacy_code from orders", days=60)
    r.commit({"migrations/2023_03_idx_a.sql": "CREATE INDEX idx_o_user ON orders (user_id);\n"}, "Index user", days=70)
    r.commit({"migrations/2023_03_idx_b.sql": "DROP INDEX idx_o_user;\n"}, "Drop user index", days=80)
    r.commit({"migrations/2023_03_idx_c.sql": "CREATE UNIQUE INDEX idx_o_user2 ON orders (user_id) WHERE active;\n"},
             "Partial unique user index", days=90)
    r.commit({"migrations/2023_04_price.sql": "ALTER TABLE items ADD COLUMN price numeric;\n"}, "Add price", days=100)
    r.commit({"migrations/2023_04_price_fix.sql": "ALTER TABLE items ALTER COLUMN price TYPE numeric(10,2);\n"},
             "Fix price column type", days=101)
    r.commit({"migrations/2023_01_create_orders.sql":
              "CREATE TABLE orders (\n  id integer primary key,\n  user_id integer,\n  total_cents integer\n);\n"},
             "Edit orders migration to add total", days=120)
    r.commit({"migrations/2023_01_create_items.sql":
              "-- items table\nCREATE TABLE items (\n  id integer primary key,\n  sku text\n);\n"},
             "Comment on items migration", days=122)
    r.commit({"migrations/2023_05_uq1.sql": "CREATE UNIQUE INDEX uq_items_sku ON items (sku);\n"}, "Unique sku", days=130)
    r.commit({"migrations/2023_05_uq2.sql": "CREATE UNIQUE INDEX uq_items_sku ON items (sku) WHERE deleted_at IS NULL;\n"},
             "Unique sku only for live rows", days=140)
    r.commit({"migrations/2023_06_bigint.sql": "ALTER TABLE orders ALTER COLUMN id TYPE bigint;\n"},
             "Fix orders id: integer overflow", days=150)
    r.commit({"alembic/versions/a1_drop_obsolete.py": "def upgrade():\n    op.drop_column('orders', 'obsolete')\n"},
             "Drop obsolete column", days=160)
    r.ingest()
    return r


def subjects(hits):
    return [h.subject for h in hits]


def test_locking_ddl(sql_repo):
    hits = sql_repo.run("locking_ddl_on_hot_table")
    assert any("orders|add_not_null_column" in h.subject for h in hits)
    h = next(h for h in hits if "add_not_null_column" in h.subject)
    assert h.evidence["later_fixes"] and h.commits[0] == h.diffs[0][0]
    assert not any("idx_o_status" in str(h.evidence) for h in hits)
    assert sql_repo.ctx.noise.get("locking_ddl_on_hot_table/safe_form", 0) >= 1


def test_migration_edited_after_commit(sql_repo):
    hits = sql_repo.run("migration_edited_after_commit")
    assert [h.evidence["migration"] for h in hits] == ["migrations/2023_01_create_orders.sql"]
    assert sql_repo.ctx.noise.get("migration_edited_after_commit/whitespace_or_comment_only") == 1


def test_rollback_unsafe(sql_repo):
    hits = sql_repo.run("rollback_unsafe_change")
    subs = subjects(hits)
    assert any(s.endswith("|orders|legacy_code|drop") for s in subs)
    assert any(s.endswith("|irreversible") for s in subs)
    assert not any("|items|note|" in s for s in subs)
    assert sql_repo.ctx.noise.get("rollback_unsafe_change/same_release_or_new_table", 0) >= 1
    legacy = next(h for h in hits if h.subject.endswith("|orders|legacy_code|drop"))
    assert legacy.evidence["same_commit_app_lines"] == 1


def test_index_cycle(sql_repo):
    hits = sql_repo.run("index_add_drop_readd_cycle")
    assert subjects(hits) == ["orders|user_id"]
    assert hits[0].evidence["definition_changed"] is True


def test_hotfix_migration(sql_repo):
    hits = sql_repo.run("migration_followed_by_hotfix_migration")
    assert [h.subject.split("|")[0] for h in hits] == ["items.price"]
    assert hits[0].evidence["fix_style_followups"] == 1


def test_unique_semantics(sql_repo):
    hits = sql_repo.run("unique_constraint_semantics_change")
    hits = [h for h in hits if h.evidence["table"] == "items"]
    assert len(hits) == 1 and hits[0].evidence["changed"] == ["predicate"]


def test_integer_width(sql_repo):
    hits = sql_repo.run("integer_width_downcast_fix")
    assert len(hits) == 1
    assert hits[0].evidence["overflow_message"] is True
    assert hits[0].evidence["narrow_siblings"][0]["column"] == "order_id"


# ---------------------------------------------------------------------------------------------- code repo
@pytest.fixture(scope="module")
def code_repo(tmp_path_factory):
    r = _repo(tmp_path_factory, "coderepo")
    base_models = ("from django.db import models\n\n\nclass Account(models.Model):\n"
                   "    state = models.CharField(max_length=5)\n"
                   "    userName = models.CharField(max_length=5, db_column='user_name')\n"
                   "    owner = models.ForeignKey(User, on_delete=models.PROTECT)\n"
                   "    team = models.ForeignKey(Team, related_name='t', on_delete=models.CASCADE)\n"
                   "    status = models.IntegerField(choices=[(s.value, s.name) for s in Status])\n")
    r.commit({"app/models.py": base_models,
              "app/status.py": "import enum\n\n\nclass Status(enum.Enum):\n    PENDING = 1\n    ACTIVE = 2\n\n\n"
                               "class Color(enum.Enum):\n    RED = 'red'\n",
              "app/billing.py": "from sqlalchemy import Column, Float\n\nprice = Column(Float)\n",
              "settings.py": "SESSION_SERIALIZER = 'django.contrib.sessions.serializers.PickleSerializer'\n"
                             "SECRET_KEY = 'abc-literal'\n",
              "app/views.py": "def view(qs):\n    return qs.all()\n",
              "scripts/backfill_users.py": "def run():\n    for u in User.objects.all():\n        u.save()\n",
              "scripts/backfill_single.py": "def run():\n    for u in User.objects.all():\n        u.save()\n",
              "shop/models.py": "from django.db import models\n\n\nclass Product(models.Model):\n"
                                "    name = models.CharField(max_length=5)\n",
              "shop/base.py": "from django.db import models\n\n\nclass Base(models.Model):\n"
                              "    a = models.CharField(max_length=5)\n\n    class Meta:\n        abstract = True\n",
              "shop/migrations/0001_create.py": "operations = []\n",
              **{f"app/q{i}.py": "rows = Order.objects.filter(a=1)\n" for i in range(1, 7)}}, "Initial code", days=0)
    r.commit({"app/models.py": base_models.replace(
        "    state = models.CharField(max_length=5)\n", "    status_code = models.CharField(max_length=5, db_column='state')\n"
    ).replace("    userName = models.CharField(max_length=5, db_column='user_name')\n",
              "    user_name = models.CharField(max_length=5, db_column='user_name')\n")}, "Rename fields", days=10)
    m2 = (r.root / "app/models.py").read_text()
    r.commit({"app/models.py": m2.replace("on_delete=models.PROTECT", "on_delete=models.CASCADE").replace(
        "related_name='t', on_delete=models.CASCADE", "on_delete=models.CASCADE, related_name='t'")},
        "Cascade owner deletes", days=30)
    r.commit({"app/status.py": "import enum\n\n\nclass Status(enum.Enum):\n    PENDING = 1\n    DONE = 2\n\n\n"
                               "class Color(enum.Enum):\n    RED = 'red'\n    BLUE = 'blue'\n"}, "Rework statuses", days=40)
    r.commit({"app/billing.py": "from sqlalchemy import Column, Numeric\n\nprice = Column(Numeric(10, 2))\n"},
             "Use exact price type", days=60)
    r.commit({"app/billing.py": "from sqlalchemy import Column, Numeric\n\nprice = Column(Numeric(10, 2))\n\n\n"
                                "def total(x):\n    return round(x, 2)\n"},
             "Fix penny rounding in invoice totals", days=62)
    r.commit({"settings.py": "SESSION_SERIALIZER = 'django.contrib.sessions.serializers.JSONSerializer'\n"
                             "SECRET_KEY = os.environ['SECRET_KEY']\n"}, "Switch session serializer", days=80)
    r.commit({"app/views.py": "def view(qs):\n    return qs.using('default').all()\n"},
             "Fix stale read after write: route to primary", days=90)
    r.commit({"scripts/backfill_users.py": "def run():\n    for u in User.objects.iterator(chunk_size=500):\n        u.save()\n"},
             "Backfill in chunks", days=100)
    r.commit({"scripts/backfill_users.py": "import time\n\n\ndef run():\n    for u in User.objects.iterator(chunk_size=500):\n"
                                           "        u.save()\n        time.sleep(0.1)\n"}, "Throttle backfill", days=101)
    r.commit({"scripts/backfill_users.py": "import time\n\n\ndef run():\n    for u in User.objects.iterator(chunk_size=500):\n"
                                           "        u.save()\n        time.sleep(0.1)\n    print('done')\n"},
             "Backfill logging", days=102)
    r.commit({"app/q1.py": "rows = Order.objects.filter(a=1, deleted_at__isnull=True)\n"},
             "Hide deleted orders in q1", days=110)
    r.commit({"shop/models.py": "from django.db import models\n\n\nclass Product(models.Model):\n"
                                "    name = models.CharField(max_length=5)\n    sku = models.CharField(max_length=10)\n"},
             "Add sku to product", days=120)
    r.commit({"shop/base.py": "from django.db import models\n\n\nclass Base(models.Model):\n"
                              "    a = models.CharField(max_length=5)\n    b = models.CharField(max_length=5)\n\n"
                              "    class Meta:\n        abstract = True\n"}, "Add b to base", days=122)
    r.commit({"shop/migrations/0002_add_sku.py": "operations = [\n    migrations.AddField(model_name='product', "
                                                 "name='sku', field=models.CharField(max_length=10)),\n]\n"},
             "Add missing migration for sku", days=125)
    r.ingest()
    return r


def test_legacy_persisted_name(code_repo):
    hits = code_repo.run("legacy_persisted_name_pinned")
    assert [h.evidence["old_name"] + ">" + h.evidence["new_name"] for h in hits] == ["state>status_code"]
    assert hits[0].evidence["pinned_to_old_name"] is True
    assert code_repo.ctx.noise.get("legacy_persisted_name_pinned/case_or_underscore_only", 0) >= 1


def test_cascade(code_repo):
    hits = code_repo.run("cascade_and_fk_rule_change")
    assert len(hits) == 1
    assert (hits[0].evidence["old_rule"], hits[0].evidence["new_rule"]) == ("PROTECT", "CASCADE")
    assert code_repo.ctx.noise.get("cascade_and_fk_rule_change/same_rule_reordered_args", 0) >= 1


def test_enum(code_repo):
    hits = code_repo.run("enum_value_persisted_change")
    assert len(hits) == 1
    assert "member_removed" in hits[0].evidence["changes"] and "value_reused" in hits[0].evidence["changes"]
    assert code_repo.ctx.noise.get("enum_value_persisted_change/pure_append", 0) >= 1


def test_float_money(code_repo):
    hits = code_repo.run("float_money_or_rounding_change")
    assert any(h.evidence.get("removed_tokens") == ["float"] and h.evidence["added_tokens"] == ["numeric(10, 2)"]
               for h in hits)
    assert any(h.subject.endswith("|message") for h in hits)


def test_signing(code_repo):
    hits = code_repo.run("signing_key_session_format_change")
    assert len(hits) == 1 and hits[0].evidence["settings_changed"] == ["SESSION_SERIALIZER"]
    assert "Pickle" not in str(hits[0].evidence) and "JSONSerializer" not in str(hits[0].evidence)
    assert code_repo.ctx.noise.get("signing_key_session_format_change/env_source_only", 0) >= 1


def test_read_after_write(code_repo):
    hits = code_repo.run("read_after_write_replica_fix")
    assert len(hits) == 1 and hits[0].paths == ["app/views.py"]


def test_backfill_scars(code_repo):
    hits = code_repo.run("backfill_job_rerun_scars")
    assert subjects(hits) == ["scripts/backfill_users.py"]
    assert set(hits[0].evidence["guards_added"]) == {"batch", "throttle"}
    assert code_repo.ctx.noise.get("backfill_job_rerun_scars/single_commit_job", 0) >= 1


def test_soft_delete(code_repo):
    hits = code_repo.run("soft_delete_filter_sibling_drift")
    assert len(hits) == 1 and hits[0].evidence["model"] == "Order"
    assert hits[0].evidence["sibling_sites_without_predicate"] >= 5


def test_orm_drift(code_repo):
    hits = code_repo.run("orm_model_migration_drift")
    assert len(hits) == 1 and hits[0].paths[0] == "shop/models.py"
    assert hits[0].evidence["field_changes"] == ["+sku"]
    assert code_repo.ctx.noise.get("orm_model_migration_drift/abstract_or_unmanaged", 0) >= 1


def test_rollback_unsafe_counts_head_symbols_that_still_name_the_column(mini):
    r = mini()
    r.commit({"migrations/001_orders.sql": "CREATE TABLE orders (\n  id integer primary key,\n  legacy_code text\n);\n",
              "app/orders.py": "def load():\n    return 'select id from orders'\n"}, "Add schema", days=0)
    r.commit({"migrations/002_drop.sql": "ALTER TABLE orders DROP COLUMN legacy_code;\n"}, "Drop legacy_code", days=30)
    r.commit({"app/orders.py": "def load():\n    return 'select id from orders'\n\n\ndef export_rows(row):\n    return row.legacy_code\n\n\n"
                               "def temp(row):\n    return row.legacy_code\n"}, "Report legacy_code", days=40)
    r.commit({"app/orders.py": "def load():\n    return 'select id from orders'\n\n\ndef export_rows(row):\n    return row.legacy_code\n"},
             "Remove temp", days=45)
    hits = r.run("rollback_unsafe_change")
    h = next(h for h in hits if h.subject.endswith("|orders|legacy_code|drop"))
    assert [u.split(":")[3] for u in h.evidence["head_symbol_references"]] == ["export_rows"]


def test_soft_delete_siblings_checked_per_function(mini):
    r = mini()
    plain = "rows = Order.objects.filter(a=1)\n"
    files = {f"app/q{i}.py": plain for i in range(1, 6)}
    files["app/checked.py"] = ("def live():\n    rows = Order.objects.filter(a=1)\n    return rows.filter(deleted_at__isnull=True)\n")
    files["app/gone.py"] = "def tmp():\n    return Order.objects.filter(a=2)\n"
    files["app/open.py"] = "def raw():\n    return Order.objects.filter(a=3)\n"
    r.commit(files, "Initial", days=0)
    r.commit({"app/q1.py": "rows = Order.objects.filter(a=1, deleted_at__isnull=True)\n"}, "Hide deleted orders in q1", days=10)
    r.commit({"app/gone.py": None}, "Remove tmp", days=11)
    hits = r.run("soft_delete_filter_sibling_drift")
    assert len(hits) == 1
    paths = {e["path"] for e in hits[0].evidence["examples"]}
    assert "app/checked.py" not in paths and "app/gone.py" not in paths
    assert r.ctx.noise["soft_delete_filter_sibling_drift/predicate_elsewhere_in_function"] >= 1
    assert r.ctx.noise["soft_delete_filter_sibling_drift/site_gone_at_head"] >= 1


# ---------------------------------------------------------------------------------------------- round-2 enrichment
def _fresh_ctx(r):
    from afd_oracle_ingest.catalog.base import Ctx
    r.ctx.store.conn.commit()
    return Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)


def _run_on(ctx, cid):
    from afd_oracle_ingest.catalog import load_all
    ctx._current = cid
    return list(load_all()[cid](ctx))


def test_legacy_name_found_from_class_field_symbols(mini):
    r = mini()
    r.commit({"m.py": "class Order:\n    state = Column('state', String)\n    other = 1\n"}, "Add order", days=0)
    r.commit({"m.py": "class Order:\n    status_code = Column('state', String)\n    other = 1\n"}, "Rename state", days=1)
    hits = r.run("legacy_persisted_name_pinned")
    assert [(h.evidence["old_name"], h.evidence["new_name"]) for h in hits] == [("state", "status_code")]
    assert hits[0].evidence["detected_via"] == "symbol" and hits[0].evidence["pinned_to_old_name"] is True


def test_legacy_name_falls_back_to_line_pairing(mini):
    r = mini()
    r.commit({"m.py": "state = Column('state', String)\n"}, "Add", days=0)
    r.commit({"m.py": "status_code = Column('state', String)\n"}, "Rename", days=1)
    hits = r.run("legacy_persisted_name_pinned")
    assert [(h.evidence["old_name"], h.evidence["new_name"]) for h in hits] == [("state", "status_code")]
    assert hits[0].evidence["detected_via"] == "lines"


_PRODUCT = "from django.db import models\n\n\nclass Product(models.Model):\n    name = models.CharField(max_length=5)\n"
_SKU = "    sku = models.CharField(max_length=10)\n"
_MIG = "operations = [\n    migrations.AddField(model_name='product', name='sku', field=models.CharField(max_length=10)),\n]\n"


def test_orm_drift_same_branch_is_same_pr(mini):
    r = mini()
    r.commit({"shop/models.py": _PRODUCT, "shop/migrations/0001_create.py": "operations = []\n"}, "Init", days=0, author="carol")
    r.branch("feat-sku")
    r.commit({"shop/models.py": _PRODUCT + _SKU}, "Add sku to product", days=1, author="alice")
    r.commit({"shop/migrations/0002_sku.py": _MIG}, "Add missing migration for sku", days=2, author="bob")
    r.checkout("main")
    r.ingest()
    assert set(r.ctx.branch_of.values()) == {"feat-sku"}          # unmerged branch: its commits are known
    assert r.run("orm_model_migration_drift") == []
    assert r.ctx.noise.get("orm_model_migration_drift/same_pr_chain", 0) == 1
    # same history without branch knowledge: authors differ, so the author proxy cannot excuse it
    r.ctx.store.conn.execute("DELETE FROM branch_commits")
    hits = _run_on(_fresh_ctx(r), "orm_model_migration_drift")
    assert len(hits) == 1 and hits[0].evidence["field_changes"] == ["+sku"]


def test_orm_drift_linked_pr_is_same_pr(mini):
    r = mini()
    r.commit({"shop/models.py": _PRODUCT, "shop/migrations/0001_create.py": "operations = []\n"}, "Init", days=0, author="carol")
    mig = r.commit({"shop/migrations/0002_sku.py": _MIG}, "Schema for sku", days=1, author="bob")
    model = r.commit({"shop/models.py": _PRODUCT + _SKU}, "Add sku to product", days=2, author="alice")
    r.commit({"shop/migrations/0003_sku2.py": _MIG.replace("sku", "sku")}, "Add missing migration for sku", days=5)
    r.ingest()
    assert len(_run_on(_fresh_ctx(r), "orm_model_migration_drift")) == 1      # no link: reported
    conn = r.ctx.store.conn
    conn.execute("INSERT INTO discussions (id, kind, number, title, body, state) VALUES ('pr7', 'pr', 7, 'Add sku', '', 'merged')")
    conn.executemany("INSERT INTO discussion_commits VALUES (7, ?)", [(mig,), (model,)])
    assert _run_on(_fresh_ctx(r), "orm_model_migration_drift") == []
