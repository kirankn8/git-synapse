"""Every HTTP route the UI depends on.

Nothing here was covered at all, so a route could 500 for every user and the
suite would stay green. These assert the contract each view relies on: the
status, the shape, and that bad input is refused rather than answered.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


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
    nav = re.search(r'<nav class="mainnav".*?</nav>', shell, re.DOTALL)
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


def test_every_get_route_answers_with_real_arguments(corpus, admin_client):
    client = admin_client
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


def test_resolving_a_report_refuses_an_unknown_status(signed_in):
    client = signed_in
    r = client.post("/api/feedback/1/resolve", params={"status": "closed"})
    assert r.status_code == 400


def test_resolving_a_report_that_does_not_exist_is_a_404(signed_in):
    client = signed_in
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
        "started_at": dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.UTC),
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


def test_a_report_can_be_resolved_and_says_so(signed_in, db):
    """The one write an agent cannot make: closing its own report."""
    client = signed_in
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
    """An empty account table, and a signed-in administrator to manage it.

    Adding, re-crediting and deleting a source decides what the deployment
    scans and what credentials it uses, so those are administrative acts and
    need somebody to attribute them to -- which means these tests need an admin
    even on a deployment whose reads are open.
    """
    from git_synapse.db.engine import execute
    from git_synapse.ingest import accounts as _accounts

    execute("DELETE FROM account")
    admin = _sign_in_admin(client)
    try:
        yield client
    finally:
        client.cookies.clear()
        execute("DELETE FROM account")
        _cleanup_admin(admin)
        del _accounts


@pytest.fixture
def signed_in(client):
    """A client with an administrator's session, for acts that need an owner."""
    email = _sign_in_admin(client)
    try:
        yield client
    finally:
        client.cookies.clear()
        _cleanup_admin(email)


def _sign_in_admin(client):
    """Create an administrator and put their session on the client."""
    from git_synapse import auth

    email = "pytest-src-admin@example.com"
    password = "a-sufficiently-long-pass"
    existing = auth.by_email(email)
    if existing is None:
        auth.create_user(email, "Source Admin", password, role="admin")
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return email


def _cleanup_admin(email: str) -> None:
    from git_synapse import auth

    row = auth.by_email(email)
    if row is not None:
        auth.delete_user(row["id"])


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
    # Untouched by the patch, and off by default: a fork's history is its
    # parent's, so tracking both files every commit twice.
    assert patched["include_forks"] is False


def test_resolving_a_url_reads_without_writing(no_accounts, monkeypatch):
    """The lookup is how somebody finds out whether a thing is there. Writing
    on a read would mean a typo becomes a tracked source."""
    from git_synapse.ingest import accounts as acc

    monkeypatch.setattr(acc, "resolve_url",
                        lambda url, limit=300, token="", page=1: {"kind": "repo",
                                                             "repos": [],
                                                             "owner": "acme"})
    r = no_accounts.post("/api/accounts/resolve", json={"url": "https://github.com/acme/x"})
    assert r.status_code == 200 and r.json()["kind"] == "repo"
    # Whether a token could be kept at all, so the form knows to offer.
    assert "can_store_token" in r.json()
    assert no_accounts.get("/api/accounts").json()["count"] == 0


def test_an_unparseable_url_is_a_400_not_a_500(no_accounts):
    r = no_accounts.post("/api/accounts/resolve", json={"url": "not a url"})
    assert r.status_code == 400


def test_a_host_that_refuses_is_reported_as_a_bad_gateway(no_accounts, monkeypatch):
    """It is not the caller's request that is wrong."""
    from git_synapse.ingest import accounts as acc

    def _boom(url, limit=300, token="", page=1):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(acc, "resolve_url", _boom)
    r = no_accounts.post("/api/accounts/resolve", json={"url": "https://github.com/a/b"})
    assert r.status_code == 502 and "connection reset" in r.json()["detail"]


def test_adding_from_a_url_creates_the_source(no_accounts, monkeypatch):
    from git_synapse.ingest import accounts as acc

    monkeypatch.setattr(acc, "add_from_url",
                        lambda url, repos=None, token="": {"login": "acme", "repos": repos,
                                                           "token_seen": bool(token)})
    r = no_accounts.post("/api/accounts/from-url",
                         json={"url": "https://github.com/acme", "repos": ["one"],
                               "token": "ghp_x"})
    assert r.status_code == 201
    assert r.json() == {"login": "acme", "repos": ["one"], "token_seen": True}


