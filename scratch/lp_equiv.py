"""The sparse sweep must produce exactly what the dense one did."""
import sys
sys.path.insert(0, "/work/src")
import numpy as np

ROUNDS = 10

def dense(n, src, dst, weight):
    labels = np.arange(n, dtype=np.int64)
    for _ in range(ROUNDS):
        keys = np.concatenate([dst * n + labels[src], src * n + labels[dst]])
        weights = np.concatenate([weight, weight])
        totals = np.bincount(keys, weights=weights, minlength=n * n).reshape(n, n)
        best = totals.argmax(axis=1)
        new = np.where(totals.max(axis=1) > 0, best, labels)
        if np.array_equal(new, labels):
            break
        labels = new
    return labels

def sparse(n, src, dst, weight):
    labels = np.arange(n, dtype=np.int64)
    end = np.concatenate([dst, src]); other = np.concatenate([src, dst])
    both = np.concatenate([weight, weight])
    for _ in range(ROUNDS):
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
        if np.array_equal(new, labels):
            break
        labels = new
    return labels

rng = np.random.default_rng(20260907)
mismatch = 0
for trial in range(400):
    n = int(rng.integers(2, 40))
    e = int(rng.integers(1, 120))
    src = rng.integers(0, n, e); dst = rng.integers(0, n, e)
    keep = src != dst
    src, dst = src[keep], dst[keep]
    if not len(src):
        continue
    # Include exact ties, which is where tie-breaking has to agree.
    w = rng.choice([1.0, 1.0, 1.0, 0.5, 2.0, 0.25], size=len(src))
    a, b = dense(n, src, dst, w), sparse(n, src, dst, w)
    if not np.array_equal(a, b):
        mismatch += 1
        if mismatch == 1:
            print("first mismatch:", n, len(src)); print(" dense ", a); print(" sparse", b)
print(f"{400 - mismatch}/400 random graphs identical (ties included)")

n = 7007
print(f"dense matrix for wireshark-sized input: {n*n*8/1e6:.0f} MB per round")
