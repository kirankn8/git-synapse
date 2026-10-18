"""Identifiers stated in commit messages, stored as written."""
from __future__ import annotations

from git_synapse.analysis import facts


def test_an_issue_key_is_taken_from_the_subject():
    assert facts.extract("AB-1234: rotate the seed", None) == [facts.Fact("issue_key", "AB-1234")]


def test_a_pr_reference_is_kept_as_written():
    assert facts.Fact("pr_ref", "99") in facts.extract("fix the thing (#99)", None)


def test_trailers_are_kept_and_deduplicated():
    got = facts.extract("subject", "Change-Id: Iabc\nChange-Id: Iabc\nSigned-off-by: A <a@b>")
    assert got == [facts.Fact("trailer", "change-id: Iabc"),
                   facts.Fact("trailer", "signed-off-by: A <a@b>")]


def test_prose_is_not_mined_for_identifiers():
    """A lowercase prefix or a word after the dash is not a key."""
    assert facts.extract("Fix the API-version parsing in a-1234567 style string", None) == []
    assert facts.extract("no identifiers here", "just a body") == []


def test_backfill_stores_facts_for_existing_commits(db):
    from datetime import UTC, datetime
    from uuid import uuid4

    from git_synapse.db.orm import models, session_scope

    tag = uuid4().hex[:8]
    with session_scope() as session:
        repo = models().Repo(full_name=f"t/facts-{tag}", name=f"facts-{tag}", owner="t")
        session.add(repo)
        session.flush()
        repo_id = repo.id
        when = datetime(2026, 1, 1, tzinfo=UTC)
        session.add(models().Commit(repo_id=repo_id, sha="ab" * 20,
                                    authored_at=when, committed_at=when,
                                    subject="AB-777: do the thing (#12)", body=None))

    try:
        assert facts.backfill() >= 1
        with session_scope() as session:
            keys = sorted((row.kind, row.key) for row in session.query(
                models().CommitFact).filter_by(repo_id=repo_id).all())
        assert keys == [("issue_key", "AB-777"), ("pr_ref", "12")]
    finally:
        with session_scope() as session:
            session.query(models().CommitFact).filter_by(repo_id=repo_id).delete(
                synchronize_session=False)
            session.query(models().Commit).filter_by(repo_id=repo_id).delete(
                synchronize_session=False)
            session.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)


def test_file_history_carries_the_facts_of_each_commit(db):
    from datetime import UTC, datetime
    from uuid import uuid4

    from git_synapse.analysis import query
    from git_synapse.db.orm import models, session_scope

    tag = uuid4().hex[:8]
    with session_scope() as session:
        repo = models().Repo(full_name=f"t/hist-{tag}", name=f"hist-{tag}", owner="t")
        session.add(repo)
        session.flush()
        file_row = models().File(repo_id=repo.id, path="a.go", basename="a.go")
        session.add(file_row)
        session.flush()
        when = datetime(2026, 1, 1, tzinfo=UTC)
        commit = models().Commit(repo_id=repo.id, sha="cd" * 20, authored_at=when,
                                 committed_at=when, subject="AB-9: fix (#7)")
        session.add(commit)
        session.flush()
        session.add(models().CommitFile(commit_id=commit.id, file_id=file_row.id, repo_id=repo.id))
        session.add_all([models().CommitFact(commit_id=commit.id, repo_id=repo.id,
                                             kind="issue_key", key="AB-9"),
                         models().CommitFact(commit_id=commit.id, repo_id=repo.id,
                                             kind="pr_ref", key="7")])
        repo_id, file_id = repo.id, file_row.id

    try:
        rows = query.file_commits(file_id, limit=5)
        assert [(f["kind"], f["key"]) for f in rows[0]["facts"]] == [("issue_key", "AB-9"), ("pr_ref", "7")]
    finally:
        with session_scope() as session:
            session.query(models().CommitFact).filter_by(repo_id=repo_id).delete(synchronize_session=False)
            session.query(models().CommitFile).filter_by(repo_id=repo_id).delete(synchronize_session=False)
            session.query(models().Commit).filter_by(repo_id=repo_id).delete(synchronize_session=False)
            session.query(models().File).filter_by(repo_id=repo_id).delete(synchronize_session=False)
            session.query(models().Repo).filter_by(id=repo_id).delete(synchronize_session=False)