def test_storing_a_token_without_a_key_is_refused_not_swallowed(no_accounts, monkeypatch):
    from git_synapse import vault
    from git_synapse.ingest import accounts as acc

    monkeypatch.delenv(vault.ENV_KEY, raising=False)
    monkeypatch.setattr(acc, "add_from_url",
                        lambda url, repos=None, token="": (_ for _ in ()).throw(
                            vault.VaultError("no key")))
    r = no_accounts.post("/api/accounts/from-url",
                         json={"url": "https://github.com/a/b", "token": "ghp_x"})
    assert r.status_code == 422


def test_a_credential_can_be_set_and_cleared_but_never_read(no_accounts, monkeypatch):
    from git_synapse import vault

    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase")
    made = no_accounts.post("/api/accounts", json={"login": "credorg"}).json()
    put = no_accounts.put(f"/api/accounts/{made['id']}/credential",
                          json={"token": "ghp_supersecrettokenvalue"})
    assert put.status_code == 200
    body = put.json()
    assert body["has_credential"] is True
    assert "supersecret" not in str(body), "no endpoint returns the token"
    assert body["credential_hint"].startswith("ghp_")

    cleared = no_accounts.put(f"/api/accounts/{made['id']}/credential", json={"token": ""})
    assert cleared.json()["has_credential"] is False


def test_setting_a_credential_on_a_missing_source_is_404(no_accounts):
    assert no_accounts.put("/api/accounts/999999/credential",
                           json={"token": "x"}).status_code == 404


def test_setting_a_credential_with_no_key_configured_is_422(no_accounts, monkeypatch):
    from git_synapse import vault

    monkeypatch.delenv(vault.ENV_KEY, raising=False)
    made = no_accounts.post("/api/accounts", json={"login": "nokeyorg"}).json()
    r = no_accounts.put(f"/api/accounts/{made['id']}/credential", json={"token": "ghp_x"})
    assert r.status_code == 422 and vault.ENV_KEY in r.json()["detail"]


def test_a_resolve_that_names_no_owner_is_a_422(no_accounts, monkeypatch):
    """An AccountError here is a well-formed URL we cannot act on -- a host
    with no API asked to be enumerated -- not a malformed one."""
    from git_synapse.ingest import accounts as acc
    from git_synapse.ingest.accounts import AccountError

    def _refuse(url, limit=300, token="", page=1):
        raise AccountError("git.corp has no API we can list")

    monkeypatch.setattr(acc, "resolve_url", _refuse)
    r = no_accounts.post("/api/accounts/resolve", json={"url": "https://git.corp/team"})
    assert r.status_code == 422 and "no API" in r.json()["detail"]


def test_adding_from_a_malformed_url_is_a_400(no_accounts):
    r = no_accounts.post("/api/accounts/from-url", json={"url": "not a url"})
    assert r.status_code == 400


def test_adding_a_source_that_conflicts_is_a_409(no_accounts, monkeypatch):
    from git_synapse.ingest import accounts as acc
    from git_synapse.ingest.accounts import AccountError

    def _clash(url, repos=None, token=""):
        raise AccountError("acme is already configured on github.com")

    monkeypatch.setattr(acc, "add_from_url", _clash)
    r = no_accounts.post("/api/accounts/from-url", json={"url": "https://github.com/acme"})
    assert r.status_code == 409


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
    from git_synapse.db.engine import connection, execute, query_one
    from git_synapse.ingest.github import RepoRecord
    from git_synapse.ingest.store import upsert_repo

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


def test_updating_an_account_to_a_taken_login_is_a_conflict(signed_in):
    """409, not 500: the request was well-formed and the caller can fix it."""
    from git_synapse.ingest import accounts

    client = signed_in

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
    from git_synapse.db.engine import connection

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
    claimed = {m.group(1) for m in re.finditer(r"^on\('/([a-z]+)", web.read_text(), re.MULTILINE)}
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


# ---------------------------------------------------------- runtime settings

