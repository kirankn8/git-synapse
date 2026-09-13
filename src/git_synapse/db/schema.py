"""Declarative ORM schema for Git Synapse.

Every persisted table is represented by a mapped class in this module.  The
classes are generated from the compact declarations below with SQLAlchemy's
declarative metaclass; they are ordinary mapped classes, not reflected or
automapped tables.  ``Base.metadata`` is the sole schema registry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CHAR, DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for every Git Synapse ORM model."""


metadata = Base.metadata

_types: dict[str, Any] = {
    "BIGINT": BigInteger,
    "INT": Integer,
    "SMALLINT": SmallInteger,
    "TEXT": Text,
    "CHAR": CHAR(1),
    "BOOL": Boolean,
    "FLOAT": DOUBLE_PRECISION,
    "TIMESTAMPTZ": DateTime(timezone=True),
    "JSONB": JSONB,
    "TEXT_ARRAY": ARRAY(Text),
}


def _python_default(value: str) -> Any:
    """Convert the small schema-default vocabulary to ORM defaults."""
    if value == "TRUE":
        return True
    if value == "FALSE":
        return False
    if value == "now()":
        return lambda: datetime.now(UTC)
    if value in {"'{}'::jsonb", "'{}'"}:
        return dict
    if value in {"'[]'::jsonb", "'[]'"}:
        return list
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    try:
        return int(value)
    except ValueError:
        return value


def _model(table_name: str, *specs: str, constraints: tuple[Any, ...] = ()) -> type[Any]:
    """Create one declarative mapped class from a typed column declaration."""
    class_name = "".join(part.capitalize() for part in table_name.split("_"))
    attrs: dict[str, Any] = {
        "__tablename__": table_name,
        "__table_args__": constraints,
        "__annotations__": {},
    }
    for spec in specs:
        parts = spec.split()
        name, type_name = parts[0], parts[1]
        flags = set(parts[2:])
        fk_index = next((i for i, part in enumerate(parts) if part == "fk"), None)
        args: list[Any] = [_types[type_name]]
        if fk_index is not None:
            args.append(ForeignKey(parts[fk_index + 1]))
        kwargs: dict[str, Any] = {
            "nullable": not ("nn" in flags or "pk" in flags),
            "primary_key": "pk" in flags,
            "unique": "u" in flags,
        }
        if "pk" in flags:
            kwargs["nullable"] = False
        default = next((part[8:] for part in parts if part.startswith("default=")), None)
        if default is not None:
            kwargs["default"] = _python_default(default)
        attrs["__annotations__"][name] = Mapped[Any]
        attrs[name] = mapped_column(*args, **kwargs)
    return type(class_name, (Base,), attrs)


account = _model(
    "account", "id BIGINT pk", "login TEXT nn", "kind TEXT nn default='org'",
    "provider TEXT nn default='github'", "host TEXT nn default='github.com'", "api_url TEXT",
    "credential TEXT", "credential_hint TEXT",
    "enabled BOOL nn default=TRUE", "include_private BOOL nn default=TRUE",
    "include_forks BOOL nn default=FALSE", "include_archived BOOL nn default=TRUE",
    "only_repos TEXT_ARRAY nn default='{}'", "skip_repos TEXT_ARRAY nn default='{}'",
    "last_discovered_at TIMESTAMPTZ", "last_discover_error TEXT", "repo_count BIGINT nn default=0",
    "created_at TIMESTAMPTZ nn default=now()", "updated_at TIMESTAMPTZ nn default=now()",
    constraints=(CheckConstraint("kind IN ('org','user','group','workspace','repo')", name="account_kind_check"),),
)

