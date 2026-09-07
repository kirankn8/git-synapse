"""The MCP tools, called the way an agent calls them.

These are the only surface an agent sees. A wrong shape here does not raise --
it becomes a confident answer, which is the expensive kind of wrong.
"""
from __future__ import annotations

import pytest

from git_synapse.mcp import server

# ------------------------------------------------------------- resolution

@pytest.mark.parametrize("name", ["", "   ", "definitely-not-a-repo", "hubbl", "api"])
def test_a_name_that_is_not_a_repository_is_refused(db, name):
    """A substring match resolved to the first hit, so a typo answered
    confidently about a different repository."""
    out = server.upstream_repos(repo=name)
    assert "error" in out, f"{name!r} resolved to {out.get('repo')}"


def test_real_names_full_names_and_case_all_resolve(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        candidates = session.query(models().Repo).filter_by(is_enabled=True).all()
        counts = {}
        for candidate in candidates:
            counts[candidate.name] = counts.get(candidate.name, 0) + 1
        row = next((candidate for candidate in candidates if counts[candidate.name] == 1), None)
    if row is None:
        pytest.skip("no repositories")
    for form in (row.name, row.full_name, row.name.upper()):
        out = server.upstream_repos(repo=form)
        assert "error" not in out, f"{form!r} failed to resolve"
        assert out["repo"] == row.full_name


# ---------------------------------------------------------- coupled_files

def test_coupled_files_rejects_an_unknown_path_with_a_hint(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    out = server.coupled_files(repo=row.name, path="no/such/file.go")
    assert "error" in out and "hint" in out


def test_coupled_files_shape_is_stable(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().Repo.is_enabled.is_(True), models().File.change_count > 30,
                 models().File.is_deleted.is_(False)).first()
    if row is None:
        pytest.skip("no busy file")
    out = server.coupled_files(repo=row.name, path=row.path, min_support=3, limit=5, detail=True)
    assert "error" not in out, out
    assert out["file"]["path"] == row.path
    for p in out["partners"]:
        for key in ("path", "score", "co_changes", "partner_total_changes",
                    "probability_also_changes", "informative", "currency"):
            assert key in p, f"{key} missing from a partner row"
        assert p["partner_total_changes"] >= p["co_changes"]
        assert 0.0 <= p["probability_also_changes"] <= 1.0


def test_coupled_files_default_is_a_compact_evidence_card(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().Repo.is_enabled.is_(True), models().File.change_count > 30,
                 models().File.is_deleted.is_(False)).first()
    if row is None:
        pytest.skip("no busy file")
    out = server.coupled_files(repo=row.name, path=row.path, min_support=3, limit=5)
    assert "error" not in out, out
    assert out["measure"] is None
    for partner in out["partners"]:
        assert {"path", "evidence", "support", "recency_days", "agreement", "summary"} <= partner.keys()
        assert "log_likelihood_ratio" not in partner


def test_an_unknown_measure_is_refused(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().Repo.is_enabled.is_(True)).first()
    out = server.coupled_files(repo=row.name, path=row.path, measure="nope", detail=True)
    assert "error" in out


# ----------------------------------------------------------------- currency

@pytest.mark.parametrize(
    ("days", "trend", "deleted", "starts"),
    [
        (3, None, True, "DELETED"),        # deletion outranks everything
        (3, "emerging", True, "DELETED"),
        (400, "emerging", False, "STALE"), # age outranks trend
        (400, "decaying", False, "STALE"),
        (400, None, False, "STALE"),
        (100, "decaying", False, "DECAYING"),
        (12, "emerging", False, "emerging"),
        (5, None, False, "current"),
        (60, None, False, "co-changed"),
    ],
)
def test_currency_precedence(days, trend, deleted, starts):
    assert server._describe_currency(days, trend, deleted).startswith(starts)


def test_currency_is_absent_when_recency_is_unknown():
    assert server._describe_currency(None, None, False) is None


# ------------------------------------------------------------- module tools

def test_module_context_rejects_a_path_that_does_not_exist(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    out = server.module_context(repo=row.name, path="not/real.go")
    assert "error" in out


def test_coupled_directories_accepts_a_file_or_a_directory(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path, models().File.dir_path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().Repo.is_enabled.is_(True), models().File.dir_path != "",
                 models().File.change_count > 20).first()
    if row is None:
        pytest.skip("no suitable file")
    by_file = server.coupled_directories(repo=row.name, path=row.path, limit=5)
    by_dir = server.coupled_directories(repo=row.name, path=row.dir_path, limit=5)
    assert by_file["directory"]["path"] == by_dir["directory"]["path"] == row.dir_path


# --------------------------------------------------------------- catalogue

def test_list_measures_describes_every_measure(db):
    out = server.list_measures()
    measures = out["measures"] if isinstance(out, dict) else out
    assert len(measures) >= 29
    assert all(m.get("key") and m.get("label") for m in measures)


def test_list_repositories_returns_usable_names(corpus):
    out = server.list_repositories(limit=5)
    repos = out["repositories"] if isinstance(out, dict) else out
    assert repos
    for r in repos:
        assert r.get("name")


def test_search_files_finds_a_known_path(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().File).filter_by(basename="go.mod").first()
    if row is None:
        pytest.skip("no go.mod indexed")
    out = server.search_files(term="go.mod", limit=5)
    files = out["files"] if isinstance(out, dict) else out
    assert files


def test_report_gap_rejects_an_opinion_and_accepts_a_defect(db):
    from git_synapse.db.engine import connection
    from git_synapse.db.orm import models

    bad = server.report_gap(kind="opinion", detail="I disagree with the ranking")
    assert "error" in bad

    good = server.report_gap(
        kind="wrong_data", detail="PROBE mcp tool test", severity="low",
        tool="coupled_files", repo="t/probe", expected="x", observed="y",
    )
    assert "error" not in good and good.get("id")
    with connection() as conn:
        conn.delete(conn.get(models().Feedback, good["id"]))


# ------------------------------------------------------- the explain tools

def test_explain_pair_answers_in_the_callers_argument_order(db):
    """Storage canonicalises by id; returning that order silently transposed
    the answer, so confidence_ab was the reverse conditional half the time."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path, models().FilePairMetric).join(
            models().FilePairMetric, models().FilePairMetric.repo_id == models().Repo.id,
        ).join(models().File, models().File.id == models().FilePairMetric.file_a_id).filter(
            models().FilePairMetric.n_ab > 5,
        ).first()
    if row is None:
        pytest.skip("no supported pair")

    # The first joined file is `a`; choose its partner from the same metric.
    with session_scope() as session:
        metric = row[2]
        partner = session.get(models().File, metric.file_b_id)
    fwd = server.explain_pair(repo=row[0], path_a=row[1], path_b=partner.path)
    rev = server.explain_pair(repo=row[0], path_a=partner.path, path_b=row[1])
    assert fwd["path_a"] == row[1] and rev["path_a"] == partner.path
    assert fwd["measures"]["confidence_ab"] == rev["measures"]["confidence_ba"]


def test_explain_pair_rejects_an_unknown_path(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().Repo.is_enabled.is_(True)).first()
    out = server.explain_pair(repo=row.name, path_a=row.path, path_b="no/such.go")
    assert "error" in out


def test_impact_of_change_and_upstream_agree(db):
    from sqlalchemy.orm import aliased

    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        target_repo = aliased(models().Repo)
        row = session.query(models().Repo.name, models().RepoImpact, target_repo).join(
            models().RepoImpact, models().RepoImpact.source_repo_id == models().Repo.id,
        ).join(target_repo, target_repo.id == models().RepoImpact.target_repo_id).filter(
            models().RepoImpact.is_declared.is_(True) |
            models().RepoImpact.has_bump_history.is_(True),
        ).first()
    if row is None:
        pytest.skip("no validated edge")

    # Query the endpoint using the two repository names; the exact projection
    # is deliberately kept ORM-only in this fixture.
    source = row[0]
    target = row[2].name
    down = server.impact_of_change(repo=source)
    up = server.upstream_repos(repo=target)
    assert any(x["repo"].endswith(target) for x in down.get("downstream", []))
    assert any(x["repo"].endswith(source) for x in up.get("upstream", []))


def test_file_history_returns_commits_for_a_real_file(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().File.change_count > 5, models().Repo.is_enabled.is_(True)).first()
    if row is None:
        pytest.skip("no busy file")
    out = server.file_history(repo=row.name, path=row.path, limit=5)
    assert "error" not in out
    assert out["recent_commits"]
    assert out["total_changes"] >= len(out["recent_commits"])


def test_repo_hotspots_are_ranked(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).order_by(
            models().Repo.commit_count.desc(),
        ).first()
    out = server.repo_hotspots(repo=row.name, limit=5)
    rows = out.get("hotspots", [])
    counts = [h["changes"] for h in rows]
    assert counts == sorted(counts, reverse=True)


def test_coupling_chain_direction_is_honoured(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    up = server.coupling_chain(repo=row.name, direction="upstream")
    down = server.coupling_chain(repo=row.name, direction="downstream")
    assert up["direction"] == "upstream"
    assert down["direction"] == "downstream"


def test_an_empty_chain_explains_itself(db):
    """A bare [] conflated "searched and found nothing" with "nothing to search"."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        candidates = session.query(models().Repo).join(
            models().RepoImpact, models().RepoImpact.target_repo_id == models().Repo.id,
        ).all()
        row = next((repo for repo in candidates if not session.query(models().RepoImpact).filter(
            models().RepoImpact.target_repo_id == repo.id,
            models().RepoImpact.is_declared.is_(True) |
            models().RepoImpact.has_bump_history.is_(True),
        ).first()), None)
    if row is None:
        pytest.skip("no all-discovery repository")
    out = server.coupling_chain(repo=row.name, direction="upstream")
    assert out["chains"] == [] and out["explanation"]


# ------------------------------------------------------ module_context prose

def test_module_context_describes_a_multi_module_repository(db):
    """In a monorepo the module graph is the structure; the guidance has to say
    which direction a change propagates."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().ModuleDependency, models().ModuleDependency.repo_id == models().Repo.id,
        ).join(models().File, models().File.repo_id == models().Repo.id).filter(
            models().File.dir_path.startswith(models().ModuleDependency.consumer_module),
        ).first()
    if row is None:
        pytest.skip("no multi-module repository indexed")
    out = server.module_context(repo=row.name, path=row.path)
    assert "error" not in out, out
    assert out["multi_module"] is True
    assert out.get("guidance")


def test_module_context_says_so_for_a_single_module_repository(db):
    """"There is no internal module graph" is a real answer, not an empty one."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = next((candidate for candidate in session.query(models().Repo.id, models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().File.change_count > 5).all()
                    if session.query(models().ModuleDependency).filter_by(repo_id=candidate[0]).first() is None), None)
    if row is None:
        pytest.skip("every repository is multi-module")
    out = server.module_context(repo=row.name, path=row.path)
    assert out.get("multi_module") is False
    assert "single-module" in (out.get("note") or "")


