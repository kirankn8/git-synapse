"""Round 5: fan-out cap, merge filter, marginal counts, replay."""
import sys
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from ing_db import *
from git_synapse.analysis.aggregate import rebuild_repo

def line(t): print("\n=========== %s ===========" % t)

line("A. fan-out cap: cap=3")
d = newrepo("cap")
write(d,"a.txt","1\n"); write(d,"b.txt","1\n"); addall(d); commit(d,"two files")
for i in range(5): write(d,f"z{i}.txt","1\n")
addall(d); commit(d,"five files (over cap)")
write(d,"a.txt","2\n"); write(d,"b.txt","2\n"); write(d,"c.txt","1\n"); addall(d); commit(d,"three files (at cap)")
commit(d,"empty commit", allow_empty=True)
m = bare_mirror(d)
rid = fresh_repo("cap")
from git_synapse.ingest.store import load_commits
from git_synapse.ingest.parser import iter_commits
from git_synapse.db.engine import connection
with connection() as conn:
    st = load_commits(rid, iter_commits(m, reverse=True, include_tags=True), conn, max_files_per_commit=3)
print("  stats:", st)
for r in q("SELECT subject, n_files, is_merge, pair_eligible FROM commit WHERE repo_id=%s ORDER BY subject",(rid,)):
    print("   ", r)
print("  EXPECT eligible: 'two files'=T 'three files (at cap)'=T 'five files (over cap)'=F 'empty commit'=F")

line("B. marginal counts: file.pair_change_count == # pair-eligible commits touching the file")
rebuild_repo(rid)
for r in q("SELECT path, change_count, pair_change_count FROM file WHERE repo_id=%s ORDER BY path",(rid,)):
    truth = q("""SELECT count(*) FILTER (WHERE c.pair_eligible), count(*)
                 FROM commit_file cf JOIN commit c ON c.id=cf.commit_id
                 JOIN file f ON f.id=cf.file_id
                 WHERE cf.repo_id=%s AND f.path=%s""",(rid,r[0]))[0]
    flag = "OK " if (r[2]==truth[0] and r[1]==truth[1]) else "BAD"
    print(f"   {flag} {r[0]}: stored change={r[1]} pair={r[2]} | truth change={truth[1]} pair={truth[0]}")

line("C. replay: cherry-pick onto a release branch, tagged")
d2 = newrepo("replay")
write(d2,"x.txt","x1\nx2\nx3\n"); write(d2,"y.txt","y1\n"); addall(d2); commit(d2,"base")
sh(["git","checkout","-q","-b","rel-1"], d2)
sh(["git","checkout","-q","main"], d2)
write(d2,"x.txt","x1\nx2\nx3\nFIX\n"); write(d2,"y.txt","y1\nFIX\n"); addall(d2)
fix = commit(d2,"the fix (main)")
write(d2,"x.txt","x1\nx2\nx3\nFIX\nmore\n"); addall(d2); commit(d2,"unrelated main work")
sh(["git","checkout","-q","rel-1"], d2)
sh(["git","cherry-pick","-x", fix], d2)
sh(["git","tag","v1.0.1"], d2)
# a DISTINCT commit on the release branch that is NOT a replay
write(d2,"z.txt","z\n"); addall(d2); commit(d2,"release-only work")
sh(["git","tag","v1.0.2"], d2)
sh(["git","checkout","-q","main"], d2)
m2 = bare_mirror(d2)
sh(["git","fetch","-q","origin","+refs/tags/*:refs/tags/*"], m2)
rid2 = fresh_repo("replay")
st2, tips, nmarked, replays = ingest(rid2, m2)
print("  stats:", st2)
print("  replayed_commits() returned:", {s[:8] for s in replays}, "marked:", nmarked)
for r in q("SELECT sha, subject, pair_eligible, is_replay FROM commit WHERE repo_id=%s ORDER BY authored_at, sha",(rid2,)):
    print("   ", r[0][:8], r[1][:32], "elig=%s replay=%s" % (r[2], r[3]))
print("  EXPECT: cherry-pick commit stored, is_replay=T, pair_eligible=F; 'release-only work' NOT a replay")

line("D. does patch-id falsely mark DISTINCT commits as replays?")
d3 = newrepo("falsereplay")
write(d3,"f1.txt","base\n"); write(d3,"f2.txt","base\n"); addall(d3); commit(d3,"base")
sh(["git","checkout","-q","-b","rel"], d3)
sh(["git","checkout","-q","main"], d3)
# main: two EMPTY commits and one that only changes a file mode
commit(d3,"empty on main", allow_empty=True)
write(d3,"f1.txt","base\nsame-line\n"); addall(d3); commit(d3,"same content change to f1 (main)")
sh(["git","checkout","-q","rel"], d3)
commit(d3,"empty on rel (DISTINCT)", allow_empty=True)
# a different file gets the SAME textual patch
write(d3,"f2.txt","base\nsame-line\n"); addall(d3); commit(d3,"same content change to f2 (rel, DISTINCT)")
sh(["git","tag","vrel"], d3)
sh(["git","checkout","-q","main"], d3)
m3 = bare_mirror(d3)
sh(["git","fetch","-q","origin","+refs/tags/*:refs/tags/*"], m3)
rid3 = fresh_repo("falsereplay")
st3, tips3, n3, rep3 = ingest(rid3, m3)
print("  replayed:", {s[:8] for s in rep3}, "marked:", n3)
for r in q("SELECT sha, subject, pair_eligible, is_replay FROM commit WHERE repo_id=%s ORDER BY authored_at, sha",(rid3,)):
    print("   ", r[0][:8], r[1][:44], "elig=%s replay=%s" % (r[2], r[3]))
print("  EXPECT: NOTHING marked replay -- every rel commit is a genuinely different change")