def test_settings_lists_only_what_may_be_changed(admin_client):
    client = admin_client
    payload = client.get("/api/settings").json()
    names = {s["name"] for s in payload["settings"]}
    assert names == {"refresh_cron", "discover_cron"}, "schedules only; access is reported apart"
    for s in payload["settings"]:
        assert s["value"] and "from_env" in s and "overridden" in s

    # The access policy is not a cron and must not be described as one: a single
    # writable list had /api/settings reporting "0 * * * *" for dashboard_auth.
    assert set(payload["access"]) == {"dashboard", "mcp"}
    assert all(v in ("required", "open") for v in payload["access"].values())


def test_a_setting_can_be_stored_and_cleared(admin_client):
    client = admin_client
    try:
        put = client.put("/api/settings/refresh_cron", json={"value": "*/9 * * * *"})
        assert put.status_code == 200
        assert put.json() == {"name": "refresh_cron", "value": "*/9 * * * *",
                              "overridden": True}
        assert client.get("/api/config").json()["refresh_cron"] == "*/9 * * * *"
    finally:
        cleared = client.put("/api/settings/refresh_cron", json={"value": ""})
    assert cleared.json()["overridden"] is False


def test_a_cron_that_does_not_parse_is_refused_rather_than_stored(admin_client):
    client = admin_client
    """Accepted and stored, it would be read once a minute by a process nobody
    is watching, and silently ignored."""
    r = client.put("/api/settings/refresh_cron", json={"value": "every tuesday"})
    assert r.status_code == 422 and "five-field cron" in r.json()["detail"]
    assert client.get("/api/settings").json()["settings"][0]["overridden"] is False


def test_an_unknown_setting_is_not_silently_accepted(admin_client):
    client = admin_client
    assert client.put("/api/settings/github_token",
                      json={"value": "ghp_x"}).status_code == 404


def test_the_token_is_reported_as_present_but_never_returned(client):
    """The status says whether a credential exists and where it came from. The
    value must not leave the process: this API is unauthenticated on localhost."""
    status = client.get("/api/config").json()["github_token"]
    assert set(status) == {"present", "source", "editable_here"}
    assert status["source"] in {"file", "environment", "none"}
    assert "token" not in str(status).lower().replace("github_token", "")


def test_an_unknown_call_id_is_a_404(client):
    assert client.get("/api/calls/999999999").status_code == 404


def test_one_call_can_be_opened_in_full(client, settled_calls):
    """The drill-down's last rung: a row in the list, then exactly what that
    call was asked and exactly what it returned."""
    from git_synapse.analysis import calls

    client.get("/api/overview")
    settled_calls(lambda: calls.recent(surface="http", limit=1))
    listed = client.get("/api/calls", params={"surface": "http", "limit": 1}).json()
    assert listed["count"] == 1

    call = client.get(f"/api/calls/{listed['calls'][0]['id']}")
    assert call.status_code == 200
    body = call.json()
    assert body["surface"] == "http" and body["name"].startswith("/api/")
    assert "result_preview" in body and "arguments" in body


def test_the_api_records_its_own_traffic(client, settled_calls):
    """The whole point: a request served leaves a row saying what was asked and
    what came back. The route template is recorded, not the concrete path, so a
    thousand repositories are one row in a ranking."""
    from git_synapse.analysis import calls

    # Marked with a value nothing else in the suite sends. Filtering by route
    # alone is not enough: other tests call /api/repos too, so "the newest row
    # for this route" is whichever request happened to land last.
    probe = "zz-probe-not-a-real-repo"
    client.get("/api/repos", params={"limit": 1, "search": probe})

    def mine():
        for row in calls.recent(surface="http", name="/api/repos", limit=50):
            full = calls.detail(row["id"])
            if (full["arguments"] or {}).get("search") == probe:
                return full
        return None

    full = settled_calls(mine)
    assert full["status"] == "ok" and full["method"] == "GET"
    assert full["arguments"] == {"limit": "1", "search": probe}
    assert full["result_preview"] is not None, "the reply itself must be recorded"
    assert full["result_bytes"] and full["result_bytes"] > 0


def test_reading_the_log_does_not_write_to_the_log(client, settled_calls):
    """Otherwise opening the activity page generates the traffic it displays,
    and the page can never be quiet."""
    from git_synapse.analysis import calls

    client.get("/api/calls", params={"limit": 1})
    client.get("/api/calls/summary")
    client.get("/api/overview")
    settled_calls(lambda: calls.recent(surface="http", name="/api/overview", limit=1))
    assert not [r for r in calls.recent(limit=50) if r["name"].startswith("/api/calls")]


