"""Mining layer: is clustering deterministic, and does the drift SQL agree with
the measure library at every boundary?"""
import sys

sys.path.insert(0, "/work/src")

import numpy as np

from git_synapse.db.engine import connection, query
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import BY_KEY

print("=== drift SQL vs measures.npmi, at every boundary ===")
# The SQL that mining.py runs, isolated, so it can be compared point by point.
SQL = """
SELECT CASE WHEN %(j)s > 0 AND %(n)s > 0 AND %(ma)s > 0 AND %(mb)s > 0
            THEN CASE WHEN %(j)s >= %(n)s THEN 1.0
                 ELSE (ln((%(j)s::numeric * %(n)s) / (%(ma)s::numeric * %(mb)s)) / ln(2))
                      / (-ln(%(j)s::numeric / %(n)s) / ln(2))
                 END
       END AS npmi
"""
cases = [
    # j (joint), n (population), ma, mb
    (5, 100, 10, 10),      # interior
    (1, 100, 1, 1),        # the rarest possible real pair
    (4, 4, 4, 4),          # spans the window: NPMI is +1
    (2, 2, 2, 2),
    (10, 10, 10, 10),
    (50, 100, 50, 50),
    (1, 1000, 500, 500),   # strongly under-associated
    (99, 100, 99, 100),
]
bad = 0
with connection() as conn:
    for j, n, ma, mb in cases:
        sql_v = conn.execute(SQL, {"j": j, "n": n, "ma": ma, "mb": mb}).fetchone()[0]
        sql_v = float(sql_v) if sql_v is not None else None
        table = Contingency.from_counts(n_ab=j, n_a=ma, n_b=mb, n_total=n)
        lib_v = float(np.asarray(BY_KEY["npmi"].compute(table)).ravel()[0])
        agree = sql_v is not None and abs(sql_v - lib_v) < 1e-6
        if not agree:
            bad += 1
        mark = "ok  " if agree else "DIFF"
        print(f"  {mark} j={j:<4} N={n:<5} ma={ma:<4} mb={mb:<4} "
              f"sql={sql_v if sql_v is None else round(sql_v, 6)!s:>10}  lib={lib_v:.6f}")
print(f"  -> {len(cases) - bad}/{len(cases)} boundaries agree")

print()
print("=== label propagation: same input, same clusters? ===")
from git_synapse.analysis import mining

repo = query(
    """SELECT r.id, r.full_name FROM repo r
        WHERE r.is_enabled AND EXISTS (SELECT 1 FROM file_cluster fc WHERE fc.repo_id = r.id)
        ORDER BY r.id LIMIT 1""")
if not repo:
    print("  no clustered repository available")
else:
    rid = repo[0]["id"]
    runs = []
    for i in range(3):
        with connection() as conn:
            mining._rebuild_clusters(conn, rid)
            conn.commit()
        rows = query("SELECT file_id, cluster_id FROM file_cluster WHERE repo_id=%s"
                     " ORDER BY file_id", (rid,))
        # Cluster ids may be renumbered; compare the *partition*, not the labels.
        groups = {}
        for r in rows:
            groups.setdefault(r["cluster_id"], set()).add(r["file_id"])
        runs.append(frozenset(frozenset(g) for g in groups.values()))
        print(f"  run {i + 1}: {len(rows)} files in {len(groups)} clusters")
    print(f"  partition identical across three runs: {len(set(runs)) == 1}"
          f"   ({repo[0]['full_name']})")
