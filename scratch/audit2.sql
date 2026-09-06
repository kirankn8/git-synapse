\pset pager off
\echo '=== contingency cells: a <= min(n_a,n_b) <= N =============================='
SELECT 'n_ab > min(n_a,n_b)' AS invariant, count(*) AS violations
  FROM file_pair_metric WHERE n_ab > LEAST(n_a, n_b)
UNION ALL SELECT 'n_a > n_total', count(*) FROM file_pair_metric WHERE n_a > n_total
UNION ALL SELECT 'n_b > n_total', count(*) FROM file_pair_metric WHERE n_b > n_total
UNION ALL SELECT 'n_ab <= 0', count(*) FROM file_pair_metric WHERE n_ab <= 0
UNION ALL SELECT 'n_total <= 0', count(*) FROM file_pair_metric WHERE n_total <= 0
UNION ALL SELECT 'metric n_ab <> stored n_ab', count(*)
  FROM file_pair_metric m JOIN file_pair p USING (repo_id, file_a_id, file_b_id)
 WHERE m.n_ab <> p.n_ab;

\echo ''
\echo '=== the marginals are the file marginals, and N is the population ========='
SELECT 'n_a <> file_a.pair_change_count' AS invariant, count(*) AS violations
  FROM file_pair_metric m JOIN file f ON f.id = m.file_a_id
 WHERE m.n_a <> f.pair_change_count
UNION ALL SELECT 'n_b <> file_b.pair_change_count', count(*)
  FROM file_pair_metric m JOIN file f ON f.id = m.file_b_id
 WHERE m.n_b <> f.pair_change_count
UNION ALL SELECT 'n_total <> repo.pair_population', count(*)
  FROM file_pair_metric m JOIN repo r ON r.id = m.repo_id
 WHERE m.n_total <> r.pair_population;

\echo ''
\echo '=== every measure inside its declared range ================================'
SELECT 'jaccard' AS measure, count(*) FILTER (WHERE jaccard < -1e-9 OR jaccard > 1+1e-9) AS out_of_range FROM file_pair_metric
UNION ALL SELECT 'dice', count(*) FILTER (WHERE dice < -1e-9 OR dice > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'ochiai', count(*) FILTER (WHERE ochiai < -1e-9 OR ochiai > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'simpson', count(*) FILTER (WHERE simpson < -1e-9 OR simpson > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'braun_blanquet', count(*) FILTER (WHERE braun_blanquet < -1e-9 OR braun_blanquet > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'kulczynski', count(*) FILTER (WHERE kulczynski < -1e-9 OR kulczynski > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'russell_rao', count(*) FILTER (WHERE russell_rao < -1e-9 OR russell_rao > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'sokal_michener', count(*) FILTER (WHERE sokal_michener < -1e-9 OR sokal_michener > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'rogers_tanimoto', count(*) FILTER (WHERE rogers_tanimoto < -1e-9 OR rogers_tanimoto > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'faith', count(*) FILTER (WHERE faith < -1e-9 OR faith > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'hamann [-1,1]', count(*) FILTER (WHERE hamann < -1-1e-9 OR hamann > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'npmi [-1,1]', count(*) FILTER (WHERE npmi < -1-1e-9 OR npmi > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'ppmi >= 0', count(*) FILTER (WHERE ppmi < -1e-9) FROM file_pair_metric
UNION ALL SELECT 'phi [-1,1]', count(*) FILTER (WHERE phi < -1-1e-9 OR phi > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'cramers_v [0,1]', count(*) FILTER (WHERE cramers_v < -1e-9 OR cramers_v > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'yules_q [-1,1]', count(*) FILTER (WHERE yules_q < -1-1e-9 OR yules_q > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'yules_y [-1,1]', count(*) FILTER (WHERE yules_y < -1-1e-9 OR yules_y > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'michael [-1,1]', count(*) FILTER (WHERE michael < -1-1e-9 OR michael > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'confidence_ab [0,1]', count(*) FILTER (WHERE confidence_ab < -1e-9 OR confidence_ab > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'confidence_ba [0,1]', count(*) FILTER (WHERE confidence_ba < -1e-9 OR confidence_ba > 1+1e-9) FROM file_pair_metric
UNION ALL SELECT 'chi_square >= 0', count(*) FILTER (WHERE chi_square < -1e-6) FROM file_pair_metric
UNION ALL SELECT 'g2 >= 0', count(*) FILTER (WHERE log_likelihood_ratio < -1e-6) FROM file_pair_metric
UNION ALL SELECT 'mutual_information >= 0', count(*) FILTER (WHERE mutual_information < -1e-9) FROM file_pair_metric
UNION ALL SELECT 'association_strength >= 0', count(*) FILTER (WHERE association_strength < -1e-9) FROM file_pair_metric;

\echo ''
\echo '=== non-finite values anywhere ============================================'
SELECT 'NaN or Infinity' AS invariant, count(*) AS violations FROM file_pair_metric
 WHERE npmi='NaN'::float8 OR pmi='NaN'::float8 OR chi_square='NaN'::float8
    OR phi='NaN'::float8 OR yules_q='NaN'::float8 OR michael='NaN'::float8
    OR log_likelihood_ratio='NaN'::float8 OR mutual_information='NaN'::float8
    OR npmi='Infinity'::float8 OR pmi='Infinity'::float8 OR pmi='-Infinity'::float8
    OR chi_square='Infinity'::float8 OR log_likelihood_ratio='Infinity'::float8;

\echo ''
\echo '=== confidence really is the conditional probability ======================'
SELECT 'confidence_ab <> n_ab/n_a' AS invariant, count(*) AS violations
  FROM file_pair_metric WHERE n_a > 0 AND abs(confidence_ab - n_ab::float8/n_a) > 1e-9
UNION ALL SELECT 'confidence_ba <> n_ab/n_b', count(*)
  FROM file_pair_metric WHERE n_b > 0 AND abs(confidence_ba - n_ab::float8/n_b) > 1e-9
UNION ALL SELECT 'jaccard <> a/(a+b+c)', count(*)
  FROM file_pair_metric WHERE abs(jaccard - n_ab::float8/(n_a + n_b - n_ab)) > 1e-9
UNION ALL SELECT 'ochiai <> a/sqrt(n_a*n_b)', count(*)
  FROM file_pair_metric WHERE abs(ochiai - n_ab::float8/sqrt(n_a::float8*n_b)) > 1e-9
UNION ALL SELECT 'dice <> 2a/(n_a+n_b)', count(*)
  FROM file_pair_metric WHERE abs(dice - 2.0*n_ab/(n_a + n_b)) > 1e-9;