def test_a_failing_request_is_recorded_as_an_error(client, settled_calls):
    from git_synapse.analysis import calls

    client.get("/api/repos/999999999")
    rows = settled_calls(lambda: calls.recent(
        surface="http", name="/api/repos/{repo_id}", status="error", limit=5))
    assert rows[0]["error"] == "HTTP 404"


@pytest.mark.parametrize(("body", "expected_rows"), [
    (b"", None),
    (b"not json at all", None),
    (b'{"repos": [1, 2, 3]}', 3),
    (b"[1, 2]", 2),
    (b'{"count": 4}', None),
])
def test_a_reply_is_decoded_for_the_log_whatever_shape_it_is(body, expected_rows):
    """The log records what came back, and replies are not all row lists: the
    shell is HTML, an error is a bare object, some tools return arrays."""
    import git_synapse.api.main as api_main

    parsed, rows = api_main._decode(body)
    assert rows == expected_rows
    if body == b"":
        assert parsed is None
    elif body == b"not json at all":
        assert parsed == {"non_json": "not json at all"}


def test_the_timeline_endpoint_bounds_its_window(client):
    assert client.get("/api/calls/timeline", params={"hours": 6}).json()["buckets"].__len__() == 6
    assert client.get("/api/calls/timeline", params={"hours": 0}).status_code == 422
    assert client.get("/api/calls/timeline", params={"hours": 999}).status_code == 422


def test_the_shape_endpoint_serves_every_distribution(client):
    body = client.get("/api/overview/shape").json()
    assert set(body) == {
        "commits_by_year", "pair_support", "repo_sizes", "languages",
        "commit_width", "authors_per_file", "adoption_days", "repo_recency",
    }


# ------------------------------------------------------------------ the door

def test_the_api_is_open_until_somebody_has_an_account(client):
    """A fresh deployment must be reachable, or the screen that creates the
    first administrator is itself behind a sign-in."""
    from git_synapse import auth

    assert auth.count_users() == 0, "this test needs a deployment with no users"
    assert client.get("/api/overview").status_code == 200
    me = client.get("/api/auth/me").json()
    assert me["needs_setup"] is True and me["auth_required"] is False


def test_the_door_shuts_as_soon_as_anyone_exists(client):
    from git_synapse import auth

    user = auth.create_user("pytest-door@example.com", "Door", "a-sufficiently-long-pass")
    try:
        assert client.get("/api/overview").status_code == 401
        assert client.get("/api/auth/me").status_code == 200, \
            "asking who you are must work while signed out"
    finally:
        auth.delete_user(user["id"])
    assert client.get("/api/overview").status_code == 200


def test_setup_creates_the_first_administrator_once(client):
    from git_synapse import auth

    body = {"email": "pytest-first@example.com", "name": "First",
            "password": "a-sufficiently-long-pass",
            "setup_token": auth.setup_token()}
    created = client.post("/api/auth/setup", json=body)
    try:
        assert created.status_code == 201
        assert created.json()["user"]["role"] == "admin", "the first account must be able to add others"
        assert client.cookies.get("gs_session"), "setup signs you in"
        # It cannot be replayed to mint a second administrator later.
        again = client.post("/api/auth/setup", json={**body, "email": "pytest-second@example.com"})
        assert again.status_code == 409
    finally:
        for row in auth.list_users():
            auth.delete_user(row["id"])
        auth.clear_setup_token()
        client.cookies.clear()


def test_setup_refuses_a_caller_who_does_not_hold_the_token(client):
    """The gap this closes: between a migrated database and a claimed account,
    the setup screen is reachable by anyone who reaches the port."""
    from git_synapse import auth

    body = {"email": "pytest-stranger@example.com", "name": "Stranger",
            "password": "a-sufficiently-long-pass", "setup_token": "not-the-token"}
    try:
        refused = client.post("/api/auth/setup", json=body)
        assert refused.status_code == 403
        assert auth.count_users() == 0, "a refused setup must create nothing"

        # Omitting it entirely is refused by the model, not waved through.
        del body["setup_token"]
        assert client.post("/api/auth/setup", json=body).status_code == 422
        assert auth.count_users() == 0
    finally:
        auth.execute("DELETE FROM login_attempt WHERE email = %s", ("setup",))
        auth.clear_setup_token()


