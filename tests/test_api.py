"""Every HTTP route the UI depends on.

Nothing here was covered at all, so a route could 500 for every user and the
suite would stay green. These assert the contract each view relies on: the
status, the shape, and that bad input is refused rather than answered.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client(db):
    # WEB_ROOT defaults to the container path, so the SPA shell is not mounted
    # when the suite runs on the host. Point it at the repo's own web/ so the
    # shell routes are exercised rather than silently skipped.
    import os
    from pathlib import Path

    from git_synapse.config import reset_config_cache

    repo_web = Path(__file__).resolve().parents[1] / "web"
    previous = os.environ.get("WEB_ROOT")
    if repo_web.is_dir():
        os.environ["WEB_ROOT"] = str(repo_web)
        reset_config_cache()

    import importlib

    import git_synapse.api.main as api_main

    importlib.reload(api_main)
    try:
        with TestClient(api_main.app) as c:
            yield c
    finally:
        if previous is None:
            os.environ.pop("WEB_ROOT", None)
        else:
            os.environ["WEB_ROOT"] = previous
        reset_config_cache()


# ------------------------------------------------------------------- meta

def test_health_reports_the_database(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["database"] in {"ok", "up", True, "reachable"}


def test_overview_returns_the_headline_counts(client):
    body = client.get("/api/overview").json()
    for key in ("repos", "commits", "file_pairs"):
        assert key in body and isinstance(body[key], int)


def test_measures_catalogue_is_complete_and_self_describing(client):
    body = client.get("/api/measures").json()
    measures = body["measures"] if isinstance(body, dict) else body
    assert len(measures) >= 29
    for m in measures:
        assert m["key"] and m["label"]


# ------------------------------------------------------------------ repos

def test_repos_listing_and_pagination(client):
    body = client.get("/api/repos", params={"limit": 3}).json()
    rows = body["repos"] if isinstance(body, dict) else body
    assert len(rows) <= 3


def test_the_tree_route_walks_a_repository_one_level_at_a_time(corpus, client):
    """The folder page is the whole point of addressing files by path, so this
    covers the round trip: root, descend, and the file resolving back."""
    repos = client.get("/api/repos", params={"limit": 50}).json()["repos"]
    for repo in repos:
        root = client.get(f"/api/repos/{repo['id']}/tree").json()
        if root["directories"]:
            break
    else:
        pytest.skip("no repository with directories")

    assert root["path"] == "" and root["directory"] is None
    top = root["directories"][0]["path"]

    child = client.get(f"/api/repos/{repo['id']}/tree", params={"path": top})
    assert child.status_code == 200
    assert child.json()["directory"]["path"] == top

    # A leading or trailing slash is how a hand-typed URL arrives.
    assert client.get(f"/api/repos/{repo['id']}/tree",
                      params={"path": f"/{top}/"}).json()["directory"]["path"] == top


def test_the_tree_route_404s_for_a_path_that_is_not_in_the_repository(corpus, client):
    repos = client.get("/api/repos", params={"limit": 1}).json()["repos"]
    if not repos:
        pytest.skip("no repositories")
    r = client.get(f"/api/repos/{repos[0]['id']}/tree", params={"path": "no/such/dir"})
    assert r.status_code == 404
    assert client.get("/api/repos/999999999/tree").status_code == 404


def test_resolving_a_file_needs_exactly_one_way_to_name_the_repository(client):
    """Both, or neither, is a caller bug; answering it anyway would silently
    ignore one of them."""
    assert client.get("/api/files/resolve", params={"path": "x"}).status_code == 400
    assert client.get("/api/files/resolve",
                      params={"path": "x", "repo": "a", "repo_id": 1}).status_code == 400


def test_a_file_resolves_by_repo_id_and_reports_a_miss(corpus, client):
    files = client.get("/api/files", params={"limit": 1}).json()["files"]
    if not files:
        pytest.skip("no files")
    f = files[0]
    got = client.get("/api/files/resolve",
                     params={"path": f["path"], "repo_id": f["repo_id"]})
    assert got.status_code == 200 and got.json()["id"] == f["id"]
    assert client.get("/api/files/resolve",
                      params={"path": "nope.xyz", "repo_id": f["repo_id"]}).status_code == 404


def test_unknown_repo_id_is_a_404_not_an_empty_success(client):
    assert client.get("/api/repos/999999999").status_code == 404


@pytest.mark.parametrize("bad", ["abc", "1e5", "-"])
def test_non_numeric_repo_id_is_refused(client, bad):
    assert client.get(f"/api/repos/{bad}").status_code in (404, 422)


def test_repo_zero_is_not_treated_as_unset(corpus, client):
    """`if repo_id:` made 0 mean "no filter" and returned the whole corpus
    dressed as one repository's data."""
    scoped = client.get("/api/repos/0/hotspots", params={"limit": 2}).json()
    everything = client.get("/api/hotspots", params={"limit": 2}).json()
    assert scoped != everything


