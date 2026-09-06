"""Converge the corpus, ingest it, then audit what came out. Hours, not minutes.

Three phases, logged as they go so the state is readable at any moment:

1. discover  -- resolve every source. GitHub allows sixty requests an hour
                without a token, so this is paced by refills, not by us.
2. ingest    -- mirror, parse, aggregate, score and mine everything found.
3. audit     -- the invariants, over whatever the corpus now is. A larger and
                more varied corpus is the point: an invariant that holds on
                163 repositories of six orgs has been tested much less than one
                that holds across three hosts and thirty languages.
"""
from __future__ import annotations

import sys
import time
import traceback

sys.path.insert(0, "/app/src")

from git_synapse.db.engine import query, query_one  # noqa: E402
from git_synapse.ingest import accounts, pipeline  # noqa: E402


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def budget() -> tuple[int, int]:
    import httpx

    try:
        core = httpx.get("https://api.github.com/rate_limit",
                         timeout=15).json()["resources"]["core"]
        return core["remaining"], max(0, int(core["reset"] - time.time()))
    except Exception:  # noqa: BLE001
        return 0, 300


def unresolved() -> list[dict]:
    return [a for a in accounts.list_accounts(enabled_only=True) if not a["repo_count"]]


def phase_discover(deadline: float) -> None:
    log("=== phase 1: discovery ===")
    while time.time() < deadline:
        pend = unresolved()
        total = len(accounts.list_accounts(enabled_only=True))
        log(f"{total - len(pend)}/{total} sources resolved")
        if not pend:
            log("every source resolved")
            return
        remaining, reset_in = budget()
        if remaining < 5 and all(a["host"] == "github.com" for a in pend):
            wait = min(reset_in + 30, deadline - time.time())
            if wait <= 0:
                break
            log(f"budget spent; waiting {int(wait)}s")
            time.sleep(wait)
            continue
        try:
            found = pipeline.discover(trigger="corpus-expansion")
            log(f"discovery returned {len(found)} repositories")
        except Exception as exc:  # noqa: BLE001
            log(f"discovery raised: {str(exc)[:140]}")
            time.sleep(30)
    log("discovery phase over (deadline or converged)")
    still = unresolved()
    if still:
        log(f"{len(still)} source(s) never resolved:")
        for a in still[:15]:
            log(f"    {a['login']:28} {str(a.get('last_discover_error'))[:70]}")


def phase_ingest() -> None:
    log("=== phase 2: ingest ===")
    started = time.time()
    try:
        run = pipeline.run_ingest(trigger="corpus-expansion")
        log(f"run {run.run_id}: {run.status}, {len(run.ok)} ok, {len(run.failed)} failed, "
            f"{run.commits_added} commits, {int(time.time() - started)}s")
        for r in run.failed[:20]:
            log(f"    FAILED {r.full_name}: {str(r.error)[:90]}")
    except Exception:  # noqa: BLE001
        log("ingest raised:\n" + traceback.format_exc()[-1200:])


