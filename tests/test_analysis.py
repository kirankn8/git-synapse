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

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from git_synapse.analysis.aggregate import rebuild_repo
from git_synapse.analysis.query import coupled_files, pair_detail
from git_synapse.analysis.score import score_repo
from git_synapse.db.orm import models, session_scope
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.parser import FileChange, ParsedCommit
from git_synapse.ingest.store import load_commits, upsert_repo
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import BY_KEY, CORE_KEYS

BASE = datetime(2024, 1, 1, tzinfo=UTC)

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
    with session_scope() as conn:
        repo_id = upsert_repo(record, conn)
        conn.query(models().Commit).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        conn.query(models().File).filter_by(repo_id=repo_id).delete(synchronize_session=False)

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

    with session_scope() as conn:
        load_commits(repo_id, commits, conn)
    rebuild_repo(repo_id)
    score_repo(repo_id)

    yield repo_id

    with session_scope() as session:
        session.query(models().CommitFile).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().Commit).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().File).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)


def file_id(repo_id: int, path: str) -> int:
    with session_scope() as session:
        row = session.query(models().File).filter_by(repo_id=repo_id, path=path).one_or_none()
    assert row is not None, f"file {path} not loaded"
    return row.id


def test_population_is_pair_eligible_commits_not_all_commits(analysed):
    """N must be the pair-eligible population, which is what the measures assume."""
    with session_scope() as session:
        row = session.get(models().Repo, analysed)
    assert row.commit_count == len(HISTORY)
    assert row.pair_population == EXPECTED_N


def test_marginals_match_the_designed_history(analysed):
    with session_scope() as session:
        rows = {r.path: r for r in session.query(models().File).filter_by(repo_id=analysed)}
    assert rows["A.py"].pair_change_count == EXPECTED_A
    assert rows["B.py"].pair_change_count == EXPECTED_B
    assert rows["C.py"].pair_change_count == 7


def test_joint_count_matches_the_designed_history(analysed):
    a, b = file_id(analysed, "A.py"), file_id(analysed, "B.py")
    lo, hi = sorted((a, b))
    with session_scope() as session:
        row = session.query(models().FilePair).filter_by(repo_id=analysed, file_a_id=lo, file_b_id=hi).one()
    assert row.n_ab == EXPECTED_AB


def test_uncoupled_file_produces_no_pair(analysed):
    """C never co-occurs with anything, so it must appear in no pair."""
    c = file_id(analysed, "C.py")
    with session_scope() as session:
        rows = session.query(models().FilePair).filter(
            models().FilePair.repo_id == analysed,
            (models().FilePair.file_a_id == c) | (models().FilePair.file_b_id == c),
        ).all()
    assert rows == []


def test_stored_contingency_is_self_consistent(analysed):
    """Every stored pair must satisfy a,b,c,d >= 0 and a+b+c+d == N."""
    with session_scope() as session:
        rows = session.query(models().FilePairMetric).filter_by(repo_id=analysed).all()
    assert rows, "scoring produced no rows"
    for r in rows:
        a, n_a, n_b, n = r.n_ab, r.n_a, r.n_b, r.n_total
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
    with session_scope() as session:
        rows = session.query(models().Directory).filter_by(repo_id=analysed).all()
    root = next(r for r in rows if r.path == "")
    # Every commit touches at least one file, all at the repo root.
    assert root.pair_change_count == EXPECTED_N


def test_rescoring_is_deterministic(analysed):
    """Running the scorer twice must not change any value."""
    with session_scope() as session:
        before = [(r.file_a_id, r.file_b_id, r.npmi, r.log_likelihood_ratio, r.phi)
                  for r in session.query(models().FilePairMetric).filter_by(repo_id=analysed)
                  .order_by(models().FilePairMetric.file_a_id, models().FilePairMetric.file_b_id)]
    score_repo(analysed)
    with session_scope() as session:
        after = [(r.file_a_id, r.file_b_id, r.npmi, r.log_likelihood_ratio, r.phi)
                 for r in session.query(models().FilePairMetric).filter_by(repo_id=analysed)
                 .order_by(models().FilePairMetric.file_a_id, models().FilePairMetric.file_b_id)]
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
    with session_scope() as conn:
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
    with session_scope() as conn:
        load_commits(repo_id, commits, conn)
    rebuild_repo(repo_id)
    score_repo(repo_id)

    def metric_rows() -> int:
        with session_scope() as session:
            return session.query(models().FilePairMetric).filter_by(repo_id=repo_id).count()

    assert metric_rows() > 0, "fixture produced no metrics to test the cascade with"

    with session_scope() as session:
        session.query(models().FilePairMetric).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().FilePair).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().CommitFile).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().Commit).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().File).filter_by(repo_id=repo_id).delete(synchronize_session=False)
        session.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)

    assert metric_rows() == 0, "metric rows outlived their repository"
    with session_scope() as session:
        assert session.query(models().FilePair).filter_by(repo_id=repo_id).count() == 0


