"""Round 5: real multi-batch scoring + fan-out cap changes."""
import datetime as dt, sys, os
sys.path.insert(0,"/work/scratch")
from deriv_lib import *
from git_synapse.analysis import aggregate, score
from git_synapse.config import get_config
import git_synapse.config as C

c = conn(); fresh_db(c)
A = mk_author(c,"a@x.com")
D = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc); day=lambda n: D+dt.timedelta(days=n)

print("### A. multi-batch scoring: 100 files -> 4950 pairs, batch=1000")
R = mk_repo(c,"test/batch")
fs = [mk_file(c,R,f"d{i%7}/f{i:03d}.py") for i in range(100)]
for k in range(3): mk_commit(c,R,f"b{k}",None,day(k),A,file_ids=fs)
c.commit()
print("  cap =", get_config().ingest.max_files_per_commit, " batch =", get_config().analysis.score_batch_size)
aggregate.rebuild_repo(R,c); c.commit()
print("  file_pair rows:", c.execute("SELECT count(*) FROM file_pair WHERE repo_id=%s",(R,)).fetchone()[0], "(expect 4950)")
print("  dir_pair rows:", c.execute("SELECT count(*) FROM dir_pair WHERE repo_id=%s",(R,)).fetchone()[0])
ss = score.score_repo(R,c); c.commit(); print("  ", ss)
dump(c,"""SELECT (SELECT count(*) FROM file_pair WHERE repo_id=%(r)s) pairs,
                 (SELECT count(*) FROM file_pair_metric WHERE repo_id=%(r)s) metrics,
                 (SELECT count(*) FROM (SELECT file_a_id,file_b_id FROM file_pair_metric WHERE repo_id=%(r)s GROUP BY 1,2 HAVING count(*)>1) x) dupes,
                 (SELECT array_agg(DISTINCT n_total) FROM file_pair_metric WHERE repo_id=%(r)s) n_totals""",
     {"r":R},"multi-batch integrity")
dump(c,"""SELECT count(*) FROM file_pair p FULL JOIN file_pair_metric m
          ON m.repo_id=p.repo_id AND m.file_a_id=p.file_a_id AND m.file_b_id=p.file_b_id
          WHERE p.repo_id IS NULL OR m.repo_id IS NULL""",None,"dropped/extra across batch boundaries (want 0)")
dump(c,"""SELECT count(*) FROM file_pair_metric m JOIN file_pair p USING (repo_id,file_a_id,file_b_id)
          JOIN file fa ON fa.id=m.file_a_id JOIN file fb ON fb.id=m.file_b_id
          WHERE m.n_ab<>p.n_ab OR m.n_a<>fa.pair_change_count OR m.n_b<>fb.pair_change_count""",
     None,"cells wrong in any batch (want 0)")

print("\n### B. two repos, different N, one score_all pass")
R2 = mk_repo(c,"test/small")
g=[mk_file(c,R2,f"s/{i}.py") for i in range(4)]
for k in range(9): mk_commit(c,R2,f"m{k}",None,day(k),A,file_ids=g)
c.commit(); aggregate.rebuild_repo(R2,c); c.commit()
score.score_all(c); c.commit()
dump(c,"""SELECT r.full_name, r.pair_population N, count(*) rows, array_agg(DISTINCT m.n_total) n_totals
          FROM file_pair_metric m JOIN repo r ON r.id=m.repo_id GROUP BY 1,2 ORDER BY 1""",None,"per-repo n_total")

print("\n### C. lower the fan-out cap to 50 and re-aggregate (100-file commits become ineligible)")
os.environ["MAX_FILES_PER_COMMIT"]="50"; C._config=None
aggregate.rebuild_repo(R,c); c.commit()
dump(c,"""SELECT (SELECT pair_population FROM repo WHERE id=%(r)s) N,
                 (SELECT count(*) FROM commit WHERE repo_id=%(r)s AND pair_eligible) eligible,
                 (SELECT count(*) FROM file_pair WHERE repo_id=%(r)s) pairs,
                 (SELECT max(pair_change_count) FROM file WHERE repo_id=%(r)s) max_marginal""",
     {"r":R},"after cap lowered (expect N=0, pairs=0, marginals 0)")
score.score_repo(R,c); c.commit()
dump(c,"SELECT count(*) FROM file_pair_metric WHERE repo_id=%s",(R,),"metrics after N=0")

print("\n### D. raise the cap back to 200 and re-aggregate (must fully restore)")
os.environ["MAX_FILES_PER_COMMIT"]="200"; C._config=None
aggregate.rebuild_repo(R,c); c.commit(); score.score_repo(R,c); c.commit()
dump(c,"""SELECT (SELECT pair_population FROM repo WHERE id=%(r)s) N,
                 (SELECT count(*) FROM file_pair WHERE repo_id=%(r)s) pairs,
                 (SELECT count(*) FROM file_pair_metric WHERE repo_id=%(r)s) metrics,
                 (SELECT array_agg(DISTINCT n_total) FROM file_pair_metric WHERE repo_id=%(r)s) n_totals""",
     {"r":R},"restored")
os.environ["MAX_FILES_PER_COMMIT"]="60"; C._config=None
