"""ORM read models used by the API, MCP server, CLI, and UI."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Float, case, cast, func, literal, literal_column
from sqlalchemy.orm import aliased

from git_synapse.config import get_config
from git_synapse.db.orm import models, session_scope
from git_synapse.stats.registry import DEFAULT_MEASURE, MEASURES, resolve

FEEDBACK_KINDS = (
    "missing_data", "wrong_data", "stale_data", "tool_error", "coverage_gap", "suggestion"
)
FEEDBACK_SEVERITIES = ("low", "medium", "high")
FEEDBACK_STATUSES = ("open", "fixed", "wontfix")


def _clamp_limit(limit: int | None) -> int:
    cfg = get_config().analysis
    return cfg.default_limit if limit is None else max(1, min(int(limit), cfg.max_limit))


def _contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _safe_order(measure: str) -> str:
    key = (measure or DEFAULT_MEASURE).strip().lower()
    return "n_ab" if key == "n_ab" else resolve(key).key


def _as_dict(obj: Any) -> dict[str, Any]:
    return {attr.key: getattr(obj, attr.key) for attr in obj.__mapper__.column_attrs}


def _rows(query: Any) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in query.all()]


def _row(query: Any) -> dict[str, Any] | None:
    row = query.first()
    return dict(row._mapping) if row else None


def _model(name: str) -> Any:
    return getattr(models(), name)


def list_repos(search: str | None = None, language: str | None = None,
               status: str | None = None, order_by: str = "commit_count",
               descending: bool = True, limit: int | None = 500, offset: int = 0,
               account_id: int | None = None, include_paused: bool = False) -> list[dict]:
    Repo = _model("Repo")
    allowed = {"commit_count", "file_count", "pair_count", "author_count", "name",
               "stargazers", "last_commit_at", "github_pushed_at", "disk_usage_kb",
               "total_insertions", "last_ingest_at"}
    order = getattr(Repo, order_by if order_by in allowed else "commit_count")
    conditions = []
    if search:
        pattern = _contains(search)
        conditions.append((Repo.full_name.ilike(pattern, escape="\\")) |
                          (Repo.description.ilike(pattern, escape="\\")))
    if language:
        conditions.append(Repo.primary_language == language)
    if status:
        conditions.append(Repo.ingest_status == status)
    if account_id is not None:
        conditions.append(Repo.account_id == account_id)
    elif not include_paused:
        conditions.append(Repo.is_enabled.is_(True))
    fields = [getattr(Repo, key) for key in (
        "id", "account_id", "full_name", "owner", "name", "host", "provider", "description",
        "html_url", "primary_language", "topics", "is_private", "is_fork", "is_archived",
        "stargazers", "forks_count", "open_issues", "license_spdx", "visibility", "default_branch",
        "disk_usage_kb", "mirror_size_kb", "clone_mode", "has_churn", "ingest_status", "ingest_error",
        "commit_count", "pair_population", "file_count", "author_count", "pair_count",
        "total_insertions", "total_deletions", "first_commit_at", "last_commit_at", "github_created_at",
        "github_pushed_at", "last_ingest_at", "last_aggregate_at")]
    with session_scope() as session:
        query = session.query(*fields).filter(*conditions).order_by(
            order.desc().nullslast() if descending else order.asc().nullsfirst()
        ).limit(_clamp_limit(limit)).offset(max(offset, 0))
        return _rows(query)


def get_repo(repo_id: int) -> dict | None:
    Repo = _model("Repo")
    with session_scope() as session:
        obj = session.get(Repo, repo_id)
        return _as_dict(obj) if obj else None


def repo_languages() -> list[dict]:
    """How many repositories each primary language accounts for."""
    Repo = _model("Repo")
    with session_scope() as session:
        rows = (session.query(Repo.primary_language, func.count().label("n"))
                .filter(Repo.primary_language.is_not(None))
                .group_by(Repo.primary_language)
                .order_by(func.count().desc(), Repo.primary_language)
                .all())
        return [{"language": language, "n": n} for language, n in rows]


def search_files(term: str | None = None, repo_id: int | None = None, extension: str | None = None,
                 min_changes: int = 0, order_by: str = "change_count", limit: int | None = 50,
                 offset: int = 0) -> list[dict]:
    File, Repo = _model("File"), _model("Repo")
    allowed = {"change_count", "path", "last_change_at", "insertions", "author_count"}
    order_name = order_by if order_by in allowed else "change_count"
    fields = [File.id, File.repo_id, Repo.full_name.label("repo"), File.path, File.dir_path,
              File.basename, File.extension, File.depth, File.is_deleted, File.change_count,
              File.pair_change_count, File.insertions, File.deletions, File.author_count,
              File.first_change_at, File.last_change_at]
    conditions = [File.change_count >= max(min_changes, 0)]
    if term:
        conditions.append(File.path.ilike(_contains(term), escape="\\"))
    if repo_id is not None:
        conditions.append(File.repo_id == repo_id)
    if extension:
        conditions.append(File.extension == extension)
    order = getattr(File, order_name)
    with session_scope() as session:
        query = session.query(*fields).join(Repo, Repo.id == File.repo_id).filter(*conditions).order_by(
            order.asc() if order_name == "path" else order.desc().nullslast()
        ).limit(_clamp_limit(limit)).offset(max(offset, 0))
        return _rows(query)


def get_file(file_id: int) -> dict | None:
    File, Repo = _model("File"), _model("Repo")
    fields = [getattr(File, attr.key) for attr in File.__mapper__.column_attrs]
    fields += [Repo.full_name.label("repo"), Repo.pair_population, Repo.has_churn,
               Repo.html_url.label("repo_url"), Repo.default_branch]
    with session_scope() as session:
        return _row(session.query(*fields).join(Repo, Repo.id == File.repo_id)
                    .filter(File.id == file_id))


def resolve_file(repo: str | None, path: str, repo_id: int | None = None) -> dict | None:
    File, Alias, Repo = _model("File"), _model("FileAlias"), _model("Repo")
    repo_filter = Repo.id == repo_id if repo_id is not None else (
        (Repo.full_name == repo) | (Repo.name == repo)
    )
    with session_scope() as session:
        row = session.query(File.id).join(Repo, Repo.id == File.repo_id).filter(
            repo_filter, File.path == path).first()
        file_id = row[0] if row else None
        if file_id is None:
            row = session.query(Alias.file_id).join(Repo, Repo.id == Alias.repo_id).filter(
                repo_filter, Alias.old_path == path).first()
            file_id = row[0] if row else None
    return get_file(int(file_id)) if file_id is not None else None


def file_extensions(repo_id: int | None = None) -> list[dict]:
    File = _model("File")
    with session_scope() as session:
        query = session.query(File)
        if repo_id is not None:
            query = query.filter_by(repo_id=repo_id)
        counts = Counter(row.extension for row in query.all() if row.extension is not None)
        return [{"extension": key, "n": value} for key, value in counts.most_common(60)]


def _oriented_order(measure: str) -> tuple[str, str, str]:
    order = _safe_order(measure)
    if order == "confidence_ab":
        return "confidence_out", "confidence_ab", "confidence_ba"
    if order == "confidence_ba":
        return "confidence_in", "confidence_ba", "confidence_ab"
    return order, order, order


def _pair_rows(model: Any, entity: int, measure: str, limit: int, minimum: int, minimum_score: float | None,
               a_name: str, b_name: str) -> list[dict]:
    _order, order_a, order_b = _oriented_order(measure)
    with session_scope() as session:
        pairs = session.query(model).filter(
            (getattr(model, a_name) == entity) | (getattr(model, b_name) == entity),
            model.n_ab >= minimum,
        ).all()
        output = []
        label_model = _model("File") if a_name == "file_a_id" else _model("Directory")
        for pair in pairs:
            left = getattr(pair, a_name) == entity
            other = getattr(pair, b_name if left else a_name)
            score_key = order_a if left else order_b
            score = getattr(pair, score_key, None)
            if minimum_score is not None and (score is None or score < minimum_score):
                continue
            row = _as_dict(pair)
            row["other_id"] = other
            row["score"] = score
            row["n_other"] = getattr(pair, "n_b" if left else "n_a", None)
            row["confidence_out"] = getattr(pair, "confidence_ab" if left else "confidence_ba", None)
            row["confidence_in"] = getattr(pair, "confidence_ba" if left else "confidence_ab", None)
            label = session.get(label_model, other)
            if label is not None:
                row["path"] = getattr(label, "path", None)
                if a_name == "file_a_id":
                    FilePair, Drift = _model("FilePair"), _model("PairDrift")
                    lo, hi = sorted((entity, other))
                    cochange = session.query(FilePair).filter_by(
                        repo_id=pair.repo_id, file_a_id=lo, file_b_id=hi
                    ).one_or_none()
                    drift = session.query(Drift).filter_by(
                        repo_id=pair.repo_id, file_a_id=lo, file_b_id=hi
                    ).one_or_none()
                    row["last_co_change"] = cochange.last_co_change if cochange else None
                    row["trend"] = drift.trend if drift else None
                    last = row["last_co_change"]
                    row["days_since_co_change"] = (
                        max(0, (datetime.now(UTC) - last).days) if last else None
                    )
                    row["is_deleted"] = bool(label.is_deleted)
            output.append(row)
        output.sort(key=lambda item: (item.get("score") is not None, item.get("score", 0)), reverse=True)
        return output[:_clamp_limit(limit)]


def coupled_files(file_id: int, measure: str = DEFAULT_MEASURE, limit: int | None = 25,
                  min_support: int = 1, min_score: float | None = None) -> list[dict]:
    return _pair_rows(_model("FilePairMetric"), file_id, measure, _clamp_limit(limit), max(min_support, 1), min_score, "file_a_id", "file_b_id")


def coupled_directories(dir_id: int, measure: str = DEFAULT_MEASURE, limit: int | None = 25,
                        min_support: int = 1, min_score: float | None = None) -> list[dict]:
    return _pair_rows(_model("DirPairMetric"), dir_id, measure, _clamp_limit(limit), max(min_support, 1), min_score, "dir_a_id", "dir_b_id")


def pair_detail(file_a_id: int, file_b_id: int) -> dict | None:
    Metric, File, Repo = _model("FilePairMetric"), _model("File"), _model("Repo")
    Pair = _model("FilePair")
    File2 = aliased(File)
    fields = [getattr(Metric, attr.key) for attr in Metric.__mapper__.column_attrs]
    fields.extend([
        File.path.label("path_a"), File2.path.label("path_b"), Repo.full_name.label("repo"),
        Pair.first_co_change, Pair.last_co_change, Pair.distinct_authors, Pair.w_ab,
    ])
    with session_scope() as session:
        query = session.query(*fields).join(
            File, File.id == Metric.file_a_id
        ).join(File2, File2.id == Metric.file_b_id).join(
            Repo, Repo.id == Metric.repo_id
        ).outerjoin(
            Pair, (Pair.repo_id == Metric.repo_id)
            & (Pair.file_a_id == Metric.file_a_id)
            & (Pair.file_b_id == Metric.file_b_id)
        )
        row = _row(query.filter(
            Metric.file_a_id == min(file_a_id, file_b_id),
            Metric.file_b_id == max(file_a_id, file_b_id),
        ))
    if row is None:
        return None
    lo, hi = sorted((file_a_id, file_b_id))
    if (file_a_id, file_b_id) != (lo, hi):
        for left, right in (
            ("file_a_id", "file_b_id"), ("path_a", "path_b"),
            ("n_a", "n_b"), ("confidence_ab", "confidence_ba"),
        ):
            row[left], row[right] = row[right], row[left]
    a, n_a, n_b, n_total = row["n_ab"], row["n_a"], row["n_b"], row["n_total"]
    row["cells"] = {
        "a": a, "b": n_a - a, "c": n_b - a,
        "d": n_total - n_a - n_b + a,
        "n_a": n_a, "n_b": n_b, "n_total": n_total,
        "expected": (n_a * n_b / n_total) if n_total else 0.0,
    }
    return row


def co_change_commits(file_a_id: int, file_b_id: int, limit: int = 25) -> list[dict]:
    Commit, Change, Author = _model("Commit"), _model("CommitFile"), _model("Author")
    with session_scope() as session:
        first = {row.commit_id for row in session.query(Change.commit_id).filter_by(file_id=file_a_id).all()}
        second = {row.commit_id for row in session.query(Change.commit_id).filter_by(file_id=file_b_id).all()}
        commits = session.query(Commit).filter(Commit.id.in_(first & second)).order_by(
            Commit.committed_at.desc(),
        ).limit(max(limit, 1)).all()
        authors = {row.id: row for row in session.query(Author).filter(
            Author.id.in_({c.author_id for c in commits if c.author_id})
        ).all()}
        return [{"id": c.id, "sha": c.sha, "subject": c.subject,
                 "committed_at": c.committed_at, "n_files": c.n_files,
                 "author": ((authors.get(c.author_id).display_name or authors.get(c.author_id).email)
                            if authors.get(c.author_id) else None)} for c in commits]


def file_commits(file_id: int, limit: int = 50) -> list[dict]:
    Commit, Change, Author = _model("Commit"), _model("CommitFile"), _model("Author")
    with session_scope() as session:
        changes = session.query(Change).filter_by(file_id=file_id).all()
        commits = {row.id: row for row in session.query(Commit).filter(
            Commit.id.in_({change.commit_id for change in changes})
        ).order_by(Commit.committed_at.desc()).limit(max(limit, 1)).all()}
        authors = {row.id: row for row in session.query(Author).filter(
            Author.id.in_({c.author_id for c in commits.values() if c.author_id})
        ).all()}
        output = []
        for change in changes:
            commit = commits.get(change.commit_id)
            if commit is None:
                continue
            author = authors.get(commit.author_id)
            output.append({"id": commit.id, "sha": commit.sha, "subject": commit.subject,
                           "committed_at": commit.committed_at, "n_files": commit.n_files,
                           "change_type": change.change_type, "insertions": change.insertions,
                           "deletions": change.deletions,
                           "author": (author.display_name or author.email) if author else None})
        output.sort(key=lambda row: row["committed_at"], reverse=True)
        return output[:max(limit, 1)]


def file_authors(file_id: int, limit: int = 20) -> list[dict]:
    Author, Link = _model("Author"), _model("AuthorFile")
    with session_scope() as session:
        query = session.query(Author.id, Author.display_name, Author.email, Link.n_commits,
                              Link.insertions, Link.deletions, Link.first_at, Link.last_at).join(
            Link, Link.author_id == Author.id).filter(Link.file_id == file_id).order_by(
                Link.n_commits.desc()).limit(max(limit, 1))
        return _rows(query)


def coupling_graph(repo_id: int, measure: str = DEFAULT_MEASURE, limit: int = 150,
                   min_support: int = 2, center_file_id: int | None = None,
                   min_score: float | None = None) -> dict:
    Metric, File = _model("FilePairMetric"), _model("File")
    order = _safe_order(measure)
    with session_scope() as session:
        filters = [Metric.repo_id == repo_id, Metric.n_ab >= max(min_support, 1)]
        if min_score is not None:
            filters.append(getattr(Metric, order) >= min_score)
        if center_file_id is not None:
            filters.append((Metric.file_a_id == center_file_id) | (Metric.file_b_id == center_file_id))
        pairs = session.query(Metric).filter(*filters).order_by(
            getattr(Metric, order).desc().nullslast()
        ).limit(max(1, min(int(limit), 2000))).all()
        ids = {x for p in pairs for x in (p.file_a_id, p.file_b_id)}
        files = {f.id: f for f in session.query(File).filter(File.id.in_(ids)).all()}
        edges = [{
            "source": p.file_a_id, "target": p.file_b_id, "score": getattr(p, order),
            "n_ab": p.n_ab, "npmi": p.npmi, "jaccard": p.jaccard,
            "log_likelihood_ratio": p.log_likelihood_ratio,
            "confidence_ab": p.confidence_ab, "confidence_ba": p.confidence_ba,
        } for p in pairs]
        nodes = [_as_dict(files[i]) for i in sorted(files)]
        return {
            "measure": order, "nodes": nodes, "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges)},
        }


def overview() -> dict:
    Repo, Commit = _model("Repo"), _model("Commit")
    names = {"repos": Repo, "commits": Commit, "changes": _model("CommitFile"),
             "files": _model("File"), "directories": _model("Directory"), "authors": _model("Author"),
             "file_pairs": _model("FilePair"), "dir_pairs": _model("DirPair")}
    with session_scope() as session:
        result = {key: session.query(func.count()).select_from(model).scalar()
                  for key, model in names.items()}
        ready, failed, mirror_kb = session.query(
            func.count().filter(Repo.ingest_status == "ready"),
            func.count().filter(Repo.ingest_status == "failed"),
            func.coalesce(func.sum(Repo.mirror_size_kb), 0),
        ).one()
        first, last = session.query(func.min(Commit.committed_at), func.max(Commit.committed_at)).one()
    result.update(repos_ready=ready, repos_failed=failed, mirror_kb=int(mirror_kb),
                  first_commit_at=first, last_commit_at=last)
    result["file_changes"] = result.pop("changes")
    return result


def hotspots(repo_id: int | None = None, limit: int = 25, min_changes: int = 0,
             include_deleted: bool = False) -> list[dict]:
    File, Pair = _model("File"), _model("FilePair")
    with session_scope() as session:
        query = session.query(File).filter(File.change_count >= min_changes)
        if not include_deleted:
            query = query.filter(File.is_deleted.is_(False))
        if repo_id is not None:
            query = query.filter(File.repo_id == repo_id)
        files = query.order_by(File.change_count.desc()).limit(max(limit, 1)).all()
        pair_rows = session.query(Pair).filter(
            Pair.repo_id.in_({row.repo_id for row in files})
        ).all()
        partner_counts = {row.id: 0 for row in files}
        for pair in pair_rows:
            if pair.file_a_id in partner_counts:
                partner_counts[pair.file_a_id] += 1
            if pair.file_b_id in partner_counts:
                partner_counts[pair.file_b_id] += 1
        return [{**_as_dict(row), "partner_count": partner_counts[row.id]} for row in files]


def strongest_pairs(repo_id: int | None = None, measure: str = DEFAULT_MEASURE, limit: int = 25,
                    min_support: int = 1) -> list[dict]:
    Metric, Repo = _model("FilePairMetric"), _model("Repo")
    order = _safe_order(measure)
    with session_scope() as session:
        query = session.query(Metric, Repo.full_name.label("repo")).join(
            Repo, Repo.id == Metric.repo_id).filter(Metric.n_ab >= min_support)
        if repo_id is not None:
            query = query.filter(Metric.repo_id == repo_id)
        rows = query.order_by(getattr(Metric, order).desc().nullslast()).limit(max(limit, 1)).all()
        return [{**_as_dict(metric), "repo": repo, "score": getattr(metric, order)}
                for metric, repo in rows]


def directories(repo_id: int, limit: int = 200) -> list[dict]:
    Directory = _model("Directory")
    with session_scope() as session:
        return [_as_dict(x) for x in session.query(Directory).filter_by(repo_id=repo_id)
                .order_by(Directory.change_count.desc()).limit(max(limit, 1)).all()]


def directory_tree(repo_id: int, path: str = "", limit: int = 1000) -> dict:
    Directory, File = _model("Directory"), _model("File")
    prefix = f"{path.rstrip('/')}/" if path else ""
    with session_scope() as session:
        directory_scope = Directory.path.like(f"{prefix}%") if path else Directory.path.like("%")
        if path:
            directory_scope = (Directory.path == path) | directory_scope
        dirs = session.query(Directory).filter(Directory.repo_id == repo_id, directory_scope).all()
        files = session.query(File).filter(File.repo_id == repo_id, File.dir_path == path).limit(max(limit, 1)).all()
        current = None if not path else next(
            (directory for directory in dirs if directory.path == path), None
        )
        return {
            "path": path,
            "directory": _as_dict(current) if current is not None else None,
            "directories": [_as_dict(x) for x in dirs
                            if x.path and x.path.count("/") == prefix.count("/")],
            "files": [_as_dict(x) for x in files],
        }


def corpus_shape(now: datetime | None = None) -> dict:
    """The distributions the landing page draws, counted in the database."""
    Commit, Pair, Repo, File, Bump = (_model(n) for n in ("Commit", "FilePair", "Repo", "File", "DepBump"))
    now = now or datetime.now(UTC)
    size = case((Repo.commit_count < 100, "<100"), (Repo.commit_count < 1000, "100-1k"),
                (Repo.commit_count < 10000, "1k-10k"), else_="10k+")
    age = func.floor(func.extract("epoch", literal(now) - Repo.last_commit_at) / 86400)
    recency = case((Repo.last_commit_at.is_(None), "never"), (age < 30, "past month"),
                   (age < 180, "past 6 months"), (age < 365, "past year"), else_="over a year")
    year = func.extract("year", func.timezone(literal_column("'UTC'"), Commit.authored_at))
    adoption = func.least(func.greatest(
        func.trunc(cast(Bump.adoption_seconds, Float) / 86400 / 60) + 1, 1), 7)

    with session_scope() as session:
        def counted(column, where=None):
            query = session.query(column, func.count())
            if where is not None:
                query = query.filter(where)
            return query.group_by(literal_column("1")).all()

        commits_by_year = counted(year, where=Commit.authored_at.is_not(None))
        pair_support = counted(func.least(Pair.n_ab, 10))
        language = func.coalesce(Repo.primary_language, "unknown")
        languages = session.query(language, func.count()).group_by(literal_column("1")).order_by(
            literal_column("2").desc(), literal_column("1")).all()
        repo_sizes = {bucket: (n, total) for bucket, n, total in session.query(
            size, func.count(), func.sum(Repo.commit_count)).filter(
            Repo.commit_count > 0).group_by(literal_column("1")).all()}
        commit_width = counted(func.least(Commit.n_files, 12), where=Commit.pair_eligible.is_(True))
        authors_per_file = counted(func.least(File.author_count, 8), where=File.change_count > 0)
        repo_recency = dict(counted(recency))
        adoption_days = counted(adoption, where=Bump.adoption_seconds.is_not(None))
    return {
        "commits_by_year": [{"year": int(y), "n": n} for y, n in sorted(commits_by_year)],
        "pair_support": [{"support": s, "n": n} for s, n in sorted(pair_support)],
        "repo_sizes": [{"bucket": b, "n": repo_sizes[b][0], "commits": int(repo_sizes[b][1])}
                       for b in ("<100", "100-1k", "1k-10k", "10k+") if b in repo_sizes],
        "languages": [{"language": name, "n": n} for name, n in languages],
        "commit_width": [{"files": f, "n": n} for f, n in sorted(commit_width)],
        "authors_per_file": [{"authors": a, "n": n} for a, n in sorted(authors_per_file)],
        "adoption_days": [{"bucket": int(b), "n": n} for b, n in sorted(adoption_days)],
        "repo_recency": [{"bucket": b, "n": repo_recency[b]} for b in
                         ("never", "past month", "past 6 months", "past year", "over a year")
                         if b in repo_recency],
    }


def recent_runs(limit: int = 20) -> list[dict]:
    Run = _model("IngestRun")
    with session_scope() as session:
        return [_as_dict(x) for x in session.query(Run).order_by(Run.id.desc()).limit(max(limit, 1)).all()]


def run_detail(run_id: int) -> dict | None:
    Run, Link, Repo = _model("IngestRun"), _model("IngestRunRepo"), _model("Repo")
    with session_scope() as session:
        run = session.get(Run, run_id)
        if not run:
            return None
        details = _rows(session.query(Link.repo_id, Repo.full_name, Link.status,
                                      Link.commits_added, Link.duration_s, Link.error)
                        .join(Repo, Repo.id == Link.repo_id).filter(Link.run_id == run_id))
        result = _as_dict(run)
        result["repos"] = details
        return result


def measure_catalog() -> list[dict]:
    return [
        {
            "key": m.key,
            "label": m.label,
            "family": m.family,
            "formula": m.formula,
            "summary": m.summary,
            "detail": m.detail,
            "lower": m.lower,
            "upper": m.upper,
            "signed": m.signed,
            "neutral": m.neutral,
            "is_significance": m.is_significance,
            "rare_item_bias": m.rare_item_bias,
            "saturates_on_sparse": m.saturates_on_sparse,
            "hit_rate": m.hit_rate,
            "aliases": list(m.aliases),
        }
        for m in MEASURES
    ]


def _module_rows(repo_id: int) -> list[dict[str, Any]]:
    Module = _model("ModuleDependency")
    with session_scope() as session:
        rows = session.query(Module.consumer_module, Module.dep_module, Module.manifest).filter(
            Module.repo_id == repo_id
        ).all()
    return [
        {"consumer_module": row.consumer_module, "dep_module": row.dep_module, "manifest": row.manifest}
        for row in rows
    ]


def module_context(repo_id: int, path: str) -> dict:
    records = _module_rows(repo_id)
    modules = sorted({row["consumer_module"] for row in records} | {
        row["dep_module"] for row in records
    })
    normalised = path.strip()
    while normalised.startswith(("./", "/")):
        normalised = normalised[2:] if normalised.startswith("./") else normalised[1:]
    owning = max(
        (module for module in modules if module and (
            normalised == module or normalised.startswith(f"{module}/")
        )),
        key=len,
        default=None,
    )
    return {
        "owning_module": owning,
        "declares": sorted({row["dep_module"] for row in records if row["consumer_module"] == owning}),
        "declared_by": sorted({row["consumer_module"] for row in records if row["dep_module"] == owning}),
        "modules": modules,
    }


def record_feedback(kind: str, severity: str = "medium", tool: str | None = None, args: dict | None = None,
                    repo: str | None = None, path: str | None = None, expected: str | None = None,
                    observed: str | None = None, detail: str | None = None, fingerprint: str = "") -> dict:
    if kind not in FEEDBACK_KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if severity not in FEEDBACK_SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    if not (detail or "").strip():
        raise ValueError("detail is required")
    if not fingerprint:
        context = (tool or "", repo or "", path or "", (expected or "").strip().lower()[:200])
        seed = "|".join((kind, *context) if any(context) else (kind, detail.strip().lower()[:200]))
        fingerprint = hashlib.sha256(seed.encode()).hexdigest()[:32]
    Feedback = _model("Feedback")
    with session_scope() as session:
        existing = session.query(Feedback).filter_by(fingerprint=fingerprint).one_or_none()
        if existing:
            existing.occurrences += 1
            existing.last_seen_at = datetime.now(UTC)
            if existing.status != "open":
                existing.status = "open"
                existing.resolution = None
                existing.resolved_at = None
            result = _as_dict(existing)
            result["deduplicated"] = True
            result["first_seen"] = existing.first_seen_at.isoformat() if existing.first_seen_at else ""
            return result
        row = Feedback(kind=kind, severity=severity, tool=tool, args=args or {}, repo=repo, path=path,
                       expected=expected, observed=observed, detail=detail, fingerprint=fingerprint)
        session.add(row)
        session.flush()
        result = _as_dict(row)
        result["deduplicated"] = False
        result["first_seen"] = row.first_seen_at.isoformat() if row.first_seen_at else ""
        return result


def list_feedback(status: str | None = None, severity: str | None = None, limit: int = 100) -> list[dict]:
    Feedback = _model("Feedback")
    conditions = []
    if status:
        conditions.append(Feedback.status == status)
    if severity:
        conditions.append(Feedback.severity == severity)
    with session_scope() as session:
        return [_as_dict(x) for x in session.query(Feedback).filter(*conditions).order_by(
            Feedback.occurrences.desc(), Feedback.last_seen_at.desc()
        ).limit(max(limit, 1)).all()]


def resolve_feedback(feedback_id: int, status: str, resolution: str) -> bool:
    Feedback = _model("Feedback")
    if status not in FEEDBACK_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with session_scope() as session:
        row = session.get(Feedback, feedback_id)
        if not row:
            return False
        row.status, row.resolution, row.resolved_at = status, resolution, datetime.now(UTC)
        return True


def feedback_summary() -> dict:
    Feedback = _model("Feedback")
    with session_scope() as session:
        rows = session.query(Feedback).all()
        return {"total": len(rows), "open": sum(x.status == "open" for x in rows),
                "resolved": sum(x.status == "resolved" for x in rows),
                "by_severity": dict(Counter(x.severity for x in rows))}


def duplicate_histories() -> list[dict]:
    Repo = _model("Repo")
    with session_scope() as session:
        repos = session.query(Repo).filter(
            Repo.is_enabled.is_(True), Repo.head_sha.is_not(None), Repo.commit_count > 0
        ).all()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for repo in repos:
            grouped[repo.head_sha].append(repo)
        return [{
            "head_sha": sha, "copies": len(rows),
            "commits": min(row.commit_count for row in rows),
            "names": [row.full_name for row in sorted(rows, key=lambda value: value.id)],
            "ids": [row.id for row in sorted(rows, key=lambda value: value.id)],
            "host": max(row.host for row in rows),
        } for sha, rows in grouped.items() if len(rows) > 1]


def directory_detail(dir_id: int) -> dict | None:
    Directory, Repo = _model("Directory"), _model("Repo")
    with session_scope() as session:
        return _row(session.query(Directory.id, Directory.path, Directory.repo_id,
                                  Repo.name.label("repo"), Repo.full_name, Repo.account_id)
                    .join(Repo, Repo.id == Directory.repo_id).filter(Directory.id == dir_id))


def directory_by_path(repo_id: int, path: str) -> dict | None:
    Directory = _model("Directory")
    with session_scope() as session:
        row = session.query(Directory).filter_by(repo_id=repo_id, path=path).one_or_none()
        return _as_dict(row) if row else None


def impact_pair(source_repo_id: int, target_repo_id: int) -> dict | None:
    Impact = _model("RepoImpact")
    with session_scope() as session:
        row = session.get(Impact, (source_repo_id, target_repo_id))
        return _as_dict(row) if row else None


def declared_dependency(dep_repo_id: int, consumer_repo_id: int) -> dict | None:
    Dependency = _model("RepoDependency")
    with session_scope() as session:
        row = session.query(Dependency).filter_by(
            dep_repo_id=dep_repo_id, consumer_repo_id=consumer_repo_id).one_or_none()
        return _as_dict(row) if row else None


def impact_edge_counts(repo_id: int, upstream: bool) -> dict:
    Impact = _model("RepoImpact")
    with session_scope() as session:
        query = session.query(Impact)
        side_name = "target_repo_id" if upstream else "source_repo_id"
        rows = query.filter_by(**{side_name: repo_id}).all()
        total = len(rows)
        validated = sum(row.is_declared or row.has_bump_history for row in rows)
        return {"total": int(total), "validated": int(validated)}


def impact_graph_data(min_score: float = 0.4, limit: int = 400) -> dict:
    Impact, Repo = _model("RepoImpact"), _model("Repo")
    with session_scope() as session:
        edges_rows = session.query(Impact).filter(Impact.score >= min_score).order_by(
            Impact.score.desc()).limit(max(limit, 1)).all()
        edges = [{"source": row.source_repo_id, "target": row.target_repo_id,
                  "score": row.score, "is_declared": row.is_declared,
                  "has_bump_history": row.has_bump_history, "bump_count": row.bump_count,
                  "median_adoption_days": row.median_adoption_days}
                 for row in edges_rows]
        ids = {x for edge in edges for x in (edge["source"], edge["target"])}
        nodes = []
        if ids:
            for repo in session.query(Repo).filter(Repo.id.in_(ids)).all():
                nodes.append({"id": repo.id, "basename": repo.name, "path": repo.full_name,
                              "dir_path": repo.primary_language or "", "extension": repo.primary_language,
                              "change_count": repo.commit_count, "is_deleted": False})
        return {"nodes": nodes, "edges": edges}


def repo_pair_bumps(consumer_id: int, dep_id: int, limit: int = 100) -> list[dict]:
    Bump, Commit = _model("DepBump"), _model("Commit")
    with session_scope() as session:
        rows = []
        bumps = session.query(Bump).filter_by(
            consumer_repo_id=consumer_id, dep_repo_id=dep_id
        ).order_by(Bump.bumped_at.desc().nullslast()).limit(max(limit, 1)).all()
        for bump in bumps:
            upstream = session.get(Commit, bump.dep_commit_id) if bump.dep_commit_id else None
            rows.append({"dep_name": bump.dep_name, "dep_version": bump.dep_version,
                         "manifest": bump.manifest, "ecosystem": bump.ecosystem,
                         "resolution": bump.resolution, "bumped_at": bump.bumped_at,
                         "consumer_sha": bump.consumer_sha, "dep_sha": bump.dep_sha,
                         "adoption_seconds": bump.adoption_seconds,
                         "upstream_sha": upstream.sha if upstream else None,
                         "upstream_subject": upstream.subject if upstream else None,
                         "upstream_at": upstream.committed_at if upstream else None})
        for row in rows:
            seconds = row.pop("adoption_seconds")
            row["adoption_days"] = round(seconds / 86400.0, 1) if seconds is not None else None
        return rows


def repo_dependencies(repo_id: int) -> dict:
    Dependency, Repo, Bump = _model("RepoDependency"), _model("Repo"), _model("DepBump")
    with session_scope() as session:
        declared = []
        for dep in session.query(Dependency).filter_by(consumer_repo_id=repo_id).all():
            dep_repo = session.get(Repo, dep.dep_repo_id) if dep.dep_repo_id else None
            declared.append({"dep_name": dep.dep_name, "dep_version": dep.dep_version,
                             "manifest": dep.manifest, "ecosystem": dep.ecosystem,
                             "dep_repo_id": dep.dep_repo_id,
                             "dep_repo": dep_repo.name if dep_repo else None})
        declared.sort(key=lambda x: (x["dep_repo_id"] is not None, x["dep_name"]))
        bumps = []
        grouped: dict[int, list[Any]] = defaultdict(list)
        for bump in session.query(Bump).filter(
            Bump.consumer_repo_id == repo_id, Bump.dep_repo_id.is_not(None)
        ).all():
            grouped[bump.dep_repo_id].append(bump)
        repos = {r.id: r for r in session.query(Repo).filter(Repo.id.in_(grouped)).all()}
        for dep_id, rows in grouped.items():
            lags = sorted(x.adoption_seconds for x in rows if x.adoption_seconds is not None)
            median = lags[len(lags) // 2] / 86400.0 if lags else None
            bumps.append({"dep_repo": repos[dep_id].name if dep_id in repos else None,
                          "dep_repo_id": dep_id, "bumps": len(rows),
                          "median_adoption_days": median,
                          "last_bump": max((x.bumped_at for x in rows if x.bumped_at), default=None)})
        bumps.sort(key=lambda x: x["bumps"], reverse=True)
        return {"declared": declared, "bumps": bumps}


def mining_overview() -> dict:
    Cluster, Drift, Risk, Impact, Bump = (_model(x) for x in ("FileCluster", "PairDrift", "FileRisk", "RepoImpact", "DepBump"))
    with session_scope() as session:
        modules = session.query(Cluster.repo_id, Cluster.cluster_id).distinct().subquery()
        spanning = session.query(Cluster.repo_id, Cluster.cluster_id).filter(
            Cluster.dirs_spanned > 1).distinct().subquery()
        trends = dict(session.query(Drift.trend, func.count()).group_by(Drift.trend).all())
        impact_edges, declared, bumped = session.query(
            func.count(), func.count().filter(Impact.is_declared.is_(True)),
            func.count().filter(Impact.has_bump_history.is_(True))).select_from(Impact).one()

        def total(model_or_subquery):
            return session.query(func.count()).select_from(model_or_subquery).scalar()

        return {"modules": total(modules),
                "clustered_files": total(Cluster),
                "cross_dir_modules": total(spanning),
                "emerging": trends.get("emerging", 0),
                "decaying": trends.get("decaying", 0),
                "stable": trends.get("stable", 0),
                "risk_scored": total(Risk),
                "impact_edges": impact_edges, "declared_edges": declared, "bump_edges": bumped,
                "dep_bumps": total(Bump)}
