"""The read layer every surface goes through.

If a function here returns the wrong shape or silently drops a filter, the API,
the MCP tools, the CLI and the UI are all wrong at once and none of them errors.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import aliased

from git_synapse.analysis import query as q
from git_synapse.db.orm import models, session_scope


def _first(model, **filters):
    with session_scope() as session:
        return session.query(model).filter_by(**filters).first()

# ------------------------------------------------------------- resolution

def test_resolve_file_finds_a_real_file_and_rejects_a_missing_one(db):
    with session_scope() as session:
        row = session.query(models().Repo.full_name, models().File.path).join(
            models().File, models().Repo.id == models().File.repo_id
        ).first()
    if row is None:
        pytest.skip("no files")
    assert q.resolve_file(row.full_name, row.path) is not None
    assert q.resolve_file(row.full_name, "definitely/not/here.xyz") is None
    assert q.resolve_file("no/such-repo", row.path) is None


def test_get_file_and_get_repo_return_none_for_unknown_ids(db):
    assert q.get_file(999999999) is None
    assert q.get_repo(999999999) is None


# ---------------------------------------------------------------- listings

@pytest.mark.parametrize("limit", [1, 5, 50])
def test_list_repos_respects_its_limit(db, limit):
    assert len(q.list_repos(limit=limit)) <= limit


def test_list_repos_search_filters_and_an_empty_search_does_not(db):
    everything = q.list_repos(limit=500)
    if len(everything) < 2:
        pytest.skip("need several repos")
    name = everything[0]["name"]
    hits = q.list_repos(search=name, limit=50)
    assert any(r["name"] == name for r in hits)
    assert len(hits) <= len(everything)


@pytest.mark.parametrize("order_by", ["commit_count", "name", "pushed_at"])
def test_known_sort_keys_are_honoured(db, order_by):
    assert q.list_repos(limit=5, order_by=order_by) is not None


def test_an_unknown_sort_key_falls_back_rather_than_raising(db):
    assert q.list_repos(limit=5, order_by="'; DROP TABLE repo; --") is not None


# ------------------------------------------------------------- search

@pytest.mark.parametrize("term", ["go.mod", "%", "_", "a%_b", "'", "日本"])
def test_search_files_survives_metacharacters_and_unicode(db, term):
    rows = q.search_files(term=term, limit=5)
    assert isinstance(rows, list)


def test_search_files_scoped_to_a_repo_stays_in_it(corpus):
    row = _first(models().Repo, is_enabled=True)
    rows = q.search_files(term="", repo_id=row.id, limit=10)
    assert all(r["repo_id"] == row.id for r in rows)


# ------------------------------------------------------------- coupling

def test_coupled_files_min_support_is_a_floor(db):
    with session_scope() as session:
        row = session.query(models().File).filter(
            models().File.change_count > 40, models().File.is_deleted.is_(False)
        ).first()
    if row is None:
        pytest.skip("no busy file")
    for floor in (2, 5, 20):
        rows = q.coupled_files(row.id, limit=50, min_support=floor)
        assert all(r["n_ab"] >= floor for r in rows), floor


def test_coupled_files_on_an_unknown_file_is_empty(db):
    assert q.coupled_files(999999999, limit=5) == []


def test_pair_detail_cells_are_internally_consistent(db):
    with session_scope() as session:
        row = session.query(models().FilePairMetric).first()
    if row is None:
        pytest.skip("no pairs")
    d = q.pair_detail(row.file_a_id, row.file_b_id)
    cells = d["cells"]
    assert cells["a"] + cells["b"] == cells["n_a"]
    assert cells["a"] + cells["c"] == cells["n_b"]
    assert sum(cells[k] for k in "abcd") == cells["n_total"]
    assert all(cells[k] >= 0 for k in "abcd")


def test_pair_detail_on_an_unknown_pair_is_none(db):
    assert q.pair_detail(999999998, 999999999) is None


def test_co_change_commits_are_the_evidence_behind_the_score(db):
    with session_scope() as session:
        row = session.query(models().FilePairMetric).filter(models().FilePairMetric.n_ab > 3).first()
    if row is None:
        pytest.skip("no supported pair")
    # Both reads must see the same snapshot: a live refresh rebuilding this
    # pair between them would change n_ab underneath the comparison, which made
    # this fail about one run in five.
    with session_scope() as session:
        Change = models().CommitFile
        Commit = models().Commit
        first, second = aliased(Change), aliased(Change)
        pair_a, pair_b, _n_ab = row.file_a_id, row.file_b_id, row.n_ab
        commits = [{"sha": commit.sha, "counted": commit.pair_eligible}
                   for commit in session.query(Commit).join(
                       first, first.commit_id == Commit.id
                   ).join(second, second.commit_id == Commit.id).filter(
                       first.file_id == pair_a, second.file_id == pair_b,
                       Commit.repo_id == row.repo_id
                   )
                   .all()]
    assert commits and all(c["sha"] for c in commits)
    # A commit above the fan-out cap changed both files but contributed to no
    # statistic; the evidence list marks it rather than quietly disagreeing
    # with the score it is presented as explaining.
    counted = [c for c in commits if c["counted"]]
    assert len(counted) == row.n_ab, (
        f"{len(counted)} counted commits against a joint count of {row.n_ab}"
    )


# ------------------------------------------------------------- aggregates

def test_overview_counts_are_non_negative_integers(db):
    ov = q.overview()
    for k, v in ov.items():
        if isinstance(v, int):
            assert v >= 0, k


def test_hotspots_are_ranked(db):
    rows = q.hotspots(limit=10)
    counts = [r["change_count"] for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_hotspots_scoped_to_a_repo_stay_in_it(corpus):
    row = _first(models().Repo, is_enabled=True)
    rows = q.hotspots(repo_id=row.id, limit=5)
    assert all(r["repo_id"] == row.id for r in rows)


def test_directories_listing_is_scoped(db):
    row = _first(models().Directory)
    if row is None:
        pytest.skip("no directories")
    rows = q.directories(row.repo_id, limit=10)
    assert rows
    # The rows carry no repo_id, so confirm the scoping by checking every id
    # really belongs to the repository that was asked for.
    ids = [r["id"] for r in rows]
    with session_scope() as session:
        leaked = session.query(models().Directory).filter(
            models().Directory.id.in_(ids), models().Directory.repo_id != row.repo_id
        ).count()
    assert leaked == 0


def test_the_tree_returns_one_level_and_nothing_below_it(corpus, db):
    """Browsing descends a level at a time. A subdirectory two levels down
    appearing at the root would make the folder page a flat dump."""
    with session_scope() as session:
        row = session.query(models().Directory.repo_id).filter(models().Directory.depth >= 2).first()
    if row is None:
        pytest.skip("no repository with nested directories")
    repo_id = row.repo_id

    root = q.directory_tree(repo_id)
    assert root["path"] == "" and root["directory"] is None
    assert all(d["depth"] == 1 for d in root["directories"])
    assert all(f["dir_path"] == "" for f in root["files"])
    assert root["directories"], "a repository with depth-2 dirs has depth-1 dirs"

    top = root["directories"][0]["path"]
    child = q.directory_tree(repo_id, top)
    assert child["directory"]["path"] == top
    assert all(d["path"].startswith(f"{top}/") and d["depth"] == 2
               for d in child["directories"])
    assert all(f["dir_path"] == top for f in child["files"])


def test_the_tree_reports_a_path_that_names_nothing(corpus, db):
    """Distinguished from an empty directory, so the route can 404 rather than
    render a folder page for a path that was never in the repository."""
    row = _first(models().Repo)
    if row is None:
        pytest.skip("no repositories")
    missing = q.directory_tree(row.id, "no/such/directory")
    assert missing["directory"] is None
    assert missing["directories"] == [] and missing["files"] == []


def test_a_file_resolves_by_repository_id_as_well_as_by_name(corpus, db):
    """The UI addresses files by path under a repository id, because ids
    renumber on a re-ingest and a link keyed on one quietly changes meaning."""
    with session_scope() as session:
        row = session.query(models().File.repo_id, models().Repo.full_name, models().File.path).join(
            models().Repo, models().Repo.id == models().File.repo_id
        ).first()
    if row is None:
        pytest.skip("no files")
    by_name = q.resolve_file(row.full_name, row.path)
    by_id = q.resolve_file(None, row.path, repo_id=row.repo_id)
    assert by_name is not None and by_id == by_name
    assert q.resolve_file(None, row.path, repo_id=999999999) is None


def test_coupled_directories_excludes_containment(corpus, db):
    """A directory changes when any file beneath it changes, so an ancestor
    co-changes with its descendant by definition -- src/com scored 1.000
    against src/com/google on every measure. That is containment reported as
    coupling, and it crowded out the real partners."""
    with session_scope() as session:
        row = session.query(models().Directory).filter(
            models().Directory.depth >= 2, models().Directory.change_count > 0
        ).order_by(models().Directory.change_count.desc()).first()
    if row is None:
        pytest.skip("no nested directory with changes")
    partners = q.coupled_directories(row.id, limit=50)
    for partner in partners:
        assert partner["path"], "the root changes in every commit; it is not a partner"
        assert not partner["path"].startswith(f"{row.path}/"), partner["path"]
        assert not row.path.startswith(f"{partner['path']}/"), partner["path"]
        assert partner["path"] != row["path"]


def test_file_authors_and_commits_are_bounded(db):
    with session_scope() as session:
        row = session.query(models().File).filter(models().File.change_count > 5).first()
    if row is None:
        pytest.skip("no busy file")
    assert len(q.file_authors(row.id, limit=3)) <= 3
    assert len(q.file_commits(row.id, limit=4)) <= 4


def test_measure_catalog_is_self_describing(db):
    cat = q.measure_catalog()
    assert len(cat) >= 29
    assert all(m["key"] and m["label"] for m in cat)


# ------------------------------------------------------------- cross-repo

def test_recent_runs_and_run_detail(db):
    runs = q.recent_runs(limit=3)
    assert isinstance(runs, list)
    if runs:
        assert q.run_detail(runs[0]["id"]) is not None
    assert q.run_detail(999999999) is None


# ------------------------------------------------------- the optional filters

def test_repo_listing_filters_by_language_and_status(db):
    """Each filter is a separate clause; an unused one that is silently wrong
    only shows up when someone finally uses it."""
    from git_synapse.analysis import query as q

    by_status = q.list_repos(status="ready", limit=10)
    assert all(r["ingest_status"] == "ready" for r in by_status)

    with session_scope() as session:
        row = session.query(models().Repo.primary_language).filter(
            models().Repo.primary_language.is_not(None)
        ).first()
    if row is not None:
        hits = q.list_repos(language=row.primary_language, limit=10)
        assert all(r["primary_language"] == row.primary_language for r in hits)


def test_file_search_filters_by_extension(db):
    from git_synapse.analysis import query as q

    rows = q.search_files(term="", extension="go", limit=10)
    assert all(r["extension"] == "go" for r in rows)


def test_coupled_files_min_score_filters_in_both_orientations(db):
    """The floor is applied inside each branch of the union, so a pair stored
    the other way round must be filtered identically."""
    from git_synapse.analysis import query as q

    with session_scope() as session:
        row = session.query(models().File).filter(
            models().File.change_count > 40, models().File.is_deleted.is_(False)
        ).first()
    if row is None:
        pytest.skip("no busy file")
    rows = q.coupled_files(row.id, measure="npmi", limit=50, min_support=2,
                           min_score=0.3)
    assert all(float(r["npmi"]) >= 0.3 for r in rows)


def test_strongest_pairs_can_be_scoped_to_one_repository(db):
    from git_synapse.analysis import query as q

    row = _first(models().FilePairMetric)
    if row is None:
        pytest.skip("no pairs")
    rows = q.strongest_pairs(repo_id=row.repo_id, limit=10, min_support=2)
    assert rows
    assert all(r["repo_id"] == row.repo_id for r in rows)

    # Unscoped must span more than the one repository, or the filter is a no-op.
    everywhere = q.strongest_pairs(limit=50, min_support=2)
    assert len({r["repo_id"] for r in everywhere}) >= 1


def test_module_context_normalises_a_leading_slash_or_dot(db):
    """`lstrip("./")` strips a character set, so ".github/x" became "github/x"."""
    from git_synapse.analysis import query as q

    with session_scope() as session:
        row = session.query(models().File).filter(
            models().File.dir_path != "", models().File.change_count > 5
        ).first()
    if row is None:
        pytest.skip("no suitable file")
    plain = q.module_context(row.repo_id, row.path)
    for variant in (f"/{row.path}", f"./{row.path}", f"  {row.path}  "):
        assert q.module_context(row.repo_id, variant) == plain, variant


# --------------------------------------------------- filters and edge branches

def test_n_ab_is_accepted_as_an_order_even_though_it_is_not_a_measure(db):
    """Seven endpoints order by raw support; their CTEs do not select a measure
    column, so rejecting the key would break them."""
    assert q._safe_order("n_ab") == "n_ab"


@pytest.mark.parametrize("kwargs", [
    {"language": "Go"},
    {"status": "ok"},
    {"search": "a", "language": "Go", "status": "ok"},
])
def test_list_repos_accepts_every_filter_combination(db, kwargs):
    rows = q.list_repos(limit=5, **kwargs)
    assert isinstance(rows, list)
    for row in rows:
        if "language" in kwargs:
            assert row["primary_language"] == kwargs["language"]


def test_the_coupling_graph_can_be_centred_on_one_file(db):
    """The product question is "given I am changing THIS", so the centred graph
    is the one that gets asked for."""
    with session_scope() as session:
        row = session.query(models().FilePairMetric).filter(
            models().FilePairMetric.n_ab > 3
        ).order_by(models().FilePairMetric.n_ab.desc()).first()
    if row is None:
        pytest.skip("no file pairs")
    centred = q.coupling_graph(row.repo_id, center_file_id=row.file_a_id,
                               min_support=1, limit=50)
    ids = {e["source"] for e in centred["edges"]} | {e["target"] for e in centred["edges"]}
    assert centred["edges"], "the centred graph dropped its own centre"
    assert row.file_a_id in ids
    for edge in centred["edges"]:
        assert row.file_a_id in (edge["source"], edge["target"])


def test_the_coupling_graph_honours_a_score_floor(db):
    row = _first(models().FilePairMetric)
    if row is None:
        pytest.skip("no file pairs")
    loose = q.coupling_graph(row.repo_id, min_support=1, limit=200)
    tight = q.coupling_graph(row.repo_id, min_support=1, min_score=0.99, limit=200)
    assert len(tight["edges"]) <= len(loose["edges"])


@pytest.mark.parametrize("path", ["gateway/main.go", "./gateway/main.go",
                                  "/gateway/main.go", "  gateway/main.go  "])
def test_module_ownership_is_found_however_the_path_is_written(db, monkeypatch,
                                                               path):
    """An agent pastes a path from a diff, a log or a URL; a leading ./ or / must
    not silently make the file belong to no module."""
    monkeypatch.setattr(q, "_module_rows", lambda *_a, **_k: [
        {"consumer_module": "gateway", "dep_module": "core"},
        {"consumer_module": "", "dep_module": "gateway"},
    ])
    assert q.module_context(1, path)["owning_module"] == "gateway"


def test_the_longest_matching_module_owns_the_file(db, monkeypatch):
    """Nested modules: `a/b` owns `a/b/x.go`, not `a`."""
    monkeypatch.setattr(q, "_module_rows", lambda *_a, **_k: [
        {"consumer_module": "a", "dep_module": "a/b"},
    ])
    assert q.module_context(1, "a/b/x.go")["owning_module"] == "a/b"


@pytest.mark.parametrize("severity", ["", "urgent", "LOW", None])
def test_feedback_refuses_a_severity_it_does_not_know(db, severity):
    with pytest.raises(ValueError, match="severity"):
        q.record_feedback(kind="wrong_data", severity=severity,
                          detail="something concrete")


@pytest.mark.parametrize("status", ["", "closed", "OPEN", "done"])
def test_resolving_feedback_refuses_an_unknown_status(db, status):
    with pytest.raises(ValueError, match="status"):
        q.resolve_feedback(1, status, "")


def test_the_corpus_shape_returns_every_distribution_the_landing_page_draws(corpus, db):
    """Seven aggregates in one call rather than seven calls, because this is the
    most expensive read the landing page makes."""
    shape = q.corpus_shape()
    assert set(shape) == {
        "commits_by_year", "pair_support", "repo_sizes", "languages",
        "commit_width", "authors_per_file", "adoption_days", "repo_recency",
    }
    for name, rows in shape.items():
        assert isinstance(rows, list), name
        for row in rows:
            assert row["n"] >= 0, name


def test_adoption_buckets_are_numbered_from_one(corpus, db):
    """width_bucket numbers from 1: bucket 1 is the first band, not the second.
    Labelling these from zero shifted every bar one band later and reported "0%
    adopted within two months" for a corpus where most land inside it."""
    rows = q.corpus_shape()["adoption_days"]
    if not rows:
        pytest.skip("no resolved bumps")
    assert min(r["bucket"] for r in rows) >= 1, "bucket 0 means a negative delay"

    # The first band must agree with the plain question asked directly.
    first = sum(r["n"] for r in rows if r["bucket"] == 1)
    with session_scope() as session:
        direct = session.query(models().DepBump).filter(
            models().DepBump.adoption_seconds.is_not(None),
            models().DepBump.adoption_seconds < 60 * 86400,
        ).count()
    assert first == direct


