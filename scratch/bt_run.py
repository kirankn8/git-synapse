import sys, time
from git_synapse.analysis import backtest as bt
rid = int(sys.argv[1])
t=time.time()
r = bt.run(repo_id=rid, k=5, min_support=2, seeding=sys.argv[2] if len(sys.argv)>2 else "all")
print(f"repo={rid} commits_seen={r.commits_seen} scored={r.commits_scored} prompts={r.prompts} ({time.time()-t:.0f}s)")
for b in r.baselines:
    print(f"  BASE {b.measure:16s} hit={b.hit_rate:.4f} ci=({b.ci_low:.4f},{b.ci_high:.4f}) n={b.prompts}")
for s in r.scores:
    print(f"  MEAS {s.measure:16s} hit={s.hit_rate:.4f} lift={s.lift:.3f} hard={s.hard_hit_rate:.4f} hard_n={s.hard_prompts} base={s.baseline_hit_rate:.4f}")
print(" verdict:", r.verdict)