repo = _model(
    "repo", "id BIGINT pk", "github_id BIGINT u", "provider TEXT nn default='github'",
    "host TEXT nn default='github.com'", "owner TEXT nn", "name TEXT nn", "full_name TEXT nn",
    "description TEXT", "homepage TEXT", "html_url TEXT", "clone_url TEXT", "ssh_url TEXT",
    "default_branch TEXT", "primary_language TEXT", "languages JSONB nn default='{}'::jsonb",
    "topics TEXT_ARRAY nn default='{}'", "license_spdx TEXT", "visibility TEXT",
    "is_private BOOL nn default=FALSE", "is_fork BOOL nn default=FALSE", "is_archived BOOL nn default=FALSE",
    "is_template BOOL nn default=FALSE", "is_disabled BOOL nn default=FALSE", "disk_usage_kb BIGINT",
    "stargazers INT nn default=0", "watchers INT nn default=0", "forks_count INT nn default=0",
    "open_issues INT nn default=0", "github_created_at TIMESTAMPTZ", "github_updated_at TIMESTAMPTZ",
    "github_pushed_at TIMESTAMPTZ", "raw_github JSONB nn default='{}'::jsonb", "mirror_path TEXT",
    "clone_mode TEXT nn default='full'", "has_churn BOOL nn default=TRUE", "mirror_size_kb BIGINT",
    "head_sha TEXT", "last_ingested_sha TEXT", "last_ingested_refs JSONB nn default='[]'::jsonb",
    "last_fetch_at TIMESTAMPTZ", "last_ingest_at TIMESTAMPTZ", "last_aggregate_at TIMESTAMPTZ",
    "last_depbump_at TIMESTAMPTZ", "last_depbump_sha TEXT", "last_declared_sha TEXT", "last_aggregate_sha TEXT",
    "last_mining_at TIMESTAMPTZ", "ingest_status TEXT nn default='pending'", "ingest_error TEXT",
    "ingest_duration_s FLOAT", "is_enabled BOOL nn default=TRUE", "commit_count BIGINT nn default=0",
    "pair_population BIGINT nn default=0", "file_count BIGINT nn default=0", "author_count BIGINT nn default=0",
    "pair_count BIGINT nn default=0", "total_insertions BIGINT nn default=0", "total_deletions BIGINT nn default=0",
    "first_commit_at TIMESTAMPTZ", "last_commit_at TIMESTAMPTZ", "created_at TIMESTAMPTZ nn default=now()",
    "updated_at TIMESTAMPTZ nn default=now()",
    "account_id BIGINT fk account.id", constraints=(UniqueConstraint("host", "full_name", name="repo_host_full_name_key"),),
)

author = _model(
    "author", "id BIGINT pk", "email TEXT nn u", "display_name TEXT", "known_names TEXT_ARRAY nn default='{}'",
    "canonical_id BIGINT fk author.id", "commit_count BIGINT nn default=0", "first_commit_at TIMESTAMPTZ",
    "last_commit_at TIMESTAMPTZ", "created_at TIMESTAMPTZ nn default=now()",
)
file = _model(
    "file", "id BIGINT pk", "repo_id BIGINT nn fk repo.id", "path TEXT nn", "dir_path TEXT nn default=''",
    "basename TEXT nn default=''", "extension TEXT", "depth SMALLINT nn default=0", "is_deleted BOOL nn default=FALSE",
    "change_count BIGINT nn default=0", "pair_change_count BIGINT nn default=0", "insertions BIGINT nn default=0",
    "deletions BIGINT nn default=0", "author_count INT nn default=0", "first_change_at TIMESTAMPTZ",
    "last_change_at TIMESTAMPTZ", "created_at TIMESTAMPTZ nn default=now()",
    constraints=(UniqueConstraint("repo_id", "path", name="file_repo_path_key"),),
)
file_alias = _model("file_alias", "repo_id BIGINT nn fk repo.id", "old_path TEXT nn", "file_id BIGINT nn fk file.id", constraints=(PrimaryKeyConstraint("repo_id", "old_path"),))

