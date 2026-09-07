\pset pager off
\echo '=== risk: every component is a percentile within its own repository ======='
SELECT 'risk score outside [0,1]' AS invariant, count(*) AS violations
  FROM file_risk WHERE score < -1e-9 OR score > 1+1e-9
UNION ALL SELECT 'a component outside [0,1]', count(*) FROM file_risk
 WHERE least(churn_pct, coupling_pct, ownership_pct) < -1e-9
    OR greatest(churn_pct, coupling_pct, ownership_pct) > 1+1e-9
UNION ALL SELECT 'bus factor < 1', count(*) FROM file_risk WHERE bus_factor < 1
UNION ALL SELECT 'risk row for a deleted file', count(*)
  FROM file_risk fr JOIN file f ON f.id = fr.file_id WHERE f.is_deleted;

\echo ''
\echo '=== drift: recent and historical windows are both real ===================='
SELECT 'drift direction not one of the three' AS invariant, count(*) AS violations
  FROM pair_drift WHERE direction NOT IN ('emerging','decaying','stable')
UNION ALL SELECT 'recent and historical both zero', count(*)
  FROM pair_drift WHERE recent_n_ab = 0 AND historical_n_ab = 0
UNION ALL SELECT 'emerging but not actually growing', count(*)
  FROM pair_drift WHERE direction='emerging' AND recent_score <= historical_score
UNION ALL SELECT 'decaying but not actually shrinking', count(*)
  FROM pair_drift WHERE direction='decaying' AND recent_score >= historical_score;

\echo ''
\echo '=== clusters: a file belongs to at most one module per repository ========='
SELECT 'file in two clusters' AS invariant, count(*) AS violations FROM (
  SELECT file_id FROM file_cluster GROUP BY file_id HAVING count(*) > 1) x
UNION ALL SELECT 'cluster spans two repositories', count(*) FROM (
  SELECT fc.repo_id, fc.cluster_id FROM file_cluster fc JOIN file f ON f.id=fc.file_id
   GROUP BY fc.repo_id, fc.cluster_id HAVING count(DISTINCT f.repo_id) > 1) y
UNION ALL SELECT 'cluster of one file', count(*) FROM (
  SELECT repo_id, cluster_id FROM file_cluster
   GROUP BY repo_id, cluster_id HAVING count(*) < 2) z;

\echo ''
\echo '=== impact: every edge is declared or bump-backed, never inferred ========='
SELECT 'edge with neither evidence' AS invariant, count(*) AS violations
  FROM repo_impact WHERE NOT (is_declared OR has_bump_history)
UNION ALL SELECT 'score outside [0,1]', count(*)
  FROM repo_impact WHERE score < -1e-9 OR score > 1+1e-9
UNION ALL SELECT 'self edge', count(*)
  FROM repo_impact WHERE source_repo_id = target_repo_id
UNION ALL SELECT 'edge to a repo we do not have', count(*)
  FROM repo_impact i LEFT JOIN repo r ON r.id = i.target_repo_id WHERE r.id IS NULL;

\echo ''
\echo '=== bumps: an adoption delay is never negative ============================'
SELECT 'negative adoption delay' AS invariant, count(*) AS violations
  FROM dep_bump WHERE adoption_seconds < 0
UNION ALL SELECT 'resolution set without a commit', count(*)
  FROM dep_bump WHERE resolution IS NOT NULL AND dep_commit_id IS NULL
UNION ALL SELECT 'commit set without a resolution', count(*)
  FROM dep_bump WHERE dep_commit_id IS NOT NULL AND resolution IS NULL
UNION ALL SELECT 'consumer bumping itself', count(*)
  FROM dep_bump WHERE consumer_repo_id = dep_repo_id;

\echo ''
\echo '=== commits: the atom is sane ============================================='
SELECT 'commit with no files' AS invariant, count(*) AS violations FROM (
  SELECT c.id FROM commit c
   WHERE c.pair_eligible
     AND NOT EXISTS (SELECT 1 FROM commit_file cf WHERE cf.commit_id = c.id)
   LIMIT 100) a
UNION ALL SELECT 'pair-eligible commit over the fan-out cap', count(*) FROM (
  SELECT cf.commit_id FROM commit_file cf JOIN commit c ON c.id=cf.commit_id
   WHERE c.pair_eligible GROUP BY cf.commit_id HAVING count(*) > 60 LIMIT 100) b
UNION ALL SELECT 'replayed commit counted as eligible', count(*)
  FROM commit WHERE is_replay AND pair_eligible
UNION ALL SELECT 'commit dated in the future', count(*)
  FROM commit WHERE committed_at > now() + interval '1 day';
