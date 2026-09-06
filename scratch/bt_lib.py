"""Instrumented re-implementation of the backtest replay loop.

Mirrors src/git_synapse/analysis/backtest.py exactly for the parts under test,
and adds alternative baselines so their hit rates can be compared side by side
on the same prompts.
"""
from collections import defaultdict
from itertools import combinations
import numpy as np
from git_synapse.analysis import backtest as bt
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import resolve


def replay(repo_id, k=5, min_support=2, seeding="all", measures=("confidence_ab",),
           shuffle=None, extra=()):
    """Returns dict of counters. `shuffle`: a random.Random to permute index order."""
    specs = [resolve(m) for m in measures]
    paths, by_stem, by_dir = bt._path_index(repo_id)
    if shuffle is not None:
        by_stem = {k2: shuffle.sample(v, len(v)) for k2, v in by_stem.items()}
        by_dir = {k2: shuffle.sample(v, len(v)) for k2, v in by_dir.items()}
    commits = bt._history(repo_id)

    joints = defaultdict(lambda: defaultdict(dict))
    marginals = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)

    C = defaultdict(int)
    C["prompts"] = 0
    per = defaultdict(int)          # baseline name -> hit prompts

    def dir_of(p):
        return p.rsplit("/", 1)[0] if "/" in p else ""

    for repo, files, cid in commits:
        joint, marginal, total = joints[repo], marginals[repo], totals[repo]
        if total >= bt.WARMUP_COMMITS and 2 <= len(files) <= bt.MAX_FILES_PER_PROMPT:
            for seed in bt._seeds(files, marginal, seeding):
                targets = set(files) - {seed}
                if not targets:
                    continue
                C["prompts"] += 1
                sp = paths.get(seed)

                appr = bt._neighbours(seed, marginal, k, paths, by_stem, by_dir)
                intern = bt._same_directory(seed, marginal, k, paths, by_dir)
                tour = bt._popular(marginal, seed, k)

                # --- variant A: stem siblings restricted to the SAME directory
                if sp is None:
                    apprA = []
                else:
                    d = dir_of(sp)
                    sib = [f for f in by_stem.get(bt.stem_of(sp), ())
                           if f != seed and f in marginal and dir_of(paths[f]) == d]
                    outA = list(sib)
                    for f in intern:
                        if f not in outA:
                            outA.append(f)
                    apprA = outA[:k]

                # --- variant B: stem siblings anywhere but ordered busiest-first
                if sp is None:
                    apprB = []
                else:
                    sib = sorted((f for f in by_stem.get(bt.stem_of(sp), ())
                                  if f != seed and f in marginal),
                                 key=lambda f: -marginal[f])
                    outB = list(sib)
                    for f in intern:
                        if f not in outB:
                            outB.append(f)
                    apprB = outB[:k]

                # --- variant D: same-dir stem sibs, then other-dir stem sibs busiest,
                #     then the folder busiest-first  ("the file's test, then its folder")
                if sp is None:
                    apprD = []
                else:
                    d = dir_of(sp)
                    sibs = [f for f in by_stem.get(bt.stem_of(sp), ())
                            if f != seed and f in marginal]
                    near = sorted((f for f in sibs if dir_of(paths[f]) == d),
                                  key=lambda f: -marginal[f])
                    far = sorted((f for f in sibs if dir_of(paths[f]) != d),
                                 key=lambda f: -marginal[f])
                    outD = near + far
                    for f in intern:
                        if f not in outD:
                            outD.append(f)
                    apprD = outD[:k]

                # --- variant C: union of the three free rules, k slots each (oracle)
                unionC = set(appr) | set(intern) | set(tour)

                hits = {}
                for nm, g in (("Apprentice", appr), ("Intern", intern), ("Tourist", tour),
                              ("ApprA_samedir", apprA), ("ApprB_busiest", apprB), ("ApprD_fixed", apprD)):
                    h = any(f in targets for f in g)
                    hits[nm] = h
                    per[nm] += h
                per["union_free"] += any(f in targets for f in unionC)
                per["best_of_appr_intern"] += hits["Apprentice"] or hits["Intern"]

                # how often a non-dir stem sibling took a slot
                if sp is not None:
                    d = dir_of(sp)
                    foreign = [f for f in appr if dir_of(paths[f]) != d
                               and bt.stem_of(paths[f]) == bt.stem_of(sp)]
                    if foreign:
                        per["appr_has_foreign_stem"] += 1
                        if len(appr) == k:
                            per["appr_full_with_foreign"] += 1
                        if not hits["Apprentice"] and hits["Intern"]:
                            per["appr_lost_to_intern_w_foreign"] += 1
                if not hits["Apprentice"] and hits["Intern"]:
                    per["appr_miss_intern_hit"] += 1
                if not hits["Apprentice"] and hits["Tourist"]:
                    per["appr_miss_tourist_hit"] += 1

                for spec in specs:
                    got = bt._rank(seed, joint, marginal, total, spec, k, min_support)
                    corr = [f for f in got if f in targets]
                    per["MEAS_" + spec.key] += bool(corr)
                    if not hits["Apprentice"] and corr:
                        per["hard_hit_" + spec.key] += 1
                    if not (hits["Apprentice"] or hits["Intern"] or hits["Tourist"]) and corr:
                        per["hardU_hit_" + spec.key] += 1
                if not hits["Apprentice"]:
                    per["hard_n"] += 1
                if not (hits["Apprentice"] or hits["Intern"] or hits["Tourist"]):
                    per["hardU_n"] += 1

        for a, b in combinations(sorted(set(files)), 2):
            joint[a][b] = joint[a].get(b, 0) + 1
            joint[b][a] = joint[b].get(a, 0) + 1
        for f in set(files):
            marginal[f] += 1
        totals[repo] += 1
    per["prompts"] = C["prompts"]
    return dict(per)
