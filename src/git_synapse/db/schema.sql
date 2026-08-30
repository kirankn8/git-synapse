-- Git Synapse schema: change-coupling statistics over git history.
--
-- DESIGN PRINCIPLE: store the atom, derive the rest.
--
-- The atomic fact in this system is a single (commit, file) row in
-- `commit_file`. Every number the product reports -- marginals, co-occurrence
-- counts, all 29 association measures, directory rollups, recency-weighted
-- variants -- is derivable from that table plus `commit`. The aggregate tables
-- below are materialised caches: they exist for query latency and can be
-- dropped and rebuilt at any time without data loss.
--
-- The practical consequence is that adding a 30th measure, changing the
-- fan-out cap, or switching to a different recency decay requires only a
-- recompute, never a re-clone or a re-parse.
--
-- This file is idempotent: every statement is IF NOT EXISTS or CREATE OR
-- REPLACE, so it is safe to run on every boot.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fast ILIKE path search
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- ===========================================================================
-- Accounts: the orgs and users whose repositories get discovered.
-- ===========================================================================

-- Discovery reads this table, so onboarding an account is a write rather than
-- a redeploy. The filters live per account because the reason to skip forks in
-- one org rarely applies to the next; the environment variables that used to
-- carry them are seeded here once and then ignored.
CREATE TABLE IF NOT EXISTS account (
    id                  BIGSERIAL PRIMARY KEY,
    -- The login exactly as GitHub spells it; lookups are case-insensitive.
    login               TEXT        NOT NULL,
    -- 'org' lists via /orgs/{login}/repos, 'user' via /users/{login}/repos.
    kind                TEXT        NOT NULL DEFAULT 'org'
                        CHECK (kind IN ('org', 'user')),
    -- Set for GitHub Enterprise; NULL means the public API.
    api_url             TEXT,
    enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    include_private     BOOLEAN     NOT NULL DEFAULT TRUE,
    include_forks       BOOLEAN     NOT NULL DEFAULT TRUE,
    include_archived    BOOLEAN     NOT NULL DEFAULT TRUE,
    -- Allowlist. When non-empty it overrides every other filter for this account.
    only_repos          TEXT[]      NOT NULL DEFAULT '{}',
    skip_repos          TEXT[]      NOT NULL DEFAULT '{}',
    last_discovered_at  TIMESTAMPTZ,
    last_discover_error TEXT,
    repo_count          BIGINT      NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-insensitive: GitHub treats logins that way, and two rows differing only
-- in case would discover the same repositories twice.
CREATE UNIQUE INDEX IF NOT EXISTS account_login_idx ON account (lower(login));

-- ===========================================================================
-- Repositories: the full GitHub record, plus ingest bookkeeping.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS repo (
    id                  BIGSERIAL PRIMARY KEY,
    github_id           BIGINT UNIQUE,
    owner               TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    full_name           TEXT        NOT NULL UNIQUE,

    -- GitHub descriptive metadata
    description         TEXT,
    homepage            TEXT,
    html_url            TEXT,
    clone_url           TEXT,
    ssh_url             TEXT,
    default_branch      TEXT,
    primary_language    TEXT,
    languages           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    topics              TEXT[]      NOT NULL DEFAULT '{}',
    license_spdx        TEXT,
    visibility          TEXT,
    is_private          BOOLEAN     NOT NULL DEFAULT FALSE,
    is_fork             BOOLEAN     NOT NULL DEFAULT FALSE,
    is_archived         BOOLEAN     NOT NULL DEFAULT FALSE,
    is_template         BOOLEAN     NOT NULL DEFAULT FALSE,
    is_disabled         BOOLEAN     NOT NULL DEFAULT FALSE,
    disk_usage_kb       BIGINT,
    stargazers          INTEGER     NOT NULL DEFAULT 0,
    watchers            INTEGER     NOT NULL DEFAULT 0,
    forks_count         INTEGER     NOT NULL DEFAULT 0,
    open_issues         INTEGER     NOT NULL DEFAULT 0,
    github_created_at   TIMESTAMPTZ,
    github_updated_at   TIMESTAMPTZ,
    github_pushed_at    TIMESTAMPTZ,
    raw_github          JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Mirror + ingest state
    mirror_path         TEXT,
    -- 'full' or 'blobless'. Blobless mirrors omit blob objects, so their
    -- commit_file rows carry no insertions/deletions and only exact renames
    -- are detected. Coupling statistics are unaffected either way.
    clone_mode          TEXT        NOT NULL DEFAULT 'full',
    has_churn           BOOLEAN     NOT NULL DEFAULT TRUE,
    mirror_size_kb      BIGINT,
    head_sha            TEXT,
    last_ingested_sha   TEXT,
    -- Tips of every branch at the end of the last successful ingest. The
    -- incremental walk excludes all of them, not just HEAD: `git log --all`
    -- covers every branch, so a single HEAD watermark would leave commits that
    -- are reachable only from other branches to be re-read on every run.
    last_ingested_refs  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_fetch_at       TIMESTAMPTZ,
    last_ingest_at      TIMESTAMPTZ,
    last_aggregate_at   TIMESTAMPTZ,
    -- Watermark for the manifest scan. Keyed on the HEAD sha rather than a
    -- timestamp: `last_ingest_at` advances on every run even when no commits
    -- landed, so a timestamp comparison never held and all 187 repositories were
    -- re-scanned nightly. A manifest can only change if HEAD moved.
    last_depbump_at     TIMESTAMPTZ,
    last_depbump_sha    TEXT,
    -- Watermark for the mining layer (clusters, drift, risk). Mining is by far
    -- the most expensive derived stage, and re-clustering a repository whose
    -- history has not moved produces byte-identical output.
    last_mining_at      TIMESTAMPTZ,
    ingest_status       TEXT        NOT NULL DEFAULT 'pending',
    ingest_error        TEXT,
    ingest_duration_s   DOUBLE PRECISION,
    is_enabled          BOOLEAN     NOT NULL DEFAULT TRUE,

    -- Denormalised history summary. Derived from commit/commit_file, cached
    -- here so the repo list renders without touching the fact tables.
    commit_count        BIGINT      NOT NULL DEFAULT 0,
    -- Commits that actually contributed pairs (i.e. survived the fan-out cap
    -- and the merge filter). This is the population size N used by every
    -- contingency table for this repo -- it is NOT commit_count.
    pair_population     BIGINT      NOT NULL DEFAULT 0,
    file_count          BIGINT      NOT NULL DEFAULT 0,
    author_count        BIGINT      NOT NULL DEFAULT 0,
    pair_count          BIGINT      NOT NULL DEFAULT 0,
    total_insertions    BIGINT      NOT NULL DEFAULT 0,
    total_deletions     BIGINT      NOT NULL DEFAULT 0,
    first_commit_at     TIMESTAMPTZ,
    last_commit_at      TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS repo_owner_idx        ON repo (owner);
CREATE INDEX IF NOT EXISTS repo_status_idx       ON repo (ingest_status);
CREATE INDEX IF NOT EXISTS repo_language_idx     ON repo (primary_language);
CREATE INDEX IF NOT EXISTS repo_pushed_idx       ON repo (github_pushed_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS repo_name_trgm_idx    ON repo USING gin (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS repo_topics_idx       ON repo USING gin (topics);

-- ===========================================================================
-- Authors: identity, deduplicated by lowercased email.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS author (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT        NOT NULL UNIQUE,
    display_name    TEXT,
    -- Alternate spellings of the same person's name seen in the history.
    known_names     TEXT[]      NOT NULL DEFAULT '{}',
    -- Optional manual identity merge: points at the surviving author row.
    canonical_id    BIGINT      REFERENCES author (id) ON DELETE SET NULL,
    commit_count    BIGINT      NOT NULL DEFAULT 0,
    first_commit_at TIMESTAMPTZ,
    last_commit_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS author_name_trgm_idx ON author USING gin (display_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS author_canonical_idx ON author (canonical_id);

-- ===========================================================================
-- Files: one row per canonical path per repo.
--
-- Renames are folded in: when git reports `R090 old new`, the old path becomes
-- a row in `file_alias` pointing at the same file id, so a file's history
-- survives being moved. `path` always holds the most recent known name.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS file (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             BIGINT      NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    path                TEXT        NOT NULL,
    dir_path            TEXT        NOT NULL DEFAULT '',
    basename            TEXT        NOT NULL DEFAULT '',
    extension           TEXT,
    depth               SMALLINT    NOT NULL DEFAULT 0,
    -- TRUE once the last observed change to this path was a delete.
    is_deleted          BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Marginal counts. `change_count` is n_a in every contingency table, and
    -- `pair_change_count` is the pair-eligible subset of it (commits that
    -- survived the fan-out cap). The measures use the latter so that marginals
    -- and joint counts come from the same population.
    change_count        BIGINT      NOT NULL DEFAULT 0,
    pair_change_count   BIGINT      NOT NULL DEFAULT 0,
    insertions          BIGINT      NOT NULL DEFAULT 0,
    deletions           BIGINT      NOT NULL DEFAULT 0,
    author_count        INTEGER     NOT NULL DEFAULT 0,
    first_change_at     TIMESTAMPTZ,
    last_change_at      TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_id, path)
);

CREATE INDEX IF NOT EXISTS file_repo_idx        ON file (repo_id);
CREATE INDEX IF NOT EXISTS file_dir_idx         ON file (repo_id, dir_path);
CREATE INDEX IF NOT EXISTS file_ext_idx         ON file (extension);
CREATE INDEX IF NOT EXISTS file_path_trgm_idx   ON file USING gin (path gin_trgm_ops);
CREATE INDEX IF NOT EXISTS file_churn_idx       ON file (repo_id, change_count DESC);

-- Historical paths that resolve to a current file row.
CREATE TABLE IF NOT EXISTS file_alias (
    repo_id     BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    old_path    TEXT   NOT NULL,
    file_id     BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    PRIMARY KEY (repo_id, old_path)
);

CREATE INDEX IF NOT EXISTS file_alias_file_idx ON file_alias (file_id);

-- ===========================================================================
-- Commits: one row per commit per repo.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS commit (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             BIGINT      NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    sha                 TEXT        NOT NULL,
    author_id           BIGINT      REFERENCES author (id) ON DELETE SET NULL,
    committer_id        BIGINT      REFERENCES author (id) ON DELETE SET NULL,
    authored_at         TIMESTAMPTZ NOT NULL,
    committed_at        TIMESTAMPTZ NOT NULL,
    subject             TEXT        NOT NULL DEFAULT '',
    body                TEXT,
    parent_count        SMALLINT    NOT NULL DEFAULT 0,
    is_merge            BOOLEAN     NOT NULL DEFAULT FALSE,
    n_files             INTEGER     NOT NULL DEFAULT 0,
    insertions          INTEGER     NOT NULL DEFAULT 0,
    deletions           INTEGER     NOT NULL DEFAULT 0,
    -- FALSE when the commit was excluded from pair generation (merge, or
    -- fan-out above max_files_per_commit). Kept explicit so the exclusion is
    -- auditable and reversible rather than silently lost at parse time.
    pair_eligible       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_id, sha)
);

CREATE INDEX IF NOT EXISTS commit_repo_time_idx  ON commit (repo_id, committed_at DESC);
CREATE INDEX IF NOT EXISTS commit_author_idx     ON commit (author_id);
CREATE INDEX IF NOT EXISTS commit_eligible_idx   ON commit (repo_id, pair_eligible) WHERE pair_eligible;
CREATE INDEX IF NOT EXISTS commit_time_brin_idx  ON commit USING brin (committed_at);

-- Commit DAG edges. Not needed by the coupling maths, but cheap to store and
-- required for any future branch/merge or lead-time analysis.
CREATE TABLE IF NOT EXISTS commit_parent (
    repo_id     BIGINT   NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    child_sha   TEXT     NOT NULL,
    parent_sha  TEXT     NOT NULL,
    ordinal     SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, child_sha, parent_sha)
);

-- ===========================================================================
-- Release tags: the bridge from a declared version to a commit.
--
-- A manifest that says `v1.2.3` names a release, not a commit, so without this
-- table every ecosystem that pins by version rather than by SHA -- Maven, NuGet,
-- Gradle, plain npm -- resolves to nothing at all. Filled from `for-each-ref`,
-- which peels annotated tags to their commit for us.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ref_tag (
    repo_id     BIGINT      NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    commit_sha  TEXT        NOT NULL,
    -- The tagger's date for an annotated tag, the committer's otherwise: when
    -- the release was cut, which is not the same as when the commit was written.
    tagged_at   TIMESTAMPTZ,
    annotated   BOOLEAN     NOT NULL DEFAULT FALSE,
    -- Resolved once the commit is ingested. Nullable because a tag can point at
    -- a commit outside the branch that ships, which the walk never reads.
    commit_id   BIGINT      REFERENCES commit (id) ON DELETE SET NULL,
    PRIMARY KEY (repo_id, name)
);

CREATE INDEX IF NOT EXISTS ref_tag_sha_idx    ON ref_tag (repo_id, commit_sha);
CREATE INDEX IF NOT EXISTS ref_tag_commit_idx ON ref_tag (commit_id);

-- ===========================================================================
-- commit_file: THE ATOMIC FACT TABLE.
--
-- Everything else in this schema is derivable from this table joined to
-- `commit`. Do not aggregate away from it.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS commit_file (
    commit_id       BIGINT   NOT NULL REFERENCES commit (id) ON DELETE CASCADE,
    file_id         BIGINT   NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    repo_id         BIGINT   NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    -- git's status letter: A add, M modify, D delete, R rename, C copy, T type change.
    change_type     CHAR(1)  NOT NULL DEFAULT 'M',
    insertions      INTEGER  NOT NULL DEFAULT 0,
    deletions       INTEGER  NOT NULL DEFAULT 0,
    is_binary       BOOLEAN  NOT NULL DEFAULT FALSE,
    -- For renames/copies: the path this file was moved from, verbatim.
    old_path        TEXT,
    -- Rename similarity score reported by git, 0-100.
    similarity      SMALLINT,
    PRIMARY KEY (commit_id, file_id)
);

CREATE INDEX IF NOT EXISTS commit_file_file_idx ON commit_file (file_id);
CREATE INDEX IF NOT EXISTS commit_file_repo_idx ON commit_file (repo_id);

-- ===========================================================================
-- Derived aggregate: file-pair joint counts.
--
-- Only the JOINT count lives here. The marginals come from file.pair_change_count
-- and the population size from repo.pair_population, which means a single pair
-- row is enough to reconstruct the entire 2x2 contingency table:
--     a = n_ab,  b = n_a - n_ab,  c = n_b - n_ab,  d = N - n_a - n_b + n_ab
-- ===========================================================================

CREATE TABLE IF NOT EXISTS file_pair (
    repo_id         BIGINT  NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    -- Canonically ordered so each unordered pair is stored exactly once.
    file_a_id       BIGINT  NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    file_b_id       BIGINT  NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    n_ab            BIGINT  NOT NULL DEFAULT 0,
    -- Exponentially recency-weighted co-occurrence, using the configured
    -- half-life. Lets a caller rank by "recently coupled" without a rescan.
    w_ab            DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_co_change TIMESTAMPTZ,
    last_co_change  TIMESTAMPTZ,
    distinct_authors INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, file_a_id, file_b_id),
    CHECK (file_a_id < file_b_id)
);

CREATE INDEX IF NOT EXISTS file_pair_a_idx      ON file_pair (file_a_id);
CREATE INDEX IF NOT EXISTS file_pair_b_idx      ON file_pair (file_b_id);
CREATE INDEX IF NOT EXISTS file_pair_support_idx ON file_pair (repo_id, n_ab DESC);

-- ===========================================================================
-- Derived aggregate: the 29 measures, materialised.
--
-- Pure function of (n_ab, n_a, n_b, N). Rebuildable from file_pair at any time
-- via `git-synapse score`. Materialised only so the UI can sort millions of pairs by
-- an arbitrary measure without recomputing.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS file_pair_metric (
    repo_id     BIGINT NOT NULL,
    file_a_id   BIGINT NOT NULL,
    file_b_id   BIGINT NOT NULL,

    -- Contingency cells, denormalised so a single row is self-describing and
    -- the API never has to join back to file/repo to explain a score.
    n_ab        BIGINT NOT NULL,
    n_a         BIGINT NOT NULL,
    n_b         BIGINT NOT NULL,
    n_total     BIGINT NOT NULL,

    -- Similarity & overlap
    jaccard                     DOUBLE PRECISION,
    dice                        DOUBLE PRECISION,
    sorensen                    DOUBLE PRECISION,
    ochiai                      DOUBLE PRECISION,
    simpson                     DOUBLE PRECISION,
    braun_blanquet              DOUBLE PRECISION,
    kulczynski                  DOUBLE PRECISION,
    fager                       DOUBLE PRECISION,
    -- Matching coefficients
    russell_rao                 DOUBLE PRECISION,
    sokal_michener              DOUBLE PRECISION,
    rogers_tanimoto             DOUBLE PRECISION,
    hamann                      DOUBLE PRECISION,
    faith                       DOUBLE PRECISION,
    -- Information theoretic
    mutual_information          DOUBLE PRECISION,
    pmi                         DOUBLE PRECISION,
    npmi                        DOUBLE PRECISION,
    ppmi                        DOUBLE PRECISION,
    -- Significance
    chi_square                  DOUBLE PRECISION,
    log_likelihood_ratio        DOUBLE PRECISION,
    t_score                     DOUBLE PRECISION,
    z_score                     DOUBLE PRECISION,
    poisson_significance        DOUBLE PRECISION,
    hypergeometric_significance DOUBLE PRECISION,
    -- Correlation
    phi                         DOUBLE PRECISION,
    cramers_v                   DOUBLE PRECISION,
    yules_q                     DOUBLE PRECISION,
    yules_y                     DOUBLE PRECISION,
    michael                     DOUBLE PRECISION,
    -- Probability / lift
    association_strength        DOUBLE PRECISION,
    -- Directional extras
    confidence_ab               DOUBLE PRECISION,
    confidence_ba               DOUBLE PRECISION,

    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, file_a_id, file_b_id)
);

-- Ranking indexes for the measures the UI defaults to. Adding an index for
-- every one of the 29 would cost more in write amplification than it saves.
CREATE INDEX IF NOT EXISTS fpm_npmi_idx    ON file_pair_metric (repo_id, npmi DESC);
CREATE INDEX IF NOT EXISTS fpm_llr_idx     ON file_pair_metric (repo_id, log_likelihood_ratio DESC);
CREATE INDEX IF NOT EXISTS fpm_jaccard_idx ON file_pair_metric (repo_id, jaccard DESC);
CREATE INDEX IF NOT EXISTS fpm_a_idx       ON file_pair_metric (file_a_id);
CREATE INDEX IF NOT EXISTS fpm_b_idx       ON file_pair_metric (file_b_id);

-- ===========================================================================
-- Derived aggregate: directory-level rollup.
--
-- Same maths one level up the tree. A directory "changes" in a commit if any
-- file under it changed, so the population and marginals differ from the file
-- level and cannot be summed from it -- they are computed independently.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS directory (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    path                TEXT   NOT NULL,
    depth               SMALLINT NOT NULL DEFAULT 0,
    file_count          INTEGER NOT NULL DEFAULT 0,
    change_count        BIGINT NOT NULL DEFAULT 0,
    pair_change_count   BIGINT NOT NULL DEFAULT 0,
    insertions          BIGINT NOT NULL DEFAULT 0,
    deletions           BIGINT NOT NULL DEFAULT 0,
    first_change_at     TIMESTAMPTZ,
    last_change_at      TIMESTAMPTZ,
    UNIQUE (repo_id, path)
);

CREATE INDEX IF NOT EXISTS directory_repo_idx ON directory (repo_id);
CREATE INDEX IF NOT EXISTS directory_churn_idx ON directory (repo_id, change_count DESC);

-- Which directories contain a file, one row per (file, ancestor directory).
-- Materialised from the path string so the directory rollup can be expressed as
-- a plain equi-join instead of a LIKE 'prefix%' scan, which would be
-- unusable at this row count.
CREATE TABLE IF NOT EXISTS file_directory (
    repo_id BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    file_id BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    dir_id  BIGINT NOT NULL REFERENCES directory (id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, dir_id)
);

CREATE INDEX IF NOT EXISTS file_directory_dir_idx  ON file_directory (dir_id);
CREATE INDEX IF NOT EXISTS file_directory_repo_idx ON file_directory (repo_id);

CREATE TABLE IF NOT EXISTS dir_pair (
    repo_id     BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    dir_a_id    BIGINT NOT NULL REFERENCES directory (id) ON DELETE CASCADE,
    dir_b_id    BIGINT NOT NULL REFERENCES directory (id) ON DELETE CASCADE,
    n_ab        BIGINT NOT NULL DEFAULT 0,
    w_ab        DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_co_change TIMESTAMPTZ,
    last_co_change  TIMESTAMPTZ,
    PRIMARY KEY (repo_id, dir_a_id, dir_b_id),
    CHECK (dir_a_id < dir_b_id)
);

CREATE INDEX IF NOT EXISTS dir_pair_a_idx ON dir_pair (dir_a_id);
CREATE INDEX IF NOT EXISTS dir_pair_b_idx ON dir_pair (dir_b_id);

CREATE TABLE IF NOT EXISTS dir_pair_metric (
    repo_id     BIGINT NOT NULL,
    dir_a_id    BIGINT NOT NULL,
    dir_b_id    BIGINT NOT NULL,
    n_ab        BIGINT NOT NULL,
    n_a         BIGINT NOT NULL,
    n_b         BIGINT NOT NULL,
    n_total     BIGINT NOT NULL,
    jaccard                     DOUBLE PRECISION,
    dice                        DOUBLE PRECISION,
    sorensen                    DOUBLE PRECISION,
    ochiai                      DOUBLE PRECISION,
    simpson                     DOUBLE PRECISION,
    braun_blanquet              DOUBLE PRECISION,
    kulczynski                  DOUBLE PRECISION,
    fager                       DOUBLE PRECISION,
    russell_rao                 DOUBLE PRECISION,
    sokal_michener              DOUBLE PRECISION,
    rogers_tanimoto             DOUBLE PRECISION,
    hamann                      DOUBLE PRECISION,
    faith                       DOUBLE PRECISION,
    mutual_information          DOUBLE PRECISION,
    pmi                         DOUBLE PRECISION,
    npmi                        DOUBLE PRECISION,
    ppmi                        DOUBLE PRECISION,
    chi_square                  DOUBLE PRECISION,
    log_likelihood_ratio        DOUBLE PRECISION,
    t_score                     DOUBLE PRECISION,
    z_score                     DOUBLE PRECISION,
    poisson_significance        DOUBLE PRECISION,
    hypergeometric_significance DOUBLE PRECISION,
    phi                         DOUBLE PRECISION,
    cramers_v                   DOUBLE PRECISION,
    yules_q                     DOUBLE PRECISION,
    yules_y                     DOUBLE PRECISION,
    michael                     DOUBLE PRECISION,
    association_strength        DOUBLE PRECISION,
    confidence_ab               DOUBLE PRECISION,
    confidence_ba               DOUBLE PRECISION,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, dir_a_id, dir_b_id)
);