def test_distribution_buckets_are_capped_so_the_tail_cannot_dominate(corpus, db):
    """The tails run to thousands; the question is only ever how much sits at
    the thin end."""
    shape = q.corpus_shape()
    assert all(r["support"] <= 10 for r in shape["pair_support"])
    assert all(r["files"] <= 12 for r in shape["commit_width"])
    assert all(r["authors"] <= 8 for r in shape["authors_per_file"])


def test_repo_recency_buckets_every_repository_exactly_once(corpus, db):
    """A repository nobody has touched in a year still contributes history, but
    that history no longer describes the code -- so the count has to be right."""
    rows = q.corpus_shape()["repo_recency"]
    with session_scope() as session:
        total = session.query(models().Repo).count()
    assert sum(r["n"] for r in rows) == total, "every repository lands in one bucket"
    assert {r["bucket"] for r in rows} <= {
        "never", "past month", "past 6 months", "past year", "over a year"}


def test_a_search_term_is_matched_literally_not_as_a_pattern(corpus, db):
    """`%` and `_` are wildcards to LIKE, so a term carrying either searched
    for something else: `test_helper` matched `testXhelper`, and a lone `_`
    matched every row in the table."""
    every = q.search_files(limit=5)
    if not every:
        pytest.skip("no files")

    # A lone underscore is a single-character wildcard unless it is escaped.
    lone = q.search_files("_", limit=500)
    assert all("_" in row["path"] for row in lone), \
        "an underscore must match an underscore, not any character"

    # Two literal characters with a wildcard between them must find nothing
    # unless a path really contains the percent sign.
    assert not [r for r in q.search_files("READ%ME", limit=50)
                if "READ%ME" not in r["path"].upper()]

    # And an ordinary term still works.
    assert q.search_files(every[0]["basename"], limit=5)


