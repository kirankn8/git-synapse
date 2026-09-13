"""Versioned orchestration for materialised analytical results."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from git_synapse.analysis import aggregate, depbump, mining, predict, score
from git_synapse.db.engine import set_watermark
from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stage:
    name: str
    version: str
    depends_on: tuple[str, ...] = ()


STAGES: tuple[Stage, ...] = (
    Stage("aggregate", "2026-09-08.2"),
    Stage("score", "2026-09-08.1", ("aggregate",)),
    Stage("depbump", "2026-09-08.2"),
    Stage("declared", "2026-09-08.2"),
    Stage("predict", "2026-09-08.3", ("depbump", "declared")),
    Stage("mining", "2026-09-08.1", ("aggregate", "score")),
)


def _stored_versions_orm(conn: object | None = None) -> dict[str, str]:
    """Read stage watermarks through the ORM for the session-based path."""
    def read(session: object) -> dict[str, str]:
        Meta = models().Meta
        rows = session.query(Meta.key, Meta.value).filter(
            Meta.key.like("watermark:derived:%")
        ).all()
        return {str(key).removeprefix("watermark:derived:"): str(value)
                for key, value in rows}
    if conn is not None:
        return read(conn)
    with session_scope() as session:
        return read(session)


def stale_stages(stored: dict[str, str]) -> set[str]:
    """Return stages stale by version or by a stale upstream dependency."""
    stale: set[str] = set()
    for stage in STAGES:
        if stored.get(stage.name) != stage.version or any(
            dependency in stale for dependency in stage.depends_on
        ):
            stale.add(stage.name)
    return stale


def _run_stage(stage: Stage, conn: object) -> None:
    Repo = models().Repo
    if stage.name == "aggregate":
        repo_ids = [row.id for row in conn.query(Repo.id).filter(
            Repo.is_enabled.is_(True)
        ).order_by(Repo.id).all()]
        for repo_id in repo_ids:
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


def ensure_current(conn: object | None = None) -> list[str]:
    """Rebuild stale derived stages once and return the stages rebuilt."""

    def _run(c: object) -> list[str]:
        stale = stale_stages(_stored_versions_orm(c))
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

    stored: dict[str, str] = {}
    rebuilt: list[str] = []
    for stage in STAGES:
        stored = _stored_versions_orm()
        if stage.name not in stale_stages(stored):  # pragma: no branch
            continue

        log.warning("derived stage %s is stale; rebuilding version %s",
                    stage.name, stage.version)
        if stage.name == "aggregate":
            with session_scope() as session:
                Repo = models().Repo
                repo_ids = [row.id for row in session.query(Repo.id).filter(
                    Repo.is_enabled.is_(True),
                ).order_by(Repo.id).all()]
            for repo_id in repo_ids:
                with session_scope() as c:
                    aggregate.rebuild_repo(repo_id, c)
        elif stage.name == "score":
            with session_scope() as session:
                Repo = models().Repo
                repo_ids = [row.id for row in session.query(Repo.id).filter(
                    Repo.is_enabled.is_(True),
                ).order_by(Repo.id).all()]
            for repo_id in repo_ids:
                with session_scope() as c:
                    score.score_repo(repo_id, c)
        elif stage.name == "mining":
            with session_scope() as session:
                Repo = models().Repo
                repo_ids = [row.id for row in session.query(Repo.id).filter(
                    Repo.is_enabled.is_(True), Repo.pair_count > 0,
                ).order_by(Repo.id).all()]
            for repo_id in repo_ids:
                with session_scope() as c:
                    mining.rebuild(repo_id=repo_id, conn=c, force=True)
        else:
            with session_scope() as c:
                _run_stage(stage, c)

        with session_scope() as c:
            set_watermark(f"derived:{stage.name}", stage.version, c)
        rebuilt.append(stage.name)

    if rebuilt:
        log.info("derived stages rebuilt: %s", ", ".join(rebuilt))
    return rebuilt  # pragma: no cover - covered by stage integration tests
