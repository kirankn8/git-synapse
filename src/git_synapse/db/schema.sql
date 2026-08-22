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

-- ===========================================================================
-- CROSS-REPOSITORY COUPLING
--
-- Within one repository, "changed together" means "in the same commit". Two
-- repositories never share a commit, so cross-repo coupling needs a wider unit
-- of work: the CHANGE SET, which is to cross-repo analysis exactly what
-- `commit` is to within-repo analysis.
--
-- Every pair-eligible commit belongs to exactly one change set, so the change
-- sets form a partition and the population size N is unambiguous. Two ways a
-- change set is formed, in priority order:
--
--   1. `ticket`   -- the commit subject carries an issue key (ACME-1234). All
--                    commits sharing that key are one change set. Precise, but
--                    only ~14% of commits are keyed, and coverage is very
--                    uneven (telemetry 37%, signer 0%).
--   2. `temporal` -- otherwise, consecutive commits by the same author with no
--                    gap longer than SESSION_GAP_HOURS form a work session.
--                    Catches repos with no commit-message discipline, at the
--                    cost of noise when someone touches unrelated repos in one
--                    afternoon.
--
-- SINGLE-REPO CHANGE SETS ARE DELIBERATELY KEPT. It is tempting to store only
-- the multi-repo ones, but the contingency table needs the cells where repo A
-- changed *without* repo B (b and c). Dropping single-repo change sets would
-- make every change set multi-repo, drive b and c toward zero, and inflate
-- every coupling score toward 1.0.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS change_set (
    id              BIGSERIAL PRIMARY KEY,
    -- Stable natural key: 'ticket:ACME-1234' or 'session:<author>:<n>'.
    key             TEXT        NOT NULL UNIQUE,
    signal          TEXT        NOT NULL,          -- ticket | temporal
    ticket          TEXT,                           -- issue key, when signal='ticket'
    author_id       BIGINT      REFERENCES author (id) ON DELETE SET NULL,
    n_commits       INTEGER     NOT NULL DEFAULT 0,
    n_repos         INTEGER     NOT NULL DEFAULT 0,
    n_files         INTEGER     NOT NULL DEFAULT 0,
    first_at        TIMESTAMPTZ,
    last_at         TIMESTAMPTZ,
    -- FALSE when the change set spans more than MAX_REPOS_PER_CHANGESET. An
    -- org-wide dependabot sweep touching 61 repos is not a design signal, and
    -- it would contribute O(k^2) repo pairs. Stored either way, so the
    -- exclusion stays auditable -- same contract as commit.pair_eligible.
    pair_eligible   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS change_set_signal_idx   ON change_set (signal);
CREATE INDEX IF NOT EXISTS change_set_ticket_idx   ON change_set (ticket);
CREATE INDEX IF NOT EXISTS change_set_repos_idx    ON change_set (n_repos DESC);
CREATE INDEX IF NOT EXISTS change_set_eligible_idx ON change_set (pair_eligible) WHERE pair_eligible;
CREATE INDEX IF NOT EXISTS change_set_time_idx     ON change_set (last_at DESC);

-- Which commits make up a change set. Files are reached through commit_file,
-- so no file ids are duplicated here.
CREATE TABLE IF NOT EXISTS change_set_commit (
    change_set_id   BIGINT NOT NULL REFERENCES change_set (id) ON DELETE CASCADE,
    commit_id       BIGINT NOT NULL REFERENCES commit (id) ON DELETE CASCADE,
    repo_id         BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    PRIMARY KEY (change_set_id, commit_id)
);

CREATE INDEX IF NOT EXISTS change_set_commit_commit_idx ON change_set_commit (commit_id);
CREATE INDEX IF NOT EXISTS change_set_commit_repo_idx   ON change_set_commit (repo_id, change_set_id);

-- Marginal: how many eligible change sets touched each repository. This is the
-- n_a of every repo-level contingency table.
CREATE TABLE IF NOT EXISTS repo_change_stats (
    repo_id             BIGINT PRIMARY KEY REFERENCES repo (id) ON DELETE CASCADE,
    change_set_count    BIGINT NOT NULL DEFAULT 0,
    ticket_set_count    BIGINT NOT NULL DEFAULT 0,
    first_at            TIMESTAMPTZ,
    last_at             TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Repo-level coupling: "changing signer implies changing packager".
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo_pair (
    repo_a_id       BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    repo_b_id       BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    n_ab            BIGINT NOT NULL DEFAULT 0,
    -- How much of the joint evidence came from precise ticket links rather
    -- than temporal proximity. Lets the UI show "12 of 47 are ticket-linked"
    -- so a reader can discount a pair built purely on same-afternoon activity.
    n_ab_ticket     BIGINT NOT NULL DEFAULT 0,
    w_ab            DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_co_change TIMESTAMPTZ,
    last_co_change  TIMESTAMPTZ,
    distinct_authors INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_a_id, repo_b_id),
    CHECK (repo_a_id < repo_b_id)
);

CREATE INDEX IF NOT EXISTS repo_pair_a_idx ON repo_pair (repo_a_id);
CREATE INDEX IF NOT EXISTS repo_pair_b_idx ON repo_pair (repo_b_id);
CREATE INDEX IF NOT EXISTS repo_pair_support_idx ON repo_pair (n_ab DESC);

CREATE TABLE IF NOT EXISTS repo_pair_metric (
    repo_a_id   BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    repo_b_id   BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
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
    PRIMARY KEY (repo_a_id, repo_b_id)
);

CREATE INDEX IF NOT EXISTS rpm_npmi_idx ON repo_pair_metric (npmi DESC);
CREATE INDEX IF NOT EXISTS rpm_llr_idx  ON repo_pair_metric (log_likelihood_ratio DESC);
-- Directional index for chain traversal, which walks outward from one repo.
CREATE INDEX IF NOT EXISTS rpm_conf_a_idx ON repo_pair_metric (repo_a_id, confidence_ab DESC);
CREATE INDEX IF NOT EXISTS rpm_conf_b_idx ON repo_pair_metric (repo_b_id, confidence_ba DESC);

-- ---------------------------------------------------------------------------
-- File-level coupling across repositories: which specific file in repo A goes
-- with which specific file in repo B. This is what an agent actually needs --
-- "the contracts spec changed, so this telemetry handler probably needs updating".
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS xrepo_file_pair (
    file_a_id       BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    file_b_id       BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    repo_a_id       BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    repo_b_id       BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    n_ab            BIGINT NOT NULL DEFAULT 0,
    n_ab_ticket     BIGINT NOT NULL DEFAULT 0,
    w_ab            DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_co_change TIMESTAMPTZ,
    last_co_change  TIMESTAMPTZ,
    PRIMARY KEY (file_a_id, file_b_id),
    -- Ordered by file id, which also guarantees each unordered pair once.
    CHECK (file_a_id < file_b_id),
    -- Same-repo pairs belong in file_pair, not here.
    CHECK (repo_a_id <> repo_b_id)
);

CREATE INDEX IF NOT EXISTS xfp_a_idx     ON xrepo_file_pair (file_a_id);
CREATE INDEX IF NOT EXISTS xfp_b_idx     ON xrepo_file_pair (file_b_id);
CREATE INDEX IF NOT EXISTS xfp_repos_idx ON xrepo_file_pair (repo_a_id, repo_b_id);
CREATE INDEX IF NOT EXISTS xfp_support_idx ON xrepo_file_pair (n_ab DESC);

CREATE TABLE IF NOT EXISTS xrepo_file_pair_metric (
    file_a_id   BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    file_b_id   BIGINT NOT NULL REFERENCES file (id) ON DELETE CASCADE,
    repo_a_id   BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    repo_b_id   BIGINT NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
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
    PRIMARY KEY (file_a_id, file_b_id)
);

CREATE INDEX IF NOT EXISTS xfpm_npmi_idx  ON xrepo_file_pair_metric (npmi DESC);
CREATE INDEX IF NOT EXISTS xfpm_llr_idx   ON xrepo_file_pair_metric (log_likelihood_ratio DESC);
CREATE INDEX IF NOT EXISTS xfpm_a_idx     ON xrepo_file_pair_metric (file_a_id, confidence_ab DESC);
CREATE INDEX IF NOT EXISTS xfpm_b_idx     ON xrepo_file_pair_metric (file_b_id, confidence_ba DESC);
CREATE INDEX IF NOT EXISTS xfpm_repos_idx ON xrepo_file_pair_metric (repo_a_id, repo_b_id);

-- Cross-repo marginal for a file: how many eligible change sets touched it.
-- Distinct from file.pair_change_count, which counts commits, not change sets.
ALTER TABLE file ADD COLUMN IF NOT EXISTS xrepo_change_count BIGINT NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- Directed, time-lagged coupling.
--
-- Unlike every other pair table here, this one is DIRECTED: (A, B) and (B, A)
-- are different rows, and there is no a < b constraint. The lag is what makes
-- direction meaningful -- "A changed, and B changed `lag_bins` later".
--
-- See git_synapse/analysis/lagged.py for the construction. In short: time is binned,
-- each repo becomes a binary vector over bins, and the 2x2 table is formed
-- between A's vector and B's vector shifted by `lag_bins`. All 29 measures then
-- apply unchanged, but become directional.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo_lag_metric (
    repo_a_id   BIGINT   NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    repo_b_id   BIGINT   NOT NULL REFERENCES repo (id) ON DELETE CASCADE,
    -- Bins that B lags behind A. 0 means the same bin (simultaneous).
    lag_bins    SMALLINT NOT NULL,
    bin_hours   SMALLINT NOT NULL,
    n_ab        BIGINT   NOT NULL,
    n_a         BIGINT   NOT NULL,
    n_b         BIGINT   NOT NULL,
    n_total     BIGINT   NOT NULL,
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
    PRIMARY KEY (repo_a_id, repo_b_id, lag_bins)
);

CREATE INDEX IF NOT EXISTS rlm_forward_idx ON repo_lag_metric (repo_a_id, lag_bins, npmi DESC);
CREATE INDEX IF NOT EXISTS rlm_reverse_idx ON repo_lag_metric (repo_b_id, lag_bins, npmi DESC);
CREATE INDEX IF NOT EXISTS rlm_lag_idx     ON repo_lag_metric (lag_bins);

-- ---------------------------------------------------------------------------
-- Dependency-bump edges recovered from manifest history.
--
-- A Go pseudo-version embeds the upstream commit it was cut from:
--     v3.0.0-20260626221153-5fc63d6f3055
--                           ^^^^^^^^^^^^ upstream commit
-- so a go.mod diff raising github.com/acme/signer/v3 to that version is
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
-- *within* this candidate set reaches 0.86 in sample, 0.69 held out in time.
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
-- to 0.86 in sample, applied inside a repository: "you changed gateway/, which declares
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

INSERT INTO meta (key, value)
VALUES ('schema_version', '12'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
