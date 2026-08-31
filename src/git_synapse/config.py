"""Runtime configuration, sourced from environment variables.

Every knob the system exposes lives here so that a deployment can be retargeted
entirely through the environment -- no code edits, no rebuilt image. Defaults are
chosen to work out of the box with the bundled docker compose stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL connection settings."""

    host: str = field(default_factory=lambda: _env_str("POSTGRES_HOST", "postgres"))
    port: int = field(default_factory=lambda: _env_int("POSTGRES_PORT", 5432))
    user: str = field(default_factory=lambda: _env_str("POSTGRES_USER", "git_synapse"))
    password: str = field(default_factory=lambda: _env_str("POSTGRES_PASSWORD", "git_synapse"))
    database: str = field(default_factory=lambda: _env_str("POSTGRES_DB", "git_synapse"))
    #: Connections held open by each API/worker process.
    pool_size: int = field(default_factory=lambda: _env_int("DB_POOL_SIZE", 10))
    pool_max_overflow: int = field(default_factory=lambda: _env_int("DB_POOL_OVERFLOW", 20))
    echo: bool = field(default_factory=lambda: _env_bool("DB_ECHO", False))

    @property
    def url(self) -> str:
        """SQLAlchemy URL using the psycopg (v3) driver."""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def dsn(self) -> str:
        """Plain libpq DSN, for psycopg's fast COPY path."""
        return (
            f"host={self.host} port={self.port} user={self.user} "
            f"password={self.password} dbname={self.database}"
        )