CREATE INDEX IF NOT EXISTS dpm_npmi_idx ON dir_pair_metric (repo_id, npmi DESC);
CREATE INDEX IF NOT EXISTS dpm_llr_idx  ON dir_pair_metric (repo_id, log_likelihood_ratio DESC);

-- ===========================================================================
-- Author-to-file affinity. Free to compute from the atomic table, and it lets
-- an agent answer "who should review this change?" alongside "what else must
-- change?".
-- ===========================================================================

CREATE TABLE IF NOT EXISTS author_file (
    repo_id     BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    author_id   BIGINT NOT NULL REFERENCES author (id) ON DELETE CASCADE,
    file_id     BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    n_commits   BIGINT NOT NULL DEFAULT 0,
    insertions  BIGINT NOT NULL DEFAULT 0,
    deletions   BIGINT NOT NULL DEFAULT 0,
    first_at    TIMESTAMPTZ,
    last_at     TIMESTAMPTZ,
    PRIMARY KEY (repo_id, author_id, file_id)
);

CREATE INDEX IF NOT EXISTS author_file_file_idx   ON author_file (file_id, n_commits DESC);
CREATE INDEX IF NOT EXISTS author_file_author_idx ON author_file (author_id, n_commits DESC);

-- ===========================================================================
-- Job history, so the UI can show what ran, when, and whether it worked.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ingest_run (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT        NOT NULL,          -- discover | sync | aggregate | score | full
    trigger         TEXT        NOT NULL DEFAULT 'manual',  -- manual | schedule | api
    status          TEXT        NOT NULL DEFAULT 'running', -- running | success | failed | partial
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    duration_s      DOUBLE PRECISION,
    repos_total     INTEGER     NOT NULL DEFAULT 0,
    repos_ok        INTEGER     NOT NULL DEFAULT 0,
    repos_failed    INTEGER     NOT NULL DEFAULT 0,
    commits_added   BIGINT      NOT NULL DEFAULT 0,
    files_added     BIGINT      NOT NULL DEFAULT 0,
    pairs_written   BIGINT      NOT NULL DEFAULT 0,
    error           TEXT,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ingest_run_started_idx ON ingest_run (started_at DESC);

-- Per-repo detail for a run, so a failure is traceable to the repo that caused it.
CREATE TABLE IF NOT EXISTS ingest_run_repo (
    run_id          BIGINT      NOT NULL REFERENCES ingest_run (id) ON DELETE CASCADE,
    repo_id         BIGINT      NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    status          TEXT        NOT NULL,
    commits_added   BIGINT      NOT NULL DEFAULT 0,
    duration_s      DOUBLE PRECISION,
    error           TEXT,
    PRIMARY KEY (run_id, repo_id)
);

-- Cross-repo marginal for a file: how many eligible change sets touched it.
-- Distinct from file.pair_change_count, which counts commits, not change sets.
-- ---------------------------------------------------------------------------
-- Removals.
--
-- This file is otherwise all CREATE IF NOT EXISTS, which means deleting a table
-- from it does nothing to a database that already has one: the table simply
-- stays, holding stale rows that no code reads. Removals therefore have to be
-- stated, once, and left here.
--
-- These eight held cross-repository coupling inferred from tickets and
-- timestamps. That construction scored G2 = 570 between two public repositories
-- sharing no code at all, because two busy repositories occupy the same time
-- bins whatever they contain. It was replaced by the declared dependency graph.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS xrepo_file_pair_metric CASCADE;
DROP TABLE IF EXISTS xrepo_file_pair        CASCADE;
DROP TABLE IF EXISTS repo_pair_metric       CASCADE;
DROP TABLE IF EXISTS repo_pair              CASCADE;
DROP TABLE IF EXISTS repo_lag_metric        CASCADE;
DROP TABLE IF EXISTS repo_change_stats      CASCADE;
DROP TABLE IF EXISTS change_set_commit      CASCADE;
DROP TABLE IF EXISTS change_set             CASCADE;

ALTER TABLE file ADD COLUMN IF NOT EXISTS xrepo_change_count BIGINT NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- Dependency-bump edges recovered from manifest history.
--
-- A Go pseudo-version embeds the upstream commit it was cut from:
--     v3.0.0-20260626221153-5fc63d6f3055
--                           ^^^^^^^^^^^^ upstream commit
-- so a go.mod diff raising github.com/acme/signing/v3 to that version is
-- a dated, DIRECTIONAL, provable statement: "this commit consumed that signer
-- commit". These rows are ground truth, not inference, and exist to validate
-- the statistical measures rather than to replace them.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dep_bump (
    id              BIGSERIAL PRIMARY KEY,
    consumer_repo_id BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    consumer_sha    TEXT   NOT NULL,
    dep_repo_id     BIGINT REFERENCES repo (id) ON DELETE CASCADE,
    dep_name        TEXT   NOT NULL,
    dep_version     TEXT   NOT NULL,
    -- Upstream commit, when the version is a pseudo-version.
    dep_sha         TEXT,
    -- Resolved commit id in the dependency repo, when dep_sha matches one.
    dep_commit_id   BIGINT REFERENCES commit (id) ON DELETE SET NULL,
    manifest        TEXT   NOT NULL DEFAULT 'go.mod',
    bumped_at       TIMESTAMPTZ,
    -- Delay between the upstream commit and this consumer picking it up. The
    -- empirical propagation lag, and the thing the lagged analysis should see.
    lag_seconds     BIGINT,
    UNIQUE (consumer_repo_id, consumer_sha, dep_name, dep_version)
);

CREATE INDEX IF NOT EXISTS dep_bump_dep_idx      ON dep_bump (dep_repo_id, consumer_repo_id);
CREATE INDEX IF NOT EXISTS dep_bump_consumer_idx ON dep_bump (consumer_repo_id);
CREATE INDEX IF NOT EXISTS dep_bump_sha_idx      ON dep_bump (dep_sha);

-- ---------------------------------------------------------------------------
-- Declared dependency graph, read from each repo's manifest at HEAD.
--
-- Structural and present-tense, unlike `dep_bump` which is historical. This is
-- the CANDIDATE SET for impact prediction, and it is what makes the statistics
-- accurate: ranking 133 declared edges instead of 70,000 arbitrary ordered
-- pairs raises the base rate from 0.23% to 82%, a ~350x lift, before any
-- measure is evaluated.
--
-- Measured on this corpus: the best single measure over all ordered pairs
-- reaches AUC 0.80 but only 0.63 directional accuracy; the ensemble ranked
-- *within* this candidate set reaches 0.88 in sample, 0.69 held out in time.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo_dependency (
    consumer_repo_id BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    -- NULL when the module is not a repository we track.
    dep_repo_id      BIGINT REFERENCES repo (id) ON DELETE CASCADE,
    dep_name         TEXT   NOT NULL,
    dep_version      TEXT,
    -- Full path, not a basename: a monorepo declares different dependencies in
    -- gateway/go.mod than in model-controller/go.mod, and both matter.
    manifest         TEXT   NOT NULL DEFAULT 'go.mod',
    ecosystem        TEXT   NOT NULL DEFAULT 'go',
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_repo_id, dep_name, manifest)
);

