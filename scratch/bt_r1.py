"""Round 1: leakage, warmup, per-repo isolation, in-process."""
import sys, types, random
from collections import defaultdict
from git_synapse.analysis import backtest as bt

SQL = []
_realquery = bt.query
def spy(sql, params=None, *a, **kw):
    SQL.append(" ".join(sql.split()))
    return _realquery(sql, params, *a, **kw)

def replay(commits, paths=None, **kw):
    """commits: list of (repo, [file ids]); returns BacktestResult"""
    orig_hist, orig_pi, orig_q = bt._history, bt._path_index, bt.query
    if paths is None:
        ids = {f for _, files in commits for f in files}
        paths = {f: f"repo/dir/f{f}.py" for f in ids}
    by_stem, by_dir = {}, {}
    for fid, path in paths.items():
        by_stem.setdefault(bt.stem_of(path), []).append(fid)
        by_dir.setdefault(path.rsplit("/", 1)[0] if "/" in path else "", []).append(fid)
    bt._history = lambda repo_id: [(r, f, i) for i, (r, f) in enumerate(commits)]
    bt._path_index = lambda repo_id: (paths, by_stem, by_dir)
    bt.query = spy
    try:
        return bt.run(**kw)
    finally:
        bt._history, bt._path_index, bt.query = orig_hist, orig_pi, orig_q

W = bt.WARMUP_COMMITS
print("WARMUP_COMMITS =", W, "MAX_FILES_PER_PROMPT =", bt.MAX_FILES_PER_PROMPT)

# --------------------------------------------------------------------------
# L1. Own-commit leakage with min_support boundary.
# Pair (1,2) seen exactly ONCE before the scored commit. min_support=2.
# Correct (no leak): support=1 < 2  -> no candidate -> miss.
# Leaky: own commit folded in first -> support=2 -> hit.
# --------------------------------------------------------------------------
commits = [(1, [90, 91]) for _ in range(W)]      # warmup, unrelated files
commits += [(1, [1, 2])]                          # pair seen once (still warmup? no: total==W now)
PY = commits
# careful: after W warmup commits total==W so commit index W IS scored.
# Make the "seen once" commit part of warmup instead:
commits = [(1, [90, 91]) for _ in range(W - 1)]
commits += [(1, [1, 2])]        # this is warmup commit #W  -> not scored, but trains
commits += [(1, [1, 2])]        # scored: prior support == 1
r = replay(commits, k=5, min_support=2)
print("L1 min_support=2  prompts=%d hits=%d  (expect hits=0)" % (r.prompts, r.scores[0].hit_prompts))
r = replay(commits, k=5, min_support=1)
print("L1 min_support=1  prompts=%d hits=%d  (expect hits=2, one per seed)" % (r.prompts, r.scores[0].hit_prompts))

# --------------------------------------------------------------------------
# L2. Pure future leakage: pair (1,2) never seen before, but seen many times AFTER.
# --------------------------------------------------------------------------
commits = [(1, [90, 91]) for _ in range(W)]
commits += [(1, [1, 2])]                       # scored, zero prior evidence
commits += [(1, [1, 2]) for _ in range(50)]    # lots of future evidence
r = replay(commits, k=5, min_support=1)
s = r.scores[0]
print("L2 future-only: prompts=%d hits=%d (first scored commit must miss)" % (r.prompts, s.hit_prompts))
# how many of the prompts are the very first (1,2) commit?
print("   note prompts includes later (1,2) commits which legitimately hit")

# isolate: limit to exactly W+1 commits
r = replay(commits, k=5, min_support=1, limit=W + 1)
print("L2b limit=W+1: prompts=%d hits=%d (expect 2 prompts, 0 hits)" % (r.prompts, r.scores[0].hit_prompts))

# --------------------------------------------------------------------------
# L3. marginal / N leakage. Does the scored commit's own marginal appear?
# --------------------------------------------------------------------------
seen = {}
orig_rank = bt._rank
def rank_spy(seed, joint, marginal, total, spec, k, min_support):
    seen.setdefault("calls", []).append((seed, dict(marginal), total,
                                         {a: dict(b) for a, b in joint.items()}))
    return orig_rank(seed, joint, marginal, total, spec, k, min_support)
bt._rank = rank_spy
commits = [(1, [90, 91]) for _ in range(W)]
commits += [(1, [1, 2, 3])]
r = replay(commits, k=5, min_support=1, limit=W + 1)
bt._rank = orig_rank
for seed, marg, total, joint in seen["calls"]:
    print("L3 seed=%s total=%d marginal(1,2,3)=%s joint_keys=%s"
          % (seed, total, {f: marg.get(f, 0) for f in (1, 2, 3)}, sorted(joint)))
print("   expect total=%d, marginals 0, joint has no 1/2/3" % W)

# --------------------------------------------------------------------------
# L4. SQL actually executed
# --------------------------------------------------------------------------
print("L4 SQL executed during run():", SQL or "(none - _history/_path_index patched)")

# --------------------------------------------------------------------------
# W1. warmup boundary: exactly which commits get scored
# --------------------------------------------------------------------------
scored = []
orig_seeds = bt._seeds
def seeds_spy(files, marginal, seeding):
    scored.append(tuple(files))
    return orig_seeds(files, marginal, seeding)
bt._seeds = seeds_spy
commits = [(1, [i, i + 1000]) for i in range(1, W + 4)]
r = replay(commits, k=5, min_support=1)
bt._seeds = orig_seeds
print("W1 scored commits (files):", scored)
print("   commits_seen=%d commits_scored=%d" % (r.commits_seen, r.commits_scored))
print("   -> first scored is commit index %d (0-based)" % (len(commits) - len(scored)))

# --------------------------------------------------------------------------
# R1. per-repo isolation of warmup: does repo B get its own warmup?
# --------------------------------------------------------------------------
scored = []
bt._seeds = seeds_spy
commits = [(1, [i, i + 1000]) for i in range(1, W + 2)]     # repo1 warm
commits += [(2, [5001, 5002])]                              # repo2 first commit
r = replay(commits, k=5, min_support=1)
bt._seeds = orig_seeds
print("R1 scored:", scored, " (repo2 commit 5001/5002 must NOT be scored)")