#: GitHub credential prefixes. Used to reject a partially written token file
#: rather than send half a credential and get an indistinguishable 401 back.
_TOKEN_PREFIXES = ("ghu_", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_")


def _looks_like_token(value: str) -> bool:
    """True if the value has the shape of a GitHub token."""
    return len(value) >= 20 and value.startswith(_TOKEN_PREFIXES)


@dataclass(frozen=True)
class GitHubConfig:
    """Which repositories to mirror, and how to reach GitHub."""

    token: str = field(default_factory=lambda: _env_str("GITHUB_TOKEN", ""))
    #: A file the host keeps current, read fresh on every use. `gh` issues
    #: short-lived ghu_ credentials, so a token captured into the environment at
    #: container start is expired within hours and every fetch 401s until someone
    #: restarts the container. Reading a file decouples credential lifetime from
    #: container lifetime.
    token_file: str = field(
        default_factory=lambda: _env_str("GITHUB_TOKEN_FILE", "/run/git-synapse/github-token")
    )

    def current_token(self) -> str:
        """The freshest token available: the file if present, else the env var."""
        path = Path(self.token_file) if self.token_file else None
        if path is not None:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            # A half-written file would otherwise be sent to GitHub as a
            # credential and come back 401, which reads as "expired token" and
            # sends someone hunting the wrong problem. An unreadable, empty or
            # malformed file must never blank or corrupt a working env token.
            if value and _looks_like_token(value):
                return value
        return self.token
    #: Legacy single-org setting, seeded into the `account` table on first boot
    #: and ignored thereafter. Empty by default: there is no sensible org to
    #: guess, and a non-empty default cannot be switched off from the
    #: environment because a blank value falls back to it.
    org: str = field(default_factory=lambda: _env_str("GITHUB_ORG", ""))
    api_url: str = field(default_factory=lambda: _env_str("GITHUB_API_URL", "https://api.github.com"))
    #: Include repositories the token can see but that are private.
    include_private: bool = field(default_factory=lambda: _env_bool("INCLUDE_PRIVATE", True))
    include_forks: bool = field(default_factory=lambda: _env_bool("INCLUDE_FORKS", True))
    include_archived: bool = field(default_factory=lambda: _env_bool("INCLUDE_ARCHIVED", True))
    #: When set, restricts ingestion to exactly these repo names.
    only_repos: tuple[str, ...] = field(default_factory=lambda: _env_list("ONLY_REPOS"))
    #: Repo names to skip regardless of the other filters.
    skip_repos: tuple[str, ...] = field(default_factory=lambda: _env_list("SKIP_REPOS"))


@dataclass(frozen=True)
class IngestConfig:
    """Tuning for the clone/parse pipeline."""

    #: Where bare mirrors live. Blobless, so this stays small.
    mirror_root: Path = field(
        default_factory=lambda: Path(_env_str("MIRROR_ROOT", "/data/mirrors"))
    )
    #: Repos cloned/parsed concurrently. Git is I/O bound, so oversubscribe a little.
    concurrency: int = field(default_factory=lambda: _env_int("INGEST_CONCURRENCY", 8))
    #: Commits touching more than this many files are recorded but excluded from
    #: pair generation. Bulk reformats and vendored-dependency bumps would
    #: otherwise dominate every co-occurrence count while carrying no design
    #: signal, and they cost O(k^2) pairs each.
    max_files_per_commit: int = field(
        default_factory=lambda: _env_int("MAX_FILES_PER_COMMIT", 60)
    )
    #: Merge commits duplicate the changes of their parents, so they are skipped
    #: by default. Recorded in the commits table either way.
    include_merges: bool = field(default_factory=lambda: _env_bool("INCLUDE_MERGES", False))
    #: Pairs seen fewer times than this are not persisted. The long tail of
    #: one-off pairs is both enormous and statistically meaningless.
    min_pair_support: int = field(default_factory=lambda: _env_int("MIN_PAIR_SUPPORT", 2))
    #: Rows per batch when streaming commits into Postgres.
    copy_batch_size: int = field(default_factory=lambda: _env_int("COPY_BATCH_SIZE", 20000))
    #: Similarity threshold for git's rename detection, as a percentage. Only
    #: applies to fully-cloned repos: inexact rename detection has to read file
    #: contents, so blobless mirrors are forced to 100 (exact renames only,
    #: which git resolves from blob SHAs without fetching the blobs).
    rename_similarity: int = field(default_factory=lambda: _env_int("RENAME_SIMILARITY", 50))
    #: Repos whose GitHub-reported size exceeds this are mirrored blobless.
    #:
    #: A full clone yields line-level churn (insertions/deletions) and inexact
    #: rename detection, because both need blob contents. A blobless clone
    #: yields neither, but is one to two orders of magnitude smaller. Since the
    #: 29 association measures depend only on which paths co-occur in a commit,
    #: a blobless repo still produces complete and correct coupling statistics
    #: -- it just loses churn as an extra attribute.
    #:
    #: Default 2 GiB, which on a 270-repository org fully clones all but the
    #: largest documentation monorepo.
    blobless_threshold_kb: int = field(
        default_factory=lambda: _env_int("BLOBLESS_THRESHOLD_KB", 2 * 1024 * 1024)
    )
    #: Force every repo blobless regardless of size. Fastest possible ingest,
    #: at the cost of all churn data.
    force_blobless: bool = field(default_factory=lambda: _env_bool("FORCE_BLOBLESS", False))
    #: Hard ceiling on git operation runtime, in seconds.
    git_timeout: int = field(default_factory=lambda: _env_int("GIT_TIMEOUT", 3600))


@dataclass(frozen=True)
class AnalysisConfig:
    """Tuning for the aggregation and scoring passes."""

    #: Pair rows pulled into memory per vectorised scoring batch.
    score_batch_size: int = field(default_factory=lambda: _env_int("SCORE_BATCH_SIZE", 200000))
    #: Half-life in days for recency weighting, when a caller asks for it.
    recency_half_life_days: int = field(
        default_factory=lambda: _env_int("RECENCY_HALF_LIFE_DAYS", 365)
    )
    #: Default row cap for coupling queries.
    default_limit: int = field(default_factory=lambda: _env_int("DEFAULT_QUERY_LIMIT", 50))
    max_limit: int = field(default_factory=lambda: _env_int("MAX_QUERY_LIMIT", 1000))


@dataclass(frozen=True)
class DependencyConfig:
    """Tuning for the cross-repository dependency graph.

    The graph is read from what repositories declare about each other in their
    manifests. It replaced a change-set model that grouped commits by ticket key
    or author session: that inferred relationships from calendar time, and two
    public repositories sharing no code at all scored G2 = 570 against each other
    because both were busy in the same years.
    """

    enabled: bool = field(default_factory=lambda: _env_bool("CROSSREPO_ENABLED", True))
    #: Confidence below which a transitive chain hop is not traversed.
    chain_min_confidence: float = field(
        default_factory=lambda: _env_float("CHAIN_MIN_CONFIDENCE", 0.3)
    )
    #: How many hops a chain may compose before it stops being actionable.
    chain_max_depth: int = field(default_factory=lambda: _env_int("CHAIN_MAX_DEPTH", 3))


@dataclass(frozen=True)
class ScheduleConfig:
    """When refreshes run.

    Two tiers, because the two halves of a run have very different costs:

    * **refresh** fetches known repositories and rebuilds whatever moved. On this
      corpus it takes about a minute, almost all of it git fetches, so it can run
      often.
    * **discover** additionally re-lists the organisation through the GitHub API
      to pick up new, renamed or archived repositories. That is the only part
      that consumes API quota, and new repositories do not appear every quarter
      hour, so it runs on its own slower schedule.
    """

    enabled: bool = field(default_factory=lambda: _env_bool("SCHEDULER_ENABLED", True))
    #: Fast refresh of already-known repositories. Five-field cron, in ``timezone``.
    cron: str = field(default_factory=lambda: _env_str("REFRESH_CRON", "*/15 * * * *"))
    #: Slower pass that re-discovers the organisation from the GitHub API.
    discover_cron: str = field(default_factory=lambda: _env_str("DISCOVER_CRON", "0 3 * * *"))
    timezone: str = field(default_factory=lambda: _env_str("SCHEDULER_TZ", "UTC"))
    #: Kick off a refresh as soon as the scheduler boots, rather than waiting
    #: for the first cron tick. Useful on a fresh deployment.
    run_on_start: bool = field(default_factory=lambda: _env_bool("REFRESH_ON_START", False))


@dataclass(frozen=True)
class ServerConfig:
    """HTTP surface."""

    host: str = field(default_factory=lambda: _env_str("API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("API_PORT", 8000))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_list("CORS_ORIGINS", ("*",))
    )
    web_root: Path = field(default_factory=lambda: Path(_env_str("WEB_ROOT", "/app/web")))


@dataclass(frozen=True)
class Config:
    """Top-level configuration aggregate."""

    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    crossrepo: DependencyConfig = field(default_factory=DependencyConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide configuration singleton, built from the environment once."""
    return Config()


def reset_config_cache() -> None:
    """Drop the cached config. Only used by tests that patch the environment."""
    get_config.cache_clear()