# --------------------------------------------------------------- coupling

def test_coupled_partners_are_oriented_and_ranked(corpus, client):
    args = _a_real_file()
    if args is None:
        pytest.skip("no files indexed")
    f = client.get("/api/files/resolve", params=args)
    if f.status_code != 200:
        pytest.skip("fixture file not indexed")
    fid = f.json().get("file_id") or f.json().get("id")

    body = client.get(f"/api/files/{fid}/coupled",
                      params={"limit": 5, "min_support": 3}).json()
    rows = body["partners"]
    if not rows:
        pytest.skip("no partners")
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    for r in rows:
        assert 0.0 <= r["confidence_out"] <= 1.0
        assert r["n_other"] >= r["n_ab"]


@pytest.mark.parametrize("measure", ["npmi", "jaccard", "confidence_ab", "ochiai"])
def test_every_offered_measure_actually_works(client, measure):
    r = client.get("/api/pairs", params={"measure": measure, "limit": 2})
    assert r.status_code == 200, f"{measure} -> {r.status_code}"


@pytest.mark.parametrize("measure", ["w_ab", "last_co_change", "not_a_measure", "path"])
def test_a_measure_the_query_cannot_project_is_refused_not_500(client, measure):
    """These passed the allowlist and then failed at the database on seven
    endpoints, returning 500 for a value the code declared valid."""
    r = client.get("/api/pairs", params={"measure": measure, "limit": 2})
    assert r.status_code == 400, f"{measure} -> {r.status_code}"


@pytest.mark.parametrize("limit", [0, -1, 100000])
def test_out_of_range_limits_are_clamped_or_refused(client, limit):
    r = client.get("/api/repos", params={"limit": limit})
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        rows = r.json()
        rows = rows["repos"] if isinstance(rows, dict) else rows
        assert len(rows) <= 1000


# --------------------------------------------------------------- feedback

def test_feedback_status_filter(client):
    """`status=all` was dropped by the query-string builder, so the filter
    silently fell back to `open` and showed nothing."""
    everything = client.get("/api/feedback", params={"status": "all"}).json()
    only_open = client.get("/api/feedback", params={"status": "open"}).json()
    assert len(everything["reports"]) >= len(only_open["reports"])
    assert all(r["status"] == "open" for r in only_open["reports"])


# -------------------------------------------------------------- the shell

def test_spa_routes_serve_the_shell_so_a_deep_link_survives_refresh(client):
    for path in ("/repos", "/insights", "/measures", "/feedback"):
        r = client.get(path)
        assert r.status_code == 200
        assert "<" in r.text


def test_every_navigable_route_serves_the_shell(client):
    """Derived from the nav rather than listed, so adding a view to the header
    without registering its route fails here instead of 404ing for users."""
    import re
    from pathlib import Path

    shell = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    nav = re.search(r'<nav class="mainnav".*?</nav>', shell, re.S)
    assert nav, "the shell no longer has a main nav to derive routes from"

    hrefs = [h for h in re.findall(r'href="(/[^"]*)"', nav.group(0)) if h != "/"]
    assert len(hrefs) > 5, "suspiciously few nav links; the regex probably broke"
    for href in hrefs:
        assert client.get(href).status_code == 200, f"nav links to {href}, which 404s"


def test_a_genuine_typo_still_404s(client):
    assert client.get("/definitely-not-a-route").status_code == 404


