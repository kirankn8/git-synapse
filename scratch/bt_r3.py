import sys, random
sys.path.insert(0, "/work/scratch")
from bt_lib import replay
from git_synapse.db.engine import query

rid = int(sys.argv[1])
print("=== D1: does the Apprentice/Intern depend on `file` table row order? ===")
base = replay(rid)
n = base["prompts"]
print(f" as-queried   Apprentice={base['Apprentice']/n:.4f} ({base['Apprentice']})  Intern={base['Intern']/n:.4f} ({base['Intern']})")
for s in (1, 2, 3):
    p = replay(rid, shuffle=random.Random(s))
    print(f" shuffle({s})   Apprentice={p['Apprentice']/n:.4f} ({p['Apprentice']})  Intern={p['Intern']/n:.4f} ({p['Intern']})"
          f"  MEAS={p['MEAS_confidence_ab']/n:.4f} hard_n={p['hard_n']} hardrate={p['hard_hit_confidence_ab']/p['hard_n']:.4f}")
print(" (bt._path_index issues `SELECT id, path, dir_path FROM file` with NO ORDER BY)")