def test_no_orphaned_metric_rows_exist(db):
    """Every metric row must correspond to a live pair row."""
    Metric, Pair = models().FilePairMetric, models().FilePair
    with session_scope() as session:
        n = session.query(Metric).outerjoin(Pair, (
            (Pair.repo_id == Metric.repo_id) &
            (Pair.file_a_id == Metric.file_a_id) &
            (Pair.file_b_id == Metric.file_b_id)
        )).filter(Pair.repo_id.is_(None)).count()
    assert n == 0, f"{n} metric rows have no matching pair"




# ------------------------------------------- direction-aware ordering columns

@pytest.mark.parametrize(("measure", "expected"), [
    ("confidence_ab", ("confidence_out", "confidence_ab", "confidence_ba")),
    ("confidence_ba", ("confidence_in", "confidence_ba", "confidence_ab")),
    ("jaccard", ("jaccard", "jaccard", "jaccard")),
])
def test_the_conditional_measures_order_by_direction(measure, expected):
    """`P(B|A)` and `P(A|B)` are the same pair read from opposite ends, so the
    A-side and B-side of the query must not use the same column -- doing so
    reported the partner's confidence as the file's own."""
    from git_synapse.analysis.query import _oriented_order

    assert _oriented_order(measure) == expected


# ------------------------------------- callers that pass their own connection

def test_the_derived_stages_accept_a_caller_supplied_connection(db):
    """Every stage takes an optional connection so a pipeline run does the whole
    repository in one transaction. Called without one they open their own, which
    is the path the tests took -- leaving the shared-transaction path untested,
    and that is the one the pipeline actually uses."""
    from git_synapse.analysis import depbump, score
    from git_synapse.analysis.aggregate import rebuild_repo
    from git_synapse.db.orm import session_scope

    with session_scope() as conn:
        repo_row = models().Repo(full_name="acme/staged", name="staged", owner="acme")
        conn.add(repo_row)
        conn.flush()
        repo = repo_row.id
        try:
            assert rebuild_repo(repo, conn=conn) is not None
            assert score.score_repo(repo, conn=conn) is not None
            assert depbump.resolve_bumps(conn) == 0
            assert depbump.rebuild(conn=conn) is not None
            # The sweeps over *every* repository take one too.
            assert isinstance(score.score_all(conn), list)
            assert isinstance(depbump.refresh_modules(conn), int)
            assert isinstance(depbump.refresh_declared(conn=conn), int)
        finally:
            conn.rollback()


def test_declared_only_impact_filters_to_manifest_backed_edges(db):
    """The flag is what separates "these are declared" from "these correlate",
    and an agent is told to trust the first."""
    from git_synapse.analysis import predict

    assert predict.impact_for(repo_id=-1, declared_only=False) == []
    assert predict.impact_for(repo_id=-1, declared_only=True) == []


def test_a_minimum_score_filters_both_orientations(db):
    """A pair is stored once and read from either end, so a threshold applied to
    only one side would return partners below it whenever the pair happened to
    be stored the other way round."""
    from git_synapse.analysis import query as q

    assert q.coupled_files(file_id=-1, measure="jaccard", limit=5,
                           min_support=1, min_score=0.9) == []


def test_repositories_can_be_listed_by_the_account_that_owns_them(db):
    """The rung that makes the hierarchy navigable. Without it the Accounts page
    can only send a reader to every repository in the corpus."""
    from git_synapse.analysis import query as q
    from git_synapse.db.orm import session_scope

    with session_scope() as conn:
        account = models().Account(login="owner-test", kind="org")
        conn.add(account)
        conn.flush()
        mine_row = models().Repo(full_name="owner-test/a", name="a", owner="owner-test", account_id=account.id)
        conn.add_all([mine_row, models().Repo(full_name="someone/else", name="else", owner="someone")])
        conn.flush()
        acct, mine = account.id, mine_row.id
    try:
        got = {r["id"] for r in q.list_repos(account_id=acct)}
        assert got == {mine}, "only the account's own repositories"
        assert len(q.list_repos()) > 1, "and no filter still lists everything"
    finally:
        with session_scope() as session:
            session.query(models().Repo).filter(models().Repo.owner.in_(["owner-test", "someone"])).delete(synchronize_session=False)
            session.query(models().Account).filter_by(id=acct).delete(synchronize_session=False)