CREATE INDEX IF NOT EXISTS repo_dependency_dep_idx ON repo_dependency (dep_repo_id);

-- ---------------------------------------------------------------------------
-- Intra-repository module graph.
--
-- A monorepo's real dependency structure lives in its own submodules, not in
-- cross-repo edges. `platform` has 14 go.mod files and every internal
-- reference in them points at itself (platform/apis, /gateway, /models,
-- /pkg) -- so cross-repo analysis correctly finds no upstream, and the structure
-- that actually matters was being discarded as a self-reference.
--
-- This is the same structural prior that lifts cross-repo ranking from AUC 0.80
-- to 0.88 in sample, applied inside a repository: "you changed gateway/, which declares
-- apis, models and pkg". Behavioural file coupling cannot state that as cleanly,
-- because a module boundary is a fact rather than a correlation.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS module_dependency (
    repo_id         BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    -- Directory owning the manifest. '' is the repository root.
    consumer_module TEXT   NOT NULL,
    -- Module path relative to the repository, as declared. '' is the root module.
    dep_module      TEXT   NOT NULL,
    manifest        TEXT   NOT NULL,
    ecosystem       TEXT   NOT NULL DEFAULT 'go',
    dep_version     TEXT,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, consumer_module, dep_module, manifest)
);

CREATE INDEX IF NOT EXISTS module_dependency_repo_idx ON module_dependency (repo_id);
CREATE INDEX IF NOT EXISTS module_dependency_dep_idx  ON module_dependency (repo_id, dep_module);

