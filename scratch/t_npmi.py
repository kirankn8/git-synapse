"""Compare the drift SQL's inline NPMI against git_synapse.stats.measures.npmi."""
import itertools, os
import numpy as np, psycopg
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.measures import npmi as py_npmi

SQL = """
SELECT CASE WHEN %(j)s > 0 AND %(N)s > 0 AND %(ma)s > 0 AND %(mb)s > 0
            THEN CASE WHEN %(j)s >= %(N)s THEN 1.0
                 ELSE (ln((%(j)s::numeric * %(N)s) / (%(ma)s::numeric * %(mb)s)) / ln(2))
                      / (-ln(%(j)s::numeric / %(N)s) / ln(2))
                 END
       END
"""

conn = psycopg.connect(host=os.environ["POSTGRES_HOST"], user=os.environ["POSTGRES_USER"],
                       password=os.environ["POSTGRES_PASSWORD"], dbname=os.environ["POSTGRES_DB"])

cases = []
# interior grid
for N in (1, 2, 5, 10, 100):
    for ma in range(0, N + 1):
        for mb in range(0, N + 1):
            for j in range(0, min(ma, mb) + 1):
                cases.append((j, ma, mb, N))
# boundaries / infeasible
cases += [(0,0,0,0),(0,5,5,10),(10,10,10,10),(5,10,10,10),(1,1,1,1),
          (2,2,2,2),(3,10,3,10),(0,0,5,10),(5,5,0,10),(10,10,10,5),  # j>N infeasible
          (7,7,7,5)]

bad = []
for j, ma, mb, N in cases:
    row = conn.execute(SQL, {"j": j, "N": N, "ma": ma, "mb": mb}).fetchone()[0]
    sqlv = None if row is None else float(row)
    t = Contingency.from_counts(j, ma, mb, N)
    pyv = float(py_npmi(t))
    if sqlv is None:
        # SQL says NULL -> drift COALESCEs to 0
        eff = 0.0
    else:
        eff = sqlv
    if abs(eff - pyv) > 1e-9:
        bad.append((j, ma, mb, N, sqlv, pyv))

print(f"cases={len(cases)} mismatches={len(bad)}")
seen = set()
for j, ma, mb, N, s, p in bad:
    kind = (s is None, round(p,3))
    print(f"  j={j:3d} ma={ma:3d} mb={mb:3d} N={N:4d}  sql={s}  py={p}")
    if len(seen) > 200: break
    seen.add(kind)