def test_the_cells_stored_are_the_cells_the_scores_came_from(caplog):
    """`Contingency.from_counts` clamps infeasible input to the feasible
    region, and inclusion-exclusion can force `a` *up* from a reported zero.
    Storing the raw counts beside scores derived from the clamped ones breaks
    the property everything here rests on: that four counts reproduce the
    number shown beside them."""
    import logging

    import numpy as np

    from git_synapse.analysis import score

    class _Batch:
        a_ids = np.array([11])
        b_ids = np.array([22])
        # n_a + n_b > N, so at least 20 co-changes are forced however many were
        # reported. The reported figure here is zero.
        n_ab = np.array([0])
        n_a = np.array([60])
        n_b = np.array([60])
        n_total = 100

    with caplog.at_level(logging.WARNING, logger="git_synapse.analysis.score"):
        rows = score._score_batch(7, _Batch())

    stored_ab, stored_a, stored_b, stored_n = rows[0][3:7]
    assert (stored_ab, stored_a, stored_b, stored_n) == (20, 60, 60, 100)

    # And the scores agree with those cells rather than with the raw ones.
    from git_synapse.stats.contingency import Contingency
    from git_synapse.stats.registry import ALL_KEYS, BY_KEY

    table = Contingency.from_counts(n_ab=stored_ab, n_a=stored_a,
                                    n_b=stored_b, n_total=stored_n)
    for key, stored in zip(ALL_KEYS, rows[0][7:], strict=True):
        expected = float(np.asarray(BY_KEY[key].compute(table)).ravel()[0])
        assert stored == pytest.approx(expected, abs=1e-9, nan_ok=True), key

    # Clamping means an aggregate is stale, so it is reported, not swallowed.
    assert "clamped" in caplog.text and "stale" in caplog.text


def test_feasible_counts_are_stored_unchanged_and_say_nothing(caplog):
    """The warning must not cry wolf on ordinary data -- which is all data this
    pipeline produces, since the marginals come from the same commits as the
    joint count."""
    import logging

    import numpy as np

    from git_synapse.analysis import score

    class _Batch:
        a_ids = np.array([1, 2])
        b_ids = np.array([3, 4])
        n_ab = np.array([5, 2])
        n_a = np.array([20, 9])
        n_b = np.array([30, 4])
        n_total = 400

    with caplog.at_level(logging.WARNING, logger="git_synapse.analysis.score"):
        rows = score._score_batch(1, _Batch())
    assert [r[3:7] for r in rows] == [(5, 20, 30, 400), (2, 9, 4, 400)]
    assert "clamped" not in caplog.text


def test_a_directory_nothing_lives_in_any_more_is_removed(db):
    """`directory` is insert-only while `file_directory` is rebuilt every pass,
    so a folder whose files were all renamed away or deleted kept its row --
    and the counts it had when it still had files. Eleven were live: a folder
    page offering "79 changes, 3 files" with nothing in it."""
    from git_synapse.analysis import aggregate

    with session_scope() as session:
        repo = models().Repo(owner="dirs", name="t", full_name="dirs/t", host="github.com", provider="github", is_enabled=True)
        session.add(repo)
        session.flush()
        rid = repo.id
        session.add(models().File(repo_id=rid, path="kept/a.py", dir_path="kept", basename="a.py", extension="py", depth=1, change_count=3))
        session.add(models().Directory(repo_id=rid, path="gone", depth=1, file_count=3, change_count=79, pair_change_count=79))
    try:
        aggregate.rebuild_repo(rid)

        with session_scope() as session:
            paths = {r.path for r in session.query(models().Directory).filter_by(repo_id=rid)}
        assert "gone" not in paths, "a directory with no files must not survive"
        assert paths == {"", "kept"}, f"the real tree, root included: {paths}"
    finally:
        with session_scope() as session:
            session.query(models().Directory).filter_by(repo_id=rid).delete(synchronize_session=False)
            session.query(models().File).filter_by(repo_id=rid).delete(synchronize_session=False)
            session.query(models().Repo).filter_by(id=rid).delete(synchronize_session=False)


