"""Runtime configuration, sourced from environment variables."""

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
    echo: bool = False

    @property
    def url(self) -> str:
        """SQLAlchemy URL using the pure-Python pg8000 driver."""
        return (
            f"postgresql+pg8000://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )



_GITHUB_TOKEN_PREFIXES = ("ghu_", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_")


@dataclass(frozen=True)
class HostCredential:
    """What may be presented to one host, and where that host answers."""

    token: str = ""
    token_file: str = ""
    #: Basic auth carries a username beside the secret; token schemes do not.
    user: str = ""
    #: The endpoint, where it is not the provider's public one.
    api_url: str = ""
    token_prefixes: tuple[str, ...] = ()

    def current_token(self) -> str:
        """The freshest credential available: the file if usable, else the value."""
        path = Path(self.token_file) if self.token_file else None
        if path is not None:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value and self._plausible(value):
                return value
        return self.token

    def _plausible(self, value: str) -> bool:
        if not self.token_prefixes:
            return True
        return len(value) >= 20 and value.startswith(self.token_prefixes)


@dataclass(frozen=True)
class SelectionConfig:
    """Which of an owner's repositories to take."""

    #: Include repositories the credential can see but that are private.
    include_private: bool = True
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
    max_files_per_commit: int = field(
        default_factory=lambda: _env_int("MAX_FILES_PER_COMMIT", 60)
    )
    min_pair_support: int = field(default_factory=lambda: _env_int("MIN_PAIR_SUPPORT", 2))
    #: Ignored for blobless mirrors, which can only detect exact renames.
    rename_similarity: int = field(default_factory=lambda: _env_int("RENAME_SIMILARITY", 50))
    blobless_threshold_kb: int = field(
        default_factory=lambda: _env_int("BLOBLESS_THRESHOLD_KB", 2 * 1024 * 1024)
    )
    force_blobless: bool = field(default_factory=lambda: _env_bool("FORCE_BLOBLESS", False))
    #: Hard ceiling on git operation runtime, in seconds.
    git_timeout: int = field(default_factory=lambda: _env_int("GIT_TIMEOUT", 3600))


@dataclass(frozen=True)
class AnalysisConfig:
    """Tuning for the aggregation and scoring passes."""

    score_batch_size: int = 200_000
    #: Half-life in days for recency weighting, when a caller asks for it.
    recency_half_life_days: int = field(
        default_factory=lambda: _env_int("RECENCY_HALF_LIFE_DAYS", 365)
    )
    default_limit: int = 50
    max_limit: int = 1000


DEFAULT_REFRESH_CRON = "0 * * * *"


@dataclass(frozen=True)
class ScheduleConfig:
    """When refreshes run."""

    enabled: bool = field(default_factory=lambda: _env_bool("SCHEDULER_ENABLED", True))
    cron: str = field(default_factory=lambda: _env_str("REFRESH_CRON", DEFAULT_REFRESH_CRON))
    #: Slower pass that re-discovers the organisation from the GitHub API.
    discover_cron: str = field(default_factory=lambda: _env_str("DISCOVER_CRON", "0 3 * * *"))
    timezone: str = field(default_factory=lambda: _env_str("SCHEDULER_TZ", "UTC"))
    run_on_start: bool = field(default_factory=lambda: _env_bool("REFRESH_ON_START", False))



def live_cron(which: str = "refresh") -> str:
    """The schedule actually in force: a stored override, else the environment."""
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

    #: Set both to require a sign-in. Neither set means the deployment is open.
    admin_email: str = field(default_factory=lambda: _env_str("ADMIN_EMAIL", ""))
    admin_password: str = field(default_factory=lambda: _env_str("ADMIN_PASSWORD", ""))


@dataclass(frozen=True)
class ProviderConfig:
    """The deployment-wide credential for each host. GitHub is one of them."""

    github: HostCredential = field(default_factory=lambda: HostCredential(
        token=_env_str("GITHUB_TOKEN", ""),
        token_file=_env_str("GITHUB_TOKEN_FILE", "/run/git-synapse/github-token"),
        api_url=_env_str("GITHUB_API_URL", "https://api.github.com"),
        token_prefixes=_GITHUB_TOKEN_PREFIXES,
    ))
    gitlab: HostCredential = field(default_factory=lambda: HostCredential(
        token=_env_str("GITLAB_TOKEN", ""),
    ))
    #: Bitbucket app passwords are basic auth, so the username is half of it.
    bitbucket: HostCredential = field(default_factory=lambda: HostCredential(
        token=_env_str("BITBUCKET_TOKEN", ""),
        user=_env_str("BITBUCKET_USER", ""),
    ))

    def for_host(self, provider: str) -> HostCredential:
        """This deployment's credential for one host."""
        return {
            "github": self.github,
            "gitlab": self.gitlab,
            "bitbucket": self.bitbucket,
        }.get(provider, HostCredential())

    def token_for(self, provider: str) -> str:
        """The credential to present to one host, or "" where there is none."""
        return self.for_host(provider).current_token()


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