commit = _model(
    "commit", "id BIGINT pk", "repo_id BIGINT nn fk repo.id", "sha TEXT nn", "author_id BIGINT fk author.id",
    "committer_id BIGINT fk author.id", "authored_at TIMESTAMPTZ nn", "committed_at TIMESTAMPTZ nn",
    "subject TEXT nn default=''", "body TEXT", "parent_count SMALLINT nn default=0", "is_merge BOOL nn default=FALSE",
    "n_files INT nn default=0", "insertions INT nn default=0", "deletions INT nn default=0",
    "pair_eligible BOOL nn default=TRUE", "is_replay BOOL nn default=FALSE", "created_at TIMESTAMPTZ nn default=now()",
    constraints=(UniqueConstraint("repo_id", "sha", name="commit_repo_sha_key"),),
)
commit_parent = _model("commit_parent", "repo_id BIGINT nn fk repo.id", "child_sha TEXT nn", "parent_sha TEXT nn", "ordinal SMALLINT nn default=0", constraints=(PrimaryKeyConstraint("repo_id", "child_sha", "parent_sha"),))
ref_tag = _model(
    "ref_tag", "repo_id BIGINT nn fk repo.id", "name TEXT nn", "commit_sha TEXT nn", "tagged_at TIMESTAMPTZ",
    "annotated BOOL nn default=FALSE", "commit_id BIGINT fk commit.id", "main_sha TEXT", "main_commit_id BIGINT fk commit.id",
    "version_key TEXT", constraints=(PrimaryKeyConstraint("repo_id", "name"),),
)
commit_file = _model(
    "commit_file", "commit_id BIGINT nn fk commit.id", "file_id BIGINT nn fk file.id", "repo_id BIGINT nn fk repo.id",
    "change_type CHAR nn default='M'", "insertions INT nn default=0", "deletions INT nn default=0",
    "is_binary BOOL nn default=FALSE", "old_path TEXT", "similarity SMALLINT",
    constraints=(PrimaryKeyConstraint("commit_id", "file_id"),),
)

file_pair = _model(
    "file_pair", "repo_id BIGINT nn fk repo.id", "file_a_id BIGINT nn fk file.id", "file_b_id BIGINT nn fk file.id",
    "n_ab BIGINT nn default=0", "w_ab FLOAT nn default=0", "first_co_change TIMESTAMPTZ", "last_co_change TIMESTAMPTZ",
    "distinct_authors INT nn default=0", constraints=(PrimaryKeyConstraint("repo_id", "file_a_id", "file_b_id"), CheckConstraint("file_a_id < file_b_id", name="file_pair_order_ck"), CheckConstraint("n_ab > 0 AND w_ab >= 0 AND w_ab <= n_ab + 1e-6", name="file_pair_weight_ck")),
)

_metric_names = ["jaccard", "dice", "sorensen", "ochiai", "simpson", "braun_blanquet", "kulczynski", "fager", "russell_rao", "sokal_michener", "rogers_tanimoto", "hamann", "faith", "mutual_information", "pmi", "npmi", "ppmi", "chi_square", "log_likelihood_ratio", "t_score", "z_score", "poisson_significance", "hypergeometric_significance", "phi", "cramers_v", "yules_q", "yules_y", "michael", "association_strength", "confidence_ab", "confidence_ba"]
_metric_specs = tuple(f"{name} FLOAT" for name in _metric_names)
file_pair_metric = _model(
    "file_pair_metric", "repo_id BIGINT nn fk repo.id", "file_a_id BIGINT nn fk file.id", "file_b_id BIGINT nn fk file.id",
    "n_ab BIGINT nn", "n_a BIGINT nn", "n_b BIGINT nn", "n_total BIGINT nn", *_metric_specs,
    "computed_at TIMESTAMPTZ nn default=now()", constraints=(PrimaryKeyConstraint("repo_id", "file_a_id", "file_b_id"),
    CheckConstraint("n_ab >= 0 AND n_a >= n_ab AND n_b >= n_ab AND n_total >= n_a AND n_total >= n_b", name="file_pair_metric_cells_ck")),
)

