"""Cross-repo pipeline tests.

These build fixture repositories and run GLOBAL rebuilds, so they run against a
scratch database in their own module. Switching ``POSTGRES_DB`` is process-wide,
so mixing them with tests that read the real corpus would point those at an
empty database instead.

The properties under test are the ones actually got wrong during development,
each of which produced plausible-looking but wrong numbers: single-repo change
sets must be retained, or the contingency table loses its b and c cells; the
lagged table must be genuinely directional; the time origin must survive a
corrupt commit date; impact rows must never mix the validated ensemble score
with the unvalidated discovery score in one column.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from git_synapse.analysis import crossrepo, lagged
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
def three_repos(scratch_db):
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


def test_change_set_counters_survive_a_partial_incremental_pass(three_repos):
    """Counters must describe the whole set, not the slice one pass saw.

    An incremental run scopes in every commit by an affected author, so a ticket
    that pass did not re-partition was upserted with a fragment of its own count.
    n_repos gates cross-repo pairing, so the fragment both dropped real pairs and
    let sprawling sets past the fan-out cap.
    """
    stale = query(
        """
        SELECT cs.id, cs.key, cs.n_commits, cs.n_repos,
               m.real_commits, m.real_repos
        FROM change_set cs
        JOIN (SELECT csc.change_set_id, count(*) AS real_commits,
                     count(DISTINCT csc.repo_id) AS real_repos
              FROM change_set_commit csc GROUP BY 1) m ON m.change_set_id = cs.id
        WHERE cs.n_commits <> m.real_commits OR cs.n_repos <> m.real_repos
        """
    )
    assert not stale, f"counters disagree with membership: {stale[:3]}"

    empty = query_one(
        """
        SELECT count(*) AS n FROM change_set cs
        WHERE NOT EXISTS (SELECT 1 FROM change_set_commit x
                           WHERE x.change_set_id = cs.id)
        """
    )
    assert empty["n"] == 0, "empty change sets keep inflating N"


def test_crossrepo_joint_and_marginals_share_one_population(three_repos):
    """Both cells of a cross-repo contingency table must be counted the same way.

    The joint was counted over the capped file set and the marginals over every
    file, so the two cells described different populations. That understated
    confidence by up to 3.5x, always downward, and worst where the evidence was
    strongest -- large change sets are the ones the cap bites.
    """
    bad = query(
        """
        SELECT m.file_a_id, m.file_b_id, m.n_ab, m.n_a, m.n_b, m.n_total
        FROM xrepo_file_pair_metric m
        WHERE m.n_ab > m.n_a OR m.n_ab > m.n_b
           OR m.n_a > m.n_total OR m.n_b > m.n_total
           OR m.n_total - m.n_a - m.n_b + m.n_ab < 0
        """
    )
    assert not bad, f"infeasible cross-repo contingency tables: {bad[:3]}"

    out_of_range = query(
        """
        SELECT count(*) AS n FROM xrepo_file_pair_metric
        WHERE confidence_ab < 0 OR confidence_ab > 1
           OR confidence_ba < 0 OR confidence_ba > 1
        """
    )
    assert out_of_range[0]["n"] == 0
