"""Round 4: score batching, cross-repo N, and interrupted aggregation."""
import datetime as dt, sys, os
sys.path.insert(0,"/work/scratch")
from deriv_lib import *
from git_synapse.analysis import aggregate, score
from git_synapse.config import get_config

c = conn(); fresh_db(c)
A = mk_author(c,"a@x.com")
D = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc); day=lambda n: D+dt.timedelta(days=n)

print("### A. batching: 80-file commits -> 3160 pairs, batch size 1000")
R = mk_repo(c,"test/batch")
fs = [mk_file(c,R,f"d{i%7}/f{i}.py") for i in range(80)]
for k in range(3):
    mk_commit(c,R,f"b{k}",None,day(k),A,file_ids=fs)
c.commit()
print("  cfg cap =", get_config().ingest.max_files_per_commit,
      " score_batch_size =", get_config().analysis.score_batch_size)
aggregate.rebuild_repo(R,c); c.commit()
n = c.execute("SELECT count(*) FROM file_pair WHERE repo_id=%s",(R,)).fetchone()[0]
print("  file_pair rows:", n, "(expect 3160 if cap allows 80-file commits)")
ss = score.score_repo(R,c); c.commit()
print("  score:", ss)
dump(c,"""SELECT (SELECT count(*) FROM file_pair WHERE repo_id=%(r)s) pairs,
                 (SELECT count(*) FROM file_pair_metric WHERE repo_id=%(r)s) metrics,
                 (SELECT count(DISTINCT (file_a_id,file_b_id)) FROM file_pair_metric WHERE repo_id=%(r)s) distinct_metrics""",
     {"r":R},"batching: pairs vs metrics vs distinct")
dump(c,"""SELECT count(*) FROM file_pair p FULL JOIN file_pair_metric m
          ON m.repo_id=p.repo_id AND m.file_a_id=p.file_a_id AND m.file_b_id=p.file_b_id
          WHERE p.repo_id IS NULL OR m.repo_id IS NULL""",None,"batching: dropped or extra pairs (want 0)")
dump(c,"SELECT DISTINCT n_total FROM file_pair_metric WHERE repo_id=%s",(R,),"n_total values in metrics")
dump(c,"SELECT count(*) FROM dir_pair_metric m JOIN dir_pair p USING (repo_id,dir_a_id,dir_b_id) WHERE 1=0",None,"noop")

print("\n### B. two repos with DIFFERENT N scored via score_all")
R2 = mk_repo(c,"test/n7")
gs2=[mk_file(c,R2,f"s/{i}.py") for i in range(3)]
for k in range(7): mk_commit(c,R2,f"n{k}",None,day(k),A,file_ids=gs2)
c.commit(); aggregate.rebuild_repo(R2,c); c.commit()
score.score_all(c); c.commit()
dump(c,"""SELECT r.full_name, r.pair_population AS N, count(*) AS metric_rows,
                 array_agg(DISTINCT m.n_total) AS n_totals
          FROM file_pair_metric m JOIN repo r ON r.id=m.repo_id GROUP BY 1,2 ORDER BY 1""",
     None,"per-repo n_total (must equal that repo's N)")

print("\n### C. interrupted aggregation: score fails after aggregate commits")
R3 = mk_repo(c,"test/interrupt")
h=[mk_file(c,R3,f"z/{i}.py") for i in range(3)]
for k in range(4): mk_commit(c,R3,f"i{k}",None,day(k),A,file_ids=h[:2])
c.execute("UPDATE repo SET last_ingest_at=now() WHERE id=%s",(R3,))
c.commit()
aggregate.rebuild_repo(R3,c); c.commit()
score.score_repo(R3,c); c.commit()
print("  initial pairs/metrics:",
      c.execute("SELECT (SELECT count(*) FROM file_pair WHERE repo_id=%(r)s),(SELECT count(*) FROM file_pair_metric WHERE repo_id=%(r)s)",{"r":R3}).fetchone())
# new commits arrive, introducing a new pair and changing N
for k in range(4,8): mk_commit(c,R3,f"i{k}",None,day(k),A,file_ids=h)
c.execute("UPDATE repo SET last_ingest_at=now() WHERE id=%s",(R3,))
c.commit()
print("  repos_needing_aggregation:", aggregate.repos_needing_aggregation(c))
aggregate.rebuild_repo(R3,c); c.commit()          # aggregate commits
print("  -- now simulate score_repo() crashing (transaction rolled back) --")
try:
    score._score_level(c, R3, "file")
    raise RuntimeError("boom: interrupted before commit")
except RuntimeError as e:
    c.rollback(); print("  ", e)
dump(c,"""SELECT (SELECT count(*) FROM file_pair WHERE repo_id=%(r)s) pairs,
                 (SELECT count(*) FROM file_pair_metric WHERE repo_id=%(r)s) metrics,
                 (SELECT pair_population FROM repo WHERE id=%(r)s) N,
                 (SELECT array_agg(DISTINCT n_total) FROM file_pair_metric WHERE repo_id=%(r)s) stale_n_total""",
     {"r":R3},"after interrupted score")
print("  repos_needing_aggregation AFTER:", aggregate.repos_needing_aggregation(c),
      "  <- empty means the repo is never revisited")
dump(c,"""SELECT count(*) FROM file_pair p FULL JOIN file_pair_metric m
          ON m.repo_id=p.repo_id AND m.file_a_id=p.file_a_id AND m.file_b_id=p.file_b_id
          WHERE (p.repo_id IS NULL OR m.repo_id IS NULL) AND COALESCE(p.repo_id,m.repo_id)=%s""",(R3,),
     "pairs with no metric / metrics with no pair")