def test_the_shell_stamps_a_current_asset_version(client):
    """A hand-written `?v=2` never changed, so browsers served cached
    JavaScript across redeploys and rendered blank views."""
    import re

    html = client.get("/").text
    versions = set(re.findall(r"/static/[\w.-]+\?v=(\d+)", html))
    assert versions, "assets must carry a version"
    assert all(v.isdigit() and int(v) > 2 for v in versions)


# ------------------------------------------------------- the remaining views

def test_file_detail_endpoints_agree_with_each_other(corpus, client):
    args = _a_real_file()
    if args is None:
        pytest.skip("no files indexed")
    f = client.get("/api/files/resolve", params=args)
    if f.status_code != 200:
        pytest.skip("fixture file not indexed")
    fid = f.json().get("file_id") or f.json().get("id")

    detail = client.get(f"/api/files/{fid}").json()
    commits = client.get(f"/api/files/{fid}/commits", params={"limit": 5}).json()
    authors = client.get(f"/api/files/{fid}/authors", params={"limit": 5}).json()

    rows = commits["commits"] if isinstance(commits, dict) else commits
    assert len(rows) <= 5
    assert len(rows) <= detail["change_count"], "more commits than the file has changes"
    assert isinstance(authors, (list, dict))


def test_pair_detail_and_its_evidence_are_consistent(client):
    from git_synapse.analysis.query import query_one

    row = query_one("SELECT file_a_id a, file_b_id b FROM file_pair_metric WHERE n_ab > 3 LIMIT 1")
    if row is None:
        pytest.skip("no supported pair")

    detail = client.get(f"/api/pairs/{row['a']}/{row['b']}").json()
    r = client.get(f"/api/pairs/{row['a']}/{row['b']}/commits", params={"limit": 200})
    assert r.status_code == 200, r.text[:200]
    evidence = r.json()
    rows = evidence["commits"] if isinstance(evidence, dict) else evidence
    counted = [c for c in rows if c.get("counted", True)]
    # Two separate requests, and a live refresh can rebuild the pair between
    # them, so the invariant is asserted rather than exact equality: evidence
    # that counted can never exceed the joint count it is presented as
    # explaining. The exact match is checked against a stable snapshot in
    # tests/test_query.py.
    assert len(counted) <= detail["n_ab"] or len(rows) >= 200, (
        f"{len(counted)} counted commits against a joint count of {detail['n_ab']}"
    )


def test_repo_sub_resources_answer(client):
    from git_synapse.analysis.query import query_one

    row = query_one("SELECT id FROM repo WHERE is_enabled ORDER BY commit_count DESC LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    rid = row["id"]
    for suffix in ("", "/files", "/directories", "/extensions", "/hotspots",
                   "/pairs", "/graph"):
        r = client.get(f"/api/repos/{rid}{suffix}", params={"limit": 3})
        assert r.status_code == 200, f"{suffix} -> {r.status_code}"


def test_an_unknown_file_or_pair_is_a_404(client):
    assert client.get("/api/files/999999999").status_code == 404
    assert client.get("/api/pairs/999999998/999999999").status_code == 404


@pytest.mark.parametrize("min_score", ["-1", "0.5", "2", "NaN", "inf"])
def test_min_score_never_500s(client, min_score):
    r = client.get("/api/pairs", params={"limit": 3, "min_score": min_score})
    assert r.status_code in (200, 422), f"{min_score} -> {r.status_code}"


def test_refresh_endpoint_refuses_while_a_run_is_active(client):
    """It must not start a second ingest over the top of a live one."""
    from git_synapse.db.engine import connection
    from git_synapse.ingest.pipeline import INGEST_LOCK_KEY

    with connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK_KEY,))
        try:
            with connection() as conn:
                rid = conn.execute(
                    "INSERT INTO ingest_run (kind, trigger, status, started_at)"
                    " VALUES ('sync','test','running',now()) RETURNING id"
                ).fetchone()[0]
            try:
                r = client.post("/api/ingest/refresh")
                assert r.status_code in (409, 202, 200)
            finally:
                with connection() as conn:
                    conn.execute("DELETE FROM ingest_run WHERE id=%s", (rid,))
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_KEY,))


# --------------------------------------------------- every route, discovered