directory = _model(
    "directory", "id BIGINT pk", "repo_id BIGINT nn fk repo.id", "path TEXT nn", "depth SMALLINT nn default=0",
    "file_count INT nn default=0", "change_count BIGINT nn default=0", "pair_change_count BIGINT nn default=0",
    "insertions BIGINT nn default=0", "deletions BIGINT nn default=0", "first_change_at TIMESTAMPTZ", "last_change_at TIMESTAMPTZ",
    constraints=(UniqueConstraint("repo_id", "path", name="directory_repo_path_key"),),
)
file_directory = _model("file_directory", "repo_id BIGINT nn fk repo.id", "file_id BIGINT nn fk file.id", "dir_id BIGINT nn fk directory.id", constraints=(PrimaryKeyConstraint("file_id", "dir_id"),))
dir_pair = _model("dir_pair", "repo_id BIGINT nn fk repo.id", "dir_a_id BIGINT nn fk directory.id", "dir_b_id BIGINT nn fk directory.id", "n_ab BIGINT nn default=0", "w_ab FLOAT nn default=0", "first_co_change TIMESTAMPTZ", "last_co_change TIMESTAMPTZ", constraints=(PrimaryKeyConstraint("repo_id", "dir_a_id", "dir_b_id"), CheckConstraint("dir_a_id < dir_b_id", name="dir_pair_order_ck"), CheckConstraint("n_ab > 0 AND w_ab >= 0 AND w_ab <= n_ab + 1e-6", name="dir_pair_weight_ck")))
dir_pair_metric = _model("dir_pair_metric", "repo_id BIGINT nn fk repo.id", "dir_a_id BIGINT nn fk directory.id", "dir_b_id BIGINT nn fk directory.id", "n_ab BIGINT nn", "n_a BIGINT nn", "n_b BIGINT nn", "n_total BIGINT nn", *_metric_specs, "computed_at TIMESTAMPTZ nn default=now()", constraints=(PrimaryKeyConstraint("repo_id", "dir_a_id", "dir_b_id"), CheckConstraint("n_ab >= 0 AND n_a >= n_ab AND n_b >= n_ab AND n_total >= n_a AND n_total >= n_b", name="dir_pair_metric_cells_ck")))
author_file = _model("author_file", "repo_id BIGINT nn fk repo.id", "author_id BIGINT nn fk author.id", "file_id BIGINT nn fk file.id", "n_commits BIGINT nn default=0", "insertions BIGINT nn default=0", "deletions BIGINT nn default=0", "first_at TIMESTAMPTZ", "last_at TIMESTAMPTZ", constraints=(PrimaryKeyConstraint("repo_id", "author_id", "file_id"),))

ingest_run = _model("ingest_run", "id BIGINT pk", "kind TEXT nn", "trigger TEXT nn default='manual'", "status TEXT nn default='running'", "started_at TIMESTAMPTZ nn default=now()", "finished_at TIMESTAMPTZ", "duration_s FLOAT", "repos_total INT nn default=0", "repos_ok INT nn default=0", "repos_failed INT nn default=0", "commits_added BIGINT nn default=0", "files_added BIGINT nn default=0", "pairs_written BIGINT nn default=0", "error TEXT", "detail JSONB nn default='{}'::jsonb")
ingest_run_repo = _model("ingest_run_repo", "run_id BIGINT nn fk ingest_run.id", "repo_id BIGINT nn fk repo.id", "status TEXT nn", "commits_added BIGINT nn default=0", "duration_s FLOAT", "error TEXT", constraints=(PrimaryKeyConstraint("run_id", "repo_id"),))

