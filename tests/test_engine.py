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
    assert engine.schema_is_current()


def test_wait_for_database_returns_promptly_when_it_is_up(db):
    engine.wait_for_database(timeout_s=5, interval_s=0.2)


def test_connection_yields_an_orm_session(db):
    from git_synapse.db.engine import connection

    with connection() as session:
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