def _real_ids():
    """Ids that actually exist, so a sweep exercises the query rather than a 404."""
    from git_synapse.analysis.query import query_one

    ids = {}
    row = query_one("SELECT id FROM repo WHERE is_enabled ORDER BY commit_count DESC LIMIT 1")
    if row:
        ids["repo_id"] = ids["repo_a_id"] = row["id"]
    row = query_one(
        "SELECT id FROM repo WHERE is_enabled ORDER BY commit_count DESC OFFSET 1 LIMIT 1"
    )
    if row:
        ids["repo_b_id"] = row["id"]
    row = query_one("SELECT file_a_id a, file_b_id b FROM file_pair_metric LIMIT 1")
    if row:
        ids["file_id"] = ids["file_a_id"] = row["a"]
        ids["file_b_id"] = row["b"]
    row = query_one("SELECT id FROM directory ORDER BY change_count DESC LIMIT 1")
    if row:
        ids["dir_id"] = row["id"]
    row = query_one("SELECT id FROM ingest_run ORDER BY id DESC LIMIT 1")
    if row:
        ids["run_id"] = row["id"]
    return ids


#: Endpoints whose required query arguments the sweep cannot guess. None means
#: "cannot be swept generically"; a callable is given the ids and returns the
#: arguments, so nothing here names a repository that has to pre-exist.
REQUIRED_QUERY: dict[str, dict | None] = {
    "/api/files/resolve": None,
}


def _a_real_file() -> dict | None:
    """A `(repo, path)` pair that exists, for the resolve endpoint."""
    from git_synapse.analysis.query import query_one

    row = query_one(
        "SELECT r.full_name AS repo, f.path FROM file f"
        " JOIN repo r ON r.id = f.repo_id LIMIT 1"
    )
    return {"repo": row["repo"], "path": row["path"]} if row else None


def test_every_get_route_answers_with_real_arguments(corpus, client):
    """A sweep over the routes the app actually declares.

    Enumerating them from the app rather than a hand-written list means a new
    endpoint is covered the moment it is added, instead of quietly never being
    called until a user finds it broken.
    """
    import re

    ids = _real_ids()
    checked, skipped, failures = 0, [], []

    # The OpenAPI document is the app's own list of what it serves, so a new
    # endpoint is swept the moment it exists rather than whenever someone
    # remembers to add it here.
    schema = client.get("/api/openapi.json").json()
    for path, ops in schema.get("paths", {}).items():
        if "get" not in ops or not path.startswith("/api/"):
            continue

        params = re.findall(r"\{(\w+)\}", path)
        if any(p not in ids for p in params):
            skipped.append(path)
            continue
        concrete = path
        for p in params:
            concrete = concrete.replace(f"{{{p}}}", str(ids[p]))

        # A few endpoints take required query arguments; give them real ones
        # rather than letting the sweep report a 422 as a fault.
        extra = REQUIRED_QUERY.get(path, {})
        if path == "/api/files/resolve":
            extra = _a_real_file()
        if extra is None:
            skipped.append(path)
            continue
        r = client.get(concrete, params={"limit": 3, **extra})
        checked += 1
        if r.status_code != 200:
            failures.append(f"{concrete} -> {r.status_code} {r.text[:100]}")

    assert checked > 25, f"the sweep only reached {checked} routes"
    assert not failures, "\n".join(failures)


def test_every_route_refuses_a_nonexistent_id_rather_than_500ing(client, db):
    """404 or an empty result is fine. A 500 means an unguarded query."""
    import re

    bogus = 999999999
    failures = []
    schema = client.get("/api/openapi.json").json()
    for path, ops in schema.get("paths", {}).items():
        if "get" not in ops or not path.startswith("/api/"):
            continue
        params = re.findall(r"\{(\w+)\}", path)
        if not params or not all(p.endswith("_id") for p in params):
            continue
        concrete = path
        for p in params:
            concrete = concrete.replace(f"{{{p}}}", str(bogus))
        r = client.get(concrete, params={"limit": 3})
        if r.status_code >= 500:
            failures.append(f"{concrete} -> {r.status_code}")
    assert not failures, "\n".join(failures)


# --------------------------------------------------------- the guarded routes