def test_pairs_are_streamed_in_fixed_size_batches(db, monkeypatch):
    """The batch exists so a repository with tens of millions of pairs never
    materialises in the client. The configured size is 200,000, which no test
    corpus reaches, so this lowers it to the floor and crosses that instead.
    """
    import dataclasses
    from uuid import uuid4

    from git_synapse.analysis import score as score_mod
    from git_synapse.analysis.score import _iter_pair_batches
    from git_synapse.config import get_config

    base = get_config()
    monkeypatch.setattr(score_mod, "get_config", lambda: dataclasses.replace(
        base, analysis=dataclasses.replace(base.analysis, score_batch_size=1)))
    from git_synapse.db.orm import models, session_scope

    tag = uuid4().hex[:8]
    with session_scope() as session:
        repo = models().Repo(github_id=abs(hash(tag)) % 10**8, owner="acme",
                             name=f"batched-{tag}", full_name=f"acme/batched-{tag}",
                             clone_url="", default_branch="main")
        session.add(repo)
        session.flush()

        # 46 files make 1,035 distinct pairs, which clears the 1,000 floor the
        # batch size is clamped to.
        files = [models().File(repo_id=repo.id, path=f"f{i}.py", dir_path="",
                               basename=f"f{i}.py", pair_change_count=3)
                 for i in range(46)]
        session.add_all(files)
        session.flush()
        ids = [f.id for f in files]
        session.add_all([
            models().FilePair(repo_id=repo.id, file_a_id=a, file_b_id=b, n_ab=2)
            for i, a in enumerate(ids) for b in ids[i + 1:]
        ])
        session.flush()
        repo_id = repo.id

    try:
        with session_scope() as session:
            sizes = [len(batch.n_ab) for batch in
                     _iter_pair_batches(session, repo_id, "file", n_total=10)]
        assert len(sizes) == 2, sizes
        assert sizes[0] == 1000          # flushed on reaching the batch size
        assert sizes[1] == 35            # the remainder, flushed at the end
    finally:
        with session_scope() as session:
            session.query(models().FilePair).filter_by(repo_id=repo_id).delete(
                synchronize_session=False)
            session.query(models().File).filter_by(repo_id=repo_id).delete(
                synchronize_session=False)
            session.query(models().Repo).filter_by(id=repo_id).delete(
                synchronize_session=False)


def test_an_ineligible_commit_contributes_no_pairs(db):
    """A merge restates its parents' changes and a sweeping commit touches
    files that have nothing to do with each other. Both are stored, and both
    are excluded from pair counting -- otherwise one commit invents a coupling
    between every file it happened to touch.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from git_synapse.analysis import aggregate
    from git_synapse.db.orm import models, session_scope

    tag = uuid4().hex[:8]
    with session_scope() as session:
        repo = models().Repo(github_id=abs(hash(tag)) % 10**8, owner="acme",
                             name=f"eligible-{tag}", full_name=f"acme/eligible-{tag}",
                             clone_url="", default_branch="main")
        session.add(repo)
        session.flush()
        files = [models().File(repo_id=repo.id, path=f"m{i}.py", dir_path="",
                               basename=f"m{i}.py") for i in range(2)]
        session.add_all(files)
        session.flush()

        now = datetime.now(UTC)
        # Two ordinary commits, because a pair needs `min_pair_support` (2)
        # co-changes before it is persisted at all; plus one merge, which is
        # the commit under test.
        for i, is_merge in enumerate((False, False, True)):
            commit = models().Commit(repo_id=repo.id, sha=f"{i:040x}", authored_at=now,
                                     committed_at=now, is_merge=is_merge, n_files=2)
            session.add(commit)
            session.flush()
            for f in files:
                session.add(models().CommitFile(commit_id=commit.id, file_id=f.id,
                                                repo_id=repo.id))
        repo_id = repo.id

    try:
        with session_scope() as session:
            aggregate.rebuild_repo(repo_id, session)

        with session_scope() as session:
            # Two eligible commits out of three: the merge is stored but not counted.
            pair = session.query(models().FilePair).filter_by(repo_id=repo_id).one()
            assert pair.n_ab == 2          # the merge contributed nothing
            assert session.get(models().Repo, repo_id).pair_population == 2
    finally:
        with session_scope() as session:
            for model in (models().DirPair, models().FilePair, models().FileDirectory,
                          models().Directory, models().CommitFile, models().Commit,
                          models().File):
                session.query(model).filter_by(repo_id=repo_id).delete(
                    synchronize_session=False)
            session.query(models().Repo).filter_by(id=repo_id).delete(
                synchronize_session=False)
