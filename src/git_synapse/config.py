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
    #: SQL echoed to the log. A debugging aid, set in a debugger, not a
    #: deployment: LOG_LEVEL=DEBUG is what an operator reaches for.
    echo: bool = False

    @property
    def url(self) -> str:
        """SQLAlchemy URL using the pure-Python pg8000 driver."""
        return (
            f"postgresql+pg8000://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )



#: GitHub credential prefixes. Used to reject a partially written token file
#: rather than send half a credential and get an indistinguishable 401 back.
_TOKEN_PREFIXES = ("ghu_", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_")


def _looks_like_token(value: str) -> bool:
    """True if the value has the shape of a GitHub token."""
    return len(value) >= 20 and value.startswith(_TOKEN_PREFIXES)


@dataclass(frozen=True)
class GitHubConfig:
    """How to reach GitHub: a credential and an endpoint.

    Which repositories to take is not here. That question is the same on every
    host, and lives in :class:`SelectionConfig`.
    """

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


@dataclass(frozen=True)
class SelectionConfig:
    """Which of an owner's repositories to take.

    Nothing here is about a particular host: GitHub, GitLab and Bitbucket all
    answer the same questions, and `select_repos` asks them of whatever the
    provider listed. Every value is carried per account and edited on the
    Accounts page; these are only what a newly created one starts from.
    """

    #: Include repositories the credential can see but that are private.
    include_private: bool = True
    #: Off, and not a question a deployment is asked: `select_repos` drops a
    #: fork only when the repository it was forked from is also in the corpus,
    #: which is the only case where anything is duplicated. This remains as the
    #: explicit override -- take every fork, duplicate or not.
    include_forks: bool = False
    include_archived: bool = True
    #: When set, restricts ingestion to exactly these repo names.
    only_repos: tuple[str, ...] = ()
    #: Repo names to skip regardless of the other filters.
    skip_repos: tuple[str, ...] = ()


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
    #: Pairs seen fewer times than this are not persisted. The long tail of
    #: one-off pairs is both enormous and statistically meaningless.
    min_pair_support: int = field(default_factory=lambda: _env_int("MIN_PAIR_SUPPORT", 2))
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
    #: 31 association measures depend only on which paths co-occur in a commit,
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

    #: Pair rows pulled into memory per vectorised scoring batch. Sized
    #: against this process's own memory, not against anything a deployment
    #: knows better than the code does.
    score_batch_size: int = 200_000
    #: Half-life in days for recency weighting, when a caller asks for it.
    recency_half_life_days: int = field(
        default_factory=lambda: _env_int("RECENCY_HALF_LIFE_DAYS", 365)
    )
    #: Row caps for coupling queries. Every endpoint takes its own ``limit``,
    #: so these are the API's shape rather than a deployment's choice.
    default_limit: int = 50
    max_limit: int = 1000


#: Hourly. A refresh re-fetches every mirror and rewrites the pair tables; on a
#: 163-repository corpus that measured 683-1662s, so a quarter-hourly tick spent
#: most of its time overlapping itself to find a handful of commits. Set
#: REFRESH_CRON to go faster; nothing here assumes the interval.
DEFAULT_REFRESH_CRON = "0 * * * *"


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
    #: Fast refresh of already-known repositories. Five-field cron, in
    #: ``timezone``. This is the *seed*: a value stored from the UI overrides it,
    #: the way GITHUB_ORG seeds the account table. Read the value in force with
    #: :func:`live_cron`, never straight off this field.
    cron: str = field(default_factory=lambda: _env_str("REFRESH_CRON", DEFAULT_REFRESH_CRON))
    #: Slower pass that re-discovers the organisation from the GitHub API.
    discover_cron: str = field(default_factory=lambda: _env_str("DISCOVER_CRON", "0 3 * * *"))
    timezone: str = field(default_factory=lambda: _env_str("SCHEDULER_TZ", "UTC"))
    #: Kick off a refresh as soon as the scheduler boots, rather than waiting
    #: for the first cron tick. Useful on a fresh deployment.
    run_on_start: bool = field(default_factory=lambda: _env_bool("REFRESH_ON_START", False))



def live_cron(which: str = "refresh") -> str:
    """The schedule actually in force: a stored override, else the environment.

    Imported lazily so ``config`` keeps no dependency on the database -- it is
    read during startup, before a connection exists.
    """
    from git_synapse.analysis import settings

    cfg = get_config().schedule
    if which == "discover":
        return settings.effective("discover_cron", cfg.discover_cron)
    return settings.effective("refresh_cron", cfg.cron)


@dataclass(frozen=True)
class ServerConfig:
    """HTTP surface."""

    host: str = field(default_factory=lambda: _env_str("API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("API_PORT", 8000))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_list("CORS_ORIGINS", ("*",))
    )
    web_root: Path = field(default_factory=lambda: Path(_env_str("WEB_ROOT", "/app/web")))

    #: Presented once, to create the first administrator. Left empty for an
    #: interactive deployment, where one is minted and written to the log:
    #: putting a password in the environment puts it in the compose file, the
    #: shell history and every process listing on the host. An automated
    #: deployment that needs to claim the account without reading a log sets
    #: this to a value it already holds in a secret store.
    admin_setup_token: str = field(
        default_factory=lambda: _env_str("ADMIN_SETUP_TOKEN", "")
    )


@dataclass(frozen=True)
class ProviderConfig:
    """The deployment-wide credential for each host, GitHub included.

    All optional. A public repository on any host clones and ingests with no
    credential at all -- these only widen what is visible and lift the
    anonymous rate limit, which is the same bargain every one of them makes.

    GitHub sits here beside the others rather than in a class of its own: a
    credential for a host is one kind of thing however early that host was
    supported, and asking "what may we use against this host?" should be one
    lookup rather than a branch per vendor.
    """

    github: GitHubConfig = field(default_factory=GitHubConfig)
    gitlab_token: str = field(default_factory=lambda: _env_str("GITLAB_TOKEN", ""))
    #: Bitbucket app passwords are basic auth, so they need the username too.
    bitbucket_user: str = field(default_factory=lambda: _env_str("BITBUCKET_USER", ""))
    bitbucket_token: str = field(default_factory=lambda: _env_str("BITBUCKET_TOKEN", ""))

    def token_for(self, provider: str) -> str:
        """The deployment-wide credential for one host, or "" when it has none.

        A source carries its own credential where somebody has pasted one; this
        is the fallback behind it, and the answer to "is this host reachable at
        all beyond the anonymous rate limit?"
        """
        if provider == "github":
            return self.github.current_token()
        if provider == "gitlab":
            return self.gitlab_token
        if provider == "bitbucket":
            return self.bitbucket_token
        return ""


@dataclass(frozen=True)
class Config:
    """Top-level configuration aggregate."""

    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    #: Credentials, one entry per host. Reached as ``cfg.providers.github``.
    providers: ProviderConfig = field(default_factory=ProviderConfig)
    #: Which repositories an account takes, on any host.
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
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
