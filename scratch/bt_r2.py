import sys
sys.path.insert(0, "/work/scratch")
from bt_lib import replay
REPOS = {2:"go-github",5:"guava",368:"tokio",27:"pytype",1396:"zx",3:"brotli",19:"flatbuffers",347:"flask"}
for rid in [int(x) for x in sys.argv[1:]]:
    p = replay(rid)
    n = p["prompts"]
    f = lambda key: p.get(key,0)/n
    print(f"\n=== repo {rid} {REPOS.get(rid,'')}  prompts={n}")
    for nm in ("Apprentice","Intern","Tourist","ApprA_samedir","ApprB_busiest","ApprD_fixed",
               "best_of_appr_intern","union_free"):
        print(f"   {nm:22s} {f(nm):.4f}   ({p.get(nm,0)})")
    print(f"   MEAS confidence_ab     {f('MEAS_confidence_ab'):.4f}")
    best_impl = max(p['Apprentice'],p['Intern'],p['Tourist'])/n
    print(f"   lift vs impl-best      {f('MEAS_confidence_ab')/best_impl:.3f}")
    for alt in ("ApprA_samedir","ApprB_busiest","ApprD_fixed","union_free"):
        b = max(best_impl, f(alt))
        print(f"   lift vs {alt:16s} {f('MEAS_confidence_ab')/b:.3f}")
    print(f"   foreign-stem in Apprentice list: {p.get('appr_has_foreign_stem',0)} ({p.get('appr_has_foreign_stem',0)/n:.3%})"
          f"  of which list was full: {p.get('appr_full_with_foreign',0)}")
    print(f"   Apprentice MISS but Intern HIT : {p.get('appr_miss_intern_hit',0)} ({p.get('appr_miss_intern_hit',0)/n:.3%})")
    print(f"   Apprentice MISS but Tourist HIT: {p.get('appr_miss_tourist_hit',0)}")
    print(f"   hard_n (Appr miss)   ={p.get('hard_n',0)}  rate={p.get('hard_hit_confidence_ab',0)/max(p.get('hard_n',1),1):.4f}")
    print(f"   hardU_n (all 3 miss) ={p.get('hardU_n',0)}  rate={p.get('hardU_hit_confidence_ab',0)/max(p.get('hardU_n',1),1):.4f}")
