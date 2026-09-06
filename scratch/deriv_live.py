"""Read-only invariant sweep over the live corpus."""
import sys
sys.path.insert(0,"/work/scratch")
from deriv_lib import conn, dump
c = conn(autocommit=True)
c.execute("SET statement_timeout='600s'")
Q = [
 ("corpus size","SELECT (SELECT count(*) FROM repo) repos,(SELECT count(*) FROM commit) commits,(SELECT count(*) FROM file_pair) fp,(SELECT count(*) FROM file_pair_metric) fpm,(SELECT count(*) FROM dir_pair) dp"),
 ("I1 n_ab > n_a (file)","SELECT count(*) FROM file_pair p JOIN file fa ON fa.id=p.file_a_id WHERE p.n_ab > fa.pair_change_count"),
 ("I1b n_ab > n_b (file)","SELECT count(*) FROM file_pair p JOIN file fb ON fb.id=p.file_b_id WHERE p.n_ab > fb.pair_change_count"),
 ("I2 n_a > N","SELECT count(*) FROM file f JOIN repo r ON r.id=f.repo_id WHERE f.pair_change_count > r.pair_population"),
 ("I2b pair_change_count > change_count","SELECT count(*) FROM file WHERE pair_change_count > change_count"),
 ("I3 file_pair repo mismatch","SELECT count(*) FROM file_pair p JOIN file fa ON fa.id=p.file_a_id JOIN file fb ON fb.id=p.file_b_id WHERE fa.repo_id<>p.repo_id OR fb.repo_id<>p.repo_id"),
 ("I3b dir_pair repo mismatch","SELECT count(*) FROM dir_pair p JOIN directory da ON da.id=p.dir_a_id JOIN directory db ON db.id=p.dir_b_id WHERE da.repo_id<>p.repo_id OR db.repo_id<>p.repo_id"),
 ("I3c file_directory repo mismatch","SELECT count(*) FROM file_directory fd JOIN file f ON f.id=fd.file_id JOIN directory d ON d.id=fd.dir_id WHERE f.repo_id<>fd.repo_id OR d.repo_id<>fd.repo_id"),
 ("I3d commit_file repo != commit repo","SELECT count(*) FROM commit_file cf JOIN commit c2 ON c2.id=cf.commit_id WHERE cf.repo_id<>c2.repo_id"),
 ("I3e commit_file repo != file repo","SELECT count(*) FROM commit_file cf JOIN file f ON f.id=cf.file_id WHERE cf.repo_id<>f.repo_id"),
 ("I4 metric rows w/o pair","SELECT count(*) FROM file_pair_metric m LEFT JOIN file_pair p ON p.repo_id=m.repo_id AND p.file_a_id=m.file_a_id AND p.file_b_id=m.file_b_id WHERE p.repo_id IS NULL"),
 ("I4b pairs w/o metric","SELECT count(*) FROM file_pair p LEFT JOIN file_pair_metric m ON p.repo_id=m.repo_id AND p.file_a_id=m.file_a_id AND p.file_b_id=m.file_b_id WHERE m.repo_id IS NULL"),
 ("I5 metric n_total != repo N","SELECT count(*) FROM file_pair_metric m JOIN repo r ON r.id=m.repo_id WHERE m.n_total <> r.pair_population"),
 ("I5b metric n_ab != pair n_ab","SELECT count(*) FROM file_pair_metric m JOIN file_pair p ON p.repo_id=m.repo_id AND p.file_a_id=m.file_a_id AND p.file_b_id=m.file_b_id WHERE m.n_ab<>p.n_ab"),
 ("I5c metric n_a != file marginal","SELECT count(*) FROM file_pair_metric m JOIN file f ON f.id=m.file_a_id WHERE m.n_a<>f.pair_change_count"),
 ("I6 w_ab non-finite/neg","SELECT count(*) FILTER (WHERE w_ab='Infinity'::float8), count(*) FILTER (WHERE w_ab='NaN'::float8), count(*) FILTER (WHERE w_ab<0), count(*) FILTER (WHERE w_ab>n_ab) FROM file_pair"),
 ("I6b dir w_ab non-finite","SELECT count(*) FILTER (WHERE w_ab='Infinity'::float8), count(*) FILTER (WHERE w_ab='NaN'::float8), count(*) FILTER (WHERE w_ab<0), count(*) FILTER (WHERE w_ab>n_ab) FROM dir_pair"),
 ("I7 future-dated commits","SELECT count(*), max(committed_at) FROM commit WHERE committed_at > now()"),
 ("I8 pairs below min support(2)","SELECT count(*) FROM file_pair WHERE n_ab < 2"),
 ("I8b dir pairs below support","SELECT count(*) FROM dir_pair WHERE n_ab < 2"),
 ("I9 N vs eligible commits","SELECT count(*) FROM repo r WHERE r.pair_population <> (SELECT count(*) FROM commit c WHERE c.repo_id=r.id AND c.pair_eligible)"),
 ("I10 dir n_ab > dir marginal","SELECT count(*) FROM dir_pair p JOIN directory da ON da.id=p.dir_a_id WHERE p.n_ab > da.pair_change_count"),
 ("I11 root dir pair_change_count vs N","SELECT count(*) FROM directory d JOIN repo r ON r.id=d.repo_id WHERE d.path='' AND d.pair_change_count <> r.pair_population"),
 ("I12 directory insertions/deletions all zero?","SELECT max(insertions), max(deletions) FROM directory"),
 ("I13 files missing root file_directory row","SELECT count(*) FROM file f WHERE NOT EXISTS (SELECT 1 FROM file_directory fd JOIN directory d ON d.id=fd.dir_id WHERE fd.file_id=f.id AND d.path='')"),
 ("I14 ancestor count mismatch","SELECT count(*) FROM (SELECT f.id, (SELECT count(*) FROM file_directory fd WHERE fd.file_id=f.id) got, CASE WHEN f.dir_path='' THEN 1 ELSE array_length(string_to_array(f.dir_path,'/'),1)+1 END want FROM file f) t WHERE got<>want"),
 ("I15 file marginal vs recount (sample repo)","SELECT count(*) FROM (SELECT cf.file_id, count(*) FILTER (WHERE c2.pair_eligible) pcc FROM commit_file cf JOIN commit c2 ON c2.id=cf.commit_id WHERE cf.repo_id=(SELECT id FROM repo ORDER BY commit_count DESC LIMIT 1) GROUP BY 1) t JOIN file f ON f.id=t.file_id WHERE f.pair_change_count<>t.pcc"),
]
for label,q in Q:
    try:
        dump(c,q,None,label)
    except Exception as e:
        print(f"--- {label}\n  ERROR {e}")