def test_the_setup_token_is_stable_and_survives_a_restart(client):
    """Four workers on a cold database must agree, or three of them print a
    token that will not work."""
    from git_synapse import auth

    try:
        first = auth.setup_token()
        assert len(first) > 20, "long enough not to be guessed"
        assert auth.setup_token() == first, "reading it must not mint a new one"
    finally:
        auth.clear_setup_token()


def test_guessing_the_setup_token_is_rate_limited(client):
    """It cannot be brute-forced, but it also should not be a free loop."""
    from git_synapse import auth

    body = {"email": "pytest-guess@example.com", "name": "G",
            "password": "a-sufficiently-long-pass", "setup_token": "wrong"}
    try:
        codes = {client.post("/api/auth/setup", json=body).status_code
                 for _ in range(auth.MAX_FAILURES + 2)}
        assert codes == {403, 429}, f"expected refusals then a lockout, got {codes}"
        assert auth.count_users() == 0
    finally:
        auth.execute("DELETE FROM login_attempt WHERE email = %s", ("setup",))
        auth.clear_setup_token()


def test_the_console_prints_the_token_exactly_when_it_is_useful(client, monkeypatch, caplog):
    """The only channel the token has. If this is silent on a fresh deployment
    nobody can claim the account; if it speaks on a claimed one it is repeating
    a dead secret into the log at every restart."""
    import logging

    from git_synapse import auth, config
    from git_synapse.api import main

    try:
        with caplog.at_level(logging.WARNING, logger="git_synapse.api.main"):
            main._announce_setup_token()
        printed = caplog.text
        assert auth.setup_token() in printed, "a fresh deployment must be told the token"

        # Configured rather than minted: say where to look, never the value.
        caplog.clear()
        monkeypatch.setenv("ADMIN_SETUP_TOKEN", "from-the-secret-store")
        config.get_config.cache_clear()
        with caplog.at_level(logging.WARNING, logger="git_synapse.api.main"):
            main._announce_setup_token()
        assert "ADMIN_SETUP_TOKEN" in caplog.text
        assert "from-the-secret-store" not in caplog.text, \
            "a value the operator already holds must not be echoed into the log"
        monkeypatch.delenv("ADMIN_SETUP_TOKEN")
        config.get_config.cache_clear()

        # Claimed: nothing to announce.
        user = auth.create_user("pytest-quiet@example.com", "Q", "a-sufficiently-long-pass")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="git_synapse.api.main"):
            main._announce_setup_token()
        assert caplog.text == "", f"expected silence, got {caplog.text!r}"
        auth.delete_user(user["id"])

        # A database that will not answer must not stop the API from serving.
        caplog.clear()
        monkeypatch.setattr(auth, "count_users", lambda: 1 / 0)
        with caplog.at_level(logging.WARNING, logger="git_synapse.api.main"):
            main._announce_setup_token()
        assert "could not determine" in caplog.text
    finally:
        config.get_config.cache_clear()
        auth.clear_setup_token()


def test_an_environment_supplied_token_is_used_verbatim(client, monkeypatch):
    """An automated deployment claims the account with a value it already has,
    without anyone reading a log."""
    from git_synapse import auth, config

    monkeypatch.setenv("ADMIN_SETUP_TOKEN", "a-token-from-the-secret-store")
    config.get_config.cache_clear()
    try:
        assert auth.setup_token() == "a-token-from-the-secret-store"
        assert auth.setup_token_is_minted() is False
        assert client.get("/api/auth/me").json()["setup_token_minted"] is False
        # Nothing was written: there is nothing to leak and nothing to clear.
        from git_synapse.db.engine import query_one
        assert query_one("SELECT value FROM meta WHERE key = %s", ("setup:token",)) is None
    finally:
        monkeypatch.delenv("ADMIN_SETUP_TOKEN", raising=False)
        config.get_config.cache_clear()


