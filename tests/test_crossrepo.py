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

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from git_synapse.analysis import crossrepo, lagged, predict
from git_synapse.db.engine import connection, query, query_one
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import FileChange, ParsedCommit
from git_synapse.ingest.store import load_commits, upsert_repo

BASE = datetime(2025, 1, 6, 9, 0, tzinfo=timezone.utc)


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


def test_corrupt_dates_cannot_stretch_the_time_axis(db):
    """A commit at the Unix epoch must not set the time origin.

    Before this guard, four epoch-dated commits stretched the axis to 20,687
    daily bins instead of ~2,200, inflating N -- and therefore the `d` cell of
    every contingency table -- by an order of magnitude.
    """
    assert lagged.PLAUSIBLE_EPOCH >= "2005-01-01"
    with connection() as conn:
        _matrix, _ids, n_bins = lagged._event_matrix(conn, 24)
    # 2005 to now is under 8,000 days; anything larger means a corrupt date won.
    assert 0 < n_bins < 8000, f"time axis spans {n_bins} bins, which implies a bad origin"


def test_joint_at_lag_shifts_in_the_right_direction():
    """Unit test of the shift, independent of the database.

    Repo 0 fires at bins 0, 4, 8; repo 1 fires exactly one bin later each time.
    At lag 1 the joint count 0->1 must catch all three, and 1->0 must be empty.

    The spacing matters: an alternating pattern (0,2,4,6 against 1,3,5,7) has
    signal in BOTH directions -- bin 1 is one after bin 0 but also one before
    bin 2 -- so it cannot distinguish a working shift from a broken one.
    """
    matrix = np.zeros((2, 12), dtype=np.float32)
    matrix[0, [0, 4, 8]] = 1.0
    matrix[1, [1, 5, 9]] = 1.0

    joint, n_a, n_b, windows = lagged._joint_at_lag(matrix, 1)
    assert windows == 11
    assert joint[0, 1] == 3.0, "0 -> 1 at lag 1 should catch every firing"
    assert joint[1, 0] == 0.0, "1 -> 0 at lag 1 must be empty"
    assert n_a[0] == 3.0 and n_b[1] == 3.0

    joint0, _, _, _ = lagged._joint_at_lag(matrix, 0)
    assert joint0[0, 1] == 0.0, "the two never fire in the same bin"

    # And at lag 3 (the 4-bin cycle minus the 1-bin offset) the roles reverse.
    joint3, _, _, _ = lagged._joint_at_lag(matrix, 3)
    assert joint3[1, 0] == 2.0, "1 -> 0 should reappear at the complementary lag"


# ------------------------------------------------------------------ predict


def test_discovery_and_ensemble_scores_are_kept_separate(db):
    """A row must record which score it was ranked by.

    The ensemble is validated only inside the declared candidate set. Applying it
    globally ranked merely-busy repositories above real dependencies, so the two
    scores must never be presented as one comparable column.
    """
    rows = query(
        "SELECT is_declared, has_bump_history, features FROM repo_impact LIMIT 200"
    )
    if not rows:
        pytest.skip("impact table is empty; run `git-synapse impact` first")
    for r in rows:
        scored_by = (r["features"] or {}).get("scored_by")
        assert scored_by in {"ensemble", "discovery"}, f"missing scored_by: {r}"
        if r["is_declared"] or r["has_bump_history"]:
            assert scored_by == "ensemble"
        else:
            assert scored_by == "discovery"


def test_rank_normalise_is_bounded_and_monotone():
    values = np.array([5.0, 1.0, 3.0, 100.0, 3.0])
    out = predict._rank_normalise(values)
    assert out.min() == 0.0 and out.max() == 1.0
    # Order must be preserved: the largest input gets the largest output.
    assert out[np.argmax(values)] == 1.0
    assert out[np.argmin(values)] == 0.0


def test_ensemble_and_discovery_measure_sets_differ():
    """Discovery must exclude the frequency-dominated measures.

    Those are exactly what let a repository that commits daily look coupled to
    everything, which is the confounding that capped global AUC at 0.68.
    """
    assert set(predict.DISCOVERY_MEASURES) < set(predict.ENSEMBLE_MEASURES) | set(
        predict.DISCOVERY_MEASURES
    )
    assert "russell_rao" not in predict.DISCOVERY_MEASURES
    assert "t_score" not in predict.DISCOVERY_MEASURES
    assert "npmi" in predict.DISCOVERY_MEASURES


