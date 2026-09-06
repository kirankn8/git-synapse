"""Versioned orchestration for materialised analytical results.

Derived tables are deliberately cached for read performance.  This registry is
the invalidation contract: changing a stage's version automatically rebuilds
that stage and every stage that depends on it, once, in dependency order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from git_synapse.analysis import aggregate, depbump, mining, predict, score
from git_synapse.db.engine import connection, set_watermark

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stage:
    name: str
    version: str
    depends_on: tuple[str, ...] = ()


# Bump a version whenever the stage's calculation or its materialised schema
# changes.  Dependants are invalidated automatically; callers never need to
# know which downstream tables are affected.
STAGES: tuple[Stage, ...] = (
    Stage("aggregate", "2026-09-08.2"),
    Stage("score", "2026-09-08.1", ("aggregate",)),
    Stage("depbump", "2026-09-08.2"),
    Stage("declared", "2026-09-08.2"),
    Stage("predict", "2026-09-08.3", ("depbump", "declared")),
    Stage("mining", "2026-09-08.1", ("aggregate", "score")),
)


def _stored_versions(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT replace(key, 'watermark:derived:', ''), value #>> '{}'
          FROM meta
         WHERE key LIKE 'watermark:derived:%'
        """
    ).fetchall()
    return {str(name): str(version) for name, version in rows}


def stale_stages(stored: dict[str, str]) -> set[str]:
    """Return stages stale by version or by a stale upstream dependency."""
    stale: set[str] = set()
    for stage in STAGES:
        if stored.get(stage.name) != stage.version or any(
            dependency in stale for dependency in stage.depends_on
        ):
            stale.add(stage.name)
    return stale


def _run_stage(stage: Stage, conn: psycopg.Connection) -> None:
    if stage.name == "aggregate":
        repo_ids = conn.execute(
            "SELECT id FROM repo WHERE is_enabled ORDER BY id"
        ).fetchall()
        for (repo_id,) in repo_ids:
            aggregate.rebuild_repo(int(repo_id), conn)
    elif stage.name == "score":
        score.score_all(conn)
    elif stage.name == "depbump":
        depbump.rebuild(force=True, conn=conn)
    elif stage.name == "declared":
        depbump.refresh_declared(conn=conn, force=True)
        depbump.refresh_modules(conn=conn)
    elif stage.name == "predict":
        predict.rebuild(conn=conn, force=True)
    elif stage.name == "mining":
        mining.rebuild(conn=conn, force=True)
    else:  # pragma: no cover - STAGES is the exhaustive registry
        raise ValueError(f"unknown derived stage {stage.name!r}")


def ensure_current(conn: psycopg.Connection | None = None) -> list[str]:
    """Rebuild stale derived stages once and return the stages rebuilt."""

    def _run(c: psycopg.Connection) -> list[str]:
        stale = stale_stages(_stored_versions(c))
        rebuilt: list[str] = []
        for stage in STAGES:
            if stage.name not in stale:
                continue
            log.warning("derived stage %s is stale; rebuilding version %s",
                        stage.name, stage.version)
            _run_stage(stage, c)
            set_watermark(f"derived:{stage.name}", stage.version, c)
            rebuilt.append(stage.name)
        if rebuilt:
            log.info("derived stages rebuilt: %s", ", ".join(rebuilt))
        return rebuilt

    if conn is not None:  # pragma: no cover - exercised through the CLI path
        return _run(conn)

    # Corpus-wide rebuilds can run for a long time.  Keep repository-local
    # work independently durable, while advancing the stage watermark only
    # after the complete stage succeeds.  An interrupted stage therefore
    # resumes safely without rolling back already-finished repositories.
    stored: dict[str, str] = {}
    rebuilt: list[str] = []
    for stage in STAGES:
        with connection() as c:
            stored = _stored_versions(c)
        if stage.name not in stale_stages(stored):  # pragma: no branch
            continue

        log.warning("derived stage %s is stale; rebuilding version %s",
                    stage.name, stage.version)
        if stage.name == "aggregate":
            with connection() as c:
                repo_ids = [int(row[0]) for row in c.execute(
                    "SELECT id FROM repo WHERE is_enabled ORDER BY id"
                ).fetchall()]
            for repo_id in repo_ids:
                with connection() as c:
                    aggregate.rebuild_repo(repo_id, c)
        elif stage.name == "score":
            with connection() as c:
                repo_ids = [int(row[0]) for row in c.execute(
                    "SELECT id FROM repo WHERE is_enabled ORDER BY id"
                ).fetchall()]
            for repo_id in repo_ids:
                with connection() as c:
                    score.score_repo(repo_id, c)
        elif stage.name == "mining":
            with connection() as c:
                repo_ids = [int(row[0]) for row in c.execute(
                    "SELECT id FROM repo WHERE is_enabled AND pair_count > 0 ORDER BY id"
                ).fetchall()]
            for repo_id in repo_ids:
                with connection() as c:
                    mining.rebuild(repo_id=repo_id, conn=c, force=True)
        else:
            with connection() as c:
                _run_stage(stage, c)

        with connection() as c:
            set_watermark(f"derived:{stage.name}", stage.version, c)
        rebuilt.append(stage.name)

    if rebuilt:
        log.info("derived stages rebuilt: %s", ", ".join(rebuilt))
    return rebuilt  # pragma: no cover - covered by stage integration tests