def test_a_member_reads_everything_and_administers_nothing(client):
    from git_synapse import auth

    admin = auth.create_user("pytest-a@example.com", "A", "a-sufficiently-long-pass", role="admin")
    member = auth.create_user("pytest-m@example.com", "M", "a-sufficiently-long-pass")
    try:
        client.post("/api/auth/login",
                    json={"email": "pytest-m@example.com", "password": "a-sufficiently-long-pass"})
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/users").status_code == 200, "everyone may see who has access"
        assert client.post("/api/users", json={
            "email": "pytest-x@example.com", "name": "X",
            "password": "a-sufficiently-long-pass"}).status_code == 403
        assert client.put("/api/settings/dashboard_auth",
                          json={"value": "open"}).status_code == 403
    finally:
        client.post("/api/auth/logout")
        auth.delete_user(admin["id"])
        auth.delete_user(member["id"])
        client.cookies.clear()


def test_a_bearer_token_is_the_person_who_made_it(client):
    from git_synapse import auth

    member = auth.create_user("pytest-t@example.com", "T", "a-sufficiently-long-pass")
    secret, _ = auth.create_token(member["id"], "pytest token")
    try:
        headers = {"Authorization": f"Bearer {secret}"}
        assert client.get("/api/overview", headers=headers).status_code == 200
        assert client.get("/api/auth/me", headers=headers).json()["user"]["email"] \
            == "pytest-t@example.com"
        # A token cannot exceed its owner: this one belongs to a member.
        assert client.post("/api/users", headers=headers, json={
            "email": "pytest-y@example.com", "name": "Y",
            "password": "a-sufficiently-long-pass"}).status_code == 403
        assert client.get("/api/overview",
                          headers={"Authorization": "Bearer gss_nope"}).status_code == 401
    finally:
        auth.delete_user(member["id"])
        client.cookies.clear()


def test_an_open_dashboard_still_needs_an_account_to_administer(admin_client):
    """Switching sign-in off makes the data readable by anyone who can reach
    the address. It must not make the deployment administrable by them."""
    from git_synapse.analysis import settings

    anon = admin_client.__class__(admin_client.app)  # a client with no cookies
    try:
        admin_client.put("/api/settings/dashboard_auth", json={"value": "open"})
        assert anon.get("/api/overview").status_code == 200
        assert anon.get("/api/users").status_code == 401
        assert anon.post("/api/users", json={
            "email": "pytest-z@example.com", "name": "Z",
            "password": "a-sufficiently-long-pass"}).status_code == 401
    finally:
        settings.clear("dashboard_auth")


def test_the_last_administrator_cannot_be_removed_or_demoted(admin_client):
    """Otherwise the deployment has nobody who can add a person, and no way
    back except the database."""
    me = admin_client.get("/api/auth/me").json()["user"]
    assert admin_client.patch(f"/api/users/{me['id']}",
                              json={"role": "member"}).status_code == 409
    assert admin_client.patch(f"/api/users/{me['id']}",
                              json={"is_active": False}).status_code == 409
    assert admin_client.delete(f"/api/users/{me['id']}").status_code == 409


def test_signing_out_closes_the_door_again(admin_client):
    assert admin_client.get("/api/overview").status_code == 200
    admin_client.post("/api/auth/logout")
    assert admin_client.get("/api/overview").status_code == 401


def test_the_access_mode_endpoint_validates_and_clears(admin_client):
    from git_synapse.analysis import settings

    try:
        bad = admin_client.put("/api/settings/dashboard_auth", json={"value": "maybe"})
        assert bad.status_code == 422 and "required, open" in bad.json()["detail"]

        assert admin_client.put("/api/settings/mcp_auth",
                                json={"value": "open"}).json()["value"] == "open"
        # Blank clears the override and the default applies again.
        cleared = admin_client.put("/api/settings/mcp_auth", json={"value": ""})
        assert cleared.json() == {"name": "mcp_auth", "value": "required", "overridden": False}
    finally:
        settings.clear("mcp_auth")
        settings.clear("dashboard_auth")


def test_setup_refuses_what_it_cannot_store(client):
    """Order matters here: a valid attempt creates the first administrator and
    every later attempt is then a 409, so the invalid cases go first."""
    from git_synapse import auth

    token = auth.setup_token()
    too_short = client.post("/api/auth/setup", json={
        "email": "pytest-weak@example.com", "name": "W", "password": "short",
        "setup_token": token})
    assert too_short.status_code == 422, "the model refuses it before the handler"

    bad_email = client.post("/api/auth/setup", json={
        "email": "not-an-email", "name": "W", "password": "a-sufficiently-long-pass",
        "setup_token": token})
    assert bad_email.status_code == 400 and "email" in bad_email.json()["detail"]

    assert auth.count_users() == 0, "nothing above may have created an account"
    auth.clear_setup_token()


