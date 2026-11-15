"""Tests for the infrastructure-impact catalogue family (planted histories, one repo per group of builders)."""
import pytest

API_V1 = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 4
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
        tier: web
    spec:
      containers:
        - name: api
          image: api:1
          resources:
            requests:
              memory: 256Mi
              cpu: 100m
            limits:
              memory: 256Mi
              cpu: 500m
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8000
            periodSeconds: 10
            timeoutSeconds: 5
          readinessProbe:
            httpGet:
              path: /ready
              port: 8000
"""
SVC = "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\nspec:\n  selector:\n    tier: web\n  ports:\n    - port: 80\n"
COMPOSE_V1 = """services:
  db:
    image: postgres:15
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
"""
TF_V1 = 'resource "aws_db_instance" "main" {\n  identifier = "prod-db"\n  engine = "postgres"\n  deletion_protection = true\n}\n'
ING_V1 = ("apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: web\n  annotations:\n"
          "    nginx.ingress.kubernetes.io/rewrite-target: /\nspec:\n  ingressClassName: nginx\n")
DOCKER_V1 = 'FROM python:3.12\nCOPY . /app\nCMD ["flask", "run", "--host=127.0.0.1"]\n'


@pytest.fixture
def infra(mini):
    r = mini()
    r.commit({"settings.py": "TRUSTED_PROXIES = 1\n", "infra/main.tf": TF_V1, "deploy/api.yaml": API_V1,
              "deploy/svc.yaml": SVC, "docker-compose.yml": COMPOSE_V1, "deploy/ingress.yaml": ING_V1,
              "config/retention.yaml": "backup_retention_period: 30\n", "Dockerfile": DOCKER_V1,
              "nginx.conf": "location / {\n    proxy_buffering on;\n}\n",
              "app/stream.py": "def events():\n    return 'text/event-stream'\n",
              "app/views.py": "from flask import session\n\n\ndef me():\n    return session.get('u')\n",
              "app/clients.py": "import requests\n\n\ndef fetch(u):\n    s = requests.Session()\n    return s.get(u)\n",
              "dev/docker-compose.dev.yml": COMPOSE_V1.replace("127.0.0.1:", "")}, "Initial infra", days=0)
    s = {}
    s["proxy"] = r.commit({"settings.py": "TRUSTED_PROXIES = 2\n"}, "Trust two proxies", days=1)
    s["proxyfix"] = r.commit({"settings.py": "TRUSTED_PROXIES = 2\nX = 1\n"}, "Fix wrong client ip behind proxy", days=2)
    tf2 = TF_V1.replace('"main"', '"primary"')
    s["tf_rename"] = r.commit({"infra/main.tf": tf2}, "Rename db resource", days=3)
    tf3 = tf2.replace("postgres", "mysql")
    s["tf_engine"] = r.commit({"infra/main.tf": tf3}, "Switch engine", days=4)
    s["guard"] = r.commit({"infra/main.tf": tf3.replace("= true", "= false")}, "Allow terraform to delete db", days=5)
    v2 = (API_V1.replace("replicas: 4", "replicas: 1").replace("periodSeconds: 10", "periodSeconds: 5")
          .replace("timeoutSeconds: 5", "timeoutSeconds: 1").replace("path: /ready", "path: /healthz"))
    s["avail"] = r.commit({"deploy/api.yaml": v2}, "Save cost: single replica", days=6)
    v3 = v2.replace("memory: 256Mi", "memory: 512Mi")
    s["mem1"] = r.commit({"deploy/api.yaml": v3}, "Fix OOM kill under load", days=7)
    v4 = v3.replace("memory: 512Mi", "memory: 1Gi")
    s["mem2"] = r.commit({"deploy/api.yaml": v4}, "Raise memory again", days=8)
    s["label"] = r.commit({"deploy/api.yaml": v4.replace("        tier: web\n", "")}, "Drop tier label", days=9)
    c2 = COMPOSE_V1.replace("pgdata", "dbdata")
    s["vol"] = r.commit({"docker-compose.yml": c2}, "Rename db volume", days=10)
    s["port"] = r.commit({"docker-compose.yml": c2.replace('"127.0.0.1:5432:5432"', '"5432:5432"')}, "Expose db", days=11)
    ing2 = ING_V1.replace("rewrite-target: /", "rewrite-target: /$2").replace(
        "    nginx.ingress", '    nginx.ingress.kubernetes.io/use-regex: "true"\n    nginx.ingress', 1)
    s["ing"] = r.commit({"deploy/ingress.yaml": ing2}, "Regex ingress paths", days=12)
    s["ret"] = r.commit({"config/retention.yaml": "backup_retention_period: 7\n"}, "Cut backups", days=13)
    d2 = DOCKER_V1.replace("127.0.0.1", "0.0.0.0")
    s["listen"] = r.commit({"Dockerfile": d2}, "Listen on all interfaces", days=14)
    s["user"] = r.commit({"Dockerfile": d2.replace("CMD", "USER app\nCMD")}, "Run as non-root", days=15)
    s["home"] = r.commit({"Dockerfile": d2.replace("CMD", "USER app\nENV HOME=/tmp\nCMD")}, "Set HOME", days=16)
    s["buf"] = r.commit({"nginx.conf": "location / {\n    proxy_buffering off;\n}\n"}, "Disable buffering", days=17)
    s["cache"] = r.commit({"app/views.py": "from flask import session\n\n\ndef me(resp):\n"
                                           "    resp.headers['Cache-Control'] = 'public, max-age=60'\n"
                                           "    return session.get('u')\n"}, "Cache profile page", days=18)
    s["client"] = r.commit({"app/clients.py": "import requests\n\nSESSION = requests.Session()\n\n\n"
                                              "def fetch(u):\n    return SESSION.get(u)\n"},
                           "Fix connection churn: reuse client session", days=19)
    r.ingest()
    r.s = s
    return r


def hashes(hits):
    return {c for h in hits for c in h.commits}


def test_proxy_trust(infra):
    hits = infra.run("trusted_proxy_trust_setting_change")
    assert [h.subject.split("|")[0] for h in hits] == [infra.s["proxy"]]
    assert infra.s["proxyfix"] in hits[0].commits


def test_terraform_and_guardrail(infra):
    hits = infra.run("terraform_replace_forcing_change")
    assert infra.s["tf_rename"] in hashes(hits) and infra.s["tf_engine"] in hashes(hits)
    g = infra.run("iac_guardrail_removal")
    assert [h.commits[0] for h in g] == [infra.s["guard"]] and g[0].evidence["flipped"] == ["deletion_protection"]


def test_k8s_family(infra):
    av = infra.run("k8s_availability_knob_reduction")
    assert [h.commits[0] for h in av] == [infra.s["avail"]]
    ch = infra.run("k8s_resource_limit_churn")
    assert [h.subject for h in ch] == ["deploy/api.yaml"] and infra.s["mem1"] in ch[0].commits
    pr = infra.run("probe_and_healthcheck_drift")
    assert infra.s["avail"] in hashes(pr)
    im = infra.run("k8s_immutable_or_deprecated_change")
    assert any(h.commits[0] == infra.s["label"] and "tier" in h.evidence["notes"][0] for h in im)


def test_compose_and_container(infra):
    ps = infra.run("persistent_state_mount_change")
    assert [h.commits[0] for h in ps] == [infra.s["vol"]] and "pgdata" in ps[0].evidence["name_absent_at_head"]
    ls = infra.run("listen_address_scope_flip")
    assert {h.subject.split("|")[2] for h in ls} == {"bind", "port"}
    assert {h.commits[0] for h in ls} == {infra.s["listen"], infra.s["port"]}
    cu = infra.run("container_user_readonly_fs_change")
    assert [h.commits[0] for h in cu] == [infra.s["user"]] and infra.s["home"] in cu[0].commits


def test_misc(infra):
    ing = infra.run("ingress_annotation_semantics_change")
    assert [h.commits[0] for h in ing] == [infra.s["ing"]]
    ret = infra.run("retention_and_backup_reduction")
    assert [h.commits[0] for h in ret] == [infra.s["ret"]]
    buf = infra.run("proxy_buffering_streaming_upgrade_change")
    assert [h.commits[0] for h in buf] == [infra.s["buf"]]
    hc = infra.run("http_cache_header_policy_change")
    assert [h.commits[0] for h in hc] == [infra.s["cache"]]
    pc = infra.run("per_request_client_construction")
    assert [h.commits[0] for h in pc] == [infra.s["client"]]


def test_ci_path_filter_gap(mini):
    r = mini()
    wf = ("name: tests\non:\n  push:\n    paths:\n      - 'src/**'\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - run: pytest\n")
    r.commit({".github/workflows/test.yml": wf, "src/a.py": "x = 1\n"}, "Init", days=0)
    c = r.commit({f"lib/core/m{i}.py": "y = 1\n" for i in range(3)}, "Add lib core", days=1)
    r.commit({"docs/a.md": "hi\n", "lib/readme.md": "doc\n"}, "Docs", days=2)
    hits = r.run("ci_path_filter_new_dir_gap")
    assert [h.subject for h in hits] == ["gap|lib/core"] and hits[0].commits[0] == c
    r2 = mini()
    r2.commit({".github/workflows/test.yml": wf,
               ".github/workflows/all.yml": "on: [push]\njobs:\n  t:\n    steps:\n      - run: pytest\n",
               "src/a.py": "x = 1\n"}, "Init", days=0)
    r2.commit({f"lib/core/m{i}.py": "y = 1\n" for i in range(3)}, "Add lib core", days=1)
    assert r2.run("ci_path_filter_new_dir_gap") == []
    assert r2.ctx.noise["ci_path_filter_new_dir_gap/unfiltered_test_workflow"] == 1


def test_noise_filters(mini):
    r = mini()
    r.commit({"dev/settings.py": "TRUSTED_PROXIES = 1\n", "docker-compose.yml": COMPOSE_V1,
              "helm/values-dev.yaml": "replicas: 4\n"}, "Init", days=0)
    r.commit({"dev/settings.py": "TRUSTED_PROXIES = 2\n", "helm/values-dev.yaml": "replicas: 1\n",
              "docker-compose.yml": COMPOSE_V1.replace("pgdata", "dbdata"),
              "scripts/migrate_volume.sh": "echo migrate\n"}, "Rename volume with migration script", days=1)
    assert r.run("trusted_proxy_trust_setting_change") == []
    assert r.run("k8s_availability_knob_reduction") == []
    assert r.run("persistent_state_mount_change") == []
    assert r.ctx.noise["persistent_state_mount_change/migration_in_commit"] == 1


def test_client_moved_out_of_function_symbol(mini):
    r = mini()
    r.commit({"app/c.py": "import requests\n\n\ndef fetch(u):\n    s = requests.Session()\n    return s.get(u)\n"},
             "Initial", days=0)
    fix = r.commit({"app/c.py": "import requests\n\nSESSION = requests.Session()\n\n\ndef fetch(u):\n"
                                "    return SESSION.get(u)\n"}, "Reuse client session", days=3)
    hits = [h for h in r.run("per_request_client_construction") if h.subject.endswith("|fix")]
    assert [h.commits[0] for h in hits] == [fix]
    assert hits[0].evidence["removed_from_function"] is True and hits[0].symbols[0].endswith(":fetch:function")


# ---------------------------------------------------------------------------------------------- round-2 enrichment
_DEPLOY = ("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  template:\n    spec:\n      containers:\n"
           "        - name: app\n          image: x\n          resources:\n{res}")


def _res(mem=None, cpu="100m"):
    out = "            requests:\n              cpu: 50m\n"
    if mem:
        out += f"              memory: {mem}\n"
    out += "            limits:\n              cpu: " + cpu + "\n"
    if mem:
        out += f"              memory: {mem}\n"
    return out


def test_k8s_resource_churn_from_rendered_helm_manifests(mini):
    from afd_oracle_ingest.catalog.base import Ctx
    from afd_oracle_ingest.catalog import load_all
    r = mini()
    tpl = "charts/api/templates/deploy.yaml"
    templated = "resources:\n  limits:\n    memory: {{ .Values.mem }}\n"
    c0 = r.commit({tpl: templated, "charts/api/values.yaml": "mem: 256Mi\n"}, "Add chart", days=0)
    c1 = r.commit({"charts/api/values.yaml": "mem: 512Mi\n"}, "Raise memory", days=10)
    c2 = r.commit({"charts/api/values.yaml": "mem: null\n"}, "Fix OOMKilled pods: drop memory limit", days=20)
    r.ingest()
    conn = r.ctx.store.conn
    for sha, mem in ((c0, "256Mi"), (c1, "512Mi"), (c2, None)):
        conn.execute("INSERT INTO helm_renders VALUES ('api', ?, ?, ?)",
                     (sha, r.ctx.by_hash[sha]["ts"], _DEPLOY.format(res=_res(mem))))
    conn.commit()
    ctx = Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)
    ctx._current = "k8s_resource_limit_churn"
    hits = list(load_all()["k8s_resource_limit_churn"](ctx))
    assert [h.subject for h in hits] == ["helm:api"] and hits[0].evidence["rendered"] is True
    ev = {e["commit"]: e["events"] for e in hits[0].evidence["changes"]}
    assert any(x.startswith("memory x2.0 Deployment/api/app.limits.memory") for x in ev[c1[:10]])
    assert any(x.startswith("limit removed Deployment/api/app.limits.memory") for x in ev[c2[:10]])
    assert hits[0].paths and all(p.startswith("charts/api/") for p in hits[0].paths)
    assert ctx.noise.get("k8s_resource_limit_churn/covered_by_helm_render", 0) >= 1
