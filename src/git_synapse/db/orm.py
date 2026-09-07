"""SQLAlchemy ORM access to the existing Git Synapse schema.

The schema is deliberately owned by ``schema.sql`` because it contains the
database-specific extensions, indexes, checks, and historical upgrades needed
by an existing corpus.  This module maps that schema at runtime instead of
re-declaring it and accidentally creating a second, subtly different schema.

Application code should use :func:`session_scope` and the mapped classes from
:func:`models` for ordinary reads and writes.  The ingest and analytics
modules may still use the lower-level engine for COPY and set-based operations;
those are infrastructure paths, not an alternative CRUD API.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.automap import AutomapBase, automap_base
from sqlalchemy.orm import Session, sessionmaker

from git_synapse.config import get_config

_engine: Engine | None = None
_base: AutomapBase | None = None
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
    """Reflect and return ORM classes for every existing application table.

    Reflection is deferred until the first use, which lets normal startup
    apply ``schema.sql`` before SQLAlchemy asks PostgreSQL for table metadata.
    The returned namespace exposes stable CamelCase names, for example
    ``models().Repo`` and ``models().AppUser``.
    """
    global _base
    if _base is None:
        with _lock:
            if _base is None:
                def class_name_for_table(_base: Any, table_name: str, _table: Any) -> str:
                    return "".join(part.capitalize() for part in table_name.split("_"))

                base = automap_base()
                base.prepare(
                    autoload_with=get_engine(),
                    classname_for_table=class_name_for_table,
                )
                _base = base
    return _base.classes


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
    global _engine, _base, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _base = None
        _session_factory = None
