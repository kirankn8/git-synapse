from git_synapse.analysis import backtest as bt
from git_synapse.db.engine import query
repos = {r["full_name"].split("/")[-1]: r["id"] for r in query("SELECT id, full_name FROM repo")}
for name in ("flatbuffers", "pytype", "osv-scanner", "closure-compiler", "go-github", "guava"):
    res = bt.run(repo_id=repos[name], measures=("confidence_ab",))
    ap = next(b for b in res.baselines if b.measure == "neighbours")
    print(f"{name}\t{res.prompts}\t{ap.hit_rate*100:.1f}\t{res.scores[0].hard_hit_rate*100:.1f}")
