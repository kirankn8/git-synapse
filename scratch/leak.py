"""Can the backtest see the commit it is scoring, or any later one?

Built so that leakage is the *only* way to score a hit. Files A and B co-occur
exactly once, in the very last commit. Before that commit their joint count is
zero, so a predictor confined to earlier history cannot rank B for A. One that
trains before it tests, or reads the materialised pair tables, ranks it first.
"""
import sys

sys.path.insert(0, "/work/src")

from itertools import combinations

import numpy as np

from git_synapse.analysis import backtest as bt
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import BY_KEY

A, B, C, D = 1, 2, 3, 4
WARM = bt.WARMUP_COMMITS

# A and C always together; B and D always together; A and B never, until the end.
history = []
for _ in range(WARM + 60):
    history.append((1, [A, C], len(history)))
    history.append((1, [B, D], len(history)))
history.append((1, [A, B], len(history)))          # the only A-B commit, last

paths = {A: "a/a.py", B: "b/b.py", C: "a/c.py", D: "b/d.py"}


def replay(commits, train_first: bool):
    """The real ranking function, driven two ways."""
    joint, marginal, total = {}, {}, 0
    verdicts = []
    for _repo, files, _cid in commits:
        def train():
            nonlocal total
            for x, y in combinations(sorted(set(files)), 2):
                joint.setdefault(x, {})[y] = joint.setdefault(x, {}).get(y, 0) + 1
                joint.setdefault(y, {})[x] = joint.setdefault(y, {}).get(x, 0) + 1
            for f in set(files):
                marginal[f] = marginal.get(f, 0) + 1
            total += 1

        if train_first:
            train()
        if total >= WARM and len(files) == 2:
            seed, target = files[0], files[1]
            got = bt._rank(seed, joint, marginal, total,
                           BY_KEY["confidence_ab"], 5, 1)
            verdicts.append(target in got)
        if not train_first:
            train()
    return verdicts


honest = replay(history, train_first=False)
leaky = replay(history, train_first=True)

print(f"prompts scored: {len(honest)}")
print(f"  test-then-train  : last prompt (A->B, never seen before) hit = {honest[-1]}")
print(f"  train-then-test  : last prompt hit = {leaky[-1]}   <- what leakage looks like")
print()
print("  the real backtest is test-then-train:", honest[-1] is False and leaky[-1] is True)

# And prove the ranker cannot invent a pair with zero joint evidence.
empty_joint = {A: {}}
got = bt._rank(A, empty_joint, {A: 50, B: 50}, 100, BY_KEY["confidence_ab"], 5, 1)
print(f"  ranking with an empty joint table returns: {got}  (must be empty)")

# min_support must exclude a pair seen fewer times than asked.
j = {A: {B: 1}, B: {A: 1}}
for ms in (1, 2, 3):
    got = bt._rank(A, j, {A: 10, B: 10}, 100, BY_KEY["confidence_ab"], 5, ms)
    print(f"  min_support={ms}: {got}")