def phase_audit() -> None:
    log("=== phase 3: audit ===")
    checks: list[tuple[str, str]] = [
        ("contingency: n_ab > min(n_a,n_b)",
         "SELECT count(*) FROM file_pair_metric WHERE n_ab > LEAST(n_a, n_b)"),
        ("contingency: marginal > N",
         "SELECT count(*) FROM file_pair_metric WHERE n_a > n_total OR n_b > n_total"),
        ("marginals disagree with the file table",
         "SELECT count(*) FROM file_pair_metric m JOIN file f ON f.id=m.file_a_id"
         " WHERE m.n_a <> f.pair_change_count"),
        ("N disagrees with the population",
         "SELECT count(*) FROM file_pair_metric m JOIN repo r ON r.id=m.repo_id"
         " WHERE m.n_total <> r.pair_population"),
        ("a measure outside [0,1]",
         "SELECT count(*) FROM file_pair_metric WHERE jaccard<-1e-9 OR jaccard>1+1e-9"
         " OR dice<-1e-9 OR dice>1+1e-9 OR ochiai<-1e-9 OR ochiai>1+1e-9"
         " OR confidence_ab<-1e-9 OR confidence_ab>1+1e-9"),
        ("a signed measure outside [-1,1]",
         "SELECT count(*) FROM file_pair_metric WHERE npmi<-1-1e-9 OR npmi>1+1e-9"
         " OR phi<-1-1e-9 OR phi>1+1e-9 OR yules_q<-1-1e-9 OR yules_q>1+1e-9"),
        ("fager below its declared -0.5",
         "SELECT count(*) FROM file_pair_metric WHERE fager < -0.5 - 1e-9"),
        ("a non-finite value",
         "SELECT count(*) FROM file_pair_metric WHERE npmi='NaN'::float8"
         " OR pmi='NaN'::float8 OR chi_square='NaN'::float8"
         " OR log_likelihood_ratio='Infinity'::float8"),
        ("negative zero stored",
         "SELECT count(*) FROM file_pair_metric"
         " WHERE poisson_significance = 0 AND 1/poisson_significance < 0"),
        ("jaccard <> a/(a+b+c)",
         "SELECT count(*) FROM file_pair_metric"
         " WHERE abs(jaccard - n_ab::float8/(n_a+n_b-n_ab)) > 1e-9"),
        ("confidence <> a/n_a",
         "SELECT count(*) FROM file_pair_metric"
         " WHERE n_a>0 AND abs(confidence_ab - n_ab::float8/n_a) > 1e-9"),
        ("metric row without a pair",
         "SELECT count(*) FROM file_pair_metric m LEFT JOIN file_pair p"
         " ON p.repo_id=m.repo_id AND p.file_a_id=m.file_a_id AND p.file_b_id=m.file_b_id"
         " WHERE p.repo_id IS NULL"),
        ("pair spanning two repositories",
         "SELECT count(*) FROM file_pair p JOIN file fa ON fa.id=p.file_a_id"
         " JOIN file fb ON fb.id=p.file_b_id WHERE fa.repo_id <> fb.repo_id"),
        ("cached commit_count wrong",
         "SELECT count(*) FROM repo r WHERE r.is_enabled AND r.commit_count <>"
         " (SELECT count(*) FROM commit c WHERE c.repo_id=r.id)"),
        ("pair_population wrong",
         "SELECT count(*) FROM repo r WHERE r.is_enabled AND r.pair_population <>"
         " (SELECT count(*) FROM commit c WHERE c.repo_id=r.id AND c.pair_eligible)"),
        ("replayed commit counted",
         "SELECT count(*) FROM commit WHERE is_replay AND pair_eligible"),
        ("commit over the fan-out cap",
         "SELECT count(*) FROM (SELECT cf.commit_id FROM commit_file cf"
         " JOIN commit c ON c.id=cf.commit_id WHERE c.pair_eligible"
         " GROUP BY cf.commit_id HAVING count(*) > 60 LIMIT 50) x"),
        ("impact edge with no evidence",
         "SELECT count(*) FROM repo_impact WHERE NOT (is_declared OR has_bump_history)"),
        ("negative adoption delay",
         "SELECT count(*) FROM dep_bump WHERE adoption_seconds < 0"),
        ("risk score outside [0,2]",
         "SELECT count(*) FROM file_risk WHERE risk_score < -1e-9 OR risk_score > 2+1e-9"),
        ("drift delta disagrees with its windows",
         "SELECT count(*) FROM pair_drift WHERE abs(delta-(npmi_recent-npmi_historic))>1e-9"),
        ("file in two clusters",
         "SELECT count(*) FROM (SELECT file_id FROM file_cluster"
         " GROUP BY file_id HAVING count(*)>1) x"),
    ]
    bad = 0
    for name, sql in checks:
        try:
            n = int(query(sql)[0]["count"])
        except Exception as exc:  # noqa: BLE001
            log(f"  ?? {name:44} query failed: {str(exc)[:60]}")
            continue
        if n:
            bad += 1
            log(f"  FAIL {name:44} {n}")
        else:
            log(f"  ok   {name:44} 0")
    shape = query_one(
        "SELECT (SELECT count(*) FROM repo WHERE is_enabled) AS repos,"
        " (SELECT count(*) FROM repo WHERE is_enabled AND commit_count>0) AS ingested,"
        " (SELECT count(*) FROM commit) AS commits,"
        " (SELECT count(*) FROM file) AS files,"
        " (SELECT count(*) FROM file_pair) AS pairs,"
        " (SELECT count(DISTINCT host) FROM repo WHERE is_enabled) AS hosts,"
        " (SELECT count(DISTINCT primary_language) FROM repo WHERE is_enabled) AS langs")
    log(f"corpus: {shape['ingested']}/{shape['repos']} repositories ingested, "
        f"{shape['commits']} commits, {shape['files']} files, {shape['pairs']} pairs, "
        f"{shape['hosts']} hosts, {shape['langs']} languages")
    for row in query("SELECT host, count(*) AS n FROM repo WHERE is_enabled"
                     " GROUP BY host ORDER BY n DESC"):
        log(f"    {row['host']:18} {row['n']}")
    log(f"AUDIT: {len(checks) - bad}/{len(checks)} invariants hold")


def main() -> None:
    # Leave plenty of the window for ingest, which is the slow part.
    phase_discover(deadline=time.time() + 4 * 3600)
    phase_ingest()
    phase_audit()
    log("=== done ===")


if __name__ == "__main__":
    main()
