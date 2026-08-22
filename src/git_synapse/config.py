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


@dataclass(frozen=True)
class GitHubConfig:
    """Which repositories to mirror, and how to reach GitHub."""

    token: str = field(default_factory=lambda: _env_str("GITHUB_TOKEN", ""))
    org: str = field(default_factory=lambda: _env_str("GITHUB_ORG", "acme"))
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
    #: Default 2 GiB, which in the acme org fully clones 269 of 270
    #: repositories and spares only the 9.8 GB documentation monorepo.
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
class CrossRepoConfig:
    """Tuning for cross-repository coupling.

    Within a repo, "changed together" means "same commit". Across repos that is
    impossible, so commits are grouped into *change sets* -- see the CROSS-
    REPOSITORY COUPLING section of ``schema.sql`` for the full rationale.
    """

    enabled: bool = field(default_factory=lambda: _env_bool("CROSSREPO_ENABLED", True))
    #: Regex for an issue key in a commit subject. The default matches the
    #: JIRA-style keys used across this org (ACME-2330, ACME-11803).
    ticket_pattern: str = field(
        default_factory=lambda: _env_str("TICKET_PATTERN", r"([A-Z][A-Z0-9]{1,9}-[0-9]{1,6})")
    )
    #: Commits by one author with no larger gap than this form one work session.
    #: Four hours approximates a working block without merging a whole day.
    session_gap_hours: int = field(default_factory=lambda: _env_int("SESSION_GAP_HOURS", 4))
    #: Change sets touching more repos than this are recorded but excluded from
    #: pairing. An org-wide dependabot sweep across 61 repos carries no design
    #: signal and would contribute O(k^2) repo pairs on its own.
    max_repos_per_changeset: int = field(
        default_factory=lambda: _env_int("MAX_REPOS_PER_CHANGESET", 8)
    )
    #: Per change set, per repo, cap on files considered for *file-level*
    #: cross-repo pairing. Uncapped this is O(files_a * files_b) per change set.
    #: A safety valve against a sprawling change set, not a routine filter: at 200
    #: it clips 5 of 24,994 (change set, repo) groups on this org for 3.5M
    #: intermediate instances, where 25 clipped 1,211 of them and dropped ~16,800
    #: real pairs. The cap also defines the population the marginals are counted
    #: over, so lowering it narrows coverage rather than biasing the scores.
    max_files_per_repo_per_changeset: int = field(
        default_factory=lambda: _env_int("MAX_FILES_PER_REPO_PER_CHANGESET", 200)
    )
    #: Minimum shared change sets before a cross-repo pair is persisted.
    min_support: int = field(default_factory=lambda: _env_int("MIN_XREPO_SUPPORT", 2))
    #: Confidence floor for a hop when following transitive chains.
    chain_min_confidence: float = field(
        default_factory=lambda: float(_env_str("CHAIN_MIN_CONFIDENCE", "0.15"))
    )
    #: Maximum hops when following chains (A -> B -> C is depth 2).
    chain_max_depth: int = field(default_factory=lambda: _env_int("CHAIN_MAX_DEPTH", 3))
    #: Time-bin width for the directed lagged analysis. Smaller bins sharpen
    #: direction but reduce the number of observed co-occurrences. Six hours is
    #: the validated setting: at 24h, directional accuracy was 0.64, and the
    #: motivating case (34 minutes between two repos) was invisible.
    lag_bin_hours: int = field(default_factory=lambda: _env_int("LAG_BIN_HOURS", 6))
    #: Minimum joint count before a lagged ordered pair is persisted. Kept at 1
    #: because impact prediction needs a score for every declared-dependency
    #: candidate; a floor of 5 left 30% of them unscored, which cost AUC.
    lag_min_support: int = field(default_factory=lambda: _env_int("LAG_MIN_SUPPORT", 1))


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
    crossrepo: CrossRepoConfig = field(default_factory=CrossRepoConfig)
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
