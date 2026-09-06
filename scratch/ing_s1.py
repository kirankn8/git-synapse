"""Round 4: rename identity, re-add after rename, two files into one path."""
import sys
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from ing_db import *

def line(t): print("\n=========== %s ===========" % t)

line("A. one file renamed three times -> ONE identity, ONE history")
d = newrepo("r3")
write(d, "one.txt", "a\nb\nc\nd\ne\n"); addall(d); commit(d, "create one.txt")
sh(["git","mv","one.txt","two.txt"], d); commit(d, "-> two.txt")
write(d, "two.txt","a\nb\nc\nd\ne\nf\n"); addall(d); commit(d,"touch two.txt")
sh(["git","mv","two.txt","three.txt"], d); commit(d, "-> three.txt")
sh(["git","mv","three.txt","four.txt"], d); commit(d, "-> four.txt")
write(d,"four.txt","a\nb\nc\nd\ne\nf\ng\n"); addall(d); commit(d,"touch four.txt")
m = bare_mirror(d)
rid = fresh_repo("r3")
st,_,_,_ = ingest(rid, m)
print("  stats:", st)
dump(rid, "renamed 3x")
n = q("SELECT count(*) FROM file WHERE repo_id=%s",(rid,))[0][0]
print("  EXPECT 1 file row; GOT", n)
h = q("""SELECT count(*) FROM commit_file cf WHERE cf.repo_id=%s""",(rid,))[0][0]
print("  EXPECT 6 commit_file rows on one file; GOT", h)

line("B. rename A->B then a NEW unrelated file created at A")
d2 = newrepo("readd")
write(d2,"A.txt","alpha content here\nmore\nlines\n"); addall(d2); commit(d2,"create A")
sh(["git","mv","A.txt","B.txt"], d2); commit(d2,"A -> B")
write(d2,"A.txt","completely unrelated brand new file\nzzz\n"); addall(d2); commit(d2,"new A (unrelated)")
write(d2,"A.txt","completely unrelated brand new file\nzzz\nqqq\n"); addall(d2); commit(d2,"touch new A")
write(d2,"B.txt","alpha content here\nmore\nlines\nextra\n"); addall(d2); commit(d2,"touch B")
m2 = bare_mirror(d2)
print("  git says:")
for c in iter_commits(m2, rev="HEAD"):
    print("    ", c.subject, [(f.change_type,f.path,f.old_path) for f in c.files])
rid2 = fresh_repo("readd")
st2,_,_,_ = ingest(rid2, m2)
print("  stats:", st2)
dump(rid2, "re-add")
print("  EXPECT 2 file rows (A and B); GOT", q("SELECT count(*) FROM file WHERE repo_id=%s",(rid2,))[0][0])

line("C. two DIFFERENT files renamed INTO the same path")
d3 = newrepo("collide")
write(d3,"p.txt","p"*20+"\n"); addall(d3)
write(d3,"q.txt","q"*20+"\n"); addall(d3); commit(d3,"create p and q")
sh(["git","mv","p.txt","t.txt"], d3); commit(d3,"p -> t")
sh(["git","rm","-q","t.txt"], d3); commit(d3,"delete t")
sh(["git","mv","q.txt","t.txt"], d3); commit(d3,"q -> t")
write(d3,"t.txt","q"*20+"\nmore\n"); addall(d3); commit(d3,"touch t")
m3 = bare_mirror(d3)
print("  git says:")
for c in iter_commits(m3, rev="HEAD"):
    print("    ", c.subject, [(f.change_type,f.path,f.old_path) for f in c.files])
rid3 = fresh_repo("collide")
st3,_,_,_ = ingest(rid3, m3)
print("  stats:", st3)
dump(rid3, "collide")

line("D. alias table integrity: self-alias / cycles / dangling")
for r,lbl in ((rid,"A"),(rid2,"B"),(rid3,"C")):
    bad_self = q("""SELECT a.old_path, a.file_id FROM file_alias a JOIN file f ON f.id=a.file_id
                    WHERE a.repo_id=%s AND f.path = a.old_path""",(r,))
    dangling = q("""SELECT a.old_path, a.file_id FROM file_alias a
                    LEFT JOIN file f ON f.id=a.file_id
                    WHERE a.repo_id=%s AND f.id IS NULL""",(r,))
    shadow = q("""SELECT a.old_path FROM file_alias a JOIN file f
                  ON f.repo_id=a.repo_id AND f.path=a.old_path
                  WHERE a.repo_id=%s AND f.id <> a.file_id""",(r,))
    print(f"  repo {lbl}: self-alias={bad_self} dangling={dangling} alias-shadows-live-file={shadow}")
