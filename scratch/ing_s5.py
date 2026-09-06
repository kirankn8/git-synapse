"""Round 7: incremental ingest -- add commits, re-ingest, no-op, force-push."""
import sys, json
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from ing_db import *

def line(t): print("\n=========== %s ===========" % t)
def snap(rid):
    return dict(
        commits=q("SELECT count(*) FROM commit WHERE repo_id=%s",(rid,))[0][0],
        files=q("SELECT count(*) FROM file WHERE repo_id=%s",(rid,))[0][0],
        cf=q("SELECT count(*) FROM commit_file WHERE repo_id=%s",(rid,))[0][0],
        parents=q("SELECT count(*) FROM commit_parent WHERE repo_id=%s",(rid,))[0][0],
        alias=q("SELECT count(*) FROM file_alias WHERE repo_id=%s",(rid,))[0][0],
        elig=q("SELECT count(*) FROM commit WHERE repo_id=%s AND pair_eligible",(rid,))[0][0],
    )
def gittruth(m):
    shas = sh(["git","rev-list","--no-merges","HEAD","--tags"], m).stdout.split()
    return len(set(shas))

src = newrepo("inc")
write(src,"a.txt","1\n"); write(src,"b.txt","1\n"); addall(src); commit(src,"c1")
write(src,"a.txt","2\n"); addall(src); commit(src,"c2")
m = bare_mirror(src)
rid = fresh_repo("inc")

line("run 1: initial ingest")
st, tips, _, _ = ingest(rid, m)
print("  stats:", st); print("  tips:", tips); print("  snap:", snap(rid), " git commits:", gittruth(m))

line("run 2: NO new commits -- must be a strict no-op")
before = snap(rid)
st2, tips2, n2, _ = ingest(rid, m, watermarks=tips)
print("  stats:", st2)
after = snap(rid)
print("  before:", before); print("  after :", after)
print("  NO-OP?", "YES" if before == after and st2.commits_read == 0 else "NO  <<<")

line("run 2b: re-ingest with NO watermark at all (idempotency of the merge)")
st2b, _, _, _ = ingest(rid, m, watermarks=[])
print("  stats:", st2b)
print("  snap :", snap(rid), "(must equal", before, ")")
print("  IDEMPOTENT?", "YES" if snap(rid) == before else "NO  <<<")

line("run 3: add commits upstream, incremental ingest")
write(src,"c.txt","1\n"); addall(src); commit(src,"c3")
sh(["git","mv","b.txt","b2.txt"], src); commit(src,"c4 rename b")
sh(["git","fetch","-q","origin","+refs/heads/*:refs/heads/*","+refs/tags/*:refs/tags/*"], m)
st3, tips3, _, _ = ingest(rid, m, watermarks=tips2)
print("  stats:", st3)
print("  snap:", snap(rid), " git commits:", gittruth(m))
print("  files:", q("SELECT path FROM file WHERE repo_id=%s ORDER BY path",(rid,)))
print("  alias:", q("SELECT old_path,file_id FROM file_alias WHERE repo_id=%s",(rid,)))

line("run 3b: full re-ingest from scratch of the SAME repo -> same counts?")
rid_ref = fresh_repo("incref")
stref, _, _, _ = ingest(rid_ref, m)
print("  incremental snap:", snap(rid))
print("  from-scratch snap:", snap(rid_ref))
print("  MATCH?", "YES" if snap(rid) == snap(rid_ref) else "NO  <<<")
# compare per-file change counts
inc = {r[0]: r[1] for r in q("""SELECT f.path, count(*) FROM file f JOIN commit_file cf ON cf.file_id=f.id
                                WHERE f.repo_id=%s GROUP BY f.path""",(rid,))}
ref = {r[0]: r[1] for r in q("""SELECT f.path, count(*) FROM file f JOIN commit_file cf ON cf.file_id=f.id
                                WHERE f.repo_id=%s GROUP BY f.path""",(rid_ref,))}
print("  per-file inc:", inc); print("  per-file ref:", ref)
print("  PER-FILE MATCH?", "YES" if inc == ref else "NO  <<<")

line("run 4: FORCE-PUSH (rewrite history) then re-ingest")
sh(["git","reset","-q","--hard","HEAD~2"], src)
write(src,"rewritten.txt","new history\n"); addall(src); commit(src,"c3-prime (rewritten)")
sh(["git","fetch","-q","--prune","origin","+refs/heads/*:refs/heads/*","+refs/tags/*:refs/tags/*"], m)
print("  git now has:", gittruth(m), "commits;", sh(["git","log","--oneline","HEAD"], m).stdout.replace("\n"," | "))
st4, tips4, _, _ = ingest(rid, m, watermarks=tips3)
print("  stats:", st4)
print("  snap after force-push ingest:", snap(rid), " git commits:", gittruth(m))
print("  commits in DB not in git:")
dbsh = {r[0] for r in q("SELECT sha FROM commit WHERE repo_id=%s",(rid,))}
gsh = set(sh(["git","rev-list","--no-merges","HEAD","--tags"], m).stdout.split())
print("   orphaned:", sorted(s[:8] for s in dbsh - gsh), " missing:", sorted(s[:8] for s in gsh - dbsh))
