"""Shared fixtures. Integration tests skip cleanly when no database is present."""

from __future__ import annotations

import os

import pytest

# Point the integration tests at the compose stack's published port unless the
# environment already says otherwise.
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "55432")


@pytest.fixture(scope="module")
def scratch_db(db):
    """A throwaway database for tests that write.

    Module-scoped, not session-scoped: switching ``POSTGRES_DB`` is process-wide,
    so a session-scoped switch would point the tests that read the real corpus at
    an empty database for the rest of the run.

    Tests that create fixture repositories or call a global ``rebuild()`` used to
    run against whatever ``POSTGRES_DB`` pointed at, which in practice was the
    production corpus: those rebuilds TRUNCATE shared tables, so running the
    suite silently replaced real analysis results and left orphan rows behind.
    """
    import psycopg

    from git_synapse.config import get_config, reset_config_cache
    from git_synapse.db.engine import apply_schema, close_pool

    cfg = get_config().db
    name = f"{cfg.database}_test"
    maintenance = (
        f"host={cfg.host} port={cfg.port} user={cfg.user} "
        f"password={cfg.password} dbname=postgres"
    )
    with psycopg.connect(maintenance, autocommit=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone():
            conn.execute(f'CREATE DATABASE "{name}"')

    previous = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = name
    reset_config_cache()
    close_pool()
    apply_schema()
    try:
        yield name
    finally:
        close_pool()
        if previous is None:
            os.environ.pop("POSTGRES_DB", None)
        else:
            os.environ["POSTGRES_DB"] = previous
        reset_config_cache()


@pytest.fixture(scope="session")
def db():
    """Yield a working database, or skip the test when none is reachable."""
    from git_synapse.db.engine import apply_schema, wait_for_database

    try:
        wait_for_database(timeout_s=5, interval_s=0.5)
    except Exception as exc:  # noqa: BLE001 - any failure means "no database"
        pytest.skip(f"no database available: {exc}")
    apply_schema()
    return True
