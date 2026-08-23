"""End-to-end verification of aggregation and scoring.

A synthetic repository with a hand-designed co-change structure is loaded, then
aggregated and scored through the real SQL. The resulting stored measures are
compared against :mod:`git_synapse.stats.measures` evaluated directly on the counts
the test itself knows to be true.

This is the test that would catch a wrong population size, a mis-scoped
marginal, or an off-by-one in the pair self-join -- none of which the unit tests
in ``test_measures.py`` can see, because those start from a contingency table
that is assumed correct.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from git_synapse.analysis.aggregate import rebuild_repo
from git_synapse.analysis.query import coupled_files, pair_detail
from git_synapse.analysis.score import score_repo
from git_synapse.db.engine import connection, query, query_one
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import FileChange, ParsedCommit
from git_synapse.ingest.store import load_commits, upsert_repo
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import BY_KEY, CORE_KEYS

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)

# A deliberately designed history.
#
#   A and B always change together        -> 10 commits
#   A changes alone                       ->  5 commits
#   B changes alone                       ->  3 commits
#   C changes alone (never with A or B)   ->  7 commits
#
# Total pair-eligible commits N = 25.
# For the pair (A, B):  n_ab = 10, n_a = 15, n_b = 13
HISTORY = (
    [["A.py", "B.py"]] * 10
    + [["A.py"]] * 5
    + [["B.py"]] * 3
    + [["C.py"]] * 7
)
EXPECTED_N = 25
EXPECTED_AB = 10
EXPECTED_A = 15
EXPECTED_B = 13


@pytest.fixture(scope="module")
def analysed(scratch_db):
    """Load, aggregate and score the synthetic repository once."""
    record = RepoRecord(
        github_id=999_000_002,
        owner="test",
        name="analysis-fixture",
        full_name="test/analysis-fixture",
        clone_url="https://example.invalid/test/analysis-fixture.git",
    )
    with connection() as conn:
        repo_id = upsert_repo(record, conn)
        conn.execute("DELETE FROM commit WHERE repo_id=%s", (repo_id,))
        conn.execute("DELETE FROM file WHERE repo_id=%s", (repo_id,))

    commits = [
        ParsedCommit(
            sha=f"{i:040x}",
            parents=[f"{i - 1:040x}"] if i else [],
            author_name="Author One" if i % 2 else "Author Two",
            author_email="one@example.com" if i % 2 else "two@example.com",
            authored_at=BASE + timedelta(hours=i),
            committer_name="Author One",
            committer_email="one@example.com",
            committed_at=BASE + timedelta(hours=i),
            subject=f"synthetic {i}",
            body="",
            files=[FileChange(path=p, change_type="M", insertions=2, deletions=1) for p in paths],
        )
        for i, paths in enumerate(HISTORY)
    ]

    with connection() as conn:
        load_commits(repo_id, commits, conn)
    rebuild_repo(repo_id)
    score_repo(repo_id)

    yield repo_id

    with connection() as conn:
        conn.execute("DELETE FROM repo WHERE id=%s", (repo_id,))


def file_id(repo_id: int, path: str) -> int:
    row = query_one("SELECT id FROM file WHERE repo_id=%s AND path=%s", (repo_id, path))
    assert row is not None, f"file {path} not loaded"
    return row["id"]


def test_population_is_pair_eligible_commits_not_all_commits(analysed):
    """N must be the pair-eligible population, which is what the measures assume."""
    row = query_one("SELECT commit_count, pair_population FROM repo WHERE id=%s", (analysed,))
    assert row["commit_count"] == len(HISTORY)
    assert row["pair_population"] == EXPECTED_N


def test_marginals_match_the_designed_history(analysed):
    rows = {
        r["path"]: r
        for r in query(
            "SELECT path, change_count, pair_change_count FROM file WHERE repo_id=%s",
            (analysed,),
        )
    }
    assert rows["A.py"]["pair_change_count"] == EXPECTED_A
    assert rows["B.py"]["pair_change_count"] == EXPECTED_B
    assert rows["C.py"]["pair_change_count"] == 7


def test_joint_count_matches_the_designed_history(analysed):
    a, b = file_id(analysed, "A.py"), file_id(analysed, "B.py")
    lo, hi = sorted((a, b))
    row = query_one(
        "SELECT n_ab FROM file_pair WHERE repo_id=%s AND file_a_id=%s AND file_b_id=%s",
        (analysed, lo, hi),
    )
    assert row["n_ab"] == EXPECTED_AB


def test_uncoupled_file_produces_no_pair(analysed):
    """C never co-occurs with anything, so it must appear in no pair."""
    c = file_id(analysed, "C.py")
    rows = query(
        "SELECT 1 FROM file_pair WHERE repo_id=%s AND (file_a_id=%s OR file_b_id=%s)",
        (analysed, c, c),
    )
    assert rows == []


def test_stored_contingency_is_self_consistent(analysed):
    """Every stored pair must satisfy a,b,c,d >= 0 and a+b+c+d == N."""
    rows = query(
        "SELECT n_ab, n_a, n_b, n_total FROM file_pair_metric WHERE repo_id=%s", (analysed,)
    )
    assert rows, "scoring produced no rows"
    for r in rows:
        a, n_a, n_b, n = r["n_ab"], r["n_a"], r["n_b"], r["n_total"]
        b, c = n_a - a, n_b - a
        d = n - n_a - n_b + a
        assert min(a, b, c, d) >= 0, f"negative cell in {r}"
        assert a + b + c + d == n
        assert a <= min(n_a, n_b), "joint count exceeds a marginal"


@pytest.mark.parametrize("key", CORE_KEYS)
def test_every_stored_measure_matches_direct_computation(analysed, key):
    """The value in the database must equal the measure evaluated in Python.

    This closes the loop between the SQL aggregation and the statistics module:
    if the pair join, the marginals or the population were wrong, the stored
    value would diverge from the value computed from the counts this test
    independently asserts.
    """
    a, b = file_id(analysed, "A.py"), file_id(analysed, "B.py")
    detail = pair_detail(a, b)
    assert detail is not None

    table = Contingency.from_counts(
        n_ab=EXPECTED_AB, n_a=EXPECTED_A, n_b=EXPECTED_B, n_total=EXPECTED_N
    )
    expected = float(np.asarray(BY_KEY[key].compute(table)).reshape(-1)[0])
    assert detail[key] == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_confidence_direction_is_correct(analysed):
    """P(B|A) = 10/15 and P(A|B) = 10/13, and the query must not swap them."""
    a, b = file_id(analysed, "A.py"), file_id(analysed, "B.py")

    from_a = {p["path"]: p for p in coupled_files(a, measure="npmi", limit=10)}
    assert "B.py" in from_a
    # Asking from A's side: "given A changed, how often did B change?" = 10/15.
    assert from_a["B.py"]["confidence_out"] == pytest.approx(10 / 15)
    assert from_a["B.py"]["confidence_in"] == pytest.approx(10 / 13)

    from_b = {p["path"]: p for p in coupled_files(b, measure="npmi", limit=10)}
    # From B's side the two must be exchanged, regardless of storage order.
    assert from_b["A.py"]["confidence_out"] == pytest.approx(10 / 13)
    assert from_b["A.py"]["confidence_in"] == pytest.approx(10 / 15)


def test_directory_rollup_counts_a_directory_once_per_commit(analysed):
    """A commit touching several files in one directory counts that dir once."""
    rows = query(
        "SELECT path, change_count, pair_change_count FROM directory WHERE repo_id=%s",
        (analysed,),
    )
    root = next(r for r in rows if r["path"] == "")
    # Every commit touches at least one file, all at the repo root.
    assert root["pair_change_count"] == EXPECTED_N


def test_rescoring_is_deterministic(analysed):
    """Running the scorer twice must not change any value."""
    before = query(
        "SELECT file_a_id, file_b_id, npmi, log_likelihood_ratio, phi"
        " FROM file_pair_metric WHERE repo_id=%s ORDER BY file_a_id, file_b_id",
        (analysed,),
    )
    score_repo(analysed)
    after = query(
        "SELECT file_a_id, file_b_id, npmi, log_likelihood_ratio, phi"
        " FROM file_pair_metric WHERE repo_id=%s ORDER BY file_a_id, file_b_id",
        (analysed,),
    )
    assert before == after


def test_deleting_a_repo_cascades_its_derived_metrics(db):
    """Metric rows must not survive their repository.

    ``file_pair_metric`` originally had no foreign key to ``repo``, so deleting a
    repository cascaded away its ``file_pair`` rows but stranded the matching
    metric rows -- and ``TRUNCATE repo ... CASCADE`` skipped them entirely, so
    ``git-synapse reset`` left stale scores the API would still serve.
    """
    record = RepoRecord(
        github_id=999_000_003,
        owner="test",
        name="cascade-fixture",
        full_name="test/cascade-fixture",
        clone_url="https://example.invalid/test/cascade-fixture.git",
    )
    with connection() as conn:
        repo_id = upsert_repo(record, conn)

    commits = [
        ParsedCommit(
            sha=f"c{i:039x}",
            parents=[],
            author_name="A",
            author_email="a@example.com",
            authored_at=BASE + timedelta(hours=i),
            committer_name="A",
            committer_email="a@example.com",
            committed_at=BASE + timedelta(hours=i),
            subject="x",
            body="",
            files=[FileChange(path="x.py"), FileChange(path="y.py")],
        )
        for i in range(4)
    ]
    with connection() as conn:
        load_commits(repo_id, commits, conn)
    rebuild_repo(repo_id)
    score_repo(repo_id)

    def metric_rows() -> int:
        return query_one(
            "SELECT count(*) AS n FROM file_pair_metric WHERE repo_id=%s", (repo_id,)
        )["n"]

    assert metric_rows() > 0, "fixture produced no metrics to test the cascade with"

    with connection() as conn:
        conn.execute("DELETE FROM repo WHERE id=%s", (repo_id,))

    assert metric_rows() == 0, "metric rows outlived their repository"
    assert (
        query_one("SELECT count(*) AS n FROM file_pair WHERE repo_id=%s", (repo_id,))["n"] == 0
    )


def test_no_orphaned_metric_rows_exist(db):
    """Every metric row must correspond to a live pair row."""
    row = query_one(
        """
        SELECT count(*) AS n FROM file_pair_metric m
        WHERE NOT EXISTS (
            SELECT 1 FROM file_pair p
            WHERE p.repo_id = m.repo_id
              AND p.file_a_id = m.file_a_id
              AND p.file_b_id = m.file_b_id
        )
        """
    )
    assert row["n"] == 0, f"{row['n']} metric rows have no matching pair"