-- ---------------------------------------------------------------------------
-- Impact prediction: the ranked answer to "I am changing X, what else?"
--
-- One row per ordered (dependency -> consumer) pair, carrying the ensemble
-- score and the features behind it so any number can be explained. Rebuilt by
-- git_synapse.analysis.predict.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo_impact (
    source_repo_id  BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    target_repo_id  BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    -- Unweighted rank-average of the lagged measures, in [0, 1].
    score           DOUBLE PRECISION NOT NULL,
    rank_in_source  INTEGER NOT NULL,
    -- TRUE when the target declares the source as a dependency: the structural
    -- evidence that lifts precision so dramatically.
    is_declared     BOOLEAN NOT NULL DEFAULT FALSE,
    -- TRUE when a manifest bump has actually been observed, i.e. ground truth.
    has_bump_history BOOLEAN NOT NULL DEFAULT FALSE,
    bump_count      INTEGER NOT NULL DEFAULT 0,
    median_lag_days DOUBLE PRECISION,
    -- Lag at which the association was strongest, in bins.
    best_lag_bins   SMALLINT,
    bin_hours       SMALLINT,
    -- Per-measure contributions, so a score is always explainable.
    features        JSONB NOT NULL DEFAULT '{}'::jsonb,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_repo_id, target_repo_id)
);