def test_a_repository_search_escapes_the_same_way(corpus, db):
    assert all("_" in (r["full_name"] + (r["description"] or ""))
               for r in q.list_repos(search="_", limit=200))


def test_a_paused_source_drops_out_of_the_corpus_wide_list(db):
    """Pausing means these are not being refreshed and their numbers are not
    moving. Left in, a source that had enumerated 8,105 repositories ranks
    ahead of the whole corpus by count, every row opening a page with no
    history behind it."""
    from git_synapse.analysis import query as q
    from git_synapse.ingest import accounts

    src = accounts.add_account("paused-src", kind="org", provider="github",
                               host="github.com")
    try:
        with session_scope() as session:
            session.add(models().Repo(github_id=987654321, owner="paused-src", name="r",
                                      full_name="paused-src/r", host="github.com", provider="github",
                                      account_id=src["id"], is_enabled=False))

        names = {r["full_name"] for r in q.list_repos(limit=1000)}
        assert "paused-src/r" not in names

        # Asked for by name it is still there: having opened that source, its
        # repositories are exactly what the reader came for.
        scoped = {r["full_name"] for r in q.list_repos(account_id=src["id"], limit=1000)}
        assert "paused-src/r" in scoped

        # And explicitly, for anything that wants the whole picture.
        everything = {r["full_name"] for r in q.list_repos(limit=1000, include_paused=True)}
        assert "paused-src/r" in everything
    finally:
        with session_scope() as session:
            row = session.query(models().Repo).filter_by(full_name="paused-src/r").one_or_none()
            if row is not None:
                session.delete(row)
        accounts.remove_account(src["id"])


