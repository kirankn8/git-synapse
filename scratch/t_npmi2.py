"""SQL drift NPMI vs measures.npmi, restricted to FEASIBLE contingency tables."""
import os
import psycopg
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

bad, total = [], 0
for N in (2, 3, 5, 10, 40, 200):
    for ma in range(1, N + 1):
        for mb in range(1, N + 1):
            lo = max(2, ma + mb - N)          # drift's HAVING is j >= 2
            for j in range(lo, min(ma, mb) + 1):
                total += 1
                s = conn.execute(SQL, {"j": j, "N": N, "ma": ma, "mb": mb}).fetchone()[0]
                sv = 0.0 if s is None else float(s)
                pv = float(py_npmi(Contingency.from_counts(j, ma, mb, N)))
                if abs(sv - pv) > 1e-9:
                    bad.append((j, ma, mb, N, s, pv))
print(f"feasible cases={total}  mismatches={len(bad)}")
for r in bad[:60]:
    j, ma, mb, N, s, p = r
    print(f"  j={j} ma={ma} mb={mb} N={N}  sql={s}  py={p}  diff={(0.0 if s is None else float(s))-p:+.6f}")