def test_search_files_scoped_to_a_repository_stays_in_it(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).order_by(
            models().Repo.commit_count.desc(),
        ).first()
    out = server.search_files(term="go", repo=row.name, limit=5)
    files = out["files"] if isinstance(out, dict) else out
    for f in files:
        assert f.get("repo", "").endswith(row.name)


def test_search_files_with_no_match_is_an_empty_list_not_an_error(db):
    out = server.search_files(term="zzz-definitely-no-such-path-zzz", limit=5)
    files = out["files"] if isinstance(out, dict) else out
    assert files == []


def test_list_repositories_can_be_filtered(corpus):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    out = server.list_repositories(search=row.name, limit=10)
    repos = out["repositories"] if isinstance(out, dict) else out
    # The tool reports full names, which is what an agent should pass back.
    assert any(r["name"].endswith(f'/{row.name}') or r["name"] == row.name
               for r in repos), [r["name"] for r in repos][:5]


# ---------------------------------------------- the evidence prose, exhaustively

@pytest.mark.parametrize(("declared", "bumped"), [(0, 0), (3, 0), (0, 4), (2, 5)])
def test_evidence_guidance_states_the_composition_it_is_describing(declared, bumped):
    """The guidance is read before the scores, so it has to say what the list is
    made of. The score mixes both tiers and cannot reveal that on its own."""
    rows = ([{"is_declared": True, "has_bump_history": True}] * declared
            + [{"is_declared": False, "has_bump_history": True}] * bumped)
    out = server._evidence_guidance(rows, "upstream")
    if not rows:
        assert "No upstream edges recorded" in out
    else:
        assert f"{declared} declared" in out and f"{bumped} bump-backed" in out
        assert "no statistical tier" in out