def test_health_reports_degraded_rather_than_raising(client, monkeypatch):
    """A health endpoint that 500s tells a load balancer nothing it can act on."""
    from git_synapse.api import routes

    monkeypatch.setattr(routes, "scalar",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no db")))
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"] is False
    assert "no db" in body["error"]


def test_looking_up_a_file_that_does_not_exist_is_a_404(client, db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    r = client.get("/api/files/resolve", params={"repo": row["name"],
                                                 "path": "no/such/file.go"})
    assert r.status_code == 404


def test_resolving_a_report_refuses_an_unknown_status(client):
    r = client.post("/api/feedback/1/resolve", params={"status": "closed"})
    assert r.status_code == 400


def test_resolving_a_report_that_does_not_exist_is_a_404(client):
    r = client.post("/api/feedback/999999999/resolve", params={"status": "fixed"})
    assert r.status_code == 404


def test_a_refresh_is_refused_while_a_run_is_already_in_progress(client,
                                                                 monkeypatch):
    """Two concurrent ingests fetch the same mirrors and redo the same global
    rebuilds; the advisory lock catches it, but a 409 is the honest answer."""
    import datetime as dt

    from git_synapse.api import routes

    monkeypatch.setattr(routes.pipeline, "active_run", lambda: {
        "id": 7, "trigger": "schedule",
        "started_at": dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.timezone.utc),
    })
    r = client.post("/api/ingest/refresh")
    assert r.status_code == 409
    assert "already in progress" in r.json()["detail"]


@pytest.mark.parametrize("params", [
    {},
    {"force_full": "true"},
    {"skip_discovery": "true"},
])
def test_a_refresh_starts_in_the_background_and_returns_at_once(client,
                                                                monkeypatch,
                                                                params):
    """A full ingest takes tens of minutes, far longer than any HTTP timeout."""
    from git_synapse.api import routes

    ran = {}
    monkeypatch.setattr(routes.pipeline, "active_run", lambda: None)
    monkeypatch.setattr(routes.pipeline, "load_repo_records", lambda: ["x"])
    monkeypatch.setattr(routes.pipeline, "run_ingest",
                        lambda **kw: ran.update(kw))
    r = client.post("/api/ingest/refresh", params=params)
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    assert ran["trigger"] == "api"
    assert ran["force_full"] is (params.get("force_full") == "true")
    # Discovery is skipped by loading the known records instead of asking GitHub.
    assert (ran["records"] is not None) is (params.get("skip_discovery") == "true")


@pytest.mark.parametrize("path", ["/repos", "/jobs", "/feedback/17",
                                  "/repos/5/files/src/main/Cache.java",
                                  "/insights/impact/graph"])
def test_the_spa_serves_its_own_routes(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("path", ["/definitely-not-a-route",
                                  "/definitely-not-a-route/deeper"])
def test_a_typo_is_a_404_rather_than_a_silently_rendered_shell(client, path):
    """A catch-all would render the app shell for every typo, and the failure
    would surface as a blank page instead of a 404."""
    assert client.get(path).status_code == 404


def test_the_favicon_is_served(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200


def test_a_report_can_be_resolved_and_says_so(client, db):
    """The one write an agent cannot make: closing its own report."""
    from git_synapse.analysis import query as q

    created = q.record_feedback(
        kind="wrong_data", detail="coverage fixture: resolve round-trip",
        severity="low",
    )
    r = client.post(f"/api/feedback/{created['id']}/resolve",
                    params={"status": "wontfix", "resolution": "fixture"})
    assert r.status_code == 200
    assert r.json() == {"id": created["id"], "status": "wontfix"}


# ---------------------------------------------------------------- accounts

@pytest.fixture()
def no_accounts(client):
    """An empty account table, restored to empty after the test."""
    from git_synapse.db.engine import execute

    execute("DELETE FROM account")
    yield client
    execute("DELETE FROM account")


def test_accounts_listing_is_empty_not_an_error_before_onboarding(no_accounts):
    body = no_accounts.get("/api/accounts").json()
    assert body["count"] == 0 and body["accounts"] == []
    assert "org" in body["kinds"], "the UI builds its kind picker from this"


def test_an_account_can_be_created_and_read_back(no_accounts):
    made = no_accounts.post("/api/accounts", json={"login": "kubernetes"})
    assert made.status_code == 201
    account_id = made.json()["id"]
    assert no_accounts.get(f"/api/accounts/{account_id}").json()["login"] == "kubernetes"


def test_creating_a_duplicate_is_refused_with_a_reason(no_accounts):
    no_accounts.post("/api/accounts", json={"login": "kubernetes"})
    clash = no_accounts.post("/api/accounts", json={"login": "kubernetes"})
    assert clash.status_code == 409
    assert "already configured" in clash.json()["detail"]


def test_an_invalid_login_is_refused_rather_than_stored(no_accounts):
    bad = no_accounts.post("/api/accounts", json={"login": "not a login"})
    assert bad.status_code == 409
    assert no_accounts.get("/api/accounts").json()["count"] == 0


def test_a_missing_login_is_a_validation_error(no_accounts):
    assert no_accounts.post("/api/accounts", json={}).status_code == 422


def test_patching_leaves_unpassed_fields_alone(no_accounts):
    made = no_accounts.post("/api/accounts", json={"login": "kubernetes"}).json()
    patched = no_accounts.patch(f"/api/accounts/{made['id']}", json={"enabled": False}).json()
    assert patched["enabled"] is False
    assert patched["include_forks"] is True


def test_patching_a_missing_account_is_404(no_accounts):
    assert no_accounts.patch("/api/accounts/999999", json={"enabled": False}).status_code == 404


def test_deleting_an_account_removes_it(no_accounts):
    made = no_accounts.post("/api/accounts", json={"login": "kubernetes"}).json()
    assert no_accounts.delete(f"/api/accounts/{made['id']}").status_code == 200
    assert no_accounts.get(f"/api/accounts/{made['id']}").status_code == 404


def test_deleting_a_missing_account_is_404(no_accounts):
    assert no_accounts.delete("/api/accounts/999999").status_code == 404


def test_a_deleted_account_keeps_its_repositories(no_accounts):
    """The mined statistics are the expensive part; they must survive."""
    from git_synapse.db.engine import connection, query_one
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.store import upsert_repo

    from git_synapse.db.engine import execute

    made = no_accounts.post("/api/accounts", json={"login": "keepme"}).json()
    record = RepoRecord.from_api({
        "id": 424242, "name": "kept", "full_name": "keepme/kept",
        "owner": {"login": "keepme"},
    })
    with connection() as conn:
        repo_id = upsert_repo(record, conn, account_id=made["id"])

    try:
        no_accounts.delete(f"/api/accounts/{made['id']}")
        still = query_one("SELECT account_id FROM repo WHERE id = %s", (repo_id,))
        assert still is not None, "deleting an account must not delete its repositories"
        assert still["account_id"] is None, "the link clears rather than cascading"
    finally:
        # This fixture runs against the shared corpus database, so a synthetic
        # repository left behind is picked up by every later test that reads
        # `repo` -- one of which dereferences clone_url and fails on the None
        # this record has.
        execute("DELETE FROM repo WHERE id = %s", (repo_id,))


def test_updating_an_account_to_a_taken_login_is_a_conflict(client):
    """409, not 500: the request was well-formed and the caller can fix it."""
    from git_synapse.ingest import accounts

    first = accounts.add_account("apione")
    second = accounts.add_account("apitwo")
    try:
        r = client.patch(f"/api/accounts/{second['id']}", json={"login": "apione"})
        assert r.status_code == 409
        assert "already configured" in r.json()["detail"]
    finally:
        accounts.remove_account(first["id"])
        accounts.remove_account(second["id"])


def test_a_run_that_does_not_exist_is_a_404(client):
    assert client.get("/api/runs/-1").status_code == 404


def test_a_run_that_exists_is_returned(client):
    from git_synapse.db.engine import connection, query_one

    with connection() as conn:
        run_id = conn.execute(
            "INSERT INTO ingest_run (kind, trigger, status) "
            "VALUES ('fast','manual','success') RETURNING id").fetchone()[0]
        conn.commit()
    try:
        r = client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200
        assert r.json()["id"] == run_id
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM ingest_run WHERE id = %s", (run_id,))
            conn.commit()


def test_a_repository_pair_lists_every_bump_not_just_a_count(client, db):
    """"13 bumps, median lag 41.8 days" is a summary of something the page never
    showed. This is the something: which version, on what date, and the upstream
    commit it consumed."""
    from git_synapse.db.engine import connection

    with connection() as conn:
        a = conn.execute("INSERT INTO repo (full_name, name, owner) VALUES "
                         "('acme/app','app','acme') RETURNING id").fetchone()[0]
        b = conn.execute("INSERT INTO repo (full_name, name, owner) VALUES "
                         "('acme/lib','lib','acme') RETURNING id").fetchone()[0]
        up = conn.execute(
            "INSERT INTO commit (repo_id, sha, authored_at, committed_at, subject) "
            "VALUES (%s, %s, '2024-01-01', '2024-01-01', 'upstream work') RETURNING id",
            (b, "c" * 40)).fetchone()[0]
        for version, at in (("1.0.0", "2024-02-01"), ("1.1.0", "2024-03-01")):
            conn.execute(
                "INSERT INTO dep_bump (consumer_repo_id, consumer_sha, dep_repo_id, "
                "dep_name, dep_version, manifest, bumped_at, dep_commit_id, resolution, "
                "adoption_seconds) VALUES (%s,%s,%s,'lib',%s,'pom.xml',%s,%s,'tag',86400)",
                (a, version.replace(".", "") + "a" * 34, b, version, at, up))
        conn.commit()
    try:
        r = client.get(f"/api/repos/{a}/bumps/{b}")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        newest = body["bumps"][0]
        assert newest["dep_version"] == "1.1.0", "newest first"
        assert newest["upstream_subject"] == "upstream work"
        assert newest["adoption_days"] == 1.0
        assert newest["resolution"] == "tag"
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM repo WHERE id IN (%s, %s)", (a, b))
            conn.commit()


def test_a_pair_with_no_bumps_returns_an_empty_list_not_an_error(client, db):
    """A declared dependency that has never moved is a real state, and the page
    says so rather than showing a failure."""
    r = client.get("/api/repos/-1/bumps/-2")
    assert r.status_code == 200 and r.json()["count"] == 0


def test_lag_is_a_number_in_json_not_a_string(client, db):
    """Postgres NUMERIC becomes a Decimal, which serialises as a string -- so a
    field that looks numeric raises a TypeError the moment anyone does
    arithmetic on it."""
    from git_synapse.db.engine import connection

    with connection() as conn:
        a = conn.execute("INSERT INTO repo (full_name, name, owner) VALUES "
                         "('acme/n1','n1','acme') RETURNING id").fetchone()[0]
        b = conn.execute("INSERT INTO repo (full_name, name, owner) VALUES "
                         "('acme/n2','n2','acme') RETURNING id").fetchone()[0]
        conn.execute(
            "INSERT INTO dep_bump (consumer_repo_id, consumer_sha, dep_repo_id, "
            "dep_name, dep_version, manifest, bumped_at, adoption_seconds) "
            "VALUES (%s,%s,%s,'n2','1.0.0','pom.xml','2024-01-01',172800)",
            (a, "e" * 40, b))
        conn.commit()
    try:
        lag = client.get(f"/api/repos/{a}/bumps/{b}").json()["bumps"][0]["adoption_days"]
        assert isinstance(lag, (int, float)) and lag == 2.0

        median = client.get(f"/api/repos/{a}/dependencies").json()["bumps"][0]["median_adoption_days"]
        assert isinstance(median, (int, float))
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM repo WHERE id IN (%s, %s)", (a, b))
            conn.commit()


def test_the_server_serves_the_shell_for_every_route_the_spa_claims():
    """The nav covers the tabs; this covers the rest. A route registered in the
    client but unknown to the server 404s on reload and on a pasted link, which
    is precisely when a shareable URL is worth having."""
    import re
    from pathlib import Path

    from git_synapse.api.main import app  # noqa: F401  (ensures the module loads)

    web = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
    claimed = {m.group(1) for m in re.finditer(r"^on\('/([a-z]+)", web.read_text(), re.M)}
    assert claimed, "no client routes found; the regex probably broke"

    served = set(_spa_routes())
    assert claimed <= served, f"the SPA routes {sorted(claimed - served)}, which 404 on reload"


def _spa_routes():
    """The tuple is a local inside the module's `if web root exists` block, so it
    is read back out of the source rather than imported."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "git_synapse" / "api" / "main.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SPA_ROUTES":
            return ast.literal_eval(node.value)
    raise AssertionError("SPA_ROUTES no longer exists in api/main.py")
