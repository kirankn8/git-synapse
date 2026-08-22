"""Shared fixtures. Integration tests skip cleanly when no database is present."""

from __future__ import annotations

import os

import pytest

# Point the integration tests at the compose stack's published port unless the
# environment already says otherwise.
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "55432")


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