def test_confidence_wording_matches_the_support_behind_it():
    """A percentage from a handful of commits and one from three hundred must
    not read the same. They did, and the confident phrasing on thin support is
    what sent a reviewer at files their own reading had already ruled out."""
    strong = server._describe_confidence(0.9, 200)
    thin = server._describe_confidence(0.9, 4)
    assert strong != thin
    assert "very likely" in strong
    assert "provisional" in thin and "very likely" not in thin

    # Below the reportable floor it is called weak outright.
    assert "weak evidence" in server._describe_confidence(0.9, 2)
    assert server._describe_confidence(None, 10) == "no directional signal"


# ------------------------------------- every tool, against inputs that do not exist

REPO_TOOLS = [
    "upstream_repos", "impact_of_change", "coupling_chain", "module_context",
    "repo_hotspots", "coupled_files", "crossrepo_files", "file_history",
    "coupled_directories", "explain_pair",
]


def test_a_directory_with_no_coupling_says_so_rather_than_returning_nothing(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().Directory.path).join(
            models().Directory, models().Directory.repo_id == models().Repo.id,
        ).filter(models().Directory.change_count <= 1).first()
    if row is None:
        pytest.skip("no quiet directory")
    out = server.coupled_directories(repo=row.name, path=row.path)
    if "error" in out:
        pytest.skip("directory not indexed for coupling")
    assert out["summary"], "an empty result still needs a sentence"


# ---------------------------------------------- the sentences an agent acts on
#
# These strings are the product. A number an agent misreads as strong evidence
# costs more than a wrong number, because it is acted on with confidence.

