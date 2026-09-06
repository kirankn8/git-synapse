"""Round 3: rename -> stale directory; support/cap changes; w_ab maths."""
import datetime as dt, sys, math
sys.path.insert(0,"/work/scratch")
from deriv_lib import *
from git_synapse.analysis import aggregate, score
from git_synapse.config import get_config

c = conn(); fresh_db(c)
A = mk_author(c,"a@x.com")
D = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc); day=lambda n: D+dt.timedelta(days=n)

print("### A. RENAME makes a directory empty -> stale directory row?")
R = mk_repo(c,"test/ren")
f1 = mk_file(c,R,"old/thing.py"); f2 = mk_file(c,R,"keep/z.py")
for i in range(1,4): mk_commit(c,R,f"r{i}",None,day(i),A,file_ids=[f1,f2])
c.commit(); aggregate.rebuild_repo(R,c); c.commit()
dump(c,"SELECT path,file_count,change_count,pair_change_count FROM directory WHERE repo_id=%s ORDER BY 1",(R,),"dirs before rename")
# exactly what store.py does on a rename: same file id, new path/dir_path
c.execute("UPDATE file SET path='new/thing.py', dir_path='new' WHERE id=%s",(f1,))
c.execute("INSERT INTO file_alias (repo_id, old_path, file_id) VALUES (%s,'old/thing.py',%s)",(R,f1))
c.commit(); aggregate.rebuild_repo(R,c); c.commit()
dump(c,"SELECT path,file_count,change_count,pair_change_count FROM directory WHERE repo_id=%s ORDER BY 1",(R,),
     "dirs after rename ('old' should be gone or zeroed)")

print("\n### B. MIN_PAIR_SUPPORT change (3 then 1) must not move marginals or N")
R2 = mk_repo(c,"test/sup")
g = {p: mk_file(c,R2,p) for p in ["a.py","b.py","c.py"]}
mk_commit(c,R2,"u1",None,day(1),A,file_ids=[g["a.py"],g["b.py"]])
mk_commit(c,R2,"u2",None,day(2),A,file_ids=[g["a.py"],g["b.py"]])
mk_commit(c,R2,"u3",None,day(3),A,file_ids=[g["a.py"],g["b.py"]])
mk_commit(c,R2,"u4",None,day(4),A,file_ids=[g["a.py"],g["c.py"]])   # n_ab=1
c.commit()
import git_synapse.config as C
for sup in (1,2,3,4,0,-5):
    C._config = None
    import os; os.environ["MIN_PAIR_SUPPORT"]=str(sup)
    C.get_config.cache_clear() if hasattr(C.get_config,"cache_clear") else None
    aggregate.rebuild_repo(R2,c); c.commit()
    n = c.execute("SELECT pair_population FROM repo WHERE id=%s",(R2,)).fetchone()[0]
    marg = c.execute("SELECT path,pair_change_count FROM file WHERE repo_id=%s ORDER BY 1",(R2,)).fetchall()
    pairs = c.execute("""SELECT fa.path,fb.path,p.n_ab FROM file_pair p JOIN file fa ON fa.id=p.file_a_id
                         JOIN file fb ON fb.id=p.file_b_id WHERE p.repo_id=%s ORDER BY 1,2""",(R2,)).fetchall()
    print(f"  MIN_PAIR_SUPPORT={sup:<3} cfg={get_config().ingest.min_pair_support:<3} N={n} marginals={marg} pairs={pairs}")
import os; os.environ["MIN_PAIR_SUPPORT"]="2"; C._config=None

print("\n### C. w_ab: half-life exactness & monotonicity")
R3 = mk_repo(c,"test/w")
h = {p: mk_file(c,R3,p) for p in ["x.py","y.py"]}
now = c.execute("SELECT now()").fetchone()[0]
hl = get_config().analysis.recency_half_life_days
for k,age in enumerate([0, hl, 2*hl, 3*hl]):
    mk_commit(c,R3,f"w{k}",None, now - dt.timedelta(days=age), A, file_ids=[h["x.py"],h["y.py"]])
c.commit(); aggregate.rebuild_repo(R3,c); c.commit()
w = c.execute("SELECT n_ab,w_ab FROM file_pair WHERE repo_id=%s",(R3,)).fetchone()
expect = 1 + 0.5 + 0.25 + 0.125
print(f"  half_life={hl}d n_ab={w[0]} w_ab={w[1]!r} expected~{expect}")
check("w_ab half-life", round(w[1],4), round(expect,4))

print("\n### D. w_ab with a FUTURE-dated commit (clock skew)")
R4 = mk_repo(c,"test/future")
j = {p: mk_file(c,R4,p) for p in ["x.py","y.py"]}
for k,when in enumerate([now, dt.datetime(2200,1,1,tzinfo=dt.timezone.utc)]):
    mk_commit(c,R4,f"fz{k}",None,when,A,file_ids=[j["x.py"],j["y.py"]])
c.commit()
try:
    aggregate.rebuild_repo(R4,c); c.commit()
    dump(c,"SELECT n_ab,w_ab FROM file_pair WHERE repo_id=%s",(R4,),"future-dated pair")
except Exception as e:
    c.rollback(); print("  AGGREGATION RAISED:", type(e).__name__, e)

print("\n### E. w_ab with an EXTREME future date (year 294276, pg max timestamp)")
R5 = mk_repo(c,"test/far")
k2 = {p: mk_file(c,R5,p) for p in ["x.py","y.py"]}
for n2,when in enumerate([now, dt.datetime(9999,12,31,tzinfo=dt.timezone.utc)]):
    mk_commit(c,R5,f"fr{n2}",None,when,A,file_ids=[k2["x.py"],k2["y.py"]])
c.commit()
try:
    aggregate.rebuild_repo(R5,c); c.commit()
    dump(c,"SELECT n_ab,w_ab, w_ab='Infinity'::float8 AS is_inf FROM file_pair WHERE repo_id=%s",(R5,),"far-future pair")
except Exception as e:
    c.rollback(); print("  AGGREGATION RAISED:", type(e).__name__, repr(e))