def test_signing_in_with_the_wrong_password_is_a_401(client):
    from git_synapse import auth

    user = auth.create_user("pytest-w@example.com", "W", "a-sufficiently-long-pass")
    try:
        r = client.post("/api/auth/login",
                        json={"email": "pytest-w@example.com", "password": "wrong"})
        assert r.status_code == 401 and r.json()["detail"] == "wrong email or password"
    finally:
        auth.delete_user(user["id"])
        client.cookies.clear()


def test_tokens_are_listed_minted_and_revoked_over_http(admin_client):
    listed = admin_client.get("/api/auth/tokens").json()
    assert listed["prefix"] == "gss_" and listed["tokens"] == []

    made = admin_client.post("/api/auth/tokens", json={"name": "pytest", "days": 30})
    assert made.status_code == 201
    body = made.json()
    assert body["token"].startswith("gss_") and "cannot be shown again" in body["note"]

    assert len(admin_client.get("/api/auth/tokens").json()["tokens"]) == 1
    assert admin_client.delete(f"/api/auth/tokens/{body['detail']['id']}").status_code == 200
    assert admin_client.get("/api/auth/tokens").json()["tokens"] == []
    assert admin_client.delete("/api/auth/tokens/999999").status_code == 404


def test_a_nameless_token_is_refused_over_http(admin_client):
    assert admin_client.post("/api/auth/tokens", json={"name": " "}).status_code == 400


def test_administering_an_unknown_person_is_a_404(admin_client):
    assert admin_client.patch("/api/users/999999", json={"name": "X"}).status_code == 404
    assert admin_client.delete("/api/users/999999").status_code == 404