@pytest.mark.parametrize(("confidence", "n_ab", "must_contain"), [
    (None, 100, "no directional signal"),
    (0.0, 100, "no directional signal"),
    (0.95, 2, "weak evidence"),
    (0.95, 300, "very likely needs updating too"),
    (0.95, 4, "may need updating too"),
    (0.55, 300, "worth checking"),
    (0.20, 300, "occasional"),
    (0.20, 4, "provisional"),
])
def test_a_confidence_reads_differently_at_different_support(confidence, n_ab,
                                                             must_contain):
    """90% of three commits and 90% of three hundred used to read identically."""
    assert must_contain in server._describe_confidence(confidence, n_ab)


def test_thin_support_never_reads_as_a_strong_recommendation():
    strong = server._describe_confidence(0.99, 400)
    thin = server._describe_confidence(0.99, server.THIN_SUPPORT - 1)
    assert "very likely" in strong
    assert "very likely" not in thin


def test_coupled_files_leads_with_the_sibling_variants_it_found(db, monkeypatch):
    """Parallel copies of the same filename in another directory are the case
    this tool finds that reading one file does not."""
    monkeypatch.setattr(server.q, "coupled_files", lambda *a, **k: [
        {"path": "b/handler.go", "n_ab": 40, "n_this": 50, "n_other": 45,
         "score": 0.8, "confidence_out": 0.8, "confidence_in": 0.7,
         "file_id": 1, "last_together": None, "repo": "t/x"},
    ])
    out = _coupled_on_any_file(monkeypatch)
    assert "sibling variant" in (out.get("summary") or "")


def test_coupled_files_warns_when_its_partners_are_all_noise(db, monkeypatch):
    """Own tests and generated output co-change by construction and tell an
    agent nothing its own reading did not."""
    monkeypatch.setattr(server.q, "coupled_files", lambda *a, **k: [
        {"path": "a/handler_test.go", "n_ab": 1, "n_this": 50, "n_other": 45,
         "score": 0.8, "confidence_out": 0.8, "confidence_in": 0.7,
         "file_id": 1, "last_together": None, "repo": "t/x"},
    ])
    out = _coupled_on_any_file(monkeypatch)
    assert "informative: false" in (out.get("summary") or "")


def _coupled_on_any_file(monkeypatch):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().File.path.like("%.go")).first()
    if row is None:
        pytest.skip("no files")
    monkeypatch.setattr(server.q, "resolve_file", lambda *a, **k: {
        "id": 1, "repo": row.name, "path": "a/handler.go", "repo_id": 1,
        "change_count": 50, "author_count": 3, "last_change_at": None,
        "is_deleted": False, "pair_population": 120,
    })
    return server.coupled_files(repo=row.name, path=row.path)


def test_a_mock_file_is_labelled_generated_not_coupled_behaviour():
    """A mock changes with the interface it mocks by construction."""
    labels, _informative = server._classify_partner("pkg/mock_client.go",
                                                   "pkg/client.go", 40)
    assert "generated" in labels


def test_two_files_that_never_changed_together_say_so_rather_than_erroring(db,
                                                                          monkeypatch):
    """`coupled: false` with a reason is actionable; an error is not."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    if row is None:
        pytest.skip("no repositories")
    monkeypatch.setattr(server.q, "resolve_file", lambda repo, path: {
        "id": 1 if path == "a.go" else 2, "repo": repo, "path": path,
        "repo_id": 1, "change_count": 5, "author_count": 1,
        "last_change_at": None, "is_deleted": False,
    })
    monkeypatch.setattr(server.q, "pair_detail", lambda *a, **k: None)
    out = server.explain_pair(repo=row.name, path_a="a.go", path_b="b.go")
    assert out["coupled"] is False
    assert "never changed in the same commit" in out["reason"]


@pytest.mark.parametrize(("counts", "must_contain"), [
    ({"total": 4, "validated": 0}, "none of this repository's 4"),
    ({"total": 0, "validated": 0}, "no edges of any kind"),
    ({"total": 4, "validated": 2}, "but none extends to a second hop"),
])
def test_an_empty_chain_explains_which_kind_of_empty_it_is(db, monkeypatch,
                                                           counts, must_contain):
    """"No chains" from an unvalidated corpus and "no chains" from a genuinely
    flat one are different answers, and an agent acts differently on each."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    if row is None:
        pytest.skip("no repositories")
    monkeypatch.setattr(server.predict, "impact_chains", lambda *a, **k: [])
    monkeypatch.setattr(server.q, "impact_edge_counts", lambda *a, **k: counts)
    out = server.coupling_chain(repo=row.name, direction="downstream")
    assert must_contain.lower() in out["explanation"].lower()


