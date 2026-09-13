"""Tests for the declarative ORM database lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from git_synapse.db import engine
from git_synapse.db.orm import models, session_scope


def test_watermark_round_trips_and_uses_the_caller_transaction(db):
    key = "test-watermark"
    try:
        assert engine.get_watermark(key) is None
        engine.set_watermark(key, "abc123")
        assert engine.get_watermark(key) == "abc123"
        with pytest.raises(RuntimeError), session_scope() as session:
            engine.set_watermark(key, "rolled-back", session=session)
            raise RuntimeError("rollback")
        assert engine.get_watermark(key) == "abc123"
    finally:
        with session_scope() as session:
            row = session.get(models().Meta, f"watermark:{key}")
            if row is not None:
                session.delete(row)


def test_apply_schema_is_idempotent_and_current(db):
    engine.apply_schema()
    engine.apply_schema()
    assert engine.recorded_schema_version() == engine.SCHEMA_VERSION


def test_wait_for_database_returns_promptly_when_it_is_up(db):
    engine.wait_for_database(timeout_s=5, interval_s=0.2)


def test_connection_yields_an_orm_session(db):
    from git_synapse.db.orm import session_scope

    with session_scope() as session:
        assert isinstance(session, Session)
        assert session.get(models().Meta, "schema_version") is not None


def test_declarative_schema_is_typed_and_complete():
    from sqlalchemy import BigInteger, Boolean, DateTime
    from sqlalchemy.dialects.postgresql import JSONB

    from git_synapse.db.schema import Base, metadata

    assert engine.SCHEMA_VERSION == 34
    assert Base.metadata is metadata
    assert len(metadata.tables) == 33
    assert isinstance(models().Repo.__table__.c.languages.type, JSONB)
    assert isinstance(models().Repo.__table__.c.is_private.type, Boolean)
    assert isinstance(models().Commit.__table__.c.committed_at.type, DateTime)
    assert isinstance(models().File.__table__.c.change_count.type, BigInteger)


def test_model_values_round_trip_through_the_orm(db):
    Meta = models().Meta
    with session_scope() as session:
        session.add(Meta(key="test-value", value={"at": datetime.now(UTC).isoformat()}))
    try:
        with session_scope() as session:
            assert session.get(Meta, "test-value").value["at"]
    finally:
        with session_scope() as session:
            row = session.get(Meta, "test-value")
            if row is not None:
                session.delete(row)



def test_a_deadlock_is_retryable_and_an_ordinary_failure_is_not():
    """The retry exists for concurrent `create_all` calls racing each other on startup."""
    from sqlalchemy.exc import OperationalError

    deadlock = OperationalError("SELECT 1", {}, Exception("deadlock detected"))
    assert engine._is_schema_retryable(deadlock) is True

    wrapped = RuntimeError("apply failed")
    wrapped.__cause__ = OperationalError("x", {}, Exception("could not obtain lock"))
    assert engine._is_schema_retryable(wrapped) is True

    assert engine._is_schema_retryable(RuntimeError("disk full")) is False
    assert engine._is_schema_retryable(
        OperationalError("x", {}, Exception("syntax error"))) is False


def test_forcing_the_schema_rewrites_the_recorded_version(db):
    """`force` is the only way the create path runs on a database that already has the schema, which is every deployment after the first boot."""
    engine.apply_schema(force=True)
    assert engine.recorded_schema_version() == engine.SCHEMA_VERSION
    assert engine.schema_drift() == 0


def test_a_blocked_schema_apply_is_retried_then_succeeds(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from git_synapse.db import schema as schema_mod

    calls = []
    real = schema_mod.metadata.create_all

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("create", {}, Exception("deadlock detected"))
        return real(*a, **kw)

    monkeypatch.setattr(schema_mod.metadata, "create_all", flaky)
    monkeypatch.setattr(engine.time, "sleep", lambda _s: None)

    engine.apply_schema(force=True)
    assert len(calls) == 2


def test_a_schema_apply_that_stays_blocked_gives_up_and_says_so(db, monkeypatch):
    """Five deadlocks in a row is not a lock to wait longer for; it is a database nobody is going to be able to migrate without looking."""
    from sqlalchemy.exc import OperationalError

    from git_synapse.db import schema as schema_mod

    def always_blocked(*a, **kw):
        raise OperationalError("create", {}, Exception("deadlock detected"))

    monkeypatch.setattr(schema_mod.metadata, "create_all", always_blocked)
    monkeypatch.setattr(engine.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="could not apply schema after"):
        engine.apply_schema(force=True)


def test_an_unretryable_schema_failure_is_raised_immediately(db, monkeypatch):
    from git_synapse.db import schema as schema_mod

    def broken(*a, **kw):
        raise RuntimeError("permission denied for schema public")

    monkeypatch.setattr(schema_mod.metadata, "create_all", broken)
    with pytest.raises(RuntimeError, match="permission denied"):
        engine.apply_schema(force=True)


def test_a_database_ahead_of_this_process_is_reported_as_drift(db, monkeypatch):
    """Running older code than the database was migrated to: every write would fail against a constraint this process does not know about."""
    monkeypatch.setattr(
        engine, "recorded_schema_version", lambda: engine.SCHEMA_VERSION + 2)
    assert engine.schema_drift() == 2
    # Ahead, so the create path is skipped and the mismatch only logged.
    engine.apply_schema()


def test_an_unreadable_meta_table_reads_as_no_version_yet(monkeypatch):
    """Called before the table exists, so a failure here means "not bootstrapped" rather than an error worth propagating."""
    def boom():
        raise RuntimeError("relation \"meta\" does not exist")

    monkeypatch.setattr(engine, "session_scope", boom)
    assert engine.recorded_schema_version() is None


def test_an_unreachable_database_names_where_it_looked(monkeypatch):
    """The message has to carry host and port: "unreachable" alone sends someone to the wrong machine."""
    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(engine, "session_scope", boom)
    monkeypatch.setattr(engine.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="unreachable after"):
        engine.wait_for_database(timeout_s=0.05, interval_s=0.01)


def test_a_database_with_no_recorded_version_has_one_inserted(db):
    """The first boot: `create_all` runs and the version row is written rather than updated."""
    with session_scope() as session:
        row = session.get(models().Meta, "schema_version")
        if row is not None:
            session.delete(row)
    assert engine.recorded_schema_version() is None

    engine.apply_schema()
    assert engine.recorded_schema_version() == engine.SCHEMA_VERSION



def test_a_default_that_is_neither_a_keyword_nor_a_number_is_kept_verbatim():
    """The vocabulary is small on purpose."""
    from git_synapse.db import schema

    assert schema._python_default("TRUE") is True
    assert schema._python_default("FALSE") is False
    assert schema._python_default("42") == 42
    assert schema._python_default("'M'") == "M"
    # Not a keyword, not an integer: passed through as written.
    assert schema._python_default("nextval('seq')") == "nextval('seq')"
    assert schema._python_default("uuid_generate_v4()") == "uuid_generate_v4()"