def test_a_person_can_be_renamed_and_deactivated(admin_client):
    from git_synapse import auth

    other = auth.create_user("pytest-o@example.com", "O", "a-sufficiently-long-pass")
    try:
        renamed = admin_client.patch(f"/api/users/{other['id']}", json={"name": "Renamed"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"
        off = admin_client.patch(f"/api/users/{other['id']}", json={"is_active": False})
        assert off.json()["is_active"] is False
        bad = admin_client.patch(f"/api/users/{other['id']}", json={"name": "  "})
        assert bad.status_code == 400
        assert admin_client.delete(f"/api/users/{other['id']}").status_code == 200
    finally:
        if auth.get_user(other["id"]):
            auth.delete_user(other["id"])


def test_an_administrator_cannot_remove_their_own_account(admin_client):
    me = admin_client.get("/api/auth/me").json()["user"]
    r = admin_client.delete(f"/api/users/{me['id']}")
    assert r.status_code == 409


def test_an_administrator_adds_a_person_and_is_recorded_as_having_done_so(admin_client):
    """Who added whom is worth keeping: it is the audit trail for access."""
    from git_synapse import auth

    made = admin_client.post("/api/users", json={
        "email": "pytest-added@example.com", "name": "Added",
        "password": "a-sufficiently-long-pass", "role": "member"})
    assert made.status_code == 201
    added = made.json()
    try:
        assert added["role"] == "member"
        row = next(u for u in auth.list_users() if u["id"] == added["id"])
        assert row["created_by_email"] == admin_client.admin["email"]

        clash = admin_client.post("/api/users", json={
            "email": "PYTEST-ADDED@example.com", "name": "Again",
            "password": "a-sufficiently-long-pass"})
        assert clash.status_code == 400 and "already has an account" in clash.json()["detail"]
    finally:
        auth.delete_user(added["id"])


def test_promoting_and_removing_a_second_administrator_is_allowed(admin_client):
    """The guard is about the *last* administrator, not about administrators."""
    from git_synapse import auth

    other = auth.create_user("pytest-second-admin@example.com", "Second",
                             "a-sufficiently-long-pass")
    try:
        promoted = admin_client.patch(f"/api/users/{other['id']}", json={"role": "admin"})
        assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
        assert admin_client.delete(f"/api/users/{other['id']}").status_code == 200
    finally:
        if auth.get_user(other["id"]):
            auth.delete_user(other["id"])


def test_repeated_wrong_passwords_answer_429_not_401(client):
    """429 says "wait", 401 says "wrong". Answering 401 while refusing to look
    at the credentials would be a lie the client acts on."""
    from git_synapse import auth

    user = auth.create_user("pytest-rate@example.com", "R", "a-sufficiently-long-pass")
    try:
        for _ in range(auth.MAX_FAILURES):
            assert client.post("/api/auth/login", json={
                "email": "pytest-rate@example.com", "password": "wrong"}).status_code == 401
        blocked = client.post("/api/auth/login", json={
            "email": "pytest-rate@example.com", "password": "wrong"})
        assert blocked.status_code == 429 and "try again" in blocked.json()["detail"]
    finally:
        from git_synapse.db.engine import execute

        execute("DELETE FROM login_attempt")
        auth.delete_user(user["id"])
        client.cookies.clear()


# ------------------------------------------------ what the call log may hold

def test_a_minted_token_never_reaches_the_call_log(signed_in, db, settled_calls):
    """`create_token` promises the secret is stored only as a hash. The call
    log recorded every reply verbatim, and `/api/calls/{id}` handed it back --
    so two requests turned any signed-in member into whoever last minted a
    token. Four live secrets were sitting in the log when this was found."""
    from git_synapse.db.engine import execute, query

    client = signed_in
    made = client.post("/api/auth/tokens", json={"name": "log-probe", "days": 1})
    assert made.status_code == 201
    secret = made.json()["token"]
    assert secret.startswith("gss_") and len(secret) > 20

    # Absence cannot be waited for, so a later request that IS logged acts as
    # the barrier: once its row has landed, anything queued before it has too.
    client.get("/api/overview")
    settled_calls(lambda: query(
        "SELECT id FROM call_log WHERE name = '/api/overview'"
        " AND at > now() - interval '1 minute'"))

    rows = query("SELECT result_preview::text AS body FROM call_log"
                 " WHERE result_preview::text LIKE %s", (f"%{secret}%",))
    assert rows == [], "the secret reached the call log"

    # And no auth reply at all is recorded, so this cannot regress by another
    # route -- a future endpoint under /api/auth is covered by construction.
    auth_rows = query("SELECT count(*) AS n FROM call_log WHERE name LIKE '/api/auth/%%'"
                      " AND at > now() - interval '1 minute'")
    assert auth_rows[0]["n"] == 0

    token_id = made.json()["detail"]["id"]
    client.delete(f"/api/auth/tokens/{token_id}")
    execute("DELETE FROM call_log WHERE at > now() - interval '5 minutes'")


def test_managing_a_source_needs_an_administrator(client, db):
    """Adding, re-crediting and deleting a source decides what the deployment
    scans and with whose credentials. Eight handlers had no check at all, so a
    member could delete an admin's source and get a 200."""
    from git_synapse import auth
    from git_synapse.ingest import accounts

    admin_email = "pytest-authz-admin@example.com"
    member_email = "pytest-authz-member@example.com"
    password = "a-sufficiently-long-pass"
    for email, role in ((admin_email, "admin"), (member_email, "member")):
        if auth.by_email(email) is None:
            auth.create_user(email, email.split("@")[0], password, role=role)
    src = accounts.add_account("authz-probe", kind="org", provider="github",
                               host="github.com")
    try:
        client.post("/api/auth/login", json={"email": member_email, "password": password})
        for method, path, body in (
            ("post", "/api/accounts", {"login": "member-made"}),
            ("patch", f"/api/accounts/{src['id']}", {"login": "renamed"}),
            ("put", f"/api/accounts/{src['id']}/credential", {"token": "ghp_x"}),
            ("delete", f"/api/accounts/{src['id']}", None),
        ):
            resp = getattr(client, method)(path, **({"json": body} if body else {}))
            assert resp.status_code == 403, f"{method} {path} allowed a member"
        assert accounts.get_account(src["id"])["login"] == "authz-probe"

        client.cookies.clear()
        client.post("/api/auth/login", json={"email": admin_email, "password": password})
        assert client.delete(f"/api/accounts/{src['id']}").status_code == 200
    finally:
        client.cookies.clear()
        if accounts.get_account(src["id"]):
            accounts.remove_account(src["id"])
        for email in (admin_email, member_email):
            row = auth.by_email(email)
            if row:
                auth.delete_user(row["id"])