def test_every_impact_edge_carries_evidence(corpus, db):
    """The claim the product rests on, checked against the data rather than
    asserted: an edge is written only from a dependency declared in a manifest
    or from an observed version bump. There is no inferred tier to withhold,
    warn about, or filter -- so no surface offers to."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        rows = session.query(models().RepoImpact).all()
    unprovable = sum(not row.is_declared and not row.has_bump_history for row in rows)
    assert unprovable == 0, (
        f"{unprovable} of {len(rows)} impact edges rest on nothing; "
        "predict.rebuild must only write declared or bump-backed rows"
    )


def test_an_empty_shortlist_is_described_not_left_blank():
    assert "No upstream edges recorded" in server._evidence_guidance([], "upstream")


def test_coupled_directories_refuses_a_measure_it_does_not_have(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path).join(
            models().File, models().File.repo_id == models().Repo.id,
        ).filter(models().File.path.like("%/%")).first()
    if row is None:
        pytest.skip("no files")
    out = server.coupled_directories(repo=row.name, path=row.path,
                                     measure="not_a_measure")
    assert "error" in out


def test_module_context_calls_a_leaf_a_leaf(db, monkeypatch):
    """Silence and "nothing depends on this" are different answers."""
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    if row is None:
        pytest.skip("no repositories")
    monkeypatch.setattr(server.q, "resolve_file", lambda repo, path: {
        "id": 1, "repo": repo, "path": path, "repo_id": 1, "change_count": 1,
        "author_count": 1, "last_change_at": None, "is_deleted": False,
    })
    monkeypatch.setattr(server.q, "module_context", lambda *a, **k: {
        "owning_module": "gateway", "declares": [], "declared_by": [],
        "modules": ["gateway", "core"], "manifest": "gateway/go.mod",
    })
    out = server.module_context(repo=row.name, path="gateway/main.go")
    assert "leaf" in out["guidance"]


def test_search_files_refuses_a_repository_it_cannot_resolve(db):
    out = server.search_files(term="handler", repo="definitely-not-a-repo")
    assert "error" in out


def test_search_files_scopes_to_a_repository_when_one_resolves(db):
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        row = session.query(models().Repo).filter_by(is_enabled=True).first()
    if row is None:
        pytest.skip("no repositories")
    out = server.search_files(term="a", repo=row.name, limit=3)
    assert "error" not in out


@pytest.mark.parametrize(("argv", "expected"), [
    ([], {"transport": "stdio"}),
    (["--transport", "sse", "--port", "9999"], {"transport": "sse", "port": 9999}),
])
def test_each_transport_starts_the_server_the_way_it_is_meant_to(monkeypatch, argv,
                                                                expected):
    """stdio speaks JSON-RPC on stdout; picking the wrong transport is a silent
    protocol failure, not a crash."""
    started = {}
    monkeypatch.setattr(server, "wait_for_database", lambda *a, **k: None)
    monkeypatch.setattr(server, "apply_schema", lambda *a, **k: None)
    monkeypatch.setattr(server.server, "run",
                        lambda **kw: started.update(kw))
    monkeypatch.setattr(server, "_serve",
                        lambda app, host, port: started.update(
                            transport="sse", host=host, port=port))
    assert server.main(argv) == 0
    for key, value in expected.items():
        assert started[key] == value


def test_http_serves_the_guarded_app_rather_than_the_bare_one(monkeypatch):
    """The token check lives in a wrapper around the ASGI app, so http cannot
    go through `server.run` -- and a test that patches `run` would sit waiting
    on a real server instead of failing."""
    served = {}
    monkeypatch.setattr(server, "wait_for_database", lambda *a, **k: None)
    monkeypatch.setattr(server, "apply_schema", lambda *a, **k: None)
    monkeypatch.setattr(server, "_publish_tool_inventory", lambda: None)
    monkeypatch.setattr(server, "_serve",
                        lambda app, host, port: served.update(app=app, host=host, port=port))

    assert server.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8081"]) == 0
    assert served["host"] == "0.0.0.0" and served["port"] == 8081
    assert served["app"] is not None


# ----------------------------------------------- explaining a repository pair

def test_explain_repo_pair_names_the_repository_it_cannot_find(monkeypatch):
    """Two unknown names would otherwise produce an empty explanation that
    reads as 'these are unrelated' rather than 'I do not know them'."""
    monkeypatch.setattr(server, "_resolve_repo", lambda name: None)
    out = server.explain_repo_pair("acme/one", "acme/two")
    assert "unknown repository" in out["error"]


@pytest.mark.parametrize(("declared", "impact", "expected"), [
    ("declared", {"bump_count": 4}, "bumped it 4 times"),
    ("declared", {"bump_count": 0}, "no bump has been observed"),
    (None, {"bump_count": 3}, "bumped"),
    (None, None, "no declared dependency"),
])
def test_a_repository_pair_is_described_by_its_evidence(declared, impact, expected):
    """Each tier reads differently on purpose: a declaration and an observed
    bump are not the same claim, and an agent acts on the difference."""
    a = {"name": "lib", "full_name": "acme/lib"}
    b = {"name": "app", "full_name": "acme/app"}
    assert expected in server._describe_repo_pair(a, b, declared, impact)


def test_resolving_a_repository_accepts_either_spelling(monkeypatch):
    """Agents pass whichever they have -- `guava` or `google/guava` -- and
    failing on one of them would look like the repository is not indexed."""
    rows = [{"id": 1, "name": "lib", "full_name": "acme/lib"}]
    monkeypatch.setattr(server.q, "list_repos", lambda **k: rows)
    assert server._resolve_repo("lib")["id"] == 1
    assert server._resolve_repo("acme/lib")["id"] == 1


def test_an_unambiguous_suffix_resolves_but_an_ambiguous_one_does_not(monkeypatch):
    """A substring match is not a resolution: answering confidently about the
    wrong repository is worse than saying the name was not found."""
    monkeypatch.setattr(server.q, "list_repos", lambda **k: [
        {"id": 1, "name": "core", "full_name": "acme/core"}])
    assert server._resolve_repo("core")["id"] == 1

    monkeypatch.setattr(server.q, "list_repos", lambda **k: [
        {"id": 1, "name": "core", "full_name": "acme/core"},
        {"id": 2, "name": "core", "full_name": "other/core"}])
    assert server._resolve_repo("kernel/core") is None


def test_resolving_an_unknown_repository_returns_nothing(monkeypatch):
    monkeypatch.setattr(server.q, "list_repos", lambda **k: [])
    assert server._resolve_repo("nope") is None


def test_explain_repo_pair_reports_every_kind_of_evidence(monkeypatch):
    """The whole point of this tool is that a declaration, an observed bump and
    a reverse edge are different claims. Collapsing them would let an agent act
    on a coincidence as though it were proven."""
    a = {"id": 1, "name": "lib", "full_name": "acme/lib"}
    b = {"id": 2, "name": "app", "full_name": "acme/app"}
    monkeypatch.setattr(server, "_resolve_repo", lambda name: a if "lib" in name else b)

    impact = {"score": 0.8, "bump_count": 3, "is_declared": True,
              "median_adoption_days": 4.0, "rank_in_source": 1}
    monkeypatch.setattr(server.q, "impact_pair", lambda *args, **kwargs: impact)
    monkeypatch.setattr(server.q, "declared_dependency", lambda *args, **kwargs: {
        "dep_name": "lib", "dep_version": "1.2.3", "manifest": "pom.xml"})
    monkeypatch.setattr(server.q, "repo_pair_bumps", lambda *args, **kwargs: [
        {"consumer_sha": "a" * 40, "dep_version": "1.2.3", "dep_sha": "b" * 12,
         "bumped_at": None, "adoption_days": 4.0}])

    out = server.explain_repo_pair("acme/lib", "acme/app")
    assert out.get("error") is None
    assert out["repo_a"] == "acme/lib" and out["repo_b"] == "acme/app"
    assert "pom.xml" in out["declared_dependency"]
    # The forward edge carries its own evidence tier, so an agent can tell a
    # declaration from a coincidence without reading the score.
    assert out["forward"]["evidence"] == "declared"


# ------------------------------------------- every tool refuses an unknown repo

@pytest.mark.parametrize(("tool", "args"), [
    ("impact_of_change",   ("no-such-repo",)),
    ("coupling_chain",     ("no-such-repo",)),
    ("coupled_directories", ("no-such-repo", "pkg")),
    ("module_context",     ("no-such-repo", "pkg/a.go")),
    ("repo_hotspots",      ("no-such-repo",)),
])
def test_a_tool_names_the_repository_it_cannot_find(tool, args, monkeypatch):
    """Returning empty results would read as "nothing is coupled here", which
    is a claim about the code rather than about the name being wrong."""
    monkeypatch.setattr(server, "_resolve_repo", lambda name: None)
    out = getattr(server, tool)(*args)
    assert "no repository matching" in out["error"]
    assert "no-such-repo" in out["error"]


def test_file_history_names_a_path_it_cannot_find(monkeypatch):
    """It reports both failures as a missing *file*, because a path is resolved
    within a repository and an unknown repository cannot contain one. The
    message names both, so the caller can tell which was wrong."""
    monkeypatch.setattr(server, "_resolve_repo",
                        lambda name: {"id": 1, "name": "app", "full_name": "acme/app"})
    monkeypatch.setattr(server.q, "resolve_file", lambda repo, path: None)
    out = server.file_history("acme/app", "gone.go")
    assert "no file" in out["error"] and "gone.go" in out["error"]


# ------------------------------------------------------------ evidence tiers

@pytest.mark.parametrize(("row", "tier"), [
    ({"is_declared": True,  "has_bump_history": True},  "declared"),
    ({"is_declared": False, "has_bump_history": True},  "bump-backed"),
    # Unreachable by construction; it must still be legible rather than blank,
    # so a stale row from an older schema cannot pass as evidence.
    ({"is_declared": False, "has_bump_history": False}, "none"),
])
def test_an_impact_row_states_which_tier_it_came_from(row, tier):
    """An agent acts differently on a manifest line than on an observed bump, so
    the tier travels with every row rather than being inferred from the score."""
    row = {**row, "score": 0.5, "bump_count": 2, "median_adoption_days": None,
           "rank_in_source": 1}
    out = server._impact_row(row, "acme/app")
    assert out["evidence"] == tier
    if tier == "none":
        assert "no evidence" in out["note"]


def test_coupled_directories_names_a_directory_it_cannot_find(monkeypatch):
    """A file path is accepted as a convenience, so a genuine miss has to say
    it wanted a directory rather than silently returning nothing."""
    monkeypatch.setattr(server, "_resolve_repo",
                        lambda name: {"id": 1, "name": "app", "full_name": "acme/app"})
    monkeypatch.setattr(server.q, "directory_by_path", lambda *a, **k: None)
    monkeypatch.setattr(server.q, "resolve_file", lambda repo, path: None)
    out = server.coupled_directories("acme/app", "nowhere")
    assert "no directory" in out["error"]
    assert "hint" in out


def test_module_context_explains_both_directions_of_a_declaration(monkeypatch):
    """Being declared by a module and declaring one are different obligations,
    and an agent needs to be told which it is looking at."""
    monkeypatch.setattr(server, "_resolve_repo",
                        lambda name: {"id": 1, "name": "app", "full_name": "acme/app"})
    monkeypatch.setattr(server.q, "resolve_file",
                        lambda repo, path: {"id": 7, "path": "core/a.go"})
    monkeypatch.setattr(server.q, "module_context", lambda repo_id, path: {
        "owning_module": "core", "declared_by": ["web"], "declares": ["util"],
        "modules": ["core", "web", "util"]})

    out = server.module_context("acme/app", "core/a.go")
    assert "declared by" in out["guidance"] and "declares" in out["guidance"]


def test_coupled_directories_reports_only_partners_outside_the_subtree(monkeypatch):
    """Every change to `pkg/auth` is a change to `pkg` by construction, so a
    parent scores 1.000 and means nothing. The query now excludes ancestors and
    descendants, so what reaches the agent is only what could have moved
    independently and did not -- and the summary says so."""
    monkeypatch.setattr(server, "_resolve_repo",
                        lambda name: {"id": 1, "name": "app", "full_name": "acme/app"})
    monkeypatch.setattr(server.q, "directory_by_path", lambda *a, **k: {
        "id": 9, "path": "pkg/auth", "file_count": 12, "change_count": 300})
    monkeypatch.setattr(server.q, "coupled_directories", lambda *a, **k: [
        {"path": "web", "score": 0.40, "n_ab": 20, "confidence_ab": 0.4,
         "confidence_ba": 0.4, "change_count": 60},
    ])

    out = server.coupled_directories("acme/app", "pkg/auth")
    assert [d["path"] for d in out["partners"]] == ["web"]
    assert all(d["informative"] for d in out["partners"])
    assert "parents and children are excluded" in out["summary"]


# ------------------------------------------------------- recording tool calls

def test_a_tool_reply_is_read_from_the_text_blocks_when_there_is_no_schema():
    """structured_content is populated only for tools that declare an output
    schema. Everything else arrives as text, which is what the agent reads --
    logging a null there would record that nothing came back."""
    from git_synapse.mcp import server as srv

    class Block:
        def __init__(self, text):
            self.text = text

    class Result:
        structured_content = None
        content = [Block('{"repo": "acme/app", "upstream": [1, 2]}')]

    assert srv._text_of(Result()) == {"repo": "acme/app", "upstream": [1, 2]}

    class Plain(Result):
        content = [Block("just words")]

    assert srv._text_of(Plain()) == {"text": "just words"}

    class Empty(Result):
        content = []

    assert srv._text_of(Empty()) is None


def test_an_error_payload_is_summarised_for_the_log():
    from git_synapse.mcp import server as srv

    assert srv._error_text({"error": "no repository matching 'x'"}) \
        == "no repository matching 'x'"
    assert srv._error_text(["something else"]) == "['something else']"


def test_calling_a_tool_records_what_was_asked_and_what_came_back(monkeypatch):
    """One interception point covers every tool, including ones added later --
    the only way this stays true without anyone remembering."""
    import asyncio

    from git_synapse.analysis import calls
    from git_synapse.mcp import server as srv

    recorded = []
    monkeypatch.setattr(calls, "record", lambda *a, **k: recorded.append((a, k)))

    class Block:
        text = '{"ok": true}'

    class Result:
        structured_content = None
        is_error = False
        content = [Block()]

    async def fake_super(self, name, arguments, context=None):
        return Result()

    monkeypatch.setattr(srv.MCPServer, "call_tool", fake_super)
    out = asyncio.run(srv.server.call_tool("coupled_files", {"repo": "guava"}))
    assert out is not None
    (args, kwargs), = recorded
    assert args == ("mcp", "coupled_files")
    assert kwargs["arguments"] == {"repo": "guava"}
    assert kwargs["result"] == {"ok": True}
    assert kwargs["status"] == "ok"


def test_a_tool_that_raises_is_recorded_before_the_error_is_re_raised(monkeypatch):
    import asyncio

    from git_synapse.analysis import calls
    from git_synapse.mcp import server as srv

    recorded = []
    monkeypatch.setattr(calls, "record", lambda *a, **k: recorded.append(k))

    async def explode(self, name, arguments, context=None):
        raise RuntimeError("tool blew up")

    monkeypatch.setattr(srv.MCPServer, "call_tool", explode)
    with pytest.raises(RuntimeError, match="tool blew up"):
        asyncio.run(srv.server.call_tool("coupled_files", {}))
    assert recorded and recorded[0]["status"] == "error"
    assert "tool blew up" in recorded[0]["error"]


def test_a_tool_returning_an_error_object_is_counted_as_a_failure(monkeypatch):
    """It succeeded at the protocol level and failed at the only level a reader
    cares about; counting it as ok would make the error rate a fiction."""
    import asyncio

    from git_synapse.analysis import calls
    from git_synapse.mcp import server as srv

    recorded = []
    monkeypatch.setattr(calls, "record", lambda *a, **k: recorded.append(k))

    class Result:
        structured_content = {"error": "no repository matching 'nope'"}
        is_error = False
        content = []

    async def fake(self, name, arguments, context=None):
        return Result()

    monkeypatch.setattr(srv.MCPServer, "call_tool", fake)
    asyncio.run(srv.server.call_tool("upstream_repos", {"repo": "nope"}))
    assert recorded[0]["status"] == "error"
    assert recorded[0]["error"] == "no repository matching 'nope'"


def test_the_server_publishes_its_tool_inventory(db):
    """Written by the MCP process rather than read by the API, which would mean
    the API importing this module to answer a question about another container."""
    from git_synapse.analysis import calls
    from git_synapse.mcp import server as srv

    names = srv.tool_names()
    assert len(names) >= 10 and "coupled_files" in names

    srv._publish_tool_inventory()
    assert calls.known_mcp_tools() == names


def test_publishing_the_inventory_never_stops_the_server(monkeypatch, caplog):
    import logging

    from git_synapse.mcp import server as srv

    def boom(*_a, **_k):
        raise RuntimeError("database gone")

    monkeypatch.setattr("git_synapse.db.engine.set_watermark", boom)
    with caplog.at_level(logging.WARNING, logger="git_synapse.mcp.server"):
        srv._publish_tool_inventory()
    assert "could not publish the tool inventory" in caplog.text


def test_the_mcp_gate_lets_a_token_through_and_turns_others_away(monkeypatch):
    """The switch has to close a real door. Exercised through the ASGI app the
    server actually serves, not a stand-in: a gate that is only tested in the
    abstract is a gate nobody has opened."""
    import asyncio

    from git_synapse import auth

    app = server._guarded_app("127.0.0.1")

    # The middleware is the outermost layer; call it with a request it can read.
    async def call(headers, mode, token_user):
        monkeypatch.setattr(auth, "access_mode", lambda surface: mode)
        monkeypatch.setattr(auth, "token_user", lambda secret: token_user)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "path": "/mcp",
            "raw_path": b"/mcp", "query_string": b"", "root_path": "", "scheme": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8081), "app": app,
        }
        try:
            await app(scope, receive, send)
        except Exception:  # noqa: BLE001 - any failure here means "past the gate"
            # The real MCP app wants a session manager this test has not
            # started. Whatever it raises, reaching it is the evidence that the
            # request was let through rather than refused.
            return None
        started = [m["status"] for m in sent if m["type"] == "http.response.start"]
        return started[0] if started else None

    # Required, no token: turned away before the MCP app sees it.
    assert asyncio.run(call({"host": "127.0.0.1"}, "required", None)) == 401
    # Required, a token that resolves to nobody: same.
    assert asyncio.run(call({"host": "127.0.0.1", "authorization": "Bearer gss_nope"},
                            "required", None)) == 401
    # Open: the gate delegates, so no 401 is produced by it. What the MCP app
    # does next is the MCP app's business, not this gate's.
    assert asyncio.run(call({"host": "127.0.0.1"}, "open", None)) != 401

    # Required, with a token that resolves: delegated in the same way.
    assert asyncio.run(call({"host": "127.0.0.1", "authorization": "Bearer gss_ok"},
                            "required", {"id": 1, "role": "member"})) != 401


def test_serving_is_a_thin_call_that_can_be_stood_in_for(monkeypatch):
    """`_serve` exists so `main` can be tested without a server starting. It
    must stay thin enough that patching it loses nothing."""
    calls = {}
    monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                        type("U", (), {"run": staticmethod(
                            lambda app, **kw: calls.update(app=app, **kw))})())
    server._serve("an-app", "0.0.0.0", 8081)
    assert calls == {"app": "an-app", "host": "0.0.0.0", "port": 8081,
                     "log_level": "info"}
