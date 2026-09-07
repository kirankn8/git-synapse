"""Regression tests for the SQLAlchemy mapping of the existing schema."""

from __future__ import annotations

from uuid import uuid4

from git_synapse.db.orm import models, session_scope


def test_all_application_tables_are_reflected_without_renaming(db):
    mapped = models()
    expected = {
        "Account", "Repo", "Author", "File", "Commit", "CommitFile",
        "FilePair", "FilePairMetric", "Directory", "DirPair", "DirPairMetric",
        "AuthorFile", "IngestRun", "IngestRunRepo", "DepBump", "RepoDependency",
        "ModuleDependency", "RepoImpact", "Feedback", "FileCluster", "PairDrift",
        "FileRisk", "RepoPackage", "CallLog", "AppUser", "UserSession",
        "ApiToken", "LoginAttempt", "Meta",
    }
    assert expected.issubset(set(vars(mapped)))
    assert mapped.Repo.__tablename__ == "repo"
    assert mapped.FilePairMetric.__tablename__ == "file_pair_metric"


def test_orm_transaction_rolls_back_and_preserves_jsonb_values(db):
    Meta = models().Meta
    key = f"orm-test:{uuid4().hex}"
    try:
        with session_scope() as session:
            session.add(Meta(key=key, value={"answer": 42}))

        with session_scope() as session:
            value = session.query(Meta).filter_by(key=key).one().value
            assert value == {"answer": 42}

        try:
            with session_scope() as session:
                session.add(Meta(key=f"orm-rollback:{uuid4().hex}", value={"ok": False}))
                raise RuntimeError("rollback probe")
        except RuntimeError:
            pass
    finally:
        with session_scope() as session:
            session.query(Meta).filter(Meta.key.like("orm-%")).delete(synchronize_session=False)