CREATE INDEX IF NOT EXISTS repo_impact_source_idx ON repo_impact (source_repo_id, score DESC);
CREATE INDEX IF NOT EXISTS repo_impact_target_idx ON repo_impact (target_repo_id, score DESC);
CREATE INDEX IF NOT EXISTS repo_impact_declared_idx ON repo_impact (is_declared) WHERE is_declared;

-- ===========================================================================
-- FEEDBACK: defects in Git Synapse itself, reported by the sessions that use it.
--
-- This is the ONLY table any agent may write to, and it deliberately feeds
-- nothing. No aggregate, measure, score or ranking reads from it.
--
-- That boundary is the whole point. Letting sessions write into the coupling
-- data would close a confirmation loop -- Git Synapse suggests a pair, the agent
-- edits both files, the commit strengthens the pair, Git Synapse suggests it more
-- confidently -- and the statistic would drift from measuring the codebase to
-- measuring its own past advice. Agent-authored commits are already 15.8% of the
-- last week's history, so that risk is live rather than hypothetical.
--
-- What a session CAN usefully report is a defect: data that is missing, wrong or
-- stale, a tool that failed, a repository or path that should be covered and is
-- not. Every improvement made on the first day of use came from precisely that
-- observation, arrived at by hand. This is that path, written down.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS feedback (
    id              BIGSERIAL PRIMARY KEY,
    -- missing_data | wrong_data | stale_data | tool_error | coverage_gap | suggestion
    kind            TEXT        NOT NULL,
    severity        TEXT        NOT NULL DEFAULT 'medium',
    -- The MCP tool involved, and the arguments that produced the problem, so a
    -- report is reproducible rather than a recollection.
    tool            TEXT,
    args            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    repo            TEXT,
    path            TEXT,
    expected        TEXT,
    observed        TEXT,
    detail          TEXT,
    -- Deduplication key. The same defect hit by twenty sessions is one entry
    -- with a count of twenty, which is also a priority signal.
    fingerprint     TEXT        NOT NULL UNIQUE,
    occurrences     INTEGER     NOT NULL DEFAULT 1,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- open | investigating | fixed | wontfix
    status          TEXT        NOT NULL DEFAULT 'open',
    resolution      TEXT,
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS feedback_status_idx ON feedback (status, occurrences DESC);
CREATE INDEX IF NOT EXISTS feedback_kind_idx   ON feedback (kind);
CREATE INDEX IF NOT EXISTS feedback_seen_idx   ON feedback (last_seen_at DESC);

-- ===========================================================================
-- MINING LAYER
--
-- Derived analyses that answer architectural and risk questions rather than
-- "what changes with what". All rebuildable from the atomic facts.
-- ===========================================================================

-- De-facto modules: clusters found by label propagation over the file-coupling
-- graph. The interesting output is not the clustering itself but its
-- DISAGREEMENT with the directory tree -- files that behave as one module while
-- living in different folders.
CREATE TABLE IF NOT EXISTS file_cluster (
    repo_id      BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    file_id      BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    cluster_id   INTEGER NOT NULL,
    cluster_size INTEGER NOT NULL DEFAULT 0,
    -- Fraction of this file's coupling weight that stays inside its cluster.
    cohesion     DOUBLE PRECISION,
    -- Number of distinct top-level directories the cluster spans. > 1 means the
    -- de-facto module cuts across the declared structure.
    dirs_spanned INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (file_id)
);

CREATE INDEX IF NOT EXISTS file_cluster_repo_idx ON file_cluster (repo_id, cluster_id);

-- Coupling drift: is a relationship strengthening or decaying? Computed by
-- recomputing the same association on a recent window and a historical window
-- and differencing. A pair that was strongly coupled years ago but is not any
-- more is an artefact of a completed refactor, not live design coupling -- and
-- reporting it as current is one of the easier ways to mislead an agent.
CREATE TABLE IF NOT EXISTS pair_drift (
    repo_id        BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    file_a_id      BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    file_b_id      BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    window_days    INTEGER NOT NULL,
    n_ab_recent    BIGINT NOT NULL DEFAULT 0,
    n_ab_historic  BIGINT NOT NULL DEFAULT 0,
    npmi_recent    DOUBLE PRECISION,
    npmi_historic  DOUBLE PRECISION,
    delta          DOUBLE PRECISION,
    -- emerging | decaying | stable
    trend          TEXT NOT NULL DEFAULT 'stable',
    PRIMARY KEY (repo_id, file_a_id, file_b_id),
    CHECK (file_a_id < file_b_id)
);

CREATE INDEX IF NOT EXISTS pair_drift_trend_idx ON pair_drift (trend, delta DESC);
CREATE INDEX IF NOT EXISTS pair_drift_repo_idx  ON pair_drift (repo_id, delta DESC);

-- Per-file risk profile. Each component is a percentile within its repository,
-- so the composite is comparable across repos of very different sizes.
CREATE TABLE IF NOT EXISTS file_risk (
    file_id            BIGINT PRIMARY KEY REFERENCES file (id) ON DELETE CASCADE,
    repo_id            BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    churn_pct          DOUBLE PRECISION,
    coupling_pct       DOUBLE PRECISION,
    -- Herfindahl-Hirschman index over each author's share of commits to this
    -- file. 1.0 means a single author owns it entirely; ~0 means widely shared.
    ownership_hhi      DOUBLE PRECISION,
    -- Effective number of contributors, 1/HHI. The "bus factor" in the sense
    -- that matters: three authors at 98/1/1 has a bus factor near 1, not 3.
    effective_authors  DOUBLE PRECISION,
    author_count       INTEGER NOT NULL DEFAULT 0,
    partner_count      INTEGER NOT NULL DEFAULT 0,
    change_count       BIGINT  NOT NULL DEFAULT 0,
    days_since_change  INTEGER,
    -- Composite: high churn AND high coupling AND concentrated ownership.
    risk_score         DOUBLE PRECISION,
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS file_risk_repo_idx  ON file_risk (repo_id, risk_score DESC);
CREATE INDEX IF NOT EXISTS file_risk_score_idx ON file_risk (risk_score DESC);

-- ---------------------------------------------------------------------------
-- Referential integrity for the derived metric tables.
--
-- These tables are written by COPY in bulk, so they deliberately carry no
-- per-row foreign keys to `file`/`directory` -- that check would cost a lookup
-- on every one of millions of inserted rows.
--
-- They DO need a foreign key to `repo`, though. Without it, deleting a
-- repository cascades away its `file_pair` rows while leaving the matching
-- `file_pair_metric` rows orphaned, and `TRUNCATE repo ... CASCADE` silently
-- skips them entirely -- so a reset would leave stale scores behind that the
-- API would happily serve. The parent table has only a few hundred rows, so the
-- check is served from cache and costs almost nothing.
--
-- Added via DO blocks rather than inline so this file stays re-runnable.
-- ---------------------------------------------------------------------------

-- Added after v1, so existing deployments need the column backfilled.
ALTER TABLE repo ADD COLUMN IF NOT EXISTS last_ingested_refs JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE repo ADD COLUMN IF NOT EXISTS last_depbump_at TIMESTAMPTZ;
ALTER TABLE repo ADD COLUMN IF NOT EXISTS last_mining_at TIMESTAMPTZ;
ALTER TABLE repo ADD COLUMN IF NOT EXISTS last_depbump_sha TEXT;
-- Separate from last_depbump_sha because the bump scan and the declared-set
-- refresh are independent passes. Sharing one watermark meant the scan cleared
-- it before the refresh read it, so the declared set silently stopped tracking
-- HEAD and reported "no repository manifests changed" forever.
ALTER TABLE repo ADD COLUMN IF NOT EXISTS last_declared_sha TEXT;
-- The commit watermark is written in the same transaction as the commits, but
-- aggregation runs in later transactions. When one of those failed, the commits
-- were durable and the watermark had advanced, so the next run found nothing to
-- do and the repository's statistics stayed frozen at an arbitrary past point
-- while the run reported success. This lags until aggregation actually lands.
ALTER TABLE repo ADD COLUMN IF NOT EXISTS last_aggregate_sha TEXT;

-- Which account discovered this repository. Nullable because rows ingested
-- before accounts existed have no owner to attribute, and ON DELETE SET NULL
-- so removing an account never deletes the history mined from it.
ALTER TABLE repo ADD COLUMN IF NOT EXISTS account_id BIGINT
    REFERENCES account (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS repo_account_idx ON repo (account_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'file_pair_metric_repo_fk'
    ) THEN
        DELETE FROM file_pair_metric m
         WHERE NOT EXISTS (SELECT 1 FROM repo r WHERE r.id = m.repo_id);
        ALTER TABLE file_pair_metric
            ADD CONSTRAINT file_pair_metric_repo_fk
            FOREIGN KEY (repo_id) REFERENCES repo (id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'dir_pair_metric_repo_fk'
    ) THEN
        DELETE FROM dir_pair_metric m
         WHERE NOT EXISTS (SELECT 1 FROM repo r WHERE r.id = m.repo_id);
        ALTER TABLE dir_pair_metric
            ADD CONSTRAINT dir_pair_metric_repo_fk
            FOREIGN KEY (repo_id) REFERENCES repo (id) ON DELETE CASCADE;
    END IF;
END $$;

-- Simple key/value for schema version and other bookkeeping.
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Must equal SCHEMA_VERSION in db/engine.py. They are checked against each
-- other by a test, because they drifted twice: the constant was bumped and this
-- was not, so schema_is_current() was permanently false and every service boot
-- re-ran the whole DDL, taking exactly the locks the fast path exists to avoid.
INSERT INTO meta (key, value)
VALUES ('schema_version', '18'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
