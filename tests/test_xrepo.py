"""Cross-repository file pairs built from declared bumps."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from git_synapse.analysis import xrepo
from git_synapse.db.orm import models, session_scope

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _repo(session, name: str, tag: str) -> int:
    row = models().Repo(full_name=f"acme/{name}-{tag}", name=f"{name}-{tag}", owner="acme")
    session.add(row)
    session.flush()
    return row.id


def _commit(session, repo_id: int, sha: str) -> int:
    row = models().Commit(repo_id=repo_id, sha=sha, authored_at=NOW, committed_at=NOW)
    session.add(row)
    session.flush()
    return row.id


def _file(session, repo_id: int, path: str) -> int:
    existing = session.query(models().File).filter_by(repo_id=repo_id, path=path).first()
    if existing is not None:
        return existing.id
    base = path.rsplit("/", 1)[-1]
    row = models().File(repo_id=repo_id, path=path, basename=base,
                        dir_path=path.rsplit("/", 1)[0] if "/" in path else "")
    session.add(row)
    session.flush()
    return row.id


def _change(session, repo_id: int, commit_id: int, file_id: int) -> None:
    session.add(models().CommitFile(commit_id=commit_id, file_id=file_id, repo_id=repo_id))


def _bump(session, consumer_id: int, consumer_sha: str, dep_id: int, dep_commit_id: int,
          version: str = "v1.2.0", when: datetime = NOW) -> None:
    session.add(models().DepBump(
        consumer_repo_id=consumer_id, consumer_sha=consumer_sha, dep_repo_id=dep_id,
        dep_name="acme/upstream", dep_version=version, dep_commit_id=dep_commit_id,
        manifest="go.mod", bumped_at=when, ecosystem="go", resolution="sha",
    ))


def _scenario(tag: str):
    """A consumer change and the upstream commit its pin adopted, with noise on both sides."""
    with session_scope() as session:
        consumer = _repo(session, "consumer", tag)
        upstream = _repo(session, "upstream", tag)
        c1 = _commit(session, consumer, "c1" * 20)
        u1 = _commit(session, upstream, "u1" * 20)
        c_go, c_code, c_test = (_file(session, consumer, "go.mod"),
                                _file(session, consumer, "svc/a.go"),
                                _file(session, consumer, "svc/a_auto_test.go"))
        u_sum, u_code, u_gen = (_file(session, upstream, "go.sum"),
                                _file(session, upstream, "pkg/x.go"),
                                _file(session, upstream, "vendor/gen/g.go"))
        for fid in (c_go, c_code, c_test):
            _change(session, consumer, c1, fid)
        for fid in (u_sum, u_code, u_gen):
            _change(session, upstream, u1, fid)
        _bump(session, consumer, "c1" * 20, upstream, u1)
        return consumer, upstream, c_code, u_code


def _cleanup(tag: str) -> None:
    with session_scope() as session:
        repos = session.query(models().Repo).filter(
            models().Repo.full_name.in_([f"acme/consumer-{tag}", f"acme/upstream-{tag}"])).all()
        ids = [row.id for row in repos]
        session.query(models().XrepoFilePair).filter(
            models().XrepoFilePair.consumer_repo_id.in_(ids)).delete(synchronize_session=False)
        session.query(models().DepBump).filter(
            models().DepBump.consumer_repo_id.in_(ids)).delete(synchronize_session=False)
        session.query(models().CommitFile).filter(
            models().CommitFile.repo_id.in_(ids)).delete(synchronize_session=False)
        session.query(models().Commit).filter(
            models().Commit.repo_id.in_(ids)).delete(synchronize_session=False)
        session.query(models().File).filter(
            models().File.repo_id.in_(ids)).delete(synchronize_session=False)
        session.query(models().Repo).filter(
            models().Repo.id.in_(ids)).delete(synchronize_session=False)


def test_only_source_files_are_paired_across_the_bump(db):
    """Manifests, lockfiles and generated trees describe the build, not a relationship."""
    tag = uuid4().hex[:8]
    consumer, _upstream, c_code, u_code = _scenario(tag)
    try:
        xrepo.rebuild(consumer_ids={consumer})
        with session_scope() as session:
            rows = session.query(models().XrepoFilePair).filter_by(
                consumer_repo_id=consumer).all()
            assert [(r.upstream_file_id, r.consumer_file_id, r.n_bumps) for r in rows] == [
                (u_code, c_code, 1)
            ]
    finally:
        _cleanup(tag)


def test_rescanning_a_consumer_replaces_its_rows_rather_than_duplicating_them(db):
    tag = uuid4().hex[:8]
    consumer, *_ = _scenario(tag)
    try:
        xrepo.rebuild(consumer_ids={consumer})
        xrepo.rebuild(consumer_ids={consumer})
        with session_scope() as session:
            assert session.query(models().XrepoFilePair).filter_by(
                consumer_repo_id=consumer).count() == 1
    finally:
        _cleanup(tag)


def test_a_bulk_bump_is_skipped_rather_than_paired_exhaustively(db):
    """A release that rewrites thousands of files would otherwise invent 100k pairs."""
    tag = uuid4().hex[:8]
    with session_scope() as session:
        consumer = _repo(session, "consumer", tag)
        upstream = _repo(session, "upstream", tag)
        c1 = _commit(session, consumer, "c2" * 20)
        u1 = _commit(session, upstream, "u2" * 20)
        for i in range(80):
            _change(session, consumer, c1, _file(session, consumer, f"svc/f{i}.go"))
            _change(session, upstream, u1, _file(session, upstream, f"pkg/f{i}.go"))
        _bump(session, consumer, "c2" * 20, upstream, u1)
    try:
        stats = xrepo.rebuild(consumer_ids={consumer})
        assert stats.skipped_bulk == 1 and stats.pairs == 0
    finally:
        _cleanup(tag)


def test_the_same_pair_seen_in_two_bumps_carries_support_two(db):
    tag = uuid4().hex[:8]
    consumer, upstream, _c_code, _u_code = _scenario(tag)
    try:
        with session_scope() as session:
            c2 = _commit(session, consumer, "c3" * 20)
            u2 = _commit(session, upstream, "u3" * 20)
            f = _file(session, consumer, "svc/a.go")
            _change(session, consumer, c2, _file(session, consumer, "go.mod"))
            _change(session, consumer, c2, f)
            _change(session, upstream, u2, _file(session, upstream, "pkg/x.go"))
            _bump(session, consumer, "c3" * 20, upstream, u2, version="v1.3.0",
                  when=datetime(2026, 2, 1, 9, 0, tzinfo=UTC))
        xrepo.rebuild(consumer_ids={consumer})
        with session_scope() as session:
            row = session.query(models().XrepoFilePair).filter_by(
                consumer_repo_id=consumer).one()
            assert row.n_bumps == 2 and row.last_version == "v1.3.0"
    finally:
        _cleanup(tag)


def test_a_consumer_file_answers_with_upstream_partners_and_the_reverse(db):
    tag = uuid4().hex[:8]
    consumer, _upstream, c_code, u_code = _scenario(tag)
    try:
        xrepo.rebuild(consumer_ids={consumer})
        up = xrepo.coupled_across_repos(c_code, min_support=1)
        down = xrepo.coupled_across_repos(u_code, min_support=1)
        assert [(row["direction"], row["path"]) for row in up] == [("upstream", "pkg/x.go")]
        assert [(row["direction"], row["path"]) for row in down] == [("downstream", "svc/a.go")]
    finally:
        _cleanup(tag)


def test_a_pair_reports_the_most_recent_bump_not_whichever_came_last(db):
    """The row is an answer about now, so the version it names must be the newest one seen."""
    tag = uuid4().hex[:8]
    consumer, upstream, c_code, u_code = _scenario(tag)
    try:
        with session_scope() as session:
            c2 = _commit(session, consumer, "c4" * 20)
            u2 = _commit(session, upstream, "u4" * 20)
            _change(session, consumer, c2, _file(session, consumer, "svc/a.go"))
            _change(session, upstream, u2, _file(session, upstream, "pkg/x.go"))
            _bump(session, consumer, "c4" * 20, upstream, u2, version="v9.9.9",
                  when=datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        xrepo.rebuild(consumer_ids={consumer})
        with session_scope() as session:
            row = session.query(models().XrepoFilePair).filter_by(
                consumer_repo_id=consumer).one()
            assert row.n_bumps == 2 and row.last_version == "v9.9.9"
    finally:
        _cleanup(tag)
