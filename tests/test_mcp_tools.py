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
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name, full_name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    for form in (row["name"], row["full_name"], row["name"].upper()):
        out = server.upstream_repos(repo=form)
        assert "error" not in out, f"{form!r} failed to resolve"
        assert out["repo"] == row["full_name"]


# ---------------------------------------------------------- coupled_files

def test_coupled_files_rejects_an_unknown_path_with_a_hint(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    out = server.coupled_files(repo=row["name"], path="no/such/file.go")
    assert "error" in out and "hint" in out


def test_coupled_files_shape_is_stable(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE f.change_count > 30 AND NOT f.is_deleted LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no busy file")
    out = server.coupled_files(repo=row["repo"], path=row["path"], min_support=3, limit=5)
    assert "error" not in out, out
    assert out["file"]["path"] == row["path"]
    for p in out["partners"]:
        for key in ("path", "score", "co_changes", "partner_total_changes",
                    "probability_also_changes", "informative", "currency"):
            assert key in p, f"{key} missing from a partner row"
        assert p["partner_total_changes"] >= p["co_changes"]
        assert 0.0 <= p["probability_also_changes"] <= 1.0


def test_an_unknown_measure_is_refused(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        "SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id=f.repo_id LIMIT 1"
    )
    out = server.coupled_files(repo=row["repo"], path=row["path"], measure="nope")
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

def test_module_context_rejects_a_path_that_does_not_exist(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    out = server.module_context(repo=row["name"], path="not/real.go")
    assert "error" in out


def test_coupled_directories_accepts_a_file_or_a_directory(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path, f.dir_path
        FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE f.dir_path <> '' AND f.change_count > 20 LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no suitable file")
    by_file = server.coupled_directories(repo=row["repo"], path=row["path"], limit=5)
    by_dir = server.coupled_directories(repo=row["repo"], path=row["dir_path"], limit=5)
    assert by_file["directory"]["path"] == by_dir["directory"]["path"] == row["dir_path"]


# --------------------------------------------------------------- catalogue

def test_list_measures_describes_every_measure(db):
    out = server.list_measures()
    measures = out["measures"] if isinstance(out, dict) else out
    assert len(measures) >= 29
    assert all(m.get("key") and m.get("label") for m in measures)


def test_list_repositories_returns_usable_names(db):
    out = server.list_repositories(limit=5)
    repos = out["repositories"] if isinstance(out, dict) else out
    assert repos
    for r in repos:
        assert r.get("name")


def test_search_files_finds_a_known_path(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT basename FROM file WHERE basename = 'go.mod' LIMIT 1")
    if row is None:
        pytest.skip("no go.mod indexed")
    out = server.search_files(term="go.mod", limit=5)
    files = out["files"] if isinstance(out, dict) else out
    assert files


def test_report_gap_rejects_an_opinion_and_accepts_a_defect(db):
    from git_synapse.db.engine import connection

    bad = server.report_gap(kind="opinion", detail="I disagree with the ranking")
    assert "error" in bad

    good = server.report_gap(
        kind="wrong_data", detail="PROBE mcp tool test", severity="low",
        tool="coupled_files", repo="t/probe", expected="x", observed="y",
    )
    assert "error" not in good and good.get("id")
    with connection() as conn:
        conn.execute("DELETE FROM feedback WHERE id=%s", (good["id"],))


# ------------------------------------------------------- the explain tools

def test_explain_pair_answers_in_the_callers_argument_order(db):
    """Storage canonicalises by id; returning that order silently transposed
    the answer, so confidence_ab was the reverse conditional half the time."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, fa.path AS a, fb.path AS b
        FROM file_pair_metric m
        JOIN file fa ON fa.id = m.file_a_id
        JOIN file fb ON fb.id = m.file_b_id
        JOIN repo r ON r.id = m.repo_id
        WHERE m.n_ab > 5 LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no supported pair")

    fwd = server.explain_pair(repo=row["repo"], path_a=row["a"], path_b=row["b"])
    rev = server.explain_pair(repo=row["repo"], path_a=row["b"], path_b=row["a"])
    assert fwd["path_a"] == row["a"] and rev["path_a"] == row["b"]
    assert fwd["measures"]["confidence_ab"] == rev["measures"]["confidence_ba"]


def test_explain_pair_rejects_an_unknown_path(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        "SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id=f.repo_id LIMIT 1"
    )
    out = server.explain_pair(repo=row["repo"], path_a=row["path"], path_b="no/such.go")
    assert "error" in out


def test_explain_repo_pair_does_not_claim_a_dependency_that_is_not_declared(db):
    """The prose field is what a model quotes; it promoted a bump-backed pair to
    `declared` while the structured field beside it said null."""
    from git_synapse.db.engine import query

    rows = query(
        """
        SELECT p.name AS a, c.name AS b
        FROM repo_impact i
        JOIN repo p ON p.id = i.source_repo_id
        JOIN repo c ON c.id = i.target_repo_id
        WHERE i.has_bump_history AND NOT i.is_declared LIMIT 3
        """
    )
    if not rows:
        pytest.skip("no bump-backed-only pair")
    for r in rows:
        out = server.explain_repo_pair(repo_a=r["a"], repo_b=r["b"])
        if "error" in out:
            continue
        if out.get("declared_dependency") is None:
            prose = out.get("interpretation") or ""
            # It may mention declaration to deny it; what it must not do is
            # assert one, which reads as the top evidence tier.
            assert f"declares {r['a']}" not in prose, prose
            assert "bump-backed rather than declared" in prose or "no declared" in prose, prose


def test_impact_of_change_and_upstream_agree(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT p.name AS src, c.name AS tgt FROM repo_impact i
        JOIN repo p ON p.id = i.source_repo_id
        JOIN repo c ON c.id = i.target_repo_id
        WHERE i.is_declared OR i.has_bump_history LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no validated edge")

    down = server.impact_of_change(repo=row["src"])
    up = server.upstream_repos(repo=row["tgt"])
    assert any(x["repo"].endswith(row["tgt"]) for x in down.get("downstream", []))
    assert any(x["repo"].endswith(row["src"]) for x in up.get("upstream", []))


def test_crossrepo_files_reports_a_specific_partner_file(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT ra.name AS repo, fa.path
        FROM xrepo_file_pair x
        JOIN file fa ON fa.id = x.file_a_id
        JOIN repo ra ON ra.id = x.repo_a_id
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no cross-repo pairs")
    out = server.crossrepo_files(repo=row["repo"], path=row["path"], min_support=2)
    assert "error" not in out, out


def test_file_history_returns_commits_for_a_real_file(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id=f.repo_id
        WHERE f.change_count > 5 LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no busy file")
    out = server.file_history(repo=row["repo"], path=row["path"], limit=5)
    assert "error" not in out
    assert out["recent_commits"]
    assert out["total_changes"] >= len(out["recent_commits"])


def test_repo_hotspots_are_ranked(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled ORDER BY commit_count DESC LIMIT 1")
    out = server.repo_hotspots(repo=row["name"], limit=5)
    rows = out.get("hotspots", [])
    counts = [h["changes"] for h in rows]
    assert counts == sorted(counts, reverse=True)


def test_coupling_chain_direction_is_honoured(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    up = server.coupling_chain(repo=row["name"], direction="upstream")
    down = server.coupling_chain(repo=row["name"], direction="downstream")
    assert up["direction"] == "upstream"
    assert down["direction"] == "downstream"


def test_an_empty_chain_explains_itself(db):
    """A bare [] conflated "searched and found nothing" with "nothing to search"."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name FROM repo r
        WHERE EXISTS (SELECT 1 FROM repo_impact i WHERE i.target_repo_id=r.id)
          AND NOT EXISTS (SELECT 1 FROM repo_impact i
                          WHERE i.target_repo_id=r.id
                            AND (i.is_declared OR i.has_bump_history))
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no all-discovery repository")
    out = server.coupling_chain(repo=row["name"], direction="upstream")
    assert out["chains"] == [] and out["explanation"]


# ------------------------------------------------------ module_context prose

def test_module_context_describes_a_multi_module_repository(db):
    """In a monorepo the module graph is the structure; the guidance has to say
    which direction a change propagates."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path
        FROM module_dependency m
        JOIN repo r ON r.id = m.repo_id
        JOIN file f ON f.repo_id = r.id AND f.dir_path LIKE m.consumer_module || '%'
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no multi-module repository indexed")
    out = server.module_context(repo=row["repo"], path=row["path"])
    assert "error" not in out, out
    assert out["multi_module"] is True
    assert out.get("guidance")


def test_module_context_says_so_for_a_single_module_repository(db):
    """"There is no internal module graph" is a real answer, not an empty one."""
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, f.path
        FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE NOT EXISTS (SELECT 1 FROM module_dependency m WHERE m.repo_id = r.id)
          AND f.change_count > 5
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("every repository is multi-module")
    out = server.module_context(repo=row["repo"], path=row["path"])
    assert out.get("multi_module") is False
    assert "single-module" in (out.get("note") or "")


def test_search_files_scoped_to_a_repository_stays_in_it(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled ORDER BY commit_count DESC LIMIT 1")
    out = server.search_files(term="go", repo=row["name"], limit=5)
    files = out["files"] if isinstance(out, dict) else out
    for f in files:
        assert f.get("repo", "").endswith(row["name"])


def test_search_files_with_no_match_is_an_empty_list_not_an_error(db):
    out = server.search_files(term="zzz-definitely-no-such-path-zzz", limit=5)
    files = out["files"] if isinstance(out, dict) else out
    assert files == []


def test_list_repositories_can_be_filtered(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    out = server.list_repositories(search=row["name"], limit=10)
    repos = out["repositories"] if isinstance(out, dict) else out
    # The tool reports full names, which is what an agent should pass back.
    assert any(r["name"].endswith(f'/{row["name"]}') or r["name"] == row["name"]
               for r in repos), [r["name"] for r in repos][:5]


# ---------------------------------------------- the evidence prose, exhaustively

@pytest.mark.parametrize(
    ("declared", "bumps", "must_say", "must_not_say"),
    [
        ("declares x in go.mod", 3, "declares", None),
        ("declares x in go.mod", 0, "no bump has been observed", None),
        (None, 3, "bump-backed rather than declared", "declares dsx-lib and"),
        (None, 0, "no declared dependency", "declares"),
    ],
)
def test_repo_pair_prose_never_outruns_its_evidence(declared, bumps, must_say, must_not_say):
    """The prose is what a model quotes, and `declared` is the tier agents are
    told to trust above all others. Every combination must say only what the
    structured fields beside it support."""
    out = server._describe_repo_pair(
        {"name": "dsx-lib"}, {"name": "dsx-app"}, declared,
        {"bump_count": bumps} if bumps else None,
    )
    assert must_say in out, out
    if must_not_say:
        assert must_not_say not in out, out


@pytest.mark.parametrize(
    ("validated", "withheld", "must_say"),
    [
        (0, 0, "No upstream edges recorded"),
        (0, 5, "Nothing validated upstream"),
        (2, 0, "declared"),
        (2, 3, "withheld"),
    ],
)
def test_upstream_guidance_covers_every_composition(validated, withheld, must_say):
    """The guidance is read before the scores; it must be right in all four
    shapes, including the one where nothing is validated at all."""
    rows = [{"is_declared": True, "has_bump_history": False} for _ in range(validated)]
    extra = [{"is_declared": False, "has_bump_history": False} for _ in range(withheld)]
    out = server._upstream_guidance(rows, withheld, False, rows + extra)
    assert must_say in out, out


def test_upstream_guidance_when_discovery_is_requested_describes_everything():
    rows = [{"is_declared": False, "has_bump_history": False} for _ in range(4)]
    out = server._upstream_guidance([], 4, True, rows)
    assert "NONE" in out and "not a probability" in out


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


@pytest.mark.parametrize("tool_name", REPO_TOOLS)
def test_every_repo_taking_tool_refuses_an_unknown_repository(db, tool_name):
    """One tool answering confidently about the wrong repository is worse than
    ten refusing, so the guard has to be on all of them."""
    tool = getattr(server, tool_name)
    kwargs = {"repo": "definitely-not-a-repository-xyz"}
    if tool_name in ("coupled_files", "crossrepo_files", "file_history",
                     "module_context", "coupled_directories"):
        kwargs["path"] = "some/file.go"
    if tool_name == "explain_pair":
        kwargs["path_a"], kwargs["path_b"] = "a.go", "b.go"
    out = tool(**kwargs)
    assert "error" in out, f"{tool_name} answered for a repository that does not exist"


@pytest.mark.parametrize("tool_name", ["coupled_files", "crossrepo_files",
                                       "file_history", "coupled_directories"])
def test_every_path_taking_tool_refuses_an_unknown_path(db, tool_name):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    out = getattr(server, tool_name)(repo=row["name"], path="no/such/path.xyz")
    assert "error" in out, f"{tool_name} answered for a path that does not exist"


def test_explain_repo_pair_refuses_an_unknown_side(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    assert "error" in server.explain_repo_pair(repo_a="nope-xyz", repo_b=row["name"])
    assert "error" in server.explain_repo_pair(repo_a=row["name"], repo_b="nope-xyz")


def test_a_directory_with_no_coupling_says_so_rather_than_returning_nothing(db):
    from git_synapse.db.engine import query_one

    row = query_one(
        """
        SELECT r.name AS repo, d.path FROM directory d JOIN repo r ON r.id = d.repo_id
        WHERE d.change_count <= 1 LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no quiet directory")
    out = server.coupled_directories(repo=row["repo"], path=row["path"])
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
    from git_synapse.db.engine import query_one

    row = query_one(
        "SELECT r.name AS repo, f.path FROM file f JOIN repo r ON r.id = f.repo_id"
        " WHERE f.path LIKE '%.go' LIMIT 1"
    )
    if row is None:
        pytest.skip("no files")
    monkeypatch.setattr(server.q, "resolve_file", lambda *a, **k: {
        "id": 1, "repo": row["repo"], "path": "a/handler.go", "repo_id": 1,
        "change_count": 50, "author_count": 3, "last_change_at": None,
        "is_deleted": False, "pair_population": 120,
    })
    return server.coupled_files(repo=row["repo"], path=row["path"])


def test_a_mock_file_is_labelled_generated_not_coupled_behaviour():
    """A mock changes with the interface it mocks by construction."""
    labels, informative = server._classify_partner("pkg/mock_client.go",
                                                   "pkg/client.go", 40)
    assert "generated" in labels


def test_two_files_that_never_changed_together_say_so_rather_than_erroring(db,
                                                                          monkeypatch):
    """`coupled: false` with a reason is actionable; an error is not."""
    from git_synapse.db.engine import query_one

    row = query_one("SELECT r.name AS repo FROM repo r WHERE r.is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    monkeypatch.setattr(server.q, "resolve_file", lambda repo, path: {
        "id": 1 if path == "a.go" else 2, "repo": repo, "path": path,
        "repo_id": 1, "change_count": 5, "author_count": 1,
        "last_change_at": None, "is_deleted": False,
    })
    monkeypatch.setattr(server.q, "pair_detail", lambda *a, **k: None)
    out = server.explain_pair(repo=row["repo"], path_a="a.go", path_b="b.go")
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
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    monkeypatch.setattr(server.predict, "impact_chains", lambda *a, **k: [])
    monkeypatch.setattr(server.q, "query_one", lambda *a, **k: counts)
    out = server.coupling_chain(repo=row["name"], direction="downstream")
    assert must_contain.lower() in out["explanation"].lower()


def test_an_entirely_unvalidated_shortlist_says_so_before_anything_else(db):
    """An agent that reads a discovery-tier rank as a probability acts on it."""
    rows = [{"is_declared": False, "has_bump_history": False} for _ in range(5)]
    note = server._evidence_guidance(rows, "upstream")
    assert note.startswith("NONE")


def test_an_empty_shortlist_is_described_not_left_blank():
    assert "No upstream edges recorded" in server._evidence_guidance([], "upstream")


def test_coupled_directories_refuses_a_measure_it_does_not_have(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT r.name AS repo, f.path FROM file f"
                    " JOIN repo r ON r.id = f.repo_id WHERE f.path LIKE '%/%' LIMIT 1")
    if row is None:
        pytest.skip("no files")
    out = server.coupled_directories(repo=row["repo"], path=row["path"],
                                     measure="not_a_measure")
    assert "error" in out


def test_module_context_calls_a_leaf_a_leaf(db, monkeypatch):
    """Silence and "nothing depends on this" are different answers."""
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
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
    out = server.module_context(repo=row["name"], path="gateway/main.go")
    assert "leaf" in out["guidance"]


def test_search_files_refuses_a_repository_it_cannot_resolve(db):
    out = server.search_files(term="handler", repo="definitely-not-a-repo")
    assert "error" in out


def test_search_files_scopes_to_a_repository_when_one_resolves(db):
    from git_synapse.db.engine import query_one

    row = query_one("SELECT name FROM repo WHERE is_enabled LIMIT 1")
    if row is None:
        pytest.skip("no repositories")
    out = server.search_files(term="a", repo=row["name"], limit=3)
    assert "error" not in out


@pytest.mark.parametrize(("argv", "expected"), [
    ([], {"transport": "stdio"}),
    (["--transport", "sse", "--port", "9999"], {"transport": "sse", "port": 9999}),
    (["--transport", "http", "--host", "0.0.0.0"], {"transport": "streamable-http"}),
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
    assert server.main(argv) == 0
    for key, value in expected.items():
        assert started[key] == value
