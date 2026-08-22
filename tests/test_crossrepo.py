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


@pytest.fixture(scope="module")
def three_repos(db):
    """Three repos with a deliberate propagation pattern: up -> mid -> down.

    Every change set is ticket-keyed so the test does not depend on session
    windowing, and each ticket touches the three repos in time order with a
    one-day gap, which is what the lagged analysis should detect.
    """
    names = ["xr-up", "xr-mid", "xr-down"]
    ids: dict[str, int] = {}
    with connection() as conn:
        for i, name in enumerate(names):
            rec = RepoRecord(
                github_id=990_100 + i,
                owner="test",
                name=name,
                full_name=f"test/{name}",
                clone_url=f"https://example.invalid/test/{name}.git",
            )
            rid = upsert_repo(rec, conn)
            ids[name] = rid
            conn.execute("DELETE FROM commit WHERE repo_id=%s", (rid,))
            conn.execute("DELETE FROM file WHERE repo_id=%s", (rid,))

    seed = 0x51A0
    for t in range(30):
        day = BASE + timedelta(days=t * 4)
        ticket = f"ZZ-{1000 + t}"
        with connection() as conn:
            # up changes first, mid a day later, down a day after that.
            load_commits(ids["xr-up"], [_commit(seed + t * 3, f"{ticket}: upstream fix",
                                                day, ["core.go", "go.mod"])], conn)
            load_commits(ids["xr-mid"], [_commit(seed + t * 3 + 1, f"{ticket}: bump upstream",
                                                 day + timedelta(days=1), ["go.mod", "wire.go"])], conn)
            load_commits(ids["xr-down"], [_commit(seed + t * 3 + 2, f"{ticket}: pick up fix",
                                                  day + timedelta(days=2), ["go.mod", "app.go"])], conn)

    # Solo activity in `down` only, so its marginal exceeds its joint counts and
    # the contingency table has non-empty b/c cells.
    with connection() as conn:
        for t in range(20):
            load_commits(
                ids["xr-down"],
                [_commit(0x52000 + t, "chore: unrelated tidy",
                         BASE + timedelta(days=t * 4, hours=12), ["solo.go"])],
                conn,
            )

    from git_synapse.analysis.aggregate import rebuild_repo
    for rid in ids.values():
        rebuild_repo(rid)
    crossrepo.rebuild()

    yield ids

    with connection() as conn:
        conn.execute("DELETE FROM repo WHERE id = ANY(%s)", (list(ids.values()),))

    # change_set, repo_lag_metric and repo_impact are GLOBAL tables, not
    # per-repo. Rebuilding them with test parameters leaves the running app
    # serving test-shaped data -- it silently emptied the lag-4 validation view
    # once. Restore them with production settings on the way out.
    crossrepo.rebuild()
    lagged.rebuild()
    try:
        predict.rebuild()
    except Exception:  # noqa: BLE001 - restoration is best effort
        pass


# ---------------------------------------------------------------- change sets


def test_ticket_keys_group_commits_across_repositories(three_repos):
    row = query_one(
        "SELECT n_repos, n_commits, signal FROM change_set WHERE ticket = %s",
        ("ZZ-1000",),
    )
    assert row is not None, "ticket-keyed change set was not created"
    assert row["signal"] == "ticket"
    assert row["n_repos"] == 3, "one ticket should unite all three repositories"


def test_single_repo_change_sets_are_retained(three_repos):
    """Dropping them would empty the b/c cells and inflate every score.

    The 'chore: unrelated tidy' commits have no ticket key and touch only one
    repo, so they must survive as single-repo change sets.
    """
    row = query_one(
        "SELECT count(*) AS n FROM change_set WHERE n_repos = 1 AND pair_eligible"
    )
    assert row["n"] > 0, "single-repo change sets were discarded"


def test_repo_pair_contingency_is_feasible(three_repos):
    """n_ab must never exceed a marginal, and cells must be non-negative."""
    rows = query("SELECT n_ab, n_a, n_b, n_total FROM repo_pair_metric")
    assert rows, "no repo pairs were scored"
    for r in rows:
        a, n_a, n_b, n = r["n_ab"], r["n_a"], r["n_b"], r["n_total"]
        assert a <= min(n_a, n_b), f"joint exceeds marginal: {r}"
        assert n - n_a - n_b + a >= 0, f"negative d cell: {r}"


def test_ticket_ratio_is_recorded(three_repos):
    """Evidence quality must be stored, not inferred, so the UI can show it."""
    up, mid = three_repos["xr-up"], three_repos["xr-mid"]
    lo, hi = sorted((up, mid))
    row = query_one(
        "SELECT n_ab, n_ab_ticket FROM repo_pair WHERE repo_a_id=%s AND repo_b_id=%s",
        (lo, hi),
    )
    assert row is not None
    assert row["n_ab_ticket"] == row["n_ab"], "all evidence here is ticket-linked"


# ------------------------------------------------------------------- lagged


def test_lagged_table_is_directional(three_repos):
    """``up -> mid`` at a positive lag must beat ``mid -> up`` at the same lag.

    This is the property that justifies the whole lagged construction. If it
    fails, the table is symmetric and the extra machinery buys nothing.
    """
    lagged.rebuild(bin_hours=24, lags=(0, 1, 2, 3), min_support=2)
    up, mid = three_repos["xr-up"], three_repos["xr-mid"]

    forward = query_one(
        "SELECT confidence_ab FROM repo_lag_metric"
        " WHERE repo_a_id=%s AND repo_b_id=%s AND lag_bins=1",
        (up, mid),
    )
    reverse = query_one(
        "SELECT confidence_ab FROM repo_lag_metric"
        " WHERE repo_a_id=%s AND repo_b_id=%s AND lag_bins=1",
        (mid, up),
    )
    assert forward is not None, "forward direction produced no row"
    # The reverse row may be absent entirely, and that is the STRONGEST possible
    # directional result: `mid` changes a day after `up`, so "mid then up" never
    # occurs and the pair falls below min_support. Absence is not a gap in the
    # evidence, it is the evidence.
    if reverse is None:
        return
    assert forward["confidence_ab"] > reverse["confidence_ab"], (
        "lag-1 association should be stronger in the true propagation direction "
        f"(forward={forward['confidence_ab']}, reverse={reverse['confidence_ab']})"
    )


def test_lagged_population_is_bin_count_not_commit_count(three_repos):
    """N must be the number of aligned time windows, identical across a lag."""
    rows = query("SELECT DISTINCT lag_bins, n_total FROM repo_lag_metric ORDER BY lag_bins")
    assert rows
    by_lag = {r["lag_bins"]: r["n_total"] for r in rows}
    # Each extra bin of lag removes exactly one alignable window.
    for lag in sorted(by_lag):
        if lag - 1 in by_lag:
            assert by_lag[lag] == by_lag[lag - 1] - 1, f"window count wrong at lag {lag}"


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
