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


def test_unknown_repo_id_is_a_404_not_an_empty_success(client):
    assert client.get("/api/repos/999999999").status_code == 404


@pytest.mark.parametrize("bad", ["abc", "1e5", "-"])
def test_non_numeric_repo_id_is_refused(client, bad):
    assert client.get(f"/api/repos/{bad}").status_code in (404, 422)


def test_repo_zero_is_not_treated_as_unset(client):
    """`if repo_id:` made 0 mean "no filter" and returned the whole corpus
    dressed as one repository's data."""
    scoped = client.get("/api/repos/0/hotspots", params={"limit": 2}).json()
    corpus = client.get("/api/hotspots", params={"limit": 2}).json()
    assert scoped != corpus


# --------------------------------------------------------------- coupling

def test_coupled_partners_are_oriented_and_ranked(client):
    f = client.get("/api/files/resolve",
                   params={"repo": "acme/runtime", "path": "go.mod"})
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
    for path in ("/repos", "/impact", "/insights", "/feedback"):
        r = client.get(path)
        assert r.status_code == 200
        assert "<" in r.text


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