dep_bump = _model("dep_bump", "id BIGINT pk", "consumer_repo_id BIGINT nn fk repo.id", "consumer_sha TEXT nn", "dep_repo_id BIGINT fk repo.id", "dep_name TEXT nn", "dep_version TEXT nn", "dep_sha TEXT", "dep_commit_id BIGINT fk commit.id", "manifest TEXT nn default='go.mod'", "bumped_at TIMESTAMPTZ", "adoption_seconds BIGINT", "version_key TEXT", "resolution TEXT", "ecosystem TEXT", constraints=(UniqueConstraint("consumer_repo_id", "consumer_sha", "dep_name", "dep_version", name="dep_bump_identity_key"),))
repo_dependency = _model("repo_dependency", "consumer_repo_id BIGINT nn fk repo.id", "dep_repo_id BIGINT fk repo.id", "dep_name TEXT nn", "dep_version TEXT", "manifest TEXT nn default='go.mod'", "ecosystem TEXT nn default='go'", "observed_at TIMESTAMPTZ nn default=now()", constraints=(PrimaryKeyConstraint("consumer_repo_id", "dep_name", "manifest"),))
module_dependency = _model("module_dependency", "repo_id BIGINT nn fk repo.id", "consumer_module TEXT nn", "dep_module TEXT nn", "manifest TEXT nn", "ecosystem TEXT nn default='go'", "dep_version TEXT", "observed_at TIMESTAMPTZ nn default=now()", constraints=(PrimaryKeyConstraint("repo_id", "consumer_module", "dep_module", "manifest"),))
repo_impact = _model("repo_impact", "source_repo_id BIGINT nn fk repo.id", "target_repo_id BIGINT nn fk repo.id", "score FLOAT nn", "rank_in_source INT nn", "is_declared BOOL nn default=FALSE", "has_bump_history BOOL nn default=FALSE", "bump_count INT nn default=0", "median_adoption_days FLOAT", "features JSONB nn default='{}'::jsonb", "computed_at TIMESTAMPTZ nn default=now()", constraints=(PrimaryKeyConstraint("source_repo_id", "target_repo_id"),))
feedback = _model("feedback", "id BIGINT pk", "kind TEXT nn", "severity TEXT nn default='medium'", "tool TEXT", "args JSONB nn default='{}'::jsonb", "repo TEXT", "path TEXT", "expected TEXT", "observed TEXT", "detail TEXT", "fingerprint TEXT nn u", "occurrences INT nn default=1", "first_seen_at TIMESTAMPTZ nn default=now()", "last_seen_at TIMESTAMPTZ nn default=now()", "status TEXT nn default='open'", "resolution TEXT", "resolved_at TIMESTAMPTZ")
file_cluster = _model("file_cluster", "repo_id BIGINT nn fk repo.id", "file_id BIGINT pk fk file.id", "cluster_id INT nn", "cluster_size INT nn default=0", "cohesion FLOAT", "dirs_spanned INT nn default=1")
pair_drift = _model("pair_drift", "repo_id BIGINT nn fk repo.id", "file_a_id BIGINT nn fk file.id", "file_b_id BIGINT nn fk file.id", "window_days INT nn", "n_ab_recent BIGINT nn default=0", "n_ab_historic BIGINT nn default=0", "npmi_recent FLOAT", "npmi_historic FLOAT", "delta FLOAT", "trend TEXT nn default='stable'", constraints=(PrimaryKeyConstraint("repo_id", "file_a_id", "file_b_id"), CheckConstraint("file_a_id < file_b_id", name="pair_drift_order_ck")))
file_risk = _model("file_risk", "file_id BIGINT pk fk file.id", "repo_id BIGINT nn fk repo.id", "churn_pct FLOAT", "coupling_pct FLOAT", "ownership_hhi FLOAT", "effective_authors FLOAT", "author_count INT nn default=0", "partner_count INT nn default=0", "change_count BIGINT nn default=0", "days_since_change INT", "risk_score FLOAT", "computed_at TIMESTAMPTZ nn default=now()")
repo_package = _model("repo_package", "repo_id BIGINT nn fk repo.id", "ecosystem TEXT nn", "name TEXT nn", constraints=(PrimaryKeyConstraint("repo_id", "ecosystem", "name"),))

call_log = _model("call_log", "id BIGINT pk", "at TIMESTAMPTZ nn default=now()", "surface TEXT nn", "name TEXT nn", "method TEXT", "status TEXT nn", "duration_ms INT nn", "arguments JSONB", "result_preview JSONB", "result_bytes INT", "result_rows INT", "error TEXT", "client TEXT", constraints=(CheckConstraint("surface IN ('mcp','http')", name="call_log_surface_ck"), CheckConstraint("status IN ('ok','error')", name="call_log_status_ck")))
app_user = _model("app_user", "id BIGINT pk", "email TEXT nn", "name TEXT nn", "role TEXT nn default='member'", "password_hash TEXT nn", "is_active BOOL nn default=TRUE", "created_at TIMESTAMPTZ nn default=now()", "created_by BIGINT fk app_user.id", "last_login_at TIMESTAMPTZ", constraints=(CheckConstraint("role IN ('admin','member')", name="app_user_role_ck"),))
user_session = _model("user_session", "token_hash TEXT pk", "user_id BIGINT nn fk app_user.id", "created_at TIMESTAMPTZ nn default=now()", "expires_at TIMESTAMPTZ nn", "last_seen_at TIMESTAMPTZ nn default=now()", "user_agent TEXT")
api_token = _model("api_token", "id BIGINT pk", "token_hash TEXT nn u", "prefix TEXT nn", "user_id BIGINT nn fk app_user.id", "name TEXT nn", "created_at TIMESTAMPTZ nn default=now()", "expires_at TIMESTAMPTZ", "last_used_at TIMESTAMPTZ")
login_attempt = _model("login_attempt", "id BIGINT pk", "email TEXT nn", "at TIMESTAMPTZ nn default=now()", "client TEXT")
meta = _model("meta", "key TEXT pk", "value JSONB nn", "updated_at TIMESTAMPTZ nn default=now()")

