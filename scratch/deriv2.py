"""Round 2: staleness, config change, incremental re-aggregation."""
import datetime as dt, sys, os
sys.path.insert(0,"/work/scratch")
from deriv_lib import *
from git_synapse.analysis import aggregate, score
from git_synapse.config import get_config

c = conn()
fresh_db(c)
R = mk_repo(c,"test/beta")
A = mk_author(c,"a@x.com")
D = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
day = lambda n: D+dt.timedelta(days=n)
P = ["p/a.py","p/b.py","q/c.py"]
F = {p: mk_file(c,R,p) for p in P}
for i in range(1,6):
    mk_commit(c,R,f"s{i}",None,day(i),A,file_ids=[F["p/a.py"],F["p/b.py"]])
mk_commit(c,R,"s9",None,day(9),A,file_ids=[F["q/c.py"]])
c.commit()
aggregate.rebuild_repo(R,c); c.commit()
dump(c,"SELECT path,change_count,pair_change_count FROM file WHERE repo_id=%s ORDER BY 1",(R,),"before: file marginals")
dump(c,"SELECT path,file_count,change_count,pair_change_count FROM directory WHERE repo_id=%s ORDER BY 1",(R,),"before: directories")
dump(c,"SELECT count(*) FROM file_pair WHERE repo_id=%s",(R,),"before: pair count")

print("\n### A. remove all commits touching q/c.py, then re-aggregate")
c.execute("DELETE FROM commit WHERE repo_id=%s AND sha='s9'",(R,))
c.commit()
aggregate.rebuild_repo(R,c); c.commit()
dump(c,"SELECT path,change_count,pair_change_count,first_change_at,last_change_at FROM file WHERE repo_id=%s ORDER BY 1",(R,),"AFTER delete: file marginals (q/c.py should be 0/0/NULL)")
dump(c,"SELECT path,file_count,change_count,pair_change_count FROM directory WHERE repo_id=%s ORDER BY 1",(R,),"AFTER delete: directories ('q' should be 0)")

print("\n### B. delete the FILE row too, re-aggregate; does dir 'q' linger?")
c.execute("DELETE FROM file WHERE id=%s",(F["q/c.py"],)); c.commit()
aggregate.rebuild_repo(R,c); c.commit()
dump(c,"SELECT path,file_count,change_count,pair_change_count FROM directory WHERE repo_id=%s ORDER BY 1",(R,),"dirs after file removed")

print("\n### C. compare against a from-scratch rebuild in a second repo with identical history")
R2 = mk_repo(c,"test/beta2")
F2 = {p: mk_file(c,R2,p) for p in ["p/a.py","p/b.py"]}
for i in range(1,6):
    mk_commit(c,R2,f"t{i}",None,day(i),A,file_ids=[F2["p/a.py"],F2["p/b.py"]])
c.commit()
aggregate.rebuild_repo(R2,c); c.commit()
dump(c,"""SELECT r.full_name,d.path,d.file_count,d.change_count,d.pair_change_count
          FROM directory d JOIN repo r ON r.id=d.repo_id ORDER BY 1,2""",None,"both repos' directories (should match for p/'' rows)")
