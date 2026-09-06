"""Round 1: hand-computed synthetic history vs real aggregation."""
import datetime as dt, sys
sys.path.insert(0, "/work/scratch")
from deriv_lib import *
from git_synapse.analysis import aggregate, score
from git_synapse.config import get_config

c = conn()
fresh_db(c)
R = mk_repo(c, "test/alpha")
paths = ["README.md","src/a.py","src/b.py","src/deep/c.py","lib/x.py","lib/y.py"]
F = {p: mk_file(c, R, p) for p in paths}
A = mk_author(c, "a@x.com")
D = dt.datetime(2026,1,1, tzinfo=dt.timezone.utc)
def day(n): return D + dt.timedelta(days=n)

# c1,c2: f1,f2,f3 ; c3: f1,f4 ; c4: f1 alone ; c5: empty ; c6: merge f1,f5,f6 ; c7,c8: f1,f5
mk_commit(c,R,"c1",None,day(1),A,file_ids=[F["README.md"],F["src/a.py"],F["src/b.py"]])
mk_commit(c,R,"c2",None,day(2),A,file_ids=[F["README.md"],F["src/a.py"],F["src/b.py"]])
mk_commit(c,R,"c3",None,day(3),A,file_ids=[F["README.md"],F["src/deep/c.py"]])
mk_commit(c,R,"c4",None,day(4),A,file_ids=[F["README.md"]])
mk_commit(c,R,"c5",None,day(5),A,file_ids=[])                      # empty commit
mk_commit(c,R,"c6",None,day(6),A,is_merge=True,
          file_ids=[F["README.md"],F["lib/x.py"],F["lib/y.py"]])    # merge
mk_commit(c,R,"c7",None,day(7),A,file_ids=[F["README.md"],F["lib/x.py"]])
mk_commit(c,R,"c8",None,day(8),A,file_ids=[F["README.md"],F["lib/x.py"]])

cfg = get_config()
print("min_pair_support =", cfg.ingest.min_pair_support,
      " max_files_per_commit =", cfg.ingest.max_files_per_commit,
      " half_life =", cfg.analysis.recency_half_life_days)

c.commit()
st = aggregate.rebuild_repo(R, c)
print("stats:", st)

ok = True
ok &= check("N (pair_population)", c.execute("SELECT pair_population FROM repo WHERE id=%s",(R,)).fetchone()[0], 6)

want_marg = {"README.md":(7,6),"src/a.py":(2,2),"src/b.py":(2,2),
             "src/deep/c.py":(1,1),"lib/x.py":(3,2),"lib/y.py":(1,0)}
rows = c.execute("SELECT path, change_count, pair_change_count FROM file WHERE repo_id=%s ORDER BY path",(R,)).fetchall()
for p, cc, pcc in rows:
    ok &= check(f"marginal {p}", (cc,pcc), want_marg[p])

want_pairs = {("README.md","src/a.py"):2, ("README.md","src/b.py"):2,
              ("src/a.py","src/b.py"):2, ("README.md","lib/x.py"):2}
rows = c.execute("""SELECT fa.path, fb.path, p.n_ab FROM file_pair p
  JOIN file fa ON fa.id=p.file_a_id JOIN file fb ON fb.id=p.file_b_id
  WHERE p.repo_id=%s ORDER BY 1,2""",(R,)).fetchall()
got_pairs = {(a,b):n for a,b,n in rows}
got_norm = {tuple(sorted(k)):v for k,v in got_pairs.items()}
ok &= check("file_pair set", got_norm, {tuple(sorted(k)):v for k,v in want_pairs.items()})

# directories
rows = c.execute("SELECT path, depth, file_count, change_count, pair_change_count FROM directory WHERE repo_id=%s ORDER BY path",(R,)).fetchall()
dump(c,"SELECT path, depth, file_count, change_count, pair_change_count FROM directory WHERE repo_id=%s ORDER BY path",(R,),"directory")
want_dirs = {"":(0,6,7,6), "lib":(1,2,3,2), "src":(1,3,3,3), "src/deep":(2,1,1,1)}
ok &= check("directory rows", {r[0]:tuple(r[1:]) for r in rows}, want_dirs)

fd = c.execute("SELECT count(*) FROM file_directory WHERE repo_id=%s",(R,)).fetchone()[0]
ok &= check("file_directory row count", fd, 12)

rows = c.execute("""SELECT da.path, db.path, p.n_ab FROM dir_pair p
  JOIN directory da ON da.id=p.dir_a_id JOIN directory db ON db.id=p.dir_b_id
  WHERE p.repo_id=%s ORDER BY 1,2""",(R,)).fetchall()
ok &= check("dir_pair set", {tuple(sorted((a,b))):n for a,b,n in rows},
            {("","src"):3, ("","lib"):2})

# score
ss = score.score_repo(R, c)
print("score:", ss)
dump(c,"""SELECT fa.path, fb.path, m.n_ab, m.n_a, m.n_b, m.n_total FROM file_pair_metric m
  JOIN file fa ON fa.id=m.file_a_id JOIN file fb ON fb.id=m.file_b_id WHERE m.repo_id=%s ORDER BY 1,2""",(R,),"file_pair_metric cells")
dump(c,"""SELECT da.path, db.path, m.n_ab, m.n_a, m.n_b, m.n_total FROM dir_pair_metric m
  JOIN directory da ON da.id=m.dir_a_id JOIN directory db ON db.id=m.dir_b_id WHERE m.repo_id=%s ORDER BY 1,2""",(R,),"dir_pair_metric cells")

print("\nROUND1 ALL PASS" if ok else "\nROUND1 HAS FAILURES")