MODEL_CLASSES = SimpleNamespace(
    **{
        model.__name__: model
        for model in (
            account, repo, author, file, file_alias, commit, commit_parent,
            ref_tag, commit_file, file_pair, file_pair_metric, directory,
            file_directory, dir_pair, dir_pair_metric, author_file, ingest_run,
            ingest_run_repo, dep_bump, repo_dependency, module_dependency,
            repo_impact, feedback, file_cluster, pair_drift, file_risk,
            repo_package, call_log, app_user, user_session, api_token,
            login_attempt, meta,
        )
    }
)


# These indexes are part of the canonical schema, not a post-bootstrap SQL
# script.  PostgreSQL-specific operator classes are intentionally kept here;
# they are needed by the path and repository search endpoints.
Index("account_login_host_idx", func.lower(account.__table__.c.login), func.lower(account.__table__.c.host), unique=True)
Index("app_user_email_idx", func.lower(app_user.__table__.c.email), unique=True)
Index("repo_owner_idx", repo.__table__.c.owner)
Index("repo_status_idx", repo.__table__.c.ingest_status)
Index("repo_language_idx", repo.__table__.c.primary_language)
Index("repo_pushed_idx", repo.__table__.c.github_pushed_at.desc())
Index("repo_provider_idx", repo.__table__.c.provider)
Index("file_repo_idx", file.__table__.c.repo_id)
Index("file_dir_idx", file.__table__.c.repo_id, file.__table__.c.dir_path)
Index("file_ext_idx", file.__table__.c.extension)
Index("file_churn_idx", file.__table__.c.repo_id, file.__table__.c.change_count.desc())
Index("commit_repo_time_idx", commit.__table__.c.repo_id, commit.__table__.c.committed_at.desc())
Index("commit_author_idx", commit.__table__.c.author_id)
Index("commit_eligible_idx", commit.__table__.c.repo_id, commit.__table__.c.pair_eligible, postgresql_where=commit.__table__.c.pair_eligible)
Index("commit_file_file_idx", commit_file.__table__.c.file_id)
Index("commit_file_repo_idx", commit_file.__table__.c.repo_id)
Index("file_pair_a_idx", file_pair.__table__.c.file_a_id)
Index("file_pair_b_idx", file_pair.__table__.c.file_b_id)
Index("fpm_npmi_idx", file_pair_metric.__table__.c.repo_id, file_pair_metric.__table__.c.npmi.desc())
Index("fpm_llr_idx", file_pair_metric.__table__.c.repo_id, file_pair_metric.__table__.c.log_likelihood_ratio.desc())
Index("fpm_jaccard_idx", file_pair_metric.__table__.c.repo_id, file_pair_metric.__table__.c.jaccard.desc())
Index("directory_repo_idx", directory.__table__.c.repo_id)
Index("directory_churn_idx", directory.__table__.c.repo_id, directory.__table__.c.change_count.desc())
Index("repo_dependency_dep_idx", repo_dependency.__table__.c.dep_repo_id)
Index("module_dependency_repo_idx", module_dependency.__table__.c.repo_id)
Index("repo_impact_source_idx", repo_impact.__table__.c.source_repo_id, repo_impact.__table__.c.score.desc())
Index("repo_impact_target_idx", repo_impact.__table__.c.target_repo_id, repo_impact.__table__.c.score.desc())
Index("feedback_status_idx", feedback.__table__.c.status, feedback.__table__.c.occurrences.desc())
Index("file_risk_repo_idx", file_risk.__table__.c.repo_id, file_risk.__table__.c.risk_score.desc())
Index("call_log_at_idx", call_log.__table__.c.at.desc())
Index("call_log_surface_idx", call_log.__table__.c.surface, call_log.__table__.c.at.desc())
Index("api_token_user_idx", api_token.__table__.c.user_id)
Index("login_attempt_idx", login_attempt.__table__.c.email, login_attempt.__table__.c.at.desc())
Index("user_session_user_idx", user_session.__table__.c.user_id)
Index("user_session_expiry_idx", user_session.__table__.c.expires_at)
Index("repo_account_idx", repo.__table__.c.account_id)


