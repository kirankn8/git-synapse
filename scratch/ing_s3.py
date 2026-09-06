"""Round 6: patch-id false positives on NON-empty distinct commits; annotated tags."""
import sys
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from ing_db import *

def line(t): print("\n=========== %s ===========" % t)

line("A. distinct commits that add DIFFERENT empty files / chmod DIFFERENT files")
d = newrepo("pid")
write(d,"seed.txt","s\n"); write(d,"m1.sh","#!/bin/sh\necho 1\n"); write(d,"m2.sh","#!/bin/sh\necho 2\n")
write(d,"b1.bin", bytes(range(200))); write(d,"b2.bin", bytes(range(200,0,-1)))
addall(d); commit(d,"base")
sh(["git","checkout","-q","-b","rel"], d)
sh(["git","checkout","-q","main"], d)
# main side
write(d,"empty_main.txt",""); addall(d); commit(d,"add EMPTY FILE empty_main.txt (main)")
sh(["git","update-index","--chmod=+x","m1.sh"], d); commit(d,"chmod m1.sh (main)")
write(d,"b1.bin", bytes(range(200))+b"\x99"); addall(d); commit(d,"binary change b1 (main)")
sh(["git","checkout","-q","rel"], d)
# rel side - all DIFFERENT changes
write(d,"empty_rel.txt",""); addall(d); commit(d,"add EMPTY FILE empty_rel.txt (rel, DISTINCT)")
sh(["git","update-index","--chmod=+x","m2.sh"], d); commit(d,"chmod m2.sh (rel, DISTINCT)")
write(d,"b2.bin", bytes(range(200,0,-1))+b"\x77"); addall(d); commit(d,"binary change b2 (rel, DISTINCT)")
sh(["git","tag","-a","v9.9","-m","annotated release"], d)
sh(["git","checkout","-q","main"], d)
m = bare_mirror(d)
sh(["git","fetch","-q","origin","+refs/tags/*:refs/tags/*"], m)
print("  tag object type:", sh(["git","cat-file","-t","v9.9"], m).stdout.strip())
rid = fresh_repo("pid")
st, tips, n, rep = ingest(rid, m)
print("  stats:", st)
print("  marked replay:", n)
for r in q("SELECT sha, subject, n_files, pair_eligible, is_replay FROM commit WHERE repo_id=%s ORDER BY authored_at, sha",(rid,)):
    tag = "  <<< FALSE REPLAY" if (r[4] and "DISTINCT" in r[1]) else ""
    print("   ", r[0][:8], r[1][:46], "n=%s elig=%s replay=%s" % (r[2],r[3],r[4]), tag)
print("  raw git cherry-mark output:")
print(sh(["git","rev-list","--cherry-mark","--right-only","--no-merges","--oneline","main...v9.9"], m).stdout)

line("B. annotated tag: does replayed_commits() peel the tag object?")
d2 = newrepo("anntag")
write(d2,"x.txt","x1\nx2\nx3\nx4\n"); addall(d2); commit(d2,"base")
sh(["git","checkout","-q","-b","rel"], d2); sh(["git","checkout","-q","main"], d2)
write(d2,"x.txt","x1\nx2\nx3\nx4\nFIXLINE\n"); addall(d2); fix=commit(d2,"real fix")
sh(["git","checkout","-q","rel"], d2)
sh(["git","cherry-pick", fix], d2)
sh(["git","tag","-a","v2.0","-m","rel"], d2)
sh(["git","checkout","-q","main"], d2)
m2 = bare_mirror(d2); sh(["git","fetch","-q","origin","+refs/tags/*:refs/tags/*"], m2)
rid2 = fresh_repo("anntag")
st2, tips2, n2, rep2 = ingest(rid2, m2)
print("  replayed set:", {s[:8] for s in rep2}, "marked:", n2)
for r in q("SELECT sha, subject, pair_eligible, is_replay FROM commit WHERE repo_id=%s ORDER BY authored_at, sha",(rid2,)):
    print("   ", r[0][:8], r[1][:34], "elig=%s replay=%s" % (r[2],r[3]))
print("  EXPECT the cherry-picked copy on rel marked replay")

line("C. ref_tips vs annotated tags")
print("  ref_tips:", tips2)
print("  for-each-ref objectname:", sh(["git","for-each-ref","--format=%(refname) %(objecttype) %(objectname)","refs/tags"], m2).stdout.strip())
