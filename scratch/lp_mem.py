"""Peak memory of both sweeps on the real worst case, read-only."""
import sys, tracemalloc
sys.path.insert(0, "/work/src")
import numpy as np
from git_synapse.db.engine import connection

with connection() as conn:
    conn.execute("SET max_parallel_workers_per_gather = 0")
    rid = conn.execute(
        "SELECT id FROM repo WHERE full_name = 'wireshark/wireshark'").fetchone()[0]
    edges = conn.execute(
        """SELECT fp.file_a_id, fp.file_b_id, m.npmi
             FROM file_pair fp JOIN file_pair_metric m
               ON m.repo_id=fp.repo_id AND m.file_a_id=fp.file_a_id
              AND m.file_b_id=fp.file_b_id
            WHERE fp.repo_id=%s AND fp.n_ab >= 3 AND m.npmi > 0""", (rid,)).fetchall()

nodes = sorted({int(e[0]) for e in edges} | {int(e[1]) for e in edges})
index = {n: i for i, n in enumerate(nodes)}
src = np.array([index[int(e[0])] for e in edges], dtype=np.int64)
dst = np.array([index[int(e[1])] for e in edges], dtype=np.int64)
weight = np.array([float(e[2] or 0.0) for e in edges], dtype=np.float64)
n = len(nodes)
print(f"wireshark: {n} coupled files, {len(edges)} edges")

def one_dense_round(labels):
    keys = np.concatenate([dst * n + labels[src], src * n + labels[dst]])
    w = np.concatenate([weight, weight])
    t = np.bincount(keys, weights=w, minlength=n * n).reshape(n, n)
    return np.where(t.max(axis=1) > 0, t.argmax(axis=1), labels)

def one_sparse_round(labels):
    end = np.concatenate([dst, src]); other = np.concatenate([src, dst])
    both = np.concatenate([weight, weight])
    lab = labels[other]
    order = np.lexsort((lab, end))
    end_s, lab_s, w_s = end[order], lab[order], both[order]
    starts = np.empty(len(order), dtype=bool); starts[0] = True
    starts[1:] = (end_s[1:] != end_s[:-1]) | (lab_s[1:] != lab_s[:-1])
    totals = np.bincount(np.cumsum(starts) - 1, weights=w_s)
    g_node, g_label = end_s[starts], lab_s[starts]
    pick = np.lexsort((g_label, -totals, g_node))
    pn = g_node[pick]
    first = np.empty(len(pn), dtype=bool); first[0] = True
    first[1:] = pn[1:] != pn[:-1]
    new = labels.copy(); new[pn[first]] = g_label[pick][first]
    return new

for name, fn in (("dense (before)", one_dense_round), ("sparse (now)", one_sparse_round)):
    labels = np.arange(n, dtype=np.int64)
    tracemalloc.start()
    out = fn(labels)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  {name:16} peak {peak/1e6:8.1f} MB for one round")
