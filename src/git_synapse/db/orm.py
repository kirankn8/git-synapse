"""SQLAlchemy ORM access for Git Synapse."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from git_synapse.config import get_config
from git_synapse.db.schema import MODEL_CLASSES

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_lock = threading.Lock()


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    global _engine
    if _engine is None:
        cfg = get_config().db
        _engine = create_engine(
            cfg.url,
            echo=cfg.echo,
            pool_size=cfg.pool_size,
            max_overflow=cfg.pool_max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return _engine


def models() -> Any:
    """Return the explicit declarative model namespace."""
    return MODEL_CLASSES


def session_factory() -> sessionmaker[Session]:
    """Return the configured ORM session factory."""
    global _session_factory
    if _session_factory is None:
        with _lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=get_engine(),
                    autoflush=False,
                    expire_on_commit=False,
                )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a transaction-scoped ORM session.

    A clean block commits once. Any exception rolls the transaction back and
    is re-raised, so callers cannot accidentally return a partially written
    user/account/settings operation to the pool.
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def close() -> None:
    """Dispose ORM resources, primarily for process shutdown and tests."""
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
