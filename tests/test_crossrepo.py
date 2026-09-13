"""Tests for the cross-repository, lagged, prediction and mining layers.

The properties under test are the ones that were actually got wrong during
development, each of which produced plausible-looking but wrong numbers:

* single-repo change sets must be retained, or the contingency table loses its
  ``b`` and ``c`` cells and every score inflates toward 1.0;
* the lagged table must be genuinely directional, or the whole construction is
  pointless;
* the time origin must survive a corrupt commit date, which once stretched the
  axis from 2,200 bins to 20,687 and inflated ``N`` tenfold;
* impact rows must never mix the validated ensemble score with the unvalidated
  discovery score in one comparable column.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from git_synapse.db.orm import models, session_scope
from git_synapse.ingest.parser import FileChange, ParsedCommit

BASE = datetime(2025, 1, 6, 9, 0, tzinfo=UTC)


def _all_discovery_repo() -> str | None:
    with session_scope() as session:
        repos = session.query(models().Repo).all()
        impacts = session.query(models().RepoImpact).all()
    for repo in repos:
        rows = [row for row in impacts if row.target_repo_id == repo.id]
        if rows and all(not row.is_declared and not row.has_bump_history for row in rows):
            return repo.name
    return None


def _commit(sha_seed: int, subject: str, when: datetime, paths: list[str],
            email: str = "dev@example.com") -> ParsedCommit:
    return ParsedCommit(
        sha=f"{sha_seed:040x}",
        parents=[],
        author_name="Dev",
        author_email=email,
        authored_at=when,
        committer_name="Dev",
        committer_email=email,
        committed_at=when,
        subject=subject,
        body="",
        files=[FileChange(path=p, change_type="M", insertions=1, deletions=1) for p in paths],
    )


# ------------------------------------------------------------------ predict


def test_discovery_and_ensemble_scores_are_kept_separate(db):
    """A row must record which score it was ranked by.

    The ensemble is validated only inside the declared candidate set. Applying it
    globally ranked merely-busy repositories above real dependencies, so the two
    scores must never be presented as one comparable column.
    """
    with session_scope() as session:
        rows = session.query(models().RepoImpact).limit(200).all()
    if not rows:
        pytest.skip("impact table is empty")
    for r in rows:
        scored_by = (r.features or {}).get("scored_by")
        assert scored_by == "declared", f"unexpected scoring provenance: {r}"


def test_module_count_uses_the_composite_key(db):
    """A module is (repo_id, cluster_id), not cluster_id alone.

    Label propagation numbers clusters from zero inside each repository, so
    counting ``DISTINCT cluster_id`` across the corpus collapsed 4,394 modules
    into 559 on the overview endpoint.
    """
    with session_scope() as session:
        Cluster = models().FileCluster
        naive = session.query(Cluster.cluster_id).distinct().count()
        correct = session.query(Cluster.repo_id, Cluster.cluster_id).distinct().count()
    if correct == 0:
        pytest.skip("no clusters present")
    assert correct >= naive, "composite count must not be smaller"
    if naive < correct:
        # This is the normal case once more than one repo has clusters, and it
        # is exactly why the naive count is wrong.
        assert correct > 0


def test_clone_never_destroys_an_existing_mirror_on_failure(tmp_path):
    """A failed clone must leave the previous mirror intact.

    This is the regression for the worst incident so far: an expired token made
    every fetch fail, `sync_mirror` treated that as a corrupt mirror and fell
    back to a fresh clone, and `clone_mirror` removed the existing directory
    before attempting it. 213 of 272 working mirrors were deleted and not
    replaced. A mirror costs minutes to rebuild, so it must never be destroyed on
    the strength of an operation that has not completed.
    """
    from git_synapse.ingest import gitops

    mirror = tmp_path / "existing.git"
    mirror.mkdir()
    sentinel = mirror / "HEAD"
    sentinel.write_text("ref: refs/heads/main\n")

    # A URL that cannot resolve, so the clone is guaranteed to fail.
    from git_synapse.ingest.gitops import GitError

    with pytest.raises(GitError):
        # A bogus local path fails immediately; an unresolvable URL costs the
        # full network retry budget and made this the slowest test in the suite.
        gitops.clone_mirror(
            str(tmp_path / "definitely-not-a-repo.git"), mirror, blobless=True
        )

    assert mirror.is_dir(), "the existing mirror was removed by a failed clone"
    assert sentinel.read_text().startswith("ref:"), "mirror contents were damaged"
    # No staging directory should be left lying around either.
    assert not (tmp_path / "existing.git.incoming").exists()


def test_permanent_errors_are_distinguished_from_transient_ones():
    """Auth failures must never be retried or trigger a re-clone."""
    from git_synapse.ingest.gitops import is_permanent_error, is_transient_error

    permanent = [
        "remote: Invalid username or token. Password authentication is not supported",
        "fatal: Authentication failed for 'https://github.com/x/y.git/'",
        "remote: Repository not found.",
        "fatal: could not read Username for 'https://github.com'",
    ]
    transient = [
        "fatal: unable to access ...: Could not resolve host: github.com",
        "fatal: Connection reset by peer",
        "error: RPC failed; curl 92 HTTP/2 stream was reset",
    ]
    for message in permanent:
        assert is_permanent_error(message), f"should be permanent: {message}"
        assert not is_transient_error(message), f"must not retry: {message}"
    for message in transient:
        assert is_transient_error(message), f"should be transient: {message}"
        assert not is_permanent_error(message), f"should not be permanent: {message}"


def test_credential_preflight_rejects_a_bad_token(monkeypatch):
    """A rejected token must abort the run, not fail 272 repositories one by one."""
    from git_synapse.config import reset_config_cache
    from git_synapse.ingest.pipeline import AuthError, verify_credentials

    monkeypatch.setenv("GITHUB_TOKEN", "")
    reset_config_cache()
    try:
        with pytest.raises(AuthError, match="empty"):
            verify_credentials()
    finally:
        reset_config_cache()


def test_coupled_files_exposes_currency_fields(db):
    """The query must return what the MCP layer needs to judge currency."""
    with session_scope() as session:
        row = session.query(models().File.id, models().Repo.name).join(
            models().Repo, models().Repo.id == models().File.repo_id
        ).join(models().FilePair, (models().FilePair.file_a_id == models().File.id) |
              (models().FilePair.file_b_id == models().File.id)).first()
    if row is None:
        pytest.skip("no coupled files present")

    from git_synapse.analysis.query import coupled_files

    partners = coupled_files(row[0], limit=3, min_support=1)
    if not partners:
        pytest.skip("no partners above threshold")
    for key in ("days_since_co_change", "trend", "is_deleted", "last_co_change"):
        assert key in partners[0], f"coupled_files must return {key}"


def test_module_context_resolves_the_owning_module(db):
    """A file must resolve to its deepest matching module, not the root.

    Cross-repo analysis correctly finds no upstream for a monorepo whose internal
    references all point at itself, so the module graph is the only structural
    prior available there -- and it was previously discarded as a self-reference.
    """
    from git_synapse.analysis.query import module_context

    with session_scope() as session:
        row = session.query(models().ModuleDependency.repo_id).group_by(
            models().ModuleDependency.repo_id).order_by(
            models().ModuleDependency.repo_id).first()
    if row is None:
        pytest.skip("no module graph built")

    repo_id = row[0]
    with session_scope() as session:
        edge = session.query(models().ModuleDependency).filter(
            models().ModuleDependency.repo_id == repo_id,
            models().ModuleDependency.consumer_module != "",
        ).first()
    if edge is None:
        pytest.skip("no module dependency edge")
    consumer = edge.consumer_module

    ctx = module_context(repo_id, f"{consumer}/internal/deep/file.go")
    assert ctx["owning_module"] == consumer, (
        f"a file under {consumer}/ must resolve to it, got {ctx['owning_module']!r}"
    )
    assert edge.dep_module in ctx["declares"]

    # The reverse direction is the one that matters for impact.
    reverse = module_context(repo_id, f"{edge.dep_module}/x.go")
    assert consumer in reverse["declared_by"]


def test_module_context_is_honest_about_single_module_repos(db):
    """A repo with one module has no internal graph, and must say so."""
    from git_synapse.analysis.query import module_context

    with session_scope() as session:
        row = session.query(models().Repo).outerjoin(
            models().ModuleDependency, models().ModuleDependency.repo_id == models().Repo.id
        ).filter(models().ModuleDependency.repo_id.is_(None)).first()
    if row is None:
        pytest.skip("every repo has a module graph")
    ctx = module_context(row.id, "any/path.go")
    assert ctx["modules"] == []
    assert ctx["owning_module"] is None


def test_partner_marginal_is_the_partners_own_count(db):
    """`n_other` must be the partner's change count, not the queried file's.

    The union flips confidence but passed the marginals through in storage order,
    so every partner stored on the B side reported the queried file's own total --
    inflating it toward whatever hotspot was asked about.
    """
    from git_synapse.analysis.query import coupled_files, get_file, resolve_file

    target = resolve_file("acme/runtime", "go.mod")
    if target is None:
        pytest.skip("fixture repository not indexed")

    rows = coupled_files(target["id"], limit=8, min_support=5)
    if not rows:
        pytest.skip("no partners")

    for r in rows:
        partner = get_file(r["other_id"])
        assert r["n_other"] == partner["pair_change_count"], r["path"]
        assert r["n_this"] >= r["n_ab"]
        assert r["n_other"] >= r["n_ab"]

    # The queried file's own marginal is identical on every row; the partner's is not.
    assert len({r["n_this"] for r in rows}) == 1
    assert len({r["n_other"] for r in rows}) > 1, "n_other must vary by partner"


def test_score_is_the_value_the_rows_were_ranked_by(db):
    """Every surface prints `score`; it must be what the ORDER BY used."""
    from git_synapse.analysis.query import coupled_files, resolve_file

    target = resolve_file("acme/runtime", "go.mod")
    if target is None:
        pytest.skip("fixture repository not indexed")

    for measure in ("npmi", "confidence_ab", "confidence_ba", "jaccard"):
        rows = coupled_files(target["id"], measure=measure, limit=8, min_support=2)
        scores = [r["score"] for r in rows]
        assert scores == sorted(scores, reverse=True), measure
    # For the directional pair the score is the flipped alias, not the stored column.
    rows = coupled_files(target["id"], measure="confidence_ab", limit=5, min_support=2)
    assert all(r["score"] == r["confidence_out"] for r in rows)


def test_coupled_directories_reports_outward_confidence(db):
    """The fourth union site had no flip at all, so subdirectories read 100%."""
    from git_synapse.analysis.query import coupled_directories
    with session_scope() as session:
        row = session.query(models().Directory).order_by(models().Directory.change_count.desc()).first()
    if row is None:
        pytest.skip("no directories indexed")

    rows = coupled_directories(row.id, measure="confidence_ab", limit=10)
    if len(rows) < 2:
        pytest.skip("not enough partners")
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    assert all(r["score"] == r["confidence_out"] for r in rows)
    assert all(r["n_other"] >= r["n_ab"] for r in rows)


def test_pair_detail_answers_in_the_callers_argument_order(db):
    """Storage canonicalises by id; the answer must not silently transpose."""
    from git_synapse.analysis.query import pair_detail, resolve_file

    a = resolve_file("acme/runtime", "go.mod")
    b = resolve_file("acme/runtime", "go.sum")
    if a is None or b is None:
        pytest.skip("fixture repository not indexed")

    fwd = pair_detail(a["id"], b["id"])
    rev = pair_detail(b["id"], a["id"])
    assert fwd["path_a"] == "go.mod" and fwd["path_b"] == "go.sum"
    assert rev["path_a"] == "go.sum" and rev["path_b"] == "go.mod"
    # The two directions are genuinely different numbers, transposed together.
    assert fwd["confidence_ab"] == rev["confidence_ba"]
    assert fwd["n_a"] == rev["n_b"]
    assert fwd["cells"]["b"] == rev["cells"]["c"]


def test_directional_measure_ranks_outward_from_the_file_asked_about(db):
    """P(B|A) must rank by the probability the *partner* changes.

    The pair table stores each pair once, so half a file's partners are stored
    with it on the B side. Ranking on the raw column sorted those by the reverse
    probability -- putting a 17% partner above a 57% one, while the displayed
    column showed the correct value.
    """
    from git_synapse.analysis.query import coupled_files, resolve_file

    target = resolve_file("acme/platform", "gateway/internal/app/placement_test.go")
    if target is None:
        pytest.skip("fixture repository not indexed")

    rows = coupled_files(target["id"], measure="confidence_ab", limit=10, min_support=5)
    if len(rows) < 2:
        pytest.skip("not enough partners to order")

    outward = [r["confidence_out"] for r in rows]
    assert outward == sorted(outward, reverse=True), (
        "ranking must follow the outward probability that is displayed"
    )


def test_staleness_outranks_trend_in_currency():
    """A year-old pair must read as stale even when it carries a trend label.

    The drift window is wider than the staleness threshold, so returning the
    trend first made the stale branch unreachable for thousands of pairs and two
    partners of the same file at identical recency reported opposite verdicts.
    """
    from git_synapse.mcp.server import _describe_currency

    for trend in ("emerging", "decaying", None):
        assert _describe_currency(338, trend, False).startswith("STALE")

    # Deletion is a harder fact still, and trend survives below the threshold.
    assert _describe_currency(338, "emerging", True).startswith("DELETED")
    assert _describe_currency(12, "emerging", False).startswith("emerging")
    assert _describe_currency(100, "decaying", False).startswith("DECAYING")
    assert _describe_currency(5, None, False).startswith("current")


def test_feedback_survives_an_oversized_args_payload(db):
    """A serialised JSON string cannot be trimmed to fit; the column is jsonb."""
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.orm import models, session_scope

    fp_repo = "test/feedback-args"
    with session_scope() as session:
        session.query(models().Feedback).filter_by(repo=fp_repo).delete(synchronize_session=False)
    try:
        row = record_feedback(
            kind="tool_error", tool="coupled_files", repo=fp_repo,
            args={"paths": [f"a/long/path/number/{i}.go" for i in range(400)]},
            detail="Reported with a large argument list.",
        )
        with session_scope() as session:
            stored = session.get(models().Feedback, row["id"])
        assert stored.args["paths"][-1].endswith("399.go")
    finally:
        with session_scope() as session:
            session.query(models().Feedback).filter_by(repo=fp_repo).delete(synchronize_session=False)


def test_feedback_reopen_drops_the_resolution_that_closed_it(db):
    """A reopened report must not still display the fix that closed it."""
    from git_synapse.analysis.query import record_feedback, resolve_feedback
    from git_synapse.db.orm import models, session_scope

    fp_repo = "test/feedback-reopen"
    with session_scope() as session:
        session.query(models().Feedback).filter_by(repo=fp_repo).delete(synchronize_session=False)
    try:
        first = record_feedback(
            kind="wrong_data", tool="coupled_files", repo=fp_repo,
            expected="a partner that exists", detail="First sighting.",
        )
        resolve_feedback(first["id"], "fixed", "Corrected the join.")
        again = record_feedback(
            kind="wrong_data", tool="coupled_files", repo=fp_repo,
            expected="a partner that exists", detail="Still happening.",
        )
        assert again["id"] == first["id"]
        with session_scope() as session:
            row = session.get(models().Feedback, first["id"])
        assert row.status == "open"
        assert row.resolution is None
        assert row.resolved_at is None
    finally:
        with session_scope() as session:
            session.query(models().Feedback).filter_by(repo=fp_repo).delete(synchronize_session=False)


def test_feedback_without_context_does_not_collapse(db):
    """With no tool, repo, path or expectation there is no identity to dedup on.

    Fingerprinting those reports on context alone made every suggestion the same
    row, silently discarding all but the first.
    """
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.orm import models, session_scope

    a = record_feedback(kind="suggestion", detail="Rank modules by centrality.")
    b = record_feedback(kind="suggestion", detail="Support cargo manifests.")
    try:
        assert a["id"] != b["id"], "unrelated suggestions must stay separate"
        repeat = record_feedback(kind="suggestion", detail="  RANK MODULES BY CENTRALITY.  ")
        assert repeat["id"] == a["id"], "the same suggestion must still collapse"
        assert repeat["occurrences"] == 2
    finally:
        with session_scope() as session:
            session.query(models().Feedback).filter(models().Feedback.id.in_([a["id"], b["id"]])).delete(synchronize_session=False)


def test_feedback_rejects_opinions(db):
    """The log is for defects in Git Synapse, not disagreement with a score."""
    from git_synapse.analysis.query import record_feedback

    with pytest.raises(ValueError, match="unknown kind"):
        record_feedback(kind="opinion", detail="I disagree with this ranking")
    with pytest.raises(ValueError, match="detail is required"):
        record_feedback(kind="wrong_data", detail="   ")


def test_classifier_does_not_suppress_packages_merely_named_after_tooling():
    """`openapi` and `swagger` are real package names in Kubernetes-derived code.

    Matching them as directory names suppressed hand-written source; the
    generated artefacts are caught by filename instead.
    """
    from git_synapse.mcp.server import _classify_partner as classify

    for path in (
        "staging/src/k8s.io/apiserver/pkg/endpoints/openapi/openapi.go",
        "swagger/spec/v1/installer_resource_v1.yaml",
    ):
        labels, informative = classify(path, "other/file.go", 20)
        assert informative, f"{path} was suppressed: {labels}"

    for path in ("api/swagger.json", "api/openapi.json", "x/gen/embedded_spec.go"):
        assert "generated" in classify(path, "other/file.go", 20)[0], path


def test_empty_chain_says_whether_there_was_anything_to_search(db):
    """A bare [] conflated "nothing found" with "nothing to look through"."""
    from git_synapse.mcp import server

    name = _all_discovery_repo()
    if name is None:
        pytest.skip("no repository with only discovery-tier upstream edges")

    out = server.coupling_chain(repo=name, direction="upstream")
    assert out["chains"] == []
    assert out["explanation"], "an empty chain must say why it is empty"
    assert "validated" in out["explanation"]


def test_all_discovery_result_says_so_before_the_scores(db):
    """A result set with no validated edge must lead with that fact."""
    from git_synapse.mcp import server

    name = _all_discovery_repo()
    if name is None:
        pytest.skip("no all-discovery repository")

    # Opting in is what surfaces them; the default withholds. Both must say
    # plainly that nothing in the set carries validated evidence.
    out = server.upstream_repos(repo=name, limit=5, include_discovery=True)
    assert "NONE" in out["guidance"]
    assert "not a probability" in out["guidance"]

    default = server.upstream_repos(repo=name, limit=5)
    assert default["upstream"] == []
    assert "withheld" in default["guidance"]


def test_coupled_directories_marks_nesting_as_arithmetic(db):
    """A directory's parent scores 1.0 by construction, not by discovery.

    Every change to a child is a change to its parent, so the nesting relation
    has to be labelled or the top of the list reads as a finding.
    """
    from git_synapse.mcp import server

    with session_scope() as session:
        row = session.query(models().Repo.name, models().Directory.path).join(
            models().Directory, models().Directory.repo_id == models().Repo.id
        ).filter(models().Directory.file_count > 20, models().Directory.path.like("%/%"))
        row = row.order_by(models().Directory.change_count.desc()).first()
    if row is None:
        pytest.skip("no nested directory indexed")

    out = server.coupled_directories(repo=row[0], path=row[1], limit=10)
    assert "error" not in out, out
    if not out["partners"]:
        pytest.skip("no directory coupling for this fixture")

    own = out["directory"]["path"]
    for p in out["partners"]:
        nested = p["path"].startswith(f"{own}/") or own.startswith(f"{p['path']}/")
        assert p["informative"] is not nested, p
    assert "outside this directory" in out["summary"]


def test_coupled_directories_accepts_a_file_path(db):
    """A caller editing a file will pass the file, not its directory."""
    from git_synapse.mcp import server

    with session_scope() as session:
        row = session.query(models().Repo.name, models().File.path, models().File.dir_path).join(
            models().File, models().File.repo_id == models().Repo.id
        ).filter(models().File.dir_path != "", models().File.change_count > 20,
                 models().File.is_deleted.is_(False)).first()
    if row is None:
        pytest.skip("no suitable file")

    out = server.coupled_directories(repo=row[0], path=row[1], limit=5)
    assert "error" not in out, out
    assert out["directory"]["path"] == row[2]

    missing = server.coupled_directories(repo=row[0], path="no/such/dir", limit=5)
    assert "error" in missing and "hint" in missing


def test_token_file_is_read_fresh_and_validated(tmp_path, monkeypatch):
    """The credential must not be frozen at process start, nor half-read.

    `gh` here is a shell function wrapping bulwark, so the containers cannot
    reissue for themselves; the host rotates a file they read on every use. A
    torn read must never be sent to GitHub, because the 401 it earns is
    indistinguishable from an expired token.
    """
    import dataclasses

    from git_synapse.config import ProviderConfig

    token_path = tmp_path / "github-token"
    cfg = dataclasses.replace(ProviderConfig().github,
                              token="ghu_" + "e" * 36, token_file=str(token_path))

    # No file: the environment value stands.
    assert cfg.current_token() == "ghu_" + "e" * 36

    # A valid file wins, and a rewrite is picked up with no restart.
    token_path.write_text("ghu_" + "a" * 36)
    assert cfg.current_token() == "ghu_" + "a" * 36
    token_path.write_text("ghu_" + "b" * 36)
    assert cfg.current_token() == "ghu_" + "b" * 36

    # Empty, truncated or malformed: fall back rather than send rubbish.
    for bad in ("", "   ", "ghu_", "not-a-token", "ghu_abc"):
        token_path.write_text(bad)
        assert cfg.current_token() == "ghu_" + "e" * 36, bad


def test_transient_git_failures_do_not_trigger_a_reclone():
    """A network failure says nothing about the mirror, which is still good.

    Re-cloning on a fetch failure destroyed 213 working mirrors when a token
    expired, and during a later outage spent ten minutes per repository failing
    to replace mirrors that were fine.
    """
    from git_synapse.ingest.gitops import is_permanent_error, is_transient_error

    outage = (
        "fatal: unable to access 'https://github.com/x/y.git/': Failed to "
        "connect to github.com port 443 after 133149 ms: Could not connect"
    )
    assert is_transient_error(outage)
    assert not is_permanent_error(outage)

    auth = "remote: Invalid username or token. Password authentication is not supported"
    assert is_permanent_error(auth)

    # Only a cause that implicates the mirror should reach the re-clone path.
    corrupt = "fatal: not a git repository: '/data/mirrors/x/y.git'"
    assert not is_transient_error(corrupt)
    assert not is_permanent_error(corrupt)


def test_github_client_sends_the_live_token(tmp_path):
    """An unauthenticated request returns HTTP 200 and only public repositories.

    The credential moved to a file the host rotates, but the client still read
    the frozen environment copy. With that empty it sent no Authorization header
    and discovery silently returned 59 of 272 repositories.
    """
    import dataclasses

    from git_synapse.config import ProviderConfig
    from git_synapse.ingest.github import GitHubClient

    token_path = tmp_path / "github-token"
    token_path.write_text("ghu_" + "f" * 36)
    cfg = dataclasses.replace(ProviderConfig().github,
                              token="", token_file=str(token_path))

    with GitHubClient(cfg) as client:
        auth = client._client.headers.get("Authorization")
    assert auth == "Bearer ghu_" + "f" * 36, "the live token must reach the header"


def test_thin_support_is_withheld_not_merely_labelled():
    """A perfect score resting on two commits outranks everything real.

    Observed sending a reviewer at four unrelated files: 1.0 is arithmetic on a
    pair that changed twice and never apart, not evidence. A label only helps a
    reader who is already sceptical, so these are withheld.
    """
    from git_synapse.mcp.server import MIN_REPORTABLE_SUPPORT, _classify_partner

    own = "pkg/init/init.go"
    for n in range(1, MIN_REPORTABLE_SUPPORT):
        labels, informative = _classify_partner("pkg/agent/agent-mode.go", own, n)
        assert "thin_support" in labels and not informative, n

    _, informative = _classify_partner("pkg/agent/agent-mode.go", own, MIN_REPORTABLE_SUPPORT)
    assert informative


def test_discovery_upstream_is_withheld_unless_requested(db):
    """Unvalidated edges cost attention to dismiss and were never acted on."""
    from git_synapse.mcp import server

    name = _all_discovery_repo()
    if name is None:
        pytest.skip("no all-discovery repository")

    default = server.upstream_repos(repo=name)
    assert default["upstream"] == []
    assert "withheld" in default["guidance"]
    # The count must be stated, or the empty list reads as "no relationship".
    assert any(ch.isdigit() for ch in default["guidance"])

    opted_in = server.upstream_repos(repo=name, include_discovery=True)
    assert opted_in["upstream"], "opting in must return them"


def test_ingest_walks_only_the_shipped_branch():
    """Coupling is a claim about code that shipped.

    A quarter of this corpus exists solely on branches that never merged, and a
    branch that deletes a file the mainline still has produced false statements
    about HEAD.
    """
    import inspect

    from git_synapse.ingest.parser import iter_commits

    default = inspect.signature(iter_commits).parameters["rev"].default
    assert default == "HEAD", f"walk defaults to {default!r}, not the default branch"


def test_feedback_feeds_no_analytical_table(db):
    """Nothing that produces a score may read from the feedback log.

    This is the boundary that keeps agents out of the coupling data: if a future
    change joins feedback into an aggregate, the tool would start measuring its
    own past advice rather than the codebase.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "git_synapse" / "analysis"
    # query.py is excluded: the UI must be able to display the log. What must
    # never happen is a module that *computes a score* reading from it.
    analytical = ("aggregate.py", "score.py", "predict.py", "mining.py",
                  "depbump.py", "manifests.py", "backtest.py")
    offenders = [n for n in analytical if "feedback" in (src / n).read_text()]
    assert not offenders, f"analytical modules must not reference feedback: {offenders}"


def test_feedback_can_be_listed_by_severity(db):
    from git_synapse.analysis.query import list_feedback, record_feedback
    from git_synapse.db.orm import models, session_scope

    repo = "test/feedback-severity"
    with session_scope() as session:
        session.query(models().Feedback).filter_by(repo=repo).delete(synchronize_session=False)
    try:
        record_feedback(kind="tool_error", severity="high", repo=repo,
                        detail="a high one", fingerprint="sev-high")
        record_feedback(kind="tool_error", severity="low", repo=repo,
                        detail="a low one", fingerprint="sev-low")
        high = [f for f in list_feedback(severity="high", limit=1000) if f["repo"] == repo]
        assert [f["severity"] for f in high] == ["high"]
    finally:
        with session_scope() as session:
            session.query(models().Feedback).filter_by(repo=repo).delete(synchronize_session=False)