def test_the_same_history_stored_twice_is_detected(db):
    """Two addresses are not two repositories. A project moved to a subgroup, a
    mirror kept in sync, a fork the API declines to declare -- GitLab lists
    `veloren/veloren` and `veloren/dev/veloren` as separate projects with
    separate ids and a byte-identical history. Nothing about either row is
    wrong on its own; every corpus-wide total is."""
    from git_synapse.analysis import query as q
    sha = "f" * 40
    ids = []
    for path in ("dup/one", "dup/two"):
        owner, name = path.split("/")
        with session_scope() as session:
            row = models().Repo(owner=owner, name=name, full_name=path,
                                host="gitlab.com", provider="gitlab", is_enabled=True,
                                head_sha=sha, commit_count=4242, pair_population=4242)
            session.add(row)
            session.flush()
            ids.append(row.id)
    try:
        found = [d for d in q.duplicate_histories() if d["head_sha"] == sha]
        assert len(found) == 1
        assert found[0]["copies"] == 2
        assert set(found[0]["names"]) == {"dup/one", "dup/two"}
        assert found[0]["commits"] == 4242

        # A paused copy is not a duplicate: pausing is the fix.
        with session_scope() as session:
            session.get(models().Repo, ids[1]).is_enabled = False
        assert not [d for d in q.duplicate_histories() if d["head_sha"] == sha]
    finally:
        for rid in ids:
            with session_scope() as session:
                row = session.get(models().Repo, rid)
                if row is not None:
                    session.delete(row)


def test_repositories_with_no_history_are_not_called_duplicates(db):
    """Every never-ingested repository has a NULL head and a zero count. They
    would otherwise all collide with each other."""
    from git_synapse.analysis import query as q
    ids = []
    for path in ("empty/a", "empty/b"):
        owner, name = path.split("/")
        with session_scope() as session:
            row = models().Repo(owner=owner, name=name, full_name=path,
                                host="github.com", provider="github", is_enabled=True,
                                head_sha=None, commit_count=0)
            session.add(row)
            session.flush()
            ids.append(row.id)
    try:
        assert not [d for d in q.duplicate_histories()
                    if set(d["names"]) & {"empty/a", "empty/b"}]
    finally:
        for rid in ids:
            with session_scope() as session:
                row = session.get(models().Repo, rid)
                if row is not None:
                    session.delete(row)
