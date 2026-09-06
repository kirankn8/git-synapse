\pset pager off
\echo '=== 1. contingency cells are internally consistent =========================='
-- a can never exceed either marginal, and the marginals can never exceed N.
SELECT 'a > min(n_a, n_b)' AS invariant,
       count(*) AS violations
  FROM file_pair p
  JOIN file fa ON fa.id = p.file_a_id
  JOIN file fb ON fb.id = p.file_b_id
 WHERE p.co_changes > LEAST(fa.change_count, fb.change_count)
UNION ALL
SELECT 'marginal > pair_population',
       count(*)
  FROM file f JOIN repo r ON r.id = f.repo_id
 WHERE r.pair_population > 0 AND f.change_count > r.pair_population
UNION ALL
SELECT 'co_changes <= 0 stored',
       count(*) FROM file_pair WHERE co_changes <= 0
UNION ALL
SELECT 'pair spans two repositories',
       count(*) FROM file_pair p
  JOIN file fa ON fa.id = p.file_a_id
  JOIN file fb ON fb.id = p.file_b_id
 WHERE fa.repo_id <> fb.repo_id
UNION ALL
SELECT 'pair of a file with itself',
       count(*) FROM file_pair WHERE file_a_id = file_b_id;

\echo ''
\echo '=== 2. every materialised measure is inside its declared range ============='
SELECT 'npmi outside [-1,1]' AS invariant, count(*) AS violations
  FROM file_pair_metric WHERE npmi < -1.0000001 OR npmi > 1.0000001
UNION ALL SELECT 'jaccard outside [0,1]', count(*)
  FROM file_pair_metric WHERE jaccard < -1e-9 OR jaccard > 1.0000001
UNION ALL SELECT 'confidence_ab outside [0,1]', count(*)
  FROM file_pair_metric WHERE confidence_ab < -1e-9 OR confidence_ab > 1.0000001
UNION ALL SELECT 'confidence_ba outside [0,1]', count(*)
  FROM file_pair_metric WHERE confidence_ba < -1e-9 OR confidence_ba > 1.0000001
UNION ALL SELECT 'cosine outside [0,1]', count(*)
  FROM file_pair_metric WHERE cosine < -1e-9 OR cosine > 1.0000001
UNION ALL SELECT 'dice outside [0,1]', count(*)
  FROM file_pair_metric WHERE dice < -1e-9 OR dice > 1.0000001
UNION ALL SELECT 'lift negative', count(*)
  FROM file_pair_metric WHERE lift < 0
UNION ALL SELECT 'log_likelihood_ratio negative', count(*)
  FROM file_pair_metric WHERE log_likelihood_ratio < -1e-6
UNION ALL SELECT 'yule_q outside [-1,1]', count(*)
  FROM file_pair_metric WHERE yule_q < -1.0000001 OR yule_q > 1.0000001;

\echo ''
\echo '=== 3. no NaN or Infinity anywhere in the measure columns =================='
SELECT 'non-finite measure values' AS invariant, count(*) AS violations
  FROM file_pair_metric
 WHERE npmi = 'NaN'::float8 OR jaccard = 'NaN'::float8 OR lift = 'NaN'::float8
    OR npmi =  'Infinity'::float8 OR lift = 'Infinity'::float8
    OR npmi = '-Infinity'::float8 OR lift = '-Infinity'::float8
    OR log_likelihood_ratio = 'NaN'::float8
    OR log_likelihood_ratio = 'Infinity'::float8;

\echo ''
\echo '=== 4. referential integrity across every derived table ===================='
SELECT 'file_pair_metric without a pair' AS invariant, count(*) AS violations
  FROM file_pair_metric m LEFT JOIN file_pair p
    ON p.repo_id=m.repo_id AND p.file_a_id=m.file_a_id AND p.file_b_id=m.file_b_id
 WHERE p.repo_id IS NULL
UNION ALL SELECT 'dir_pair_metric without a pair', count(*)
  FROM dir_pair_metric m LEFT JOIN dir_pair p
    ON p.repo_id=m.repo_id AND p.dir_a_id=m.dir_a_id AND p.dir_b_id=m.dir_b_id
 WHERE p.repo_id IS NULL
UNION ALL SELECT 'commit_file with no commit', count(*)
  FROM commit_file cf LEFT JOIN commit c ON c.id=cf.commit_id WHERE c.id IS NULL
UNION ALL SELECT 'commit_file with no file', count(*)
  FROM commit_file cf LEFT JOIN file f ON f.id=cf.file_id WHERE f.id IS NULL
UNION ALL SELECT 'file_risk with no file', count(*)
  FROM file_risk fr LEFT JOIN file f ON f.id=fr.file_id WHERE f.id IS NULL
UNION ALL SELECT 'pair_drift with no pair', count(*)
  FROM pair_drift d LEFT JOIN file_pair p
    ON p.repo_id=d.repo_id AND p.file_a_id=d.file_a_id AND p.file_b_id=d.file_b_id
 WHERE p.repo_id IS NULL
UNION ALL SELECT 'file_cluster with no file', count(*)
  FROM file_cluster fc LEFT JOIN file f ON f.id=fc.file_id WHERE f.id IS NULL;

\echo ''
\echo '=== 5. cached counts match the facts they summarise ========================'
SELECT 'repo.commit_count wrong' AS invariant, count(*) AS violations FROM (
  SELECT r.id FROM repo r
   WHERE r.is_enabled AND r.commit_count <> (SELECT count(*) FROM commit c WHERE c.repo_id=r.id)
) x
UNION ALL
SELECT 'repo.file_count wrong', count(*) FROM (
  SELECT r.id FROM repo r
   WHERE r.is_enabled AND r.file_count <> (SELECT count(*) FROM file f WHERE f.repo_id=r.id)
) y
UNION ALL
SELECT 'file.change_count wrong', count(*) FROM (
  SELECT f.id FROM file f JOIN repo r ON r.id=f.repo_id
   WHERE r.is_enabled AND f.change_count <> (
     SELECT count(*) FROM commit_file cf JOIN commit c ON c.id=cf.commit_id
      WHERE cf.file_id=f.id AND c.pair_eligible)
  LIMIT 200
) z;

\echo ''
\echo '=== 6. the population N is the pair-eligible commit count =================='
SELECT 'repo.pair_population wrong' AS invariant, count(*) AS violations FROM (
  SELECT r.id FROM repo r
   WHERE r.is_enabled AND r.pair_population <> (
     SELECT count(*) FROM commit c WHERE c.repo_id=r.id AND c.pair_eligible)
) p;

\echo ''
\echo '=== 7. directory rollup: a directory changes iff a file beneath it does ===='
SELECT 'dir_pair spans two repos' AS invariant, count(*) AS violations
  FROM dir_pair p JOIN directory da ON da.id=p.dir_a_id
  JOIN directory db2 ON db2.id=p.dir_b_id WHERE da.repo_id <> db2.repo_id
UNION ALL
SELECT 'ancestor/descendant pair kept', count(*)
  FROM dir_pair p JOIN directory da ON da.id=p.dir_a_id
  JOIN directory db2 ON db2.id=p.dir_b_id
 WHERE da.path <> '' AND db2.path <> ''
   AND (db2.path LIKE da.path || '/%' OR da.path LIKE db2.path || '/%');