# Keep the deletion semantics explicit.  Derived facts must disappear with
# their repository/file, while optional identity links may be nulled when an
# author or account is removed.  Setting this on the SQLAlchemy ForeignKey
# objects makes it part of the generated DDL rather than an application rule.
_cascade = {
    "repo": ["account_id"],
    "file": ["repo_id"],
    "file_alias": ["repo_id", "file_id"],
    "commit": ["repo_id"],
    "commit_parent": ["repo_id"],
    "ref_tag": ["repo_id"],
    "commit_file": ["commit_id", "file_id", "repo_id"],
    "file_pair": ["repo_id", "file_a_id", "file_b_id"],
    "file_pair_metric": ["repo_id", "file_a_id", "file_b_id"],
    "directory": ["repo_id"],
    "file_directory": ["repo_id", "file_id", "dir_id"],
    "dir_pair": ["repo_id", "dir_a_id", "dir_b_id"],
    "dir_pair_metric": ["repo_id", "dir_a_id", "dir_b_id"],
    "author_file": ["repo_id", "author_id", "file_id"],
    "ingest_run_repo": ["run_id", "repo_id"],
    "dep_bump": ["consumer_repo_id", "dep_repo_id"],
    "repo_dependency": ["consumer_repo_id", "dep_repo_id"],
    "module_dependency": ["repo_id"],
    "repo_impact": ["source_repo_id", "target_repo_id"],
    "file_cluster": ["repo_id", "file_id"],
    "pair_drift": ["repo_id", "file_a_id", "file_b_id"],
    "file_risk": ["file_id", "repo_id"],
    "repo_package": ["repo_id"],
    "user_session": ["user_id"],
    "api_token": ["user_id"],
}
_set_null = {
    ("commit", "author_id"), ("commit", "committer_id"),
    ("ref_tag", "commit_id"), ("ref_tag", "main_commit_id"),
    ("dep_bump", "dep_commit_id"), ("app_user", "created_by"),
    ("repo", "account_id"),
}
for _table_name, _columns in _cascade.items():
    for _column_name in _columns:
        for _foreign_key in metadata.tables[_table_name].c[_column_name].foreign_keys:
            _action = "SET NULL" if (_table_name, _column_name) in _set_null else "CASCADE"
            _foreign_key.ondelete = _action
            _foreign_key.constraint.ondelete = _action
for _table_name, _column_name in _set_null:
    for _foreign_key in metadata.tables[_table_name].c[_column_name].foreign_keys:
        _foreign_key.ondelete = "SET NULL"
        _foreign_key.constraint.ondelete = "SET NULL"


# PostgreSQL search indexes used by repository/file path queries.  They are
# declared here so a fresh install has the same query characteristics as the
# populated development database.
Index("repo_name_trgm_idx", repo.__table__.c.full_name, postgresql_using="gin", postgresql_ops={"full_name": "gin_trgm_ops"})
Index("repo_topics_idx", repo.__table__.c.topics, postgresql_using="gin")
Index("author_name_trgm_idx", author.__table__.c.display_name, postgresql_using="gin", postgresql_ops={"display_name": "gin_trgm_ops"})
Index("file_path_trgm_idx", file.__table__.c.path, postgresql_using="gin", postgresql_ops={"path": "gin_trgm_ops"})
Index("commit_time_brin_idx", commit.__table__.c.committed_at, postgresql_using="brin")