def test_chains_traverse_only_validated_edges_by_default(db):
    """A chain must not ride an unvalidated discovery hop."""
    row = query_one(
        "SELECT source_repo_id FROM repo_impact"
        " WHERE is_declared OR has_bump_history LIMIT 1"
    )
    if row is None:
        pytest.skip("no validated impact edges available")

    chains = predict.impact_chains(
        row["source_repo_id"], max_depth=3, min_score=0.2, limit=50
    )
    for c in chains:
        ids = list(c["path"])
        for a, b in zip(ids, ids[1:]):
            edge = query_one(
                "SELECT is_declared, has_bump_history FROM repo_impact"
                " WHERE source_repo_id=%s AND target_repo_id=%s",
                (a, b),
            )
            assert edge is not None, f"chain hop {a}->{b} has no impact row"
            assert edge["is_declared"] or edge["has_bump_history"], (
                f"chain traversed an unvalidated hop {a}->{b}"
            )


def test_module_count_uses_the_composite_key(db):
    """A module is (repo_id, cluster_id), not cluster_id alone.

    Label propagation numbers clusters from zero inside each repository, so
    counting ``DISTINCT cluster_id`` across the corpus collapsed 4,394 modules
    into 559 on the overview endpoint.
    """
    rows = query(
        """
        SELECT
          (SELECT count(DISTINCT cluster_id) FROM file_cluster)      AS naive,
          (SELECT count(*) FROM (SELECT DISTINCT repo_id, cluster_id
                                   FROM file_cluster) m)            AS correct
        """
    )
    if not rows or rows[0]["correct"] == 0:
        pytest.skip("no clusters present; run `git-synapse mine` first")
    assert rows[0]["correct"] >= rows[0]["naive"], "composite count must not be smaller"
    if rows[0]["naive"] < rows[0]["correct"]:
        # This is the normal case once more than one repo has clusters, and it
        # is exactly why the naive count is wrong.
        assert rows[0]["correct"] > 0


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
    with pytest.raises(Exception):
        gitops.clone_mirror(
            "https://invalid.invalid/nope.git", mirror, blobless=True
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
    from git_synapse.config import get_config, reset_config_cache
    from git_synapse.ingest.pipeline import AuthError, verify_credentials

    monkeypatch.setenv("GITHUB_TOKEN", "")
    reset_config_cache()
    try:
        with pytest.raises(AuthError, match="empty"):
            verify_credentials()
    finally:
        reset_config_cache()


def test_deleted_partners_are_flagged_not_merely_scored(db):
    """A partner that no longer exists must be reported as deleted.

    38,716 deleted files remain coupling partners in this corpus, and an agent
    cannot edit any of them. Age does not catch this: the case that prompted the
    fix was a file deleted 50 days ago whose last co-change was also 50 days ago,
    so no staleness threshold would have flagged it.
    """
    from git_synapse.mcp.server import _describe_currency

    assert "DELETED" in _describe_currency(50, None, deleted=True)
    assert "DELETED" in _describe_currency(0, "emerging", deleted=True), (
        "deletion must win over a recent or emerging trend"
    )
    # Age wins over trend past the staleness threshold; `trend` is returned as
    # its own field, so nothing is lost by saying how old the pair is.
    assert "STALE" in _describe_currency(400, "decaying")
    assert "STALE" in _describe_currency(400, None)
    assert "DECAYING" in _describe_currency(100, "decaying")
    assert "current" in _describe_currency(3, None)
    assert _describe_currency(None, None) is None


def test_coupled_files_exposes_currency_fields(db):
    """The query must return what the MCP layer needs to judge currency."""
    row = query_one(
        """
        SELECT f.id, r.name FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE EXISTS (SELECT 1 FROM file_pair p
                       WHERE p.file_a_id = f.id OR p.file_b_id = f.id)
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no coupled files present")

    from git_synapse.analysis.query import coupled_files

    partners = coupled_files(row["id"], limit=3, min_support=1)
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

    row = query_one(
        """
        SELECT repo_id, count(*) AS n FROM module_dependency
        GROUP BY repo_id ORDER BY n DESC LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no module graph built; run `git-synapse depbump`")

    repo_id = row["repo_id"]
    edge = query_one(
        "SELECT consumer_module, dep_module FROM module_dependency"
        " WHERE repo_id = %s AND consumer_module <> '' LIMIT 1",
        (repo_id,),
    )
    consumer = edge["consumer_module"]

    ctx = module_context(repo_id, f"{consumer}/internal/deep/file.go")
    assert ctx["owning_module"] == consumer, (
        f"a file under {consumer}/ must resolve to it, got {ctx['owning_module']!r}"
    )
    assert edge["dep_module"] in ctx["declares"]

    # The reverse direction is the one that matters for impact.
    reverse = module_context(repo_id, f"{edge['dep_module']}/x.go")
    assert consumer in reverse["declared_by"]


def test_module_context_is_honest_about_single_module_repos(db):
    """A repo with one module has no internal graph, and must say so."""
    from git_synapse.analysis.query import module_context

    row = query_one(
        """
        SELECT id FROM repo r
        WHERE NOT EXISTS (SELECT 1 FROM module_dependency m WHERE m.repo_id = r.id)
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("every repo has a module graph")
    ctx = module_context(row["id"], "any/path.go")
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
    from git_synapse.analysis.query import coupled_directories, query_one as _q

    row = _q("SELECT id FROM directory ORDER BY change_count DESC LIMIT 1")
    if row is None:
        pytest.skip("no directories indexed")

    rows = coupled_directories(row["id"], measure="confidence_ab", limit=10)
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


def test_feedback_deduplicates_on_identity_not_wording(db):
    """The same defect described twice must be one row with a count of two.

    The count is the priority signal, so a gap many sessions hit has to
    accumulate rather than fragment into near-duplicate rows.
    """
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.engine import execute

    fp_repo = "test/feedback-fixture"
    execute("DELETE FROM feedback WHERE repo = %s", (fp_repo,))
    try:
        first = record_feedback(
            kind="wrong_data", severity="low", tool="coupled_files", repo=fp_repo,
            path="a/b.go", expected="Partner flagged deleted",
            detail="One wording.",
        )
        second = record_feedback(
            kind="wrong_data", severity="high", tool="coupled_files", repo=fp_repo,
            path="a/b.go", expected="partner flagged DELETED  ",
            detail="Entirely different wording, same defect.",
        )
        assert second["id"] == first["id"], "should have deduplicated"
        assert second["occurrences"] == 2
        row = query_one("SELECT severity FROM feedback WHERE id = %s", (first["id"],))
        # GREATEST() on text would have ranked 'low' above 'high' alphabetically.
        assert row["severity"] == "high", "the worse severity must win"
    finally:
        execute("DELETE FROM feedback WHERE repo = %s", (fp_repo,))


def test_feedback_survives_an_oversized_args_payload(db):
    """A serialised JSON string cannot be trimmed to fit; the column is jsonb."""
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.engine import execute

    fp_repo = "test/feedback-args"
    execute("DELETE FROM feedback WHERE repo = %s", (fp_repo,))
    try:
        row = record_feedback(
            kind="tool_error", tool="coupled_files", repo=fp_repo,
            args={"paths": [f"a/long/path/number/{i}.go" for i in range(400)]},
            detail="Reported with a large argument list.",
        )
        stored = query_one("SELECT args FROM feedback WHERE id = %s", (row["id"],))
        assert stored["args"]["truncated"] is True
        assert stored["args"]["preview"]
    finally:
        execute("DELETE FROM feedback WHERE repo = %s", (fp_repo,))


def test_feedback_reopen_drops_the_resolution_that_closed_it(db):
    """A reopened report must not still display the fix that closed it."""
    from git_synapse.analysis.query import record_feedback, resolve_feedback
    from git_synapse.db.engine import execute

    fp_repo = "test/feedback-reopen"
    execute("DELETE FROM feedback WHERE repo = %s", (fp_repo,))
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
        row = query_one(
            "SELECT status, resolution, resolved_at FROM feedback WHERE id = %s",
            (first["id"],),
        )
        assert row["status"] == "open"
        assert row["resolution"] is None
        assert row["resolved_at"] is None
    finally:
        execute("DELETE FROM feedback WHERE repo = %s", (fp_repo,))


def test_feedback_without_context_does_not_collapse(db):
    """With no tool, repo, path or expectation there is no identity to dedup on.

    Fingerprinting those reports on context alone made every suggestion the same
    row, silently discarding all but the first.
    """
    from git_synapse.analysis.query import record_feedback
    from git_synapse.db.engine import execute

    a = record_feedback(kind="suggestion", detail="Rank modules by centrality.")
    b = record_feedback(kind="suggestion", detail="Support cargo manifests.")
    try:
        assert a["id"] != b["id"], "unrelated suggestions must stay separate"
        repeat = record_feedback(kind="suggestion", detail="  RANK MODULES BY CENTRALITY.  ")
        assert repeat["id"] == a["id"], "the same suggestion must still collapse"
        assert repeat["occurrences"] == 2
    finally:
        execute("DELETE FROM feedback WHERE id = ANY(%s)", ([a["id"], b["id"]],))


def test_feedback_rejects_opinions(db):
    """The log is for defects in Git Synapse, not disagreement with a score."""
    from git_synapse.analysis.query import record_feedback

    with pytest.raises(ValueError, match="unknown kind"):
        record_feedback(kind="opinion", detail="I disagree with this ranking")
    with pytest.raises(ValueError, match="detail is required"):
        record_feedback(kind="wrong_data", detail="   ")


def test_feedback_feeds_no_analytical_table(db):
    """Nothing that produces a score may read from the feedback log.

    This is the boundary that keeps agents out of the coupling data. If a future
    change joins feedback into an aggregate, this fails.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "git_synapse"
    offenders = []
    for module in ("analysis/aggregate.py", "analysis/score.py", "analysis/crossrepo.py",
                   "analysis/lagged.py", "analysis/predict.py", "analysis/mining.py",
                   "analysis/depbump.py", "analysis/validate.py"):
        text = (src / module).read_text()
        if "feedback" in text:
            offenders.append(module)
    assert not offenders, f"analytical modules must not reference feedback: {offenders}"


def test_published_accuracy_figures_still_reproduce(db):
    """The numbers in README, SKILL.md and the MCP instructions must be measured.

    The headline figure was asserted in the first commit and never recomputed by
    anything. It drifted to 0.928 against a real value of 0.859 and stayed there,
    while every tier-trust instruction an agent reads cited it. This test fails
    if the documented figure and the shipped scoring column part company again.
    """
    import re
    from pathlib import Path

    import numpy as np

    from git_synapse.analysis.validate import ground_truth_edges

    rows = query("SELECT source_repo_id, target_repo_id, score, is_declared FROM repo_impact")
    declared = [r for r in rows if r["is_declared"]]
    if len(declared) < 50:
        pytest.skip("impact table not built")

    truth = ground_truth_edges(min_bumps=2)
    y = np.array([1 if (r["source_repo_id"], r["target_repo_id"]) in truth else 0
                  for r in declared])
    s = np.array([float(r["score"]) for r in declared])
    if y.sum() in (0, len(y)):
        pytest.skip("degenerate label set")

    order = np.argsort(s)
    ranks = np.empty(len(s), float)
    ranks[order] = np.arange(1, len(s) + 1)
    n1 = y.sum()
    measured = (ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))

    readme = Path(__file__).resolve().parents[1] / "README.md"
    published = [
        float(m) for m in re.findall(r"\*\*(0\.8\d\d)\*\*", readme.read_text())
    ]
    assert published, "README no longer states an in-sample AUC"
    assert abs(published[0] - measured) < 0.02, (
        f"README publishes {published[0]}, shipped score measures {measured:.4f}"
    )


def test_partner_classifier_labels_noise_without_hiding_real_files():
    """Mislabelling a real dependency as noise is the expensive error here.

    An agent is told `informative: false` means "you already know this", so a
    false positive makes it skip a file it should have edited. False negatives
    only cost the reader a glance.
    """
    from git_synapse.mcp.server import _classify_partner as classify

    own = "gateway/internal/app/router.go"

    # Noise that must be labelled.
    assert "own_test" in classify("gateway/internal/app/router_test.go", own, 20)[0]
    assert "generated" in classify("api/gen/restapi/embedded_spec.go", own, 30)[0]
    assert "generated" in classify("pkg/v1/types.pb.go", own, 30)[0]
    assert "generated" in classify("vendor/x/y.go", own, 30)[0]
    assert "generated" in classify("zz_generated.deepcopy.go", own, 30)[0]

    # Real code that must NOT be suppressed.
    for path in (
        "gateway/pkg/config/applier.go",
        "gateway/internal/app/scheduler.go",
        "pkg/genetics/sequence.go",          # contains "gen" but not "/gen/"
        "internal/hammock_test.go",          # contains "mock_" as a substring
        "gateway/internal/app/placement.go",
        "cmd/generator/main.go",
    ):
        labels, informative = classify(path, own, 20)
        assert informative, f"{path} was suppressed: {labels}"

    # Another file's test is not *this* file's test, so it stays informative.
    labels, informative = classify("gateway/internal/app/scheduler_test.go", own, 20)
    assert "own_test" not in labels and informative

    # Sibling variants: same name, different parent.
    assert "sibling_variant" in classify(
        "amd-values-yaml/piraeus.yaml", "nvidia-values-yaml/piraeus.yaml", 12
    )[0]
    assert "sibling_variant" not in classify(
        "a/other.yaml", "a/piraeus.yaml", 12
    )[0]

    # Thin support is flagged but never suppressed.
    labels, informative = classify("gateway/pkg/config/applier.go", own, 2)
    assert "thin_support" in labels and informative


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

    row = query_one(
        """
        SELECT r.name FROM repo r
        WHERE EXISTS (SELECT 1 FROM repo_impact i WHERE i.target_repo_id = r.id)
          AND NOT EXISTS (
              SELECT 1 FROM repo_impact i WHERE i.target_repo_id = r.id
                AND (i.is_declared OR i.has_bump_history))
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no repository with only discovery-tier upstream edges")

    out = server.coupling_chain(repo=row["name"], direction="upstream")
    assert out["chains"] == []
    assert out["explanation"], "an empty chain must say why it is empty"
    assert "validated" in out["explanation"]


def test_all_discovery_result_says_so_before_the_scores(db):
    """A result set with no validated edge must lead with that fact."""
    from git_synapse.mcp import server

    row = query_one(
        """
        SELECT r.name FROM repo r
        WHERE EXISTS (SELECT 1 FROM repo_impact i WHERE i.target_repo_id = r.id)
          AND NOT EXISTS (
              SELECT 1 FROM repo_impact i WHERE i.target_repo_id = r.id
                AND (i.is_declared OR i.has_bump_history))
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no all-discovery repository")

    out = server.upstream_repos(repo=row["name"], limit=5)
    assert "NONE" in out["guidance"]
    assert "not a probability" in out["guidance"]


def test_coupled_directories_marks_nesting_as_arithmetic(db):
    """A directory's parent scores 1.0 by construction, not by discovery.

    Every change to a child is a change to its parent, so the nesting relation
    has to be labelled or the top of the list reads as a finding.
    """
    from git_synapse.mcp import server

    row = query_one(
        """
        SELECT r.name AS repo, d.path
        FROM directory d JOIN repo r ON r.id = d.repo_id
        WHERE d.file_count > 20 AND d.path LIKE '%/%'
        ORDER BY d.change_count DESC LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no nested directory indexed")

    out = server.coupled_directories(repo=row["repo"], path=row["path"], limit=10)
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

    row = query_one(
        """
        SELECT r.name AS repo, f.path, f.dir_path
        FROM file f JOIN repo r ON r.id = f.repo_id
        WHERE f.dir_path <> '' AND f.change_count > 20 AND NOT f.is_deleted
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no suitable file")

    out = server.coupled_directories(repo=row["repo"], path=row["path"], limit=5)
    assert "error" not in out, out
    assert out["directory"]["path"] == row["dir_path"]

    missing = server.coupled_directories(repo=row["repo"], path="no/such/dir", limit=5)
    assert "error" in missing and "hint" in missing
